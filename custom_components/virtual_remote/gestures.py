"""Pure gesture-synthesis state machine for a single virtual button.

This module MUST NOT import anything from homeassistant. It is enforced by
tools/check_engine_purity.py, because the boundary is what makes the engine
unit-testable in milliseconds and extractable to a standalone package later.

The engine holds no clock and arms no timers. It is a synchronous function of
(state, signal, now) returning the gestures to emit plus the absolute instant at
which it next needs waking. The caller owns every timer, which reduces timer
discipline to exactly one cancel/re-arm site.

Two rules eliminate whole classes of bug:

1. While a press is physically down, the multi-press window is paused. At most
   one deadline is ever armed, so the multi-press window and the long-press
   threshold can never race.
2. Deadlines are absolute and reported on EVERY step, including no-ops. The
   caller cancels-then-re-arms unconditionally; because a no-op step reports the
   still-pending deadline, re-arming lands on the same absolute instant instead
   of silently dropping or postponing it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum, auto

_LOGGER = logging.getLogger(__name__)

# Two clicks closer together than this are treated as one. No human
# double-presses this fast, and it absorbs duplicate Zigbee frame
# retransmission, which is a real phenomenon. Judgement call, not measured -
# a module constant so it can be tuned centrally without a config migration.
TAP_COALESCE: float = 0.05

# A button held longer than this is not a gesture. Guards against a release
# signal that never arrives, which would otherwise pause the multi-press window
# forever and hang the click count.
STUCK_TIMEOUT: float = 60.0


class RawSignal(StrEnum):
    """A normalised input from a hardware source."""

    DOWN = auto()
    """An edge source asserted - the button is now physically down."""
    UP = auto()
    """An edge source released."""
    TAP = auto()
    """One atomic interaction from a single-shot source; no duration available."""


class DecodedGesture(StrEnum):
    """A gesture the hardware already decoded for us; passed straight through."""

    CLICK = auto()
    DOUBLE = auto()
    TRIPLE = auto()
    QUADRUPLE = auto()
    HOLD_START = auto()
    HOLD_END = auto()


class Emission(StrEnum):
    """The engine's abstract output vocabulary.

    Deliberately not Home Assistant's ButtonEventType: keeping the engine's
    vocabulary its own is what lets this module stay free of homeassistant
    imports. The caller owns the translation table.
    """

    PRESS_DOWN = auto()
    CLICK_PROGRESS = auto()
    CLICK_FINAL = auto()
    MULTI_FINAL = auto()
    HOLD_START = auto()
    HOLD_END = auto()


class _State(StrEnum):
    IDLE = auto()
    """Nothing down, no multi-press window open."""
    DOWN = auto()
    """Physically down; a hold has not yet been recognised."""
    COUNT = auto()
    """Nothing down, but a multi-press window is open."""
    HOLD = auto()
    """A hold was recognised; awaiting release."""


class _Deadline(StrEnum):
    HOLD_THRESHOLD = auto()
    MULTI_WINDOW = auto()
    STUCK = auto()


@dataclass(frozen=True, slots=True)
class GestureEmission:
    """One gesture to emit."""

    kind: Emission
    count: int | None = None
    synthetic: bool = False
    """True when produced by a timeout or reset rather than by real input."""


@dataclass(frozen=True, slots=True)
class EngineStep:
    """The result of feeding one signal or tick to the engine."""

    emissions: tuple[GestureEmission, ...]
    deadline: float | None
    """Absolute, on the same scale as `now`. Always the engine's current
    deadline, even when `emissions` is empty - see rule 2 in the module docstring.
    """


@dataclass(frozen=True, slots=True)
class GestureConfig:
    """Per-button behaviour. All times in seconds."""

    long_press_threshold: float | None = 0.5
    """None disables long-press synthesis entirely."""
    multi_press_window: float = 0.4
    multi_press_max: int = 1
    """1 disables multi-press synthesis. See the latency note below."""
    multi_press_immediate: bool = True
    """Emit CLICK_PROGRESS as each non-terminal click lands, so a single-shot
    source produces *something* without waiting out the window."""


_DECODED_MAP: dict[DecodedGesture, tuple[GestureEmission, ...]] = {
    DecodedGesture.CLICK: (GestureEmission(Emission.CLICK_FINAL, 1),),
    DecodedGesture.DOUBLE: (GestureEmission(Emission.MULTI_FINAL, 2),),
    DecodedGesture.TRIPLE: (GestureEmission(Emission.MULTI_FINAL, 3),),
    DecodedGesture.QUADRUPLE: (GestureEmission(Emission.MULTI_FINAL, 4),),
    DecodedGesture.HOLD_START: (GestureEmission(Emission.HOLD_START),),
    DecodedGesture.HOLD_END: (GestureEmission(Emission.HOLD_END),),
}


class GestureEngine:
    """Synthesises gestures for one virtual button.

    Latency note: when multi-press synthesis is on, a click cannot be reported
    as final until the window closes, because it might turn out to be the first
    of two. The mitigation is terminate-on-max - once `multi_press_max` clicks
    have landed the sequence is provably complete, so the highest configured
    gesture is always zero-latency and only shorter ones pay the window.
    """

    def __init__(self, config: GestureConfig, name: str = "?") -> None:
        """Initialise in the idle state."""
        self._cfg = config
        self._name = name
        self._state = _State.IDLE
        self._count = 0
        self._deadline: float | None = None
        self._deadline_kind: _Deadline | None = None
        self._last_click_at: float | None = None

    # --- introspection, for diagnostics and tests ---------------------------

    @property
    def state(self) -> str:
        """Current state name."""
        return self._state.value

    @property
    def pending_count(self) -> int:
        """Clicks banked but not yet reported as a terminal gesture."""
        return self._count

    # --- internals ----------------------------------------------------------

    def _arm(self, kind: _Deadline, at: float) -> None:
        self._deadline_kind, self._deadline = kind, at

    def _disarm(self) -> None:
        self._deadline_kind = self._deadline = None

    def _step(self, *emissions: GestureEmission) -> EngineStep:
        return EngineStep(emissions, self._deadline)

    def _to_idle(self) -> None:
        self._state, self._count = _State.IDLE, 0
        self._disarm()

    def _flush(self) -> tuple[GestureEmission, ...]:
        """The terminal gesture for the clicks banked so far.

        A single press emits CLICK_FINAL, not MULTI_FINAL with a count of 1:
        upstream calls press_end "the standard click", and one press is not a
        sequence. So a terminal event never carries a count of 1.
        """
        if self._count == 1:
            return (GestureEmission(Emission.CLICK_FINAL, 1),)
        return (GestureEmission(Emission.MULTI_FINAL, self._count),)

    def _coalesced(self, now: float) -> bool:
        return (
            self._last_click_at is not None
            and now - self._last_click_at < TAP_COALESCE
        )

    def _press_deadline(self, now: float) -> tuple[_Deadline, float]:
        if self._cfg.long_press_threshold is not None:
            return _Deadline.HOLD_THRESHOLD, now + self._cfg.long_press_threshold
        # Without a hold threshold there is nothing else to wake us, and a
        # press that is never released would pause the window indefinitely.
        return _Deadline.STUCK, now + STUCK_TIMEOUT

    def _click(
        self, now: float, *pre: GestureEmission
    ) -> EngineStep:
        """Bank one completed click. Shared by a short UP and by a TAP."""
        self._last_click_at = now
        self._count += 1

        if self._cfg.multi_press_max <= 1:
            out = (*pre, GestureEmission(Emission.CLICK_FINAL, 1))
            self._to_idle()
            return self._step(*out)

        if self._count >= self._cfg.multi_press_max:
            # Provably complete: report immediately rather than waiting.
            out = (*pre, GestureEmission(Emission.MULTI_FINAL, self._count))
            self._to_idle()
            return self._step(*out)

        out_list = list(pre)
        if self._cfg.multi_press_immediate:
            out_list.append(GestureEmission(Emission.CLICK_PROGRESS, self._count))
        self._state = _State.COUNT
        self._arm(_Deadline.MULTI_WINDOW, now + self._cfg.multi_press_window)
        return self._step(*out_list)

    # --- public API ---------------------------------------------------------

    def signal(
        self, sig: RawSignal, now: float, source_id: str = "?"
    ) -> EngineStep:
        """Feed one normalised hardware signal."""
        if sig is RawSignal.DOWN:
            if self._state in (_State.DOWN, _State.HOLD):
                _LOGGER.debug(
                    "%s: duplicate DOWN from %s ignored in state %s",
                    self._name,
                    source_id,
                    self._state,
                )
                return self._step()
            if self._coalesced(now):
                _LOGGER.debug("%s: DOWN from %s coalesced", self._name, source_id)
                return self._step()
            # From IDLE (count 0) or COUNT (count N) - the count is preserved,
            # and the window deadline is replaced by the press deadline.
            self._state = _State.DOWN
            self._arm(*self._press_deadline(now))
            return self._step(GestureEmission(Emission.PRESS_DOWN))

        if sig is RawSignal.UP:
            if self._state is _State.HOLD:
                self._to_idle()
                return self._step(GestureEmission(Emission.HOLD_END))
            if self._state is not _State.DOWN:
                _LOGGER.debug(
                    "%s: UP from %s with no matching DOWN", self._name, source_id
                )
                return self._step()
            # A press released before the hold threshold is exactly a click.
            return self._click(now)

        if self._state in (_State.DOWN, _State.HOLD):
            _LOGGER.debug(
                "%s: TAP from %s while an edge source is down; ignored",
                self._name,
                source_id,
            )
            return self._step()
        if self._coalesced(now):
            _LOGGER.debug("%s: TAP from %s coalesced", self._name, source_id)
            return self._step()
        return self._click(now)

    def tick(self, now: float) -> EngineStep:
        """Feed the expiry of the deadline previously reported."""
        kind = self._deadline_kind
        self._disarm()

        if kind is _Deadline.HOLD_THRESHOLD:
            # Any clicks banked before this press are a completed sequence.
            pre = self._flush() if self._count else ()
            self._state, self._count = _State.HOLD, 0
            self._arm(_Deadline.STUCK, now + STUCK_TIMEOUT)
            return self._step(*pre, GestureEmission(Emission.HOLD_START))

        if kind is _Deadline.MULTI_WINDOW:
            out = self._flush()
            self._to_idle()
            return self._step(*out)

        if kind is _Deadline.STUCK:
            _LOGGER.warning(
                "%s: press was never released within %ss; synthesising a release",
                self._name,
                STUCK_TIMEOUT,
            )
            out: tuple[GestureEmission, ...] = ()
            if self._state is _State.HOLD:
                out = (GestureEmission(Emission.HOLD_END, synthetic=True),)
            elif self._state is _State.DOWN and self._count:
                # HOLD_START was never emitted, so there is nothing to
                # terminate - just report the clicks banked before this press.
                out = self._flush()
            self._to_idle()
            return self._step(*out)

        return self._step()

    def decoded(self, gesture: DecodedGesture, now: float) -> EngineStep:
        """Feed a gesture the hardware already decoded.

        A decoded hold start still enters the HOLD state and arms the stuck
        timer, even though no synthesis is involved: if the hardware's matching
        hold-end is lost in transit, the hold would otherwise dangle forever and
        a hold-to-dim automation would never stop.
        """
        pre: tuple[GestureEmission, ...] = ()
        if self._state is _State.COUNT:
            pre = self._flush()
        elif self._state is _State.HOLD and gesture is not DecodedGesture.HOLD_END:
            # Terminate the previous hold before starting anything new; a real
            # HOLD_END terminates it by itself, so do not double up.
            pre = (GestureEmission(Emission.HOLD_END, synthetic=True),)

        self._to_idle()

        if gesture is DecodedGesture.HOLD_START:
            self._state = _State.HOLD
            self._arm(_Deadline.STUCK, now + STUCK_TIMEOUT)

        return self._step(*pre, *_DECODED_MAP[gesture])

    def reset(self, *, flush: bool) -> EngineStep:
        """Abandon any gesture in progress.

        Pass flush=True when a source became unavailable: an un-terminated hold
        is the worst failure mode here (a hold-to-dim automation that never
        stops), so emitting a synthetic terminator is safer than silence.
        Pass flush=False when the entity is being removed - emitting state for
        an entity about to vanish is pointless.
        """
        out: tuple[GestureEmission, ...] = ()
        if flush:
            if self._state is _State.COUNT:
                out = self._flush()
            elif self._state is _State.HOLD:
                out = (GestureEmission(Emission.HOLD_END, synthetic=True),)
        self._to_idle()
        return self._step(*out)
