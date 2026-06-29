//! Backtest launch surface — binding wall-clock **calendar dates** to the
//! engine's deterministic timestamp axis (SRS-BT-001).
//!
//! The SRS-BT-001 acceptance criterion is "A backtest can be launched with system
//! data or uploaded Parquet data; **start and end dates are selectable** through
//! API and dashboard." The [`backtest`](crate::backtest) engine takes a
//! configurable [`DateRange`] of opaque ordered `u64` timestamps and deliberately
//! leaves "binding them to wall-clock calendar dates [as] the launch surface's
//! concern". This module is that binding: it parses an operator-supplied
//! `YYYY-MM-DD` start/end date into the engine's epoch-second [`DateRange`], so the
//! "dates are selectable" half of the acceptance criterion has a real, reusable,
//! fail-closed implementation rather than living only as a comment.
//!
//! # Why it lives here (and what is still deferred)
//!
//! This is the **Rust** launch-surface binding shared by the `bt001_backtest_cli`
//! operator binary today and available to any in-process Rust launcher. The
//! acceptance criterion's named *operator interfaces* — the REST handler for
//! `POST /api/v1/backtests` (SRS-API-001) and the dashboard date pickers (SRS-UI) —
//! and the user-uploaded **Apache Parquet** reader (a new external Rust dependency
//! that needs SyRS scope approval; SyRS SYS-14 / AC-4 / IF-5) and the Rust↔Python
//! strategy host remain deferred, so SRS-BT-001 stays `passes:false`. This module
//! deliberately closes none of those; it makes the date-selection half concrete and
//! gives the deferred surfaces one canonical, tested place to convert dates.
//!
//! # Integer-only, dependency-free
//!
//! The workspace carries zero third-party crates, so the calendar math here is the
//! canonical proleptic-Gregorian day-count (`days_from_civil` / `civil_from_days`,
//! after Howard Hinnant's `chrono`-algorithms), implemented in pure `i64` integer
//! arithmetic — no `f64`, no clock, no RNG — so a parsed window is a deterministic
//! pure function of its input strings. A date is validated by round-tripping it
//! through the inverse, so an impossible date (`2024-02-30`, month `13`) fails
//! closed rather than silently normalizing to a neighbouring real date.

use std::fmt;

use crate::backtest::DateRange;

/// Seconds in one UTC day. The engine's timestamp axis is interpreted as epoch
/// seconds by this launch binding (the axis itself is opaque to the engine).
pub const SECONDS_PER_DAY: u64 = 86_400;

/// A fail-closed error parsing an operator-supplied launch window.
///
/// Carries the offending input verbatim (no broker/vendor identifiers) so an
/// operator sees exactly which value was rejected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LaunchError {
    /// The string was not a `YYYY-MM-DD` date (wrong shape or non-numeric fields).
    MalformedDate { input: String },
    /// The string parsed as a date but is not a real calendar date, or falls
    /// before the `1970-01-01` epoch the `u64` axis can represent.
    DateOutOfRange { input: String },
    /// The window was inverted: the start date is after the end date. Mirrors the
    /// engine's [`BacktestError::InvalidDateRange`](crate::backtest::BacktestError::InvalidDateRange)
    /// guard at the launch boundary so an inverted window is rejected before a run
    /// is ever constructed.
    InvertedRange { start: String, end: String },
}

impl fmt::Display for LaunchError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::MalformedDate { input } => {
                write!(f, "malformed date {input:?}: expected YYYY-MM-DD")
            }
            Self::DateOutOfRange { input } => write!(
                f,
                "date {input:?} is not a real calendar date on or after 1970-01-01"
            ),
            Self::InvertedRange { start, end } => write!(
                f,
                "inverted backtest window: start {start:?} is after end {end:?}"
            ),
        }
    }
}

impl std::error::Error for LaunchError {}

/// A parsed launch window: the engine [`DateRange`] plus the operator's original
/// calendar-date strings, so a launcher can echo back exactly what it selected.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchWindow {
    /// The operator's start date, verbatim (`YYYY-MM-DD`).
    pub start_date: String,
    /// The operator's end date, verbatim (`YYYY-MM-DD`).
    pub end_date: String,
    /// The engine window: `[start-of-start-day, end-of-end-day]` in epoch seconds,
    /// inclusive on both ends so a single-day window still covers that whole day.
    pub range: DateRange,
}

