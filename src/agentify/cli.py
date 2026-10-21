"""Operator CLI. Read-only developer tools for now; `serve` arrives with the MCP server."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table
from rich.tree import Tree

from agentify import __version__
from agentify.adapters import create_adapter, platform_support
from agentify.adapters.base import AdapterError, Element, Selector
from agentify.config import load_settings
from agentify.observability import configure_logging

app = typer.Typer(help="Agentify developer CLI", no_args_is_help=True, add_completion=False)
console = Console(stderr=False)
err = Console(stderr=True)


@app.callback()
def _main(
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Debug logging to stderr")
    ] = False,
) -> None:
    # Paths may contain non-ASCII characters; legacy console code pages (cp1252) cannot encode them.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    settings = load_settings()
    configure_logging("DEBUG" if verbose else settings.log_level, settings.log_format)


def _fail(exc: AdapterError) -> None:
    err.print(f"[red]{exc.code}[/red]: {exc.message}")
    if exc.details:
        err.print_json(json.dumps(exc.details, default=str))
    raise typer.Exit(2)


def _selector(
    automation_id: str | None,
    slug: str | None,
    role: str | None,
    name: str | None,
    exact: bool,
    index: int | None,
) -> Selector:
    try:
        return Selector(
            automation_id=automation_id, slug=slug, role=role, name=name, exact=exact, index=index
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _settings_for(data_dir: Path | None) -> Any:
    settings = load_settings()
    if data_dir is not None:
        settings = settings.model_copy(
            update={
                "data_dir": data_dir,
                "db_path": data_dir / "agentify.sqlite",
                "skills_dir": data_dir / "skills",
                "screenshots_dir": data_dir / "screenshots",
            }
        )
    return settings


@app.command()
def serve(
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir", help="Skill/evidence store (default: AGENTIFY_DATA_DIR or ./data)"
        ),
    ] = None,
    app_key: Annotated[
        str | None,
        typer.Option("--app", help="Serve one wrapped app only: its skills as plain-named tools"),
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="MCP server name")] = None,
    explore_scope: Annotated[
        str | None,
        typer.Option(
            "--explore-scope", help="Explorer sub-agent mode: learning tools for this app only"
        ),
    ] = None,
) -> None:
    """Run the Agentify MCP server over stdio (logs go to stderr).

    Without --app: the learning server (discover/learn/update + all skills).
    With --app <app_key>: a standalone MCP service for that app.
    """
    import anyio

    from agentify.mcp import build_app, serve_stdio

    settings = _settings_for(data_dir)
    adapter = create_adapter(settings=settings)
    app = build_app(
        adapter, settings, app_key=app_key, server_name=name, explore_scope=explore_scope
    )
    anyio.run(serve_stdio, app)


@app.command("mcp-config")
def mcp_config(
    app_key: Annotated[
        str | None, typer.Option("--app", help="Wrapped app key (omit for the learning server)")
    ] = None,
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    name: Annotated[str | None, typer.Option("--name")] = None,
) -> None:
    """Print ready-to-paste MCP configuration for this server (Claude Code / Claude Desktop / others)."""
    exe = Path(sys.executable).with_name("agentify.exe" if sys.platform == "win32" else "agentify")
    store = (data_dir or _settings_for(None).data_dir).resolve()
    server = name or (f"agentify-{app_key.lower()}" if app_key else "agentify")
    args = ["serve", "--data-dir", str(store)]
    if app_key:
        args += ["--app", app_key.lower()]
    if name:
        args += ["--name", name]
    entry = {"command": str(exe), "args": args, "env": {"WINAPP_CLI_TELEMETRY_OPTOUT": "1"}}
    console.print_json(json.dumps({"mcpServers": {server: entry}}))
    quoted = " ".join(f'"{a}"' if " " in a else a for a in args)
    console.print(f'claude mcp add {server} -- "{exe}" {quoted}', soft_wrap=True, markup=False)


@app.command()
def catalog(
    data_dir: Annotated[Path | None, typer.Option("--data-dir")] = None,
    html_out: Annotated[
        Path | None, typer.Option("--html", help="Also write an HTML page to this path")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    serve_live: Annotated[
        bool, typer.Option("--serve", help="Serve a live page (refreshes every 2 s)")
    ] = False,
    port: Annotated[int, typer.Option("--port")] = 8765,
    scan: Annotated[
        Path | None,
        typer.Option("--scan", help="With --serve: also show the newest stores in this folder"),
    ] = None,
    latest: Annotated[int, typer.Option("--latest", help="How many stores --scan shows")] = 1,
) -> None:
    """Show the MCP services Agentify generated: each app's tools as MCP clients see them."""
    import anyio

    from agentify.catalog import build_catalog, to_html, to_markdown

    if serve_live:
        from agentify.catalog_live import serve

        dirs = [data_dir.resolve()] if data_dir else []
        serve(dirs, scan.resolve() if scan else None, latest, port)
        return
    store = (data_dir or _settings_for(None).data_dir).resolve()
    cat = anyio.run(build_catalog, store)
    if html_out is not None:
        html_out.write_text(to_html(cat), encoding="utf-8")
    if as_json:
        print(json.dumps(cat, indent=2, ensure_ascii=False, default=str))
    else:
        print(to_markdown(cat))


@app.command()
def version() -> None:
    """Print the Agentify version."""
    console.print(__version__)


@app.command()
def platforms() -> None:
    """Show which platform adapters are supported."""
    table = Table("platform", "status")
    for k, v in platform_support().items():
        table.add_row(k, v)
    console.print(table)


