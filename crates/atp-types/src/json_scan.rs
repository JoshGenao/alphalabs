//! # A minimal, fail-closed reader for the ONE field a persisted JSON record must be trusted on
//!
//! SRS-DATA-015 (SyRS SYS-66) requires every persisted entity to record a schema version and every
//! reader to refuse a version it cannot understand. Several of the repo's persisted formats are
//! JSON-object-per-line logs, and their readers must answer exactly one question about a raw line —
//! *what schema version does this record declare?* — before deciding whether they may interpret the
//! rest of it.
//!
//! This module answers that question for the callers that must answer it identically: the
//! SRS-DATA-015 inspection CLI (`data015_schema_cli`) and the SRS-RESV-003 trigger-log reader
//! (`resv003_hot_swap_trigger_cli`). It lives in `atp-types` — a leaf crate both already depend on —
//! because the same parser drifting apart in two places is how the two of them disagreed about which
//! records were readable in the first place.
//!
//! It also answers the two adjacent questions a *fail-closed* reader of a persisted record needs,
//! under the identical rules: **which keys does this object declare** ([`top_level_json_keys`]) and
//! **is this value a real JSON boolean** ([`parse_strict_bool`]). Both exist for the SRS-RESV-003
//! durable trigger configuration, where a key the reader does not recognise, or a flag it coerced
//! rather than read, would decide whether an automatic Hot-Swap may fire.
//!
//! ## Why hand-written
//! The workspace carries no `serde`, and pulling a JSON dependency into the core runtime for a
//! single field lookup is not a trade this codebase makes (AGENTS.md: no new dependency without
//! confirming scope). What is required here is much narrower than a JSON parser: recognise a flat
//! object, find one key, and be *certain* about malformed input.
//!
//! ## The three properties that matter, and why
//! 1. **The whole object is validated before any value is returned.** Returning a version the moment
//!    the key matched would accept `{"schema_version":1,` — a torn line whose remaining fields are
//!    missing — as a well-formed record declaring v1. A reader would then interpret bytes that are
//!    not there.
//! 2. **A key is only a key.** `"schema_version"` appearing inside a *string value* (an
//!    operator-supplied `rationale`, say) or inside a nested object is never read as the record's
//!    own version, in either direction: it can neither spoof a version onto a legacy record nor mask
//!    the real one.
//! 3. **Ambiguity is refused, not resolved.** A duplicated key, trailing bytes after the closing
//!    brace, or any structural malformation is [`JsonScanError`] — never a best guess. A record that
//!    cannot say unambiguously which layout it is in has not said.
//!
//! Absence, by contrast, is *not* an error: a record with no version key predates SRS-DATA-015 and
//! its reader is entitled to treat it as the format's floor version. Distinguishing "absent" from
//! "malformed" is the entire point — collapsing them is what makes a version gate fail open.

use std::fmt;

/// The line is not a single well-formed flat JSON object, or it declares its key ambiguously.
///
/// Deliberately opaque: every caller's response is the same — refuse the record — so there is
/// nothing for a caller to branch on, and a richer error would only invite one to try.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct JsonScanError;

impl fmt::Display for JsonScanError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("line is not a single well-formed JSON object")
    }
}

impl std::error::Error for JsonScanError {}

/// The raw, unparsed text of top-level `key`'s value in the JSON object `line`.
///
/// * `Ok(Some(raw))` — the key is present exactly once; `raw` is its value text, trimmed.
/// * `Ok(None)` — the object is well-formed and simply does not carry the key.
/// * `Err(JsonScanError)` — the line is not one well-formed flat JSON object, has trailing bytes
///   after it, or declares `key` more than once.
///
/// The ENTIRE object is walked before a match is returned, so malformation anywhere in the line
/// disqualifies it even when the key itself parsed cleanly earlier.
pub fn top_level_json_field<'a>(
    line: &'a str,
    key: &str,
) -> Result<Option<&'a str>, JsonScanError> {
    let bytes = line.as_bytes();
    let mut i = skip_ws(bytes, 0);
    if bytes.get(i) != Some(&b'{') {
        return Err(JsonScanError);
    }
    i = skip_ws(bytes, i + 1);

    let mut found: Option<&str> = None;
    if bytes.get(i) == Some(&b'}') {
        i += 1;
    } else {
        loop {
            i = skip_ws(bytes, i);
            let (name, after_key) = read_json_string(line, i)?;
            i = skip_ws(bytes, after_key);
            if bytes.get(i) != Some(&b':') {
                return Err(JsonScanError);
            }
            i = skip_ws(bytes, i + 1);
            let value_start = i;
            i = skip_json_value(line, i)?;
            if name == key {
                if found.is_some() {
                    // Two declarations of the same key: the record does not unambiguously state its
                    // layout, so there is no honest value to return.
                    return Err(JsonScanError);
                }
                found = Some(line[value_start..i].trim());
            }
            i = skip_ws(bytes, i);
            match bytes.get(i) {
                Some(b',') => i += 1,
                Some(b'}') => {
                    i += 1;
                    break;
                }
                _ => return Err(JsonScanError),
            }
        }
    }

    // Anything but whitespace after the object means this is not the single record it claims to be.
    if skip_ws(bytes, i) != bytes.len() {
        return Err(JsonScanError);
    }
    Ok(found)
}

