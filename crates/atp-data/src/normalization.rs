//! SRS-DATA-012 — split-adjusted historical normalization (the historical slice).
//!
//! The acceptance criterion (docs/SRS.md SRS-DATA-012): *"support raw, split-adjusted, fully
//! adjusted, and total-return normalization modes per security subscription ... indicators can
//! request adjusted series."* This module implements the **split-adjusted** historical read: given a
//! symbol's raw equity bars (from [`MarketDataStore::query_unified`](crate::query)) and its stock-split
//! corporate actions (records of [`DatasetKind::CorporateActionSplit`]), it returns the bars on a
//! split-comparable basis so a backtest / indicator spanning a split date sees a continuous series.
//! Fully-adjusted (dividends) and total-return modes are deferred (they need dividend data); the
//! live-subscription leg of SRS-DATA-012 is deferred (the Market Data Subscription Manager is unbuilt).
//!
//! ## The math (integer-exact, deterministic, no `f64`)
//!
//! For a bar at `event_ts = t`, over every split for the symbol with `effective_ts > t`:
//!
//! ```text
//!   NUM = ∏ numerator_i      DEN = ∏ denominator_i
//!   adjusted_price(t)  = round( raw_price  · DEN / NUM )   // OHLC: factor DEN/NUM
//!   adjusted_volume(t) = round( raw_volume · NUM / DEN )   // volume: the inverse NUM/DEN
//! ```
//!
//! `effective_ts` is the FIRST session on the new basis, so the filter is **strict** (`effective_ts >
//! t`): a bar dated ON the split date is already post-split and is left unadjusted; a bar dated the
//! day before is adjusted. A 4-for-1 forward split (`numerator=4, denominator=1`) divides pre-split
//! prices by 4 and multiplies pre-split volumes by 4, so the pre-split `$400` bar reads `$100` — the
//! same basis the post-split bars are quoted on.
//!
//! Three correctness disciplines, because this is money math an adversarial reviewer scrutinizes:
//!
//! 1. **Compose-then-divide.** All numerators are multiplied into `NUM` and all denominators into
//!    `DEN` FIRST; each field is divided exactly ONCE. Never adjust split-by-split with rounding in
//!    between — that would compound rounding error across multiple splits.
//! 2. **`i128` intermediates, fail-closed narrowing.** Products and the per-field multiply use `i128`
//!    (an `i64` value times a split product); the final result is narrowed back to `i64` with
//!    `try_from`, and an overflow is a fail-closed [`NormalizationError::Overflow`], never a silent
//!    wrap. A non-positive split factor is rejected before it can divide-by-zero or zero out a price.
//! 3. **Round half to even (banker's rounding).** Truncation toward zero would bias every adjusted
//!    price systematically DOWN — a real, persistent P&L drift across a long backtest. Round-half-to-
//!    even has zero expected bias across many roundings, is the IEEE-754 / fixed-point financial
//!    default, and best matches vendor reference adjusted series. Observable only on exact-half ties.
//!
//! Only the OHLC fields (`open`/`high`/`low`/`close`) take the price factor and `volume` takes the
//! inverse; every other field name passes through verbatim (an `open_interest` or a fundamental field
//! is never split-scaled). The natural key — including `event_ts` — is unchanged: a split adjustment
//! re-quotes the value fields, it does not move the bar in time.
//!
//! This module is CRATE-INTERNAL: the raw `split_adjust_records` / `SplitEvent` are not re-exported, so
//! the ONLY caller is the sibling coverage-enforcing gate ([`crate::coverage::MarketDataStore::
//! query_split_adjusted`]), which checks that the symbol's corporate-action coverage frontier reaches
//! the query end (SRS-DATA-011) BEFORE it reaches this math. So a split-adjusted result is served on a
//! public surface only behind proven coverage — there is no public path to raw-as-adjusted (a Rust
//! consumer cannot call this math directly and get IDENTITY values over an empty/incomplete split set).
//! It is foundational substrate proven by the unit tests below, so `dead_code` is allowed module-wide
//! rather than per item.
#![allow(dead_code)]

use crate::store::{DatasetKind, MarketDataRecord, MarketField};

/// The OHLC price fields that take the split price factor (`DEN/NUM`). Every other field name except
/// [`VOLUME_FIELD`] passes through unscaled.
const PRICE_FIELDS: [&str; 4] = ["close", "high", "low", "open"];
/// The volume field that takes the inverse split factor (`NUM/DEN`).
const VOLUME_FIELD: &str = "volume";

