#!/usr/bin/env python3
"""Self-consistency checks for the virtual-remote blueprints.

Not a substitute for Home Assistant's own validation - it cannot tell you
whether a blueprint loads. It catches the mistakes that are easy to make by hand
and slow to find in the UI:

  * dangling or unused !input references
  * gesture names that are not in the canonical vocabulary
  * a box-adapter whose `triggers:` / `gesture_names` / `gesture_triggers` lists
    have drifted out of lockstep, which would silently map presses to the wrong
    gesture
  * a trigger wrapper carrying extra keys, which stops Home Assistant flattening
    it (see _base_trigger_list_flatten: the wrapper must hold `triggers` alone)
  * an adapter and dispatcher for the same topology disagreeing on gestures

Usage:  python tools/check_blueprints.py [blueprints_dir]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

# Legacy vocabulary: buttons b1..b8 plus dial, crossed with the gesture verbs.
# Used by the pre-integration adapter/dispatcher pairs.
VOCABULARY = re.compile(r"^(b[1-8]|dial)_(short|double|triple|long|release|cw|ccw)$")

# Home Assistant's standard button event types, which the virtual_remote
# integration emits and the consumer blueprint listens for.
BUTTON_EVENT_TYPES = frozenset(
    {
        "press_start",
        "press_end",
        "long_press_start",
        "long_press_end",
        "multi_press_ongoing",
        "multi_press_end",
    }
)


class BPLoader(yaml.SafeLoader):
    """SafeLoader that keeps !input references as inspectable markers."""


BPLoader.add_constructor(
    "!input", lambda loader, node: {"__INPUT__": loader.construct_scalar(node)}
)
BPLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: {"__TAG__": suffix}
)


def is_ref(node) -> bool:
    return isinstance(node, dict) and set(node) == {"__INPUT__"}


def declared_inputs(doc) -> dict[str, str | None]:
    """Input keys mapped to their section, descending one level into sections."""
    found: dict[str, str | None] = {}

    def walk(block, section=None):
        for key, val in (block or {}).items():
            if isinstance(val, dict) and "input" in val and "selector" not in val:
                walk(val["input"], section=key)
            else:
                found[key] = section

    walk(doc.get("blueprint", {}).get("input", {}))
    return found


def input_refs(node, acc=None) -> list[str]:
    acc = [] if acc is None else acc
    if is_ref(node):
        acc.append(node["__INPUT__"])
    elif isinstance(node, dict):
        for val in node.values():
            input_refs(val, acc)
    elif isinstance(node, list):
        for val in node:
            input_refs(val, acc)
    return acc


def topology(name: str) -> str | None:
    """'virtual_remote_5button_adapter.yaml' -> '5button'."""
    match = re.search(r"_(\d+button|dial)", name)
    return match.group(1) if match else None


def check(path: Path):
    problems: list[str] = []
    notes: list[str] = []
    doc = yaml.load(path.read_text(encoding="utf-8"), Loader=BPLoader)

    declared = declared_inputs(doc)
    body = {k: v for k, v in doc.items() if k != "blueprint"}
    refs = input_refs(body)

    for ref in sorted(set(refs)):
        if ref not in declared:
            problems.append(f"!input {ref} referenced but never declared")
    for key in declared:
        if key not in refs:
            problems.append(f"input '{key}' declared but never referenced")

    if doc.get("blueprint", {}).get("domain") != "automation":
        problems.append("blueprint.domain is not 'automation'")
    for required in ("triggers", "actions"):
        if required not in doc:
            problems.append(f"no `{required}:` block")

    triggers = doc.get("triggers") or []
    gestures: set[str] = set()

    # ---- shape: label-based adapter (`triggers:` is one spliced !input) ----
    if is_ref(triggers):
        notes.append(f"label-adapter: triggers spliced from !input {triggers['__INPUT__']}")
        return path.name, problems, notes, None, gestures

    # ---- shape: consumer of the virtual_remote integration ----
    if any(
        isinstance(trig, dict) and trig.get("trigger") == "event.received"
        for trig in triggers
    ):
        consumed: list[str] = []

        def walk_consumer(node) -> None:
            if isinstance(node, dict):
                for branch in node.get("choose") or []:
                    for cond in branch.get("conditions") or []:
                        if isinstance(cond, dict) and "id" in cond:
                            consumed.append(cond["id"])
                    if branch.get("sequence") is None:
                        problems.append(
                            f"branch {branch.get('alias')!r} has no sequence"
                        )
                    walk_consumer(branch.get("sequence"))
                for key, val in node.items():
                    if key != "choose":
                        walk_consumer(val)
            elif isinstance(node, list):
                for val in node:
                    walk_consumer(val)

        walk_consumer(doc["actions"])

        trigger_ids: list[str] = []
        for trig in triggers:
            tid = trig.get("id")
            if tid is None:
                problems.append(f"trigger without an id: {trig!r}")
            else:
                trigger_ids.append(tid)

            # event.received nests its filter under `options`, not at top level.
            event_types = (trig.get("options") or {}).get("event_type")
            if not event_types:
                problems.append(f"trigger {tid} has no options.event_type")
            else:
                for event_type in event_types:
                    if event_type not in BUTTON_EVENT_TYPES:
                        problems.append(
                            f"trigger {tid} listens for {event_type!r}, which is "
                            "not a standard button event type"
                        )
                    gestures.add(event_type)

            if not is_ref((trig.get("target") or {}).get("entity_id")):
                problems.append(f"trigger {tid} does not target an !input entity")

        if missing := set(trigger_ids) - set(consumed):
            problems.append(f"triggers with no choose branch: {sorted(missing)}")
        if orphan := set(consumed) - set(trigger_ids):
            problems.append(f"choose branches with no trigger: {sorted(orphan)}")

        notes.append(
            f"consumer: {len(trigger_ids)} triggers, {len(consumed)} branches, "
            f"{len(declared)} inputs, listens for {len(gestures)} event types"
        )
        return path.name, problems, notes, "consumer", gestures

    # ---- shape: box-based adapter ----
    if "gesture_names" in (doc.get("variables") or {}):
        variables = doc["variables"]
        names = variables.get("gesture_names") or []
        box_refs = [
            node["__INPUT__"] if is_ref(node) else None
            for node in (variables.get("gesture_triggers") or [])
        ]

        wrapper_refs: list[str | None] = []
        for item in triggers:
            if not isinstance(item, dict):
                problems.append(f"trigger entry is not a mapping: {item!r}")
                continue
            if set(item) != {"triggers"}:
                problems.append(
                    f"trigger wrapper has extra keys {sorted(set(item) - {'triggers'})} "
                    "- Home Assistant will not flatten it"
                )
                continue
            inner = item["triggers"]
            wrapper_refs.append(inner["__INPUT__"] if is_ref(inner) else None)

        if not (len(names) == len(box_refs) == len(wrapper_refs)):
            problems.append(
                f"lockstep broken: {len(wrapper_refs)} triggers, "
                f"{len(names)} gesture_names, {len(box_refs)} gesture_triggers"
            )
        else:
            for idx, (name, box, wrapper) in enumerate(zip(names, box_refs, wrapper_refs)):
                if not (name == box == wrapper):
                    problems.append(
                        f"lockstep broken at index {idx}: gesture_names={name!r}, "
                        f"gesture_triggers=!input {box}, triggers=!input {wrapper}"
                    )

        for name in names:
            if not VOCABULARY.match(str(name)):
                problems.append(f"gesture {name!r} is not in the canonical vocabulary")
            if name not in declared:
                problems.append(f"gesture {name!r} has no matching input")

        gestures = {str(n) for n in names}
        notes.append(f"box-adapter: {len(names)} gesture boxes, {len(declared)} inputs")
        return path.name, problems, notes, "adapter", gestures

    # ---- shape: dispatcher ----
    trigger_ids: list[str] = []
    for trig in triggers:
        tid = trig.get("id")
        if tid is None:
            problems.append(f"trigger without an id: {trig!r}")
            continue
        trigger_ids.append(tid)
        if trig.get("event_type") != "virtual_remote":
            problems.append(f"trigger {tid} listens to {trig.get('event_type')!r}")
        gesture = (trig.get("event_data") or {}).get("gesture")
        if gesture != tid:
            problems.append(f"trigger id {tid!r} does not match gesture {gesture!r}")
        if not VOCABULARY.match(str(tid)):
            problems.append(f"gesture {tid!r} is not in the canonical vocabulary")

    duplicates = {i for i in trigger_ids if trigger_ids.count(i) > 1}
    if duplicates:
        problems.append(f"duplicate trigger ids: {sorted(duplicates)}")

    consumed: list[str] = []

    def walk_choose(node):
        if isinstance(node, dict):
            for branch in node.get("choose") or []:
                for cond in branch.get("conditions") or []:
                    if isinstance(cond, dict) and "id" in cond:
                        consumed.append(cond["id"])
                if branch.get("sequence") is None:
                    problems.append(f"branch {branch.get('alias')!r} has no sequence")
                walk_choose(branch.get("sequence"))
            for key, val in node.items():
                if key != "choose":
                    walk_choose(val)
        elif isinstance(node, list):
            for val in node:
                walk_choose(val)

    walk_choose(doc["actions"])

    if missing := set(trigger_ids) - set(consumed):
        problems.append(f"triggers with no choose branch: {sorted(missing)}")
    if orphan := set(consumed) - set(trigger_ids):
        problems.append(f"choose branches with no trigger: {sorted(orphan)}")

    gestures = set(trigger_ids)
    notes.append(
        f"dispatcher: {len(trigger_ids)} triggers, {len(consumed)} branches, "
        f"{len(declared)} inputs"
    )
    return path.name, problems, notes, "dispatcher", gestures


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "blueprints")
    files = sorted(root.rglob("*.yaml"))
    if not files:
        print(f"no yaml found under {root}")
        return 1

    failed = False
    by_topology: dict[str, dict[str, set[str]]] = {}

    for path in files:
        try:
            name, problems, notes, kind, gestures = check(path)
        except yaml.YAMLError as err:
            print(f"FAIL {path.name}: YAML will not parse\n       {err}")
            failed = True
            continue

        print(f"{'FAIL' if problems else 'ok  '} {name}")
        for note in notes:
            print(f"       - {note}")
        for problem in problems:
            print(f"       ! {problem}")
            failed = True

        if kind and (topo := topology(path.name)):
            by_topology.setdefault(topo, {})[kind] = gestures

    for topo, kinds in sorted(by_topology.items()):
        adapter, dispatcher = kinds.get("adapter"), kinds.get("dispatcher")
        if adapter is None or dispatcher is None:
            continue
        if adapter == dispatcher:
            print(f"ok   {topo}: adapter and dispatcher agree on {len(adapter)} gestures")
            continue
        print(f"FAIL {topo}: adapter and dispatcher disagree")
        if only_adapter := adapter - dispatcher:
            print(f"       ! emitted but never handled: {sorted(only_adapter)}")
        if only_dispatcher := dispatcher - adapter:
            print(f"       ! handled but never emitted: {sorted(only_dispatcher)}")
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
