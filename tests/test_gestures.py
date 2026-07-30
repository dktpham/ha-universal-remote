"""Tests for the pure gesture engine and the event-type mapping.

No Home Assistant, no async, no real timers. Ticks are driven explicitly and
every tick asserts it fired at exactly the deadline the engine reported, so
these tests verify timer *scheduling* as well as emissions.
"""

from __future__ import annotations

import random

import pytest
from virtual_remote_pure.const import (
    EVENT_LONG_PRESS_END,
    EVENT_LONG_PRESS_START,
    EVENT_MULTI_PRESS_END,
    EVENT_MULTI_PRESS_ONGOING,
    EVENT_PRESS_END,
    EVENT_PRESS_START,
    SHAPE_DECODED,
    SHAPE_EDGE,
    SHAPE_SINGLE_SHOT,
)
from virtual_remote_pure.gestures import (
    STUCK_TIMEOUT,
    DecodedGesture,
    Emission,
    GestureConfig,
    GestureEmission,
    GestureEngine,
    RawSignal,
)
from virtual_remote_pure.model import (
    ButtonConfig,
    SourceConfig,
    derive_event_types,
    translate,
)

DOWN, UP, TAP = RawSignal.DOWN, RawSignal.UP, RawSignal.TAP
TICK = "tick"

CFG_NO_MULTI = GestureConfig(long_press_threshold=0.5, multi_press_max=1)
CFG_MAX2 = GestureConfig(
    long_press_threshold=0.5, multi_press_window=0.4, multi_press_max=2
)
CFG_MAX3 = GestureConfig(
    long_press_threshold=0.5, multi_press_window=0.4, multi_press_max=3
)
CFG_MAX2_QUIET = GestureConfig(
    long_press_threshold=0.5, multi_press_max=2, multi_press_immediate=False
)
CFG_NO_HOLD = GestureConfig(long_press_threshold=None, multi_press_max=1)


def drive(engine: GestureEngine, script) -> list[tuple[Emission, int | None]]:
    """Run a script of (time, action) pairs, returning flattened emissions.

    A TICK asserts that a deadline was armed and that we are firing it at
    exactly the instant the engine asked for - so a wrong deadline fails here
    rather than silently producing plausible-looking emissions.
    """
    out: list[tuple[Emission, int | None]] = []
    deadline: float | None = None

    for at, action in script:
        if action == TICK:
            assert deadline is not None, f"tick at {at} with no deadline armed"
            assert deadline == pytest.approx(at), (
                f"engine asked to be woken at {deadline}, test ticked at {at}"
            )
            step = engine.tick(at)
        elif isinstance(action, DecodedGesture):
            step = engine.decoded(action, at)
        else:
            step = engine.signal(action, at)

        out.extend((em.kind, em.count) for em in step.emissions)
        deadline = step.deadline

    return out


P_DOWN = (Emission.PRESS_DOWN, None)
HOLD_START = (Emission.HOLD_START, None)
HOLD_END = (Emission.HOLD_END, None)


def click(n: int = 1) -> tuple[Emission, int | None]:
    return (Emission.CLICK_FINAL, n)


def multi(n: int) -> tuple[Emission, int | None]:
    return (Emission.MULTI_FINAL, n)


def progress(n: int) -> tuple[Emission, int | None]:
    return (Emission.CLICK_PROGRESS, n)


