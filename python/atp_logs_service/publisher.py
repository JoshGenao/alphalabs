"""The ``LOGS`` WebSocket publisher (SRS-LOG-001 on the SRS-API-001 runtime).

The ``LOGS`` channel is the one **event-driven** channel in the ``atp_ws``
contract: it declares ``refresh_seconds=0``, which
:func:`atp_dashboard.publisher.cadence_for` deliberately rejects ("an
event-driven 0 is not a periodic dashboard channel"). So this publisher owns its
own ticker rather than riding :class:`~atp_dashboard.publisher.DashboardPublisher`
— publishing a *status snapshot* on a cadence would drift the declared per-event
payload into something the AsyncAPI document does not describe.

What it publishes
-----------------
New records only. Each poll re-reads both stores and emits the records appended
since the previous poll, in write order, as one event per record carrying
exactly the channel's declared ``payload_fields``. A subscriber therefore sees
the audit trail, not a rolling re-broadcast of the same tail.

Honesty rules
-------------
* **Never fabricates.** Every published event is a record read back out of a
  store. There is no synthetic "heartbeat" event and no placeholder.
* **A read failure is surfaced, not swallowed.** A corrupt store, a vanished
  file, or any other read error is recorded on :attr:`last_error` and counted;
  the ticker keeps running (a monitoring surface must not die), but it never
  reports itself healthy while a store is unreadable. Going quiet is the
  dangerous failure here: a silent LOGS channel is indistinguishable from a
  system with nothing to report.
* **A fan-out failure cannot kill the ticker either.** Once the channel is
  claimed, a thread that died on a wedged hub would leave the runtime reporting
  the workflow *served* while nothing is published. A publish that raises is
  counted, surfaced, and ends that drain — never the thread.
* **A missing store is a failure, not an empty stream.** A configured trail that
  is not there reads as an empty scan, so the channel would publish silence and
  call itself healthy. It is recorded as a read failure, and the channel is not
  claimed until a poll reads BOTH trails cleanly — so runtime readiness cannot
  report LOGS served over an audit trail nobody can read.
* **Progress is only recorded for what was actually delivered.** Neither a
  failed read nor a failed publish advances the per-store cursor past an
  undelivered record, so the window is retried once the fault clears rather
  than skipped.

The cursor: why a count alone is not enough
-------------------------------------------
The obvious cursor — "I have published N records, so publish ``records[N:]``" —
is WRONG under rotation, and wrong in the silent direction. Rotation drops the
oldest segments while new records keep appending, so the retained list can stay
the same length (or grow) while its contents shift left underneath the index.
``records[N:]`` then names the wrong window and skips real audit events with
nothing to signal it: evict 30 while 30 arrive and the publisher reports a clean,
healthy, completely silent poll.

So the cursor is an ANCHOR: the PHYSICAL POSITION (segment device/inode + byte
offset) of the last record actually published, TOGETHER WITH that record. Each
poll streams the trail and buffers whatever follows the matching entry.

Both halves are load-bearing. A value alone is not an identity — two audit
records may legitimately be byte-identical, and a value cursor would read the
second as "already sent" and drop it. A position alone is not an identity
forever either: an EVICTED segment's inode can be reused by the filesystem, and
a later record can land at the same offset, so a position-only compare could
read a brand-new line as the anchor. Matching on both means a false match needs
the same physical slot AND identical content simultaneously. That residual is
recorded in ``log_persistence_contract.deferred`` — closing it for good needs a
persisted monotonic sequence in the record envelope, which is a format change
governed by the SRS-DATA-015 schema registry.

* Anchor found → publish everything after it, wherever rotation has moved it to.
  Nothing lost, nothing re-broadcast, no gap.
* Anchor GONE → it was evicted. Rotation drops a PREFIX, so everything still
  retained was written after the anchor and has never been published: publish all
  of it. What cannot be recovered is the window between the anchor and the
  retained head — records that rotated out before this channel reached them — so
  that is counted as an eviction gap and surfaced on ``health()``. Re-syncing to
  the end instead would skip exactly the records the operator can still see in
  the store.

The scan STREAMS (:func:`atp_logging.persistence.iter_records`) and buffers at
most ``max_events_per_poll`` records, so memory is bounded by the page size and
not by the trail — which matters because rotation is opt-in and the operator log
store defaults to unbounded append.

SRS trace
---------
``SRS-LOG-001`` (log event publication), ``SYS-38`` / ``SYS-61``,
``SRS-API-001`` (``publish`` / ``register_publisher`` seam).
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from itertools import islice
from pathlib import Path

from atp_logging.persistence import (
    LogStoreRotationError,
    RecordPosition,
    active_identity,
    iter_records_with_positions,
    read_tail,
    record_at,
)
from atp_logging.records import LogClass, LogRecord

from .handlers import render_event

__all__ = ["LOGS_CHANNEL", "LogEventPublisher"]

#: The declared channel this publisher claims.
LOGS_CHANNEL = "LOGS"

#: Default seconds between store polls. The channel is event-driven, so this is
#: a *detection* interval, not a refresh cadence: it bounds how long a written
#: record waits before it is fanned out.
DEFAULT_POLL_INTERVAL_S = 1.0

#: Longest a poll may sleep between stop-flag checks, so ``stop()`` stays
#: responsive even with a long poll interval (mirrors DashboardPublisher).
_POLL_CEILING_S = 0.5

#: How many times one drain re-reads a store that rotated mid-scan before giving
#: up for this poll. Rotation is a rare, bounded event; a store that rotates
#: through every attempt is reported rather than retried forever.
_ROTATION_RETRIES = 3


class LogEventPublisher:
    """Publishes newly-persisted log records on the ``LOGS`` channel.

    Args:
        publish: The fan-out callable — ``runtime.publish``. Takes the channel
            name and one event payload; its return value (delivery count) is
            ignored: zero subscribers is not an error.
        release_channel: Gives the ``LOGS`` claim back
            (``runtime.unregister_publisher``) when a poll cannot read a store or
            cannot fan out. A latched claim would keep runtime readiness
            reporting the workflow served by a stream that has stopped
            delivering — readiness that cannot be revoked eventually lies.
        claim_channel: Claims the ``LOGS`` channel's publisher slot
            (``runtime.register_publisher``). Called on the first poll that reads
            BOTH trails cleanly — never at wiring time, and not merely because
            ``start()`` was called: the runtime counts a claimed channel toward
            its workflow readiness, so claiming while nothing can be read would
            report the workflow served over a trail that is not there.
        system_store_path: Path of the SYSTEM store's active segment.
        strategy_store_path: Path of the STRATEGY store's active segment.
        poll_interval_s: Seconds between polls.
        max_files: Rotation depth of the stores being read.
        max_events_per_poll: Cap on events emitted from ONE store in one poll,
            so a large backlog cannot monopolise the ticker thread. The
            remainder is emitted on the following polls (the cursor advances by
            exactly what was published).
    """

    def __init__(
        self,
        *,
        publish: Callable[[str, object], int],
        claim_channel: Callable[[], None],
        release_channel: Callable[[], None],
        system_store_path: str | os.PathLike[str],
        strategy_store_path: str | os.PathLike[str],
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        max_files: int = 5,
        max_events_per_poll: int = 200,
    ) -> None:
        if poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be > 0; got {poll_interval_s!r}")
        if isinstance(max_events_per_poll, bool) or max_events_per_poll <= 0:
            raise ValueError(f"max_events_per_poll must be > 0; got {max_events_per_poll!r}")
        self._publish = publish
        self._claim_channel = claim_channel
        self._release_channel = release_channel
        self._paths: dict[LogClass, Path] = {
            LogClass.SYSTEM: Path(system_store_path),
            LogClass.STRATEGY: Path(strategy_store_path),
        }
        self._poll_interval_s = poll_interval_s
        self._max_files = max_files
        self._max_events_per_poll = max_events_per_poll
        # The anchor cursor: the PHYSICAL position of the last record actually
        # published, per class. ``None`` means "nothing published yet", which is
        # NOT the same as "anchor evicted" (see _collect_after_anchor).
        self._anchor: dict[LogClass, tuple[RecordPosition, LogRecord] | None] = {
            LogClass.SYSTEM: None,
            LogClass.STRATEGY: None,
        }
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        # Serialises the readiness DECISION with the callback that acts on it.
        # Deciding under ``_lock`` and then calling out unlocked left a window
        # where stop() could release the claim and a pending poll would re-take
        # it, leaving the runtime reporting LOGS served by a stopped ticker.
        # Held across both the decision and the callback; the callbacks are
        # trivial registry set operations that never re-enter this object.
        self._claim_lock = threading.Lock()
        # Serialises whole polls against each other. The read, the fan-out, and
        # the cursor advance are three separate steps, so two polls overlapping
        # both read the same records BEFORE either advances the anchor and each
        # publishes them — the same audit line delivered twice, which on a
        # channel keyed by record identity is a subscriber's problem, not a
        # cosmetic one. The ticker is the only caller in production, but
        # poll_once is public and driven directly by tests and by any future
        # flush-now path, so the invariant belongs here rather than in a comment
        # asking callers to stay single-threaded.
        self._poll_lock = threading.Lock()
        self._published_total = 0
        self._read_failures = 0
        self._publish_failures = 0
        self._eviction_gaps = 0
        self._rotation_races = 0
        self._stop_timeouts = 0
        # The LOGS channel is claimed on the first CLEAN poll, never at start:
        # see poll_once. One claim only.
        self._claimed = False
        self._last_error: str | None = None
        # Classes whose cursor start() anchored at the tail — i.e. that already
        # held history this channel deliberately did not replay. See _seed_cursor.
        self._seeded_past_history: set[LogClass] = set()
        # Classes whose cursor has been positioned for live delivery. Until a
        # class is in here its cursor has NOT been established, and draining it
        # would publish the whole retained trail; the drain seeds first and skips
        # the poll if seeding fails, so a transient read error cannot degrade the
        # channel into a history replay. Empty until start() — a publisher driven
        # by poll_once() alone (the unit rigs) keeps the from-the-beginning
        # behaviour those tests were written against.
        self._seeded: set[LogClass] = set()
        # Classes whose trail already existed when start() was called. Only those
        # have history to skip. A trail that appears LATER (the writer booting
        # after the dashboard) holds nothing this channel could have published
        # before it existed, so every record in it is new and skipping to its tail
        # would be a silent drop of the operator's first events.
        self._existed_at_start: set[LogClass] = set()
        self._live_mode = False

    # ----- observability ----- #

    @property
    def published_total(self) -> int:
        """Events fanned out since construction (real records only)."""

        with self._lock:
            return self._published_total

    @property
    def read_failures(self) -> int:
        """Polls that could not read a store. Non-zero means events may be missing."""

        with self._lock:
            return self._read_failures

    @property
    def publish_failures(self) -> int:
        """Fan-out attempts that raised. Non-zero means events were delayed."""

        with self._lock:
            return self._publish_failures

    @property
    def rotation_races(self) -> int:
        """Polls abandoned because the store rotated mid-scan (nothing skipped)."""

        with self._lock:
            return self._rotation_races

    @property
    def stop_timeouts(self) -> int:
        """Stops where the ticker outlived its join budget (start stays refused)."""

        with self._lock:
            return self._stop_timeouts

    @property
    def eviction_gaps(self) -> int:
        """Polls where the trail shrank, so publication may have a hole."""

        with self._lock:
            return self._eviction_gaps

    @property
    def last_error(self) -> str | None:
        """The most recent read failure or gap, ``None`` if every poll was clean."""

        with self._lock:
            return self._last_error

    def health(self) -> dict[str, object]:
        """Publisher health — ``ok`` is false after ANY read failure or gap.

        ``ok`` means one specific thing: no event may have been MISSED. It is
        deliberately sticky — a channel that failed to read a store, failed to
        fan an event out, or lost its place to an eviction has already
        under-published, and flipping back to ``ok`` on the next clean poll would
        erase the evidence that a subscriber is missing events.

        ``rotation_races`` is reported but does NOT clear ``ok``: a scan
        abandoned mid-rotation published nothing and advanced nothing, so the
        records are still on disk and the next poll carries them. That is a
        delay, not a loss, and folding it into a data-loss flag would blunt the
        flag's meaning. A store rotating faster than it can be read still shows
        up — in the counter and in ``last_error``.

        ``running`` is reported separately and is NOT part of ``ok``: a caller
        checking whether the stream is alive must ask that question directly
        rather than inferring liveness from an absence of errors.
        """

        with self._lock:
            return {
                "channel": LOGS_CHANNEL,
                "ok": (
                    self._read_failures == 0
                    and self._publish_failures == 0
                    and self._eviction_gaps == 0
                ),
                "published_total": self._published_total,
                "read_failures": self._read_failures,
                "publish_failures": self._publish_failures,
                "eviction_gaps": self._eviction_gaps,
                "rotation_races": self._rotation_races,
                "stop_timeouts": self._stop_timeouts,
                "last_error": self._last_error,
                "running": self._thread is not None and self._thread.is_alive(),
                # Which trails already held records when this publisher started.
                # Those records are NOT replayed onto the channel (see
                # _seed_cursor), and a subscriber that assumed otherwise would
                # read the stream as a complete history. Stated rather than
                # counted: counting the skipped records means scanning the whole
                # trail, which is exactly the unbounded read this class refuses.
                "history_not_replayed": sorted(
                    log_class.value for log_class in self._seeded_past_history
                ),
            }

    # ----- one poll (synchronous; the unit tests drive this directly) ----- #

    def poll_once(self) -> int:
        """Publish every record appended since the last successful poll.

        Returns the number of events published. Never raises, and never skips:
        a store that cannot be read, and a fan-out call that raises, are both
        recorded on :attr:`last_error` and leave the cursor at the last record
        actually delivered — so the undelivered window is retried on the next
        poll rather than lost.

        Serialised against other polls (see ``_poll_lock``): two overlapping
        polls would each read the trail before either advanced the cursor and
        publish the same records twice.
        """

        with self._poll_lock:
            return self._poll_locked()

    def _poll_locked(self) -> int:
        with self._lock:
            faults_before = self._fault_counts()

        published = 0
        for log_class in (LogClass.SYSTEM, LogClass.STRATEGY):
            published += self._drain(log_class)

        # Claim the channel on the first poll that was clean in EVERY dimension —
        # not on start(), and not merely on a successful READ. The runtime counts
        # a claimed channel toward the LOGS workflow's readiness, so claiming
        # while a store is missing, a fan-out is already raising, or a rotation
        # cost us the scan would report the workflow served by a stream that is
        # not delivering. Claiming here (rather than never) also means a trail
        # whose writer boots after the dashboard is picked up automatically.
        with self._claim_lock, self._lock:
            faults_now = self._fault_counts()
            # Readiness tracks DELIVERY, in both directions and by the same
            # measure. A read that failed, a fan-out that raised, or a scan
            # abandoned to rotation all mean the same thing: this poll delivered
            # nothing from that store, so the channel is not delivering right
            # now. All three are transient — the next clean poll re-claims — so
            # counting them cannot latch readiness off.
            #
            # An eviction gap is the one that does NOT count. It is a fact about
            # lost HISTORY, not about the current stream, and it persists: a
            # restored-but-emptied trail produces one on the very poll that
            # resumes delivery, so gating on it would pin readiness OFF forever
            # — the same lie as latching it on. It stays visible on health().
            delivering = (
                faults_now[0] == faults_before[0]  # read failures
                and faults_now[1] == faults_before[1]  # publish failures
                and faults_now[2] == faults_before[2]  # rotation races
            )
            # A poll already in flight when stop() was called must not take the
            # claim back: stop() releases it deliberately, and re-claiming here
            # would leave readiness green for a ticker that is shutting down.
            claim_now = delivering and not self._claimed and not self._stop.is_set()
            release_now = not delivering and self._claimed
            if claim_now:
                self._claimed = True
            if release_now:
                self._claimed = False
            # Still inside _claim_lock: a stop() cannot slip between deciding
            # and acting.
            if claim_now:
                self._claim_channel()
            if release_now:
                self._release_channel()
        return published

    def _fault_counts(self) -> tuple[int, int, int, int]:
        """Every failure counter, for "was this poll clean?" (call under the lock)."""

        return (
            self._read_failures,
            self._publish_failures,
            self._rotation_races,
            self._eviction_gaps,
        )

    def _drain(self, log_class: LogClass) -> int:
        path = self._paths[log_class]
        # Fast path only — the scan below passes require_active=True, so a trail
        # that disappears mid-read is a read failure rather than an empty scan.
        if not path.exists():
            # A CONFIGURED trail that is not there reads as an empty scan: the
            # iterator yields nothing, the identity check trivially agrees, and
            # the channel would sit there publishing silence while reporting
            # itself healthy. Same rule the REST handler and the dashboard pane
            # apply — a missing audit trail is a failure, not an empty one.
            with self._lock:
                self._read_failures += 1
                self._last_error = (
                    f"the configured {log_class.value} log store does not exist ({path}); "
                    "this channel has no audit trail to publish"
                )
            return 0

        # Establish the cursor before the first read of this class. A failure
        # here skips the poll rather than falling through with an unset cursor,
        # which the scan below would read as "publish everything retained".
        if self._live_mode and log_class not in self._seeded and not self._seed_cursor(log_class):
            return 0

        with self._lock:
            anchor = self._anchor[log_class]

        # A scan enumerates segment PATHS and then opens them, so a rotation
        # landing in that window makes the scan incoherent: the old active
        # segment can be renamed past a path already visited, and its records
        # after the anchor never appear. Acting on such a scan would drop them
        # (or re-publish an older span). So the active segment's identity is
        # taken before and after; if it moved, the scan is DISCARDED — nothing
        # published, cursor untouched — and retried. Discarding is always safe
        # here: the trail is append-only, so a later scan sees a superset.
        for _attempt in range(_ROTATION_RETRIES):
            before = active_identity(path)
            try:
                # Resume from the anchor's physical slot when it still holds the
                # record we left there. Without this the poll re-reads, re-parses
                # and re-validates the ENTIRE retained trail every tick just to
                # find its place again — and rotation is opt-in, so that trail
                # grows without bound. The cost would climb until the ticker's
                # real cadence quietly drifted past its interval and stop() began
                # timing out, with health() still reporting ok. Verified by
                # CONTENT as well as position (record_at), because an evicted
                # segment's inode can be reused and a later line can land at the
                # same offset; a bare offset seek would then resume past records
                # nobody published.
                resume = None
                if anchor is not None:
                    at_slot = record_at(
                        path,
                        anchor[0],
                        max_files=self._max_files,
                        expect_class=log_class,
                    )
                    if at_slot is not None and (anchor[0], at_slot) == anchor:
                        resume = anchor[0]
                entries = iter_records_with_positions(
                    path,
                    max_files=self._max_files,
                    require_active=True,
                    # Publishing a record from the wrong physical trail would
                    # carry a broken separation onward to every subscriber.
                    expect_class=log_class,
                    resume_after=resume,
                )
                if resume is not None:
                    # The anchor was found and skipped past by construction.
                    fresh, found_anchor = list(islice(entries, self._max_events_per_poll)), True
                else:
                    fresh, found_anchor = _collect_after_anchor(
                        entries, anchor=anchor, cap=self._max_events_per_poll
                    )
            except LogStoreRotationError:
                # The resume point rotated out between verifying it and reading.
                # Retry; if the trail keeps moving, the loop below reports it as
                # a rotation race rather than as an unreadable store.
                continue
            except Exception as error:  # noqa: BLE001 - a monitoring surface must not die
                with self._lock:
                    self._read_failures += 1
                    self._last_error = f"{log_class.value} store unreadable: {error}"
                return 0
            if active_identity(path) == before:
                break
        else:
            # Rotation kept landing mid-scan. Publishing nothing is the correct
            # outcome — the records are still on disk and the next poll will
            # carry them — but a store rotating faster than it can be read is a
            # real operator condition, so it is counted and surfaced.
            with self._lock:
                self._rotation_races += 1
                self._last_error = (
                    f"the {log_class.value} store rotated during every read attempt; "
                    "no events were published from this poll (they are retried next "
                    "poll — nothing was skipped)"
                )
            return 0

        if anchor is not None and not found_anchor:
            # The anchor was evicted. Rotation drops a PREFIX of the trail, so
            # everything still retained was written after the anchor and has
            # therefore never been published — publish all of it. (Re-syncing to
            # the end instead would skip exactly the records the operator can
            # still see in the store, which is the silent drop this cursor
            # exists to prevent.)
            #
            # What IS lost is the window between the anchor and the retained
            # head: records that rotated out before this channel reached them.
            # That is unknowable, so it is counted and surfaced rather than
            # papered over.
            with self._lock:
                self._eviction_gaps += 1
                self._last_error = (
                    f"the {log_class.value} record this channel last published is no "
                    "longer in the trail (rotation/eviction); the retained history is "
                    "being published, but records written between them may have "
                    "rotated out unpublished"
                )
        delivered = 0
        for position, record in fresh:
            try:
                self._publish(LOGS_CHANNEL, _event_payload(position, record))
            except Exception as error:  # noqa: BLE001 - a fan-out fault must not kill the ticker
                # A failing publish path (a wedged hub, a serialisation fault)
                # must not take the thread down: start() has already claimed the
                # LOGS channel, so a dead ticker would leave runtime readiness
                # reporting the workflow served while nothing is published. Stop
                # this drain WITHOUT advancing the cursor past the record that
                # failed, so it is retried on the next poll rather than skipped.
                with self._lock:
                    self._publish_failures += 1
                    self._last_error = (
                        f"publishing a {log_class.value} record failed: {error}; "
                        "the record was NOT skipped — it is retried on the next poll"
                    )
                break
            delivered += 1
        with self._lock:
            if delivered:
                # Anchor on the last record we actually FANNED OUT, never on the
                # last one we read: an undelivered record must stay ahead of the
                # cursor so the next poll retries it.
                self._anchor[log_class] = fresh[delivered - 1]
            self._published_total += delivered
        return delivered

    # ----- lifecycle ----- #

    def _seed_cursor(self, log_class: LogClass) -> bool:
        """Anchor at the trail's current tail so the channel starts LIVE.

        Without this the cursor starts at ``None``, which means "publish
        everything retained" — and the trail is append-only and unbounded by
        default. A deployment with a large existing audit history would then
        spend poll after poll fanning out old records at ``max_events_per_poll``
        apiece, and a kill-switch activation written one second after startup
        would queue behind all of it. The channel would look perfectly healthy
        the whole time: no read failure, no gap, events flowing. That is the
        worst shape an alerting surface can take — delivering, in order, far too
        late, while reporting itself fine.

        So the LOGS channel is what it is declared to be: event-driven. It
        carries what happens from now on, and the whole history stays available
        on ``GET /api/v1/logs``, which is built to page it. The trade is
        recorded on :meth:`health` under ``history_not_replayed`` rather than
        left for a subscriber to infer.

        Returns whether the class is now seeded. A failure returns ``False`` and
        is RETRIED — it must never be silently treated as "seeded at nothing".
        A transient error here (a rotation window where the active segment has
        been renamed but not yet recreated, EMFILE, EINTR) would otherwise leave
        the cursor at ``None`` while the very next poll read the store fine and
        replayed the entire archive: the exact failure this method exists to
        prevent, reached through its error path, with no counter raised and
        ``ok`` still true. A store that does not exist yet is also "not seeded
        yet" for the same reason — it is seeded on the first poll that can read
        it, so the channel goes live from the moment it has a trail to be live
        against.
        """

        if log_class not in self._existed_at_start:
            # Nothing preceded this channel on this trail: no history to skip.
            with self._lock:
                self._seeded.add(log_class)
            return True

        path = self._paths[log_class]
        if not path.exists():
            return False
        try:
            newest = read_tail(
                path,
                limit=1,
                max_files=self._max_files,
                require_active=True,
                expect_class=log_class,
            )
        except Exception as error:  # noqa: BLE001 - a monitoring surface must not die
            with self._lock:
                self._read_failures += 1
                self._last_error = (
                    f"could not read the {log_class.value} trail's tail to start the channel "
                    f"live ({error}); NOT publishing its history — retrying next poll"
                )
            return False
        with self._lock:
            if newest:
                # read_tail returns newest-first, so [0] is the last record written.
                self._anchor[log_class] = newest[0]
                self._seeded_past_history.add(log_class)
            # An EMPTY trail is seeded too: there is no history to skip, so the
            # cursor stays None and everything appended from here is new.
            self._seeded.add(log_class)
        return True

    def start(self) -> None:
        """Seed the cursor at each trail's tail, run one poll, then the ticker.

        The first poll runs SYNCHRONOUSLY so the channel claim is deterministic:
        a composition whose stores are readable is served the moment ``start()``
        returns, and one whose configured trail is missing has NOT claimed the
        channel — so runtime readiness cannot report the LOGS workflow served
        over an audit trail nobody can read.

        Seeding happens FIRST, so that poll publishes what is new rather than
        draining history (see :meth:`_seed_cursor`).
        """

        if self._thread is not None:
            if self._thread.is_alive():
                raise RuntimeError(
                    "publisher already started (or its previous thread has not exited); "
                    "call stop() and wait for it before starting again"
                )
            # A previous stop() timed out but the thread has since finished —
            # safe to take the slot back.
            self._thread = None
        # Clear the stop flag BEFORE the first poll: that poll is what claims the
        # channel, and the claim path deliberately refuses while a stop is
        # pending (see poll_once), so a restart would otherwise never re-claim.
        self._stop.clear()
        # Live mode from here on: the drain seeds each class's cursor at the
        # trail's tail before its first read (see _seed_cursor). Seeding lives in
        # the drain rather than here so there is exactly ONE seeding path — a
        # seed attempted only at start() would be skipped for good if it failed
        # transiently or if the store had not been created yet.
        self._existed_at_start = {
            log_class for log_class, path in self._paths.items() if path.exists()
        }
        self._live_mode = True
        self._poll_guarded()
        thread = threading.Thread(target=self._run, name="atp-logs-publisher", daemon=True)
        self._thread = thread
        thread.start()

    def _poll_guarded(self) -> None:
        """One poll that cannot raise.

        :meth:`poll_once` already contains every EXPECTED failure, so an escape
        means something unforeseen. Neither the ticker nor ``start()`` may die of
        it: a dead ticker under a claimed channel is a runtime reporting the
        workflow served while publishing nothing.
        """

        try:
            self.poll_once()
        except Exception as error:  # noqa: BLE001 - last line of defence
            with self._lock:
                self._publish_failures += 1
                self._last_error = f"poll loop error (ticker kept alive): {error}"
            # An unforeseen failure is still a NON-DELIVERY, so it must give the
            # claim back like every other one. Keeping it would leave runtime
            # readiness reporting LOGS served by a ticker that is erroring out
            # every poll — the exact silent-observability failure the rest of
            # this class refuses.
            with self._claim_lock, self._lock:
                release_now = self._claimed
                self._claimed = False
                if release_now:
                    self._release_channel()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._poll_guarded()
            remaining = self._poll_interval_s
            while remaining > 0 and not self._stop.is_set():
                slice_s = min(_POLL_CEILING_S, remaining)
                if self._stop.wait(slice_s):
                    return
                remaining -= slice_s

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the loop and join the thread (bounded — no leaked daemon).

        If the join TIMES OUT the thread reference is KEPT. Dropping it would let
        a subsequent :meth:`start` clear the stop flag and launch a second
        ticker beside the first: two loops sharing one cursor, duplicating and
        skipping audit events on the channel. The timeout is surfaced instead,
        and ``start`` refuses until the old thread has actually exited.
        """

        self._stop.set()
        # Give the claim back BEFORE joining, and regardless of how the join
        # goes. A stopped publisher is not publishing, so leaving LOGS claimed
        # would keep runtime readiness reporting the workflow fully served by a
        # ticker that is shutting down — the same silent-observability failure
        # this publisher refuses for a missing store. Releasing early also means
        # a join that TIMES OUT errs toward understating readiness, which is the
        # safe direction; ``start`` re-claims on its first clean poll.
        with self._claim_lock, self._lock:
            release_now = self._claimed
            self._claimed = False
            if release_now:
                self._release_channel()

        thread = self._thread
        if thread is None:
            return
        thread.join(timeout=timeout)
        if thread.is_alive():
            with self._lock:
                self._stop_timeouts += 1
                self._last_error = (
                    f"the publisher thread did not exit within {timeout}s and is still "
                    "running; the publisher will refuse to start a second ticker until it does"
                )
            return
        self._thread = None


