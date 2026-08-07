# Broker adapters, the IB wire, and live runs

## Do not touch these three files

`tools/ib_adapter_check.py::_code_digest` SHA-256s exactly:

- `crates/atp-adapters/src/interactive_brokers.rs`
- `crates/atp-adapters/src/interactive_brokers/wire.rs`
- `crates/atp-adapters/tests/srs_exe_006_ib_adapter.rs`

Editing ANY of them — a comment counts — invalidates
`architecture/ib_paper_account_evidence.json` and flips closed-green SRS-EXE-006 back to
red, recoverable only by an operator re-run against the live paper account.

**Add a new file instead.** `connection_control.rs` exists for exactly this reason;
ERR-001 put its live-rejection gate in a new test file rather than appending. Run
`python3 tools/ib_adapter_check.py` before committing and expect
`git diff --stat` against those three paths to be empty. `(EXE-006)`

## Adapter scaffolding

1. **Explicit IO timeout, no DNS.** `connect_timeout` on a literal `SocketAddr` +
   read/write timeouts — never bare `TcpStream::connect` (OS-default hang) and never
   `to_socket_addrs()` (blocking getaddrinfo sits OUTSIDE the deadline). Validate the host
   as an `IpAddr` at config load and fail closed on a hostname. `(EXE-006 r1)`
2. **Confine the raw vendor error to the transport seam.** The public adapter maps it onto
   the crate's common `AdapterError` taxonomy; the raw type never reaches a caller.
   `(EXE-006 r2)`
3. **Implement the CANONICAL traits and ALL their methods**, including ones outside the AC
   (`account_status`, `positions`) — otherwise you advertise a capability that
   `NotConfigured`s at runtime. `(EXE-006 r3)`
4. **Fail-closed config:** missing → default, but malformed / out-of-range / zero /
   non-Unicode → typed error. `std::env::var`'s Err is BOTH NotPresent and NotUnicode; match
   them separately. `(EXE-006 r4)`
5. **Feature-gate the unverifiable live scaffold** behind a non-default cargo feature
   (`ib-live-transport`). The default build ships only the deterministic half and fails loud.
   `(EXE-006 r6)`
6. **The check must FAIL CLOSED without cargo** (a skip is a vacuous pass for a gate that
   claims it compiled the binary) and must build the feature-gated TEST target
   (`cargo test --features … --no-run`; `cargo build` does not). The `#[ignore]`d operator
   test must ASSERT its env gate, never early-`return`. `(EXE-006 r7)`

## The wire

7. **Pin the handshake to `v176..176`** so the negotiated server version is deterministic —
   one byte layout, no per-field version conditionals. `(EXE-006)`
8. **Generate golden vectors from the real `ibapi==10.19.4`** at serverVersion 176
   (`nautilus-ibapi` on PyPI; `ibapi` 10.x is not). `placeOrder` is 115 fields. Assert
   byte-for-byte against a fake gateway on an ephemeral port. `(EXE-006)`
9. **The handshake payload is RAW with NO trailing NUL** — every later message is
   NUL-terminated fields. A trailing NUL makes the real gateway go silent: connect succeeds,
   handshake is never answered. Found live, not in tests. `(EXE-006)`
10. **Reject control bytes (`< 0x20`) in `encode_frame`** — a control character in a symbol
    shifts every NUL-delimited field. `(EXE-006)`
11. **Broker acknowledgements are narrower than they look.** `openOrder` echo is NOT
    acceptance (a rejection can still follow) — only acknowledged `orderStatus` states.
    `PendingCancel` is not a terminal cancel (only Cancelled / ApiCancelled or code 202).
    Error 10197 ("no market data during competing live session") is data WITHHELD, not a
    subscribe confirmation → fail closed. `(EXE-006)`
12. **Connectivity codes must DROP the cached session**, and both `is_transport_fault` and
    the notice filter must derive from ONE
    `classify_ib_order_error == ConnectivityBlocked` helper so 1100/2110 cannot be masked as
    a benign 2100–2169 farm notice. `(EXE-006)`
