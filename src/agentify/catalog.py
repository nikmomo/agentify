"""The MCP services Agentify has generated, as their clients see them (`agentify catalog`).

For each wrapped app, the per-app service (`agentify serve --app <key>`) is built in-process and
its tool list is read through a real MCP client: names, descriptions, input and output schemas
exactly as a host receives them. Status and measured efficiency come from the skill store.
Nothing here touches an app.
"""

from __future__ import annotations

import html
import json
import sys
from pathlib import Path
from typing import Any

from agentify.config import Settings


def _settings(data_dir: Path) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": data_dir,
            "db_path": data_dir / "agentify.sqlite",
            "skills_dir": data_dir / "skills",
            "screenshots_dir": data_dir / "screenshots",
            "uia_fast_path": False,
        }
    )


async def build_catalog(data_dir: Path) -> dict[str, Any]:
    from mcp import Client

    from agentify.adapters.fake import FakeAdapter
    from agentify.mcp import build_app
    from agentify.storage import Store

    settings = _settings(data_dir)
    store = Store(settings.db_path, settings.skills_dir)
    exe = Path(sys.executable).with_name("agentify.exe" if sys.platform == "win32" else "agentify")
    services = []
    app_keys = sorted({store.skill_row(sid)["app_key"] for sid in store.skill_ids()})
    for key in app_keys:
        app = build_app(FakeAdapter(), settings, store, app_key=key)  # listing only, no UI
        async with Client(app.server) as c:
            listed = (await c.list_tools()).tools
        tools = []
        for t in listed:
            sid = next(
                (s for s in store.skill_ids(key) if t.name in (app.tool_names(s) or ())),
                None,
            )
            info: dict[str, Any] = {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "output_schema": t.output_schema,
            }
            if sid is not None:
                summary = app.skills.summary(sid)
                info |= {
                    "capability_id": sid,
                    "status": summary["status"],
                    "version": summary["serving_version"],
                    "outputs": summary.get("outputs") or {},
                    "stats": app.skills.stats(sid),
                    "batch": t.name.endswith("_batch"),
                }
            tools.append(info)
        args = ["serve", "--data-dir", str(data_dir.resolve()), "--app", key]
        services.append(
            {
                "server": f"agentify-{key}",
                "app": key,
                "instructions": app.server.instructions,
                "config": {"command": str(exe), "args": args},
                "tools": tools,
            }
        )
    store.close()
    return {"data_dir": str(data_dir.resolve()), "services": services}


def to_markdown(cat: dict[str, Any]) -> str:
    out = [f"# Generated MCP services\n\nData: `{cat['data_dir']}`\n"]
    for svc in cat["services"]:
        single = [t for t in svc["tools"] if not t.get("batch")]
        out.append(f"## `{svc['server']}` ({len(single)} capabilities)\n")
        out.append(
            "```json\n"
            + json.dumps(
                {"mcpServers": {svc["server"]: svc["config"]}}, indent=2, ensure_ascii=False
            )
            + "\n```\n"
        )
        out.append(
            "| Tool | Status | Inputs | Outputs | Replays (median) | Discovery |\n|---|---|---|---|---|---|"
        )
        for t in single:
            props = (t["input_schema"] or {}).get("properties", {})
            inputs = ", ".join(f"`{k}`" for k in props if k != "app_id") or "none"
            outputs = ", ".join(f"`{k}`" for k in (t.get("outputs") or {})) or "-"
            st = t.get("stats") or {}
            replays = f"{st.get('replays_verified', 0)}/{st.get('replays', 0)} ({st.get('replay_median_ms') or '-'} ms)"
            disc = f"{st.get('discovery_ms') or '-'} ms"
            out.append(
                f"| `{t['name']}` | {t.get('status', '-')} v{t.get('version', '-')} | {inputs} | {outputs} | {replays} | {disc} |"
            )
        out.append("")
        for t in single:
            out.append(f"### `{t['name']}`\n\n{t['description']}\n")
            out.append(
                "```json\n"
                + json.dumps(t["input_schema"], indent=2, ensure_ascii=False)
                + "\n```\n"
            )
    return "\n".join(out)


