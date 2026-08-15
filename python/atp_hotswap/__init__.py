"""``SRS-RESV-003`` — the surface-neutral Hot-Swap trigger client (SyRS SYS-49a).

SYS-49a gives the operator three arms: the dashboard, the CLI, and the REST API. Two of them
are Python — ``atp_dashboard`` renders the trigger configuration on the UI-5 pane, and
``atp_orchestration`` serves it over REST — and both must read exactly the same fact from
exactly the same place, or they will eventually disagree about whether an automatic Hot-Swap
can fire.

This module is the one implementation they share. It lives in its own package rather than in
either surface for a specific reason: ``atp_orchestration`` importing ``atp_dashboard`` (or
the reverse) couples two operator surfaces that are peers, while the dependency direction the
runtime documents is that surfaces compose onto ``atp_runtime`` from above and never onto each
other. A neutral package lets both import downward.

It also fixes the failure that made co-location tempting in the first place: the exception
type below is declared HERE and re-exported by ``atp_dashboard.hotswap``, so the class the
pane catches and the class this client raises are the *same object*. Two same-named exceptions
in two modules is not a naming quibble — the provider's ``except`` clause misses the foreign
one entirely, and an unreadable configuration 500s the whole snapshot route instead of
rendering its honest error state.

Everything here shells the cargo-built ``resv003_hot_swap_trigger_cli`` (the repo's
cross-language boundary pattern: subprocess → Rust binary → parse ``key:value`` stdout), so the
trigger decision, the fail-closed logging rule, and the durable configuration format keep a
single implementation in Rust.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

__all__ = [
    "CliHotSwapCooldownSource",
    "CliHotSwapDemotionSource",
    "CliHotSwapPromotionSource",
    "CliHotSwapTriggerSource",
    "CompositeHotSwapStatusSource",
    "HotSwapCooldownLeg",
    "HotSwapDemotionLeg",
    "HotSwapTriggerLeg",
    "HotSwapStatusUnavailable",
    "HotSwapPromotionLeg",
    "HotSwapTriggerCliRunner",
    "PROMOTE_BINARY_ENV_KNOB",
    "default_promote_binary",
    "HotSwapTriggerCliUnavailable",
    "HotSwapTriggerOutputUnreadable",
    "BINARY_ENV_KNOB",
    "COOLDOWN_BINARY_ENV_KNOB",
    "DEMOTION_BINARY_ENV_KNOB",
    "default_binary",
    "default_cooldown_binary",
    "default_demotion_binary",
    "parse_trigger_cli_output",
    "strict_trigger_bool",
]

#: Environment override for the operator binary's location.
#:
#: The fallback below is a DEVELOPMENT path — `cargo build` output inside the checkout — and
#: a deployed image has no reason to keep that layout. Without a knob the surface would look
#: mounted and then fail on first use in production, so the path is configurable and the
#: fallback is only the convenience default for a working tree.
BINARY_ENV_KNOB = "ATP_HOT_SWAP_TRIGGER_BINARY"

#: Fallback location of the cargo-built operator binary, relative to the repo root
#: (python/atp_hotswap/__init__.py -> parents[2] == repo root). Build it with
#: ``cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli``.
_DEFAULT_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv003_hot_swap_trigger_cli"
)


def default_binary(env: "Mapping[str, str] | None" = None) -> Path:
    """The operator binary's path: the env override when set, else the dev-tree fallback."""

    import os

    source = os.environ if env is None else env
    override = source.get(BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_BINARY


#: Per-invocation subprocess budget (seconds): a wedged binary surfaces an explicit
#: unavailable state, never an indefinite hang of the operator surface.
_DEFAULT_TIMEOUT_S = 30.0


class HotSwapStatusUnavailable(Exception):
    """A Hot-Swap status source cannot be read right now.

    Reported to the operator verbatim — never swallowed into a clean-looking
    snapshot, and never treated as "no swap in progress".
    """


class HotSwapTriggerCliUnavailable(HotSwapStatusUnavailable):
    """The trigger binary could not be RUN — it timed out, or could not be launched.

    A subclass so the pane still degrades on the base class, while the REST arm can tell a
    wedged or missing dependency apart from a configuration it read but could not parse.
    Those need different operator responses (restart/repair the binary versus repair the
    file), and reporting one as the other sends the operator to the wrong place.
    """


class HotSwapTriggerOutputUnreadable(HotSwapStatusUnavailable):
    """The trigger CLI answered, but not in a shape this reader can trust.

    A subclass so the pane's blanket `except HotSwapStatusUnavailable` still degrades
    honestly, while the REST arm can tell an unparseable answer apart from an unreadable
    file and name the right cause.
    """


#: Default location of the cargo-built operator binary, relative to the repo root
#: (python/atp_dashboard/hotswap.py -> parents[2] == repo root). Build it with
#: ``cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli``.
_DEFAULT_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv003_hot_swap_trigger_cli"
)

