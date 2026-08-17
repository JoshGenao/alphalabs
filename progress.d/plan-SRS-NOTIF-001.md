# SRS-NOTIF-001 — replace the SMS channel with push (ntfy)

## Context

SRS-NOTIF-001 is built and `passes:false`. The dispatcher, both transports, the
detection wiring and the operator CLI all exist and survived nine adversarial
review rounds. The one thing that has never happened is a real message reaching
the operator, because no SMS provider was ever chosen.

US A2P 10DLC registration is weeks of lead time with silent carrier filtering.
Push over a self-hosted ntfy on the LAN reaches the operator's phone over VPN
with no carrier in the path, and preserves SN-1.12's intent — reach the operator
when they are not looking. SC-9 ("at least two configured channels") is unchanged
and still satisfied by email + push.

This change swaps the channel. It does **not** close the feature: the two
standing reasons NOTIF-001 stays `passes:false` (no automatic dispatcher runtime;
detection is observation-driven, not loss-driven) are untouched.

**Operator decisions, confirmed this session:**
- Rename **both** channel enums — `atp_notification::NotificationChannel::Sms`
  and `atp_types::OperatorAlertChannel::Sms` → `Push`.
- Verify ntfy against public `ntfy.sh` plus a throwaway local container.
- Config keys `ATP_PUSH_*`; the topic is a **secret**.

**Correction to the brief:** `hot_swap_demotion_drill.rs` does not exist. The
third live call site is `crates/atp-orchestrator/tests/err_7_hot_swap_demotion_timeout.rs`,
which uses `OperatorAlertChannel` — the second enum. That is why the rename
reaches `atp-types`.

---

## Step 0 — Verify ntfy empirically (blocking; do this first)

The docs do not state the success body or the auth rejection shape, and both
decide `push.rs`'s code. Pin them before writing it.

Against public `ntfy.sh`, using a long random topic:

```bash
TOPIC="atp-probe-$(openssl rand -hex 16)"
curl -v -H "Authorization: Bearer tk_definitely_invalid" -d "probe" "https://ntfy.sh/$TOPIC"
python3 -c "print('x'*5000)" | curl -v --data-binary @- "https://ntfy.sh/$TOPIC"
```

Record: exact 2xx status; the JSON body and whether `id` is present; whether a
bad Bearer token on an unprotected topic is accepted or rejected; what the
>4,096-byte response looks like (attachment conversion).

Then a throwaway container for the protected-topic path `ntfy.sh` cannot show —
bare `docker run`, bound to loopback on a high port, no compose, no shared
resource:

```bash
docker run --rm -p 127.0.0.1:18080:80 binwiederhier/ntfy serve
# then: no token, wrong token, right token → record each status + body
```

Write the transcript to `progress.d/session-SRS-NOTIF-001.md`. If the success
body has no `id`, the `ChannelReceipt` reference becomes the raw bounded 2xx body
and the plan below changes only there.

---

## Commit 1 — `prep`: catalogue the push config keys

`architecture/runtime_services.json` is the single source of truth
(`python/atp_config/validate.py` loads it at import). It is also the one file
parallel branches collide on, so it lands first and small.

| File | Change |
|---|---|
| `architecture/runtime_services.json` | Replace the `ATP_SMS_API_KEY` catalogue entry with `ATP_PUSH_TOKEN`; add `ATP_PUSH_TOPIC` (secret), `ATP_PUSH_HOST`, `ATP_PUSH_PORT`; add the two missing `ATP_OPERATOR_*` keys; update `required_env_vars` |
| `python/atp_config/README.md` | Mirror the table rows; key count 19 → 24; `notification_channels` category text "Email and SMS" → "Email and push" |
| `.env.example`, `docker-compose.yml` | Swap `ATP_SMS_API_KEY` for the four `ATP_PUSH_*` keys in both the shared env block and the `x-atp-no-secrets` blanking block |
| `tools/deployment_check.py` | `_SECRET_BLANK_KEYS`: drop `ATP_SMS_API_KEY`, add `ATP_PUSH_TOKEN` + `ATP_PUSH_TOPIC` |
| `tools/credential_security_check.py` | `_FAKE_SMS` → `_FAKE_PUSH_TOKEN` + `_FAKE_PUSH_TOPIC`; the three-secret assertions become four |
| `tests/unit/test_configuration.py`, `tests/unit/test_secret_vault.py`, `tests/domain/test_credential_redaction.py`, `tests/domain/test_startup_readiness_gate.py`, `tests/integration/test_jupyter_isolation_inspect.py` | Update the pinned secret-key sets |

**Fixes the pre-existing bug you flagged, in full.** `ATP_OPERATOR_SMS` was read
by `from_env` but absent from the catalogue, so `deployment_check.py` never
validated it. `ATP_OPERATOR_EMAIL` has the identical defect. Both are catalogued
here — `ATP_OPERATOR_EMAIL` (non-secret) and `ATP_PUSH_TOPIC` (secret, replacing
the destination role `ATP_OPERATOR_SMS` played).

