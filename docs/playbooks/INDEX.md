# Playbook index — read the ones whose trigger matches your feature

Every rule here was paid for once, by a session that got blocked, shipped a false green, or
turned `main` red. Sessions are fresh-context; this directory is the only carry-forward.

**How to use:** during orientation (coding prompt Step 1), read the two always-on playbooks
plus the 1–3 whose trigger matches your claimed feature. Before requesting adversarial
review (Step 6.6), walk them again against your own diff.

| Playbook | Read it when… |
|---|---|
| [adversarial-precheck.md](adversarial-precheck.md) | **Always** — before the first review round, and after every BLOCK |
| [test-integrity.md](test-integrity.md) | **Always** — you are writing or trusting any test |
| [honest-surfaces.md](honest-surfaces.md) | you add or change a REST route, CLI command, WS channel, or dashboard pane |
| [contract-drift.md](contract-drift.md) | you touch OpenAPI / AsyncAPI / the CLI manual / `architecture/*.json`, or a deferred surface goes live |
| [durable-writes.md](durable-writes.md) | anything is persisted to, or read back from, disk |
| [lifecycle-and-concurrency.md](lifecycle-and-concurrency.md) | you add a background thread, publisher, cursor, lock, readiness claim, or deadline |
| [safety-paths.md](safety-paths.md) | the diff touches kill switch, connectivity, stale data, live mode, orders, or callbacks |
| [broker-and-live.md](broker-and-live.md) | IB, the wire protocol, the paper account, or `atp-adapters` is involved |
| [data-substrate.md](data-substrate.md) | ingestion, point-in-time reads, tiering, coverage, corporate actions, or a derived series |
| [security-boundaries.md](security-boundaries.md) | bind policy, docker-compose isolation, credentials, or a proxy seam |
| [measurement-and-certification.md](measurement-and-certification.md) | the feature emits a verdict ABOUT the system — availability, latency NFRs, reproducibility, coverage |
| [pipeline-and-integrate.md](pipeline-and-integrate.md) | you are about to commit, rebase, integrate, or run the CI mirror |
| [scope-and-serialization.md](scope-and-serialization.md) | you are classifying complete vs serialized vs blocked, or the review will not converge |

## Rule format

Each rule is: **the rule** — why it matters — `(provenance)`. Provenance is the feature and
review round that found it, so you can judge whether it applies to your case. If a rule
demonstrably does not apply to your feature, say so in the session note rather than
silently skipping it.

## Adding to a playbook

If a review round, a live run, or a red `main` finds a defect class that is not already
here, add it in your **chore** commit, in the same format, with your feature id and round
number as provenance. Prefer extending an existing playbook over creating a new one; keep
each under ~150 lines. If a rule turns out to be wrong, delete it — a stale rule costs more
than a missing one.