def to_html(cat: dict[str, Any]) -> str:
    esc = html.escape
    parts = []
    for svc in cat["services"]:
        single = [t for t in svc["tools"] if not t.get("batch")]
        cards = []
        for t in single:
            props = (t["input_schema"] or {}).get("properties", {})
            req = set((t["input_schema"] or {}).get("required", []))
            rows = "".join(
                f"<tr><td><code>{esc(k)}</code>{' *' if k in req else ''}</td>"
                f"<td>{esc(str(v.get('type') or v.get('anyOf') or v.get('enum') or ''))[:80]}</td>"
                f"<td>{esc(str(v.get('enum') or v.get('description') or ''))[:160]}</td></tr>"
                for k, v in props.items()
                if k != "app_id"
            )
            outs = "".join(
                f"<li><code>{esc(k)}</code> {esc(o.get('description', ''))}</li>"
                for k, o in (t.get("outputs") or {}).items()
            )
            st = t.get("stats") or {}
            cards.append(
                f"""<article class="card"><header><h3><code>{esc(t["name"])}</code></h3>
<span class="pill {esc(str(t.get("status", "")))}">{esc(str(t.get("status", "-")))} v{t.get("version", "-")}</span></header>
<p>{esc(t["description"] or "")}</p>
<table><thead><tr><th>input</th><th>type</th><th>values / description</th></tr></thead><tbody>{rows or "<tr><td colspan=3>no inputs</td></tr>"}</tbody></table>
{f'<p class="label">returns</p><ul>{outs}</ul>' if outs else ""}
<dl><div><dt>verified replays</dt><dd>{st.get("replays_verified", 0)}/{st.get("replays", 0)}</dd></div>
<div><dt>replay median</dt><dd>{st.get("replay_median_ms") or "-"} ms</dd></div>
<div><dt>discovery</dt><dd>{st.get("discovery_ms") or "-"} ms</dd></div>
<div><dt>LLM calls per replay</dt><dd>{st.get("replay_llm_calls", 0)}</dd></div></dl></article>"""
            )
        config = json.dumps(
            {"mcpServers": {svc["server"]: svc["config"]}}, indent=2, ensure_ascii=False
        )
        parts.append(
            f"""<section><h2><code>{esc(svc["server"])}</code> <small>{len(single)} tools</small></h2>
<details><summary>MCP config</summary><pre>{esc(config)}</pre></details>
<div class="grid">{"".join(cards)}</div></section>"""
        )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agentify MCP Services</title>
<style>
:root{{--bg:#f7f7f5;--fg:#1d1d1b;--muted:#6b6b66;--card:#fff;--line:#e3e3df;--accent:#2f6f4f}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151514;--fg:#ececea;--muted:#a0a09a;--card:#1f1f1d;--line:#33332f;--accent:#7fc7a0}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;padding:24px 16px}}
main{{max-width:1100px;margin:0 auto}} h1{{margin:0 0 4px}} .muted{{color:var(--muted)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}}
.card header{{display:flex;justify-content:space-between;gap:8px;align-items:center}} h3{{margin:0;font-size:15px;word-break:break-all}}
.pill{{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid var(--line)}} .pill.active{{color:var(--accent);border-color:var(--accent)}}
table{{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}} td,th{{border-top:1px solid var(--line);padding:4px;text-align:left;vertical-align:top}}
dl{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin:8px 0 0}} dt{{font-size:12px;color:var(--muted)}} dd{{margin:0;font-weight:600}}
pre{{overflow:auto;background:var(--card);border:1px solid var(--line);padding:10px;border-radius:8px;font-size:12px}}
.label{{margin:6px 0 0;font-size:12px;color:var(--muted)}} ul{{margin:4px 0;padding-left:18px}}
</style></head><body><main><h1>Agentify MCP Services</h1>
<p class="muted">Generated by exploration, verified before publishing, replayed without LLM calls. Data: {esc(cat["data_dir"])}</p>
{"".join(parts) or "<p>No capabilities yet.</p>"}</main></body></html>"""