@pytest.mark.parametrize(
    ("config", "script", "expected"),
    [
        # --- synthesis off: everything is zero-latency ---------------------
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, DOWN), (0.12, UP)],
            [P_DOWN, click()],
            id="edge-single-click",
        ),
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, DOWN), (0.5, TICK), (2.3, UP)],
            [P_DOWN, HOLD_START, HOLD_END],
            id="edge-hold-then-release",
        ),
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, TAP)],
            [click()],
            id="single-shot-click-no-synthetic-press-start",
        ),
        # --- multi-press synthesis on -------------------------------------
        pytest.param(
            CFG_MAX2,
            [(0.0, DOWN), (0.12, UP), (0.52, TICK)],
            [P_DOWN, progress(1), click()],
            id="max2-single-click-pays-the-window",
        ),
        pytest.param(
            CFG_MAX2,
            [(0.0, DOWN), (0.12, UP), (0.28, DOWN), (0.38, UP)],
            [P_DOWN, progress(1), P_DOWN, multi(2)],
            id="max2-double-click-is-instant",
        ),
        pytest.param(
            CFG_MAX3,
            [
                (0.0, DOWN),
                (0.12, UP),
                (0.28, DOWN),
                (0.38, UP),
                (0.54, DOWN),
                (0.64, UP),
            ],
            [P_DOWN, progress(1), P_DOWN, progress(2), P_DOWN, multi(3)],
            id="max3-triple-click-is-instant",
        ),
        pytest.param(
            CFG_MAX2,
            [(0.0, TAP), (0.25, TAP)],
            [progress(1), multi(2)],
            id="max2-single-shot-double",
        ),
        pytest.param(
            CFG_MAX2_QUIET,
            [(0.0, DOWN), (0.12, UP), (0.52, TICK)],
            [P_DOWN, click()],
            id="max2-immediate-off-emits-nothing-early",
        ),
        # The window is paused while the button is down, so the banked click is
        # flushed when the hold is recognised - two emissions in one tick.
        pytest.param(
            CFG_MAX2,
            [(0.0, DOWN), (0.12, UP), (0.28, DOWN), (0.78, TICK), (2.0, UP)],
            [P_DOWN, progress(1), P_DOWN, click(), HOLD_START, HOLD_END],
            id="max2-click-then-hold-flushes-in-one-tick",
        ),
        # Documented artifact: a third press with max=2 degrades to
        # double-then-single, the way a mouse triple-click does.
        pytest.param(
            CFG_MAX2,
            [
                (0.0, DOWN),
                (0.12, UP),
                (0.28, DOWN),
                (0.38, UP),
                (0.54, DOWN),
                (0.64, UP),
                (1.04, TICK),
            ],
            [P_DOWN, progress(1), P_DOWN, multi(2), P_DOWN, progress(1), click()],
            id="max2-three-presses-degrades-to-double-then-single",
        ),
        # --- robustness ----------------------------------------------------
        pytest.param(
            CFG_NO_MULTI, [(0.0, UP)], [], id="release-without-press-ignored"
        ),
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, DOWN), (0.05, DOWN), (0.2, UP)],
            [P_DOWN, click()],
            id="duplicate-down-ignored",
        ),
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, TAP), (0.02, TAP)],
            [click()],
            id="two-taps-within-coalesce-window-are-one",
        ),
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, DOWN), (0.5, TICK), (0.5 + STUCK_TIMEOUT, TICK)],
            [P_DOWN, HOLD_START, HOLD_END],
            id="stuck-hold-synthesises-a-release",
        ),
        pytest.param(
            CFG_NO_HOLD,
            [(0.0, DOWN), (STUCK_TIMEOUT, TICK)],
            [P_DOWN],
            id="no-hold-threshold-still-arms-a-stuck-timer",
        ),
        # --- pre-decoded passthrough ---------------------------------------
        pytest.param(
            CFG_NO_MULTI,
            [(0.0, DecodedGesture.DOUBLE)],
            [multi(2)],
            id="decoded-double-passes-through",
        ),
        pytest.param(
            CFG_MAX2,
            [(0.0, DecodedGesture.HOLD_START), (1.0, DecodedGesture.HOLD_END)],
            [HOLD_START, HOLD_END],
            id="decoded-hold-passes-through",
        ),
    ],
)
def test_emission_sequences(config, script, expected) -> None:
    """The engine emits exactly this, in this order, at these times."""
    assert drive(GestureEngine(config), script) == expected


def test_stuck_hold_release_is_marked_synthetic() -> None:
    """A synthesised terminator is flagged so it is diagnosable."""
    engine = GestureEngine(CFG_NO_MULTI)
    engine.signal(DOWN, 0.0)
    engine.tick(0.5)
    step = engine.tick(0.5 + STUCK_TIMEOUT)

    assert [em.kind for em in step.emissions] == [Emission.HOLD_END]
    assert step.emissions[0].synthetic is True


