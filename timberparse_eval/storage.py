import csv
import io
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from baseline import APP, SYSTEMS, catalog, digest, excluded_fields, prompt, schemas
from collections import Counter
from evaluation import DEPTHS, effective_depth, field_type, inventory, load_depth_map, ratio, run_totals, scores
SPLITS = ("unassigned", "development", "test")

LOCK = threading.RLock()


def now():
    return datetime.now(timezone.utc).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)
    with temp.open("w", encoding="utf-8", newline="") as f:
        f.write(payload)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def new_id(prefix):
    return prefix + "_" + uuid.uuid4().hex[:16]


FIELD_COLUMNS = ["project", "split", "run", "run_kind", "model", "stage", "revision", "field_id", "field_type", "system", "member_id",
                 "member_verdict", "presence", "prediction", "verdict", "correction", "original_flag", "review_flag",
                 "effective_flag", "depth_default", "depth_override", "depth", "error_type", "note", "reviewer"]


def field_rows(project, view, rid, stage):
    """One row per inventoried field; the long format used for pooled analysis."""
    r = view["review"]
    meta = view["metadata"]
    for f in view["inventory"]["fields"]:
        a = r["fields"].get(f["id"], {})
        flag = a.get("flag", "original")
        verdict = a.get("verdict", "unreviewed")
        yield [project["id"], project.get("split", "unassigned"), rid, meta.get("kind", ""), meta.get("model", ""), stage, r["revision"],
               f["id"], f.get("type", field_type(f["id"])), f["system"], f["member_id"], r.get("members", {}).get(f["member_id"], ""),
               f["presence"], json.dumps(f["value"], ensure_ascii=False), verdict, a.get("correction", ""), f["flag"], flag,
               f["flag"] if flag == "original" else flag, f.get("depth") or "", a.get("depth") or "", effective_depth(f, r) or "",
               (a.get("error_type") or "") if verdict in {"incorrect", "unsupported"} else "", a.get("note", ""), a.get("reviewer", r.get("reviewer", ""))]