/// A stock-split corporate action for ONE symbol: an `numerator`-for-`denominator` ratio effective at
/// `effective_ts` (the first session on the new basis). A 4-for-1 forward split is
/// `{numerator: 4, denominator: 1}`; a 1-for-10 reverse split is `{numerator: 1, denominator: 10}`.
/// Both factors are validated `> 0`. The `symbol` binds the split to its instrument: a split adjusts
/// ONLY records of the same symbol, so a mixed-symbol batch can never cross-contaminate (an AAPL split
/// never touches an MSFT bar).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SplitEvent {
    /// The symbol this split applies to. A split adjusts only records whose symbol equals this.
    pub symbol: String,
    /// The effective instant (epoch seconds) — the first session quoted on the post-split basis.
    pub effective_ts: i64,
    /// The split numerator (`N` in an `N`-for-`M` split). Validated strictly positive.
    pub numerator: i64,
    /// The split denominator (`M` in an `N`-for-`M` split). Validated strictly positive.
    pub denominator: i64,
}

/// A fail-closed split-normalization error. Money math never silently wraps or divides by zero.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum NormalizationError {
    /// A split record carried a non-positive numerator or denominator (a zero/negative ratio could
    /// divide by zero or zero out a price). Rejected rather than applied.
    NonPositiveSplitFactor {
        /// The symbol the malformed split was keyed to.
        symbol: String,
        /// The offending numerator.
        numerator: i64,
        /// The offending denominator.
        denominator: i64,
    },
    /// A split record was missing its `numerator` or `denominator` value field.
    MissingSplitField {
        /// The symbol the malformed split was keyed to.
        symbol: String,
        /// The missing field name.
        field: &'static str,
    },
    /// An adjustment intermediate or result exceeded the `i64` value-field range. Fail closed —
    /// returning a wrapped price would be a silent money error.
    Overflow {
        /// A short description of where the overflow occurred (the field or product).
        context: String,
    },
    /// Split adjustment was requested for a record that is not an equity bar. Split-adjusting an
    /// option-chain snapshot or a fundamental record is meaningless and would corrupt it (e.g. it
    /// would scale an option's `volume` while leaving its `bid`/`ask`/`last` raw). Fail closed.
    UnsupportedKind {
        /// The (vendor-neutral) kind label that cannot be split-adjusted.
        kind: &'static str,
    },
}

impl std::fmt::Display for NormalizationError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::NonPositiveSplitFactor {
                symbol,
                numerator,
                denominator,
            } => write!(
                f,
                "non-positive split factor for {symbol}: {numerator}-for-{denominator} (both must be > 0)"
            ),
            Self::MissingSplitField { symbol, field } => {
                write!(f, "split record for {symbol} is missing its '{field}' field")
            }
            Self::UnsupportedKind { kind } => write!(
                f,
                "split-adjusted normalization applies only to equity bars (daily / minute); \
                 refusing to adjust a '{kind}' record"
            ),
            Self::Overflow { context } => {
                write!(f, "split-adjustment overflow ({context}) — refusing to wrap a money value")
            }
        }
    }
}

impl std::error::Error for NormalizationError {}

/// Extract the [`SplitEvent`]s for `symbol` from a set of split records (records of
/// [`DatasetKind::CorporateActionSplit`]). Records of other kinds or other symbols are ignored, so a
/// caller can pass the raw result of a kind-narrowed query. Fails closed on a malformed split — a
/// missing ratio field or a non-positive factor — rather than applying it.
pub fn split_events_for(
    symbol: &str,
    split_records: &[&MarketDataRecord],
) -> Result<Vec<SplitEvent>, NormalizationError> {
    let mut events = Vec::new();
    for record in split_records {
        let key = record.key();
        if key.kind != DatasetKind::CorporateActionSplit || key.symbol != symbol {
            continue;
        }
        let numerator = field_value(record, "numerator").ok_or_else(|| {
            NormalizationError::MissingSplitField {
                symbol: symbol.to_string(),
                field: "numerator",
            }
        })?;
        let denominator = field_value(record, "denominator").ok_or_else(|| {
            NormalizationError::MissingSplitField {
                symbol: symbol.to_string(),
                field: "denominator",
            }
        })?;
        if numerator <= 0 || denominator <= 0 {
            return Err(NormalizationError::NonPositiveSplitFactor {
                symbol: symbol.to_string(),
                numerator,
                denominator,
            });
        }
        events.push(SplitEvent {
            symbol: symbol.to_string(),
            effective_ts: key.event_ts,
            numerator,
            denominator,
        });
    }
    Ok(events)
}

