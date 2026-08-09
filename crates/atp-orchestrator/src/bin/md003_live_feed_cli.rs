//! SRS-MD-003 — the live heartbeat feed daemon (operator-run).
//!
//! Opens a DEDICATED IB market-data session, subscribes the requested symbols,
//! and runs the freshness loop continuously: observe every delivered tick,
//! round-trip the gateway on a cadence, evaluate against the 15 s threshold,
//! and rewrite a durable snapshot the dashboard reads
//! (`SnapshotHeartbeatSource`). This is the producer
//! `heartbeat_freshness_contract.deferred[]` named — the fixture
//! `md003_heartbeat_cli` replays a script; this one watches a real market.
//!
//! ```text
//! md003_live_feed_cli --snapshot <path> --symbol AAPL [--symbol MSFT]
//!                     [--client-id 7] [--cadence-ms 5000]
//!                     [--poll-budget-ms 1500] [--max-steps N]
//! ```
//!
//! Requires the operator-gated `ib-live-transport` feature and a reachable IB
//! Gateway (`ATP_IB_HOST` / `ATP_IB_PAPER_PORT`); it binds the IB port, so it
//! never runs in the parallel agent pool (SyRS SYS-2e).
//!
//! Every argument is allow-listed and every value is range-checked before a
//! socket is opened: a daemon that silently accepted `--cadence-ms 0` or an
//! unknown flag would monitor on terms nobody chose.

#[cfg(not(feature = "ib-live-transport"))]
fn main() -> std::process::ExitCode {
    eprintln!(
        "md003_live_feed_cli: built without the `ib-live-transport` feature — rebuild with \
         `cargo build -p atp-orchestrator --features ib-live-transport --bin md003_live_feed_cli`. \
         Refusing to start rather than pretending to monitor a feed."
    );
    std::process::ExitCode::from(2)
}

#[cfg(feature = "ib-live-transport")]
fn main() -> std::process::ExitCode {
    live::main()
}

#[cfg(feature = "ib-live-transport")]
mod live {
    use atp_market_data::live_feed::{write_snapshot, BrokerProbe, FeedError, LiveFeedLoop};
    use atp_market_data::HeartbeatPublishError;
    use atp_orchestrator::live_market_data::IbLiveTickSource;
    use atp_types::{HeartbeatStalenessEvent, HEARTBEAT_STALENESS_THRESHOLD_MS};
    use std::path::PathBuf;
    use std::process::ExitCode;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    /// Prints each Fresh↔Stale transition as it is published, so the operator
    /// watching the live window sees the flip the AC asks them to witness.
    struct StdoutEventSink;

    impl atp_market_data::HeartbeatEventSink for StdoutEventSink {
        fn record(&self, event: HeartbeatStalenessEvent) -> Result<(), HeartbeatPublishError> {
            println!(
                "event kind={} feed={:?} staleness_ms={} evaluated_at_ns={} threshold_ms={}",
                match event.transition {
                    atp_types::HeartbeatTransition::BecameStale => "HEARTBEAT_STALE",
                    atp_types::HeartbeatTransition::Recovered => "HEARTBEAT_RECOVERED",
                },
                event.feed,
                event
                    .staleness_ms
                    .map_or_else(|| "none".to_string(), |ms| ms.to_string()),
                event.evaluated_at_ns,
                event.threshold_ms,
            );
            Ok(())
        }
    }

    #[derive(Debug)]
    struct Args {
        snapshot: PathBuf,
        symbols: Vec<String>,
        client_id: i32,
        cadence: Duration,
        poll_budget: Duration,
        max_steps: Option<u64>,
    }

    const USAGE: &str = "usage: md003_live_feed_cli --snapshot <path> --symbol <SYM> \
                         [--symbol <SYM>...] [--client-id <n>] [--cadence-ms <n>] \
                         [--poll-budget-ms <n>] [--max-steps <n>]";

