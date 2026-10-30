import asyncio
import copy
import os
import time

from dotenv import load_dotenv
from baseline import APP, SYSTEMS, USER_PROMPT, resolver_module
from storage import atomic, now, read
from evaluation import run_totals


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def provider():
    load_dotenv(APP / ".env", override=False)
    choice = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if choice in {"openrouter", "google"}:
        return choice
    return "openrouter" if os.environ.get("OPENROUTER_API_KEY") and not (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")) else "google"


def configuration():
    # Read only the backend's normal environment file; never expose its contents.
    load_dotenv(APP / ".env", override=False)
    if provider() == "openrouter":
        return {"provider": "openrouter", "model": os.environ.get("OPENROUTER_MODEL", ""),
                "lite_model": os.environ.get("OPENROUTER_LITE_MODEL", ""),
                "has_key": bool(os.environ.get("OPENROUTER_API_KEY"))}
    return {"provider": "google", "model": os.environ.get("GEMINI_MODEL_NAME", ""),
            "lite_model": os.environ.get("GEMINI_LITE_MODEL_NAME", "gemma-4-31b-it"),
            "has_key": bool(os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))}


def inline_schema(schema):
    """Inline $defs/$ref so every provider behind OpenRouter receives one self-contained schema."""
    defs = schema.get("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


class OpenRouterError(Exception):
    pass


async def openrouter_json(http, model, system_prompt, user_text, schema, name, pdf_b64=None, filename="drawing.pdf"):
    """One schema-constrained chat completion on OpenRouter. Returns (parsed_json, raw_response)."""
    content = [{"type": "text", "text": user_text}]
    if pdf_b64 is not None:
        content.append({"type": "file", "file": {"filename": filename, "file_data": "data:application/pdf;base64," + pdf_b64}})
    body = {"model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": content}],
            "response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": inline_schema(schema)}},
            # Only route to endpoints that honour the schema; send the PDF to the model itself, not a text converter.
            "provider": {"require_parameters": True},
            "plugins": [{"id": "file-parser", "pdf": {"engine": "native"}}],
            "usage": {"include": True}}
    response = await http.post(OPENROUTER_URL, json=body, headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"],
                                                                 "X-Title": "TimberParse Eval"})
    try:
        data = response.json()
    except ValueError:
        raise OpenRouterError(f"HTTP {response.status_code}: non-JSON response")
    if response.status_code != 200 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        raise OpenRouterError(f"HTTP {response.status_code}: {str(err.get('message', 'request failed'))[:300]}")
    import json
    text = data["choices"][0]["message"].get("content") or ""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    return json.loads(text), data


