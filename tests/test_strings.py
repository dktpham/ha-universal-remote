"""Check that strings.json covers every key the config flow actually uses.

A missing key does not raise anything - it renders as a raw placeholder in the
UI - so nothing else would catch it. This walks config_flow.py's AST for the
step ids, progress actions, abort reasons and error keys it references, and
checks each one is translated. It also flags translations nothing uses, which is
how stale keys survive refactors.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

_INTEGRATION = (
    Path(__file__).resolve().parents[1] / "custom_components" / "virtual_remote"
)
_SUBENTRY_TYPE = "remote"

# A progress step is addressed by its progress_action, not by a `step` entry -
# core's snooz and improv_ble both ship progress actions with no matching step.
_FORM_CALL = "async_show_form"
_PROGRESS_CALL = "async_show_progress"


def _string_literals(node: ast.AST) -> list[str]:
    """String constants in an expression, descending into conditionals."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return _string_literals(node.body) + _string_literals(node.orelse)
    return []


def _keyword(call: ast.Call, name: str) -> list[str]:
    for keyword in call.keywords:
        if keyword.arg == name:
            return _string_literals(keyword.value)
    return []


@pytest.fixture(scope="module")
def used() -> dict[str, set[str]]:
    """Keys referenced by config_flow.py."""
    source = (_INTEGRATION / "config_flow.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    found: dict[str, set[str]] = {
        "step": set(),
        "progress": set(),
        "abort": set(),
        "error": set(),
    }

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            if name == _FORM_CALL:
                found["step"].update(_keyword(node, "step_id"))
            elif name == _PROGRESS_CALL:
                found["progress"].update(_keyword(node, "progress_action"))
            elif name == "async_abort":
                found["abort"].update(_keyword(node, "reason"))
        # errors["field"] = "error_key"
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "errors"
                    and isinstance(node.value.value, str)
                ):
                    found["error"].add(node.value.value)

    return found


@pytest.fixture(scope="module", params=["strings.json", "translations/en.json"])
def translations(request) -> dict:
    return json.loads((_INTEGRATION / request.param).read_text(encoding="utf-8"))


def _section(translations: dict, key: str) -> dict:
    """A section merged across the config flow and the subentry flow.

    Both live in one file and one module, so a key may legitimately be declared
    under either - `single_instance_allowed` belongs to the config flow while
    `no_buttons` belongs to the subentry flow.
    """
    return {
        **translations.get("config", {}).get(key, {}),
        **translations.get("config_subentries", {})
        .get(_SUBENTRY_TYPE, {})
        .get(key, {}),
    }


@pytest.mark.parametrize("kind", ["step", "progress", "abort", "error"])
def test_every_used_key_is_translated(used, translations, kind) -> None:
    available = set(_section(translations, kind))
    missing = used[kind] - available

    assert not missing, f"untranslated {kind} keys: {sorted(missing)}"


def test_no_unused_step_or_progress_translations(used, translations) -> None:
    """Stale keys mean a step was renamed and the strings were not."""
    for kind in ("step", "progress"):
        declared = set(_section(translations, kind))
        assert not declared - used[kind], (
            f"{kind} translations nothing references: "
            f"{sorted(declared - used[kind])}"
        )


def test_strings_and_english_translations_match() -> None:
    """Custom components ship their own translations; they must not drift."""
    strings = json.loads((_INTEGRATION / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (_INTEGRATION / "translations" / "en.json").read_text(encoding="utf-8")
    )

    assert strings == english


def test_no_core_only_key_references() -> None:
    """`[%key:...%]` indirection is resolved by hassfest and only works in core."""
    for name in ("strings.json", "translations/en.json"):
        assert "[%key:" not in (_INTEGRATION / name).read_text(encoding="utf-8"), (
            f"{name} uses a core-only translation reference"
        )


def test_form_fields_are_labelled(translations) -> None:
    """Every field in a step's schema needs a label under `data`.

    Approximate but useful: an unlabelled field shows its raw key in the UI.
    """
    steps = _section(translations, "step")
    for step_id, step in steps.items():
        if "data" in step:
            assert step["data"], f"step {step_id} has an empty data section"
        assert "description" in step or "title" in step, (
            f"step {step_id} has neither a title nor a description"
        )
