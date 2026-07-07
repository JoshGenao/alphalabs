//! SRS-ORCH-005 / SyRS SYS-80 / NFR-S2 — the rollback operator CLI must FAIL
//! CLOSED at the PROCESS level: an unconfirmed live rollback, a mistargeted
//! rollback, a degraded live probe, a tampered state snapshot, and malformed
//! flags all exit NONZERO with the state file untouched, so shell automation
//! (and the python/atp_orchestration handler that shells this bin) can never
//! treat a refused rollback as a success.
//!
//! L4 boundary test: spawns the real `orch005_rollback_cli` binary against a
//! per-test state file under `CARGO_TARGET_TMPDIR`, and proves the
//! record -> record -> rollback walk round-trips durably across invocations.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

const BIN: &str = env!("CARGO_BIN_EXE_orch005_rollback_cli");

const HASH_V1: &str = "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const HASH_V2: &str = "sha256:2222222222222222222222222222222222222222222222222222222222222222";

fn state_path(name: &str) -> PathBuf {
    let path = Path::new(env!("CARGO_TARGET_TMPDIR")).join(name);
    let _ = fs::remove_file(&path);
    path
}

fn run(args: &[&str]) -> (bool, String, String) {
    let output = Command::new(BIN)
        .args(args)
        .output()
        .expect("run orch005_rollback_cli");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stdout).to_string(),
        String::from_utf8_lossy(&output.stderr).to_string(),
    )
}

fn seed_two_versions(state: &Path) {
    let (ok, _, err) = run(&[
        "record",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--hash",
        HASH_V1,
        "--observed-at",
        "100",
    ]);
    assert!(ok, "seed v1 failed: {err}");
    let (ok, out, err) = run(&[
        "record",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--hash",
        HASH_V2,
        "--observed-at",
        "200",
    ]);
    assert!(ok, "seed v2 failed: {err}");
    assert!(
        out.contains("retained-previous:true"),
        "SYS-80 retention proof line missing: {out}"
    );
}

#[test]
fn orch_5_cli_unconfirmed_live_rollback_exits_nonzero_and_leaves_state_unchanged() {
    let state = state_path("orch005-unconfirmed.state");
    seed_two_versions(&state);
    let before = fs::read_to_string(&state).expect("state exists");

    let (ok, _, err) = run(&[
        "rollback",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
        "--live",
        "alpha-1",
    ]);
    assert!(!ok, "an unconfirmed LIVE rollback must exit nonzero");
    assert!(
        err.contains("NFR-S2") || err.contains("confirmation"),
        "the refusal must name the confirmation control: {err}"
    );
    assert_eq!(
        fs::read_to_string(&state).expect("state exists"),
        before,
        "a refused rollback must leave the state snapshot byte-identical"
    );
}

#[test]
fn orch_5_cli_list_prints_the_sorted_inventory_and_fails_closed_on_a_missing_snapshot() {
    // The SYS-41 / SRS-UI-002 inventory read: every recorded strategy, id-sorted,
    // as indexed proof lines; a missing snapshot is data absence (fail closed),
    // never an empty inventory.
    let state = state_path("orch005-list.state");
    seed_two_versions(&state);
    let (ok, _, err) = run(&[
        "record",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "beta-9",
        "--hash",
        HASH_V1,
        "--observed-at",
        "300",
    ]);
    assert!(ok, "seed beta-9 failed: {err}");

    let (ok, out, err) = run(&["list", "--state", state.to_str().unwrap()]);
    assert!(ok, "list failed: {err}");
    assert!(out.contains("strategy_count:2"), "{out}");
    assert!(out.contains("strategy.0.id:alpha-1"), "{out}");
    assert!(
        out.contains(&format!("strategy.0.current:{HASH_V2}@200")),
        "{out}"
    );
    assert!(
        out.contains(&format!("strategy.0.previous:{HASH_V1}@100")),
        "{out}"
    );
    assert!(out.contains("strategy.1.id:beta-9"), "{out}");
    assert!(out.contains("strategy.1.previous:-"), "{out}");

    // A missing snapshot fails closed; `list` also refuses a stray --strategy.
    let missing = state_path("orch005-list-missing.state");
    let (ok, _, _) = run(&["list", "--state", missing.to_str().unwrap()]);
    assert!(
        !ok,
        "a missing snapshot must fail closed, never read as an empty inventory"
    );
    let (ok, _, _) = run(&[
        "list",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
    ]);
    assert!(!ok, "`list` must refuse --strategy (use `show`)");
}