def _collect_after_anchor(
    entries: Iterator[tuple[RecordPosition, LogRecord]],
    *,
    anchor: tuple[RecordPosition, LogRecord] | None,
    cap: int,
) -> tuple[list[tuple[RecordPosition, LogRecord]], bool]:
    """Stream the trail and buffer the records that follow ``anchor``.

    The anchor is a PHYSICAL position (segment device/inode + byte offset), not
    a record value. Two audit records may legitimately be byte-identical — a
    retried operation writing the same message with the same correlation id in
    the same nanosecond — and a value-equality cursor would mistake the second
    for the one already published and drop it, silently. A position is unique
    even when the content is not.

    Memory is O(``cap``) however large the trail is: the audit log is
    append-only and unbounded by default, so the publisher must never
    materialise it just to find its place.

    Args:
        entries: ``(position, record)`` pairs in write order.
        anchor: Position of the last record published, or ``None`` if none yet
            (in which case everything retained is new to this channel).
        cap: Maximum records to buffer in one poll; the rest follow next poll.

    Returns:
        ``(entries_to_publish, anchor_was_seen)``. A ``False`` flag with a
        non-``None`` anchor means the anchor's line is gone — the caller reports
        the gap, and the buffer holds the retained history from the start, all
        of which post-dates the evicted anchor.
    """

    buffered: list[tuple[RecordPosition, LogRecord]] = []
    seen_anchor = False
    for position, record in entries:
        # BOTH the physical slot and the record must match. A position alone is
        # not quite unique forever: when rotation EVICTS a segment the
        # filesystem may reuse its inode, and a later record can land at the
        # same offset — a bare position compare would then read a brand-new line
        # as "the one I already sent" and skip the retained history before it,
        # silently. Requiring the record too means a false match needs the same
        # physical slot AND identical content at once.
        if anchor is not None and (position, record) == anchor:
            # Everything up to and including this line has already been
            # published; anything buffered before it was history.
            seen_anchor = True
            buffered.clear()
            continue
        if len(buffered) < cap:
            buffered.append((position, record))
    return buffered, seen_anchor


def _event_payload(position: RecordPosition, record: LogRecord) -> dict[str, object]:
    """One published event: exactly the declared per-record payload."""

    return render_event(record, position)