def test_decoded_hold_start_is_terminated_if_the_hold_end_is_lost() -> None:
    """Pre-decoded holds get the same dangling-hold protection as synthesised ones.

    Hardware that reports a hold start whose matching hold end never arrives
    would otherwise leave a hold-to-dim automation running forever.
    """
    engine = GestureEngine(CFG_NO_MULTI)

    step = engine.decoded(DecodedGesture.HOLD_START, 0.0)
    assert [em.kind for em in step.emissions] == [Emission.HOLD_START]
    assert step.deadline == pytest.approx(STUCK_TIMEOUT)

    step = engine.tick(STUCK_TIMEOUT)
    assert [em.kind for em in step.emissions] == [Emission.HOLD_END]
    assert step.emissions[0].synthetic is True
    assert engine.state == "idle"


def test_decoded_hold_end_does_not_double_up() -> None:
    """A real hold end terminates the hold by itself."""
    engine = GestureEngine(CFG_NO_MULTI)
    engine.decoded(DecodedGesture.HOLD_START, 0.0)

    step = engine.decoded(DecodedGesture.HOLD_END, 1.0)

    assert [em.kind for em in step.emissions] == [Emission.HOLD_END]
    assert step.emissions[0].synthetic is False
    assert step.deadline is None


def test_a_decoded_hold_start_source_declares_the_terminator() -> None:
    """Because the stuck timer can synthesise long_press_end."""
    types = derive_event_types(
        button(
            [SHAPE_DECODED], CFG_NO_MULTI, decoded_gestures=(DecodedGesture.HOLD_START,)
        )
    )

    assert types == [EVENT_LONG_PRESS_START, EVENT_LONG_PRESS_END]


def test_reset_flush_terminates_a_hold() -> None:
    """A source going unavailable mid-hold must not leave the hold dangling."""
    engine = GestureEngine(CFG_NO_MULTI)
    engine.signal(DOWN, 0.0)
    engine.tick(0.5)

    step = engine.reset(flush=True)

    assert [em.kind for em in step.emissions] == [Emission.HOLD_END]
    assert step.deadline is None
    assert engine.state == "idle"


def test_reset_without_flush_emits_nothing() -> None:
    """On entity removal there is nobody left to tell."""
    engine = GestureEngine(CFG_NO_MULTI)
    engine.signal(DOWN, 0.0)
    engine.tick(0.5)

    assert engine.reset(flush=False).emissions == ()


def test_noop_step_preserves_the_pending_deadline() -> None:
    """A no-op must report the still-pending deadline, not None.

    The entity cancels and re-arms on every step, so a no-op returning None
    would silently drop the pending hold timer.
    """
    engine = GestureEngine(CFG_NO_MULTI)
    armed = engine.signal(DOWN, 0.0).deadline
    assert armed == pytest.approx(0.5)

    # A duplicate DOWN changes nothing and must not move the deadline.
    assert engine.signal(DOWN, 0.1).deadline == pytest.approx(armed)
    # Nor may an unmatched TAP while down.
    assert engine.signal(TAP, 0.2).deadline == pytest.approx(armed)


# --- translation ------------------------------------------------------------


@pytest.mark.parametrize(
    ("emission", "expected"),
    [
        (GestureEmission(Emission.PRESS_DOWN), (EVENT_PRESS_START, None)),
        (GestureEmission(Emission.CLICK_FINAL, 1), (EVENT_PRESS_END, None)),
        (GestureEmission(Emission.HOLD_START), (EVENT_LONG_PRESS_START, None)),
        (GestureEmission(Emission.HOLD_END), (EVENT_LONG_PRESS_END, None)),
        (
            GestureEmission(Emission.MULTI_FINAL, 2),
            (EVENT_MULTI_PRESS_END, {"multi_press_count": 2}),
        ),
        (
            GestureEmission(Emission.CLICK_PROGRESS, 1),
            (EVENT_MULTI_PRESS_ONGOING, {"multi_press_count": 1}),
        ),
    ],
)
def test_translate(emission, expected) -> None:
    assert translate(emission) == expected


