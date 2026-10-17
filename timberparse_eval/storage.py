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
from evaluation import inventory, scores

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
                "pdf_sha256": digest(pdf_bytes), "demo": demo}
        atomic(folder / "project.json", meta)
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
        inv = inventory(data, schema_map, read(folder / "exclusions.json"))
        review_path = folder / "annotations" / stage / "reviews.json"
        review = read(review_path) if review_path.exists() else {"revision": 0, "fields": {}, "members": {}, "missing": [], "markings": [], "complete_groups": [], "reviewer": ""}
        return {"metadata": read(folder / "run_metadata.json"), "inventory": inv, "review": review,
                "scores": scores(inv, review), "collections": catalog(schema_map),
                "stages": [s for s in ["raw", "reconciled"] if (folder / f"extraction_{s}.json").exists()]}

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
            writer.writerow(["run", "stage", "revision", "field_id", "member_id", "presence", "prediction", "verdict", "correction", "original_flag", "review_flag", "note", "reviewer"])
            def safe(v):
                value = str(v)
                return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value
            for f in view["inventory"]["fields"]:
                a = r["fields"].get(f["id"], {})
                writer.writerow([safe(x) for x in [rid, stage, r["revision"], f["id"], f["member_id"], f["presence"], json.dumps(f["value"], ensure_ascii=False), a.get("verdict", "unreviewed"), a.get("correction", ""), f["flag"], a.get("flag", "original"), a.get("note", ""), a.get("reviewer", r.get("reviewer", ""))]])
            atomic(folder / "review_export.csv", out.getvalue())
            atomic(folder / "summary.txt", "TimberParse Eval\nRun: " + rid + "\nStage: " + stage + "\nAnnotation revision: " + str(r["revision"]) + "\n\n" + json.dumps(view["scores"], indent=2))
            return folder