class Store:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def project(self, pid):
        if not re.fullmatch(r"project_[a-f0-9]{16}", pid):
            raise ValueError("Invalid project ID")
        p = self.root / pid
        if not (p / "project.json").exists():
            raise FileNotFoundError("Project not found in this results folder")
        return p

    def run(self, pid, rid):
        if not re.fullmatch(r"run_[a-f0-9]{16}", rid):
            raise ValueError("Invalid run ID")
        p = self.project(pid) / "runs" / rid
        if not (p / "run_metadata.json").exists():
            raise FileNotFoundError("Run not found")
        return p

    def projects(self):
        return sorted([read(p) for p in self.root.glob("project_*/project.json")], key=lambda p: p["created"], reverse=True)

    def create_project(self, name, pdf_bytes, pages, demo=False):
        pid = new_id("project")
        folder = self.root / pid
        (folder / "drawings").mkdir(parents=True)
        (folder / "drawings/input.pdf").write_bytes(pdf_bytes)
        meta = {"id": pid, "name": name[:160], "created": now(), "pages": pages, "pdf": "drawings/input.pdf",
                "pdf_sha256": digest(pdf_bytes), "demo": demo, "split": "unassigned"}
        atomic(folder / "project.json", meta)
        return meta

    def set_split(self, pid, split):
        if split not in SPLITS:
            raise ValueError("Split must be unassigned, development, or test")
        with LOCK:
            path = self.project(pid) / "project.json"
            meta = read(path)
            if meta.get("demo") and split != "unassigned":
                raise ValueError("Synthetic examples cannot be part of the dataset")
            meta["split"] = split
            atomic(path, meta)
            return meta

    def create_run(self, pid, kind, model=None, reconcile=False):
        project = self.project(pid)
        rid = new_id("run")
        folder = project / "runs" / rid
        folder.mkdir(parents=True)
        hashes = {}
        for key in SYSTEMS:
            text = prompt(key)
            atomic(folder / "prompts" / (key + ".txt"), text)
            hashes[key] = digest(text.encode())
        atomic(folder / "schemas.json", schemas())
        atomic(folder / "exclusions.json", excluded_fields())
        sources = ["taskiq_task/utils.py", "core/schema/schema.py", "core/utils/member_size.py", "services/matching/link_resolver.py"]
        source_hashes = {}
        for path in sources:
            payload = (APP / path).read_bytes()
            target = folder / "baseline_source" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            source_hashes[path] = digest(payload)
        meta = {"id": rid, "project_id": pid, "created": now(), "kind": kind, "model": model,
                "status": "queued" if kind == "live" else "complete", "reconcile": reconcile,
                "input_sha256": read(project / "project.json")["pdf_sha256"], "prompt_hashes": hashes,
                "source_hashes": source_hashes, "settings": {"response_mime_type": "application/json", "other_generation_settings": "provider defaults"},
                "schema_sha256": digest((folder / "schemas.json").read_bytes()),
                "baseline_note": "Imported schema classes and exact prompt files; local adapter replaces queue/database infrastructure."}
        atomic(folder / "run_metadata.json", meta)
        return meta

    def view(self, pid, rid, stage="raw"):
        if stage not in {"raw", "reconciled"}:
            raise ValueError("Invalid extraction stage")
        folder = self.run(pid, rid)
        target = folder / f"extraction_{stage}.json"
        if not target.exists():
            raise ValueError("This extraction stage is not available yet")
        data = read(target)
        schema_map = read(folder / "schemas.json")
        depth_map = load_depth_map()
        inv = inventory(data, schema_map, read(folder / "exclusions.json"), depth_map)
        review_path = folder / "annotations" / stage / "reviews.json"
        review = read(review_path) if review_path.exists() else {"revision": 0, "fields": {}, "members": {}, "missing": [], "markings": [], "complete_groups": [], "reviewer": ""}
        meta = read(folder / "run_metadata.json")
        return {"metadata": meta, "run_totals": run_totals(meta), "inventory": inv, "review": review,
                "scores": scores(inv, review), "collections": catalog(schema_map),
                "stages": [s for s in ["raw", "reconciled"] if (folder / f"extraction_{s}.json").exists()],
                "depth_levels": depth_map.get("levels", {}), "depth_map_version": depth_map.get("version")}

    def save_review(self, pid, rid, stage, review):
        with LOCK:
            view = self.view(pid, rid, stage)
            if review.get("revision") != view["review"]["revision"]:
                raise RuntimeError("This review changed in another window. Reload before editing.")
            review["revision"] += 1
            review["updated"] = now()
            folder = self.run(pid, rid) / "annotations" / stage
            atomic(folder / "history" / f"revision_{review['revision']:06d}.json", review)
            atomic(folder / "reviews.json", review)
            self.export(pid, rid, stage)
            return review

    def export(self, pid, rid, stage):
        with LOCK:
            view = self.view(pid, rid, stage)
            folder = self.run(pid, rid) / "annotations" / stage
            r = view["review"]
            atomic(folder / "missing_members.json", r["missing"])
            atomic(folder / "pdf_markings.json", r["markings"])
            atomic(folder / "scores.json", {"annotation_revision": r["revision"], "stage": stage, **view["scores"]})
            out = io.StringIO(newline="")
            writer = csv.writer(out)
            writer.writerow(FIELD_COLUMNS)
            def safe(v):
                value = str(v)
                return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            for row in field_rows(read(self.project(pid) / "project.json"), view, rid, stage):
                writer.writerow([safe(x) for x in row])
            atomic(folder / "review_export.csv", out.getvalue())
            atomic(folder / "summary.txt", "TimberParse Eval\nRun: " + rid + "\nStage: " + stage + "\nAnnotation revision: " + str(r["revision"]) + "\n\n" + json.dumps(view["scores"], indent=2))
            return folder