/// Every top-level key the JSON object `line` declares, in the order written.
///
/// The companion to [`top_level_json_field`] for readers that must reject an *unknown* key rather
/// than ignore it. Looking fields up one at a time can only ever confirm what a reader expected to
/// find; it is structurally blind to a key the writer added, misspelled, or renamed. For a record
/// whose fields are load-bearing (a safety configuration, say) that blindness is a fail-open: a
/// `drawdown_demotion_enabld` typo reads as "the operator did not enable it", and a field a newer
/// build made meaningful is silently dropped by an older reader that still parses the rest cleanly.
///
/// Same guarantees as [`top_level_json_field`]: the entire object is walked before any key is
/// returned, a duplicate key is [`JsonScanError`] rather than a resolved last-one-wins, and a key
/// occurring inside a string value or a nested object is never mistaken for a top-level one.
pub fn top_level_json_keys(line: &str) -> Result<Vec<&str>, JsonScanError> {
    let bytes = line.as_bytes();
    let mut i = skip_ws(bytes, 0);
    if bytes.get(i) != Some(&b'{') {
        return Err(JsonScanError);
    }
    i = skip_ws(bytes, i + 1);

    let mut keys: Vec<&str> = Vec::new();
    if bytes.get(i) == Some(&b'}') {
        i += 1;
    } else {
        loop {
            i = skip_ws(bytes, i);
            let (name, after_key) = read_json_string(line, i)?;
            i = skip_ws(bytes, after_key);
            if bytes.get(i) != Some(&b':') {
                return Err(JsonScanError);
            }
            i = skip_ws(bytes, i + 1);
            i = skip_json_value(line, i)?;
            if keys.contains(&name) {
                // Ambiguity is refused, not resolved — the same rule `top_level_json_field`
                // applies. An object that declares a key twice has not stated its layout.
                return Err(JsonScanError);
            }
            keys.push(name);
            i = skip_ws(bytes, i);
            match bytes.get(i) {
                Some(b',') => i += 1,
                Some(b'}') => {
                    i += 1;
                    break;
                }
                _ => return Err(JsonScanError),
            }
        }
    }

    if skip_ws(bytes, i) != bytes.len() {
        return Err(JsonScanError);
    }
    Ok(keys)
}

/// A JSON boolean, and nothing else: rejects `"true"` (a quoted string), `1`, `null`, and any
/// trailing junk. A configuration flag that decides whether an automatic action may fire must come
/// from a literal the writer actually wrote, never from a coercion the reader invented.
pub fn parse_strict_bool(raw: &str) -> Option<bool> {
    match raw.trim() {
        "true" => Some(true),
        "false" => Some(false),
        _ => None,
    }
}

/// A JSON integer, and nothing else: rejects floats (`1.5`), quoted numbers (`"1"`), `true`/`null`,
/// a leading `+`, and any trailing junk — each of which would otherwise be coerced into a version a
/// reader then acts on.
pub fn parse_strict_i64(raw: &str) -> Option<i64> {
    let trimmed = raw.trim();
    let digits = trimmed.strip_prefix('-').unwrap_or(trimmed);
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    trimmed.parse().ok()
}