def test_a_terminal_event_never_reports_a_count_of_one() -> None:
    """press_end is "the standard click"; one press is not a sequence."""
    event_type, attributes = translate(GestureEmission(Emission.CLICK_FINAL, 1))

    assert event_type == EVENT_PRESS_END
    assert attributes is None


# --- derive_event_types -----------------------------------------------------


def button(
    shapes, config, decoded_gestures=(DecodedGesture.DOUBLE,)
) -> ButtonConfig:
    """A ButtonConfig with one source per requested shape.

    A decoded source carries exactly one gesture, so a button that recognises
    several pre-decoded gestures has one source each. Keeping this faithful
    matters: the declared event types are derived from these sources, so a test
    that fed gestures no source declares would be testing a configuration the
    flow cannot produce.
    """
    sources: list[SourceConfig] = []
    for shape in shapes:
        if shape == SHAPE_DECODED:
            sources.extend(
                SourceConfig(shape=shape, gesture=gesture)
                for gesture in decoded_gestures
            )
        else:
            sources.append(SourceConfig(shape=shape))
    return ButtonConfig(name="Test", sources=tuple(sources), gesture_config=config)


@pytest.mark.parametrize(
    ("shapes", "config", "expected"),
    [
        pytest.param(
            [SHAPE_EDGE],
            CFG_NO_MULTI,
            [
                EVENT_PRESS_START,
                EVENT_PRESS_END,
                EVENT_LONG_PRESS_START,
                EVENT_LONG_PRESS_END,
            ],
            id="edge-with-hold",
        ),
        pytest.param(
            [SHAPE_EDGE],
            CFG_NO_HOLD,
            [EVENT_PRESS_START, EVENT_PRESS_END],
            id="edge-without-hold",
        ),
        pytest.param(
            [SHAPE_SINGLE_SHOT],
            CFG_NO_MULTI,
            [EVENT_PRESS_END],
            id="single-shot-cannot-report-press-start-or-holds",
        ),
        pytest.param(
            [SHAPE_SINGLE_SHOT],
            CFG_MAX2,
            [EVENT_PRESS_END, EVENT_MULTI_PRESS_ONGOING, EVENT_MULTI_PRESS_END],
            id="single-shot-with-multi-press",
        ),
        pytest.param(
            [SHAPE_SINGLE_SHOT],
            CFG_MAX2_QUIET,
            [EVENT_PRESS_END, EVENT_MULTI_PRESS_END],
            id="immediate-off-drops-ongoing",
        ),
        pytest.param(
            [SHAPE_DECODED],
            CFG_MAX2,
            [EVENT_MULTI_PRESS_END],
            id="decoded-only-declares-what-it-maps-to",
        ),
    ],
)
def test_derive_event_types(shapes, config, expected) -> None:
    assert derive_event_types(button(shapes, config)) == expected


def test_event_types_are_ordered_by_declaration_not_sorted() -> None:
    """Capability attributes land in the entity registry; order must be stable."""
    types = derive_event_types(button([SHAPE_EDGE], CFG_MAX2))

    assert types == sorted(types, key=list(
        (
            EVENT_PRESS_START,
            EVENT_PRESS_END,
            EVENT_LONG_PRESS_START,
            EVENT_LONG_PRESS_END,
            EVENT_MULTI_PRESS_ONGOING,
            EVENT_MULTI_PRESS_END,
        )
    ).index)
    assert types != sorted(types), "alphabetical order would be a regression"


def test_a_button_with_no_sources_is_refused() -> None:
    """event_types would be empty and EventEntity.event_types would raise."""
    with pytest.raises(ValueError, match="no sources"):
        ButtonConfig.from_dict({"name": "Broken", "sources": []})


# --- invariants over random input -------------------------------------------

_CONFIG_MATRIX = [
    GestureConfig(
        long_press_threshold=threshold,
        multi_press_window=0.4,
        multi_press_max=maximum,
        multi_press_immediate=immediate,
    )
    for threshold in (None, 0.5)
    for maximum in (1, 2, 3)
    for immediate in (False, True)
]

_SHAPE_SETS = [
    [SHAPE_EDGE],
    [SHAPE_SINGLE_SHOT],
    [SHAPE_DECODED],
    [SHAPE_EDGE, SHAPE_SINGLE_SHOT],
    [SHAPE_EDGE, SHAPE_SINGLE_SHOT, SHAPE_DECODED],
]


