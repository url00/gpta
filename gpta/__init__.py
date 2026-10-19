"""GPTA: modular receipt data server for thermal printers."""

from pathlib import Path

__version__ = "0.1.0"

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent
DEFAULT_MODULES_DIR = PROJECT_ROOT / "modules"
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web"