    fn parse_u64(value: &str, flag: &str) -> Result<u64, String> {
        value
            .parse::<u64>()
            .map_err(|_| format!("{flag} expects a non-negative integer, got {value:?}"))
    }

    fn parse_args(argv: Vec<String>) -> Result<Args, String> {
        let mut snapshot = None;
        let mut symbols = Vec::new();
        let mut client_id = 7_i32;
        let mut cadence_ms = 5_000_u64;
        // 1500 ms, not the 500 ms this shipped with. A poll budget that expires
        // partway through an inbound frame is a transport fault (the session is
        // dropped and rebuilt so the stream resynchronizes), and the shorter the
        // budget the more often a re-armed drain lands mid-frame: the
        // 2026-08-03 live window took 5 such resyncs in ~150 steps at 500 ms and
        // far fewer at 1500 ms. Still well under the cadence, so the broker
        // probe keeps its interval.
        let mut poll_budget_ms = 1_500_u64;
        let mut max_steps = None;

        let mut rest = argv.into_iter();
        while let Some(flag) = rest.next() {
            let mut value = || {
                rest.next()
                    .ok_or_else(|| format!("{flag} expects a value\n{USAGE}"))
            };
            match flag.as_str() {
                "--snapshot" => snapshot = Some(PathBuf::from(value()?)),
                "--symbol" => symbols.push(value()?),
                "--client-id" => {
                    let raw = value()?;
                    client_id = raw
                        .parse()
                        .map_err(|_| format!("--client-id expects an integer, got {raw:?}"))?;
                }
                "--cadence-ms" => cadence_ms = parse_u64(&value()?, "--cadence-ms")?,
                "--poll-budget-ms" => poll_budget_ms = parse_u64(&value()?, "--poll-budget-ms")?,
                "--max-steps" => max_steps = Some(parse_u64(&value()?, "--max-steps")?),
                // Allow-list: an unrecognized flag is a refusal, never a
                // silently-ignored intention.
                other => return Err(format!("unknown argument {other:?}\n{USAGE}")),
            }
        }

        let snapshot = snapshot.ok_or_else(|| format!("--snapshot is required\n{USAGE}"))?;
        if symbols.is_empty() {
            return Err(format!("at least one --symbol is required\n{USAGE}"));
        }
        // Degenerate values are refused HERE rather than producing a daemon that
        // spins at 0 ms or probes so slowly it manufactures its own staleness.
        if cadence_ms == 0 || poll_budget_ms == 0 {
            return Err("--cadence-ms and --poll-budget-ms must be greater than zero".to_string());
        }
        if cadence_ms >= HEARTBEAT_STALENESS_THRESHOLD_MS {
            return Err(format!(
                "--cadence-ms {cadence_ms} must stay below the {HEARTBEAT_STALENESS_THRESHOLD_MS} \
                 ms staleness threshold, or the broker line goes stale from the probe interval \
                 alone"
            ));
        }
        if poll_budget_ms >= cadence_ms {
            return Err(format!(
                "--poll-budget-ms {poll_budget_ms} must stay below --cadence-ms {cadence_ms}, or \
                 one drain consumes the whole probe interval"
            ));
        }
        // The SUM, not just each part: a degraded step pauses and then drains
        // before the snapshot is rewritten, so two individually-legal values
        // can still leave the dashboard reading a file older than the 15 s
        // guard. `LiveFeedLoop::new` refuses this too — it is repeated here so
        // the daemon fails on its arguments instead of after opening an IB
        // session and subscribing lines.
        if poll_budget_ms + cadence_ms >= HEARTBEAT_STALENESS_THRESHOLD_MS {
            return Err(format!(
                "--poll-budget-ms {poll_budget_ms} plus --cadence-ms {cadence_ms} reaches the \
                 {HEARTBEAT_STALENESS_THRESHOLD_MS} ms staleness threshold — a degraded step \
                 could leave the snapshot older than the threshold it reports on"
            ));
        }
        if max_steps == Some(0) {
            return Err("--max-steps must be greater than zero".to_string());
        }

        Ok(Args {
            snapshot,
            symbols,
            client_id,
            cadence: Duration::from_millis(cadence_ms),
            poll_budget: Duration::from_millis(poll_budget_ms),
            max_steps,
        })
    }

