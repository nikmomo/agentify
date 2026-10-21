# Security

Agentify sends real input to real applications. These controls are enforced in the runtime, not
left to the agent.

## Execution boundaries

| Control | Mechanism |
|---|---|
| Window scoping | Actions target the attached window. A target owned by another process is rejected with `permission_denied` (the check uses the owning process because dropdowns are separate popup windows). |
| Window allowlist | `AGENTIFY_ALLOWED_WINDOWS` limits which windows are visible and actionable at all. |
| Action cap | `AGENTIFY_MAX_ACTIONS_PER_EXECUTION` (default 40) per exploration or replay. |
| Time caps | `AGENTIFY_MAX_EXECUTION_SECONDS` (default 180) per replay, `AGENTIFY_MAX_EXPLORE_SECONDS` per exploration. |
| Per-command timeout | Every WinApp CLI call has a timeout (`AGENTIFY_WINAPP_TIMEOUT_MS`). |
| Non-idempotent actions | Never retried automatically. |
| System keys | `send_keys` rejects system combinations such as `win+*`, `alt+f4`, `alt+tab`, `ctrl+alt+del`, `ctrl+shift+esc` (`adapters/base/keys.py`). WinApp CLI's `--allow-system-keys` is never passed. |
| Opening apps | `open_app` only starts apps that have a Start-menu shortcut: no command lines, no arbitrary paths. The adapter never terminates processes. |
| Subprocesses | Argument lists only, never shell strings. |

## Risk and approval

```
read_only < local_configuration_change < data_write < destructive < external_effect
```

Each action and each workflow carries a risk; a workflow's risk is the highest of its steps.
Actions at or above `AGENTIFY_APPROVAL_THRESHOLD` (default `data_write`) need `approved: true`,
which the host must obtain from the user.

## Untrusted UI content

Text read from windows is data. It is returned in structured fields and is never interpreted as
an instruction by Agentify.

## Secrets

- `${secret:NAME}` references let a workflow use credentials without storing them. Only names in
  `AGENTIFY_SECRET_NAMES` are allowed; values are read from the environment or
  `AGENTIFY_SECRETS_FILE` at run time.
- Secret values never appear in workflows, traces, run records, or tool results.
- Typed keys are redacted in logs.
- The runtime needs no model API key. The Explorer uses the Claude Code CLI's own sign-in, and API
  keys are removed from its environment.
- `.env` and `data/` are git-ignored.

## Learning safety

- A version is published only after a verified replay (skipped, and recorded as skipped, when the
  goal state already holds before the replay).
- A single run never promotes a skill to `active` (see `SKILL_LIFECYCLE.md`).
- Workflow versions are immutable; an update creates a new version and keeps the previous one.
- Self-repair (`improve_capability`, `AGENTIFY_AUTO_IMPROVE`) is off by default.

## Out of scope

Malicious applications attacking the agent through accessibility metadata, elevated (UIPI)
targets, multi-user machines, and remote execution.
