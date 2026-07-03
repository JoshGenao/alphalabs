//! SRS-SDK-004 / SyRS NFR-P4 order-event callback latency percentile CLI.
//!
//! The operator-/test-facing surface for the callback-delivery latency budget of
//! "deliver order event callbacks to Python strategy code" (docs/SRS.md SRS-5.2
//! SRS-SDK-004; SyRS SYS-7 / SYS-85 / NFR-P4: live callback delivery < 1000 ms p95
//! from broker fill acknowledgement, paper callback delivery < 100 ms p95 from
//! simulated fill). The percentile substrate (`atp_types::perf`, SRS-PERF-001) is
//! already built and owns the nearest-rank math and the NFR-P4 leg budgets; this
//! binary makes it *drivable over caller-supplied samples* so the SDK delivery
//! seam's real latency samples (produced by
//! `tests/domain/test_paper_callback_delivery.py` over
//! `atp_strategy.dispatch.deliver_order_event`) are evaluated against the NFR-P4
//! budget by the ONE authoritative percentile engine — no re-implemented
//! percentile math on the Python side.
//!
//! Usage:
//!   `nfr_p95_cli <paper|live>` — read whitespace-separated `u64` nanosecond
//!   latency samples from stdin, compute p50/p95/p99/p99.9 via
//!   [`LatencyPercentiles::from_samples`], look up the NFR-P4 leg's budget, and
//!   print the percentiles (ms) + a `verdict:PASS|FAIL` on the leg's stated
//!   percentile vs its budget. Exit code is success only on `PASS`.
//!
//!   `nfr_p95_cli <paper|live> --ptp-offset-ns N --window-start-ns S
//!   --window-end-ns E` — additionally build the full
//!   [`LatencyVerificationArtifact`] (the PTP-disciplined SRS-PERF-001 artifact)
//!   and print it. This path is for an operator running on a **PTP-disciplined
//!   host**: only there can the NFR-P4 verification artifact be honestly claimed
//!   (the substrate refuses a non-disciplined clock). All three flags are required
//!   together; supplying some but not all fails closed.
//!
//! **Honesty:** the default (no-flags) path is a *percentile computation over
//! caller-supplied samples*, NOT a PTP-disciplined NFR-P4 verification artifact.
//! The end-to-end, PTP-disciplined p95 proof (live from a real IB broker fill ack;
//! paper through the running simulation engine) is deferred / serialized to
//! SRS-EXE-001 / SRS-EXE-006 (live) and SRS-SIM-001 (paper) per the deferred[]
//! owners in `architecture/runtime_services.json`.
//!
//! Fail closed: an unknown leg, a partial PTP flag set, an unknown flag, an empty
//! or unparseable sample set, or a clock/window the substrate rejects exits
//! non-zero with an error on stderr and NO `verdict:` line — a budget "proof" is
//! never printed over an input the substrate would refuse.

use std::io::{self, Read};
use std::process::ExitCode;

use atp_types::perf::{
    LatencyNfr, LatencyPercentiles, LatencyThreshold, LatencyVerificationArtifact, Percentile,
    PtpClockDiscipline, ThresholdComparison,
};

const NFR: LatencyNfr = LatencyNfr::OrderEventCallback;

/// Parsed optional PTP-artifact flags: all-or-nothing.
#[derive(Debug)]
struct PtpArgs {
    offset_ns: u64,
    window_start_ns: i64,
    window_end_ns: i64,
}

fn main() -> ExitCode {
    match run() {
        Ok(pass) => {
            if pass {
                ExitCode::SUCCESS
            } else {
                // A well-formed measurement that breaches the budget: the verdict
                // line was printed; exit non-zero so a regression fails the gate.
                ExitCode::FAILURE
            }
        }
        Err(msg) => {
            eprintln!("nfr_p95_cli: {msg}");
            ExitCode::FAILURE
        }
    }
}

