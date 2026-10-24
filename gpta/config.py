"""JSON configuration with defaults derived from the module registry."""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable

from .module_api import CORE_LIMITS_SCHEMA, Module, limits_schema

DEFAULT_SERVER = {
    "host": "127.0.0.1",
    "port": 8787,
    "run_timeout_seconds": 120,
    # Hostnames accepted in the Host header (port ignored). Add any public
    # hostname a tunnel or reverse proxy forwards, e.g. "gpta.example.com".
    "allowed_hosts": ["localhost", "127.0.0.1", "::1"],
}
DEFAULT_PRINTER = {
    "enabled": False,
    "auto_print": True,
    "device": "/dev/usb/lp0",
    "width": 32,
    "feed_lines": 3,
    "cut": True,
    "write_timeout_seconds": 10,
    "min_print_spacing_seconds": 30,
    "max_prints_per_hour": 60,
    "failure_threshold": 3,
}

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
        choices = [
            c["value"] if isinstance(c, dict) else c for c in schema.get("choices", [])
        ]
        if value not in choices:
            raise ConfigError(f"{where}.{key} must be one of {choices!r}")
    else:  # pragma: no cover - schema is developer supplied
        raise ConfigError(f"unknown schema type for {where}.{key}: {kind!r}")


Reject = Callable[[str], None]


def _strict(message: str) -> None:
    raise ConfigError(message)


def _module_defaults(module: Module) -> dict[str, Any]:
    raw = copy.deepcopy(module.defaults)
    for key in ("enabled", "interval_seconds", "limits", "options"):
        if key not in raw:
            raise ConfigError(f"module {module.id!r} defaults missing {key!r}")
    raw["limits"] = dict(raw["limits"])
    raw["options"] = dict(raw["options"])
    return raw


def validate_module_defaults(module: Module) -> None:
    """Check a module's defaults against its own schema.

    Called by the registry so a module with inconsistent defaults is
    quarantined instead of breaking config loading for every module.
    """
    defaults = _module_defaults(module)
    where = f"module {module.id!r} defaults"
    redefined = set(module.limits_schema) & set(CORE_LIMITS_SCHEMA)
    if redefined:
        raise ConfigError(
            f"module {module.id!r} limits_schema redefines core limits: "
            f"{', '.join(sorted(redefined))}"
        )
    for section, schema in (
        ("limits", limits_schema(module)),
        ("options", module.options_schema),
    ):
        missing = set(schema) - set(defaults[section])
        extra = set(defaults[section]) - set(schema)
        if missing or extra:
            raise ConfigError(
                f"{where}: {section} keys must match {section}_schema "
                f"(missing {sorted(missing)}, unexpected {sorted(extra)})"
            )
    _normalise_module(module, defaults, defaults, where)


def _validate_interval(value: Any, where: str) -> int:
    if type(value) is not int:
        raise ConfigError(f"{where}.interval_seconds must be an integer")
    if not 5 <= value <= 604800:
        raise ConfigError(f"{where}.interval_seconds must be between 5 and 604800")
    return value


