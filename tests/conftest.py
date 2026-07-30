"""Load the integration's pure modules without importing Home Assistant.

`custom_components/virtual_remote/__init__.py` imports homeassistant, so the
package cannot be imported normally in a bare environment. The pure modules
(const, gestures, model) use relative imports, so they need *a* parent package
to resolve against - but not that one.

So we register a synthetic parent package whose ``__init__`` is never executed
and load the pure modules into it. This is exactly the boundary the engine
design promises: if someone adds a homeassistant import to gestures.py or
model.py, these tests stop importing, which is the point.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

_PKG = "virtual_remote_pure"
_ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "virtual_remote"
_PURE_MODULES = ("const", "gestures", "model")


def _load_pure_package() -> None:
    if _PKG in sys.modules:
        return

    package = types.ModuleType(_PKG)
    package.__path__ = [str(_ROOT)]  # type: ignore[attr-defined]
    sys.modules[_PKG] = package

    for name in _PURE_MODULES:
        spec = importlib.util.spec_from_file_location(
            f"{_PKG}.{name}", _ROOT / f"{name}.py"
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"{_PKG}.{name}"] = module
        spec.loader.exec_module(module)
        setattr(package, name, module)


_load_pure_package()