/// The contents of a raw JSON string token (`"…"`), or `None` when `raw` is any other JSON value.
///
/// The contents are returned still-escaped: callers here only ask "is this a string, and is it
/// non-empty?", and un-escaping would invent a second, subtly-different notion of the value than
/// the writer used.
pub fn json_string_value(raw: &str) -> Option<&str> {
    let trimmed = raw.trim();
    let inner = trimmed.strip_prefix('"')?.strip_suffix('"')?;
    // `"` alone strips to nothing on the prefix and then fails the suffix strip, so a lone quote
    // cannot masquerade as an empty string.
    Some(inner)
}

fn skip_ws(bytes: &[u8], mut i: usize) -> usize {
    while i < bytes.len() && bytes[i].is_ascii_whitespace() {
        i += 1;
    }
    i
}

/// Read the JSON string starting at `i`, returning its raw contents and the index just past it.
///
/// Escapes are VALIDATED, not merely skipped: `\q` is not a JSON escape, and a lone trailing `\`
/// must not be allowed to consume the closing quote and swallow the rest of the line into the
/// string. Both would let a malformed record read as well-formed.
fn read_json_string(line: &str, i: usize) -> Result<(&str, usize), JsonScanError> {
    let bytes = line.as_bytes();
    if bytes.get(i) != Some(&b'"') {
        return Err(JsonScanError);
    }
    let mut j = i + 1;
    while j < bytes.len() {
        match bytes[j] {
            b'\\' => {
                match bytes.get(j + 1) {
                    Some(b'"' | b'\\' | b'/' | b'b' | b'f' | b'n' | b'r' | b't') => j += 2,
                    Some(b'u') => {
                        let hex = bytes.get(j + 2..j + 6).ok_or(JsonScanError)?;
                        if !hex.iter().all(|b| b.is_ascii_hexdigit()) {
                            return Err(JsonScanError);
                        }
                        j += 6;
                    }
                    // An unknown escape, or a backslash at end of input.
                    _ => return Err(JsonScanError),
                }
            }
            b'"' => return Ok((&line[i + 1..j], j + 1)),
            // A raw control character is not legal inside a JSON string.
            0x00..=0x1f => return Err(JsonScanError),
            _ => j += 1,
        }
    }
    Err(JsonScanError)
}

/// Whether `token` is a legal JSON scalar: `true`, `false`, `null`, or a number.
///
/// Without this, `skip_json_value` would accept any run of bytes between structural characters as a
/// value — so `{"a":tru,"schema_version":1}` would scan as a well-formed object. The callers only
/// strictly parse the ONE field they care about, so every other field's well-formedness is exactly
/// what this check is for.
fn is_json_scalar(token: &str) -> bool {
    match token {
        "true" | "false" | "null" => return true,
        _ => {}
    }
    // JSON number: -?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?
    let rest = token.strip_prefix('-').unwrap_or(token);
    let (int_part, rest) = split_digits(rest);
    if int_part.is_empty() || (int_part.len() > 1 && int_part.starts_with('0')) {
        return false;
    }
    let rest = match rest.strip_prefix('.') {
        None => rest,
        Some(after_dot) => {
            let (frac, rest) = split_digits(after_dot);
            if frac.is_empty() {
                return false;
            }
            rest
        }
    };
    let rest = match rest.strip_prefix(['e', 'E']) {
        None => return rest.is_empty(),
        Some(after_e) => after_e.strip_prefix(['+', '-']).unwrap_or(after_e),
    };
    let (exp, rest) = split_digits(rest);
    !exp.is_empty() && rest.is_empty()
}

/// Split off the leading ASCII digits.
fn split_digits(text: &str) -> (&str, &str) {
    let end = text
        .find(|c: char| !c.is_ascii_digit())
        .unwrap_or(text.len());
    text.split_at(end)
}

/// Skip one JSON value starting at `i`, returning the index just past it. Nested containers are
/// skipped whole (tracking string state), so a nested key is never seen as a top-level one.
fn skip_json_value(line: &str, i: usize) -> Result<usize, JsonScanError> {
    skip_json_value_at(line, i, 0)
}

/// Maximum container nesting this scanner will walk.
///
/// The records it reads are flat or shallow (the deepest is the kill-switch activation record's
/// `report`/`response` sub-objects), so anything near this bound is already not a record this build
/// wrote. The cap exists so a pathologically-nested line — the cheapest possible hostile input to a
/// recursive parser — fails closed instead of exhausting the stack.
const MAX_NESTING_DEPTH: usize = 32;

