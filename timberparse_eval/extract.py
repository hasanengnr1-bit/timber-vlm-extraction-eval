import asyncio
import copy
import os
import time

from dotenv import load_dotenv
from baseline import APP, SYSTEMS, USER_PROMPT, resolver_module
from storage import atomic, now, read


def configuration():
    # Read only the backend's normal environment file; never expose its contents.
    load_dotenv(APP / ".env", override=False)
    return {"model": os.environ.get("GEMINI_MODEL_NAME", ""),
            "has_key": bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))}


async def execute(store, pid, rid):
    from google import genai
    from google.genai import types
    folder = store.run(pid, rid)
    meta = read(folder / "run_metadata.json")
    meta.update(status="running", started=now())
    atomic(folder / "run_metadata.json", meta)
    started = time.monotonic()
    client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
    uploaded = None
    raw = {}
    try:
        uploaded = await client.aio.files.upload(file=str(store.project(pid) / "drawings/input.pdf"), config={"mime_type": "application/pdf"})
        deadline = time.monotonic() + 300
        while uploaded.state and uploaded.state.name == "PROCESSING":
            if time.monotonic() > deadline:
                raise TimeoutError("Provider PDF processing timed out")
            await asyncio.sleep(2)
            uploaded = await client.aio.files.get(name=uploaded.name)
        if not uploaded.state or uploaded.state.name != "ACTIVE":
            raise ValueError("Provider could not process PDF")

        async def one(system, cls):
            tick = time.monotonic()
            try:
                response = await client.aio.models.generate_content(
                    model=meta["model"],
                    config=types.GenerateContentConfig(system_instruction=(folder / "prompts" / (system + ".txt")).read_text(encoding="utf-8"), response_schema=cls, response_mime_type="application/json"),
                    contents=[types.Part.from_uri(file_uri=uploaded.uri, mime_type="application/pdf"), USER_PROMPT])
                atomic(folder / "responses" / (system + ".json"), response.model_dump(mode="json", exclude_none=True))
                parsed = response.parsed
                if parsed is None:
                    raise ValueError("No schema-parsed response")
                parsed = cls.model_validate(parsed)
                raw[system] = parsed.model_dump(mode="json")
                return {"system": system, "status": "complete", "seconds": time.monotonic()-tick,
                        "usage": response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None}
            except Exception as exc:
                # Provider exception strings may contain request data or credentials.
                return {"system": system, "status": "failed", "seconds": time.monotonic()-tick, "error_type": type(exc).__name__, "message": "Extraction failed; check credentials, model access, quota, or response schema."}

        calls = await asyncio.gather(*(one(s, cls) for s, (cls, _) in SYSTEMS.items()))
        atomic(folder / "extraction_raw.json", raw)
        meta["calls"] = calls
        meta["status"] = "complete" if all(c["status"] == "complete" for c in calls) else "partial" if raw else "failed"
        if meta["reconcile"] and raw:
            ns = resolver_module()
            linked = copy.deepcopy(raw)
            tasks = ns["collect_link_tasks"](linked)
            model = os.environ.get("GEMINI_LITE_MODEL_NAME", "gemma-4-31b-it")
            atomic(folder / "prompts/resolver.txt", ns["SYSTEM_PROMPT"])
            atomic(folder / "resolver_schema.json", ns["LinkResolution"].model_json_schema())
            atomic(folder / "resolver_tasks.json", tasks)
            meta["resolver"] = {"model": model, "task_count": len(tasks), "status": "not_needed"}
            try:
                if tasks:
                    import json
                    response = await client.aio.models.generate_content(model=model, config=types.GenerateContentConfig(system_instruction=ns["SYSTEM_PROMPT"], response_schema=ns["LinkResolution"], response_mime_type="application/json"), contents=[json.dumps(tasks, indent=1)])
                    atomic(folder / "responses/resolver.json", response.model_dump(mode="json", exclude_none=True))
                    if not isinstance(response.parsed, ns["LinkResolution"]):
                        raise ValueError("No parsed resolver decisions")
                    applied = ns["apply_link_decisions"](linked, response.parsed.decisions, tasks)
                    atomic(folder / "resolver_changes.json", applied)
                    meta["resolver"].update(status="complete", changes=len(applied))
                atomic(folder / "extraction_reconciled.json", linked)
            except Exception as exc:
                meta["resolver"].update(status="failed", error_type=type(exc).__name__)
                # Preserve fallback as a distinct, visibly failed reconciliation stage.
                atomic(folder / "extraction_reconciled.json", raw)
    except Exception as exc:
        meta.update(status="failed", error_type=type(exc).__name__, message="Provider run failed. Check PDF, API credentials, configured model, and network.")
    finally:
        if uploaded:
            try:
                await client.aio.files.delete(name=uploaded.name)
                meta["provider_file_cleanup"] = "deleted"
            except Exception:
                meta["provider_file_cleanup"] = "failed; provider retention policy applies"
        try:
            await client.aio.aclose()
            client.close()
        except Exception:
            meta["client_cleanup"] = "failed"
        meta.update(finished=now(), seconds=time.monotonic()-started)
        atomic(folder / "run_metadata.json", meta)


def run_background(store, pid, rid):
    try:
        asyncio.run(execute(store, pid, rid))
    except Exception as exc:
        folder = store.run(pid, rid)
        meta = read(folder / "run_metadata.json")
        meta.update(status="failed", finished=now(), error_type=type(exc).__name__, message="Local extraction adapter failed; verify installation and configuration.")
        atomic(folder / "run_metadata.json", meta)
