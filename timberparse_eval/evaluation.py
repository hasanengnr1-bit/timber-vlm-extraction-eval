"""Field inventory and denominator-explicit manual evaluation. No model judge."""
from collections import Counter
from baseline import catalog, deref

VERDICTS = {"unreviewed", "correct", "incorrect", "unsupported", "indeterminate", "not_applicable"}
ERRORS = {"incorrect", "unsupported"}


def inventory(data, schema_map, excluded=None):
    excluded = excluded or {}
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
            fields.append({"id": path, "member_id": member_id, "name": key,
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


def scores(inv, review):
    def field_scores(fields):
        rows = []
        excluded_members = 0
        for f in fields:
            if review.get("members", {}).get(f["member_id"], "unreviewed") in {"extra", "duplicate"}:
                excluded_members += 1
                continue
            r = review.get("fields", {}).get(f["id"], {})
            rows.append((r.get("verdict", "unreviewed"), r.get("flag", "original") if r.get("flag", "original") != "original" else f["flag"]))
        counts = Counter(v for v, _ in rows)
        eligible = [(v, flag) for v, flag in rows if v in ERRORS | {"correct"}]
        known = [(v, flag) for v, flag in eligible if flag != "unknown"]
        flagged = [(v, flag) for v, flag in known if flag == "flagged"]
        unflagged = [(v, flag) for v, flag in known if flag == "unflagged"]
        errors = sum(v in ERRORS for v, _ in known)
        flagged_errors = sum(v in ERRORS for v, _ in flagged)
        return {"counts": {v: counts[v] for v in sorted(VERDICTS)}, "total": len(rows),
                "excluded_extra_duplicate_fields": excluded_members,
                "accuracy": ratio(counts["correct"], len(eligible)),
                "flag_precision": ratio(flagged_errors, len(flagged)),
                "error_flag_recall": ratio(flagged_errors, errors),
                "unflagged_error_rate": ratio(sum(v in ERRORS for v, _ in unflagged), len(unflagged)),
                "unknown_flag_eligible": len(eligible)-len(known),
                "partial": counts["unreviewed"] > 0}

    result = field_scores(inv["fields"])
    result["by_system"] = {s: field_scores([f for f in inv["fields"] if f["system"] == s]) for s in sorted({f["system"] for f in inv["fields"]})}
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
        "exclusions": "Unreviewed, indeterminate, not applicable; fields of extra/duplicate members. Notes and uncertainty metadata are not scored.",
        "member_unit": "One object in an existing schema collection; repeated joist groups stay groups.",
        "warning": "Partial or selectively reviewed results do not estimate whole-dataset accuracy. Missing-member properties are not included in matched-field accuracy."}
    return result