The topic being secret is what pulls it into the vault, the log redactor and the
`x-atp-no-secrets` blanking list — on ntfy, whoever holds the topic can publish.

---

## Commit 2 — `feat`: the rename and `push.rs`

### 2a. `crates/atp-adapters/src/notification/push.rs` (replaces `sms.rs`)

`sms.rs` is already an HTTP/1.1 client sending `Authorization: Bearer` with
`Connection: close`. Keep its whole safety spine — `EgressEndpoint` (re-resolves
per connect, validates **every** resolved address, RFC 1918 + loopback only, no
link-local), `SendBudget` (created once per send, every operation armed from what
is left), `read_line_budgeted` / `read_to_end_budgeted`, `arm_socket`,
`io_error_to_channel_error`, `fold_protocol_line`. Do not reintroduce std's
`read_line`/`read_to_end` on this path.

What changes:

- **No relay hop.** Connect straight to the LAN ntfy host. `EgressEndpoint`
  still refuses anything that does not resolve inside RFC 1918 or loopback.
- **Request line** `POST /<topic>` — the topic replaces the fixed `/sms` path.
- **Body** plain UTF-8 text, not JSON. First line is the summary; the alert
  detail follows.
- **`MAX_PUSH_BODY_BYTES = 1024`, measured in bytes**, truncated on a character
  boundary with the existing `…[truncated]` marker. ntfy converts anything over
  4,096 **bytes** into an attachment; the old cap was 480 *chars*, which is up to
  1,920 bytes for multibyte text. An alert must never become a file.
- **`X-Title`** carries severity + trigger from the enums' `as_str()` — a closed
  ASCII vocabulary, never free text. **`X-Priority`** maps `Critical` → 5,
  `Error` → 4. Free-text summary stays in the body, so no operator- or
  alert-controlled string ever reaches a header. This removes header-injection
  and header-encoding risk outright.
- **`ChannelReceipt` reference** is the `id` field parsed out of the 2xx JSON
  with a hand-rolled bounded extractor (no external crates), still capped at
  `MAX_REFERENCE_CHARS`.