#: Per-invocation subprocess budget (seconds): a wedged binary surfaces an explicit
#: unavailable state, never an indefinite hang of the operator surface.
_DEFAULT_TIMEOUT_S = 30.0


class HotSwapTriggerCliRunner(Protocol):
    """The subprocess surface this module depends on (injectable for tests)."""

    def __call__(self, argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]: ...


def _default_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the trigger CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"hot-swap trigger binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def parse_trigger_cli_output(stdout: str) -> dict[str, str]:
    """Parse the bin's deterministic ``key:value`` proof lines."""

    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        if key in values and values[key] != value:
            # Ambiguity is refused, not resolved — the rule json_scan already applies to a
            # persisted record, applied to the proof stream that stands in for one. A
            # version-skewed or wrong binary emitting two different `manual-logged` or
            # `trigger-record-ordinal` lines has not said which is true, and last-one-wins
            # would let the handler accept whichever happened to come second as durable
            # evidence.
            raise HotSwapTriggerOutputUnreadable(
                f"resv003_hot_swap_trigger_cli emitted contradictory {key!r} lines "
                f"({values[key]!r} then {value!r}); the proof stream is ambiguous"
            )
        values[key] = value
    return values


def strict_trigger_bool(values: dict[str, str], key: str) -> bool:
    """Read a boolean proof line, refusing anything that is not literally ``true``/``false``.

    A missing or misspelled key must not read as ``False``: every flag here answers "may an
    automatic Hot-Swap fire", and the absent-means-off reading is precisely the false
    all-clear this surface exists to avoid.
    """

    raw = values.get(key)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise HotSwapTriggerOutputUnreadable(
        f"resv003_hot_swap_trigger_cli emitted no readable {key!r} line "
        f"(got {raw!r}); refusing to report a trigger configuration that cannot be evidenced"
    )


