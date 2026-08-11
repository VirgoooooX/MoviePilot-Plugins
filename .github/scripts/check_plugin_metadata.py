#!/usr/bin/env python3
"""Validate MoviePilot V2 plugin index and runtime metadata."""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path


REQUIRED_METHODS = {
    "init_plugin",
    "get_state",
    "get_api",
    "get_form",
    "get_page",
    "stop_service",
}
FIELD_MAP = {
    "name": "plugin_name",
    "description": "plugin_desc",
    "version": "plugin_version",
    "icon": "plugin_icon",
    "author": "plugin_author",
    "labels": "plugin_label",
    "level": "auth_level",
}


def literal_class_values(plugin_class: ast.ClassDef) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in plugin_class.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target = node.target.id
            value = node.value
        if target and value is not None:
            try:
                values[target] = ast.literal_eval(value)
            except (ValueError, TypeError):
                pass
    return values


def validate(index_path: Path) -> list[str]:
    errors: list[str] = []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    plugin_root = index_path.parent / "plugins.v2"
    indexed_dirs = {plugin_id.lower() for plugin_id in index}

    for plugin_id, metadata in index.items():
        plugin_dir = plugin_root / plugin_id.lower()
        main_file = plugin_dir / "__init__.py"
        if not main_file.is_file():
            errors.append(f"{plugin_id}: missing {main_file}")
            continue

        tree = ast.parse(main_file.read_text(encoding="utf-8"), filename=str(main_file))
        plugin_class = next(
            (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == plugin_id),
            None,
        )
        if plugin_class is None:
            errors.append(f"{plugin_id}: class name must match plugin ID")
            continue

        values = literal_class_values(plugin_class)
        methods = {
            node.name
            for node in plugin_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing_methods = sorted(REQUIRED_METHODS - methods)
        if missing_methods:
            errors.append(f"{plugin_id}: missing required methods: {', '.join(missing_methods)}")

        for metadata_field, runtime_field in FIELD_MAP.items():
            metadata_value = metadata.get(metadata_field)
            runtime_value = values.get(runtime_field)
            if metadata_value is None and runtime_value is None:
                continue
            if metadata_value != runtime_value:
                errors.append(
                    f"{plugin_id}: {metadata_field}={metadata_value!r} does not match "
                    f"{runtime_field}={runtime_value!r}"
                )

        prefix = values.get("plugin_config_prefix")
        if not isinstance(prefix, str) or not prefix or not prefix.endswith("_"):
            errors.append(f"{plugin_id}: plugin_config_prefix must be a non-empty string ending in '_'")

        history = metadata.get("history") or {}
        version = metadata.get("version")
        if version and f"v{version}" not in history:
            errors.append(f"{plugin_id}: history is missing v{version}")

    for plugin_dir in plugin_root.iterdir():
        if plugin_dir.is_dir() and plugin_dir.name not in indexed_dirs and not plugin_dir.name.startswith("__"):
            errors.append(f"{plugin_dir.name}: plugin directory is not indexed")
    return errors


def main() -> int:
    paths = [Path(arg) for arg in sys.argv[1:]] or [Path("package.v2.json")]
    errors = [error for path in paths for error in validate(path)]
    if errors:
        print("Plugin metadata validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Plugin metadata validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