async def execute_openrouter(store, pid, rid, meta, folder, started):
    import base64
    import httpx
    raw = {}
    pdf_b64 = base64.b64encode((store.project(pid) / "drawings/input.pdf").read_bytes()).decode()
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=30)) as http:
        async def one(system, cls):
            tick = time.monotonic()
            try:
                parsed, data = await openrouter_json(http, meta["model"], (folder / "prompts" / (system + ".txt")).read_text(encoding="utf-8"),
                                                     USER_PROMPT, cls.model_json_schema(), system, pdf_b64)
                atomic(folder / "responses" / (system + ".json"), data)
                raw[system] = cls.model_validate(parsed).model_dump(mode="json")
                return {"system": system, "status": "complete", "seconds": time.monotonic()-tick,
                        "served_by": data.get("provider"), "served_model": data.get("model"), "usage": data.get("usage")}
            except Exception as exc:
                message = str(exc) if isinstance(exc, OpenRouterError) else "Extraction failed; check key, model slug, credits, or response schema."
                return {"system": system, "status": "failed", "seconds": time.monotonic()-tick, "error_type": type(exc).__name__, "message": message}

        calls = await asyncio.gather(*(one(s, cls) for s, (cls, _) in SYSTEMS.items()))
        atomic(folder / "extraction_raw.json", raw)
        meta["calls"] = calls
        meta["status"] = "complete" if all(c["status"] == "complete" for c in calls) else "partial" if raw else "failed"
        if meta["reconcile"] and raw:
            ns = resolver_module()
            linked = copy.deepcopy(raw)
            tasks = ns["collect_link_tasks"](linked)
            model = os.environ.get("OPENROUTER_LITE_MODEL") or meta["model"]
            atomic(folder / "prompts/resolver.txt", ns["SYSTEM_PROMPT"])
            atomic(folder / "resolver_schema.json", ns["LinkResolution"].model_json_schema())
            atomic(folder / "resolver_tasks.json", tasks)
            meta["resolver"] = {"model": model, "task_count": len(tasks), "status": "not_needed"}
            try:
                if tasks:
                    import json
                    tick = time.monotonic()
                    parsed, data = await openrouter_json(http, model, ns["SYSTEM_PROMPT"], json.dumps(tasks, indent=1),
                                                         ns["LinkResolution"].model_json_schema(), "link_resolution")
                    atomic(folder / "responses/resolver.json", data)
                    decisions = ns["LinkResolution"].model_validate(parsed).decisions
                    applied = ns["apply_link_decisions"](linked, decisions, tasks)
                    atomic(folder / "resolver_changes.json", applied)
                    meta["resolver"].update(status="complete", changes=len(applied), served_by=data.get("provider"),
                                            served_model=data.get("model"), usage=data.get("usage"), seconds=time.monotonic()-tick)
                atomic(folder / "extraction_reconciled.json", linked)
            except Exception as exc:
                meta["resolver"].update(status="failed", error_type=type(exc).__name__)
                atomic(folder / "extraction_reconciled.json", raw)


async def execute(store, pid, rid):
    folder = store.run(pid, rid)
    meta = read(folder / "run_metadata.json")
    meta.update(status="running", started=now())
    atomic(folder / "run_metadata.json", meta)
    started = time.monotonic()
    if meta.get("provider") == "openrouter":
        try:
            await execute_openrouter(store, pid, rid, meta, folder, started)
        except Exception as exc:
            meta.update(status="failed", error_type=type(exc).__name__, message="OpenRouter run failed. Check OPENROUTER_API_KEY, OPENROUTER_MODEL, credits, and network.")
        finally:
            meta.update(finished=now(), seconds=time.monotonic()-started)
            meta["totals"] = {k: v for k, v in run_totals(meta).items() if k != "calls"}
            atomic(folder / "run_metadata.json", meta)
        return
    from google import genai
    from google.genai import types
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
                    tick = time.monotonic()
                    response = await client.aio.models.generate_content(model=model, config=types.GenerateContentConfig(system_instruction=ns["SYSTEM_PROMPT"], response_schema=ns["LinkResolution"], response_mime_type="application/json"), contents=[json.dumps(tasks, indent=1)])
                    atomic(folder / "responses/resolver.json", response.model_dump(mode="json", exclude_none=True))
                    if not isinstance(response.parsed, ns["LinkResolution"]):
                        raise ValueError("No parsed resolver decisions")
                    applied = ns["apply_link_decisions"](linked, response.parsed.decisions, tasks)
                    atomic(folder / "resolver_changes.json", applied)
                    meta["resolver"].update(status="complete", changes=len(applied), seconds=time.monotonic()-tick,
                                            usage=response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None)
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
        meta["totals"] = {k: v for k, v in run_totals(meta).items() if k != "calls"}
        atomic(folder / "run_metadata.json", meta)


def run_background(store, pid, rid):
    try:
        asyncio.run(execute(store, pid, rid))
    except Exception as exc:
        folder = store.run(pid, rid)
        meta = read(folder / "run_metadata.json")
        meta.update(status="failed", finished=now(), error_type=type(exc).__name__, message="Local extraction adapter failed; verify installation and configuration.")
        atomic(folder / "run_metadata.json", meta)
