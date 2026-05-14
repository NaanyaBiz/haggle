"""Import the AGL client from custom_components/haggle/agl/ without triggering
the HA-dependent custom_components/haggle/__init__.py.

The agl/ subpackage uses `from ..const import ...`, so we cannot just add the
agl directory to sys.path. Instead we register stub parent packages with the
right __path__ so submodule imports resolve, but neither parent's __init__.py
ever runs.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HAGGLE_DIR = REPO_ROOT / "custom_components" / "haggle"

if not HAGGLE_DIR.is_dir():
    raise RuntimeError(f"Cannot find haggle integration at {HAGGLE_DIR}")

if "custom_components" not in sys.modules:
    cc = types.ModuleType("custom_components")
    cc.__path__ = [str(REPO_ROOT / "custom_components")]
    sys.modules["custom_components"] = cc

if "custom_components.haggle" not in sys.modules:
    pkg = types.ModuleType("custom_components.haggle")
    pkg.__path__ = [str(HAGGLE_DIR)]
    sys.modules["custom_components.haggle"] = pkg