@app.command()
def doctor() -> None:
    """Check the local runtime prerequisites."""
    from agentify.adapters.windows import WinAppCliRunner
    from agentify.adapters.windows.win32 import is_interactive_session

    settings = load_settings()
    rows: list[tuple[str, bool, str]] = []
    rows.append(("python 3.12", sys.version_info[:2] == (3, 12), sys.version.split()[0]))
    rows.append(("windows", sys.platform == "win32", sys.platform))
    rows.append(
        (
            "interactive session",
            is_interactive_session(),
            "input injection needs an unlocked console session",
        )
    )
    try:
        v = WinAppCliRunner(settings.winapp_path).version()
        rows.append(("winapp cli", bool(v), f"{v} (pinned {settings.winapp_pinned_version})"))
        rows.append(
            (
                "winapp version pinned",
                v == settings.winapp_pinned_version,
                "re-verify command syntax if different",
            )
        )
    except AdapterError as exc:
        rows.append(("winapp cli", False, exc.message))
    table = Table("check", "ok", "detail")
    for name, ok, detail in rows:
        table.add_row(name, "[green]yes[/green]" if ok else "[yellow]no[/yellow]", detail)
    console.print(table)


@app.command()
def apps(
    show_hidden: Annotated[bool, typer.Option("--hidden", help="Include hidden windows")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List candidate application windows."""
    try:
        found = create_adapter().list_applications(include_hidden=show_hidden)
    except AdapterError as exc:
        _fail(exc)
        return
    if as_json:
        console.print_json(json.dumps([a.model_dump() for a in found]))
        return
    table = Table("app_id", "pid", "title", "class", "size", "fg")
    for a in found:
        table.add_row(
            a.app_id,
            str(a.pid),
            a.window_title,
            a.window_class or "",
            f"{a.width}x{a.height}",
            "*" if a.is_foreground else "",
        )
    console.print(table)


def _add(tree: Tree, e: Element) -> None:
    label = f"[bold]{e.role}[/bold]"
    if e.name:
        label += f" {e.name!r}"
    if e.automation_id:
        label += f" [cyan]#{e.automation_id}[/cyan]"
    if e.value is not None:
        label += f" = {e.value!r}"
    if e.slug:
        label += f" [dim]({e.slug})[/dim]"
    node = tree.add(label)
    for c in e.children:
        _add(node, c)


@app.command()
def inspect(
    app_id: Annotated[str, typer.Argument(help="process:hwnd from `agentify apps`")],
    depth: Annotated[int, typer.Option(min=1, max=60)] = 30,
    interactive: Annotated[
        bool,
        typer.Option("--interactive", help="Only interactive elements (hides WebView content)"),
    ] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Print the UI tree of an application window."""
    adapter = create_adapter()
    try:
        handle = adapter.attach(app_id)
        obs = adapter.observe(handle, depth=depth, interactive_only=interactive)
    except AdapterError as exc:
        _fail(exc)
        return
    if as_json:
        console.print_json(obs.model_dump_json())
        return
    for w in obs.windows:
        tree = Tree(
            f"[bold magenta]{w.info.title}[/bold magenta] hwnd={w.info.hwnd} scale={w.info.scale}"
        )
        for e in w.elements:
            _add(tree, e)
        console.print(tree)


@app.command()
def find(
    app_id: str,
    name: Annotated[str | None, typer.Option()] = None,
    role: Annotated[str | None, typer.Option()] = None,
    automation_id: Annotated[str | None, typer.Option("--automation-id")] = None,
    slug: Annotated[str | None, typer.Option()] = None,
    exact: bool = False,
) -> None:
    """Find elements matching a selector."""
    adapter = create_adapter()
    try:
        handle = adapter.attach(app_id)
        matches = adapter.find_elements(
            handle, _selector(automation_id, slug, role, name, exact, None)
        )
    except AdapterError as exc:
        _fail(exc)
        return
    table = Table("role", "name", "automation_id", "value", "slug")
    for e in matches:
        table.add_row(e.role, e.name or "", e.automation_id or "", e.value or "", e.slug or "")
    console.print(table)


@app.command()
def value(
    app_id: str,
    name: Annotated[str | None, typer.Option()] = None,
    role: Annotated[str | None, typer.Option()] = None,
    automation_id: Annotated[str | None, typer.Option("--automation-id")] = None,
    slug: Annotated[str | None, typer.Option()] = None,
    exact: bool = False,
    index: Annotated[int | None, typer.Option()] = None,
) -> None:
    """Read the current value of one element (read-only)."""
    adapter = create_adapter()
    try:
        handle = adapter.attach(app_id)
        result: Any = adapter.read_value(
            handle, _selector(automation_id, slug, role, name, exact, index)
        )
    except AdapterError as exc:
        _fail(exc)
        return
    console.print(result if result is not None else "[dim]<none>[/dim]")


@app.command()
def screenshot(
    app_id: str,
    out_dir: Annotated[Path | None, typer.Option("--out-dir")] = None,
) -> None:
    """Capture a screenshot of an application window."""
    adapter = create_adapter()
    if out_dir is not None and hasattr(adapter, "screenshots_dir"):
        adapter.screenshots_dir = out_dir
    try:
        path = adapter.capture_screenshot(adapter.attach(app_id))
    except AdapterError as exc:
        _fail(exc)
        return
    console.print(str(path))


if __name__ == "__main__":  # pragma: no cover
    app()