/// Returns `Ok(pass)` where `pass` is the budget verdict, or `Err(message)` when
/// the input is refused (fail-closed, no verdict printed).
fn run() -> Result<bool, String> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (leg, ptp) = parse_args(&args)?;

    // The NFR-P4 leg must be a real leg of the catalog ("paper"/"live"); reject
    // anything else before reading samples so an unknown leg never yields a proof.
    let threshold: &LatencyThreshold = NFR.threshold_for_leg(&leg).ok_or_else(|| {
        format!(
            "{} has no leg {leg:?} (expected \"paper\" or \"live\")",
            NFR.id()
        )
    })?;

    let samples = read_samples(&mut io::stdin())?;
    if samples.is_empty() {
        return Err("no latency samples on stdin (expected whitespace-separated u64 ns)".into());
    }

    let percentiles = LatencyPercentiles::from_samples(&samples)
        .map_err(|e| format!("percentile computation failed: {e}"))?;

    // Evaluate the leg's STATED percentile against its budget. NFR-P4's legs are
    // both p95 budgets; a leg with no stated percentile (a flat-max leg) is not an
    // NFR-P4 concern, but guard against it so the CLI never invents a p95 verdict
    // for a flat-threshold leg.
    let stated = threshold.stated_percentile.ok_or_else(|| {
        format!(
            "{} leg {leg:?} states no percentile budget; cannot evaluate a p95 verdict",
            NFR.id()
        )
    })?;
    let observed_ms = percentiles.get_millis_f64(stated);
    let budget_ms = threshold.bound_ms as f64;
    let pass = match threshold.comparison {
        ThresholdComparison::LessThan => observed_ms < budget_ms,
        ThresholdComparison::LessThanOrEqual => observed_ms <= budget_ms,
    };

    // Human-readable percentile report.
    println!("{} order-event callback latency — leg:{leg}", NFR.id());
    for p in [
        Percentile::P50,
        Percentile::P95,
        Percentile::P99,
        Percentile::P999,
    ] {
        println!("  {}: {:.6} ms", p.as_str(), percentiles.get_millis_f64(p));
    }
    println!(
        "  budget: {} {} ms (stated at {})",
        threshold.comparison.as_str(),
        threshold.bound_ms,
        stated.as_str()
    );

    // Optional PTP-disciplined verification artifact — only honest on a PTP host,
    // so it is opt-in via the operator-supplied offset + window.
    if let Some(ptp) = ptp {
        let artifact = LatencyVerificationArtifact::from_samples(
            NFR,
            &leg,
            PtpClockDiscipline::Disciplined {
                max_offset_ns: ptp.offset_ns,
            },
            ptp.window_start_ns,
            ptp.window_end_ns,
            &samples,
        )
        .map_err(|e| format!("verification artifact construction failed: {e}"))?;
        println!("--- PTP-disciplined NFR-P4 verification artifact ---");
        print!("{artifact}");
    } else {
        eprintln!(
            "note: percentile computation over caller-supplied samples; NOT a \
             PTP-disciplined NFR-P4 verification artifact (that requires a \
             PTP-disciplined host and is deferred to SRS-PERF-001 / SRS-EXE-001 \
             (live) / SRS-SIM-001 (paper))"
        );
    }

    // Machine-parseable verdict line (last, single line) for the driving test.
    println!(
        "nfr:{} leg:{leg} samples:{} {}_ms:{:.6} budget_ms:{} comparison:{} verdict:{}",
        NFR.id(),
        samples.len(),
        stated.as_str(),
        observed_ms,
        threshold.bound_ms,
        threshold.comparison.as_str(),
        if pass { "PASS" } else { "FAIL" },
    );
    Ok(pass)
}

/// Parse `<leg> [--ptp-offset-ns N --window-start-ns S --window-end-ns E]`.
/// The three PTP flags are all-or-nothing; any other flag or a missing value
/// fails closed.
fn parse_args(args: &[String]) -> Result<(String, Option<PtpArgs>), String> {
    let mut leg: Option<String> = None;
    let mut offset_ns: Option<u64> = None;
    let mut window_start_ns: Option<i64> = None;
    let mut window_end_ns: Option<i64> = None;

    let mut i = 0;
    while i < args.len() {
        let arg = &args[i];
        match arg.as_str() {
            "--ptp-offset-ns" => offset_ns = Some(parse_flag_u64(args, &mut i, "--ptp-offset-ns")?),
            "--window-start-ns" => {
                window_start_ns = Some(parse_flag_i64(args, &mut i, "--window-start-ns")?)
            }
            "--window-end-ns" => {
                window_end_ns = Some(parse_flag_i64(args, &mut i, "--window-end-ns")?)
            }
            other if other.starts_with("--") => {
                return Err(format!("unknown flag {other:?}"));
            }
            other => {
                if leg.is_some() {
                    return Err(format!("unexpected extra positional argument {other:?}"));
                }
                leg = Some(other.to_string());
            }
        }
        i += 1;
    }

    let leg = leg.ok_or("missing leg argument (expected \"paper\" or \"live\")")?;

    // All-or-nothing PTP flag set.
    let ptp = match (offset_ns, window_start_ns, window_end_ns) {
        (None, None, None) => None,
        (Some(offset_ns), Some(window_start_ns), Some(window_end_ns)) => Some(PtpArgs {
            offset_ns,
            window_start_ns,
            window_end_ns,
        }),
        _ => {
            return Err(
                "--ptp-offset-ns, --window-start-ns, and --window-end-ns must be \
                        supplied together (the PTP verification-artifact path is all-or-nothing)"
                    .into(),
            );
        }
    };
    Ok((leg, ptp))
}

