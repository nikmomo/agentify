Recorded WinApp CLI 0.7.1 payloads (2026-10-15, Windows 11 25H2) from `winapp ui ... --json`
against `tests/fixtures/ui_change_fixture/fixture_app.ps1 -Version A` (system-window entries in
list_windows are representative). Unit tests replay these shapes without a desktop.
Each file: {"exit": int, "stdout": json|null, "stderr": json|null}.
