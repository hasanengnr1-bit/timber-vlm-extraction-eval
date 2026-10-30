import copy
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

import server
from baseline import APP, SYSTEMS, catalog, prompt, resolver_module, schemas
from evaluation import inventory, scores
from storage import Store, atomic, read


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "store", Store(tmp_path / "results"))
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def sample(client):
    response = client.post("/api/demo")
    assert response.status_code == 200, response.text
    data = response.json()
    base = f"/api/projects/{data['project']['id']}/runs/{data['run']['id']}"
    return data, base, client.get(base).json()


def test_manual_denominators_and_unknown_flags():
    fields = [{"id": str(i), "member_id": "m", "flag": flag, "system": "roof_system"} for i, flag in enumerate(["flagged", "unflagged", "flagged", "unknown", "unflagged", "flagged", "flagged"])]
    review = {"fields": {str(i): {"verdict": v} for i, v in enumerate(["incorrect", "incorrect", "correct", "unsupported", "unreviewed", "indeterminate", "not_applicable"])}}
    result = scores({"fields": fields, "members": []}, review)
    assert result["accuracy"]["value"] == .25
    assert result["error_flag_recall"]["value"] == .5
    assert result["flag_precision"]["value"] == .5
    assert result["unflagged_error_rate"]["value"] == 1
    assert result["unknown_flag_eligible"] == 1
    assert result["counts"]["unreviewed"] == 1
    assert scores({"fields": [], "members": []}, {})["accuracy"]["value"] is None


def test_original_uncertainty_scope():
    data = {"roof_system": {"roof_rafters": [{"zone": "R1", "size": "2x8", "lumber_spec": {"species": "DF-L"}, "uncertain_areas": ["lumber_spec", "clear_span_ft"]}]}}
    inv = inventory(data, schemas())
    by_id = {f["id"]: f for f in inv["fields"]}
    prefix = "roof_system/roof_rafters/0/"
    assert by_id[prefix+"clear_span_ft"]["flag"] == "flagged"
    assert by_id[prefix+"size"]["flag"] == "unflagged"
    assert by_id[prefix+"lumber_spec/species"]["flag"] == "unknown"
    assert not any(f["name"].endswith("_note") or f["name"] == "uncertain_areas" for f in inv["fields"])
    data["roof_system"]["roof_rafters"][0]["uncertain_areas"] = None
    assert all(f["flag"] == "unknown" for f in inventory(data, schemas())["fields"])


def test_save_reopen_history_export_and_immutable_predictions(client, sample):
    data, base, view = sample
    review = view["review"]
    field = next(f["id"] for f in view["inventory"]["fields"] if f["name"] == "clear_span_ft")
    review["fields"][field] = {"verdict": "incorrect", "correction": "12", "note": "Dimension", "flag": "original"}
    review["markings"] = [{"id": "mark1", "target": field, "page": 1, "rect": [.1, .2, .4, .5]}]
    folder = server.store.run(data["project"]["id"], data["run"]["id"])
    before = (folder / "extraction_raw.json").read_bytes()
    result = client.put(base+"/review", json=review)
    assert result.status_code == 200, result.text
    assert result.json()["revision"] == 1
    reopened = Store(server.store.root).view(data["project"]["id"], data["run"]["id"])
    assert reopened["review"]["markings"] == review["markings"]
    assert reopened["review"]["fields"][field]["correction"] == "12"
    assert (folder / "extraction_raw.json").read_bytes() == before
    assert read(folder / "annotations/raw/history/revision_000001.json")["revision"] == 1
    assert client.put(base+"/review", json=review).status_code == 409
    zipped = client.get(base+"/export")
    assert zipped.status_code == 200
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as z:
        scorefile = next(n for n in z.namelist() if n.endswith("scores.json"))
        assert json.loads(z.read(scorefile))["accuracy"]["denominator"] == 1
        assert any(n.endswith("pdf_markings.json") for n in z.namelist())
        assert any(n.endswith("input.pdf") for n in z.namelist())


