"""
link_resolver.py
LLM-assisted reconciliation of cross-references between independently
extracted components.

The extraction pipeline runs six isolated Gemini calls (roof, floor, footing,
post, wall, shear wall), so fields that reference a component from ANOTHER
call drift in wording: a strip footing's supported_element says
"Exterior bearing wall along south side" while the wall extraction labeled
that wall "South exterior bearing wall — 1st floor". The calc engine links
loads through these names, so a mismatch silently drops the load cascade.

This resolver runs ONCE, right after extraction (see taskiq_task/
data_extraction.py):
  1. collect_link_tasks   — gathers every cross-reference whose value does
                            not already exactly match a candidate label
  2. one batched call to a lightweight LLM decides which candidate (if any)
                            each reference means
  3. apply_link_decisions — rewrites the linking field to the target's exact
                            label and appends an audit note with the original

Guards: a decision is applied only if its target is verbatim in the
candidate list (no hallucinated labels) and confidence is high/medium.
Every failure path returns the data unchanged — the calc engine's heuristic
token matching (services.mapping._match_mark) remains as runtime fallback.

Links covered:
  - continuous_strip_footings[].supported_element -> stud wall zone / masonry wall_mark
  - pad_footings[].supported_element              -> post_mark
  - masonry_walls[].swl_id (participating)        -> shear wall swl_id
"""

import os
import re
import json
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel
from google.genai import types

from core.llm.clients import google_client_async
from services.utils.logger import get_logger

logger = get_logger(__name__)

# Cheap, fast model — link reconciliation is a trivial task for an LLM.
GEMINI_LITE_MODEL_NAME = os.environ.get("GEMINI_LITE_MODEL_NAME", "gemma-4-31b-it")


def _norm(text: Any) -> str:
    return re.sub(r"[\s\-_]+", "", str(text or "").strip().lower())


class LinkDecision(BaseModel):
    source_id: str
    target_label: Optional[str] = None   # exact label from candidates, or null
    confidence: Literal["high", "medium", "low"] = "low"
    reason: str = ""


class LinkResolution(BaseModel):
    decisions: List[LinkDecision]


SYSTEM_PROMPT = """You reconcile cross-references between structural plan components that were extracted by independent processes and therefore use slightly different wording for the same physical component.

You receive a JSON list of link tasks. Each task has:
- source_id: opaque id, echo it back unchanged
- kind: "footing_to_wall" (strip footing -> the wall it supports), "footing_to_post" (pad footing -> the post it supports), or "masonry_swl" (masonry shear wall -> the shear wall line id it belongs to)
- query: the reference text as extracted
- context: additional location text, may be empty
- candidates: the exact labels of the possible targets

For EVERY task, decide which single candidate refers to the SAME physical component as the query, or null if none plausibly does.

Rules:
- Match on marks (SW-1, P-1, MW-2), compass directions, room/area names, grid lines, and function. Word order and boilerplate ("bearing wall", "exterior") differences do not matter.
- A footing supports the LOWEST story of a wall — prefer the 1st-floor/lowest-story candidate when a wall exists on several stories.
- For masonry_swl: X-lines run along the building's long axis, Y-lines along the short axis; "SWL-A"/"Line 1" style names match by letter/number.
- target_label must be copied VERBATIM from the candidates list. Never invent, edit, or combine labels.
- confidence: "high" = shared mark or unambiguous location; "medium" = likely the same location; "low" = a guess. Use null + "low" rather than forcing a bad match.
- Return one decision per task, same order.

Return ONLY the JSON object matching the schema."""