    /// The single wall-clock read in the whole path, taken at the outermost
    /// edge so the monitor and the loop stay deterministic.
    fn now_ns() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .ok()
            .and_then(|since| i64::try_from(since.as_nanos()).ok())
            .unwrap_or(i64::MAX)
    }

    pub fn main() -> ExitCode {
        let args = match parse_args(std::env::args().skip(1).collect()) {
            Ok(args) => args,
            Err(message) => {
                eprintln!("md003_live_feed_cli: {message}");
                return ExitCode::from(2);
            }
        };

        let source =
            match IbLiveTickSource::connect(&args.symbols, args.client_id, args.poll_budget) {
                Ok(source) => source,
                Err(err) => {
                    eprintln!("md003_live_feed_cli: {err}");
                    return ExitCode::from(2);
                }
            };
        let watched = source.watched();
        println!(
            "subscribed lines={} snapshot={:?} cadence={:?} poll_budget={:?}",
            watched.len(),
            args.snapshot,
            args.cadence,
            args.poll_budget
        );

        let mut feed = match LiveFeedLoop::new(source, watched, args.poll_budget, args.cadence) {
            Ok(feed) => feed,
            Err(err) => {
                eprintln!("md003_live_feed_cli: {err}");
                return ExitCode::from(2);
            }
        };

        let sink = StdoutEventSink;
        let mut steps = 0_u64;
        // Consecutive degraded steps, which is what paces this loop when the
        // source stops blocking. A healthy drain spends its poll budget on the
        // wire, so the loop is self-pacing; a refused connection returns at
        // once, and without this the loop spins through its whole step budget
        // in seconds during exactly the outage it is supposed to be reporting.
        let mut consecutive_failures = 0_u32;
        loop {
            // `step` reads the clock itself — before the drain, and again after
            // the blocking I/O for the verdict. The snapshot is stamped with
            // THAT second reading, so the header a reader ages the file by is
            // the same instant the rows were judged at.
            let step = feed.step(now_ns, &sink);
            if let Err(err) = write_snapshot(&args.snapshot, &step, step.evaluated_at_ns) {
                // The snapshot IS the dashboard's view. If it cannot be
                // written, continuing would leave the operator reading an
                // increasingly old file with no indication anything is wrong —
                // so stop loudly instead.
                eprintln!("md003_live_feed_cli: {err}");
                return ExitCode::from(1);
            }
            if let Some(FeedError::Source(detail)) = &step.source_error {
                eprintln!("md003_live_feed_cli: degraded step: {detail}");
            }
            if step.broker_probe == BrokerProbe::Failed {
                eprintln!("md003_live_feed_cli: broker round trip did not answer");
            }

            steps += 1;
            if args.max_steps.is_some_and(|max| steps >= max) {
                println!("completed {steps} steps");
                return ExitCode::SUCCESS;
            }

            // The pacing rule itself lives on the loop, next to the timing
            // state it depends on (`LiveFeedLoop::pace_after`) — a driver that
            // decided this for itself would have to re-derive when a broker
            // probe is due, and get it right. The snapshot above is already
            // written before any pause, so the operator's view is current
            // while we wait. A clean drain clears the counter: this paces
            // failure, it does not ration recovery.
            if step.poll_failed {
                consecutive_failures = consecutive_failures.saturating_add(1);
            } else {
                consecutive_failures = 0;
            }
            std::thread::sleep(feed.pace_after(&step, consecutive_failures));
        }
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn args(raw: &[&str]) -> Result<Args, String> {
            parse_args(raw.iter().map(|s| (*s).to_string()).collect())
        }

        #[test]
        fn a_minimal_invocation_parses_with_safe_defaults() {
            let parsed = args(&["--snapshot", "/tmp/s", "--symbol", "AAPL"]).expect("valid");
            assert_eq!(parsed.symbols, vec!["AAPL".to_string()]);
            assert!(parsed.cadence < Duration::from_millis(HEARTBEAT_STALENESS_THRESHOLD_MS));
            assert!(parsed.poll_budget < parsed.cadence);
        }

        #[test]
        fn a_pause_plus_a_drain_that_reaches_the_threshold_is_refused() {
            // Each value is individually legal — cadence under the threshold,
            // budget under the cadence — but a degraded step pauses and THEN
            // drains, so the snapshot would be rewritten less often than the
            // 15 s guard the dashboard ages it by. Refused on the arguments,
            // before an IB session is opened and lines are subscribed.
            let error = args(&[
                "--snapshot",
                "/tmp/s",
                "--symbol",
                "A",
                "--cadence-ms",
                "14999",
                "--poll-budget-ms",
                "14998",
            ])
            .unwrap_err();
            assert!(
                error.contains("staleness threshold"),
                "unexpected error: {error}"
            );
        }

        #[test]
        fn the_default_poll_budget_is_wide_enough_to_land_between_frames() {
            // Pins the value, not just the ordering: at the 500 ms this shipped
            // with, the 2026-08-03 live window hit 5 mid-frame budget expiries
            // in ~150 steps, each one a transport fault that drops and rebuilds
            // the session. `poll_budget < cadence` alone would still hold at
            // 500 ms, so asserting only that would let the regression back in.
            let parsed = args(&["--snapshot", "/tmp/s", "--symbol", "AAPL"]).expect("valid");
            assert!(
                parsed.poll_budget >= Duration::from_millis(1_000),
                "default poll budget {:?} is back in the range that resynchronized \
                 the stream every ~30 steps",
                parsed.poll_budget
            );
        }

        #[test]
        fn unknown_flags_are_refused() {
            let error = args(&["--snapshot", "/tmp/s", "--symbol", "A", "--yolo"]).unwrap_err();
            assert!(error.contains("unknown argument"));
        }

        #[test]
        fn degenerate_numerics_are_refused() {
            assert!(args(&["--snapshot", "/tmp/s", "--symbol", "A", "--cadence-ms", "0"]).is_err());
            assert!(args(&[
                "--snapshot",
                "/tmp/s",
                "--symbol",
                "A",
                "--poll-budget-ms",
                "0"
            ])
            .is_err());
            assert!(args(&["--snapshot", "/tmp/s", "--symbol", "A", "--max-steps", "0"]).is_err());
            assert!(args(&[
                "--snapshot",
                "/tmp/s",
                "--symbol",
                "A",
                "--cadence-ms",
                "-5"
            ])
            .is_err());
        }

        #[test]
        fn a_cadence_at_the_staleness_threshold_is_refused() {
            let error = args(&[
                "--snapshot",
                "/tmp/s",
                "--symbol",
                "A",
                "--cadence-ms",
                "15000",
            ])
            .unwrap_err();
            assert!(error.contains("staleness threshold"));
        }

        #[test]
        fn a_poll_budget_at_the_cadence_is_refused() {
            let error = args(&[
                "--snapshot",
                "/tmp/s",
                "--symbol",
                "A",
                "--cadence-ms",
                "1000",
                "--poll-budget-ms",
                "1000",
            ])
            .unwrap_err();
            assert!(error.contains("--poll-budget-ms"));
        }

        #[test]
        fn the_required_arguments_are_required() {
            assert!(args(&["--symbol", "AAPL"])
                .unwrap_err()
                .contains("--snapshot"));
            assert!(args(&["--snapshot", "/tmp/s"])
                .unwrap_err()
                .contains("--symbol"));
            assert!(args(&["--snapshot"])
                .unwrap_err()
                .contains("expects a value"));
        }
    }
}
