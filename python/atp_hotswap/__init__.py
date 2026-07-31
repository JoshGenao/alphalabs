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
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

__all__ = [
    "CliHotSwapTriggerSource",
    "HotSwapStatusUnavailable",
    "HotSwapTriggerCliRunner",
    "HotSwapTriggerCliUnavailable",
    "HotSwapTriggerOutputUnreadable",
    "BINARY_ENV_KNOB",
    "default_binary",
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

    Only :meth:`trigger_config` resolves. :meth:`live_state` and :meth:`promotion_candidate`
    return ``None`` — no ``SRS-RESV-002``/``004``/``005``/``006`` producer persists a
    queryable fact yet, and inventing one would be exactly the fabrication the pane's
    deferred cells exist to prevent. The three legs fail INDEPENDENTLY, as the protocol
    requires: an unreadable trigger configuration must not blank an otherwise readable live
    state, so each raises on its own rather than through a shared cached read.
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
