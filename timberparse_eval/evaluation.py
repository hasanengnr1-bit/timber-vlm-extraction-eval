"""Field inventory and denominator-explicit manual evaluation. No model judge."""
import json
import re
from collections import Counter
from pathlib import Path
from baseline import catalog, deref

VERDICTS = {"unreviewed", "correct", "incorrect", "unsupported", "indeterminate", "not_applicable"}
ERRORS = {"incorrect", "unsupported"}
DEPTHS = ("explicit", "derived", "inferred")
ERROR_TYPES = ("perceptual", "reasoning")
DEPTH_MAP_PATH = Path(__file__).resolve().parent / "depth_map.json"


def load_depth_map(path=DEPTH_MAP_PATH):
    """Field type -> inference-depth level. Missing file means every field is unlabelled."""
    path = Path(path)
    if not path.exists():
        return {"fields": {}, "levels": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if any(v.get("level") not in DEPTHS for v in data.get("fields", {}).values()):
        raise ValueError("depth_map.json contains an unknown level")
    return data


def field_type(field_id):
    """Field path without member indices: roof_system/roof_rafters/0/size -> roof_system/roof_rafters/size."""
    return "/".join(part for part in field_id.split("/") if not re.fullmatch(r"\d+", part))


def inventory(data, schema_map, excluded=None, depth_map=None):
    excluded = excluded or {}
    depth_fields = (depth_map or {}).get("fields", {})
    members, fields = [], []

    def walk(obj, node, root, prefix, member_id, inherited_unknown=False):
        node = deref(node, root)
        obj = obj if isinstance(obj, dict) else {}
        uncertainty = obj.get("uncertain_areas")
        for key, raw in node.get("properties", {}).items():
            if key == "uncertain_areas" or key.endswith("_note") or key in excluded.get(node.get("title"), []):
                continue
            child = deref(raw, root)
            path = prefix + "/" + key
            direct_flag = isinstance(uncertainty, list) and key in uncertainty
            if child.get("type") == "object":
                walk(obj.get(key), child, root, path, member_id, inherited_unknown or direct_flag)
                continue
            flag = "flagged" if direct_flag else ("unflagged" if isinstance(uncertainty, list) and not inherited_unknown else "unknown")
            ftype = field_type(path)
            fields.append({"id": path, "member_id": member_id, "name": key, "type": ftype,
                           "depth": depth_fields.get(ftype, {}).get("level"),
                           "value": obj.get(key), "presence": "omitted" if key not in obj else "null" if obj[key] is None else "value",
                           "flag": flag, "system": path.split("/")[0], "description": raw.get("description", "")})

    collections = catalog(schema_map)
    for system, root in schema_map.items():
        if system not in data:
            continue
        source = data[system]
        for key, raw in root.get("properties", {}).items():
            group = f"{system}/{key}"
            if group in collections:
                for index, member in enumerate(source.get(key) or []):
                    mid = f"{group}/{index}"
                    label = next((str(member[k]) for k in ["zone", "beam_mark", "post_mark", "wall_mark", "footing_mark", "swl_id", "name", "mark"] if member.get(k)), f"{key} {index+1}")
                    members.append({"id": mid, "group": group, "system": system, "label": label, "data": member, "countable": True})
                    walk(member, collections[group]["schema"], root, mid, mid)
            else:
                node = deref(raw, root)
                if node.get("type") == "object":
                    members.append({"id": group, "group": group, "system": system, "label": key, "data": source.get(key), "countable": False})
                    walk(source.get(key), node, root, group, group)
    return {"members": members, "fields": fields}


def ratio(n, d):
    return {"value": n / d if d else None, "numerator": n, "denominator": d}


def effective_depth(field, review):
    override = review.get("fields", {}).get(field["id"], {}).get("depth")
    return override if override in DEPTHS else field.get("depth")


def scores(inv, review):
    def field_scores(fields):
        rows = []
        error_types = []
        excluded_members = 0
        for f in fields:
            if review.get("members", {}).get(f["member_id"], "unreviewed") in {"extra", "duplicate"}:
                excluded_members += 1
                continue
            r = review.get("fields", {}).get(f["id"], {})
            verdict = r.get("verdict", "unreviewed")
            flag = r.get("flag", "original") if r.get("flag", "original") != "original" else f["flag"]
            rows.append((verdict, flag))
            if verdict in ERRORS:
                error_types.append((r.get("error_type") if r.get("error_type") in ERROR_TYPES else "unlabelled", flag))
        counts = Counter(v for v, _ in rows)
        eligible = [(v, flag) for v, flag in rows if v in ERRORS | {"correct"}]
        known = [(v, flag) for v, flag in eligible if flag != "unknown"]
        flagged = [(v, flag) for v, flag in known if flag == "flagged"]
        unflagged = [(v, flag) for v, flag in known if flag == "unflagged"]
        errors = sum(v in ERRORS for v, _ in known)
        flagged_errors = sum(v in ERRORS for v, _ in flagged)
        by_error_type = {}
        for t in ERROR_TYPES + ("unlabelled",):
            these = [flag for et, flag in error_types if et == t]
            known_t = [flag for flag in these if flag != "unknown"]
            by_error_type[t] = {"errors": len(these),
                                "error_flag_recall": ratio(sum(flag == "flagged" for flag in known_t), len(known_t))}
        return {"counts": {v: counts[v] for v in sorted(VERDICTS)}, "total": len(rows),
                "errors": sum(v in ERRORS for v, _ in eligible),
                "by_error_type": by_error_type,
                # Coverage = share of eligible fields the flags would let through without review;
                # selective risk = error rate among those accepted (unflagged) fields.
                "coverage": ratio(len(unflagged), len(known)),
                "selective_risk": ratio(sum(v in ERRORS for v, _ in unflagged), len(unflagged)),
                "excluded_extra_duplicate_fields": excluded_members,
                "accuracy": ratio(counts["correct"], len(eligible)),
                "flag_precision": ratio(flagged_errors, len(flagged)),
                "error_flag_recall": ratio(flagged_errors, errors),
                "unflagged_error_rate": ratio(sum(v in ERRORS for v, _ in unflagged), len(unflagged)),
                "unknown_flag_eligible": len(eligible)-len(known),
                "partial": counts["unreviewed"] > 0}

    result = field_scores(inv["fields"])
    result["by_system"] = {s: field_scores([f for f in inv["fields"] if f["system"] == s]) for s in sorted({f["system"] for f in inv["fields"]})}
    depth_of = {f["id"]: effective_depth(f, review) for f in inv["fields"]}
    result["by_depth"] = {d: field_scores([f for f in inv["fields"] if depth_of[f["id"]] == d]) for d in DEPTHS}
    result["by_depth"]["unlabelled"] = field_scores([f for f in inv["fields"] if depth_of[f["id"]] not in DEPTHS])
    groups = set(m["group"] for m in inv["members"] if m["countable"]) | set(review.get("complete_groups", [])) | {m["group"] for m in review.get("missing", [])}
    result["member_groups"] = {}
    totals = Counter()
    for group in sorted(groups):
        members = [m for m in inv["members"] if m["countable"] and m["group"] == group]
        counts = Counter(review.get("members", {}).get(m["id"], "unreviewed") for m in members)
        missing = sum(m["group"] == group for m in review.get("missing", []))
        complete = group in review.get("complete_groups", []) and not counts["unreviewed"]
        tp, fp = counts["present"], counts["extra"] + counts["duplicate"]
        result["member_groups"][group] = {"complete": complete, "counts": dict(counts), "missing": missing,
            "precision": ratio(tp, tp+fp) if complete else ratio(0, 0), "recall": ratio(tp, tp+missing) if complete else ratio(0, 0)}
        totals.update({"missed": missing, "extra": counts["extra"], "duplicate": counts["duplicate"]})
    result["member_counts"] = dict(totals)
    result["definitions"] = {"accuracy": "correct / (correct + incorrect + unsupported); manual verdicts only",
        "flag_metrics": "Eligible reviewed fields with known field-level flag status only. Errors = incorrect + unsupported.",
        "coverage": "unflagged / eligible fields with known flag status (share accepted without review)",
        "selective_risk": "errors among unflagged fields / unflagged fields",
        "depth": "Inference depth per field type from depth_map.json (explicit, derived, inferred); a reviewer override per value takes precedence.",
        "error_type": "Perceptual (misread the drawing) or reasoning (wrong interpretation, derivation, or domain knowledge); labelled per error by the reviewer.",
        "exclusions": "Unreviewed, indeterminate, not applicable; fields of extra/duplicate members. Notes and uncertainty metadata are not scored.",
        "member_unit": "One object in an existing schema collection; repeated joist groups stay groups.",
        "warning": "Partial or selectively reviewed results do not estimate whole-dataset accuracy. Missing-member properties are not included in matched-field accuracy."}
    return result