def collect_link_tasks(extracted_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every cross-component reference whose value does not already exactly
    match a candidate label (after normalization). Already-exact links need
    no LLM."""
    tasks: List[Dict[str, Any]] = []

    wall_sys = extracted_data.get("wall") or {}
    if not isinstance(wall_sys, dict):
        wall_sys = {}
    stud_walls = [w for w in (wall_sys.get("stud_walls") or []) if isinstance(w, dict)]
    masonry_walls = [m for m in (wall_sys.get("masonry_walls") or []) if isinstance(m, dict)]

    wall_candidates: List[str] = [str(w.get("zone")) for w in stud_walls if w.get("zone")]
    wall_candidates += [str(m.get("wall_mark")) for m in masonry_walls if m.get("wall_mark")]
    post_candidates: List[str] = [
        str(p.get("post_mark"))
        for p in ((extracted_data.get("post") or {}).get("standalone_posts") or [])
        if isinstance(p, dict) and p.get("post_mark")
    ]
    swl_candidates = sorted({
        str(w.get("swl_id"))
        for w in ((extracted_data.get("shear_wall") or {}).get("shear_walls") or [])
        if isinstance(w, dict) and w.get("swl_id")
    })

    def _needs_llm(query: Any, candidates: List[str]) -> bool:
        q = _norm(query)
        return bool(q) and bool(candidates) and not any(_norm(c) == q for c in candidates)

    footing = extracted_data.get("footing") or {}
    if isinstance(footing, dict):
        for i, f in enumerate(footing.get("continuous_strip_footings") or []):
            if not isinstance(f, dict):
                continue
            query = f.get("supported_element") or f.get("location_description")
            if _needs_llm(query, wall_candidates):
                tasks.append({
                    "source_id": f"continuous_strip_footings[{i}]",
                    "kind": "footing_to_wall",
                    "query": str(query),
                    "context": str(f.get("location_description") or ""),
                    "candidates": wall_candidates,
                })
        for i, f in enumerate(footing.get("pad_footings") or []):
            if not isinstance(f, dict):
                continue
            query = f.get("supported_element") or f.get("location_description")
            if _needs_llm(query, post_candidates):
                tasks.append({
                    "source_id": f"pad_footings[{i}]",
                    "kind": "footing_to_post",
                    "query": str(query),
                    "context": str(f.get("location_description") or ""),
                    "candidates": post_candidates,
                })

    for i, m in enumerate(masonry_walls):
        if str(m.get("seismic_participation") or "").strip().lower() != "participating":
            continue
        query = m.get("swl_id")
        if _needs_llm(query, swl_candidates):
            tasks.append({
                "source_id": f"masonry_walls[{i}]",
                "kind": "masonry_swl",
                "query": str(query),
                "context": str(m.get("masonry_note") or ""),
                "candidates": swl_candidates,
            })
    return tasks


_ID_RE = re.compile(r"^(continuous_strip_footings|pad_footings|masonry_walls)\[(\d+)\]$")


def apply_link_decisions(
    extracted_data: Dict[str, Any],
    decisions: List[LinkDecision],
    tasks: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Rewrite linking fields in place per the accepted decisions.
    Returns a list of {source_id, old, new} for logging."""
    by_id = {t["source_id"]: t for t in tasks}
    applied: List[Dict[str, str]] = []
    for d in decisions:
        task = by_id.get(d.source_id)
        if task is None or not d.target_label or d.confidence == "low":
            continue
        # hallucination guard: target must be one of the offered candidates
        target = next(
            (c for c in task["candidates"] if _norm(c) == _norm(d.target_label)),
            None,
        )
        if target is None:
            logger.warning(
                f"link resolver: rejected '{d.target_label}' for {d.source_id} — not in candidate list"
            )
            continue
        m = _ID_RE.match(d.source_id)
        if not m:
            continue
        coll, idx = m.group(1), int(m.group(2))

        if coll == "masonry_walls":
            items = (extracted_data.get("wall") or {}).get("masonry_walls") or []
            field, note_field = "swl_id", "masonry_note"
        else:
            items = (extracted_data.get("footing") or {}).get(coll) or []
            field, note_field = "supported_element", "footing_note"
        if idx >= len(items) or not isinstance(items[idx], dict):
            continue

        item = items[idx]
        old = item.get(field)
        if _norm(old) == _norm(target):
            continue
        item[field] = target
        note = (item.get(note_field) or "").strip()
        audit = f"[link resolver: {field} '{old}' -> '{target}' ({d.confidence}) {d.reason}]".strip()
        item[note_field] = f"{note} {audit}".strip()
        applied.append({"source_id": d.source_id, "old": str(old), "new": str(target)})
    return applied


async def resolve_links(extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """Reconcile cross-component references with one lightweight LLM call.
    Mutates and returns extracted_data. Never raises — on any failure the
    data is returned unchanged and the calc engine's heuristic matching
    remains the fallback."""
    try:
        tasks = collect_link_tasks(extracted_data)
        if not tasks:
            return extracted_data

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_schema=LinkResolution,
            response_mime_type="application/json",
        )
        contents_list: List[Any] = [json.dumps(tasks, indent=1)]
        response = await google_client_async.models.generate_content(
            model=GEMINI_LITE_MODEL_NAME,
            config=config,
            contents=contents_list,
        )
        parsed = response.parsed
        if parsed is None or not isinstance(parsed, LinkResolution):
            logger.warning("link resolver: model returned no parseable decisions")
            return extracted_data

        applied = apply_link_decisions(extracted_data, parsed.decisions, tasks)
        for a in applied:
            logger.info(f"link resolver: {a['source_id']}: '{a['old']}' -> '{a['new']}'")
        logger.info(f"link resolver: resolved {len(applied)}/{len(tasks)} ambiguous links")
    except Exception as e:
        logger.error(f"link resolver failed — heuristic matching will be used: {e}")
    return extracted_data