def test_member_recall_duplicates_and_complete_inventory(client, sample):
    data, base, view = sample
    review = view["review"]
    group = "roof_system/roof_rafters"
    review["complete_groups"] = [group]
    assert client.put(base+"/review", json=review).status_code == 400
    review["members"] = {group+"/0": "present", group+"/1": "duplicate"}
    review["missing"] = [{"id": "missing1", "group": group, "label": "R2", "values": {"size": "2x6"}}]
    review["fields"][group+"/1/size"] = {"verdict": "correct"}
    result = client.put(base+"/review", json=review)
    assert result.status_code == 200, result.text
    s = result.json()["scores"]
    assert s["member_groups"][group]["precision"]["value"] == .5
    assert s["member_groups"][group]["recall"]["value"] == .5
    assert s["accuracy"]["denominator"] == 0


def test_mark_validation_and_stage_isolation(client, sample):
    data, base, view = sample
    review = view["review"]
    review["markings"] = [{"id": "bad", "target": view["inventory"]["fields"][0]["id"], "page": 2, "rect": [.1,.1,.3,.3]}]
    assert client.put(base+"/review", json=review).status_code == 400
    folder = server.store.run(data["project"]["id"], data["run"]["id"])
    atomic(folder / "extraction_reconciled.json", read(folder / "extraction_raw.json"))
    assert client.get(base+"?stage=reconciled").json()["review"]["revision"] == 0
    assert client.get(base+"?stage=../../bad").status_code == 400


def test_pdf_render_import_and_cross_origin(client, sample):
    data, base, view = sample
    pid = data["project"]["id"]
    response = client.get(f"/api/projects/{pid}/pages/1")
    assert response.status_code == 200 and response.content.startswith(b"\x89PNG")
    assert client.get(f"/api/projects/{pid}/pages/0").status_code == 400
    assert client.post("/api/demo", headers={"Origin": "https://untrusted.example"}).status_code == 403
    assert client.post(f"/api/projects/{pid}/import", json={"data": {"new_task": {}}}).status_code == 400
    imported = client.post(f"/api/projects/{pid}/import", json={"data": {"roof_system": {"roof_rafters": [{"zone": "R1", "size": "2x8"}]}}})
    assert imported.status_code == 200
    assert imported.json()["kind"] == "imported"


def test_baseline_snapshots_and_resolver_reuse(client, sample):
    data, base, view = sample
    folder = server.store.run(data["project"]["id"], data["run"]["id"])
    for system in SYSTEMS:
        assert (folder / "prompts" / (system+".txt")).read_text(encoding="utf-8") == prompt(system)
    ns = resolver_module()
    data = {"wall": {"stud_walls": [{"zone": "South wall"}]}, "footing": {"continuous_strip_footings": [{"supported_element": "southern wall"}]}}
    tasks = ns["collect_link_tasks"](data)
    assert len(tasks) == 1
    decision = ns["LinkDecision"](source_id=tasks[0]["source_id"], target_label="Invented wall", confidence="high")
    assert ns["apply_link_decisions"](data, [decision], tasks) == []
    decision.target_label = "South wall"
    assert len(ns["apply_link_decisions"](data, [decision], tasks)) == 1


def test_live_adapter_with_mocked_provider(client, sample, monkeypatch):
    """Exercise the seven-call adapter without credentials, network, or cost."""
    import asyncio
    from types import SimpleNamespace
    from google import genai
    from extract import execute
    from baseline import USER_PROMPT
    data, _, _ = sample
    pid = data["project"]["id"]
    calls, deleted = [], []

    class Files:
        async def upload(self, **kwargs):
            return SimpleNamespace(name="files/mock", uri="https://example.invalid/mock", state=SimpleNamespace(name="ACTIVE"))
        async def delete(self, **kwargs):
            deleted.append(kwargs["name"])

    class Models:
        async def generate_content(self, **kwargs):
            calls.append(kwargs)
            cls = kwargs["config"].response_schema
            payload = {key: [] for key, prop in cls.model_json_schema()["properties"].items() if prop.get("type") == "array"}
            # FootingProjectInfo has required metadata; model_construct deliberately
            # bypasses provider validation but our adapter revalidates it and reports failure.
            if cls.__name__ == "FootingSystemData":
                raise RuntimeError("simulated provider failure")
            obj = cls.model_validate(payload)
            return SimpleNamespace(parsed=obj, usage_metadata=None, model_dump=lambda **kw: {"text": obj.model_dump_json()})

    class FakeClient:
        def __init__(self, **kwargs):
            async def close():
                pass
            self.aio = SimpleNamespace(files=Files(), models=Models(), aclose=close)
        def close(self):
            pass

    monkeypatch.setattr(genai, "Client", FakeClient)
    meta = server.store.create_run(pid, "live", "mock-model", False)
    asyncio.run(execute(server.store, pid, meta["id"]))
    folder = server.store.run(pid, meta["id"])
    recorded = read(folder / "run_metadata.json")
    assert len(calls) == 7
    assert recorded["status"] == "partial"
    assert recorded["provider_file_cleanup"] == "deleted"
    assert deleted == ["files/mock"]
    assert len(read(folder / "extraction_raw.json")) == 6
    assert {c["config"].system_instruction for c in calls} == {prompt(s) for s in SYSTEMS}
    assert all(c["contents"][1] == USER_PROMPT for c in calls)


