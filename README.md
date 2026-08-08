# ATP — Algorithmic Trading Platform

A single-user platform for running Python-authored trading strategies. Core
runtime services are Rust (`crates/`); user strategies and the Strategy API are
Python (`python/atp_strategy`). Live brokerage integration is Interactive Brokers
Gateway only, and exactly one strategy may trade the live account at a time.

Most of the work here is done by AI coding agents, coordinated by a scheduler in
`tools/agent_pool.py`.

## Start here

| You are | Read |
|---|---|
| **An AI agent** | [`AGENTS.md`](AGENTS.md) — navigation, architecture, the parallel-agent protocol. `CLAUDE.md` is loaded automatically. |
| **A human, new to the project** | [`docs/StRS_v0.7.md`](docs/StRS_v0.7.md) — the stakeholder vision and why this exists. |
| **Looking for the requirements** | [`docs/README.md`](docs/README.md) — the StRS → SyRS → SRS → `feature_list.json` chain. |
| **Wondering what's done** | `python3 tools/agent_pool.py status` |

## Running it

```bash
./init.sh          # venv, build, dev server, contract checks — prints "✓ Environment ready"
./init.sh --full   # additionally runs the cargo-strict contract scope
```

Verification: `tools/gates.json` lists every gate and
`tools/verify_contracts.sh --scope ci` runs them; `tools/run_ci_locally.sh`
mirrors CI exactly. See "What 'correct' means" in `AGENTS.md`.

## Layout

```
crates/          Rust core runtime services
python/          Strategy API + user-authored strategies
tests/           L1 unit · L2 property · L3 contract · L4 boundary
                 L5 integration · L6 e2e · L7 domain
tools/           checks, the agent scheduler, the critic, the gate registry
docs/            requirements chain + docs/playbooks/ (agent lessons)
progress.d/      per-session resume notes; progress.txt is the folded archive
architecture/    machine-checked architecture boundary (runtime_services.json)
```
