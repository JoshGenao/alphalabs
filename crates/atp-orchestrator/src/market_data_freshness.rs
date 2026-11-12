//! SRS-MD-004 — the production market-data freshness probe.
//!
//! `atp-execution` declares the [`MarketDataFreshnessProbe`] port and
//! `atp-market-data` owns the freshness state ([`LineFreshnessBoard`]); neither
//! may see the other (SRS-ARCH-002). This composition layer binds them:
//! [`BoardFreshnessProbe`] answers the execution gate from the board, so a
//! live or paper order is judged against the same verdict the feed keeps.
//!
//! [`MonotonicEpochClock`] is the clock a production board is built with. The
//! board stamps observations and evaluates verdicts on one clock, and that
//! clock must never step backwards (see [`FreshnessClock`]).
//!
//! What is NOT here: feeding the board from the live IB tick stream. The
//! SRS-MD-003 feed loop (`atp_market_data::live_feed::LiveFeedLoop`) keeps its
//! own monitor today; composing it into a shared board inside the live
//! execution host is the host's IB-tier wiring, owned by SRS-EXE-001.

use std::sync::Arc;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use atp_execution::MarketDataFreshnessProbe;
use atp_market_data::{FreshnessClock, GapTracking, LineFreshnessBoard};
use atp_types::{AssetClass, MarketDataFreshness};

/// A monotonic clock reported in epoch nanoseconds.
///
/// The epoch reading is taken once, at construction; every later reading adds
/// the monotonic time elapsed since then. Readings therefore never go
/// backwards when the wall clock is corrected (an NTP step or a manual
/// change), which would otherwise clamp a silent line's age to zero and read it
/// as fresh. The epoch anchor only keeps the numbers recognizable as times;
/// no decision depends on it.
///
/// Residual: on some platforms the monotonic clock does not advance while the
/// host is suspended, so a line silent across a sleep ages by the awake time
/// only. A trading host that sleeps has a larger problem than this gate.
#[derive(Debug, Clone, Copy)]
pub struct MonotonicEpochClock {
    anchor_epoch_ns: i64,
    anchor: Instant,
}

impl MonotonicEpochClock {
    pub fn new() -> Self {
        let anchor_epoch_ns = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|since| i64::try_from(since.as_nanos()).unwrap_or(i64::MAX))
            // A wall clock set before 1970 gives a zero anchor. Ages stay
            // correct, because they come from the monotonic part.
            .unwrap_or(0);
        Self {
            anchor_epoch_ns,
            anchor: Instant::now(),
        }
    }
}

impl Default for MonotonicEpochClock {
    fn default() -> Self {
        Self::new()
    }
}

impl FreshnessClock for MonotonicEpochClock {
    fn now_ns(&self) -> i64 {
        let elapsed_ns = i64::try_from(self.anchor.elapsed().as_nanos()).unwrap_or(i64::MAX);
        self.anchor_epoch_ns.saturating_add(elapsed_ns)
    }
}

/// A production [`LineFreshnessBoard`] on a [`MonotonicEpochClock`].
pub fn production_board(gap_tracking: GapTracking) -> LineFreshnessBoard {
    LineFreshnessBoard::new(Arc::new(MonotonicEpochClock::new()), gap_tracking)
}

/// The execution gate's [`MarketDataFreshnessProbe`], answered from a shared
/// [`LineFreshnessBoard`]. Cloning shares the board.
///
/// Every call reads the board at that instant; nothing is cached, so a line
/// that goes silent blocks the next order after it crosses the threshold and
/// a line that recovers admits the next order after its tick lands.
#[derive(Debug, Clone)]
pub struct BoardFreshnessProbe {
    board: LineFreshnessBoard,
}

impl BoardFreshnessProbe {
    pub fn new(board: LineFreshnessBoard) -> Self {
        Self { board }
    }

    pub fn board(&self) -> &LineFreshnessBoard {
        &self.board
    }
}

impl MarketDataFreshnessProbe for BoardFreshnessProbe {
    fn freshness(&self, symbol: &str, asset_class: AssetClass) -> MarketDataFreshness {
        self.board.freshness_for(symbol, asset_class).freshness
    }

    fn staleness_seconds(&self, symbol: &str, asset_class: AssetClass) -> Option<u64> {
        self.board.freshness_for(symbol, asset_class).age_seconds()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn monotonic_epoch_clock_never_goes_backwards_and_reads_as_epoch_time() {
        let clock = MonotonicEpochClock::new();
        let mut previous = clock.now_ns();
        for _ in 0..10_000 {
            let now = clock.now_ns();
            assert!(now >= previous, "the board clock went backwards");
            previous = now;
        }
        // After 2020-01-01: the anchor is an epoch reading, not zero.
        assert!(previous > 1_577_836_800_000_000_000);
    }
}