/// Skip one JSON value, validating nested containers RECURSIVELY.
///
/// Balancing delimiters is not enough: `{"payload":{bad}}` balances, and an earlier version of this
/// accepted it, so a corrupted record whose *required* top-level fields happened to be intact could
/// still be counted as durable audit evidence. Every nested value must itself parse.
fn skip_json_value_at(line: &str, i: usize, depth: usize) -> Result<usize, JsonScanError> {
    if depth > MAX_NESTING_DEPTH {
        return Err(JsonScanError);
    }
    let bytes = line.as_bytes();
    match bytes.get(i) {
        None => Err(JsonScanError),
        Some(b'"') => Ok(read_json_string(line, i)?.1),
        Some(b'{') => skip_json_object(line, i, depth),
        Some(b'[') => skip_json_array(line, i, depth),
        // A scalar runs to the next structural character, and must actually BE a JSON scalar —
        // otherwise any run of bytes (`tru`, `1.2.3`) would pass as a value and a corrupt record
        // would scan as well-formed. An empty span (e.g. `{"k":}`) is rejected the same way.
        Some(_) => {
            let mut j = i;
            while j < bytes.len() && !matches!(bytes[j], b',' | b'}' | b']') {
                j += 1;
            }
            let token = line[i..j].trim();
            if token.is_empty() || !is_json_scalar(token) {
                return Err(JsonScanError);
            }
            Ok(j)
        }
    }
}

/// Validate a whole `{...}` object, returning the index just past its closing brace.
fn skip_json_object(line: &str, i: usize, depth: usize) -> Result<usize, JsonScanError> {
    let bytes = line.as_bytes();
    let mut j = skip_ws(bytes, i + 1);
    if bytes.get(j) == Some(&b'}') {
        return Ok(j + 1);
    }
    loop {
        j = skip_ws(bytes, j);
        let (_, after_key) = read_json_string(line, j)?;
        j = skip_ws(bytes, after_key);
        if bytes.get(j) != Some(&b':') {
            return Err(JsonScanError);
        }
        j = skip_ws(bytes, j + 1);
        j = skip_json_value_at(line, j, depth + 1)?;
        j = skip_ws(bytes, j);
        match bytes.get(j) {
            Some(b',') => j += 1,
            Some(b'}') => return Ok(j + 1),
            _ => return Err(JsonScanError),
        }
    }
}