def test_depth_error_type_coverage_and_selective_risk():
    fields = [{"id": f"r/z/{i}/f{i}", "member_id": "m", "flag": flag, "system": "roof_system", "depth": depth}
              for i, (flag, depth) in enumerate([("flagged", "explicit"), ("unflagged", "explicit"), ("unflagged", "derived"),
                                                 ("flagged", "inferred"), ("unflagged", "inferred"), ("unknown", None)])]
    review = {"fields": {
        "r/z/0/f0": {"verdict": "incorrect", "error_type": "perceptual"},
        "r/z/1/f1": {"verdict": "correct"},
        "r/z/2/f2": {"verdict": "incorrect", "error_type": "reasoning"},
        "r/z/3/f3": {"verdict": "unsupported", "error_type": "reasoning", "depth": "derived"},
        "r/z/4/f4": {"verdict": "correct", "error_type": "perceptual"},  # ignored: not an error
        "r/z/5/f5": {"verdict": "incorrect"}}}
    result = scores({"fields": fields, "members": []}, review)
    assert result["errors"] == 4
    # Known flag status: f0..f4 -> 3 unflagged of 5.
    assert result["coverage"]["value"] == 3 / 5
    assert result["selective_risk"]["value"] == 1 / 3
    assert result["by_error_type"]["perceptual"]["errors"] == 1
    assert result["by_error_type"]["perceptual"]["error_flag_recall"]["value"] == 1
    assert result["by_error_type"]["reasoning"]["errors"] == 2
    assert result["by_error_type"]["reasoning"]["error_flag_recall"]["value"] == .5
    assert result["by_error_type"]["unlabelled"]["errors"] == 1
    # Override moves f3 from inferred to derived.
    assert result["by_depth"]["explicit"]["accuracy"]["value"] == .5
    assert result["by_depth"]["derived"]["accuracy"]["denominator"] == 2
    assert result["by_depth"]["inferred"]["accuracy"]["value"] == 1
    assert result["by_depth"]["unlabelled"]["total"] == 1


def test_depth_map_covers_schema_and_assigns_field_types():
    from evaluation import DEPTHS, load_depth_map
    from baseline import excluded_fields
    depth_map = load_depth_map()
    data = {"roof_system": {"roof_rafters": [{"zone": "R1", "size": "2x8", "lumber_spec": {"species": "DF-L"}, "uncertain_areas": []}]}}
    inv = inventory(data, schemas(), excluded_fields(), depth_map)
    by_id = {f["id"]: f for f in inv["fields"]}
    assert by_id["roof_system/roof_rafters/0/size"]["type"] == "roof_system/roof_rafters/size"
    assert by_id["roof_system/roof_rafters/0/size"]["depth"] == "explicit"
    assert by_id["roof_system/roof_rafters/0/clear_span_ft"]["depth"] == "derived"
    assert by_id["roof_system/roof_rafters/0/lumber_spec/species"]["depth"] == "explicit"
    assert all(v["level"] in DEPTHS for v in depth_map["fields"].values())
    # Every scored field type in a full inventory has a level.
    full = {}
    for system, root in schemas().items():
        for key, raw in root.get("properties", {}).items():
            full.setdefault(system, {})[key] = [{}] if f"{system}/{key}" in catalog(schemas()) else {}
    missing = [f["type"] for f in inventory(full, schemas(), excluded_fields(), depth_map)["fields"] if f["depth"] is None]
    assert missing == []


