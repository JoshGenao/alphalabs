# Plan — SRS-DATA-013 close-out (approved by the operator 2026-10-19)

The data-layer validator and quarantine-and-continue path are on `main` (session 1,
2026-07-03). What is missing is the acceptance criterion's last clause: "dashboard and
notification alerts include counts and reasons". SyRS SYS-77 also says rejected records are
"quarantined in a separate store" — nothing persists them today, and the `deferred[]` entry
that names SRS-DATA-014/015 as the store's owner is wrong (DATA-014 is SYS-78 anomaly
detection, R2; DATA-015 is schema evolution). The store is this feature's.

Three landings, each reviewed on its own (estimated ~1,900 lines in total). Landings 1 and 2
integrate `serialized`; landing 3 carries the live evidence and closes on operator attestation.

## Landing 1 — durable quarantine store (atp-data), ~650 lines

- `crates/atp-data/src/quarantine_store.rs`: append-only, per-line-checksummed store. One RUN
  record per ingestion batch (run id, source, detected_at, records_in, valid_written, a count
  for each of the six `QuarantineReason`s) and one RECORD entry per quarantined record (record
  hash, natural key, reason, source, timestamp — the SyRS "Data Quarantine Record").
- Written BEFORE the valid subset reaches primary storage; a failed quarantine write refuses
  the whole batch (a quarantine nobody can see is the failure SYS-77 exists to prevent).
- Reads are tri-state: absent / unreadable / ok. Tail reads are bounded.
- Every ingestion CLI that runs `Sys77RecordValidator` (data013, data016, data005, data008)
  takes a required `--quarantine-dir`. `data013_ingestion_validation_cli summary` prints the
  latest runs for the dashboard.
- Correct the mis-owned `deferred[]` entries in `architecture/runtime_services.json`.

## Landing 2 — operator notification, ~550 lines

- `TriggerKind::DataQuarantine` (severity Error) with a `data_quarantine(summary, ms)`
  constructor; sweep every match on the kind.
- `crates/atp-orchestrator`: `data013_quarantine_alert_cli notify` reads unnotified runs from
  the quarantine store, dispatches one Email + Push per run with counts and reasons through
  `OperatorNotifier`, appends to `notification_events.store`, and advances a durable cursor so
  no run alerts twice.
- Deferred, owned by SRS-DATA-001/002 (the scheduled ingestion jobs): running `notify`
  automatically after every ingestion job.

## Landing 3 — dashboard pane + evidence, ~700 lines

- `python/atp_dashboard/quarantine.py` provider over the `summary` CLI; `ATP_QUARANTINE_DIR`;
  `GET /dashboard/api/quarantine`; REST poll only (no new WS channel).
- Pane, in the existing Mission Control language: a "sieve" bar for the latest run (records
  in → written | quarantined, reason chips under the quarantined share); a reason matrix with
  all six reasons always listed (zero dimmed, never hidden) across recent runs; Email/Push
  delivery per run. Five visually distinct states: unconfigured, store unreadable (never
  zero), no runs yet, clean run, run with quarantines.
- Tests: L1 codec/aggregation, L4 route + CLI, L7 domain (safety path), browser e2e for all
  five states with evidence capture. Live leg: ingest → real Email + Push via the VM's relay
  and ntfy → screenshot.
- Expected outcome: `passes:true` on operator attestation of the live run.
