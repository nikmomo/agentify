# MCP API Specification (implemented, MVP)

- **Transport:** stdio.
- **Server name:** `agentify`.
- **Start:** `agentify serve [--data-dir DIR]`.
- **Implementation:** `src/agentify/mcp/server.py`.

All tools return JSON objects. On failure the tool result has `isError=true`, and its text is `Error executing tool <name>: {"error": {"code", "message", "details"}}`. The prefix comes from the MCP SDK.

The MVP covers three verbs:
- **discover**: inspect a running app.
- **learn**: turn a verified exploration into a skill, which immediately becomes an MCP tool.
- **update**: create the next version of a skill, and with it the next version of its tool.

UI-change repair, degradation, and rollback are out of scope for the MVP.

## Stable tools

| Tool | Verb | Purpose | Side effects |
|---|---|---|---|
| `list_apps()` | discover | Top-level windows, each with `app_id`, `app_key` (process name, lowercase), and the number of learned skills | none |
| `inspect_app(app_id, max_elements=150, depth=30, query?)` | discover | Compact accessibility outline, one line per meaningful element: `[n] Role 'Name' #AutomationId value='...' label='...' (state)`. Numeric per-launch automation ids are hidden. `label` marks an unnamed control that is the first of its role after a text label; `{role, label}` selects it. `query` keeps only matching lines. | none |
| `start_learning(app_id, goal, observe=true)` | learn | Opens a recorded, bounded exploration and returns `execution_id` plus the UI outline (`ui`), so no separate inspect call is needed | DB insert |
| `perform_action(execution_id, action? \| actions?, approved=false, observe=true, settle_ms=500, query?)` | learn | One action, or a list run in order that stops at the first failure. An item `{"verify": Expectation}` checks the UI at that point. Returns per item `index` (the step index used in bindings), `ok`, `element`, `value`, `error`, plus `ui_after`: the outline after the last item (waits `settle_ms` first). A failed action is recorded, not raised. | UI input, trace append |
| `verify_outcome(execution_id, expectation)` | learn | Checks live state (see Expectation) and returns `{passed, observed, expected, ...}` | trace append |
| `learn_skill(execution_id, name, description, inputs?, bindings?, skill_id?, keep_steps?, verify?, outputs?, optional_steps?)` | learn | `outputs` declares the values the capability returns (WORKFLOW_IR.md, Outputs). `verify` checks the final state first; if a check fails nothing is learned (`learned: false` + results). Compiles the trace into Workflow v1 (`candidate`), registers the generated tool, and announces `tools/list_changed`. Closes the execution. | DB + YAML write, tool list change |
| `abandon_learning(execution_id, notes?)` | learn | Closes an exploration without learning; the trace is kept as evidence | DB update |
| `list_skills(app_key?)` | manage | Skill summaries: status, `serving_version`, inputs, versions with run counts, `tool_name` | none |
| `run_skill(skill_id, params?, version?, app_id?)` | manage | Replays a skill. With `version`, runs a specific version, e.g. a new candidate. | UI input, DB |
| `update_skill(skill_id, execution_id?, name?, description?, inputs?, bindings?, keep_steps?, verification?)` | update | Creates version N+1 and refreshes the generated tool. `verification` replaces the checks, e.g. to remove a learned counter check. `learn_skill` and `update_skill` return `warnings` for constant checks that do not depend on any input. | DB + YAML write, tool list change |
| `get_skill(skill_id, version?)` | manage | Returns the workflow YAML | none |
| `get_execution(execution_id)` | manage | Returns the execution record: trace, verifications, outcome | none |

Capability tools (full server only):

| Tool | Purpose |
|---|---|
| `explore_app(app, goal, max_actions=40, max_minutes=10)` | Runs the Explorer sub-agent (`claude -p`, OAuth, scoped to one app) to discover, verify, and compile a capability. Before publishing, Agentify replays it: once with other values for inputs that have a closed set of choices (`value_map`/`enum`), then with the learned values, which restores the state the goal asked for. If a replay fails, the evidence goes back to the Explorer as a repair task, and the repaired version is verified again (at most `AGENTIFY_EXPLORE_MAX_REPAIRS`, default 0). A version that still fails is rejected, and its tool is removed unless an older verified version serves. The result lists every `iterations[]` entry. |
| `improve_capability(capability_id, problem, failing_arguments?, max_minutes=10)` (only with `AGENTIFY_ENABLE_IMPROVE_TOOL=true`; off in the MVP) | Hands a problem found during use to the Explorer (repair task: the problem, the arguments, and the workflow YAML). The Explorer creates the next version, and Agentify verifies it, also with `failing_arguments`, before it serves. An unverified run's result names this tool in `next_step`. |
| `list_capabilities(app?)` | Capabilities with tool names, inputs, status, and discovery-vs-replay stats |
| `execute_capability(capability_id, arguments?)` | Same as the generated tool |