def test_review_accepts_depth_and_error_type_and_rejects_bad_values(client, sample):
    data, base, view = sample
    review = view["review"]
    field = next(f["id"] for f in view["inventory"]["fields"] if f["name"] == "roof_pitch")
    review["fields"][field] = {"verdict": "unsupported", "error_type": "reasoning", "depth": "inferred", "flag": "original"}
    saved = client.put(base + "/review", json=review)
    assert saved.status_code == 200, saved.text
    assert saved.json()["scores"]["by_error_type"]["reasoning"]["errors"] == 1
    bad = dict(review, revision=1, fields={field: {"verdict": "incorrect", "error_type": "spatial"}})
    assert client.put(base + "/review", json=bad).status_code == 400
    bad["fields"][field] = {"verdict": "incorrect", "depth": "deep"}
    assert client.put(base + "/review", json=bad).status_code == 400
    csv_text = (server.store.run(data["project"]["id"], data["run"]["id"]) / "annotations/raw/review_export.csv").read_text()
    assert "field_type" in csv_text.splitlines()[0] and ",inferred,inferred,reasoning," in csv_text


def test_split_and_pooled_summary(client):
    first = client.post("/api/demo").json()
    second = client.post("/api/demo").json()
    # Demo projects stay out of the dataset.
    assert client.put(f"/api/projects/{first['project']['id']}/split", json={"split": "test"}).status_code == 400
    for item in (first, second):
        meta = server.store.project(item["project"]["id"]) / "project.json"
        project = read(meta)
        project["demo"] = False
        atomic(meta, project)
    assert client.put(f"/api/projects/{first['project']['id']}/split", json={"split": "test"}).json()["split"] == "test"
    assert client.put(f"/api/projects/{second['project']['id']}/split", json={"split": "development"}).status_code == 200
    assert client.put(f"/api/projects/{second['project']['id']}/split", json={"split": "holdout"}).status_code == 400
    # Demo runs are excluded too, so relabel the fixture runs as imported.
    for item in (first, second):
        path = server.store.run(item["project"]["id"], item["run"]["id"]) / "run_metadata.json"
        atomic(path, dict(read(path), kind="imported"))
    for item, verdict in ((first, "correct"), (second, "incorrect")):
        base = f"/api/projects/{item['project']['id']}/runs/{item['run']['id']}"
        view = client.get(base).json()
        review = view["review"]
        field = next(f["id"] for f in view["inventory"]["fields"] if f["name"] == "size")
        review["fields"][field] = {"verdict": verdict}
        members = [m for m in view["inventory"]["members"] if m["group"] == "roof_system/roof_rafters"]
        review["members"] = {members[0]["id"]: "present", members[1]["id"]: "duplicate"}
        review["missing"] = [{"id": "miss1", "group": "roof_system/roof_rafters", "values": {"zone": "R2"}}]
        review["complete_groups"] = ["roof_system/roof_rafters"]
        assert client.put(base + "/review", json=review).status_code == 200
    test = client.get("/api/summary?split=test").json()
    assert test["run_count"] == 1 and test["pooled"]["accuracy"]["value"] == 1
    assert test["pooled"]["members"]["precision"]["value"] == .5
    assert test["pooled"]["members"]["recall"]["value"] == .5
    both = client.get("/api/summary?split=all").json()
    assert both["run_count"] == 2 and both["pooled"]["accuracy"]["value"] == .5
    assert both["pooled"]["members"]["totals"]["missed"] == 2
    assert client.get("/api/summary?split=nope").status_code == 400
    csv_text = client.get("/api/summary.csv?split=all").text
    assert csv_text.splitlines()[0].startswith("project,split,run")
    assert sum(",test," in line for line in csv_text.splitlines()) > 0


def minimal(cls):
    from baseline import deref
    schema = cls.model_json_schema()
    out = {}
    for key in schema.get("required", []):
        node = deref(schema["properties"][key], schema)
        out[key] = [] if node.get("type") == "array" else {} if node.get("type") == "object" else None
    return cls.model_validate(out).model_dump(mode="json")


