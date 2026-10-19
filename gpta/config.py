"""JSON configuration with defaults derived from the module registry."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from .module_api import Module

DEFAULT_SERVER = {"host": "127.0.0.1", "port": 8787}
DEFAULT_PRINTER = {
    "enabled": False,
    "auto_print": True,
    "device": "/dev/usb/lp0",
    "width": 32,
    "encoding": "cp437",
    "feed_lines": 3,
    "cut": True,
}
SUPPORTED_ENCODINGS = {"cp437", "ascii", "latin-1"}

MODULE_KEYS = {"enabled", "interval_seconds", "limits", "options"}
PRINTER_KEYS = set(DEFAULT_PRINTER)
SERVER_KEYS = set(DEFAULT_SERVER)


class ConfigError(ValueError):
    pass


def _validate_value(key: str, value: Any, schema: dict[str, Any], where: str) -> None:
    optional = bool(schema.get("optional", False))
    if value is None:
        if optional:
            return
        raise ConfigError(f"{where}.{key} may not be null")
    kind = schema.get("type", "str")
    label = schema.get("label", key)
    if kind == "int":
        if type(value) is not int:
            raise ConfigError(f"{where}.{key} ({label}) must be an integer")
        if "min" in schema and value < schema["min"]:
            raise ConfigError(f"{where}.{key} must be >= {schema['min']}")
        if "max" in schema and value > schema["max"]:
            raise ConfigError(f"{where}.{key} must be <= {schema['max']}")
    elif kind == "bool":
        if type(value) is not bool:
            raise ConfigError(f"{where}.{key} ({label}) must be true or false")
    elif kind == "str":
        if not isinstance(value, str):
            raise ConfigError(f"{where}.{key} ({label}) must be a string")
        if "max_length" in schema and len(value) > schema["max_length"]:
            raise ConfigError(
                f"{where}.{key} is longer than {schema['max_length']} characters"
            )
    elif kind == "select":
        choices = [c["value"] if isinstance(c, dict) else c for c in schema.get("choices", [])]
        if value not in choices:
            raise ConfigError(f"{where}.{key} must be one of {choices!r}")
    else:  # pragma: no cover - schema is developer supplied
        raise ConfigError(f"unknown schema type for {where}.{key}: {kind!r}")


def _module_defaults(module: Module) -> dict[str, Any]:
    raw = copy.deepcopy(module.defaults)
    for key in ("enabled", "interval_seconds", "limits", "options"):
        if key not in raw:
            raise ConfigError(f"module {module.id!r} defaults missing {key!r}")
    if type(raw["enabled"]) is not bool:
        raise ConfigError(f"module {module.id!r} default enabled must be bool")
    if type(raw["interval_seconds"]) is not int:
        raise ConfigError(f"module {module.id!r} default interval must be int")
    raw["limits"] = dict(raw["limits"])
    raw["options"] = dict(raw["options"])
    return raw


def _validate_interval(value: Any, where: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{where}.interval_seconds must be an integer")
    if not 5 <= value <= 604800:
        raise ConfigError(f"{where}.interval_seconds must be between 5 and 604800")
    return value


def _normalise_module(
    module: Module, raw: Any, defaults: dict[str, Any], where: str
) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{where} must be an object")
    unknown = set(raw) - MODULE_KEYS
    if unknown:
        raise ConfigError(f"{where} has unknown keys: {', '.join(sorted(unknown))}")

    enabled = raw.get("enabled", defaults["enabled"])
    if type(enabled) is not bool:
        raise ConfigError(f"{where}.enabled must be true or false")
    interval = _validate_interval(
        raw.get("interval_seconds", defaults["interval_seconds"]), where
    )

    limits_raw = raw.get("limits", defaults["limits"])
    if not isinstance(limits_raw, dict):
        raise ConfigError(f"{where}.limits must be an object")
    unknown_limits = set(limits_raw) - set(module.limits_schema)
    if unknown_limits:
        raise ConfigError(
            f"{where}.limits has unknown keys: {', '.join(sorted(unknown_limits))}"
        )
    limits = {}
    for key, schema in module.limits_schema.items():
        if key not in limits_raw:
            raise ConfigError(f"{where}.limits missing {key!r}")
        _validate_value(key, limits_raw[key], schema, f"{where}.limits")
        limits[key] = limits_raw[key]

    options_raw = raw.get("options", defaults["options"])
    if not isinstance(options_raw, dict):
        raise ConfigError(f"{where}.options must be an object")
    unknown_options = set(options_raw) - set(module.options_schema)
    if unknown_options:
        raise ConfigError(
            f"{where}.options has unknown keys: {', '.join(sorted(unknown_options))}"
        )
    options = {}
    for key, schema in module.options_schema.items():
        if key not in options_raw:
            raise ConfigError(f"{where}.options missing {key!r}")
        _validate_value(key, options_raw[key], schema, f"{where}.options")
        options[key] = options_raw[key]

    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "limits": limits,
        "options": options,
    }


def _normalise_server(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("server must be an object")
    unknown = set(raw) - SERVER_KEYS
    if unknown:
        raise ConfigError(f"server has unknown keys: {', '.join(sorted(unknown))}")
    result = dict(DEFAULT_SERVER)
    result.update(raw)
    if not isinstance(result["host"], str) or not result["host"].strip():
        raise ConfigError("server.host must be a non-empty string")
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")
    return result


def _normalise_printer(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("printer must be an object")
    unknown = set(raw) - PRINTER_KEYS
    if unknown:
        raise ConfigError(f"printer has unknown keys: {', '.join(sorted(unknown))}")
    result = dict(DEFAULT_PRINTER)
    result.update(raw)
    for key in ("enabled", "auto_print", "cut"):
        if type(result[key]) is not bool:
            raise ConfigError(f"printer.{key} must be true or false")
    if not isinstance(result["device"], str) or not result["device"].startswith("/"):
        raise ConfigError("printer.device must be an absolute path")
    if type(result["width"]) is not int or result["width"] not in (32, 48):
        raise ConfigError("printer.width must be 32 or 48")
    if result["encoding"] not in SUPPORTED_ENCODINGS:
        raise ConfigError(
            f"printer.encoding must be one of {sorted(SUPPORTED_ENCODINGS)}"
        )
    if type(result["feed_lines"]) is not int or not 0 <= result["feed_lines"] <= 10:
        raise ConfigError("printer.feed_lines must be between 0 and 10")
    return result


class ConfigStore:
    """Owns the merged configuration and persists it atomically."""

    def __init__(self, path: Path, modules: dict[str, Module], log=None):
        self.path = Path(path)
        self._modules = modules
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None
        self._log = log or logging.getLogger("gpta.config")

    @property
    def data_dir(self) -> Path:
        return self.path.parent

    def load(self) -> None:
        with self._lock:
            existed = self.path.exists()
            raw: Any = {}
            if existed:
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise ConfigError(f"cannot read {self.path}: {exc}") from exc
            if not isinstance(raw, dict):
                raise ConfigError(f"{self.path} must contain a JSON object")
            unknown_top = set(raw) - {"server", "printer", "modules"}
            if unknown_top:
                raise ConfigError(
                    f"config has unknown top-level keys: {', '.join(sorted(unknown_top))}"
                )
            raw_modules = raw.get("modules", {})
            if not isinstance(raw_modules, dict):
                raise ConfigError("modules must be an object")
            for stale in sorted(set(raw_modules) - set(self._modules)):
                self._log.warning("ignoring configuration for unknown module %s", stale)
            modules = {}
            for module_id, module in self._modules.items():
                defaults = _module_defaults(module)
                modules[module_id] = _normalise_module(
                    module,
                    raw_modules.get(module_id, {}),
                    defaults,
                    f"modules.{module_id}",
                )
            self._data = {
                "server": _normalise_server(raw.get("server", {})),
                "printer": _normalise_printer(raw.get("printer", {})),
                "modules": modules,
            }
            if not existed:
                self._write(self._data)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                raise RuntimeError("ConfigStore.load() must be called first")
            return copy.deepcopy(self._data)

    def module_config(self, module_id: str) -> dict[str, Any]:
        with self._lock:
            if self._data is None:
                raise RuntimeError("ConfigStore.load() must be called first")
            return copy.deepcopy(self._data["modules"][module_id])

    def update_module(self, module_id: str, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConfigError("module patch must be an object")
        unknown = set(patch) - MODULE_KEYS
        if unknown:
            raise ConfigError(f"unknown module keys: {', '.join(sorted(unknown))}")
        with self._lock:
            if self._data is None:
                raise RuntimeError("ConfigStore.load() must be called first")
            if module_id not in self._modules:
                raise KeyError(module_id)
            module = self._modules[module_id]
            current = self._data["modules"][module_id]
            candidate = copy.deepcopy(current)
            for key in ("enabled", "interval_seconds"):
                if key in patch:
                    candidate[key] = patch[key]
            if "limits" in patch:
                if not isinstance(patch["limits"], dict):
                    raise ConfigError("limits patch must be an object")
                candidate["limits"].update(patch["limits"])
            if "options" in patch:
                if not isinstance(patch["options"], dict):
                    raise ConfigError("options patch must be an object")
                candidate["options"].update(patch["options"])
            defaults = _module_defaults(module)
            normalised = _normalise_module(
                module, candidate, defaults, f"modules.{module_id}"
            )
            data = copy.deepcopy(self._data)
            data["modules"][module_id] = normalised
            self._write(data)
            self._data = data
            return copy.deepcopy(normalised)

    def update_printer(self, patch: Any) -> dict[str, Any]:
        if not isinstance(patch, dict):
            raise ConfigError("printer patch must be an object")
        unknown = set(patch) - PRINTER_KEYS
        if unknown:
            raise ConfigError(f"unknown printer keys: {', '.join(sorted(unknown))}")
        with self._lock:
            if self._data is None:
                raise RuntimeError("ConfigStore.load() must be called first")
            candidate = copy.deepcopy(self._data["printer"])
            candidate.update(patch)
            normalised = _normalise_printer(candidate)
            data = copy.deepcopy(self._data)
            data["printer"] = normalised
            self._write(data)
            self._data = data
            return copy.deepcopy(normalised)

    def save(self) -> None:
        with self._lock:
            if self._data is None:
                raise RuntimeError("ConfigStore.load() must be called first")
            self._write(self._data)

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise ConfigError(f"cannot write {self.path}: {exc}") from exc