/// Parse an inclusive operator launch window from `YYYY-MM-DD` start/end dates.
///
/// The window spans `[00:00:00 of start, 23:59:59 of end]` in epoch seconds, so the
/// engine's inclusive `[start, end]` replay covers every bar timestamped within the
/// selected calendar days. Fails closed on a malformed date, an impossible calendar
/// date, a pre-epoch date, or an inverted window — never silently produces an empty
/// or wrong window.
pub fn parse_window(start: &str, end: &str) -> Result<LaunchWindow, LaunchError> {
    let start_day = parse_epoch_day(start)?;
    let end_day = parse_epoch_day(end)?;
    if start_day > end_day {
        return Err(LaunchError::InvertedRange {
            start: start.to_string(),
            end: end.to_string(),
        });
    }
    // start_day / end_day are >= 0 (parse_epoch_day rejects pre-epoch), so these
    // products are non-negative and well within u64.
    let start_secs = start_day as u64 * SECONDS_PER_DAY;
    let end_secs = end_day as u64 * SECONDS_PER_DAY + (SECONDS_PER_DAY - 1);
    Ok(LaunchWindow {
        start_date: start.to_string(),
        end_date: end.to_string(),
        range: DateRange::new(start_secs, end_secs),
    })
}

/// Parse one `YYYY-MM-DD` date to days since `1970-01-01`, rejecting non-dates,
/// impossible dates, and pre-epoch dates. Public so a launcher can resolve a single
/// date (e.g. a default-end-of-today binding the caller supplies).
pub fn parse_epoch_day(input: &str) -> Result<i64, LaunchError> {
    let (y, m, d) = parse_ymd(input)?;
    // Reject an out-of-band month/day before the round-trip, so days_from_civil is
    // only ever handed a plausible (y, m, d).
    if !(1..=12).contains(&m) || !(1..=31).contains(&d) {
        return Err(LaunchError::DateOutOfRange {
            input: input.to_string(),
        });
    }
    let day = days_from_civil(y, m, d);
    // Round-trip through the inverse: an impossible date (2024-02-30) maps forward
    // to a real day that maps back to a DIFFERENT (y, m, d), so the mismatch is the
    // fail-closed validity check.
    if civil_from_days(day) != (y, m, d) {
        return Err(LaunchError::DateOutOfRange {
            input: input.to_string(),
        });
    }
    // The u64 axis cannot represent a pre-epoch instant; reject rather than wrap.
    if day < 0 {
        return Err(LaunchError::DateOutOfRange {
            input: input.to_string(),
        });
    }
    Ok(day)
}

/// Split `YYYY-MM-DD` into `(year, month, day)` integers, or fail closed.
fn parse_ymd(input: &str) -> Result<(i64, i64, i64), LaunchError> {
    let malformed = || LaunchError::MalformedDate {
        input: input.to_string(),
    };
    let parts: Vec<&str> = input.split('-').collect();
    if parts.len() != 3 {
        return Err(malformed());
    }
    // Fixed field widths (YYYY-MM-DD) so "2024-1-2" or "24-01-02" is rejected as
    // malformed rather than quietly accepted.
    if parts[0].len() != 4 || parts[1].len() != 2 || parts[2].len() != 2 {
        return Err(malformed());
    }
    let y = parse_field(parts[0]).ok_or_else(malformed)?;
    let m = parse_field(parts[1]).ok_or_else(malformed)?;
    let d = parse_field(parts[2]).ok_or_else(malformed)?;
    Ok((y, m, d))
}

/// Parse a run of ASCII digits to an `i64` (no sign, no whitespace).
fn parse_field(field: &str) -> Option<i64> {
    if field.is_empty() || !field.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    field.parse::<i64>().ok()
}

/// Days from `1970-01-01` for a proleptic-Gregorian `(y, m, d)`.
///
/// Howard Hinnant's `days_from_civil`; exact integer arithmetic, valid for the full
/// `i64` year range. Caller guarantees `1 <= m <= 12` and `1 <= d <= 31`.
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400; // [0, 399]
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1; // [0, 365]
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy; // [0, 146096]
    era * 146097 + doe - 719_468
}