class CliHotSwapTriggerSource:
    """A concrete ``atp_dashboard.hotswap.HotSwapStatusSource`` backed by the real binary.

    Implements the protocol structurally (duck-typed, like every other provider source here),
    so the module that declares it never has to name an implementation.

    Only :meth:`trigger_config` resolves HERE — this is ``SRS-RESV-003``'s leg.
    :meth:`live_state` and :meth:`promotion_candidate` return ``None`` because they are
    not this source's facts, and inventing one would be exactly the fabrication the
    pane's deferred cells exist to prevent.

    Both are now produced by SIBLING legs, composed through
    :class:`CompositeHotSwapStatusSource` rather than folded in here:
    :class:`CliHotSwapDemotionSource` (``SRS-RESV-004``: demotion-pending) and
    :class:`CliHotSwapPromotionSource` (``SRS-RESV-005``: the current live strategy).
    ``promotion_candidate`` is still genuinely unproduced (owner ``SRS-RESV-002``).

    The legs fail INDEPENDENTLY, as the protocol requires: an unreadable trigger
    configuration must not blank an otherwise readable live state, so each raises on its
    own rather than through a shared cached read.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        binary: str | Path | None = None,
        runner: HotSwapTriggerCliRunner | None = None,
        timeout: float | None = None,
        log_path: str | Path | None = None,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else default_binary()
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout) if timeout is not None else _DEFAULT_TIMEOUT_S
        self._log_path = str(log_path) if log_path is not None else None

    # ------------------------------------------------------------------ #
    # HotSwapStatusSource
    # ------------------------------------------------------------------ #

    def trigger_config(self) -> dict[str, object] | None:
        """The persisted automatic-trigger enabled-state (``SRS-RESV-003``).

        ``None`` when nothing has ever been configured (no file) — the pane may then state
        the all-disabled default truthfully. Raises :class:`HotSwapStatusUnavailable` when
        the configuration exists but cannot be read.
        """

        completed = self._invoke(["config", "--state", self._state_path])
        if completed.returncode != 0:
            raise HotSwapStatusUnavailable(
                f"hot-swap trigger configuration unreadable: {completed.stderr.strip()}"
            )
        values = parse_trigger_cli_output(completed.stdout)
        source = values.get("config-source")
        if source not in ("default", "persisted"):
            raise HotSwapStatusUnavailable(
                f"hot-swap trigger configuration reported an unknown source {source!r}"
            )
        # A readable producer ALWAYS returns a payload, including for the never-configured
        # case — `None` would render every chip deferred, i.e. "SRS-RESV-003 has not
        # produced this yet", which stopped being true the moment this source was mounted.
        # The runtime posture is known and it is disabled; saying so is what makes the
        # SYS-49a "automatic triggers default to disabled" clause observable rather than
        # merely unclaimed. Provenance is not lost — `config_source` carries it, so a caller
        # can still tell "off by default" from "an operator turned it off".
        return self._config_payload(values, source=source)

    def live_state(self) -> dict[str, object] | None:
        """No producer persists a queryable live-swap state (owner ``SRS-RESV-004``/``005``/
        ``006``), so there is no fact to report and none is invented."""

        return None

    def promotion_candidate(self) -> dict[str, object] | None:
        """No ranking engine names a candidate yet (owner ``SRS-RESV-002``)."""

        return None

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    @staticmethod
    def _config_payload(values: dict[str, str], *, source: str) -> dict[str, object]:
        """The shape ``HotSwapStatusProvider`` reads.

        The per-trigger detail is NESTED under each ``kind`` because that is how the pane
        looks it up (``triggers[kind]["enabled"]`` — ``atp_dashboard.hotswap``'s
        ``auto_triggers_live`` comprehension). A flat ``<kind>_enabled`` key parses fine and
        resolves nothing: every chip silently keeps rendering its deferred placeholder while
        the aggregate above it goes live, which reads as "configured, but no trigger is" —
        worse than an honest deferral because it looks resolved.
        """

        drawdown_enabled = strict_trigger_bool(values, "drawdown-demotion-enabled")
        drawdown: dict[str, object] = {"enabled": drawdown_enabled}
        if drawdown_enabled:
            raw = values.get("drawdown-demotion-threshold-bps")
            if raw is None or not raw.isdigit():
                raise HotSwapStatusUnavailable(
                    "hot-swap drawdown trigger reports enabled with no readable threshold "
                    f"(got {raw!r})"
                )
            drawdown["threshold_bps"] = int(raw)
        return {
            # Provenance: "default" (never configured, so the disabled values below are the
            # built-in posture) vs "persisted" (an operator chose them).
            "config_source": source,
            "any_enabled": strict_trigger_bool(values, "any-automatic-enabled"),
            "manual_promotion_available": strict_trigger_bool(values, "manual-promotion-available"),
            "default_disabled": strict_trigger_bool(values, "default-disabled"),
            "drawdown_demotion": drawdown,
            "top_ranked_promotion": {
                "enabled": strict_trigger_bool(values, "top-ranked-promotion-enabled")
            },
            "highest_momentum_promotion": {
                "enabled": strict_trigger_bool(values, "highest-momentum-promotion-enabled")
            },
        }

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise HotSwapTriggerCliUnavailable(
                f"resv003_hot_swap_trigger_cli timed out after {self._timeout}s"
            ) from expired
        except OSError as launch_error:
            raise HotSwapTriggerCliUnavailable(
                "resv003_hot_swap_trigger_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv003_hot_swap_trigger_cli`): "
                f"{launch_error}"
            ) from launch_error


# ============================================================================== #
# SRS-RESV-004 — the demotion-pending leg (SyRS SYS-49b / SYS-49c)
# ============================================================================== #

#: Environment override for the demotion binary's location. Same reasoning as
#: :data:`BINARY_ENV_KNOB`: a deployed image has no reason to keep the dev-tree layout.
DEMOTION_BINARY_ENV_KNOB = "ATP_HOT_SWAP_DEMOTION_BINARY"

#: Fallback location of the cargo-built demotion binary, relative to the repo root. Build it
#: with ``cargo build -p atp-orchestrator --bin resv004_hot_swap_demotion_cli``.
_DEFAULT_DEMOTION_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv004_hot_swap_demotion_cli"
)


def default_demotion_binary(env: "Mapping[str, str] | None" = None) -> Path:
    """The demotion binary's path: the env override when set, else the dev-tree fallback."""

    import os

    source = os.environ if env is None else env
    override = source.get(DEMOTION_BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_DEMOTION_BINARY


def _demotion_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the demotion CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"hot-swap demotion binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin resv004_hot_swap_demotion_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


class CliHotSwapDemotionSource:
    """``live_state()`` from the durable SRS-RESV-004 demotion-pending lockout.

    The producer the UI-5 pane has been waiting on. ``atp_dashboard.hotswap`` renders
    ``demotion_pending`` / ``demotion_detail`` as ``deferred:SRS-RESV-004`` cells whenever no
    source resolves them; this class resolves them by shelling
    ``resv004_hot_swap_demotion_cli status``, which reads the same file the demotion gate
    engages. One format owner, in Rust, for the durable record — the repo's cross-language
    boundary pattern.

    **Three states, and only two of them are answers.** No lockout is ``demotion_pending:
    False``; a held lockout is ``True`` with the detail; an UNREADABLE lockout is
    :class:`HotSwapStatusUnavailable`, which the pane renders as ``value: None``. That third
    case is the whole point: a corrupt lockout that rendered as "no demotion is pending" would
    be a false all-clear about whether a live changeover is half-finished.

    The other legs return ``None`` rather than inventing a value: the current live strategy is
    ``SRS-RESV-005``'s and the cool-down is ``SRS-RESV-006``'s, and neither persists a queryable
    fact yet.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        binary: str | Path | None = None,
        runner: HotSwapTriggerCliRunner | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else default_demotion_binary()
        self._runner = runner or _demotion_runner
        self._timeout = timeout

    @property
    def state_path(self) -> str:
        return self._state_path

    def live_state(self) -> dict[str, object] | None:
        """The demotion-pending facts, or raise when the lockout cannot be read.

        Never returns ``None``: a readable producer always has an answer, and ``None`` would
        render the cells deferred — i.e. "SRS-RESV-004 has not produced this yet", which stopped
        being true the moment this source was mounted.
        """

        completed = self._invoke(["status", "--state", self._state_path])
        if completed.returncode != 0:
            raise HotSwapStatusUnavailable(
                f"hot-swap demotion state unreadable: {completed.stderr.strip()}"
            )
        values = parse_trigger_cli_output(completed.stdout)
        source = values.get("state-source")
        if source not in ("clear", "pending"):
            raise HotSwapTriggerOutputUnreadable(
                f"resv004_hot_swap_demotion_cli reported an unknown state source {source!r}"
            )
        pending = strict_trigger_bool(values, "demotion-pending")
        # Cross-check the two facts against each other rather than trusting either alone: a
        # binary that said `state-source:clear` alongside `demotion-pending:true` has not told
        # us which is true, and picking one would publish a swap state nobody asserted.
        if pending != (source == "pending"):
            raise HotSwapTriggerOutputUnreadable(
                f"resv004_hot_swap_demotion_cli reported state-source {source!r} with "
                f"demotion-pending {pending!r}; the proof stream contradicts itself"
            )
        detail = values.get("demotion-detail")
        if not detail:
            raise HotSwapTriggerOutputUnreadable(
                "resv004_hot_swap_demotion_cli emitted no demotion-detail line; refusing to "
                "report a demotion state with no description"
            )

        state: dict[str, object] = {
            "demotion_pending": pending,
            "demotion_detail": detail,
        }
        if pending:
            # The SYS-49b changeover ladder's timeout branch. Resolved ONLY while a demotion is
            # actually pending — a clear lockout says nothing about whether that phase was ever
            # reached, and claiming otherwise would light a rung on no evidence.
            state["sequence"] = {"demotion_pending": {"status": "BLOCKED", "detail": detail}}
        return state

    def trigger_config(self) -> dict[str, object] | None:
        """Not this source's fact — the trigger configuration is ``SRS-RESV-003``'s."""

        return None

    def promotion_candidate(self) -> dict[str, object] | None:
        """Not this source's fact — the ranking candidate is ``SRS-RESV-002``'s."""

        return None

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise HotSwapTriggerCliUnavailable(
                f"resv004_hot_swap_demotion_cli timed out after {self._timeout}s"
            ) from expired
        except OSError as launch_error:
            raise HotSwapTriggerCliUnavailable(
                "resv004_hot_swap_demotion_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv004_hot_swap_demotion_cli`): "
                f"{launch_error}"
            ) from launch_error


class HotSwapTriggerLeg(Protocol):
    """The trigger + candidate half of the pane's source protocol (``SRS-RESV-003``/``002``)."""

    def trigger_config(self) -> "Mapping[str, object] | None": ...

    def promotion_candidate(self) -> "Mapping[str, object] | None": ...


# ============================================================================== #
# SRS-RESV-006 — the cool-down leg (SyRS SYS-49e)
# ============================================================================== #

#: Environment override for the cool-down binary's location.
COOLDOWN_BINARY_ENV_KNOB = "ATP_HOT_SWAP_COOLDOWN_BINARY"

_DEFAULT_COOLDOWN_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv006_hot_swap_cooldown_cli"
)