/// Split-adjust one EQUITY-BAR record against `splits`. The natural key (including `event_ts`) is
/// preserved; the OHLC fields are scaled by the cumulative `DEN/NUM` of every split effective strictly
/// after the bar, `volume` by the inverse, and every other field passes through. With no applicable
/// split the cumulative factor is `1/1` — the identity, so an unsplit series is returned verbatim
/// (correct, not a masquerade).
///
/// Fails closed with [`NormalizationError::UnsupportedKind`] for any non-equity-bar record: split
/// adjustment is an equity-bar operation, and applying it to an option-chain snapshot (which carries
/// its own `volume`) or a fundamental record would corrupt it by scaling only the field names this
/// function knows. A caller must narrow to a daily/minute equity bar before requesting split-adjusted.
///
/// Every [`SplitEvent`] in `splits` is re-validated here (it is publicly constructible, so a direct
/// caller may bypass [`split_events_for`]'s checks): a non-positive numerator/denominator fails closed
/// with [`NormalizationError::NonPositiveSplitFactor`] before any arithmetic, so an invalid factor can
/// never divide-by-zero panic or silently miscompute a price.
pub fn split_adjust_record(
    record: &MarketDataRecord,
    splits: &[SplitEvent],
) -> Result<MarketDataRecord, NormalizationError> {
    if !matches!(
        record.key().kind,
        DatasetKind::DailyEquityBar | DatasetKind::MinuteEquityBar
    ) {
        return Err(NormalizationError::UnsupportedKind {
            kind: record.key().kind.as_str(),
        });
    }
    let symbol = &record.key().symbol;
    let event_ts = record.key().event_ts;
    // Compose-then-divide: accumulate the WHOLE numerator/denominator product first. ONLY splits for
    // THIS record's symbol apply -- a split carries its own symbol, so a mixed-symbol batch (or a
    // store-wide split list) can never cross-contaminate (an AAPL split never touches an MSFT bar).
    // `SplitEvent` is publicly constructible, so re-validate every matching factor HERE rather than
    // trusting the caller (`split_events_for` checks store-derived records, but a direct caller can
    // hand us a raw `SplitEvent`): a non-positive numerator/denominator -- a zero denominator zeroes a
    // price, a zero numerator used as the price divisor divide-by-zero panics -- fails closed (typed
    // error), uniformly for this symbol's splits, BEFORE any arithmetic.
    let mut num: i128 = 1;
    let mut den: i128 = 1;
    for split in splits {
        if split.symbol != *symbol {
            continue;
        }
        if split.numerator <= 0 || split.denominator <= 0 {
            return Err(NormalizationError::NonPositiveSplitFactor {
                symbol: symbol.clone(),
                numerator: split.numerator,
                denominator: split.denominator,
            });
        }
        if split.effective_ts > event_ts {
            num = checked_mul(num, i128::from(split.numerator), "split numerator product")?;
            den = checked_mul(den, i128::from(split.denominator), "split denominator product")?;
        }
    }

    let mut adjusted: Vec<MarketField> = Vec::with_capacity(record.fields().len());
    for field in record.fields() {
        let value = i128::from(field.value_minor);
        let new_value = if PRICE_FIELDS.contains(&field.name.as_str()) {
            // price · DEN / NUM  (NUM > 0: every numerator was validated positive)
            let scaled = checked_mul(value, den, &field.name)?;
            div_round_half_even(scaled, num)
        } else if field.name == VOLUME_FIELD {
            // volume · NUM / DEN  (DEN > 0)
            let scaled = checked_mul(value, num, &field.name)?;
            div_round_half_even(scaled, den)
        } else {
            value
        };
        let narrowed = i64::try_from(new_value).map_err(|_| NormalizationError::Overflow {
            context: format!("field '{}' result {new_value}", field.name),
        })?;
        adjusted.push(MarketField {
            name: field.name.clone(),
            value_minor: narrowed,
        });
    }

    // Adjusting values cannot break record validity: the key and the (sorted, unique, non-empty)
    // field names are unchanged, only the i64 values differ. So the rebuild is infallible by
    // construction — any error here would be a store-invariant bug, not a data condition.
    Ok(MarketDataRecord::new(record.key().clone(), adjusted)
        .expect("split-adjusted record preserves the source record's validity"))
}