The server `instructions` tell the host what to do:
1. Prefer a generated tool when one exists.
2. Otherwise learn: list_apps → inspect_app → start_learning → perform_action… → verify_outcome → learn_skill.
3. Update with update_skill.

## Per-app MCP service

`agentify serve --app <app_key> [--data-dir DIR] [--name NAME]` serves one wrapped application as a standalone MCP server. It is the server to install in Claude Code, Claude Desktop, Cursor, or any other MCP host that just wants to *use* the app.

- **Server name:** `agentify-<app_key>`, unless `--name` is given.
- **Instructions:** describe the app and recommend the `*_batch` tools for several items.
- **Tools:** only that app's skills, under plain names. The skill id becomes snake_case, so `add-customer-record` becomes `add_customer_record`, plus `add_customer_record_batch`. There are no learning tools, no namespace prefix, and no other apps.
- **Store sync:** the server shares the skill store with the learning server.
  - Tools are re-synced from the store before every `tools/list`.
  - A 2-second background poll publishes `tools/list_changed` when another process learns, updates, or promotes a skill.
- **Configuration:** `agentify mcp-config --app <app_key> [--data-dir DIR]` prints a ready-to-paste `mcpServers` JSON entry and the matching `claude mcp add` command.

The learning server (`agentify serve` without `--app`) keeps every skill's tools under `<app_key>__<skill_id>` and `<app_key>__<skill_id>_batch`.

## Batch tools

Every skill also gets `<tool>_batch(items: [ {<same fields as the single tool>} ], stop_on_failure=true, app_id?)`.

- Items run in order within a single MCP call, so there are no model round trips between them.
- Execution is serialized with all other UI work by the runtime's UI lock.
- Per-item argument validation, including enums, happens before any UI input.
- Result: `{skill_id, total, attempted, verified, all_verified, llm_calls: 0, duration_ms, results: [{index, verified, failure, duration_ms, execution_id}]}`.
- `stop_on_failure` stops at the first unverified item, so nothing after a failure is entered.

## Generated tools (one per skill)

- **Name:** `<app_key>__<skill_id>`, lowercased, with characters outside `[a-z0-9_-]` replaced by `_`, and at most 64 characters. Example: `agentifyfixture__set-profile`.
- **Arguments:** one per workflow input.
  - `string` maps to a string.
  - `integer` maps to an integer.
  - `boolean` maps to a boolean.
  - An input with `enum` is exposed as a JSON-schema `enum`, which the SDK enforces before any UI input happens.
  - Optional inputs may be omitted.
  - There is always an optional `app_id`. Without it, the runtime picks the foreground (else the largest) window whose process name equals `app_key`.
- **Description:** `[Agentify skill for app '<app_key>', v<N> <status>] <name>. <description> Inputs: ... Replays a verified UI workflow (<k> steps, no screenshots, no LLM calls) ...`
- **Result:** the same as `run_skill`: `{execution_id, skill_id, version, verified, duration_ms, action_count, llm_calls: 0, fallback_used, failure, verification, promoted_to_active, status}`.
- **Served version:** the `active` version, or the newest `candidate` if no version is active. Candidates are exposed on purpose; their verified runs are how they get promoted.
- **Lifetime:**
  - Registered at learn time and re-registered from storage at server start.
  - Replaced in place when the served version, name, description, or inputs change.
  - Every change is announced with `ctx.notify_tools_changed()` for protocol 2026-07-28 clients and with the legacy `notifications/tools/list_changed` for older clients. The server advertises `tools.listChanged=true`.
- **Claude Code behaviour:** newly added tools are *deferred*. The model finds them through tool search, which costs one extra turn.

## Schemas

### Action (`perform_action.action`)

```json
{"type": "invoke|set_value|send_keys|click|focus|wait_for|scroll_into_view|locate|read_value|screenshot",
 "selector": {"automation_id"?: str, "role"?: str, "name"?: str, "exact"?: bool, "label"?: str, "index"?: int, "root"?: Selector},
 "value"?: str, "keys"?: str, "pattern"?: "invoke|select|toggle|toggle-on|toggle-off|expand|collapse",
 "via"?: "send-input|post-message", "button"?: "left|right", "double"?: bool,
 "property_name"?: str, "contains"?: bool, "gone"?: bool, "timeout_ms"?: int, "idempotent"?: bool}
```