- **The topic is redacted like the credential.** This is the exact class that
  took four adversarial rounds to close: every server-controlled or secret string
  reaching a persisted field is scrubbed at construction, success paths included.
  Concretely — hand-written `Debug` redacts topic *and* token; the topic is
  validated for whitespace/control characters but the failure detail must
  **not** echo it (today's `sms.rs` does `got {path:?}`, which would leak it);
  no error, status line or success receipt may interpolate the request line.

### 2b. `NotificationChannel::Sms` → `Push`

`crates/atp-notification/`: `event.rs` (variant, `as_str()` → `"PUSH"`,
`REQUIRED_CHANNELS`, module docs), `channel.rs`, `dispatcher.rs`, `lib.rs`,
`store.rs`, `tests/srs_notif_001_dispatch.rs`.

**On-disk format break.** `store.rs::channel_tag` writes `"S"`; it becomes
`"P"`. Bump `SCHEMA_VERSION` to `2` **and** `MIN_SUPPORTED_SCHEMA_VERSION` to
`2`, so a v1 blob fails with the precise `unknown notification store schema
version` error rather than the vaguer `unknown channel tag`. No production store
exists, so nothing real is lost — but the failure must be loud and specific.

### 2c. `OperatorAlertChannel::Sms` → `Push`

`crates/atp-types/src/lib.rs` (variant, `as_str()` → `"PUSH"`, its unit test) and
`crates/atp-types/src/perf.rs` (the NFR-P6 description string and its assertion).
Call sites: `crates/atp-execution/src/lib.rs:1367`,
`crates/atp-orchestrator/src/{kill_switch_timeout.rs,connectivity_notification.rs}`,
`crates/atp-orchestrator/src/bin/notif001_operator_alert_cli.rs`, and the
`err_7` / `err_8` tests.

**Both sides of the pinned contracts move together, or the checks go red:**
`architecture/runtime_services.json` carries the variant list twice
(`operator_alert_channel.variants`) and the qualified names twice
(`alert_channels: ["OperatorAlertChannel::Sms", …]`) — four spots.
`tools/hot_swap_demotion_check.py` and `tools/kill_switch_timeout_check.py` read
those blocks; their docstrings need the same wording change. Run both checks plus
`architecture_check.py` before committing.

### 2d. Python prose mirrors

Doc-comment text only, no identifiers: `python/atp_safety/timeout.py`,
`python/atp_dashboard/{alerts.py,killswitch.py}`,
`python/atp_readiness/{probes.py,runtime.py}`, `python/atp_logging/*.py`.
`tests/e2e/test_dashboard_refresh.py:708` pins `"channel": "SMS"` in the
`/api/v1/alerts` shape — update it (I cannot run e2e while siblings are active;
flag it as unrun).

---

## Commit 3 — `docs`: the approved requirement edits

`docs/SyRS_v0.7.md` — IF-11 (line 149) "SMS Notification / Third-party SMS
gateway API" → "Push Notification / ntfy publish API"; SYS-44b (272), SYS-46
(274), SYS-49c (284), NFR-P6 (427) "email and SMS" → "email and push
notification"; NFR-S4 (443) "(SMTP, SMS gateway API keys)" → "(SMTP, push
service API tokens)"; the data-dictionary and cost-model lines. SC-9 unchanged.

`docs/StRS_v0.7.md` — SN-1.12 (line 85): push moves into Phase 1, SMS moves to
future phases, rationale sentence unchanged; the traceability row at line 258.

`docs/SRS.md` — mirror every one of the above: lines 79, 107, 223, 238, 245, 249,
297, 559. Add a dated revision note giving the 10DLC reason.

`docs/DEPLOYMENT.md` — the secret-key lists at lines 39–48, 187, 192, 264. "The
five secret keys" becomes six.

**Known, accepted drift:** `feature_list.json`'s `description` for SRS-NOTIF-001
will still read "email and SMS". Hand-editing it is forbidden — only the locked
`integrate` mutates it. No tool cross-validates it against `docs/SRS.md`, so this
is cosmetic; it is yours to reconcile at close time.

---

## Tests

- **L1 unit** (in `push.rs`): byte-exact truncation on a character boundary; a
  multibyte body near the cap; `X-Title` built only from enum strings; topic and
  token absent from `Debug`; blank/control-character config rejected **without**
  echoing the topic; JSON `id` extraction, including a malformed and an
  oversized body.
- **L4 boundary** (`crates/atp-adapters/tests/srs_notif_001_transports.rs`):
  rewrite the SMS cases against a scripted ntfy-shaped HTTP relay on a real
  loopback socket — 2xx with a JSON `id`; 401 and 403 → `ChannelError::Rejected`;
  5xx → `TransportUnavailable`; a dribbling relay → `Timeout` via the budget; a
  relay echoing the token in its reply → **token absent from the persisted
  detail**; the same test for the topic; the success receipt scrubbed too.
- **L7 domain** — **mandatory**, and the critic enforces it:
  `connectivity_notification.rs` and `kill_switch_timeout.rs` both match
  `SAFETY_PATH_RE`, so the same commit must carry a `tests/domain/` diff. Update
  `tests/domain/test_notification_transports.py`,
  `test_notification_dispatch.py`, `test_connectivity_notification.py`,
  `test_kill_switch_liquidation_timeout.py`, `test_credential_redaction.py`.
- **Discriminating-test method** (kept from the prior session): temporarily write
  each bug in and confirm exactly one test catches it. Apply it to the byte-cap
  and the topic-redaction tests — both have wrong implementations that pass
  everything else.

---

## Verification

```bash
./init.sh                                    # "✓ Environment ready"
cargo test --workspace                       # expect ~2107 passing, 0 failed
pytest -m "not integration and not e2e"
cargo clippy --workspace -- -D warnings && cargo fmt --check
python3 tools/config_check.py
python3 tools/deployment_check.py
python3 tools/credential_security_check.py
python3 tools/architecture_check.py
python3 tools/hot_swap_demotion_check.py
python3 tools/kill_switch_timeout_check.py
tools/run_ci_locally.sh
```

Then end-to-end against the local container from Step 0, driving the real gate:

```bash
ATP_PUSH_HOST=127.0.0.1 ATP_PUSH_PORT=18080 ATP_PUSH_TOPIC=... ATP_PUSH_TOKEN=... \
  cargo run -p atp-orchestrator --bin notif001_operator_alert_cli -- \
  outage --state unreachable --store <dir>
```

Inspect the stored `NotificationEvent` — not just the exit code — and confirm
both deliveries, the push receipt carrying ntfy's real message id, and dispatch
inside 60,000 ms. Then the negative path with the container stopped: both
channels `TRANSPORT_UNAVAILABLE`, failure still stored, exit 1.

Critic gate: `tools/critic_check.py --staged` must APPROVE, then
`tools/adversarial_review.py origin/main`. Record the verdict **and** which
reviewer ran.

## Pre-existing red, not caused by this change

`run_ci_locally.sh`'s mypy step is red on `python/atp_strategy/examples/`
(66 errors, identical on `origin/main`). Do not chase it.

## Outcome

**Serialized** — `passes` stays `false`, integrated with
`agent_pool.py integrate SRS-NOTIF-001 --mode serialized`.

Push removes the *provider* blocker, and after this the operator can actually
receive an alert. It does not remove the two structural blockers: there is still
no long-running dispatcher process (needs SRS-EXE-001), and detection is still
observation-driven — a Gateway dying while no order is routed goes unnoticed
(needs SRS-MD-003 / SRS-EXE-001). The flip still requires a real IB Gateway
outage on the Proxmox host with a real email and a real push arriving inside 60
seconds.