/// Split-adjust a borrowed slice of records (the entry point the query CLI calls over a kind-narrowed,
/// `event_ts`-ascending result). Returns owned adjusted records in the same order.
pub fn split_adjust_records(
    records: &[&MarketDataRecord],
    splits: &[SplitEvent],
) -> Result<Vec<MarketDataRecord>, NormalizationError> {
    records
        .iter()
        .map(|record| split_adjust_record(record, splits))
        .collect()
}

/// `numer / denom` rounded half-to-even (banker's rounding), integer-exact. `denom` MUST be `> 0`
/// (guaranteed: it is a product of validated-positive split factors). Works for negative `numer`
/// too (`div_euclid`/`rem_euclid` floor toward −∞ with a non-negative remainder), and the half
/// comparison is written to avoid overflowing on a large `denom`.
fn div_round_half_even(numer: i128, denom: i128) -> i128 {
    debug_assert!(denom > 0, "denominator must be positive");
    let quotient = numer.div_euclid(denom);
    let remainder = numer.rem_euclid(denom); // 0 <= remainder < denom
    // Compare 2*remainder with denom WITHOUT computing 2*remainder (which could overflow for a huge
    // denom): 2r < d  <=>  r < d-r, and d-r is in (0, denom] so the subtraction is overflow-free.
    let complement = denom - remainder;
    if remainder < complement {
        quotient // closer to the floor
    } else if remainder > complement {
        quotient + 1 // closer to the ceiling
    } else if quotient.rem_euclid(2) == 0 {
        quotient // exact half -> round to the even neighbour
    } else {
        quotient + 1
    }
}

/// `a * b` in `i128`, mapping an overflow to a fail-closed [`NormalizationError::Overflow`].
fn checked_mul(a: i128, b: i128, context: &str) -> Result<i128, NormalizationError> {
    a.checked_mul(b).ok_or_else(|| NormalizationError::Overflow {
        context: context.to_string(),
    })
}

