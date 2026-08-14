# Running a live-IB verification window

The operator checklist for closing a `live-ib` feature. Everything here was learned
the expensive way — the rule numbers point at `docs/playbooks/broker-and-live.md`,
where each one is recorded with its provenance.

**A live window does not parallelize.** One API client, one live designation, one
box. Confirm `agent_pool.py status` shows `leased:0` and `pgrep -x cargo` is empty
before you start, and never set `ATP_RUN_INTEGRATION=1` or touch ports 4001/4002
while siblings are running (rule 24).

---

## 0. Preflight — five things, in this order

```bash
# 1. Is the market open? reqMktData asks for delayed data (type 3, no entitlement
#    needed) and that STILL requires regular trading hours.
TZ=America/New_York date '+%a %H:%M ET'          # need Mon-Fri 09:30-16:00

# 2. Is the box exclusive?
python3 tools/agent_pool.py status --no-fetch | head -1   # leased:0
pgrep -x cargo || echo "no cargo"                          # rule 9 — NOT pgrep -f

# 3. Is the VM reachable at all? .env.integration is gitignored; never echo it.
set -a; . ./.env.integration; set +a
ping -c2 -W2000 "$ATP_IB_HOST"
nc -z -G4 "$ATP_IB_HOST" 22 && echo "ssh up"
nc -z -G4 "$ATP_IB_HOST" "$ATP_IB_PAPER_PORT" && echo "API port listening"

# 4. What does the feature still need?
python3 tools/verify_queue.py show <FEATURE_ID>

# 5. Which steps are already recorded, and at what HEAD?
python3 tools/evidence.py verify <FEATURE_ID>
```

If step 3 fails, stop and diagnose the network before touching anything else. A
subnet mismatch looks identical to a wedged gateway from the application's side:
on 2026-08-14 the workstation was on `192.168.2.0/24` while `.env.integration`
still named `10.0.0.54`, and every symptom pointed at IB.

---

## 1. Wake the gateway, and get the API actually ready

`ATP_IB_HOST` must be a **literal IP** — `IbConnectionConfig::from_env` rejects
hostnames outright (`crates/atp-adapters/src/interactive_brokers.rs`).

On the VM:

- **The IBC config in force is `$HOME/ibc/config.ini`.** Editing `/opt/ibc/config.ini`
  is a no-op (rule 22).
- **`ExistingSessionDetectedAction=manual` wedges the boot.** IBC stops at a dialog
  nobody is there to click. Set `primary` and restart `ibgateway.service`, or clear
  it over VNC. Check `AcceptIncomingConnectionAction` too.
- **`TradingMode=paper` and `ReadOnlyApi=no` must be *in force*, not merely written
  in a file** (rule 23).
- **`jts.ini` needs `TrustedIPs=127.0.0.1`.**
- **Port 4002 listening does NOT mean the API is ready — restart after login**
  (rule 19).
- **The gateway serves ONE API client and leaves the previous connection in
  `CLOSE_WAIT`.** Only a full restart clears it. Diagnose with
  `lsof -nP -iTCP:4002 | grep CLOSE_WAIT` (rule 21).

**Run live-IB tests on the VM over loopback** (rule 17), or forward the port and
keep the tunnel in its own shell (rule 18):

```bash
ssh -N -L 14002:127.0.0.1:4002 <user>@<vm-ip>
# then, in the working shell:
export ATP_IB_HOST=127.0.0.1 ATP_IB_PAPER_PORT=14002
```

---

## 2. Clear the digest tripwire

`tools/ib_adapter_check.py` binds recorded evidence to the exact bytes of three
files (`interactive_brokers.rs`, `wire.rs`, `srs_exe_006_ib_adapter.rs`). Editing
any of them — *a comment counts* — invalidates
`architecture/ib_paper_account_evidence.json` and flips closed-green SRS-EXE-006
back to red, recoverable only by an operator re-run.

```bash
ATP_RUN_INTEGRATION=1 python3 tools/ib_adapter_check.py
python3 tools/ib_api_version_check.py --sync
```

**Read the per-operation output, not the exit code.** A harness once reported
`PASSED` on `0/6 operations succeeded` against a dead connection (test-integrity
rules 4–6). If the fix touched a digest-pinned file, run it **twice** — the first
run regenerates the digest, the second proves the regenerated one holds.

Every `paper_account_round_trip` submits a **real 1-share AAPL market order**, so
run the no-order readiness probe first (rule 20).

---

## 3. Prove it, and capture what you prove

Drive the feature's own operator CLI. For SRS-MD-003:

```bash
cargo build -p atp-orchestrator --features ib-live-transport --bin md003_live_feed_cli
# run with a DEDICATED --client-id and a --snapshot path; point the dashboard at it:
export ATP_MD003_SNAPSHOT=<path> ATP_MD003_LOG_DIR=<dir>
```

Then exercise the acceptance criterion literally. MD-003's four legs: confirm ticks
arriving and health `FRESH`, **pause the feed >15 s**, and confirm all four —
detected, `HEARTBEAT_STALE` logged, dashboard row stale, `health.market_data_heartbeat`
flipped — then resume for `HEARTBEAT_RECOVERED`.

**A `live-ib` feature cannot close without an image on the acceptance-criterion
step.** Its AC is a claim about what a human would *see*, and an exit code cannot
show it. Capture the dashboard mid-incident:

```bash
python3 tools/evidence.py artifact <FID> --step 3 \
    --file ~/Desktop/heartbeat-stale.png \
    --caption "dashboard row STALE at staleness_ms=15001, health.market_data_heartbeat=STALE"
python3 tools/evidence.py render <FID>        # -> .harness/runs/<FID>/EVIDENCE.md
```

Screenshot the pane, not the whole page — a full-page shot of a ~4000px dashboard
renders the row a few illegible pixels tall. Browser-driven legs should use
`tests/e2e/capture.py`'s `evidence_browser(...)` with `element=`, under
`ATP_CAPTURE_EVIDENCE=1`.

Record the steps a subprocess genuinely cannot capture:

```bash
python3 tools/evidence.py record <FID> --step 3 \
    --command "md003_live_feed_cli --client-id N --snapshot ..." \
    --observed "staleness_ms=15001 broker / 15000 market data; GET /dashboard/api/heartbeat any_stale:true; ..." \
    --status pass
```

`record` stores `executed:false` and **does not satisfy the gate on its own** — it
counts only when a human closes with `--attested-by`. That is the point: a live
window is exactly the case the human path exists for.

---

## 4. Close it

Both critic layers must read `approve`; `evidence.verify` refuses otherwise. A
stale `block` from an earlier round is real work, not bookkeeping — re-run the
judgment reviewer against the diff the live run produced.

```bash
python3 tools/evidence.py verify <FID> --allow-attested      # expect ok
python3 tools/close_feature.py <FID> --verified --attested-by operator
```

Run it from the **primary checkout**. `integrate` hard-resets `feature_list.json`
from the base ref before staging, so a flip made inside a worktree cannot reach
main. Every attested close is appended to `.harness/closes.jsonl` in the primary
checkout, and the durable record is the commit message plus the retired
`closed-<ts>.json`.

**If a leg fails, fix the implementation — not the assertion.** A serialized
feature's e2e ships unrun, so the first real verification routinely fails on a
detail neither side noticed. That is the point of the run
(`scope-and-serialization.md` rule 7).
