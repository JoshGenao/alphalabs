//! SRS-EXE-001 / SyRS NFR-P1 live order acknowledgement latency, MVP form.
//!
//! The MVP acceptance criterion (docs/SRS.md §3.1) reads NFR-P1 as "< 1,000 ms p95
//! from strategy API invocation to strategy acknowledgement callback, measured with
//! the host monotonic clock". PTP-disciplined measurement is R2 (SRS-PERF-001), and
//! `LatencyVerificationArtifact` deliberately refuses a non-disciplined clock, so this
//! binary does NOT build that artifact. It feeds caller-supplied samples to the ONE
//! percentile engine (`LatencyPercentiles::from_samples`, nearest-rank) and compares
//! the p95 with the budget the catalog states for `LatencyNfr::OrderSignalToAck`,
//! so neither the math nor the budget is restated here.
//!
//! Usage:
//!   `nfr_p1_ack_cli --tier <FIXTURE|LIVE_IB>` — read whitespace-separated `u64`
//!   nanosecond samples from stdin; print the percentiles and one machine-readable
//!   line `nfr:NFR-P1 clock:host-monotonic tier:<tier> samples:N p95_ms:… budget_ms:…
//!   comparison:… verdict:PASS|FAIL`. Exit 0 only on PASS.
//!
//! `--tier` is required and is copied onto the verdict line: a measurement over the
//! fixture gateway proves the strategy-side and host path, not IB, and the line has
//! to say which one it is. Fail closed: a missing or unknown tier, an unknown flag,
//! or an empty or unparseable sample set exits non-zero with NO `verdict:` line.

use std::io::{self, Read};
use std::process::ExitCode;

use atp_types::perf::{LatencyNfr, LatencyPercentiles, Percentile, ThresholdComparison};

const NFR: LatencyNfr = LatencyNfr::OrderSignalToAck;
const TIERS: [&str; 2] = ["FIXTURE", "LIVE_IB"];

fn main() -> ExitCode {
    match run(
        &std::env::args().skip(1).collect::<Vec<_>>(),
        &mut io::stdin(),
    ) {
        Ok(true) => ExitCode::SUCCESS,
        Ok(false) => ExitCode::FAILURE,
        Err(message) => {
            eprintln!("nfr_p1_ack_cli: {message}");
            ExitCode::FAILURE
        }
    }
}

fn parse_tier(args: &[String]) -> Result<String, String> {
    match args {
        [flag, tier] if flag == "--tier" && TIERS.contains(&tier.as_str()) => Ok(tier.clone()),
        [flag, tier] if flag == "--tier" => Err(format!("--tier {tier:?} is not one of {TIERS:?}")),
        _ => Err(format!(
            "usage: nfr_p1_ack_cli --tier <{}>",
            TIERS.join("|")
        )),
    }
}

fn read_samples(reader: &mut impl Read) -> Result<Vec<u64>, String> {
    let mut buf = String::new();
    reader
        .read_to_string(&mut buf)
        .map_err(|e| format!("failed to read samples from stdin: {e}"))?;
    let samples = buf
        .split_whitespace()
        .map(|token| {
            token
                .parse::<u64>()
                .map_err(|e| format!("sample {token:?} is not a u64 nanosecond value: {e}"))
        })
        .collect::<Result<Vec<_>, _>>()?;
    if samples.is_empty() {
        return Err("no latency samples on stdin".into());
    }
    Ok(samples)
}

fn run(args: &[String], stdin: &mut impl Read) -> Result<bool, String> {
    let tier = parse_tier(args)?;
    let threshold = NFR
        .threshold_for_leg("")
        .ok_or_else(|| format!("{} has no single-leg threshold in the catalog", NFR.id()))?;
    let stated = threshold
        .stated_percentile
        .ok_or_else(|| format!("{} states no percentile budget", NFR.id()))?;
    let samples = read_samples(stdin)?;
    let percentiles = LatencyPercentiles::from_samples(&samples)
        .map_err(|e| format!("percentile computation failed: {e}"))?;
    let observed_ms = percentiles.get_millis_f64(stated);
    let budget_ms = threshold.bound_ms as f64;
    let pass = match threshold.comparison {
        ThresholdComparison::LessThan => observed_ms < budget_ms,
        ThresholdComparison::LessThanOrEqual => observed_ms <= budget_ms,
    };
    println!("{} {} (host monotonic clock)", NFR.id(), NFR.metric());
    for p in [
        Percentile::P50,
        Percentile::P95,
        Percentile::P99,
        Percentile::P999,
    ] {
        println!("  {}: {:.6} ms", p.as_str(), percentiles.get_millis_f64(p));
    }
    println!(
        "nfr:{} clock:host-monotonic tier:{tier} samples:{} {}_ms:{:.6} budget_ms:{} \
         comparison:{} verdict:{}",
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

#[cfg(test)]
mod tests {
    use super::*;

    fn args(v: &[&str]) -> Vec<String> {
        v.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn the_tier_is_required_and_closed() {
        assert!(parse_tier(&args(&[])).is_err());
        assert!(parse_tier(&args(&["--tier", "PAPER"])).is_err());
        assert!(parse_tier(&args(&["--tier", "FIXTURE", "extra"])).is_err());
        assert_eq!(
            parse_tier(&args(&["--tier", "LIVE_IB"])).unwrap(),
            "LIVE_IB"
        );
    }

    #[test]
    fn the_budget_comes_from_the_catalog_and_is_one_second_p95() {
        let threshold = NFR.threshold_for_leg("").expect("NFR-P1 has a single leg");
        assert_eq!(threshold.bound_ms, 1_000);
        assert_eq!(threshold.stated_percentile, Some(Percentile::P95));
    }

    #[test]
    fn verdicts_follow_the_p95_against_the_budget() {
        let fast = "1000000 ".repeat(100); // 1 ms each
        assert_eq!(
            run(&args(&["--tier", "FIXTURE"]), &mut fast.as_bytes()),
            Ok(true)
        );
        // 6 of 100 samples over budget puts p95 over budget.
        let slow = format!("{}{}", "1000000 ".repeat(94), "2000000000 ".repeat(6));
        assert_eq!(
            run(&args(&["--tier", "FIXTURE"]), &mut slow.as_bytes()),
            Ok(false)
        );
    }

    #[test]
    fn empty_or_garbage_samples_yield_no_verdict() {
        assert!(run(&args(&["--tier", "FIXTURE"]), &mut "".as_bytes()).is_err());
        assert!(run(&args(&["--tier", "FIXTURE"]), &mut "12 x".as_bytes()).is_err());
    }
}
