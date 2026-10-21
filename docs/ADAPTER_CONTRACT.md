# Platform Adapter Contract

Status: **implemented** (2026-10-15). The code is the source of truth; this document explains it.

| Piece | Module |
|---|---|
| Protocol | `src/agentify/adapters/base/protocol.py` |
| Models | `src/agentify/adapters/base/models.py` |
| Typed errors | `src/agentify/adapters/base/errors.py` |
| Selector semantics (shared by all adapters) | `src/agentify/adapters/base/selectors.py` |
| Keyboard deny list | `src/agentify/adapters/base/keys.py` |
| Windows (WinApp CLI 0.7.1) | `src/agentify/adapters/windows/` |
| Fake (unit tests only) | `src/agentify/adapters/fake/` |
| Browser / macOS / Linux | `src/agentify/adapters/unsupported.py` (raise `UnsupportedPlatformError`) |
| Construction | `src/agentify/adapters/registry.py` (`create_adapter`, `platform_support`) |

## Protocol

```python
class PlatformAdapter(Protocol):
    platform: str
    def list_applications(self, *, include_hidden: bool = False) -> list[AppInfo]: ...
    def attach(self, app_id: str) -> AppHandle: ...
    def observe(self, app, *, root: Selector | None = None, depth: int = 30,
                interactive_only: bool = False) -> Observation: ...
    def find_elements(self, app, selector, *, max_results: int = 20) -> list[Element]: ...
    def perform(self, app, action: Action, *, timeout_ms: int | None = None) -> ActionResult: ...
    def read_value(self, app, selector) -> str | None: ...
    def get_property(self, app, selector, name: str) -> Any: ...
    def capture_screenshot(self, app, selector: Selector | None = None) -> Path: ...
    def read_state(self, app) -> AppState: ...
```

Rules:

1. Every call is bounded (per-call timeout plus CLI startup grace).
2. Failures are typed `AdapterError` subclasses, never raw exceptions.
3. Adapters never retry. `Action.idempotent` is carried for the runtime, which owns retry policy.
4. Adapters know nothing about skills, workflows, or applications by name.
5. Adapters never launch or terminate applications.
6. `send_keys` is checked against the keyboard deny list **before** reaching the backend.

## Identity: app_id

`app_id = "<process_name>:<hwnd>"` and is session-scoped. `attach` re-reads `status` and raises `WindowGone` if the hwnd now belongs to a different process (handle reuse). Skills must reference applications by a stable `app_key` (process name), never by `app_id`.

## Selector semantics

```python
Selector(automation_id=None, slug=None, role=None, name=None, exact=False, root=None, index=None)
```

- At least one of `automation_id`, `slug`, `name`.
- Strategies in order: `automation_id`, then `slug`, then `role_name`. The first strategy that yields candidates decides; `ActionResult.resolved_by` names it.
- `name` matches case-insensitively with whitespace normalized; substring unless `exact=True`.
- More than one match and no `index` raises `AmbiguousSelector` (candidates in `details`).
- `root` scopes the search to a subtree (resolved first, recursively).
- `Element.durable_selector()` gives the selector to persist: stable automation id (plus role/name) if available, else exact role+name, else slug.
- `Element.has_stable_automation_id` is False for purely numeric ids. WinForms uses HWND-derived numbers for unnamed controls and they change per launch.

## Models (summary)

| Model | Key fields |
|---|---|
| `Element` | role, name, automation_id, class_name, slug (session selector), value, bounds, enabled, offscreen, invokable, expand_state, ancestor_path, children, has_more_children |
| `Observation` | app_id, observed_at, windows: `ObservedWindow(info, elements, element_count)`, `iter_elements()` |
| `WindowInfo` | hwnd, title, class_name, dpi, scale, dpi_awareness, coordinate_space |
| `AppInfo` | app_id, platform, process_name, pid, hwnd, window_title, window_class, width, height, owner_hwnd, is_foreground |
| `AppHandle` | app_id, platform, hwnd, pid, process_name, window_title, dpi_scale, dpi_awareness |
| `AppState` | app_id, alive, window_title, is_foreground, width, height, dpi_scale |
| `Action` | type, selector, value, keys, pattern, via, verbatim, button, double, gone, contains, property_name, timeout_ms, idempotent, risk (and `effective_risk`) |
| `ActionResult` | ok, action_type, resolved_by, element, value, screenshot_path, duration_ms, target_hwnd, raw |
| `RiskLevel` | read_only < local_configuration_change < data_write < destructive < external_effect |

Action kinds: `locate, invoke, set_value, send_keys, click, focus, wait_for, scroll_into_view, screenshot, read_value`.

## Errors

| Error | code | Typical cause |
|---|---|---|
| `ElementNotFound` | element_not_found | no strategy matched |
| `StaleSelector` (subclass of ElementNotFound) | stale_selector | the slug's RuntimeId hash no longer matches: **UI changed or app relaunched** |
| `AmbiguousSelector` | ambiguous_selector | multiple matches, no index |
| `ActionNotSupported` | action_not_supported | element lacks the UIA pattern (WinForms ComboBox has no ExpandCollapse) |
| `WindowGone` | window_gone | hwnd closed or reused |
| `AdapterTimeout` | timeout | `wait_for` deadline, or the CLI exceeded its budget; `details.possibly_performed` is True for input commands |
| `PermissionDenied` | permission_denied | keyboard deny list, outside-process target, UIPI, `foreground_not_target` |
| `NoInteractiveDesktop` | no_interactive_desktop | locked or secure desktop |
| `InvalidAction` | invalid_action | malformed action, or the CLI rejected its arguments |
| `AdapterUnavailable` | adapter_unavailable | `winapp` missing |
| `Interrupted` | interrupted | CLI exit 130 |
| `UnsupportedPlatformError` | unsupported_platform | browser / macOS / Linux adapters |

