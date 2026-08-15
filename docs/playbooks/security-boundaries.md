# Security boundaries — bind policy, container isolation, proxy seams

Every rule here comes from a static check that reported PASS over a real bypass.

## Network binding (SEC-002, 4 rounds)

1. **Reconcile ALL docs to ONE contract first.** `SECURITY.md` said "never public" while
   `DEPLOYMENT.md` still called a raw public bind "an explicit non-default". The settled
   contract is **proxy-only**: the process exposes no public-bind mode; public reachability
   comes from an operator-configured authenticated reverse proxy. Do not add a public-bind
   toggle — an uncovered capability gets no surface.
2. **`${VAR:-loopback}` interpolation is NOT proof of safe exposure.** Resolving only the
   default over-claims: an operator can set `ATP_IB_HOST=0.0.0.0`. Split the claim — the
   requirement's actual subject must publish a FIXED loopback host with no `${}`; the
   broader guard is honestly scoped to DEFAULT hosts plus "no bare `PORT:PORT`", and its
   evidence string must not claim overrides are constrained.
3. **"External host" means non-RFC1918, i.e. `is_global`.** A test that connects to any
   non-loopback IPv4 hits the LAN address behind NAT — not the AC. On a NAT'd host that
   test cannot run: SKIP with an explicit deferred reason (never a silent green), keep the
   RFC1918 refusal as a labelled proxy, and classify the feature serialized.
4. **Reuse the converged bind helpers** (`is_allowed_bind_host`); do not reinvent, and do
   not switch to `is_private`.

## Container isolation (SEC-003, 7 rounds — a text parser keeps losing to Compose)

Each round found a Compose-VALID way to express an unsafe setting that the parser reported
as PASS. A compose text-check must handle all of these or refuse:

5. **Volume syntax:** short (`src:tgt:ro`), long (`type: bind / source: / target:`), AND
   flow (`[a, b]`). A long-form bind slips a host mount past a short-only parser.
6. **Filesystem is a strict ALLOW-list, not a deny-list.** The template is cloned per
   strategy, so ANY extra shared named volume is a cross-strategy channel.
7. **"No host network" ≠ absence of `network_mode: host`.** The default bridge still has
   egress. Require a dedicated network declared `internal: true`, and read the network's
   DIRECT-CHILD `internal:` scalar — a nested `labels: {internal: true}` under
   `internal: false` must not masquerade.
8. **`cap_add`: reject ANY non-empty**, not a hazardous deny-list. `cap_add: [CHOWN]`
   regains privilege too.
9. **Normalize booleans** (`true` / `"true"` / `yes` / `on`) — a quoted `privileged: "true"`
   beats a substring check.
10. **Refuse duplicate keys** at service indent: Docker is last-wins, so a second
    `privileged:` silently overrides.
11. **Anything you cannot statically resolve FAILS CLOSED** — a service-level `<<:` merge,
    a `*alias` or `${VAR}` on a security key, and `extends:` can each inject unseen values.
    (The template's legitimate merge is nested under `environment:`, not a security key.)
12. **Do NOT add PyYAML to fix this.** A hard `import yaml` enters `architecture_check`'s
    import chain and breaks every worktree that installs only `requirements.txt`
    (`init.sh` skips `requirements-dev.txt`). Harden the text parser and fail closed; the
    gated docker-inspect integration test is the effective-config proof.

## Jupyter isolation (SEC-004, 4 rounds — the network-peer dimension)

13. **Credential env is a merge ORDER problem, not a presence problem.** YAML merge is
    earlier-wins, so assert the blanking anchor is listed FIRST, that it blanks every
    catalogued secret, and that no catalogued secret is re-set INLINE (explicit keys
    override a merge regardless of order). `environment` legitimately uses aliases —
    resolve it rather than blanket-refusing.
14. **A peer can share Jupyter's network NAMESPACE** with `network_mode:
    service:phase1-jupyter` or `container:…` and get a localhost path without declaring a
    shared network. Refuse it peer-side, and fail closed on any alias or interpolation in a
    peer `network_mode`.
15. **Parse `networks:` in list AND map AND flow form.** A map-style attachment reads as
    `[]` to a list-only parser, i.e. "on default" — a fail-open.
16. **A live-control-bearing dashboard IS an execution peer.** `phase1-dashboard-api` hosts
    the kill-switch / live-designation / Hot-Swap REST surface. And do not document the
    deferred dashboard→Jupyter proxy as "joins dashboard-api to the research net" — that
    contradicts the isolation; frame it as a one-way boundary the checker enforces.
17. **Integrate gotcha:** the honesty guard keyword-scans `steps[]` and false-positives on
    AC wording like "cannot submit live orders" / "live trading credentials". For a pure
    static security-inspection feature verified solo, `--mode complete --force-complete` is
    the honest path.

## Same-origin proxy embeds (RES-001, 4 rounds)

18. **Strip `Authorization` upstream** — a token-less research upstream needs none, and in
    the external-auth model it would otherwise be delivered to Jupyter.
19. **Filter `Cookie` to an EXACT reserved allow-list, never a prefix.** A `username-*`
    prefix leaks an operator cookie that merely resembles a Jupyter name. Forward only what
    the real e2e proves necessary, and apply the same filter to BOTH the HTTP and the
    WebSocket handshake path.
20. **Know the irreducible residual and stop.** A `Cookie` header carries no issuer, path,
    or domain, so an exact-name collision on `_xsrf` cannot be resolved by name-matching;
    the only enforced fix breaks JupyterLab's client-side XSRF, and a separate origin bends
    the same-origin requirement. Document it as a reserved-name deployment constraint with
    a regression test — do not loop into a fifth round.
21. **The same-origin browser vector is an operator decision, not a code bug.**
    Notebook-rendered JS shares the dashboard origin and can POST to a confirm-guarded
    endpoint whose guard is a mintable query flag. The ENFORCED boundary is the container
    (credential-less, no execution-network route), not the browser session. Ask the
    operator, write the sign-off gate into `SECURITY.md`, integrate serialized.

## General

22. **Every static security check needs `--fixture` negative self-tests — one per bypass
    class.** A checker with no proof it can fail is the same false green as any other.
23. **Wire the check into `architecture_check.py` and BOTH CI slots.** See
    [contract-drift.md](contract-drift.md) rule 17.

## Public primitives under a guarded API (RESV-006 r24)

- **A `pub` write primitive undoes every invariant its callers enforce.** RESV-006 built five
  guarded writers — validation, same-strategy refusal, O_EXCL lock, monotonicity — over a
  `pub fn save` that had none of them, so the whole guard was one import away from being
  bypassed. Audit the module's exported surface, not just the paths you wrote: `pub(crate)`
  the primitive and make the guarded writers the only door.
- **The tests that resist this are usually the tell.** All three external callers of that
  primitive were the feature's own tests planting arbitrary records — i.e. the tests were
  reaching around the production path, which is the same defect the reviewer found, wearing
  a test's clothes. Rewriting them through the shipped writers made them better tests and
  cost nothing. If a test genuinely needs an impossible record, write the BYTES, so it reads
  as the hand-built artefact it is. `(RESV-006 r24)`