def _normalise_fields(
    schema: dict[str, dict[str, Any]],
    raw: Any,
    defaults: dict[str, Any],
    where: str,
    reject: Reject,
) -> dict[str, Any]:
    """Validate a limits/options object field by field.

    Missing fields take their default. ``reject`` either raises (API updates)
    or logs a warning (loading from disk), in which case the offending field
    falls back to its default.
    """
    if not isinstance(raw, dict):
        reject(f"{where} must be an object")
        raw = {}
    unknown = set(raw) - set(schema)
    if unknown:
        reject(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    result = {}
    for key, field_schema in schema.items():
        value = raw.get(key, defaults[key])
        try:
            _validate_value(key, value, field_schema, where)
        except ConfigError as exc:
            reject(str(exc))
            value = defaults[key]
        result[key] = copy.deepcopy(value)
    return result


def _normalise_module(
    module: Module,
    raw: Any,
    defaults: dict[str, Any],
    where: str,
    reject: Reject = _strict,
) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        reject(f"{where} must be an object")
        raw = {}
    unknown = set(raw) - MODULE_KEYS
    if unknown:
        reject(f"{where} has unknown keys: {', '.join(sorted(unknown))}")

    enabled = raw.get("enabled", defaults["enabled"])
    if type(enabled) is not bool:
        reject(f"{where}.enabled must be true or false")
        enabled = defaults["enabled"]
    try:
        interval = _validate_interval(
            raw.get("interval_seconds", defaults["interval_seconds"]), where
        )
    except ConfigError as exc:
        reject(str(exc))
        interval = defaults["interval_seconds"]

    return {
        "enabled": enabled,
        "interval_seconds": interval,
        "limits": _normalise_fields(
            limits_schema(module),
            raw.get("limits", {}),
            defaults["limits"],
            f"{where}.limits",
            reject,
        ),
        "options": _normalise_fields(
            module.options_schema,
            raw.get("options", {}),
            defaults["options"],
            f"{where}.options",
            reject,
        ),
    }


def _tolerant_section(
    normalise: Callable[[Any], dict[str, Any]],
    defaults: dict[str, Any],
    raw: Any,
    where: str,
    reject: Reject,
) -> dict[str, Any]:
    """Apply ``normalise`` to a stored section, keeping only valid keys."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        reject(f"{where} must be an object")
        raw = {}
    accepted: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in defaults:
            reject(f"{where} has unknown key: {key}")
            continue
        try:
            normalise({**accepted, key: value})
        except ConfigError as exc:
            reject(str(exc))
            continue
        accepted[key] = value
    return normalise(accepted)


def _normalise_server(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("server must be an object")
    unknown = set(raw) - SERVER_KEYS
    if unknown:
        raise ConfigError(f"server has unknown keys: {', '.join(sorted(unknown))}")
    result = copy.deepcopy(DEFAULT_SERVER)
    result.update(raw)
    if not isinstance(result["host"], str) or not result["host"].strip():
        raise ConfigError("server.host must be a non-empty string")
    if type(result["port"]) is not int or not 1 <= result["port"] <= 65535:
        raise ConfigError("server.port must be an integer between 1 and 65535")
    if (
        type(result["run_timeout_seconds"]) is not int
        or not 5 <= result["run_timeout_seconds"] <= 3600
    ):
        raise ConfigError("server.run_timeout_seconds must be between 5 and 3600")
    hosts = result["allowed_hosts"]
    if (
        not isinstance(hosts, list)
        or not hosts
        or not all(isinstance(h, str) and h.strip() for h in hosts)
    ):
        raise ConfigError("server.allowed_hosts must be a non-empty list of hostnames")
    result["allowed_hosts"] = [h.strip().lower() for h in hosts]
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
    device = result["device"]
    if not isinstance(device, str) or not device.startswith("/dev/"):
        raise ConfigError("printer.device must be a path under /dev/")
    if os.path.normpath(device) != device:
        raise ConfigError("printer.device must be a normalised path under /dev/")
    if type(result["width"]) is not int or result["width"] not in (32, 48):
        raise ConfigError("printer.width must be 32 or 48")
    if type(result["feed_lines"]) is not int or not 0 <= result["feed_lines"] <= 10:
        raise ConfigError("printer.feed_lines must be between 0 and 10")
    if (
        type(result["write_timeout_seconds"]) is not int
        or not 1 <= result["write_timeout_seconds"] <= 120
    ):
        raise ConfigError("printer.write_timeout_seconds must be between 1 and 120")
    if (
        type(result["min_print_spacing_seconds"]) is not int
        or not 0 <= result["min_print_spacing_seconds"] <= 3600
    ):
        raise ConfigError(
            "printer.min_print_spacing_seconds must be between 0 and 3600"
        )
    if (
        type(result["max_prints_per_hour"]) is not int
        or not 1 <= result["max_prints_per_hour"] <= 10000
    ):
        raise ConfigError("printer.max_prints_per_hour must be between 1 and 10000")
    if (
        type(result["failure_threshold"]) is not int
        or not 1 <= result["failure_threshold"] <= 100
    ):
        raise ConfigError("printer.failure_threshold must be between 1 and 100")
    return result


def read_server_settings(path: Path) -> dict[str, Any]:
    """Read only the ``server`` section of a config file, with defaults.

    Used by the control scripts (``gpta --print-url``) without loading
    modules. Invalid values fall back to defaults silently; the server
    itself logs them at startup.
    """
    raw: Any = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    section = raw.get("server") if isinstance(raw, dict) else None
    return _tolerant_section(
        _normalise_server, DEFAULT_SERVER, section, "server", lambda _msg: None
    )


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
        """Load the stored config, repairing what can be repaired.

        Only an unreadable file is fatal. Unknown or invalid settings are
        logged and replaced by defaults, so changing a module's schema never
        stops the server from starting.
        """
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

            def warn(message: str) -> None:
                self._log.warning("config: %s; using default", message)

            unknown_top = set(raw) - {"server", "printer", "modules"}
            if unknown_top:
                warn(f"unknown top-level keys: {', '.join(sorted(unknown_top))}")
            raw_modules = raw.get("modules", {})
            if not isinstance(raw_modules, dict):
                warn("modules must be an object")
                raw_modules = {}
            for stale in sorted(set(raw_modules) - set(self._modules)):
                self._log.warning("ignoring configuration for unknown module %s", stale)
            modules = {}
            for module_id, module in self._modules.items():
                modules[module_id] = _normalise_module(
                    module,
                    raw_modules.get(module_id, {}),
                    _module_defaults(module),
                    f"modules.{module_id}",
                    warn,
                )
            self._data = {
                "server": _tolerant_section(
                    _normalise_server, DEFAULT_SERVER, raw.get("server"), "server", warn
                ),
                "printer": _tolerant_section(
                    _normalise_printer,
                    DEFAULT_PRINTER,
                    raw.get("printer"),
                    "printer",
                    warn,
                ),
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