#[test]
fn orch_5_cli_confirmed_live_rollback_round_trips_durably() {
    let state = state_path("orch005-confirmed.state");
    seed_two_versions(&state);

    let (ok, out, err) = run(&[
        "rollback",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
        "--live",
        "alpha-1",
        "--acknowledge",
        "operator confirmed rollback of alpha-1 via CLI",
        "--observed-at",
        "300",
    ]);
    assert!(ok, "confirmed live rollback failed: {err}");
    assert!(
        out.contains(&format!("rolled-back-from:{HASH_V2}")),
        "{out}"
    );
    assert!(
        out.contains(&format!("rolled-back-to:{HASH_V1}@300")),
        "{out}"
    );
    assert!(out.contains("was-live:true"), "{out}");

    // A FRESH invocation reads the persisted swap (durability across processes).
    let (ok, shown, _) = run(&[
        "show",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
    ]);
    assert!(ok);
    assert!(shown.contains(&format!("current:{HASH_V1}@300")), "{shown}");
    assert!(
        shown.contains(&format!("previous:{HASH_V2}@200")),
        "{shown}"
    );
}

#[test]
fn orch_5_cli_mistargeted_rollback_exits_nonzero_naming_the_retained_hash() {
    let state = state_path("orch005-mistarget.state");
    seed_two_versions(&state);
    // Naming the CURRENT hash is not a rollback — the target must be the
    // retained previous version.
    let (ok, _, err) = run(&[
        "rollback",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V2,
    ]);
    assert!(!ok, "a mistargeted rollback must exit nonzero");
    assert!(
        err.contains(HASH_V1),
        "the refusal must name the retained previous hash so the operator can retry: {err}"
    );
}

#[test]
fn orch_5_cli_degraded_live_probe_refuses_even_with_acknowledgement() {
    let state = state_path("orch005-degraded.state");
    seed_two_versions(&state);
    let (ok, _, err) = run(&[
        "rollback",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
        "--degraded-live-probe",
        "--acknowledge",
        "operator confirmed rollback of alpha-1",
    ]);
    assert!(
        !ok,
        "an unprovable live status must refuse the rollback (fail closed)"
    );
    assert!(err.contains("live status unavailable"), "{err}");
}

#[test]
fn orch_5_cli_tampered_state_snapshot_is_refused() {
    let state = state_path("orch005-tampered.state");
    seed_two_versions(&state);
    // Corrupt the previous-version hash field in place.
    let tampered = fs::read_to_string(&state)
        .expect("state exists")
        .replace(HASH_V1, "sha256:nothex");
    fs::write(&state, tampered).expect("tamper");
    let (ok, _, err) = run(&[
        "rollback",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
        "--target",
        HASH_V1,
    ]);
    assert!(
        !ok,
        "a tampered snapshot must refuse the whole load, never read as 'no previous version'"
    );
    assert!(err.contains("invalid source hash"), "{err}");

    // A foreign file (wrong magic) is refused before any field is parsed.
    fs::write(&state, "NOT-A-SNAPSHOT\n").expect("overwrite");
    let (ok, _, err) = run(&[
        "show",
        "--state",
        state.to_str().unwrap(),
        "--strategy",
        "alpha-1",
    ]);
    assert!(!ok);
    assert!(err.contains("refusing a foreign/truncated file"), "{err}");
}

