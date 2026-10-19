"""Discover and validate modules from the modules/ directory."""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

from .module_api import Module


class RegistryError(Exception):
    pass


REQUIRED_DEFAULTS = {"enabled", "interval_seconds", "limits", "options"}


def discover(modules_dir: Path) -> dict[str, Module]:
    modules_dir = Path(modules_dir)
    if not modules_dir.is_dir():
        raise RegistryError(f"modules directory not found: {modules_dir}")

    found: dict[str, Module] = {}
    for child in sorted(modules_dir.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        module_file = child / "module.py"
        if not module_file.is_file():
            continue
        mod_name = f"gpta_modules_{child.name}"
        spec = importlib.util.spec_from_file_location(mod_name, module_file)
        if spec is None or spec.loader is None:
            raise RegistryError(f"cannot load {module_file}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception as exc:
            raise RegistryError(f"failed importing {module_file}: {exc}") from exc

        classes = [
            obj
            for obj in vars(mod).values()
            if isinstance(obj, type)
            and issubclass(obj, Module)
            and obj is not Module
            and obj.__module__ == mod_name
        ]
        if len(classes) != 1:
            raise RegistryError(
                f"{module_file} must define exactly one Module subclass, found {len(classes)}"
            )
        module = classes[0]()
        if not isinstance(module.id, str) or not module.id:
            raise RegistryError(f"{module_file}: module id must be a non-empty string")
        if module.id != child.name:
            raise RegistryError(
                f"{module_file}: module id {module.id!r} must match folder {child.name!r}"
            )
        if module.id in found:
            raise RegistryError(f"duplicate module id: {module.id}")
        if not isinstance(module.name, str) or not module.name:
            raise RegistryError(f"{module_file}: module name must be non-empty")
        if not isinstance(module.description, str):
            raise RegistryError(f"{module_file}: module description must be a string")
        if not isinstance(module.defaults, dict) or not REQUIRED_DEFAULTS <= set(
            module.defaults
        ):
            raise RegistryError(
                f"{module_file}: defaults must contain {sorted(REQUIRED_DEFAULTS)}"
            )
        if not isinstance(module.limits_schema, dict):
            raise RegistryError(f"{module_file}: limits_schema must be a dict")
        if not isinstance(module.options_schema, dict):
            raise RegistryError(f"{module_file}: options_schema must be a dict")
        # Freeze a private copy so runtime mutation cannot leak into defaults.
        module.defaults = copy.deepcopy(module.defaults)
        found[module.id] = module

    if not found:
        raise RegistryError(f"no modules found in {modules_dir}")
    return dict(sorted(found.items()))


def describe(module: Module) -> dict:
    return {
        "id": module.id,
        "name": module.name,
        "description": module.description,
        "limits_schema": copy.deepcopy(module.limits_schema),
        "options_schema": copy.deepcopy(module.options_schema),
        "default_interval_seconds": module.defaults["interval_seconds"],
    }
