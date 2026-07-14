=== SESSION SRS-REL-002 ===
Date: 2026-07-14
Feature: SRS-REL-002 — restore a full system restart to trade-ready state within the SyRS recovery
         target (SyRS NFR-R6: ≤ 10 minutes; SYS-76 readiness check).
Outcome: serialized (RTO measurement substrate built + fixture-verified + a REAL partial infra
         measurement captured on the reference deployment; passes stays FALSE per the feature's own
         step 4 and NFR-R6's system-test method — the full trade-ready proof is deferred).

## What I did
Built the **restart-recovery-time (RTO) certification substrate** — the NFR-R6 analog of the
SRS-REL-001 availability substrate — extending `python/atp_reliability/` (REL-001's
`availability.py`/`cli.py`/`__main__.py` left byte-identical). Plus a boot-timeline evidence
collector, and a REAL reference-deployment measurement on the operator's Proxmox host.

Files:
- `restart.py` — pure, clock-free engine (imports no `atp_*`; reuses `Verdict`/`NS_PER_SECOND`).
  `compute_restart_recovery(phases, readiness, during_market_hours, target)` →
  `RestartRecoveryArtifact` with a three-valued verdict (PASS/FAIL/INCONCLUSIVE). Honesty rules:
  - Elapsed is END-TO-END (`readiness_check.end − proxmox_vm.start`); inter-phase GAPS count
    against the 10-min budget (not sum-of-durations, which would hide idle/retry/crash-loop time).
  - `observed_span_ns = max(present end) − min(present start)` is the provable lower bound (correct
    even with overlapping/nested phases — on a real boot Docker starts DURING userspace OS boot).
  - Complete required set from the enum authorities (`REQUIRED_PHASES` 5, `REQUIRED_SUBCHECKS` 5) —
    a missing phase/sub-check → INCONCLUSIVE (a caller can't drop the slow phase to squeak under).
  - Trade-ready needs gate == READY (a manual OVERRIDDEN of a *failed* check does NOT certify) +
    all five SYS-76 sub-checks passing (NAS DEGRADED only with the operator alert, per SYS-76(d)).
  - Verdict order = provable-FAIL (`observed_span > budget`, or readiness observed-not-ready)
    BEFORE any missing-evidence INCONCLUSIVE — a definite breach is never downgraded.
  - Integer boundary gate (`elapsed_ns <= 600_000_000_000`, exact); `RestartRecoveryTarget`
    labelled SRS-REL-002 is `__post_init__`-locked to the 10-min budget (explicit raise, survives
    `python -O`); timestamps bounded to `[0, 2**63)`.
  - Market-hours scope: NFR-R6 applies to market-hours restarts. An SRS PASS requires the scope
    proven, DERIVED (not caller-supplied — that would be forgeable) from the actual restart-trigger
    timestamp via the real `UsEquityTradingCalendar` (adapter-side; the pure engine stays
    calendar-free). Out-of-scope / scope-unknown → INCONCLUSIVE (a provable breach is still FAIL).
- `restart_cli.py` — `PYTHONPATH=python python -m atp_reliability.restart_cli --fixture PATH`.
  Fail-closed parsing: strict duplicate-JSON-key rejection (`object_pairs_hook`); absent optional
  key → default, present-but-wrong-type → refuse; derives market-hours from the `proxmox_vm`
  trigger (scope-derivation failure degrades to unknown, never hides a breach). `--json` stdout is
  PURE JSON (summary → stderr). Exit 0=PASS / 1=FAIL|INCONCLUSIVE / 2=refused. No `--budget` flag.
- `boot_evidence.py` — host-telemetry adapters. `run_host_collection` MEASURES ONLY `OS_BOOT`
  (`/proc/stat` btime + `systemd-analyze`, initrd-aware, firmware/loader excluded as pre-btime) and
  `DOCKER_DAEMON` (`systemctl show docker` monotonic, rebased on btime), via a BOUNDED, error-
  translating command runner (fail-closed, never hangs). `PROXMOX_VM`/`ATP_SERVICE_INIT`/
  `READINESS_CHECK` are EXTERNAL caller inputs (the collector does NOT drive Compose/the gate).
  `derive_during_market_hours` (calendar-based scope). No host identity embedded.
- `__init__.py` — restart exports. `architecture/runtime_services.json` — new
  `restart_recovery_contract` block (mirrors `availability_measurement_contract`).
- Tests: `tests/unit/test_restart_engine.py` (L1), `tests/unit/test_boot_evidence.py` (L1),
  `tests/property/test_restart_properties.py` (L2), `tests/domain/test_restart_recovery.py`
  (L7 @domain @safety), `tests/test_restart_recovery_contract.py` (L3). 125 restart/boot tests;
  full non-integration suite green.

Why Python not Rust: mirrors REL-001 — the concrete DST/holiday calendar + evidence sources are
Python; offline analysis, not a runtime service (AC-16-permitted, alongside atp_readiness/atp_cli).

## Reference-deployment measurement (part C — operator-directed, on the Proxmox host)
Stood up / used the operator's on-prem Proxmox reference deployment and captured a REAL cold-boot
infra timeline through the collector (the `dockerhost-alphalabs` KVM VM, Ubuntu 24.04 + Docker):
- PROXMOX_VM (qm-start → kernel start; hypervisor-side, cross-clock caveat): **4.664 s**
- OS_BOOT (Ubuntu 24.04 kernel+userspace): **4.111 s**
- DOCKER_DAEMON (nested during userspace boot — the overlap model accepted it): **0.503 s**
- observed infra span (VM-start → OS/Docker up): **8.775 s** (budget 600 s)
- SRS-REL-002 verdict on the real (partial) evidence: **INCONCLUSIVE** — honestly, because
  `atp_service_init` + `readiness_check` phases are absent (ATP services are `cargo test` stubs, not
  a running platform; SYS-76 runtime probes deferred to SRS-MD-006). The substrate refused to
  certify rather than emit a false PASS — the serialized deferral working exactly as designed.
(Also captured a real 68.3 s OS_BOOT from the PVE node itself, read-only, earlier.)
Host credentials/IP handled as secrets — never committed; stored only in the operator's `~/.ssh`.

## What I tested (per feature step)
- Step 1 (init.sh): PASS — `./init.sh` → "✓ Environment ready" (installed requirements-dev.txt).
- Step 2 (exercise): PASS — `restart_cli` over fixtures: in-hours compliant → PASS exit 0;
  over-budget → FAIL exit 1; missing phase/sub-check / out-of-hours → INCONCLUSIVE exit 1;
  overridden gate → FAIL; malformed/dup-key/oversized/unknown-phase → refused exit 2. AND the real
  reference-deployment cold-boot measurement above.
- Step 3 (AC verification): PASS at the MECHANISM level — 125 restart/boot tests (L1/L2/L3/L7) +
  full non-integration suite green; ruff clean; mypy 0 atp_reliability errors. The real
  ≤10-min-to-trade-ready proof under reference deployment is deferred (system test).
- Step 4: recorded here; passes stays FALSE.

## Critic verdicts
  deterministic (critic_check.py --staged): APPROVE — no findings (every commit).
  judgment (adversarial_review.py, reviewer=codex): APPROVE at round 11, after 14 in-scope fixes
    (each with a regression test; never a faked APPROVE) — the certification-substrate adversarial
    pattern (REL-001 took 20). The journey:
    R1  overlap-refuses-valid-boots (Docker nests in OS boot → milestone model, max−min span);
        duplicate-JSON-keys silently collapsed → object_pairs_hook; huge-int → OverflowError →
        `[0,2**63)` bound.
    R2  host command runner unbounded → hang → bounded + error-translating `default_command_runner`.
    R3  `--json` stdout not pure JSON → summary to stderr.
    R4  OS_BOOT double-counts pre-btime firmware/loader → `total − firmware − loader`.
    R5  OVERRIDDEN gate falsely certifies → only READY certifies (override = manual bypass → FAIL).
    R6  systemd `(initrd)` time dropped → initrd-aware parse; market-hours scope unenforced → gate.
    R7  docstring drift (overridden + market-hours) → corrected.
    R8  forgeable market-hours BOOLEAN → DERIVE scope from the trigger timestamp + trading calendar.
    R9  scope-derivation failure preempts a provable over-budget FAIL → degrade to unknown, never
        refuse.
    R10 collector overclaims (docs say it drives Compose/gate) → doc/contract reconciled to the real
        collection boundary (OS_BOOT + DOCKER only; the rest is external caller evidence).
    R11 APPROVE.

## Resume / next (to flip passes:true — operator, serialized)
The engine + collector are complete and fixture-verified; the deferred owners (also in
`restart_recovery_contract.deferred`) are real-operation evidence, not unbuilt features to `block`:
1. The SYS-76 runtime readiness probes (IB connectivity/auth, IB account, SSD ingestion-freshness,
   NAS reachability, service health) — SRS-MD-006 (blocked on SRS-EXE-006). Until these exist there
   is no genuine `readiness_check` pass to measure.
2. A running ATP platform (the phase1 compose services are `cargo test` stubs today) to give a real
   `atp_service_init` phase.
3. A real full-system restart of that platform DURING market hours, on the reference Proxmox
   deployment, with all 5 phases + all 5 SYS-76 sub-checks observed → run the collector →
   `restart_cli` → certify ≤10 min → flip via verified-e2e.
No `block` — deps (`atp_readiness`, `atp_reliability`, `atp_strategy.calendar`) are on main.