ALL_DECODED = tuple(DecodedGesture)


def _actions_for(shapes, decoded_gestures=ALL_DECODED) -> list:
    actions: list = []
    if SHAPE_EDGE in shapes:
        actions += [DOWN, UP]
    if SHAPE_SINGLE_SHOT in shapes:
        actions.append(TAP)
    if SHAPE_DECODED in shapes:
        actions += list(decoded_gestures)
    return actions


def _random_walk(engine, actions, rng, steps=60):
    """Feed random input, ticking whenever the rng says to. Yields each step."""
    now = 0.0
    deadline: float | None = None

    for _ in range(steps):
        if deadline is not None and rng.random() < 0.35:
            now = deadline
            step = engine.tick(now)
        else:
            now += rng.choice([0.01, 0.03, 0.2, 0.45, 0.6])
            if deadline is not None and now > deadline:
                now = deadline
                step = engine.tick(now)
            else:
                action = rng.choice(actions)
                step = (
                    engine.decoded(action, now)
                    if isinstance(action, DecodedGesture)
                    else engine.signal(action, now)
                )
        deadline = step.deadline
        yield now, step


@pytest.mark.parametrize("config", _CONFIG_MATRIX, ids=str)
@pytest.mark.parametrize("shapes", _SHAPE_SETS, ids=lambda s: "+".join(s))
def test_every_reachable_emission_is_declared(config, shapes) -> None:
    """The invariant that makes _trigger_event's ValueError unreachable.

    EventEntity rejects an event type the entity did not declare, so anything
    the engine can produce for a configuration must appear in
    derive_event_types() for that same configuration.
    """
    # Declare a source for every decoded gesture we are going to feed, so the
    # configuration under test is one the config flow could actually produce.
    declared = set(
        derive_event_types(button(shapes, config, decoded_gestures=ALL_DECODED))
    )
    engine = GestureEngine(config)
    rng = random.Random(f"{config}{shapes}")

    for _, step in _random_walk(engine, _actions_for(shapes), rng):
        for emission in step.emissions:
            event_type, _ = translate(emission)
            assert event_type in declared, (
                f"engine emitted {event_type!r} which is not declared: {declared}"
            )


@pytest.mark.parametrize("config", _CONFIG_MATRIX, ids=str)
def test_engine_invariants_hold_under_random_input(config) -> None:
    """Structural guarantees that keep the caller's timer loop sane."""
    engine = GestureEngine(config)
    rng = random.Random(str(config))
    holding = False

    for now, step in _random_walk(engine, [DOWN, UP, TAP], rng, steps=200):
        # A deadline in the past would busy-loop the caller's timer.
        if step.deadline is not None:
            assert step.deadline > now, f"deadline {step.deadline} not after {now}"

        assert engine.pending_count <= max(1, config.multi_press_max), (
            "banked clicks exceeded the configured maximum"
        )

        for emission in step.emissions:
            if emission.kind is Emission.HOLD_START:
                assert not holding, "two HOLD_STARTs without an intervening HOLD_END"
                holding = True
            elif emission.kind is Emission.HOLD_END:
                assert holding, "HOLD_END without a preceding HOLD_START"
                holding = False

            if emission.kind is Emission.MULTI_FINAL:
                assert emission.count is not None and emission.count >= 2, (
                    "a multi-press terminal event must report at least two presses"
                )


@pytest.mark.parametrize("config", _CONFIG_MATRIX, ids=str)
def test_engine_always_returns_to_idle_when_left_alone(config) -> None:
    """No input plus enough ticks must always drain to idle.

    Guards against a state that stays armed forever, and against a tick that
    neither clears nor advances its deadline.
    """
    engine = GestureEngine(config)
    now = 0.0
    step = engine.signal(DOWN, now)

    for _ in range(10):
        if step.deadline is None:
            break
        assert step.deadline > now
        now = step.deadline
        step = engine.tick(now)

    assert step.deadline is None
    assert engine.state == "idle"
    assert engine.pending_count == 0
