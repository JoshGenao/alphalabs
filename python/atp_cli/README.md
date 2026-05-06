# `atp_cli` — operator CLI contract for ATP

This package is the **contract** for the CLI described by ``API-4`` in
``feature_list.json`` and traced to ``SRS-API-001`` and ``SRS-SAFE-001``
in ``docs/SRS.md`` §7. It contains an argparse-backed runner so the
surface is exercisable today, but every command exits with
``ExitCode.NOT_IMPLEMENTED (64)`` — concrete handlers land with the
downstream features that own each workflow (``EXE-1``, ``ORCH-1``,
``RESV-1``, ``LOG-1``, ``NOTIF-1``).

The contract surface is introspectable at runtime via
``atp_cli.COMMANDS`` and is rendered to a frozen JSON manual at
[`python/atp_cli/manual.json`](./manual.json). The snapshot is verified
by ``tools/cli_check.py``.

## Access policy (SRS-SEC-002)

* `ACCESS_MODEL = "local-shell"` — the CLI runs only under the local
  operator account on the ATP host. There is no remote-CLI mode.
* `AUTH_MODEL = "local-single-user"` — single operator (``StRS C-6``);
  no auth tokens, sessions, or RBAC.
* Entry point: ``atp <group> <command>`` (rendered as
  ``python -m atp_cli`` while the console-script wrapper is unwired).

## Command groups (API-4)

| Group | Commands | SRS trace |
|---|---|---|
| `kill-switch` | `activate`, `status` | SRS-SAFE-001, SRS-API-001 |
| `strategy` | `list`, `show`, `start`, `stop`, `restart`, `rollback` | SRS-ORCH-004, SRS-ORCH-005 |
| `live` | `promote`, `show` | SYS-2c, SYS-2d, SRS-API-001 |
| `hot-swap` | `trigger`, `status` | SRS-RESV-003..006, SYS-49a..e |
| `readiness` | `check`, `wait` | SYS-76, SRS-ARCH-005 |
| `admin` | `logs`, `alerts`, `config`, `version` | SRS-LOG-001, SRS-NOTIF-001 |

Confirmation-required commands (`kill-switch activate`,
`strategy rollback`, `live promote`, `hot-swap trigger`) all enforce
``--confirm`` before invoking their (stub) handlers; missing the flag
exits with ``ExitCode.CONFIRMATION_REQUIRED (3)``.

## Exit codes

| Code | Name | Meaning |
|---|---|---|
| 0 | `OK` | Command completed successfully. |
| 2 | `USAGE_ERROR` | Argparse rejected the invocation. |
| 3 | `CONFIRMATION_REQUIRED` | Irreversible command without `--confirm`. |
| 4 | `NOT_FOUND` | Strategy or resource id is unknown. |
| 5 | `NOT_READY` | Readiness gate not satisfied. |
| 6 | `TIMEOUT` | Watchdog elapsed before completion. |
| 64 | `NOT_IMPLEMENTED` | Handler not yet wired (API-4 contract stub). |

## Sample invocations

```
# List the surface
python -m atp_cli --list

# Show readiness as JSON
python -m atp_cli readiness check --json

# Kill switch refuses without --confirm
python -m atp_cli kill-switch activate         # exit 3
python -m atp_cli kill-switch activate --confirm  # exit 64 (stub)
```

## Regenerating the manual snapshot

The snapshot is byte-frozen: every change to ``COMMANDS`` must be
reflected in ``manual.json``.

```
python3 tools/cli_check.py --update
git diff -- python/atp_cli/manual.json
```

## Verification

```
python3 tools/cli_check.py        # → "API-4 PASS"
python3 -m unittest tests.test_cli
```