/// The value of `record`'s field named `name`, if present.
fn field_value(record: &MarketDataRecord, name: &str) -> Option<i64> {
    record
        .fields()
        .iter()
        .find(|field| field.name == name)
        .map(|field| field.value_minor)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::store::{MarketField, NaturalKey};

    fn field(name: &str, value_minor: i64) -> MarketField {
        MarketField {
            name: name.to_string(),
            value_minor,
        }
    }

    fn daily_bar(symbol: &str, event_ts: i64, ohlcv: [i64; 5]) -> MarketDataRecord {
        let [open, high, low, close, volume] = ohlcv;
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: symbol.to_string(),
                resolution: "1d".to_string(),
                event_ts,
                option_contract: None,
            },
            [
                field("open", open),
                field("high", high),
                field("low", low),
                field("close", close),
                field("volume", volume),
            ],
        )
        .expect("well-formed daily bar")
    }

    fn split_rec(
        symbol: &str,
        effective_ts: i64,
        numerator: i64,
        denominator: i64,
    ) -> MarketDataRecord {
        MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::CorporateActionSplit,
                symbol: symbol.to_string(),
                resolution: "split".to_string(),
                event_ts: effective_ts,
                option_contract: None,
            },
            [field("denominator", denominator), field("numerator", numerator)],
        )
        .expect("well-formed split record")
    }

    fn field_of(record: &MarketDataRecord, name: &str) -> i64 {
        field_value(record, name).expect("field present")
    }

    #[test]
    fn forward_split_divides_prices_and_multiplies_volume() {
        // 4-for-1 forward split effective at ts 200; the bar at ts 100 is pre-split.
        let bar = daily_bar("AAPL", 100, [9950, 10075, 9910, 10000, 100_000]);
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 1)]).unwrap();
        let adjusted = split_adjust_record(&bar, &splits).unwrap();
        // close 10000/4 = 2500 exactly; volume 100000*4 = 400000 exactly.
        assert_eq!(field_of(&adjusted, "close"), 2500);
        assert_eq!(field_of(&adjusted, "volume"), 400_000);
        // open 9950/4 = 2487.5 -> even 2488; high 10075/4 = 2518.75 -> 2519; low 9910/4 = 2477.5 -> 2478.
        assert_eq!(field_of(&adjusted, "open"), 2488);
        assert_eq!(field_of(&adjusted, "high"), 2519);
        assert_eq!(field_of(&adjusted, "low"), 2478);
        // The natural key (incl. event_ts) is unchanged.
        assert_eq!(adjusted.key(), bar.key());
    }

    #[test]
    fn reverse_split_multiplies_prices_and_divides_volume() {
        // 1-for-10 reverse split: pre-split prices ×10, volume /10.
        let bar = daily_bar("ZZZ", 100, [1000, 1000, 1000, 1000, 50_000]);
        let splits = split_events_for("ZZZ", &[&split_rec("ZZZ", 200, 1, 10)]).unwrap();
        let adjusted = split_adjust_record(&bar, &splits).unwrap();
        assert_eq!(field_of(&adjusted, "close"), 10_000);
        assert_eq!(field_of(&adjusted, "volume"), 5_000);
    }

    #[test]
    fn effective_date_boundary_is_strict() {
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 1)]).unwrap();
        // A bar ON the effective date (200) is already post-split -> unadjusted.
        let on_bar = daily_bar("AAPL", 200, [0, 0, 0, 4000, 10]);
        let on = split_adjust_record(&on_bar, &splits).unwrap();
        assert_eq!(field_of(&on, "close"), 4000);
        assert_eq!(field_of(&on, "volume"), 10);
        // A bar the second before (199) is pre-split -> adjusted.
        let before_bar = daily_bar("AAPL", 199, [0, 0, 0, 4000, 10]);
        let before = split_adjust_record(&before_bar, &splits).unwrap();
        assert_eq!(field_of(&before, "close"), 1000);
        assert_eq!(field_of(&before, "volume"), 40);
    }

    #[test]
    fn no_split_is_the_identity() {
        let bar = daily_bar("AAPL", 100, [9950, 10075, 9910, 10003, 100_001]);
        let adjusted = split_adjust_record(&bar, &[]).unwrap();
        assert_eq!(adjusted, bar, "split-adjusted of an unsplit series equals the raw series");
    }

    #[test]
    fn multi_split_composes_then_divides_once() {
        // 2-for-1 at ts 200 then 3-for-1 at ts 300 → cumulative 6-for-1 for a bar at ts 100.
        let bar = daily_bar("AAPL", 100, [600, 600, 600, 600, 60]);
        let records = [&split_rec("AAPL", 200, 2, 1), &split_rec("AAPL", 300, 3, 1)];
        let splits = split_events_for("AAPL", &records).unwrap();
        let adjusted = split_adjust_record(&bar, &splits).unwrap();
        // 600/6 = 100 (a single /6, not (600/2)/3 with intermediate rounding); volume 60*6 = 360.
        assert_eq!(field_of(&adjusted, "close"), 100);
        assert_eq!(field_of(&adjusted, "volume"), 360);
        // Compose-then-divide vs iterative on a value that rounds differently: 7 over a 6-for-1.
        let odd_bar = daily_bar("AAPL", 100, [0, 0, 0, 7, 0]);
        let odd = split_adjust_record(&odd_bar, &splits).unwrap();
        // 7/6 = 1.166... -> 1 (single division). Iterative (7/2=4 [3.5 -> even 4], 4/3=1) also 1 here,
        // but the single division is the contract; pin it.
        assert_eq!(field_of(&odd, "close"), 1);
    }

    #[test]
    fn round_half_to_even_breaks_ties_to_even() {
        // 2-for-1 split → factor 1/2. Odd-cent prices land on exact .5 ties.
        assert_eq!(div_round_half_even(5, 2), 2, "2.5 → even 2");
        assert_eq!(div_round_half_even(7, 2), 4, "3.5 → even 4");
        assert_eq!(div_round_half_even(9, 2), 4, "4.5 → even 4");
        assert_eq!(div_round_half_even(11, 2), 6, "5.5 → even 6");
        // Non-tie cases round to nearest.
        assert_eq!(div_round_half_even(8, 3), 3, "2.66… → 3");
        assert_eq!(div_round_half_even(7, 3), 2, "2.33… → 2");
        // Negative numerator (general correctness, even though prices are non-negative).
        assert_eq!(div_round_half_even(-5, 2), -2, "-2.5 → even -2");
    }

    #[test]
    fn non_ohlcv_fields_pass_through_unscaled() {
        // An equity bar carrying an extra non-OHLCV field: only the OHLC names scale; the rest is
        // verbatim (the field-name whitelist is defensive even within the equity-bar kind).
        let record = MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::DailyEquityBar,
                symbol: "AAPL".to_string(),
                resolution: "1d".to_string(),
                event_ts: 100,
                option_contract: None,
            },
            [field("adjustment_marker", 4000), field("close", 5000), field("volume", 80)],
        )
        .unwrap();
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 1)]).unwrap();
        let adjusted = split_adjust_record(&record, &splits).unwrap();
        // `close` scales (5000/4 = 1250), `volume` scales inverse (80*4 = 320); the marker is verbatim.
        assert_eq!(field_of(&adjusted, "close"), 1250);
        assert_eq!(field_of(&adjusted, "volume"), 320);
        assert_eq!(field_of(&adjusted, "adjustment_marker"), 4000);
    }

    #[test]
    fn rejects_non_equity_kinds_fail_closed() {
        // Split adjustment is an equity-bar operation. An option-chain snapshot (which has its own
        // `volume`) or a fundamental record must fail closed rather than be partially scaled/corrupted.
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 1)]).unwrap();
        let option = MarketDataRecord::new(
            NaturalKey {
                kind: DatasetKind::OptionChainSnapshot,
                symbol: "AAPL".to_string(),
                resolution: "chain".to_string(),
                event_ts: 100,
                option_contract: Some("AAPL  240119C00150000".to_string()),
            },
            [field("bid", 5000), field("last", 5100), field("volume", 40)],
        )
        .unwrap();
        assert!(matches!(
            split_adjust_record(&option, &splits),
            Err(NormalizationError::UnsupportedKind { kind: "option-chain" })
        ));
        // A split record itself cannot be split-adjusted either.
        let split = split_rec("AAPL", 100, 2, 1);
        assert!(matches!(
            split_adjust_record(&split, &splits),
            Err(NormalizationError::UnsupportedKind { .. })
        ));
    }

    #[test]
    fn non_positive_split_factor_fails_closed() {
        let zero_den = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 0)]);
        assert!(matches!(
            zero_den,
            Err(NormalizationError::NonPositiveSplitFactor { .. })
        ));
    }

    fn bad_split(effective_ts: i64, numerator: i64, denominator: i64) -> SplitEvent {
        SplitEvent { symbol: "AAPL".to_string(), effective_ts, numerator, denominator }
    }

    #[test]
    fn directly_constructed_invalid_split_event_fails_closed() {
        // SplitEvent is publicly constructible, bypassing split_events_for's validation. A direct
        // caller passing a non-positive factor (for THIS symbol) must get a typed NormalizationError,
        // NEVER a divide-by-zero panic (a zero numerator would be the price divisor) or a miscompute.
        let bar = daily_bar("AAPL", 100, [0, 0, 0, 4000, 10]);
        let bad_factors = [
            bad_split(200, 0, 1),
            bad_split(200, 4, 0),
            bad_split(200, -2, 1),
            bad_split(200, 4, -1),
            // Even a NON-applicable (effective_ts <= event_ts) malformed split for this symbol fails
            // closed, so the result is uniform across every bar in a series, not bar-date-dependent.
            bad_split(50, 0, 1),
        ];
        for bad in bad_factors {
            let label = format!("{bad:?}");
            assert!(
                matches!(
                    split_adjust_record(&bar, &[bad]),
                    Err(NormalizationError::NonPositiveSplitFactor { .. })
                ),
                "expected NonPositiveSplitFactor for {label}"
            );
        }
        // The slice entry point fails closed too.
        assert!(matches!(
            split_adjust_records(&[&bar], &[bad_split(200, 0, 1)]),
            Err(NormalizationError::NonPositiveSplitFactor { .. })
        ));
    }

    #[test]
    fn a_split_only_adjusts_its_own_symbol() {
        // CRITICAL safety invariant: a split carries its symbol and adjusts ONLY records of that
        // symbol. A mixed-symbol batch (or a store-wide split list) can never cross-contaminate -- an
        // AAPL 4-for-1 split must NOT touch an MSFT bar.
        let aapl = daily_bar("AAPL", 100, [0, 0, 0, 4000, 10]);
        let msft = daily_bar("MSFT", 100, [0, 0, 0, 8000, 20]);
        let aapl_split = SplitEvent { symbol: "AAPL".to_string(), effective_ts: 200, numerator: 4, denominator: 1 };
        let adjusted = split_adjust_records(&[&aapl, &msft], &[aapl_split]).unwrap();
        // AAPL is adjusted by its split (4000/4 = 1000, 10*4 = 40)...
        assert_eq!(field_of(&adjusted[0], "close"), 1000);
        assert_eq!(field_of(&adjusted[0], "volume"), 40);
        // ...but MSFT is UNTOUCHED (the AAPL split does not apply to it).
        assert_eq!(adjusted[1], msft);
        // A malformed AAPL split also does not poison an MSFT-only batch (wrong symbol -> skipped).
        let bad_aapl = SplitEvent { symbol: "AAPL".to_string(), effective_ts: 200, numerator: 0, denominator: 1 };
        assert_eq!(split_adjust_records(&[&msft], &[bad_aapl]).unwrap()[0], msft);
    }

    #[test]
    fn overflow_fails_closed_rather_than_wrapping() {
        // A bar near i64::MAX with a >1 price factor overflows the i64 result → fail closed.
        let bar = daily_bar("AAPL", 100, [0, 0, 0, i64::MAX, 0]);
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 1, 1000)]).unwrap();
        let result = split_adjust_record(&bar, &splits);
        assert!(matches!(result, Err(NormalizationError::Overflow { .. })));
    }

    #[test]
    fn split_events_ignores_other_symbols_and_kinds() {
        let records = [
            &split_rec("AAPL", 200, 4, 1),
            &split_rec("MSFT", 200, 2, 1),
            &daily_bar("AAPL", 100, [1, 1, 1, 1, 1]),
        ];
        let aapl = split_events_for("AAPL", &records).unwrap();
        assert_eq!(aapl.len(), 1, "only the AAPL split, not MSFT's and not the bar");
        assert_eq!(aapl[0].numerator, 4);
    }

    #[test]
    fn split_adjust_records_preserves_order() {
        let b1 = daily_bar("AAPL", 100, [0, 0, 0, 400, 10]);
        let b2 = daily_bar("AAPL", 150, [0, 0, 0, 800, 20]);
        let splits = split_events_for("AAPL", &[&split_rec("AAPL", 200, 4, 1)]).unwrap();
        let adjusted = split_adjust_records(&[&b1, &b2], &splits).unwrap();
        assert_eq!(adjusted.len(), 2);
        assert_eq!(field_of(&adjusted[0], "close"), 100);
        assert_eq!(field_of(&adjusted[1], "close"), 200);
    }

    // ----------------------------------------------------------------------- //
    // Generative property test (L2-style). `atp-data` is a zero-dependency crate, so rather than pull
    // in proptest/quickcheck this drives the split math over thousands of deterministically-generated
    // (seeded) bar + split sequences and asserts the money-math INVARIANTS Codex flagged: identity with
    // no applicable split, symbol isolation, non-positive-factor rejection, compose-then-divide
    // equivalence (multi-split == one composed split) and order-independence (no intermediate rounding
    // drift), and a fail-closed-not-panic guarantee across the whole input space.
    // ----------------------------------------------------------------------- //

    /// A tiny deterministic PRNG (SplitMix64) -- reproducible generated inputs without an external dep.
    struct Rng(u64);
    impl Rng {
        fn next_u64(&mut self) -> u64 {
            self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut z = self.0;
            z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            z ^ (z >> 31)
        }
        /// An inclusive integer in `[lo, hi]`.
        fn range(&mut self, lo: i64, hi: i64) -> i64 {
            lo + (self.next_u64() % ((hi - lo + 1) as u64)) as i64
        }
    }

    fn close_of(bar: &MarketDataRecord) -> i64 {
        field_of(bar, "close")
    }

    #[test]
    fn property_split_adjustment_invariants() {
        let symbols = ["AAPL", "MSFT", "ZZZ"];
        let mut rng = Rng(0x0DD_F00D_CAFE);
        for _ in 0..5000 {
            let sym = symbols[rng.range(0, 2) as usize];
            let event_ts = rng.range(1, 200);
            // Bounded so a value scaled by a split ratio stays well inside i64 (overflow fail-closed
            // is its own unit test).
            let ohlcv = [
                rng.range(0, 5_000_000),
                rng.range(0, 5_000_000),
                rng.range(0, 5_000_000),
                rng.range(1, 5_000_000),
                rng.range(0, 5_000_000),
            ];
            let bar = daily_bar(sym, event_ts, ohlcv);

            // A random set of 0..=3 splits across symbols, including non-positive factors.
            let mut splits = Vec::new();
            let mut has_bad_matching = false;
            for _ in 0..rng.range(0, 3) {
                let s_sym = symbols[rng.range(0, 2) as usize];
                let numerator = rng.range(-2, 8);
                let denominator = rng.range(-2, 8);
                if s_sym == sym && (numerator <= 0 || denominator <= 0) {
                    has_bad_matching = true;
                }
                splits.push(SplitEvent {
                    symbol: s_sym.to_string(),
                    effective_ts: rng.range(1, 200),
                    numerator,
                    denominator,
                });
            }

            let result = split_adjust_record(&bar, &splits);

            // INVARIANT: a non-positive factor for THIS symbol fails closed (never a panic / miscompute).
            if has_bad_matching {
                assert!(
                    matches!(result, Err(NormalizationError::NonPositiveSplitFactor { .. })),
                    "non-positive matching split must fail closed: {splits:?}"
                );
                continue;
            }

            let adjusted = result.expect("well-formed splits adjust without error");

            // INVARIANT: only this symbol's splits effective AFTER the bar apply. Compute the cumulative
            // factor from the APPLICABLE splits and require identity when none apply (symbol isolation +
            // non-applicable-date are the no-op cases).
            let applicable: Vec<&SplitEvent> = splits
                .iter()
                .filter(|s| s.symbol == sym && s.effective_ts > event_ts)
                .collect();
            if applicable.is_empty() {
                assert_eq!(adjusted, bar, "no applicable split must be the identity: {splits:?}");
            }

            // INVARIANT (compose-then-divide + order-independence + no intermediate rounding drift):
            // applying all applicable splits equals applying ONE split whose ratio is their product, in
            // any order. Build that single composed split and a shuffled split list; both must match.
            let mut cum_num: i64 = 1;
            let mut cum_den: i64 = 1;
            for s in &applicable {
                cum_num *= s.numerator; // bounded factors (<=7 each, <=3 splits) stay in i64 here
                cum_den *= s.denominator;
            }
            let composed = SplitEvent {
                symbol: sym.to_string(),
                effective_ts: event_ts + 1, // strictly after the bar, so it applies
                numerator: cum_num,
                denominator: cum_den,
            };
            let via_composed = split_adjust_record(&bar, &[composed]).unwrap();
            assert_eq!(
                adjusted, via_composed,
                "multi-split must equal one composed split (compose-then-divide, no rounding drift): {splits:?}"
            );

            // Order-independence: reversing the split list yields the identical result.
            let mut reversed = splits.clone();
            reversed.reverse();
            assert_eq!(
                split_adjust_record(&bar, &reversed).unwrap(),
                adjusted,
                "split application must be order-independent: {splits:?}"
            );

            // A split for a DIFFERENT symbol never changes THIS bar (symbol isolation, isolated).
            let foreign = SplitEvent {
                symbol: "NOSUCH".to_string(),
                effective_ts: 1,
                numerator: 5,
                denominator: 1,
            };
            let mut with_foreign = splits.clone();
            with_foreign.push(foreign);
            assert_eq!(
                split_adjust_record(&bar, &with_foreign).unwrap(),
                adjusted,
                "a foreign-symbol split must not change this bar: {splits:?}"
            );

            // Exact rounding: every OHLC field equals round-half-to-even(raw * cum_den / cum_num).
            for name in ["open", "high", "low", "close"] {
                let raw = field_of(&bar, name) as i128;
                let expected =
                    div_round_half_even(raw * cum_den as i128, cum_num as i128) as i64;
                assert_eq!(field_of(&adjusted, name), expected, "OHLC rounding for {name}: {splits:?}");
            }
            // Volume takes the inverse factor.
            let raw_vol = field_of(&bar, "volume") as i128;
            let expected_vol = div_round_half_even(raw_vol * cum_num as i128, cum_den as i128) as i64;
            assert_eq!(field_of(&adjusted, "volume"), expected_vol, "volume rounding: {splits:?}");
            let _ = close_of(&adjusted);
        }
    }
}