A selector needs at least one of `automation_id`, `slug`, or `name`, or a `role` with `label` (the first control of that role after an element named `label`, for unnamed form controls) or with `index` (positional, fragile). `focus` requires a selector.

Each action carries a risk level, with defaults per type (`read_value` is `read_only`; `set_value` and `click` are `local_configuration_change`). An action at or above `AGENTIFY_APPROVAL_THRESHOLD` (default `data_write`) fails with `approval_required` unless the call passes `approved: true`, and the host must obtain that approval from the user.

The guards are the action cap (`AGENTIFY_MAX_ACTIONS_PER_EXECUTION`, default 40) and the time cap (`AGENTIFY_MAX_EXPLORE_SECONDS`, default 1800). Both fail with `limit_exceeded`.

### Expectation (`verify_outcome.expectation`)

```json
{"type": "ui_value_equals", "selector": Selector, "expected": str,
 "property"?: "value|name", "contains"?: bool, "normalize"?: "none|trim|casefold", "timeout_ms"?: int}
{"type": "ui_element_exists|ui_element_absent", "selector": Selector}
{"type": "file_exists", "path": str}
{"type": "file_text_equals", "path": str, "expected": str, "contains"?: bool, "normalize"?: ...}
```

`property: "name"` is for labels and static text. Checks poll for up to `timeout_ms` (default 3000) so that asynchronous UI updates are seen.

### learn_skill inputs and bindings

```json
"inputs":   {"colour": {"type": "string|integer|boolean|enum", "description": "...", "example": "Blue",
                         "enum"?: [...], "pattern"?: "regex", "required"?: true}},
"bindings": {"colour": [3]}
```

- The **literal** of an input is its `example`. If there is no example, it is the bound step's `value`, then `keys`, then selector name.
- In each bound step, whole-token occurrences of the literal in `value` and `keys` are replaced with `{{colour}}`. If the literal is not in either, it must be in the element name, for example a list item, and then the selector becomes `role` + `{{colour}}`.
- The same literal is also replaced in the final verification's `expected` value and in its selector name.
- A passed check made before later kept steps (for example, verify a value, then navigate Back) becomes a step check (`steps[].checks`) on the last kept step before it, if it depends on an input. Constant mid-way checks are dropped. A workflow needs at least one final or step check.
- Errors (`learn_error`):
  - an input bound to no step;
  - a literal that is not found in its bound step;
  - a binding to a failed or observation-only step;
  - no passed verification after the last kept step;
  - an element with no durable identity.

## Automatic improvement (off by default)

A capability run that fails with a repairable category is repaired in the same call when
`AGENTIFY_AUTO_IMPROVE=true` (default `false`) and the skill's risk is below `data_write`. The
result is then the repaired version's verified run with the caller's arguments, plus `improved`
(the first attempt's failure, iterations, and timings). Otherwise the result carries `next_step`.

## Secret references

Action values, keys, and skill inputs of type `secret` take `${secret:NAME}`. Agentify types the real value (from the environment or `AGENTIFY_SECRETS_FILE`; only names in `AGENTIFY_SECRET_NAMES`) and records only the reference. Secrets cannot be verified or read back.

## Window allowlist

`AGENTIFY_ALLOWED_WINDOWS=<app_id>[,<app_id or hwnd>...]` restricts the server, and the Explorer
sub-agent it starts, to those windows. Other windows are missing from `list_apps`, and acting on
them fails with `permission_denied`.

## Error codes

| Code | Raised for |
|---|---|
| `bad_parameters` | Malformed action or expectation |
| `not_found` | Unknown or closed execution or skill |
| `invalid_state` | Skill already exists; execution closed |
| `limit_exceeded` | Action or time cap reached |
| `approval_required` | Action at or above the risk threshold without approval |
| `learn_error` | Bad learning request (see above) |
| `workflow_invalid` | Invalid workflow |
| `app_not_running` | No running window for the skill's app |
| Adapter codes | `element_not_found`, `ambiguous_selector`, `action_not_supported`, `timeout`, `permission_denied`, `window_gone`, ... |

## Host registration

```powershell
claude mcp add agentify -- "<repo>\.venv\Scripts\agentify.exe" serve --data-dir "<repo>\data"
```
