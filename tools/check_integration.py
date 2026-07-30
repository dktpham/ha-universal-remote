#!/usr/bin/env python3
"""Static checks for the virtual_remote integration.

Two jobs, both of which would otherwise need a full Home Assistant install:

1. **Purity.** const.py, gestures.py and model.py must not import homeassistant.
   That boundary is what keeps the engine unit-testable in a bare environment
   and extractable to a standalone package later, so it is enforced, not hoped
   for.

2. **Import resolution.** Every `from homeassistant... import X` is resolved
   against a real Home Assistant checkout and X is checked to exist. Without a
   running HA this is the only thing standing between a typo'd helper name and a
   traceback at integration-load time.

Usage:
    python tools/check_integration.py [path-to-homeassistant-core]

The core checkout defaults to $HA_CORE, then to a sibling `core` directory.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PURE_MODULES = ("const.py", "gestures.py", "model.py")


def find_core(argv: list[str]) -> Path | None:
    """Locate a Home Assistant checkout."""
    candidates = []
    if len(argv) > 1:
        candidates.append(Path(argv[1]))
    if env := os.environ.get("HA_CORE"):
        candidates.append(Path(env))
    here = Path(__file__).resolve().parents[1]
    candidates += [here.parent / "core", here.parent.parent / "core"]

    for candidate in candidates:
        if (candidate / "homeassistant" / "core.py").is_file():
            return candidate
    return None


def module_path(core: Path, module: str) -> Path | None:
    """Resolve a dotted module name to a file in the checkout."""
    parts = module.split(".")
    as_module = core.joinpath(*parts).with_suffix(".py")
    if as_module.is_file():
        return as_module
    as_package = core.joinpath(*parts, "__init__.py")
    if as_package.is_file():
        return as_package
    return None


def exported_names(path: Path) -> set[str] | None:
    """Top-level names a module provides, including re-exports.

    Returns None if the file cannot be parsed. That is not hypothetical: some
    Home Assistant checkouts contain files this parser rejects, and silently
    treating an unparseable module as "exports nothing" would turn every import
    from it into a false failure. Unverifiable is reported as unverifiable.
    """
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return None

    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, (ast.AnnAssign, ast.TypeAlias)):
            target = node.target if isinstance(node, ast.AnnAssign) else node.name
            if isinstance(target, ast.Name):
                names.add(target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.If):
            # TYPE_CHECKING blocks and the like still export at type level.
            for inner in ast.walk(node):
                if isinstance(inner, (ast.ClassDef, ast.FunctionDef)):
                    names.add(inner.name)

    return names


def check_file(path: Path, core: Path) -> tuple[list[str], list[str]]:
    """Resolve one of our modules' homeassistant imports.

    Returns (problems, unverified) so that partial coverage is visible rather
    than being mistaken for a clean pass.
    """
    problems: list[str] = []
    unverified: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_pure = path.name in PURE_MODULES

    for node in ast.walk(tree):
        module: str | None = None
        if isinstance(node, ast.ImportFrom):
            module = node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "homeassistant" and is_pure:
                    problems.append(
                        f"line {node.lineno}: pure module imports {alias.name}"
                    )
            continue

        if not module or module.split(".")[0] != "homeassistant":
            continue

        if is_pure:
            problems.append(f"line {node.lineno}: pure module imports {module}")
            continue

        target = module_path(core, module)
        if target is None:
            problems.append(f"line {node.lineno}: no such module {module}")
            continue

        available = exported_names(target)
        if available is None:
            # A submodule import is still verifiable from the filesystem even
            # when the package's __init__ will not parse.
            opaque = [
                alias.name
                for alias in node.names
                if module_path(core, f"{module}.{alias.name}") is None
            ]
            if opaque:
                unverified.append(
                    f"line {node.lineno}: {module} could not be parsed; "
                    f"{', '.join(opaque)} unchecked"
                )
            continue

        for alias in node.names:
            if alias.name in available:
                continue
            # `from homeassistant.util import dt` imports a submodule, which is
            # not a top-level name of the package's __init__.
            if module_path(core, f"{module}.{alias.name}") is not None:
                continue
            problems.append(
                f"line {node.lineno}: {module} has no {alias.name!r}"
            )

    return problems, unverified


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "custom_components" / "virtual_remote"
    if not root.is_dir():
        print(f"no integration found at {root}")
        return 1

    core = find_core(sys.argv)
    if core is None:
        print(
            "SKIP import resolution: no Home Assistant checkout found.\n"
            "      Pass one as an argument or set $HA_CORE.",
            file=sys.stderr,
        )

    failed = False
    total_unverified = 0

    for path in sorted(root.glob("*.py")):
        problems, unverified = check_file(path, core or Path("/nonexistent"))
        if core is None:
            # Purity needs no checkout; import resolution does.
            problems = [p for p in problems if "pure module" in p]
            unverified = []

        label = "pure" if path.name in PURE_MODULES else "hass"
        print(f"{'FAIL' if problems else 'ok  '} {path.name} ({label})")
        for problem in problems:
            print(f"       ! {problem}")
            failed = True
        for note in unverified:
            print(f"       ? {note}")
            total_unverified += 1

    if core is not None:
        print(f"\nresolved against {core}")
    if total_unverified:
        print(
            f"{total_unverified} import(s) UNVERIFIED - the checkout could not be "
            "parsed there, so only a live Home Assistant will prove them."
        )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