fn parse_flag_u64(args: &[String], i: &mut usize, name: &str) -> Result<u64, String> {
    *i += 1;
    let raw = args
        .get(*i)
        .ok_or_else(|| format!("{name} requires a value"))?;
    raw.parse::<u64>()
        .map_err(|e| format!("{name} value {raw:?} is not a u64: {e}"))
}

fn parse_flag_i64(args: &[String], i: &mut usize, name: &str) -> Result<i64, String> {
    *i += 1;
    let raw = args
        .get(*i)
        .ok_or_else(|| format!("{name} requires a value"))?;
    raw.parse::<i64>()
        .map_err(|e| format!("{name} value {raw:?} is not an i64: {e}"))
}

/// Read whitespace-separated `u64` nanosecond samples from `reader`. A single
/// unparseable token fails the whole run closed (a corrupt sample must not be
/// silently dropped from a latency distribution).
fn read_samples(reader: &mut impl Read) -> Result<Vec<u64>, String> {
    let mut buf = String::new();
    reader
        .read_to_string(&mut buf)
        .map_err(|e| format!("failed to read samples from stdin: {e}"))?;
    let mut samples = Vec::new();
    for token in buf.split_whitespace() {
        let value = token
            .parse::<u64>()
            .map_err(|e| format!("sample {token:?} is not a u64 nanosecond value: {e}"))?;
        samples.push(value);
    }
    Ok(samples)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn leg_only_parses_without_ptp() {
        let (leg, ptp) = parse_args(&args(&["paper"])).unwrap();
        assert_eq!(leg, "paper");
        assert!(ptp.is_none());
    }

    #[test]
    fn full_ptp_flag_set_parses() {
        let (leg, ptp) = parse_args(&args(&[
            "live",
            "--ptp-offset-ns",
            "500",
            "--window-start-ns",
            "1000",
            "--window-end-ns",
            "2000",
        ]))
        .unwrap();
        assert_eq!(leg, "live");
        let ptp = ptp.expect("full flag set yields ptp args");
        assert_eq!(ptp.offset_ns, 500);
        assert_eq!(ptp.window_start_ns, 1000);
        assert_eq!(ptp.window_end_ns, 2000);
    }

    #[test]
    fn partial_ptp_flag_set_fails_closed() {
        let err = parse_args(&args(&["paper", "--ptp-offset-ns", "500"])).unwrap_err();
        assert!(err.contains("all-or-nothing"), "unexpected error: {err}");
    }

    #[test]
    fn unknown_flag_fails_closed() {
        let err = parse_args(&args(&["paper", "--bogus", "5"])).unwrap_err();
        assert!(err.contains("unknown flag"), "unexpected error: {err}");
    }

    #[test]
    fn missing_flag_value_fails_closed() {
        assert!(parse_args(&args(&["paper", "--ptp-offset-ns"])).is_err());
    }

    #[test]
    fn missing_leg_fails_closed() {
        assert!(parse_args(&args(&[])).is_err());
    }

    #[test]
    fn extra_positional_fails_closed() {
        assert!(parse_args(&args(&["paper", "live"])).is_err());
    }

    #[test]
    fn non_numeric_flag_value_fails_closed() {
        assert!(parse_args(&args(&["paper", "--ptp-offset-ns", "xyz"])).is_err());
    }

    #[test]
    fn read_samples_parses_whitespace_separated() {
        let mut data: &[u8] = b"1 2\n3\t4";
        let parsed = read_samples(&mut data).unwrap();
        assert_eq!(parsed, vec![1u64, 2, 3, 4]);
    }

    #[test]
    fn read_samples_empty_is_ok_empty_vec() {
        // read_samples returns an empty vec on blank input; run() is what rejects
        // an empty sample set (verified end-to-end by the Python subprocess test).
        let mut data: &[u8] = b"   \n\t ";
        assert_eq!(read_samples(&mut data).unwrap(), Vec::<u64>::new());
    }

    #[test]
    fn read_samples_bad_token_fails_closed() {
        let mut data: &[u8] = b"1 x 3";
        assert!(read_samples(&mut data).is_err());
    }
}
