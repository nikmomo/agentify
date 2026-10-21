# UI-Change Fixture Specification

Purpose: a tiny independent Windows app that performs one business operation in two UI layouts, so learning and UI-change repair can be tested deterministically. It is test infrastructure, not evidence of generalization to real software.

## Business operation

"Set the profile colour to X and save." Observable postcondition: a label `Current colour: X` and a file `%TEMP%\agentify_fixture\profile.json` containing `{"colour": "X"}`.

## Implementation

Implemented as `fixture_app.ps1` (PowerShell + WinForms). Launch:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tests\fixtures\ui_change_fixture\fixture_app.ps1 -Version A -StateDir $env:TEMP\agentify_fixture
.\scripts\dev.ps1 fixture -Version B
```

Verified UIA surface (WinApp CLI 0.7.1): `Edit "Notes"` (#NotesBox), `ComboBox "Colour"` (#ColourCombo, child `Button "Open"`, items as `ListItem` only while open), `Button "Save"` (#SaveButton), `Text` (#StatusLabel). vB: `Button "Preferences..."` (#PreferencesButton) opens an owned modal `Preferences` dialog with `RadioButton`s and `Button "Apply"`. Use `click`, not `invoke`, on `Preferences...`: UIA Invoke blocks while the modal dialog is open.

## Version A layout

| Control | UIA type | Name / AutomationId |
|---|---|---|
| ComboBox | ComboBox | name `Colour`, items `Red`, `Green`, `Blue` |
| Button | Button | name `Save` |
| Label | Text | name `Current colour: <X>` |

## Version B layout (same operation, different structure)

| Control | UIA type | Name / AutomationId |
|---|---|---|
| Button | Button | name `Preferences…` → opens a child dialog `Preferences` |
| in dialog: RadioButtons | RadioButton | names `Red`, `Green`, `Blue` |
| in dialog: Button | Button | name `Apply` |
| Label (main) | Text | name `Profile colour is <X>` |

Differences deliberately cover: control type change (ComboBox → RadioButton), label rename (`Save` → `Apply`), an extra navigation step (dialog), and a verification text change.

## Verification helpers

The fixture writes `profile.json` atomically on Save/Apply so `file_text_equals` is a reliable postcondition independent of the label text.
