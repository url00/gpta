"""GPTA: modular receipt data server for thermal printers."""

import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_MODULES_DIR = PROJECT_ROOT / "modules"
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"


def _read_version() -> str:
    # pyproject.toml is the single source; the Nix package copies it too.
    try:
        with open(PROJECT_ROOT / "pyproject.toml", "rb") as handle:
            return str(tomllib.load(handle)["project"]["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return "0+unknown"


__version__ = _read_version()