/// Inverse of [`days_from_civil`]: the proleptic-Gregorian `(y, m, d)` for a day
/// count since `1970-01-01`. Howard Hinnant's `civil_from_days`.
fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = doy - (153 * mp + 2) / 5 + 1; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 }; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_anchor_is_day_zero() {
        assert_eq!(parse_epoch_day("1970-01-01").unwrap(), 0);
    }

    #[test]
    fn known_epoch_days() {
        // Cross-checked against the proleptic Gregorian calendar.
        assert_eq!(parse_epoch_day("1970-01-02").unwrap(), 1);
        assert_eq!(parse_epoch_day("2000-01-01").unwrap(), 10_957);
        assert_eq!(parse_epoch_day("2024-01-02").unwrap(), 19_724);
        // 2024 is a leap year: Feb 29 exists and March 1 is the day after.
        assert_eq!(
            parse_epoch_day("2024-03-01").unwrap() - parse_epoch_day("2024-02-29").unwrap(),
            1
        );
    }

    #[test]
    fn civil_round_trips_for_a_span_of_days() {
        // Every day across a multi-year span maps forward and back to itself.
        for day in 0..(40 * 365) {
            let (y, m, d) = civil_from_days(day);
            assert_eq!(days_from_civil(y, m, d), day, "round trip failed at {day}");
        }
    }

    #[test]
    fn rejects_impossible_calendar_dates() {
        // 2023 is not a leap year: Feb 29 does not exist.
        assert_eq!(
            parse_epoch_day("2023-02-29"),
            Err(LaunchError::DateOutOfRange {
                input: "2023-02-29".to_string()
            })
        );
        assert!(matches!(
            parse_epoch_day("2024-02-30"),
            Err(LaunchError::DateOutOfRange { .. })
        ));
        assert!(matches!(
            parse_epoch_day("2024-13-01"),
            Err(LaunchError::DateOutOfRange { .. })
        ));
        assert!(matches!(
            parse_epoch_day("2024-04-31"),
            Err(LaunchError::DateOutOfRange { .. })
        ));
    }

    #[test]
    fn rejects_malformed_dates() {
        for bad in [
            "2024/01/02",
            "2024-1-2",
            "24-01-02",
            "not-a-date",
            "2024-01",
            "2024-01-0x",
        ] {
            assert!(
                matches!(parse_epoch_day(bad), Err(LaunchError::MalformedDate { .. })),
                "expected MalformedDate for {bad:?}"
            );
        }
    }

    #[test]
    fn rejects_pre_epoch_dates() {
        assert!(matches!(
            parse_epoch_day("1969-12-31"),
            Err(LaunchError::DateOutOfRange { .. })
        ));
    }

    #[test]
    fn window_is_inclusive_on_both_ends() {
        let window = parse_window("2024-01-02", "2024-01-05").unwrap();
        assert_eq!(window.start_date, "2024-01-02");
        assert_eq!(window.end_date, "2024-01-05");
        let start_day = parse_epoch_day("2024-01-02").unwrap() as u64;
        let end_day = parse_epoch_day("2024-01-05").unwrap() as u64;
        assert_eq!(window.range.start, start_day * SECONDS_PER_DAY);
        // The end is the LAST second of the end day, so a bar at the end day's
        // midnight is inside the window.
        assert_eq!(
            window.range.end,
            end_day * SECONDS_PER_DAY + (SECONDS_PER_DAY - 1)
        );
        assert!(window.range.contains(end_day * SECONDS_PER_DAY));
    }

    #[test]
    fn single_day_window_covers_that_whole_day() {
        let window = parse_window("2024-06-29", "2024-06-29").unwrap();
        let day = parse_epoch_day("2024-06-29").unwrap() as u64;
        assert!(window.range.contains(day * SECONDS_PER_DAY));
        assert!(window
            .range
            .contains(day * SECONDS_PER_DAY + (SECONDS_PER_DAY - 1)));
        assert!(!window.range.contains((day + 1) * SECONDS_PER_DAY));
    }

    #[test]
    fn rejects_inverted_window() {
        assert_eq!(
            parse_window("2024-02-01", "2024-01-01"),
            Err(LaunchError::InvertedRange {
                start: "2024-02-01".to_string(),
                end: "2024-01-01".to_string()
            })
        );
    }

    #[test]
    fn propagates_a_bad_bound_in_a_window() {
        assert!(matches!(
            parse_window("2024-02-30", "2024-03-01"),
            Err(LaunchError::DateOutOfRange { .. })
        ));
        assert!(matches!(
            parse_window("2024-01-01", "bogus"),
            Err(LaunchError::MalformedDate { .. })
        ));
    }
}