def default_cooldown_binary(env: "Mapping[str, str] | None" = None) -> Path:
    """The cool-down binary's path: the env override when set, else the dev-tree fallback."""

    import os

    source = os.environ if env is None else env
    override = source.get(COOLDOWN_BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_COOLDOWN_BINARY


def _cooldown_runner(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run the cool-down CLI with ``argv`` as a list (``shell=False``)."""

    if not Path(argv[0]).exists():
        raise FileNotFoundError(
            f"hot-swap cool-down binary not found at {argv[0]}; build it with "
            "`cargo build -p atp-orchestrator --bin resv006_hot_swap_cooldown_cli`"
        )
    return subprocess.run(argv, check=False, capture_output=True, text=True, timeout=timeout)


def _iso_utc(epoch_seconds: int) -> str:
    """Render epoch seconds as the ISO-8601 UTC string the pane's dial parses."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch_seconds))


class CliHotSwapCooldownSource:
    """The ``cooldown`` half of ``live_state()``, from the durable SRS-RESV-006 window.

    ``atp_dashboard.hotswap`` renders ``cooldown.in_effect`` / ``started_at`` /
    ``expires_at`` as ``deferred:SRS-RESV-006`` cells whenever no source resolves them.
    This class resolves them by shelling ``resv006_hot_swap_cooldown_cli status``, which
    reads the same file the SYS-49e gate classifies — one format owner, in Rust, for the
    durable record.

    **Three states, and only two of them are answers.** No swap ever → ``in_effect: False``
    with NO timestamps (there is genuinely no window to date); an open or closed window →
    the boolean plus both ISO-8601 UTC strings; an UNREADABLE window →
    :class:`HotSwapStatusUnavailable`.

    That third case is the whole point, and it is why this class never returns a bare
    ``in_effect: False`` on a failed read. A corrupt window rendered as "no cool-down" is a
    false all-clear: the pane would enable the promote control, and an operator would be
    told a swap is safe by a producer that could not read the fact it was reporting.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        binary: str | Path | None = None,
        runner: HotSwapTriggerCliRunner | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else default_cooldown_binary()
        self._runner = runner or _cooldown_runner
        self._timeout = timeout

    @property
    def state_path(self) -> str:
        return self._state_path

    def live_state(self) -> dict[str, object] | None:
        """The cool-down facts, or raise when the window cannot be read.

        Never returns ``None``: a readable producer always has an answer, and ``None``
        would render the cells deferred — i.e. "SRS-RESV-006 has not produced this yet",
        which stopped being true the moment this source was mounted.

        Returns ONLY the ``cooldown`` key. ``current_live_strategy_id`` (SRS-RESV-005) and
        the demotion facts (SRS-RESV-004) are not this source's to state, and omitting them
        is what keeps their cells rendering their own owners' deferrals.
        """

        completed = self._invoke(["status", "--state", self._state_path])
        values = parse_trigger_cli_output(completed.stdout)
        state = values.get("cooldown-state")

        # The binary exits non-zero on an UNREADABLE window (and only then — an ACTIVE
        # window is healthy and exits zero). Read the state line first so the raise can name
        # what the tool actually said, then refuse on either signal.
        if state == "UNKNOWN" or completed.returncode != 0:
            reason = values.get("cooldown-unreadable-reason") or completed.stderr.strip()
            raise HotSwapStatusUnavailable(f"hot-swap cool-down window unreadable: {reason}")
        if state not in ("NEVER_SWAPPED", "ACTIVE", "EXPIRED"):
            raise HotSwapTriggerOutputUnreadable(
                f"resv006_hot_swap_cooldown_cli reported an unknown cool-down state {state!r}"
            )

        in_effect = strict_trigger_bool(values, "cooldown-in-effect")
        # Cross-check the two facts against each other rather than trusting either alone: a
        # binary that said `cooldown-state:ACTIVE` alongside `cooldown-in-effect:false` has
        # not told us which is true, and picking one would publish a swap-safety state
        # nobody asserted.
        if in_effect != (state == "ACTIVE"):
            raise HotSwapTriggerOutputUnreadable(
                f"resv006_hot_swap_cooldown_cli reported cooldown-state {state!r} with "
                f"cooldown-in-effect {in_effect!r}; the proof stream contradicts itself"
            )

        cooldown: dict[str, object] = {"in_effect": in_effect}
        if state == "NEVER_SWAPPED":
            # No timestamps, deliberately. There is no start instant to report, and an
            # invented one would put a resolved date on a window that never existed; the
            # pane renders those two cells deferred, which is the truth.
            return {"cooldown": cooldown}

        started = values.get("cooldown-started-at-seconds")
        expires = values.get("cooldown-expires-at-seconds")
        if not started or not expires:
            raise HotSwapTriggerOutputUnreadable(
                f"resv006_hot_swap_cooldown_cli reported state {state!r} without both "
                "window boundaries; refusing to render a half-known cool-down"
            )
        # Adversarial review r13. A window opened by phase one and never confirmed
        # suppresses exactly like a completed one — but only an operator can find out
        # whether the candidate actually went live, so the pane has to be able to TELL
        # them the two apart. `unknown` is carried as ``None`` and never as ``False``:
        # "this swap completed" is a claim, and a store that could not be read supports
        # no claims (CLAUDE.md rule 3).
        provisional = values.get("cooldown-completion-provisional")
        if provisional not in ("true", "false", "unknown", None):
            raise HotSwapTriggerOutputUnreadable(
                f"resv006_hot_swap_cooldown_cli reported an unknown provisional flag "
                f"{provisional!r}; refusing to guess whether a swap completed"
            )
        cooldown["completion_provisional"] = (
            {"true": True, "false": False}.get(provisional) if provisional else None
        )
        try:
            cooldown["started_at"] = _iso_utc(int(started))
            cooldown["expires_at"] = _iso_utc(int(expires))
        except (TypeError, ValueError) as bad:
            raise HotSwapTriggerOutputUnreadable(
                f"resv006_hot_swap_cooldown_cli emitted a non-numeric window boundary: {bad}"
            ) from bad
        return {"cooldown": cooldown}

    def trigger_config(self) -> dict[str, object] | None:
        """Not this source's fact — the trigger configuration is ``SRS-RESV-003``'s."""

        return None

    def promotion_candidate(self) -> dict[str, object] | None:
        """Not this source's fact — the ranking candidate is ``SRS-RESV-002``'s."""

        return None

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise HotSwapTriggerCliUnavailable(
                f"resv006_hot_swap_cooldown_cli timed out after {self._timeout}s"
            ) from expired
        except OSError as launch_error:
            raise HotSwapTriggerCliUnavailable(
                "resv006_hot_swap_cooldown_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv006_hot_swap_cooldown_cli`): "
                f"{launch_error}"
            ) from launch_error


class HotSwapDemotionLeg(Protocol):
    """The live-state half of the pane's source protocol (``SRS-RESV-004``)."""

    def live_state(self) -> "Mapping[str, object] | None": ...


#: Environment override for the SRS-RESV-005 promotion binary — the reader of the
#: durable live-designation record. Same knob the REST execution handler uses, because
#: it is the same binary; a deployment that relocates it should not have to say so twice.
PROMOTE_BINARY_ENV_KNOB = "ATP_HOT_SWAP_PROMOTE_BINARY"

_DEFAULT_PROMOTE_BINARY = (
    Path(__file__).resolve().parents[2] / "target" / "debug" / "resv005_hot_swap_promote_cli"
)


def default_promote_binary(env: "Mapping[str, str] | None" = None) -> Path:
    """The promotion binary's path: the env override when set, else the dev fallback."""

    import os

    source = os.environ if env is None else env
    override = source.get(PROMOTE_BINARY_ENV_KNOB)
    return Path(override) if override else _DEFAULT_PROMOTE_BINARY


class CliHotSwapPromotionSource:
    """``SRS-RESV-005``'s leg: which strategy currently holds the live designation.

    Reads the durable snapshot ``resv005_hot_swap_promote_cli`` maintains — the same
    record the promotion gate writes — so the pane's `current_live_strategy_id` cell
    reports the authority itself rather than a second copy that could disagree with it.

    Three outcomes, kept apart, because collapsing them is the failure this whole
    feature exists to prevent:

    * **readable, a strategy is designated** → ``{"current_live_strategy_id": id}``.
    * **readable, nothing designated** → ``None``. The pane's cell vocabulary (UI-5's)
      has no way to say "genuinely nothing is live", so the cell renders deferred — an
      UNDER-claim, and the safe direction: with nothing live there is nothing to demote,
      the promote control stays inert, and the gate would refuse the swap anyway
      (``NoLiveStrategyToDemote``). Pane and gate therefore agree.
    * **unreadable** → :class:`HotSwapStatusUnavailable`. A designation record that
      exists but cannot be read is NOT "no strategy is live"; the binary already refuses
      a foreign or truncated snapshot rather than reading it as empty, and this leg
      carries that refusal through instead of flattening it.

    Owns ONLY that cell. The demotion-pending (``SRS-RESV-004``) and cool-down
    (``SRS-RESV-006``) facts are other legs' — omitting their keys leaves those cells
    deferred rather than fabricating them.
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        binary: str | Path | None = None,
        runner: HotSwapTriggerCliRunner | None = None,
        timeout: float | None = None,
    ) -> None:
        self._state_path = str(state_path)
        self._binary = Path(binary) if binary is not None else default_promote_binary()
        self._runner = runner if runner is not None else _default_runner
        self._timeout = float(timeout) if timeout is not None else _DEFAULT_TIMEOUT_S

    @property
    def state_path(self) -> str:
        return self._state_path

    def live_state(self) -> dict[str, object] | None:
        completed = self._invoke(["status", "--state", self._state_path])
        if completed.returncode != 0:
            raise HotSwapStatusUnavailable(
                f"hot-swap live designation unreadable: {completed.stderr.strip()}"
            )
        designated = parse_trigger_cli_output(completed.stdout).get("designated")
        if designated is None:
            raise HotSwapTriggerOutputUnreadable(
                "resv005_hot_swap_promote_cli emitted no readable `designated` line; "
                "refusing to report a live strategy that cannot be evidenced"
            )
        if designated == "none":
            return None
        return {"current_live_strategy_id": designated}

    def _invoke(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = [str(self._binary), *args]
        try:
            return self._runner(argv, timeout=self._timeout)
        except subprocess.TimeoutExpired as expired:
            raise HotSwapTriggerCliUnavailable(
                f"resv005_hot_swap_promote_cli timed out after {self._timeout}s"
            ) from expired
        except OSError as launch_error:
            raise HotSwapTriggerCliUnavailable(
                "resv005_hot_swap_promote_cli could not be launched (is it built? "
                "`cargo build -p atp-orchestrator --bin resv005_hot_swap_promote_cli`): "
                f"{launch_error}"
            ) from launch_error


class HotSwapPromotionLeg(Protocol):
    """The ``SRS-RESV-005`` leg of the pane's source."""

    def live_state(self) -> "Mapping[str, object] | None": ...


class HotSwapCooldownLeg(Protocol):
    """The ``SRS-RESV-006`` cool-down leg of the pane's ``live_state``."""

    def live_state(self) -> "Mapping[str, object] | None": ...


class CompositeHotSwapStatusSource:
    """One :class:`~atp_dashboard.hotswap.HotSwapStatusSource` over several per-leg producers.

    The pane's protocol has three legs owned by three different features, and they land one at a
    time. This composes whichever exist without either producer having to know about the other —
    ``atp_orchestration`` and ``atp_dashboard`` are peers, and neither may import the other.

    Each leg keeps failing INDEPENDENTLY, which is the property the pane relies on: an unreadable
    trigger configuration must not blank an otherwise readable demotion state, so this class does
    not catch :class:`HotSwapStatusUnavailable` — it lets each leg's exception reach the
    provider's per-leg handler, which records it as one ``ok:false`` reason and defers only that
    leg. Swallowing here would turn one degraded producer into a silently all-deferred pane.
    """

    def __init__(
        self,
        *,
        triggers: "HotSwapTriggerLeg | None" = None,
        demotion: "HotSwapDemotionLeg | None" = None,
        promotion: "HotSwapPromotionLeg | None" = None,
        cooldown: "HotSwapCooldownLeg | None" = None,
    ) -> None:
        self._triggers = triggers
        self._demotion = demotion
        self._promotion = promotion
        self._cooldown = cooldown

    def trigger_config(self) -> "Mapping[str, object] | None":
        if self._triggers is None:
            return None
        return self._triggers.trigger_config()

    def live_state(self) -> "Mapping[str, object] | None":
        """The live-swap state, MERGED from the three legs that own its parts.

        Three features answer different parts of one protocol method: SRS-RESV-004 owns
        the demotion-pending facts (``demotion_pending`` / ``demotion_detail`` /
        ``sequence``), SRS-RESV-005 owns which strategy holds the live designation
        (``current_live_strategy_id``), SRS-RESV-006 owns the cool-down window
        (``cooldown``). Their key sets are disjoint by construction, so the merge is a
        union rather than a precedence rule — nothing here has to decide which leg wins.

        A key COLLISION is refused rather than silently resolved: it would mean one owner
        had started answering for another, and quietly letting the last writer win is how
        a pane ends up reporting a fact nobody produced. `merged.update()` would do
        exactly that, which is why this is a loop and not a one-liner.

        No exception is caught, matching this class's per-leg discipline: the provider
        records it as one ``ok:false`` reason. Note the honest limit, also recorded in
        ``hot_swap_cooldown_contract.deferred`` — all three sub-legs feed ONE protocol
        method, so an unreadable lockout defers the live strategy and the cool-down cells
        too. That is the fail-closed direction (the control goes inert) and the raise
        names which sub-leg failed, but it is coarser than per-fact independence.
        Splitting ``live_state`` into per-owner legs is UI-5's pane protocol to change,
        not this class's.
        """

        merged: dict[str, object] = {}
        for owner, leg in (
            ("SRS-RESV-004", self._demotion),
            ("SRS-RESV-005", self._promotion),
            ("SRS-RESV-006", self._cooldown),
        ):
            if leg is None:
                continue
            produced = leg.live_state()
            if produced is None:
                continue
            for key, value in produced.items():
                if key in merged:
                    raise HotSwapTriggerOutputUnreadable(
                        f"two hot-swap live-state producers both answered {key!r} "
                        f"(latest: {owner}); one of them is reporting a fact it does not own"
                    )
                merged[key] = value
        # `None` (no leg mounted, or every leg silent) keeps every live cell deferred to its
        # own owner — which is the truth when nothing is composed.
        return merged or None

    def promotion_candidate(self) -> "Mapping[str, object] | None":
        # Owner SRS-RESV-002. Asked of the trigger source only because that is where a ranking
        # producer would land; today it answers None.
        if self._triggers is None:
            return None
        return self._triggers.promotion_candidate()