def test_openrouter_run_with_mocked_api(client, monkeypatch, tmp_path):
    import asyncio, httpx, extract
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/test-gemini")
    assert extract.configuration() == {"provider": "openrouter", "model": "google/test-gemini", "lite_model": "", "has_key": True}
    # Schemas sent to OpenRouter are self-contained.
    for cls, _ in SYSTEMS.values():
        assert "$ref" not in json.dumps(extract.inline_schema(cls.model_json_schema()))
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert request.headers["authorization"] == "Bearer test-key"
        assert body["model"] == "google/test-gemini"
        assert body["response_format"]["json_schema"]["strict"] is True
        assert body["provider"]["require_parameters"] is True
        part = body["messages"][1]["content"][1]
        assert part["type"] == "file" and part["file"]["file_data"].startswith("data:application/pdf;base64,")
        name = body["response_format"]["json_schema"]["name"]
        if name == "roof_system":
            return httpx.Response(500, json={"error": {"message": "upstream overloaded"}})
        cls = SYSTEMS[name][0]
        return httpx.Response(200, json={"id": "gen-1", "provider": "Google", "model": body["model"],
                                         "choices": [{"message": {"content": json.dumps(minimal(cls))}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler)))
    project = server.store.create_project("Mock set", client.post("/api/demo").json() and (server.store.project(server.store.projects()[0]["id"]) / "drawings/input.pdf").read_bytes(), 1)
    started = client.post(f"/api/projects/{project['id']}/extract", json={"reconcile": False})
    assert started.status_code == 200, started.text
    meta = read(server.store.run(project["id"], started.json()["id"]) / "run_metadata.json")
    assert meta["provider"] == "openrouter"
    assert meta["status"] == "partial", meta
    failed = [c for c in meta["calls"] if c["status"] == "failed"]
    assert [c["system"] for c in failed] == ["roof_system"] and "upstream overloaded" in failed[0]["message"]
    ok = [c for c in meta["calls"] if c["status"] == "complete"]
    assert len(ok) == 6 and ok[0]["served_by"] == "Google" and ok[0]["usage"]["cost"] == 0.001
    assert len(seen) == 7
    totals = meta["totals"]
    assert totals["calls_complete"] == 6 and totals["calls_failed"] == 1
    assert totals["input_tokens"] == 60 and totals["output_tokens"] == 30 and abs(totals["cost_usd"] - 0.006) < 1e-9
    assert totals["wall_seconds"] is not None
    view = client.get(f"/api/projects/{project['id']}/runs/{started.json()['id']}").json()
    assert view["run_totals"]["calls"][0]["input_tokens"] == 10
    runs_csv = client.get("/api/summary_runs.csv?split=all").text
    assert runs_csv.splitlines()[0].startswith("project,project_name,split,run,kind,model,status,wall_seconds")


def test_usage_normalisation_for_both_providers():
    from evaluation import call_usage, run_totals
    o = call_usage({"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140, "cost": 0.5,
                    "completion_tokens_details": {"reasoning_tokens": 30}})
    assert o == {"input_tokens": 100, "output_tokens": 40, "reasoning_tokens": 30, "total_tokens": 140, "cost_usd": 0.5}
    g = call_usage({"prompt_token_count": 100, "candidates_token_count": 10, "thoughts_token_count": 30, "total_token_count": 140})
    assert g == {"input_tokens": 100, "output_tokens": 40, "reasoning_tokens": 30, "total_tokens": 140, "cost_usd": None}
    meta = {"seconds": 90, "calls": [{"system": "roof_system", "status": "complete", "seconds": 80, "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "cost": .1}},
                                     {"system": "wall", "status": "failed", "seconds": 5}],
            "resolver": {"status": "complete", "seconds": 4, "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": .01}}}
    t = run_totals(meta)
    assert t["wall_seconds"] == 90 and t["longest_call_seconds"] == 80 and t["total_tokens"] == 5
    assert abs(t["cost_usd"] - .11) < 1e-9 and t["cost_complete"] and t["calls_failed"] == 1
    assert [c["system"] for c in t["calls"]] == ["roof_system", "wall", "link_resolver"]
    assert run_totals({})["calls"] == [] and run_totals({})["cost_usd"] is None