/// Validate a whole `[...]` array, returning the index just past its closing bracket.
fn skip_json_array(line: &str, i: usize, depth: usize) -> Result<usize, JsonScanError> {
    let bytes = line.as_bytes();
    let mut j = skip_ws(bytes, i + 1);
    if bytes.get(j) == Some(&b']') {
        return Ok(j + 1);
    }
    loop {
        j = skip_ws(bytes, j);
        j = skip_json_value_at(line, j, depth + 1)?;
        j = skip_ws(bytes, j);
        match bytes.get(j) {
            Some(b',') => j += 1,
            Some(b']') => return Ok(j + 1),
            _ => return Err(JsonScanError),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KEY: &str = "schema_version";

    #[test]
    fn finds_the_key_wherever_the_writer_placed_it() {
        for line in [
            r#"{"schema_version":1,"kind":"X"}"#,
            r#"{"kind":"X","schema_version":1,"rationale":"y"}"#,
            r#"{"kind":"X","schema_version":1}"#,
            "{ \"kind\" : \"X\" , \"schema_version\" : 1 }",
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Ok(Some("1")),
                "failed on {line}"
            );
        }
    }

    #[test]
    fn a_well_formed_object_without_the_key_is_absent_not_an_error() {
        // The legacy case. It MUST be distinguishable from malformation: absent means "read at the
        // floor version", malformed means "refuse".
        for line in [r#"{"kind":"X"}"#, "{}", "  {}  "] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Ok(None),
                "failed on {line}"
            );
        }
    }

    #[test]
    fn malformation_after_the_key_still_disqualifies_the_line() {
        // The round-3 adversarial finding: returning as soon as the key matched accepted a torn
        // line whose remaining fields never arrived.
        for line in [
            r#"{"schema_version":1,"#,
            r#"{"schema_version":1,"kind""#,
            r#"{"schema_version":1,"kind":}"#,
            r#"{"schema_version":1"#,
            r#"{"schema_version":1}trailing"#,
            r#"{"schema_version":1} {"schema_version":2}"#,
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line}"
            );
        }
    }

    #[test]
    fn a_duplicated_key_is_refused_rather_than_resolved() {
        assert_eq!(
            top_level_json_field(r#"{"schema_version":1,"schema_version":2}"#, KEY),
            Err(JsonScanError)
        );
    }

    #[test]
    fn a_key_name_inside_a_string_value_is_not_a_key() {
        // Neither direction may leak: the string must not spoof a version onto a legacy record...
        assert_eq!(
            top_level_json_field(r#"{"kind":"X","note":"\"schema_version\":99 here"}"#, KEY),
            Ok(None)
        );
        // ...nor mask the real one when both are present.
        assert_eq!(
            top_level_json_field(
                r#"{"note":"\"schema_version\":99 here","schema_version":1}"#,
                KEY
            ),
            Ok(Some("1"))
        );
    }

    #[test]
    fn a_nested_key_is_not_the_records_own() {
        assert_eq!(
            top_level_json_field(r#"{"kind":"X","payload":{"schema_version":99}}"#, KEY),
            Ok(None)
        );
        assert_eq!(
            top_level_json_field(
                r#"{"payload":{"schema_version":99},"schema_version":1}"#,
                KEY
            ),
            Ok(Some("1"))
        );
        assert_eq!(
            top_level_json_field(
                r#"{"list":[{"schema_version":99}],"schema_version":1}"#,
                KEY
            ),
            Ok(Some("1"))
        );
    }

    #[test]
    fn a_non_object_line_is_refused() {
        for line in [
            "",
            "   ",
            "not json at all",
            "[1,2,3]",
            "\"just a string\"",
            "42",
            "{",
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line:?}"
            );
        }
    }

    #[test]
    fn strict_integer_parsing_rejects_everything_that_is_not_one() {
        assert_eq!(parse_strict_i64("1"), Some(1));
        assert_eq!(parse_strict_i64(" 42 "), Some(42));
        assert_eq!(parse_strict_i64("-1"), Some(-1));
        for bad in [
            "1.5", "\"1\"", "true", "null", "+1", "1x", "", "-", " ", "1 2",
        ] {
            assert_eq!(parse_strict_i64(bad), None, "must reject {bad:?}");
        }
    }

    #[test]
    fn mismatched_container_delimiters_are_refused() {
        // Adversarial-review round 5: a depth COUNTER treats `}` and `]` as interchangeable, so a
        // corrupt record balances and scans as well-formed. The stack must match types.
        for line in [
            r#"{"a":[},"schema_version":1}"#,
            r#"{"a":{],"schema_version":1}"#,
            r#"{"a":[[1,2},"schema_version":1}"#,
            r#"{"a":{"b":[}},"schema_version":1}"#,
            r#"{"a":[1,2],"schema_version":1]"#,
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line}"
            );
        }
        // ...while correctly-nested containers still scan.
        assert_eq!(
            top_level_json_field(r#"{"a":[1,{"b":[2]}],"schema_version":1}"#, KEY),
            Ok(Some("1"))
        );
    }

    #[test]
    fn invalid_string_escapes_are_refused() {
        for line in [
            r#"{"a":"bad \q escape","schema_version":1}"#,
            r#"{"a":"trailing backslash \"#,
            r#"{"a":"short unicode \u12","schema_version":1}"#,
            r#"{"a":"bad unicode \uZZZZ","schema_version":1}"#,
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line}"
            );
        }
        // ...while every legal escape still scans.
        assert_eq!(
            top_level_json_field(
                r#"{"a":"\" \\ \/ \b \f \n \r \t \u00e9","schema_version":1}"#,
                KEY
            ),
            Ok(Some("1"))
        );
    }

    #[test]
    fn non_scalar_garbage_in_another_field_disqualifies_the_line() {
        // The callers strictly parse only the ONE field they need, so every OTHER field's
        // well-formedness is exactly what this check exists to establish.
        for line in [
            r#"{"a":tru,"schema_version":1}"#,
            r#"{"a":1.2.3,"schema_version":1}"#,
            r#"{"a":01,"schema_version":1}"#,
            r#"{"a":+1,"schema_version":1}"#,
            r#"{"a":1e,"schema_version":1}"#,
            r#"{"a":.5,"schema_version":1}"#,
            r#"{"a":,"schema_version":1}"#,
            r#"{"a":undefined,"schema_version":1}"#,
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line}"
            );
        }
        // ...while every legal scalar form still scans.
        for line in [
            r#"{"a":true,"schema_version":1}"#,
            r#"{"a":false,"schema_version":1}"#,
            r#"{"a":null,"schema_version":1}"#,
            r#"{"a":0,"schema_version":1}"#,
            r#"{"a":-1,"schema_version":1}"#,
            r#"{"a":1.5,"schema_version":1}"#,
            r#"{"a":1e10,"schema_version":1}"#,
            r#"{"a":-2.5E-3,"schema_version":1}"#,
        ] {
            assert_eq!(top_level_json_field(line, KEY), Ok(Some("1")), "on {line}");
        }
    }

    #[test]
    fn a_raw_control_character_inside_a_string_is_refused() {
        assert_eq!(
            top_level_json_field("{\"a\":\"raw\ttab\",\"schema_version\":1}", KEY),
            Err(JsonScanError)
        );
    }

    #[test]
    fn malformed_nested_containers_are_refused() {
        // Adversarial-review round 7: balancing delimiters is not validating them. A record whose
        // REQUIRED top-level fields are intact could still carry a corrupt sub-object, and would
        // have been counted as durable evidence.
        for line in [
            r#"{"schema_version":1,"payload":{bad}}"#,
            r#"{"schema_version":1,"payload":{"k"}}"#,
            r#"{"schema_version":1,"payload":{"k":}}"#,
            r#"{"schema_version":1,"payload":{"k":tru}}"#,
            r#"{"schema_version":1,"payload":{"k":1,}}"#,
            r#"{"schema_version":1,"list":[1,]}"#,
            r#"{"schema_version":1,"list":[1 2]}"#,
            r#"{"schema_version":1,"list":[nope]}"#,
            r#"{"schema_version":1,"deep":{"a":{"b":{bad}}}}"#,
        ] {
            assert_eq!(
                top_level_json_field(line, KEY),
                Err(JsonScanError),
                "must reject {line}"
            );
        }
        // ...while well-formed nesting still scans (the kill-switch activation record is nested).
        for line in [
            r#"{"schema_version":1,"payload":{}}"#,
            r#"{"schema_version":1,"list":[]}"#,
            r#"{"schema_version":1,"report":{"ok":true,"n":2},"response":{"accepted":true}}"#,
            r#"{"schema_version":1,"deep":{"a":{"b":[1,{"c":null}]}}}"#,
        ] {
            assert_eq!(top_level_json_field(line, KEY), Ok(Some("1")), "on {line}");
        }
    }

    #[test]
    fn pathological_nesting_fails_closed_instead_of_exhausting_the_stack() {
        // The cheapest hostile input to a recursive parser. It must be refused, not crash the
        // process that is trying to decide whether a file is safe to read.
        let deep = format!(
            "{{\"schema_version\":1,\"deep\":{}{}}}",
            "[".repeat(MAX_NESTING_DEPTH + 10),
            "]".repeat(MAX_NESTING_DEPTH + 10)
        );
        assert_eq!(top_level_json_field(&deep, KEY), Err(JsonScanError));

        // ...while nesting within the cap still scans.
        let shallow = format!(
            "{{\"schema_version\":1,\"deep\":{}1{}}}",
            "[".repeat(4),
            "]".repeat(4)
        );
        assert_eq!(top_level_json_field(&shallow, KEY), Ok(Some("1")));
    }

    #[test]
    fn json_string_value_distinguishes_strings_from_every_other_value() {
        assert_eq!(json_string_value(r#""hello""#), Some("hello"));
        assert_eq!(json_string_value(r#""""#), Some(""));
        assert_eq!(json_string_value(r#"  "padded"  "#), Some("padded"));
        for not_a_string in ["1", "true", "null", "[1]", "{}", r#"""#, "", "unquoted"] {
            assert_eq!(
                json_string_value(not_a_string),
                None,
                "must reject {not_a_string:?}"
            );
        }
    }

    #[test]
    fn escaped_backslash_before_a_quote_does_not_swallow_the_string_end() {
        // `"a\\"` ends at that quote — the backslash is escaped, not the quote.
        assert_eq!(
            top_level_json_field(r#"{"note":"a\\","schema_version":1}"#, KEY),
            Ok(Some("1"))
        );
    }
}
