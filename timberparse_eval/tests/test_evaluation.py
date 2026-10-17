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
