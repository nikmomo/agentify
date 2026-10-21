# Workflow Intermediate Representation

> **Implemented (2026-10-17, MVP):** `src/agentify/workflows/` (`models.py`, `template.py`, `loader.py`, `verify.py`, `executor.py`).
>
> Differences from the sketch below:
> - `preconditions` and `on_failure` are **not implemented** in the MVP, which has no repair path.
> - Statuses used: `candidate`, `active`, `superseded`, `rejected`.
> - Input types: `string`, `integer`, `boolean`, `enum`.
> - `Expectation` adds:
>   - `property: value|name` (labels are read by Name);
>   - `contains`;
>   - `normalize: none|trim|casefold`;
>   - `timeout_ms` (polls for async UI).
> - `SelectorSpec` is the adapter `Selector` plus one `fallback`. Slugs are never persisted by learning.
> - YAML is written without default-valued fields, except for `provenance`, which is always written in full.
>
> Binding rules, all enforced:
> - Every input must be bound to at least one step.
> - The literal is the input's `example` and is replaced as a whole token.
> - A literal found in a step's value or keys keeps that step's stable selector. Otherwise it templates the element name, as for a list item.
> - The literal is also replaced in the final verification.

A workflow is the executable half of a skill. It is a declarative YAML document validated by pydantic models. No code, no expressions, no eval. The only templating is `{{input_name}}` substitution into allowlisted string fields.

## Top-level document

```yaml
schema_version: 1
id: set-target-language            # skill_id; lowercase, hyphens
app_key: translator                # stable application key (process name by default)
version: 1
status: candidate                  # see SKILL_LIFECYCLE.md
name: Set target translation language
description: Sets the target translation language in the application's main window.
risk: local_configuration_change   # read_only | local_configuration_change | data_write | destructive | external_effect

inputs:
  language:
    type: string                   # string | integer | boolean | path | enum
    required: true
    description: Display name of the language as shown in the UI
    example: Japanese
    enum: null                     # optional list of allowed values
    pattern: null                  # optional regex, validated with re.fullmatch

preconditions:
  - type: app_running
  - type: ui_element_exists
    selector: { role: ComboBox, name: "Target language" }

steps:
  - id: s1
    action: invoke
    pattern: expand
    selector:
      slug: cmb-targetlanguage-1a2b
      fallback: { role: ComboBox, name: "Target language" }
    timeout_ms: 5000
    idempotent: true

  - id: s2
    action: wait_for
    selector: { role: ListItem, name: "{{language}}" }
    timeout_ms: 3000

  - id: s3
    action: invoke
    pattern: select
    selector: { role: ListItem, name: "{{language}}", exact: true }
    idempotent: true

verification:
  - type: ui_value_equals
    selector:
      slug: cmb-targetlanguage-1a2b
      fallback: { role: ComboBox, name: "Target language" }
    expected: "{{language}}"
    normalize: trim

on_failure:
  policy: stop                      # stop | rollback_steps (future) ; never "retry_all"
  classify: true                    # run the failure classifier
  max_repair_attempts: 2

provenance:
  source_execution_id: exec_01J...
  created_at: 2026-10-17T10:12:00Z
  created_by: host-agent
  parent_version: null
  notes: "Extracted from exploratory run; combobox slugs recorded with role+name fallbacks."
```

This example is illustrative; real selectors come from a recorded trace.

## Models (pydantic sketch)

```python
RiskLevel = Literal["read_only","local_configuration_change","data_write","destructive","external_effect"]
Status = Literal["discovered","candidate","validated","active","degraded","superseded","rejected"]

class InputSpec(BaseModel):
    type: Literal["string","integer","boolean","path","enum"]
    required: bool = True
    description: str = ""
    example: str | int | bool | None = None
    enum: list[str] | None = None
    pattern: str | None = None

class SelectorSpec(Selector):          # extends adapter Selector
    fallback: Selector | None = None   # tried when the primary fails to resolve

class Step(BaseModel):
    id: str
    action: ActionKind
    selector: SelectorSpec | None = None
    pattern: str | None = None
    value: str | None = None           # templated
    keys: str | None = None            # templated
    via: str | None = None
    verbatim: bool = False
    timeout_ms: int = 5000
    idempotent: bool = True
    optional: bool = False             # failure of an optional step does not fail the workflow
    expect: Expectation | None = None  # intermediate state check after the step

class Workflow(BaseModel):
    schema_version: Literal[1]
    id: str; app_key: str; version: int; status: Status
    name: str; description: str; risk: RiskLevel
    inputs: dict[str, InputSpec]
    preconditions: list[Precondition]
    steps: list[Step]                  # 1..AGENTIFY_MAX_ACTIONS_PER_EXECUTION
    verification: list[Expectation]    # at least one
    on_failure: OnFailure
    provenance: Provenance
```

## Templating rules

- Syntax: `{{name}}` where `name` matches `[a-z][a-z0-9_]*` and is a declared input.
- Allowed fields: `Step.value`, `Step.keys`, `Selector.name`, `Expectation.expected`, `Expectation.path`, `Precondition.path`.
- Substitution is literal string replacement after input validation (type, required, enum, pattern). No filters, no expressions, no nesting.
- A template referencing an undeclared input is a validation error at load time.
- Values are never re-interpreted as templates after substitution (no second pass).

## Validation (load time)

1. YAML parsed with `yaml.safe_load` only.
2. pydantic strict mode; unknown fields rejected.
3. Step ids unique; `steps` non-empty and ≤ max actions.
4. Every `{{name}}` reference resolves to an input.
5. `verification` non-empty.
6. Selectors have at least one of `automation_id | slug | name`; a `slug`-only selector without `fallback` emits a warning (slugs are session-scoped).
7. `send_keys` steps must not contain system key combos (`win+l`, `ctrl+alt+del`, `alt+f4`, …) unless `risk >= destructive` and approval is configured.

## Execution semantics

For each step in order:

1. Resolve selector (`automation_id → slug → fallback → role+name`), record `resolved_by`.
2. Check risk against the approval threshold; stop with `approval_required` if needed.
3. Perform via adapter with `timeout_ms`.
4. If `checks` is set (a list of Expectations), verify them right after the step; a failed check stops the run as `verification_failed` with the step id. Learning creates step checks from input-dependent checks verified before later steps (e.g. before navigating back to the start screen).
5. On failure: if `optional`, continue; else stop, classify, record, and return. Never continue past a failed non-optional step.

After all steps: run every verification; the execution is `verified` only if all pass.

## Parameter inference during extraction

`propose_skill` receives explicit `bindings` from the host agent (`{"language": ["s3"]}` meaning "the literal in step s3's selector name is the `language` input"). The runtime replaces that literal with `{{language}}` in the bound fields and also in any verification `expected` value equal to the same literal. The runtime does not guess bindings in the MVP; the host agent does, which keeps extraction deterministic and inspectable.

## Outputs

A workflow can return values that the app shows after the task, for example a translation.

```yaml
outputs:
  translation:
    selector: { role: Text, index: -1 }   # negative index counts from the end: the newest entry
    property: name                        # name (labels, text) or value (edits, combos)
    description: The translated text
```

Outputs are read only after every check passes, and they are returned as `outputs` in the run
result. `learn_skill(outputs=...)` takes either the trace index of a successful `read_value`
step or an explicit `{selector, property, description}`. An output that cannot be read is
returned as `null`; the run's `verified` does not depend on it.
