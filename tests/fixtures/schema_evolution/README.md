# SRS-DATA-015 golden corpus — byte-frozen historical payloads

These files are **frozen bytes written by an older build**. They are the durable evidence for
SRS-DATA-015's second acceptance clause:

> data written under older schema versions remains queryable after schema updates **without bulk
> migration**

Every test over this corpus reads these files **in place** with today's readers and asserts the bytes
are unchanged afterwards. That is the whole point: if reading old data required rewriting it, the
requirement would be unmet.

## Do not regenerate

Regenerating a fixture from the current writer would silently convert this regression lock into a
tautology — it would then only prove that today's writer round-trips with today's reader, which is
what the ordinary unit tests already cover. If a fixture "fails", the correct response is to fix the
reader, or (for a deliberate, documented format break) to **add** a new fixture beside the old one and
record the break in the owning module's version history.

## Contents

| File | Entity | Represents |
|---|---|---|
| `market_store_v1.store` | `market-data-store` | v1 — the four original dataset kinds |
| `market_store_v2.store` | `market-data-store` | v2 — adds `CorporateActionSplit` |
| `market_store_v3.store` | `market-data-store` | v3 — adds `CorporateActionCoverage` |
| `market_store_v4.store` | `market-data-store` | v4 — adds the dividend/delisting/merger/rename kinds |
| `access_journal_legacy.log` | `access-journal` | pre-SRS-DATA-015: no `v<N>` line tag |
| `kill_switch_last_activation_legacy.json` | `kill-switch-last-activation` | pre-SRS-DATA-015: no `schema_version` key |
| `system_log_segment_legacy.jsonl` | `system-log-segment` | pre-SRS-DATA-015: bare `LogRecord.as_dict()` lines |
| `hot_swap_trigger_log_legacy.jsonl` | `hot-swap-trigger-log` | pre-SRS-DATA-015: no leading `schema_version` key |

The four `market_store_v*.store` blobs were emitted by the real `MarketDataStore::serialize` (which
writes the *minimum* version its contained kinds require, so choosing the kinds chooses the version).
The four `*_legacy.*` payloads reproduce exactly what the writers emitted before this feature added
their version fields.
