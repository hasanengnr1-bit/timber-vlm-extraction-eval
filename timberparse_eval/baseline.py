"""Thin local adapter; original SmartStructs source and prompts remain untouched."""
import ast
import hashlib
import logging
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))
from core import schema as original

SYSTEMS = {
    "floor_system": (original.FloorSystemData, "floor_system"),
    "footing": (original.FootingSystemData, "footing"),
    "post": (original.PostData, "post"),
    "roof_system": (original.RoofSystemData, "roof_system"),
    "shear_wall": (original.ShearWallData, "shear_wall"),
    "wall": (original.WallSystemData, "wall_system"),
    "project_context": (original.ProjectContextData, "project_context"),
}
USER_PROMPT = "Analyze the given structural plan based on the provided instructions"


def prompt(system):
    return (APP / "core/llm/prompts/system_prompts" / (SYSTEMS[system][1] + ".txt")).read_text(encoding="utf-8")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def schemas():
    return {key: cls.model_json_schema() for key, (cls, _) in SYSTEMS.items()}


def deref(node, root):
    if "$ref" in node:
        return root["$defs"][node["$ref"].split("/")[-1]]
    if "anyOf" in node:
        return deref(next((n for n in node["anyOf"] if n.get("type") != "null"), {}), root)
    return node


def catalog(schema_map):
    result = {}
    for system, root in schema_map.items():
        for key, raw in root.get("properties", {}).items():
            node = deref(raw, root)
            if node.get("type") == "array" and deref(node.get("items", {}), root).get("type") == "object":
                result[f"{system}/{key}"] = {"system": system, "collection": key, "schema": deref(node["items"], root), "root": root}
    return result


def excluded_fields():
    """Honor the baseline's explicit SKIP_EXTRACTION source annotations."""
    text = (APP / "core/schema/schema.py").read_text(encoding="utf-8")
    lines = text.splitlines()
    excluded = {}
    for cls in ast.parse(text).body:
        if isinstance(cls, ast.ClassDef):
            excluded[cls.name] = [n.target.id for n in cls.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and "SKIP_EXTRACTION" in lines[n.lineno - 1]]
    return excluded


def resolver_module():
    """Load unchanged resolver logic without importing DB/Telegram service setup.

    Only infrastructure imports and logger construction are substituted. The
    matching functions, schemas, prompt, and admission guards are original code.
    """
    path = APP / "services/matching/link_resolver.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tree.body = [n for n in tree.body if not (isinstance(n, ast.ImportFrom) and (n.module or "").startswith(("services.", "core.")))]
    namespace = {"__name__": "timberparse_original_resolver", "get_logger": logging.getLogger}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace
