import argparse
import io
import json
import os
import threading
import uuid
import zipfile
from pathlib import Path

import pymupdf as fitz
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from baseline import catalog, deref, schemas
from evaluation import VERDICTS
from extract import configuration, run_background
from storage import LOCK, Store, atomic, now, read

HERE = Path(__file__).resolve().parent
store = Store(os.environ.get("TIMBERPARSE_RESULTS", str(HERE / "evaluation_results")))
app = FastAPI(title="TimberParse Eval", docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Local files and paid extraction must not be writable from arbitrary websites.
    host = request.url.hostname
    if host not in {"127.0.0.1", "localhost", "testserver"}:
        return JSONResponse({"detail": "Local connections only"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        return JSONResponse({"detail": "Cross-origin requests are disabled"}, status_code=403)
    if request.headers.get("sec-fetch-site") == "cross-site":
        return JSONResponse({"detail": "Cross-site requests are disabled"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(FileNotFoundError)
async def missing(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=404)


@app.exception_handler(RuntimeError)
async def conflict(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.get("/")
def index():
    return FileResponse(HERE / "static/index.html")


@app.get("/api/workspace")
def workspace():
    return {"root": str(store.root), "projects": store.projects(), "configuration": configuration()}


@app.post("/api/workspace")
def change_workspace(body: dict):
    global store
    path = Path(str(body.get("path", ""))).expanduser()
    if not path.is_absolute():
        raise ValueError("Enter an absolute folder path")
    with LOCK:
        for p in store.root.glob("project_*/runs/run_*/run_metadata.json"):
            if read(p)["status"] in {"queued", "running"}:
                raise ValueError("Wait for the active extraction before switching folders")
        store = Store(path)
    return workspace()


@app.post("/api/projects")
async def upload_project(file: UploadFile = File(...), name: str = Form("Untitled drawing set")):
    chunks, size = [], 0
    while chunk := await file.read(1024*1024):
        size += len(chunk)
        if size > 64*1024*1024:
            raise ValueError("PDF must be 64 MB or smaller")
        chunks.append(chunk)
    data = b"".join(chunks)
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass or not doc.page_count:
                raise ValueError("PDF is encrypted or empty")
            pages = doc.page_count
    except Exception:
        raise ValueError("Choose a readable, unencrypted PDF")
    return store.create_project(name.strip() or Path(file.filename or "Drawing").stem, data, pages)


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    folder = store.project(pid)
    return {**read(folder / "project.json"), "runs": sorted([read(p) for p in folder.glob("runs/run_*/run_metadata.json")], key=lambda r: r["created"], reverse=True)}


@app.get("/api/projects/{pid}/pdf")
def get_pdf(pid: str):
    return FileResponse(store.project(pid) / "drawings/input.pdf", media_type="application/pdf")


@app.get("/api/projects/{pid}/pages/{page}")
def page_image(pid: str, page: int, width: int = 1600):
    width = max(400, min(width, 3000))
    with fitz.open(store.project(pid) / "drawings/input.pdf") as doc:
        if page < 1 or page > len(doc):
            raise ValueError("Invalid page number")
        p = doc[page-1]
        scale = min(width / p.rect.width, 4000 / p.rect.height)
        pix = p.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
        return Response(pix.tobytes("png"), media_type="image/png")


@app.post("/api/projects/{pid}/extract")
def extract(pid: str, body: dict, background: BackgroundTasks):
    config = configuration()
    if not config["has_key"] or not config["model"]:
        raise ValueError("Set GOOGLE_API_KEY (or GEMINI_API_KEY) and GEMINI_MODEL_NAME in the backend .env or environment, then try again")
    with LOCK:
        project = store.project(pid)
        if any(read(p)["status"] in {"queued", "running"} for p in project.glob("runs/run_*/run_metadata.json")):
            raise ValueError("An extraction is already running for this project")
        meta = store.create_run(pid, "live", config["model"], bool(body.get("reconcile", False)))
        background.add_task(run_background, store, pid, meta["id"])
    return meta


def validate_import(data):
    if not isinstance(data, dict) or not data or set(data) - set(schemas()):
        raise ValueError("Use the original extraction object with supported system keys, e.g. roof_system, floor_system, wall")
    def check(value, raw, root, path):
        if value is None:
            return
        node = deref(raw, root)
        if node.get("type") == "object":
            if not isinstance(value, dict):
                raise ValueError(path + " must be an object")
            props = node.get("properties", {})
            if set(value) - set(props):
                raise ValueError(path + " contains fields outside the baseline schema")
            for key, v in value.items():
                check(v, props[key], root, path + "/" + key)
        elif node.get("type") == "array":
            if not isinstance(value, list):
                raise ValueError(path + " must be a list")
            for v in value:
                if deref(node["items"], root).get("type") == "object" and not isinstance(v, dict):
                    raise ValueError(path + " must contain member objects")
                check(v, node["items"], root, path)
    for s, obj in data.items():
        if not isinstance(obj, dict):
            raise ValueError(s + " must be an object")
        check(obj, schemas()[s], schemas()[s], s)


@app.post("/api/projects/{pid}/import")
def import_predictions(pid: str, body: dict):
    data = body.get("data")
    validate_import(data)
    with LOCK:
        meta = store.create_run(pid, "imported", str(body.get("model", "unknown (imported)"))[:160])
        meta["baseline_note"] = "Imported output: original prompt/model provenance is unverified. Schema snapshot is this local codebase, not evidence of the source run configuration."
        folder = store.run(pid, meta["id"])
        atomic(folder / "run_metadata.json", meta)
        atomic(folder / "extraction_raw.json", data)
    return meta


@app.get("/api/projects/{pid}/runs/{rid}")
def get_run(pid: str, rid: str, stage: str = "raw"):
    with LOCK:
        return store.view(pid, rid, stage)


def validate_review(view, review, pages):
    if not isinstance(review, dict):
        raise ValueError("Invalid review")
    field_ids = {f["id"] for f in view["inventory"]["fields"]}
    member_ids = {m["id"] for m in view["inventory"]["members"] if m["countable"]}
    if set(review.get("fields", {})) - field_ids or set(review.get("members", {})) - member_ids:
        raise ValueError("Review refers to unknown fields or members")
    for r in review.get("fields", {}).values():
        if r.get("verdict", "unreviewed") not in VERDICTS or r.get("flag", "original") not in {"original", "flagged", "unflagged", "unknown"}:
            raise ValueError("Invalid review verdict or flag")
    if any(v not in {"unreviewed", "present", "extra", "duplicate"} for v in review.get("members", {}).values()):
        raise ValueError("Invalid member verdict")
    groups = set(view["collections"])
    if set(review.get("complete_groups", [])) - groups:
        raise ValueError("Unknown collection")
    for group in review.get("complete_groups", []):
        if any(review.get("members", {}).get(m["id"], "unreviewed") == "unreviewed" for m in view["inventory"]["members"] if m["countable"] and m["group"] == group):
            raise ValueError("Mark every predicted member present, extra, or duplicate before completing its inventory")
    missing_ids = set()
    for m in review.get("missing", []):
        if not m.get("id") or m["id"] in missing_ids or m.get("group") not in groups:
            raise ValueError("Invalid missed member")
        missing_ids.add(m["id"])
        s, c = m["group"].split("/")
        validate_import({s: {c: [m.get("values", {})]}})
    ids = field_ids | missing_ids
    mark_ids = set()
    for mark in review.get("markings", []):
        if not mark.get("id") or mark["id"] in mark_ids or mark.get("target") not in ids:
            raise ValueError("Invalid marking target")
        mark_ids.add(mark["id"])
        if not isinstance(mark.get("page"), int) or not 1 <= mark["page"] <= pages:
            raise ValueError("Invalid marking page")
        rect = mark.get("rect")
        if not isinstance(rect, list) or len(rect) != 4 or not all(isinstance(x, (int, float)) and 0 <= x <= 1 for x in rect) or rect[0] >= rect[2] or rect[1] >= rect[3]:
            raise ValueError("Markings must be normalized nonempty rectangles")


@app.put("/api/projects/{pid}/runs/{rid}/review")
def save_review(pid: str, rid: str, body: dict, stage: str = "raw"):
    with LOCK:
        view = store.view(pid, rid, stage)
        validate_review(view, body, read(store.project(pid) / "project.json")["pages"])
        saved = store.save_review(pid, rid, stage, body)
        return {"revision": saved["revision"], "updated": saved["updated"], "scores": store.view(pid, rid, stage)["scores"]}


@app.get("/api/projects/{pid}/runs/{rid}/export")
def export_run(pid: str, rid: str, stage: str = "raw"):
    with LOCK:
        store.export(pid, rid, stage)
        project = store.project(pid)
        run = store.run(pid, rid)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(project / "project.json", f"{pid}/project.json")
            z.write(project / "drawings/input.pdf", f"{pid}/drawings/input.pdf")
            for p in run.rglob("*"):
                if p.is_file() and not p.name.endswith(".tmp"):
                    z.write(p, f"{pid}/" + p.relative_to(project).as_posix())
        return Response(output.getvalue(), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{pid}_{rid}.zip"'})


@app.post("/api/demo")
def demo():
    # Synthetic instructional fixture, never reported as research evidence.
    doc = fitz.open()
    p = doc.new_page(width=900, height=600)
    p.insert_text((40, 45), "SYNTHETIC DEMONSTRATION - NOT RESEARCH DATA", fontsize=20)
    p.insert_text((40, 75), "Roof framing review practice | 1 page | Not for construction", fontsize=12)
    for y in [180, 240, 300, 360]:
        p.draw_line((120, y), (740, y), color=(0.12, 0.3, 0.4), width=3)
    p.draw_rect(fitz.Rect(110, 150, 750, 390), color=(0.12, 0.3, 0.4), width=2)
    p.insert_text((130, 130), "R1: 2x8 DF-L No.2 rafters at 16 in o.c. | Clear span 12 ft", fontsize=14)
    p.insert_text((130, 425), "R2: 2x6 DF-L No.2 rafters at 24 in o.c. | Clear span 10 ft", fontsize=14)
    p.insert_text((40, 480), "R2 is intentionally omitted from the sample extraction. R1 is duplicated.", fontsize=12)
    p.insert_text((40, 510), "Roof pitch and support bearing are not specified. A pitch of 6/12 is unsupported.", fontsize=12)
    data = doc.tobytes()
    doc.close()
    project = store.create_project("Demo - synthetic timber plan", data, 1, demo=True)
    predicted = {"roof_system": {"roof_rafters": [{"zone": "R1", "size": "2x8", "number_of_plies": 1,
        "lumber_spec": {"species": "DF-L", "grade": "No.2", "species_grade_note": ""},
        "clear_span_ft": 14, "clear_span_note": "Span unclear", "spacing_in": 20, "roof_pitch": "6/12",
        "uncertain_areas": ["clear_span_ft", "roof_pitch"]}, {"zone": "R1 duplicate", "size": "2x8", "uncertain_areas": []}],
        "ceiling_joists": [], "ridge_beams": [], "hip_valley_rafters": [], "roof_drop_beams": [], "roof_flush_beams": []}}
    meta = store.create_run(project["id"], "demo", "synthetic fixture (no model call)")
    atomic(store.run(project["id"], meta["id"]) / "extraction_raw.json", predicted)
    return {"project": project, "run": meta}


app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")


def main():
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", default=str(store.root))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    global_store = Store(args.results)
    globals()["store"] = global_store
    # A prior process cannot still own jobs when starting this single-worker app.
    for path in global_store.root.glob("project_*/runs/run_*/run_metadata.json"):
        meta = read(path)
        if meta["status"] in {"queued", "running"}:
            meta.update(status="interrupted", message="Previous local process ended during extraction", finished=now())
            atomic(path, meta)
    print("TimberParse Eval: http://127.0.0.1:" + str(args.port))
    print("Results folder: " + str(global_store.root))
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