## WinAppCliAdapter mapping (verified against WinApp CLI 0.7.1)

All commands are scoped with `-w <hwnd>` and use `--json`.

| Operation | CLI |
|---|---|
| `list_applications` | `list-windows --json [--show-hidden]`; Windows shell surfaces (Start, Search, TextInputHost, Program Manager, untitled windows) are filtered unless `include_system_windows` |
| `attach` | `status -w <hwnd> --json`, then one discarded `inspect -w <hwnd> --depth 30 --json` to warm Chromium accessibility trees (`warm_accessibility=True`) |
| `read_state` | `list-windows --json --show-hidden`, look up the hwnd |
| `observe` | `inspect [root] -w <hwnd> --depth N --json [--interactive]`; defaults depth 30, not interactive |
| resolve (every selector action) | `search <query> -w <hwnd> --max 50 --json [--type <role>] [--root <sel>]`, then the shared `filter_matches` / `pick`; exit 1 with `matchCount: 0` means no candidates |
| `invoke` | `invoke <cli_sel> -w <hwnd> --json [--action <pattern>]` |
| `set_value` | `set-value <cli_sel> <value> -w <hwnd> --json` |
| `send_keys` | `send-keys <keys> -w <hwnd> --json --via send-input [--target <cli_sel>] [--verbatim]` |
| `click` | `click <cli_sel> -w <hwnd> --json [--double] [--right]` |
| `focus` | `focus [cli_sel] -w <hwnd> --json` |
| `wait_for` | `wait-for <query> -w <hwnd> -t <ms> --json [--type] [--root] [--gone] [--value v [--property P] [--contains]]`, tried per strategy within one deadline |
| `scroll_into_view` | `scroll-into-view <cli_sel> -w <hwnd> --json` |
| `read_value` | `get-value <cli_sel> -w <hwnd> --json`, field `text` |
| `get_property` | `get-property <cli_sel> -p <Name> -w <hwnd> --json`, field `properties[Name]` |
| `capture_screenshot` | `screenshot [cli_sel] -w <hwnd> -o <path> --json` |

**Why resolve-then-act:** `invoke`, `set-value`, and `click` do not accept `--type` or `--root` in 0.7.1 (`invoke ... --type` returns `invalid_arguments`). `search` does, so the adapter resolves with `search` and acts on the session selector the CLI returned.

**Process scope guard:** when an action's JSON reports an `hwnd` different from the attached one (a ComboBox dropdown list is a separate popup window), the adapter compares owning PIDs via `GetWindowThreadProcessId` and raises `PermissionDenied` on mismatch. This guard is detective; prevention comes from always passing `-w`.

**Subprocess policy:** argument lists only; `WINAPP_CLI_TELEMETRY_OPTOUT=1`; `WINAPP_UI_WORKFLOW_ID` when the runner has a workflow id; `CREATE_NO_WINDOW`; timeout is the action timeout plus startup grace. Logged arguments redact `set-value` values and `send-keys` text.

## Observed backend behaviour the adapter codes to

| Behaviour | Handling |
|---|---|
| UIA Invoke on a button that opens a **modal** dialog blocks until the dialog closes | use `click` for modal-opening buttons; invoke timeouts flag `possibly_performed` |
| WinForms ComboBox has no ExpandCollapsePattern; items are in the tree only while open | invoke the child `Button "Open"` (root = ComboBox), then `invoke --action select` on the `ListItem`; or `set_value` on the ComboBox |
| `send-keys` via post-message was ignored by a WinForms TextBox | default `--via send-input` |
| `send-keys alt+f4` was not rejected by the CLI and closed the window | Agentify deny list in `keys.py` |
| Stale slug gives "RuntimeId hash doesn't match, the UI may have changed" | `StaleSelector`; resolution continues with role+name |
| Owned dialogs (WinForms `ShowDialog(owner)`) are reachable with the main window's `-w` | no extra attach needed |
| First UIA query on a fresh WebView2 app returns a shallow tree | warm-up inspect in `attach` |
| WebView2 controls are about 20 levels deep and hidden by `--interactive` | observe defaults: depth 30, not interactive |
| WebView2 ComboBox supports Expand but not Collapse | close with `send_keys esc` |

## FakeAdapter

An in-memory desktop (`FakeDesktop`) of `FakeApp`s built from `FakeNode`s: buttons with handlers, ComboBoxes whose items appear only while expanded, ListItem / RadioButton selection, Edit / Document values, and dialogs as extra windows. It uses the same `strategies` / `filter_matches` / `pick` and the same keyboard deny list as the Windows adapter. Slugs embed a per-launch hash, and `FakeDesktop.relaunch()` changes them. Scenarios: `colour_fixture_app(version="A"|"B")` mirrors the real fixture, and `simple_editor_app()` is a generic editor.

It is never cited as evidence of Windows support.