def pool(views):
    """Pool reviewed fields from several (project, run) views into one score table.

    Field metrics use every field once; member precision/recall sum the
    present/extra/duplicate/missed counts of completed collections only.
    """
    fields, members, review = [], [], {"fields": {}, "members": {}}
    member_totals = {}
    for key, view in views:
        for f in view["inventory"]["fields"]:
            g = dict(f, id=key + ":" + f["id"], member_id=key + ":" + f["member_id"])
            fields.append(g)
            if f["id"] in view["review"]["fields"]:
                review["fields"][g["id"]] = view["review"]["fields"][f["id"]]
        for mid, verdict in view["review"].get("members", {}).items():
            review["members"][key + ":" + mid] = verdict
        for group, g in view["scores"]["member_groups"].items():
            if not g["complete"]:
                continue
            t = member_totals.setdefault(group, Counter())
            t.update({"present": g["counts"].get("present", 0), "extra": g["counts"].get("extra", 0),
                      "duplicate": g["counts"].get("duplicate", 0), "missed": g["missing"], "runs": 1})
    result = scores({"fields": fields, "members": members}, review)
    result.pop("member_groups", None)
    result.pop("member_counts", None)
    total = Counter()
    groups = {}
    for group, t in sorted(member_totals.items()):
        total.update(t)
        tp, fp = t["present"], t["extra"] + t["duplicate"]
        groups[group] = {**t, "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + t["missed"])}
    tp, fp = total["present"], total["extra"] + total["duplicate"]
    result["members"] = {"by_collection": groups, "totals": dict(total),
                         "precision": ratio(tp, tp + fp), "recall": ratio(tp, tp + total["missed"])}
    return result


def dataset_views(store, split="test", stage="raw", runs="all"):
    """Every non-demo run of every project in a split, newest first per project."""
    chosen = []
    for project in store.projects():
        if project.get("demo") or (split != "all" and project.get("split", "unassigned") != split):
            continue
        metas = sorted([read(p) for p in store.project(project["id"]).glob("runs/run_*/run_metadata.json")], key=lambda m: m["created"], reverse=True)
        metas = [m for m in metas if m["status"] in {"complete", "partial"} and m["kind"] != "demo"]
        if runs == "latest":
            metas = metas[:1]
        for meta in metas:
            try:
                view = store.view(project["id"], meta["id"], stage)
            except ValueError:
                continue
            chosen.append((project, meta, view))
    return chosen


def dataset_summary(store, split="test", stage="raw", runs="all"):
    if split not in SPLITS + ("all",) or stage not in {"raw", "reconciled"} or runs not in {"all", "latest"}:
        raise ValueError("Invalid summary options")
    chosen = dataset_views(store, split, stage, runs)
    per_run = [{"project": p["id"], "project_name": p["name"], "split": p.get("split", "unassigned"), "run": m["id"],
                "kind": m["kind"], "model": m.get("model"), "status": m["status"], "annotation_revision": v["review"]["revision"],
                "partial_review": v["scores"]["partial"], "accuracy": v["scores"]["accuracy"],
                **{k: val for k, val in run_totals(m).items() if k != "calls"}} for p, m, v in chosen]
    pooled = pool([(p["id"] + "/" + m["id"], v) for p, m, v in chosen])
    depth_map = load_depth_map()
    live = [r for r in per_run if r["kind"] == "live" and r["calls_complete"]]
    def mean(key):
        vals = [r[key] for r in live if isinstance(r.get(key), (int, float))]
        return sum(vals) / len(vals) if vals else None
    resources = {"live_runs": len(live),
                 "total_cost_usd": sum(r["cost_usd"] for r in live if r["cost_usd"] is not None) if live else None,
                 "total_tokens": sum(r["total_tokens"] for r in live),
                 "mean_wall_seconds": mean("wall_seconds"), "mean_cost_usd": mean("cost_usd"),
                 "mean_input_tokens": mean("input_tokens"), "mean_output_tokens": mean("output_tokens"),
                 "mean_reasoning_tokens": mean("reasoning_tokens"),
                 "note": "Live model runs only (imported or demo runs have no timing or usage)."}
    return {"split": split, "stage": stage, "runs": runs, "generated": now(), "run_count": len(chosen), "resources": resources,
            "project_count": len({p["id"] for p, _, _ in chosen}), "depth_map_version": depth_map.get("version"),
            "per_run": per_run, "pooled": pooled,
            "warning": "Pooled over every selected run. Unreviewed fields are excluded; check partial_review before reporting."}


def dataset_csv(store, split="test", stage="raw", runs="all"):
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(FIELD_COLUMNS)
    for project, meta, view in dataset_views(store, split, stage, runs):
        for row in field_rows(project, view, meta["id"], stage):
            writer.writerow([("'" + str(x)) if str(x).startswith(("=", "+", "-", "@", "\t", "\r")) else str(x) for x in row])
    return out.getvalue()


RUN_COLUMNS = ["project", "project_name", "split", "run", "kind", "model", "status", "wall_seconds", "longest_call_seconds",
               "sum_call_seconds", "input_tokens", "output_tokens", "reasoning_tokens", "total_tokens", "cost_usd",
               "calls_complete", "calls_failed", "field_accuracy", "annotation_revision", "partial_review"]


def dataset_runs_csv(store, split="test", stage="raw", runs="all"):
    data = dataset_summary(store, split, stage, runs)
    out = io.StringIO(newline="")
    writer = csv.writer(out)
    writer.writerow(RUN_COLUMNS)
    for r in data["per_run"]:
        row = dict(r, field_accuracy=r["accuracy"]["value"])
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in RUN_COLUMNS])
    return out.getvalue()