13. **IB exposes no tick sequence number**, so live gap detection (SRS-MD-007) is
    unsatisfiable as specified — say so rather than inventing a proxy. `(MD-003 live)`

## Proving a live path

14. **Prove via `route_order`, never `submit_live_order`.** The latter takes the mode as a
    caller argument, so the proof can reach the broker with no designated live strategy at
    all. `designate(...)` then `route_order(...)`. `(ERR-001)`
15. **Count wire attempts.** Assert `wire-attempts:1` so a rejection is provably broker-side
    rather than a gate short-circuit, and add a non-designated case asserting refusal at
    `wire-attempts:0` so the gate is shown to be load-bearing. `(ERR-001)`
16. **A bounded-wait timeout on a MUTATING operation is UNKNOWN, not did-not-happen.**
    Verified live: `cancel_order` returned `ConnectivityBlocked` after its 15s deadline while
    the broker had actually cancelled. Recovery must re-read broker state, never blind-retry.
    `(EXE-007, 2026-07-30)`

## Running against the real gateway

17. **Run live-IB tests ON the VM over loopback.** The gateway answers the v100+ handshake
    correctly on loopback but resets a remote connection even with `TrustedIPs` listing the
    IP, because the running process loaded `jts.ini` at startup. `(MD-006)`
18. **Or SSH local-forward** when the VM has no cargo and no clone:
    `ssh -N -L 4002:127.0.0.1:4002 <vm>`, then `ATP_IB_HOST=127.0.0.1
    ATP_IB_PAPER_PORT=4002 ATP_RUN_INTEGRATION=1 …`. Cargo runs where the repo is, the
    gateway sees a loopback client. `(EXE-007)`
19. **Port 4002 listening does NOT mean the API is ready — restart after login.** The
    identical command that failed on a first session passed after
    `systemctl restart ibgateway`. `(ERR-001; EXE-007)`
20. **Run the no-order readiness probe first** (a rejected nonexistent symbol proves
    handshake + error path in ~0.2s and creates nothing restable) before spending a live
    order. Every `paper_account_round_trip` run submits a real 1-share AAPL market order.
    `(EXE-007)`
21. **The gateway serves ONE API client and leaves the prior connection in `CLOSE_WAIT`.**
    That half-dead socket occupies the single slot, so new handshakes are accepted at TCP and
    never answered ("API Client: red", 15s wire timeout). Only a full gateway restart clears
    it. Recipe: restart → wait for account + green on the main screen → run the check EXACTLY
    once, no retry loop (each probe consumes the one fresh slot). Diagnose with
    `lsof -nP -iTCP:4002 | grep CLOSE_WAIT`. `(EXE-007)`
22. **Which IBC config is in force:** `$HOME/ibc/config.ini` (set as `IBC_INI` by
    `/opt/ibc/gatewaystart.sh`) — editing `/opt/ibc/config.ini` is a no-op. `gatewaystart.sh`
    refuses to start when a matching JVM is alive, so a `systemctl restart` can silently
    no-op; confirm `systemctl show ibgateway -p ActiveEnterTimestamp` actually moved. The
    gateway also self-stops at IB's daily closedown. `(MD-006)`
23. **`TradingMode=paper` AND `ReadOnlyApi=no` must be in force**, not merely written in a
    file. `(MD-006)`
24. **Live windows do not parallelize** — exactly one strategy may execute against IB at a
    time, and the single-live invariant is a hard constraint, not a convention. Never set
    `ATP_RUN_INTEGRATION=1` or touch ports 4001/4002 while siblings are running.
25. **Read the per-operation output before believing a live green.** See
    [test-integrity.md](test-integrity.md) rule 4. `(MD-006)`

## Attestation

26. **A live-execution slice cannot be re-proven in CI, and the reviewer will (correctly)
    block committed self-attested evidence.** Fix every real bug, then let the operator
    authorize the flip — `--force-complete`, never a faked APPROVE. Every `wire.rs` change
    invalidates the digest and needs a fresh live run, so keep the per-operation DIAGNOSTIC
    in a separate file from the digested test. `(EXE-006)`
