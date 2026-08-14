# The harness, end to end

What actually happens between "I want to work on a feature" and `passes: true`.
Companion to `docs/verification-queue.md`, which covers what to do when a feature
*can't* close.

---

## 1. The session — launcher to integrate

Steps 0–8.1 are `prompts/coding_prompt.md`; the agent reads them in order. Nothing
is skipped — every step below exists in that file.

```mermaid
flowchart TD
    OP(["you: tools/claim_and_work.sh"]) --> CLAIM["agent_pool.py claim<br/>under the fcntl pool lock"]
    CLAIM --> SYNC["_sync_primary_checkout<br/>fast-forward to origin/main"]
    SYNC --> PICK{"anything<br/>claimable?"}

    PICK -->|"no"| EMPTY["FEATURE=EMPTY<br/>prints the board + deadlock_advice"]
    EMPTY --> QUEUE(["read docs/verification-queue.md"])

    PICK -->|"yes"| LEASE["take lease + private ports<br/>dev / ib-live / ib-paper"]
    LEASE --> WTQ{"worktree<br/>already there?"}
    WTQ -->|"no"| CUT["cut fresh from origin/main"]
    WTQ -->|"yes"| REFRESH["_refresh_worktree<br/>ff-only if clean + no own commits,<br/>else warn and leave alone"]
    CUT --> OPEN
    REFRESH --> OPEN["claude opens in PLAN MODE<br/>seeded with coding_prompt.md"]

    OPEN --> S0["Step 0 confirm claim + worktree"]
    S0 --> S1["Step 1 orient<br/>verify_queue.py show FID"]
    S1 --> S2["Step 2 start env — ./init.sh"]
    S2 --> S3["Step 3 read the requirement in SRS.md"]
    S3 --> S4["Step 4 check dependencies"]
    S4 --> S41{"Step 4.1<br/>plan approved<br/>by you?"}
    S41 -->|"no"| S4
    S41 -->|"yes"| S5["Step 5 implement"]
    S5 --> BLOCKED{"hit a<br/>blocker?"}
    BLOCKED -->|"yes"| TAX(["blocker taxonomy — diagram 3"])
    BLOCKED -->|"no"| S51["Step 5.1 tests at the right layer"]
    S51 --> S6["Step 6 walk every step through<br/>evidence.py run"]
    S6 --> S61["Step 6.1 critic: deterministic + judgment<br/>both must APPROVE"]
    S61 --> S7["Step 7 commit prep -> feat -> chore"]
    S7 --> S71["Step 7.1 integrate"]
    S71 --> GATE(["the close gate — diagram 2"])
    S71 --> S8["Step 8 session note + Outcome line"]
    S8 --> S81["Step 8.1 write back to the playbooks"]
```

**Step 4.1 is a hard stop.** The session starts read-only; the agent cannot edit
anything until you approve its plan.

---

## 2. The close gate — how a feature reaches `passes: true`

This is the mechanism worth understanding, because it's the one that refuses.
There are exactly **two** legitimate paths, and describing the work is neither.

```mermaid
flowchart TD
    INT(["agent_pool.py integrate FID --mode complete"]) --> HON{"honesty guard<br/>verification_method<br/>== solo?"}

    HON -->|"no"| FORCE{"--force-complete<br/>passed?"}
    FORCE -->|"no"| EX5["exit 5 — use --mode serialized"]
    FORCE -->|"yes"| EV
    HON -->|"yes"| EV{"evidence.verify"}

    EV -->|"every step executed,<br/>exit 0, both critics approve,<br/>image if e2e/live-ib"| REEX{"solo?<br/>integrator RE-RUNS<br/>the recorded argv"}
    EV -->|"anything missing"| DEGRADE["DEGRADES to serialized<br/>code merges, passes stays false"]

    REEX -->|"exit codes match"| CLOSE["close_feature.py --verified<br/>passes := true<br/>note folded, evidence retired"]
    REEX -->|"mismatch"| DEGRADE

    DEGRADE --> HUMAN(["the human path"])
    HUMAN --> ATT["close_feature.py FID --verified<br/>--attested-by operator<br/>or the verified-e2e PR label"]
    ATT --> ATTEV{"evidence.verify<br/>allow_attested=true"}
    ATTEV -->|"record complete"| CLOSE
    ATTEV -->|"no record at all"| REFUSE["exit 3 — refused"]
```

Two things this diagram is drawn to make unmissable:

- **`--force-complete` only bypasses the honesty guard.** The evidence gate still
  runs and still degrades you to `serialized`. It cannot rescue a missing record —
  the old deadlock message advised exactly that, and it looped forever.
- **`--attested-by` relaxes *which* steps count, never *whether* there is a
  record.** A human vouching for the work still has to say what the work was.

---

## 3. Where a blocker goes

One word — "blocked" — used to cover four situations. Putting one in the wrong
home is what produced a bucket of eleven features no agent could claim and no
human could close.

```mermaid
flowchart TD
    Q(["this feature can't reach passes:true"]) --> W{"what is<br/>in the way?"}

    W -->|"nothing — only<br/>evidence is missing"| A["class A — actionable<br/>finish the record and close it"]

    W -->|"another FEATURE<br/>that isn't built"| B{"would the edge<br/>close a loop?"}
    B -->|"no"| BOK["class B<br/>agent_pool.py block FID --on IDS"]
    B -->|"yes"| D["class D — cycle<br/>block REFUSES, exit 13, writes nothing<br/>prints the full path + the one unblock"]

    W -->|"a real-world resource<br/>NO feature owns"| C["class C — external<br/>operator adds external_blocker<br/>to feature_list.json"]

    D --> DEC(["operator decides:<br/>is the reverse edge a CODE edge<br/>or a FLIP edge?"])
    C --> PROC(["appears in status under<br/>blocked on an external resource<br/>= the procurement backlog"])
```

**Never use `block --on` for class C.** A dependency edge asserts some feature owns
the blocker; if none does, the edge never clears and the feature is parked forever.
Class C today: an SMS provider account, a PTP-disciplined host, 30 real market
days, a market-hours restart on Proxmox.

---

## 4. The watch loop

Runs without you, changes nothing, and reports only what moved.

```mermaid
flowchart LR
    CRON(["daily 06:30 UTC"]) --> WF
    PUSH(["push to main touching<br/>feature_list.json,<br/>feature_deps.json,<br/>.harness/runs/"]) --> WF
    MAN(["gh workflow run<br/>verification-watch.yml"]) --> WF

    WF["verification-watch.yml"] --> CHK["verify_queue.py check"]
    CHK -->|"exit 1 — drift"| ISSUE["open or UPDATE one issue<br/>drift first, board second"]
    CHK -->|"exit 0 — clean"| CLOSEI["close that issue"]
```

It detects what a person wouldn't notice: a feature whose last blocker just
closed, one held off the frontier by a note with nothing recording why, an
evidence record invalidated by a re-spec, and any cycle in the graph.

---

## Which command do I run?

```bash
tools/claim_and_work.sh          # auto-picks the highest-impact ready feature
tools/work_on.sh <FEATURE_ID>    # you choose — the only way onto an awaiting-verification feature
```

Both take the pool lock, allocate private ports, materialise the worktree, and
open the session in plan mode. `work_on.sh` additionally bypasses the
ready-frontier and anti-churn filters, which is why it is the operator's tool.

Before either, to see what you're walking into:

```bash
python3 tools/verify_queue.py list          # the ranked queue
python3 tools/verify_queue.py show <ID>     # the full brief for one feature
```