#[test]
fn orch_5_cli_malformed_flags_exit_nonzero() {
    let state = state_path("orch005-flags.state");
    seed_two_versions(&state);
    let state_str = state.to_str().unwrap();
    for (label, args) in [
        (
            "unknown flag",
            vec![
                "show",
                "--state",
                state_str,
                "--strategy",
                "alpha-1",
                "--bogus",
                "x",
            ],
        ),
        (
            "duplicate flag",
            vec![
                "show",
                "--state",
                state_str,
                "--strategy",
                "alpha-1",
                "--strategy",
                "alpha-1",
            ],
        ),
        (
            "valueless flag",
            vec!["show", "--state", state_str, "--strategy"],
        ),
        (
            "malformed record hash",
            vec![
                "record",
                "--state",
                state_str,
                "--strategy",
                "alpha-1",
                "--hash",
                "md5:nope",
            ],
        ),
        (
            "missing subcommand args",
            vec!["rollback", "--state", state_str],
        ),
        (
            // Write-side symmetry with the loader: an empty/whitespace strategy id
            // is refused at parse, so an exit-0 record can never write a snapshot
            // entry every later load refuses (bricking the durable record).
            "empty strategy id",
            vec![
                "record",
                "--state",
                state_str,
                "--strategy",
                "",
                "--hash",
                HASH_V1,
            ],
        ),
        (
            "whitespace strategy id",
            vec![
                "record",
                "--state",
                state_str,
                "--strategy",
                "   ",
                "--hash",
                HASH_V1,
            ],
        ),
    ] {
        let (ok, _, _) = run(&args);
        assert!(!ok, "{label} must exit nonzero");
    }
    // None of the refused commands above may have altered the snapshot.
    let (ok, shown, _) = run(&["show", "--state", state_str, "--strategy", "alpha-1"]);
    assert!(
        ok,
        "the snapshot must still load after every refused command"
    );
    assert!(shown.contains(&format!("current:{HASH_V2}@200")), "{shown}");
}

#[test]
fn orch_5_cli_concurrent_saves_never_clobber_each_others_scratch() {
    // The scratch file is UNIQUE per (pid, seq): concurrent invocations may race the
    // final rename (last-publish-wins; the single-logical-writer lock is the deferred
    // durable-store owner) but must never truncate each other's scratch bytes — every
    // surviving snapshot is a complete, loadable publish of ONE writer.
    let state = state_path("orch005-concurrent.state");
    let state_str = state.to_str().unwrap().to_string();
    let handles: Vec<_> = (0..8)
        .map(|worker| {
            let state_str = state_str.clone();
            std::thread::spawn(move || {
                let strategy = format!("alpha-{worker}");
                Command::new(BIN)
                    .args([
                        "record",
                        "--state",
                        &state_str,
                        "--strategy",
                        &strategy,
                        "--hash",
                        HASH_V1,
                    ])
                    .output()
                    .expect("run orch005_rollback_cli")
            })
        })
        .collect();
    for handle in handles {
        let _ = handle.join().expect("worker finished");
    }
    // Whatever interleaving happened, the published snapshot must be complete and
    // loadable (never a torn/foreign file) — `show` on any strategy it contains works,
    // and a fresh record on top still succeeds.
    let (ok, _, err) = run(&[
        "record",
        "--state",
        &state_str,
        "--strategy",
        "final-check",
        "--hash",
        HASH_V2,
    ]);
    assert!(
        ok,
        "the snapshot must remain loadable after concurrent saves: {err}"
    );
    let (ok, shown, _) = run(&["show", "--state", &state_str, "--strategy", "final-check"]);
    assert!(ok);
    assert!(shown.contains(&format!("current:{HASH_V2}")), "{shown}");
}

#[test]
fn orch_5_cli_line_breaking_strategy_ids_are_refused_at_parse() {
    // A strategy id embedding ANY line/field-breaking character could forge
    // whole proof lines in a downstream line splitter (Python's splitlines()
    // splits on \r, \x0b, \x0c, NEL, U+2028/U+2029 — not just \n). The write
    // side must be a strict superset of every consumer's splitter: refused at
    // parse, exit nonzero, snapshot untouched.
    let state = state_path("orch005-ctrl-id.state");
    seed_two_versions(&state);
    let before = fs::read_to_string(&state).expect("state exists");
    for hostile in [
        "z\rstrategy_count:zzz",
        "z\tfield",
        "z\nnewline",
        "z\u{0b}vt",
        "z\u{0c}ff",
        "z\u{85}nel",
        "z\u{2028}ls",
        "z\u{2029}ps",
    ] {
        let (ok, _, err) = run(&[
            "record",
            "--state",
            state.to_str().unwrap(),
            "--strategy",
            hostile,
            "--hash",
            HASH_V1,
        ]);
        assert!(!ok, "hostile id {hostile:?} must be refused");
        assert!(
            err.contains("control or line-separator"),
            "refusal must name the cause for {hostile:?}: {err}"
        );
    }
    assert_eq!(
        fs::read_to_string(&state).expect("state exists"),
        before,
        "refused ids must leave the snapshot byte-identical"
    );
}
