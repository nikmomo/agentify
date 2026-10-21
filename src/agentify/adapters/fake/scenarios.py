"""Prebuilt fake applications for unit tests.

`colour_fixture_app` mirrors tests/fixtures/ui_change_fixture/fixture_app.ps1 (versions A and B),
so the same skill can be learned on vA and broken by vB in pure-Python tests.
"""

from __future__ import annotations

from pathlib import Path

from agentify.adapters.fake.adapter import FakeApp, FakeDesktop, FakeNode, FakeWindow

COLOURS = ("Red", "Green", "Blue")


def colour_fixture_app(
    desktop: FakeDesktop, *, version: str = "A", state_file: Path | None = None
) -> FakeApp:
    if version not in ("A", "B"):
        raise ValueError("version must be 'A' or 'B'")
    notes = FakeNode(role="Edit", name="Notes", automation_id="NotesBox", value="")
    status = FakeNode(role="Text", automation_id="StatusLabel")
    main = FakeWindow(hwnd=desktop.next_hwnd(), title=f"Agentify Fixture v{version}")
    app = FakeApp(
        process_name="powershell", pid=40000 + main.hwnd, windows=[main], state_file=state_file
    )

    def save(colour: str | None) -> None:
        app.state = {"colour": colour, "notes": notes.value or "", "version": version}
        app.write_state()

    if version == "A":
        combo = FakeNode(
            role="ComboBox",
            name="Colour",
            automation_id="ColourCombo",
            items=list(COLOURS),
            value="Red",
        )
        status.name = "Current colour: (unsaved)"

        def on_save(a: FakeApp, n: FakeNode) -> None:
            save(combo.value)
            status.name = f"Current colour: {combo.value}"

        main.nodes = [
            FakeNode(role="Text", name="Notes", automation_id="788468"),
            notes,
            FakeNode(role="Text", name="Colour", automation_id="393514"),
            combo,
            FakeNode(role="Button", name="Save", automation_id="SaveButton", on_invoke=on_save),
            status,
        ]
    else:
        status.name = "Profile colour is (unsaved)"

        def open_prefs(a: FakeApp, n: FakeNode) -> None:
            radios = [
                FakeNode(
                    role="RadioButton",
                    name=c,
                    automation_id=f"Radio{c}",
                    group="colour",
                    selected=(i == 0),
                )
                for i, c in enumerate(COLOURS)
            ]

            def apply(a2: FakeApp, n2: FakeNode) -> None:
                chosen = next(r.name for r in radios if r.selected)
                save(chosen)
                status.name = f"Profile colour is {chosen}"
                a2.close_window("Preferences")

            dialog = FakeWindow(hwnd=desktop.next_hwnd(), title="Preferences", modal=True)
            dialog.nodes = [
                *radios,
                FakeNode(role="Button", name="Apply", automation_id="ApplyButton", on_invoke=apply),
            ]
            a.open_window(dialog)

        main.nodes = [
            FakeNode(role="Text", name="Notes", automation_id="788468"),
            notes,
            FakeNode(
                role="Button",
                name="Preferences...",
                automation_id="PreferencesButton",
                on_invoke=open_prefs,
            ),
            status,
        ]
    return desktop.add(app)


def simple_editor_app(desktop: FakeDesktop, *, state_file: Path | None = None) -> FakeApp:
    """A generic one-document editor: type into the document, press Save, content is persisted."""
    doc = FakeNode(role="Document", name="Text editor", automation_id="Editor", value="")
    main = FakeWindow(hwnd=desktop.next_hwnd(), title="Untitled - Fake Editor")
    app = FakeApp(
        process_name="fakeeditor", pid=50000 + main.hwnd, windows=[main], state_file=state_file
    )

    def on_save(a: FakeApp, n: FakeNode) -> None:
        a.state = {"text": doc.value or ""}
        a.write_state()
        main.title = "Saved - Fake Editor"

    main.nodes = [
        doc,
        FakeNode(role="Button", name="Save", automation_id="SaveButton", on_invoke=on_save),
    ]
    return desktop.add(app)
