"""Live catalog of the MCP services Agentify generated (`agentify catalog --serve`).

A small local web server (127.0.0.1 only): GET / is a page that polls GET /api/catalog every
2 seconds and shows every Agentify MCP service (one per wrapped app) with its capabilities,
expandable to their description, input/output schema, status, and measured replays. Newly
learned capabilities are highlighted the moment they appear. The data is read the same way an
MCP host sees it (agentify.catalog.build_catalog: a real MCP client per service).
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio

from agentify.catalog import build_catalog


def stores(data_dirs: list[Path], scan: Path | None, latest: int) -> list[Path]:
    """Explicit stores, plus the `latest` most recently changed stores under `scan`."""
    out = [d for d in data_dirs if (d / "agentify.sqlite").exists()]
    if scan is not None and scan.is_dir():
        found = [p.parent for p in scan.glob("*/agentify.sqlite")]
        found.sort(key=lambda d: (d / "agentify.sqlite").stat().st_mtime, reverse=True)
        out += [d for d in found[:latest] if d not in out]
    return out


def snapshot(data_dirs: list[Path], scan: Path | None, latest: int) -> dict[str, Any]:
    services = []
    for d in stores(data_dirs, scan, latest):
        try:
            cat = anyio.run(build_catalog, d)
        except Exception as exc:
            services.append(
                {"server": f"({d.name})", "store": d.name, "error": str(exc), "tools": []}
            )
            continue
        for svc in cat["services"]:
            services.append(svc | {"store": d.name})
    return {"generated_at": time.strftime("%H:%M:%S"), "services": services}


class _Cache:
    def __init__(self, data_dirs: list[Path], scan: Path | None, latest: int) -> None:
        self.args = (data_dirs, scan, latest)
        self.data: dict[str, Any] = {"generated_at": None, "services": []}
        self.lock = threading.Lock()

    def refresh_forever(self, every_s: float = 2.0) -> None:
        while True:
            data = snapshot(*self.args)
            with self.lock:
                self.data = data
            time.sleep(every_s)


def serve(data_dirs: list[Path], scan: Path | None, latest: int, port: int) -> None:
    cache = _Cache(data_dirs, scan, latest)
    threading.Thread(target=cache.refresh_forever, daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path.startswith("/api/catalog"):
                with cache.lock:
                    body = json.dumps(cache.data, ensure_ascii=False, default=str).encode()
                ctype = "application/json; charset=utf-8"
            elif self.path in ("/", "/index.html"):
                body, ctype = PAGE.encode(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:  # quiet
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Agentify catalog: http://127.0.0.1:{port}/", flush=True)
    server.serve_forever()


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Agentify MCP Services</title>
<style>
:root{--bg:#f6f6f3;--fg:#1c1c1a;--muted:#6b6b65;--card:#fff;--line:#e2e2dd;--accent:#2f6f4f;--new:#fff3c4}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#ececea;--muted:#9f9f99;--card:#1e1e1c;--line:#33332f;--accent:#7fc7a0;--new:#4a4220}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;padding:24px 16px}
main{max-width:1000px;margin:0 auto}h1{margin:0}.muted{color:var(--muted)}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.live{display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--accent);animation:p 2s infinite}@keyframes p{50%{opacity:.3}}
details.svc{background:var(--card);border:1px solid var(--line);border-radius:12px;margin:14px 0;padding:4px 14px}
details.svc>summary{cursor:pointer;padding:10px 0;font-weight:600;list-style:none}details.svc>summary::-webkit-details-marker{display:none}
.count{font-weight:400;color:var(--muted)}details.tool{border-top:1px solid var(--line);padding:6px 0}
details.tool>summary{cursor:pointer;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
code{font-family:ui-monospace,Consolas,monospace;font-size:.92em}.pill{font-size:12px;padding:1px 8px;border-radius:99px;border:1px solid var(--line)}
.pill.active{color:var(--accent);border-color:var(--accent)}.new{background:var(--new);border-radius:6px;transition:background 3s}
table{border-collapse:collapse;font-size:13px;margin:6px 0;width:100%}td,th{border-top:1px solid var(--line);padding:4px;text-align:left;vertical-align:top}
.stats{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:6px 0}.stats b{display:block;font-size:15px}
pre{white-space:pre-wrap;word-break:break-all;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:8px;font-size:12px}
</style></head><body><main>
<div class="row"><h1>Agentify MCP Services</h1><span class="live" title="live"></span><span id="ts" class="muted"></span></div>
<p class="muted">MCP services generated by exploration, verified before publishing, replayed without LLM calls. Updates live.</p>
<div id="root"><p class="muted">Loading…</p></div></main>
<script>
const seen=new Set(); let first=true; const open=new Set();
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function inputs(t){const p=(t.input_schema||{}).properties||{};const req=new Set((t.input_schema||{}).required||[]);
 const rows=Object.entries(p).filter(([k])=>k!=='app_id').map(([k,v])=>`<tr><td><code>${esc(k)}</code>${req.has(k)?' *':''}</td><td>${esc(v.enum?v.enum.join(', '):(v.type||(v.anyOf||[]).map(x=>x.type).join('|')))}</td><td>${esc(v.description||'')}</td></tr>`).join('');
 return rows?`<table><tr><th>input</th><th>values / type</th><th>description</th></tr>${rows}</table>`:'<p class="muted">no inputs</p>'}
function tool(svc,t){const key=svc.store+'/'+t.name;const isNew=!first&&!seen.has(key);seen.add(key);const st=t.stats||{};
 const outs=Object.entries(t.outputs||{}).map(([k,o])=>`<li><code>${esc(k)}</code> ${esc(o.description||'')}</li>`).join('');
 return `<details class="tool${isNew?' new':''}" data-k="${esc(key)}" ${open.has(key)?'open':''}><summary><code>${esc(t.name)}</code>${t.status?`<span class="pill ${esc(t.status)}">${esc(t.status)} v${esc(t.version)}</span>`:''}<span class="muted">${esc((t.description||'').split('. ')[0].replace(/^\[[^\]]*\]\s*/,''))}</span></summary>
 <p>${esc(t.description)}</p>${inputs(t)}${outs?`<p class="muted">returns</p><ul>${outs}</ul>`:''}
 <div class="stats"><div>verified replays<b>${st.replays_verified??0}/${st.replays??0}</b></div><div>replay median<b>${st.replay_median_ms??'-'} ms</b></div><div>discovery<b>${st.discovery_ms??'-'} ms</b></div><div>LLM calls / replay<b>${st.replay_llm_calls??0}</b></div></div></details>`}
function render(d){document.getElementById('ts').textContent=d.generated_at?'updated '+d.generated_at:'';
 const root=document.getElementById('root');document.querySelectorAll('details[data-k]').forEach(e=>{if(e.open)open.add(e.dataset.k);else open.delete(e.dataset.k)});
 const svcOpen=new Set([...document.querySelectorAll('details.svc')].filter(e=>e.open).map(e=>e.dataset.s));
 if(!d.services.length){root.innerHTML='<p class="muted">No services yet: nothing has been learned.</p>';first=false;return}
 root.innerHTML=d.services.map(s=>{const tools=(s.tools||[]).filter(t=>!t.batch);const sk=s.store+'/'+s.server;
  return `<details class="svc" data-s="${esc(sk)}" ${svcOpen.has(sk)||first?'open':''}><summary>${esc(s.server)} <span class="count">· ${tools.length} capabilities · store ${esc(s.store)}</span></summary>
  ${s.error?`<p class="muted">${esc(s.error)}</p>`:''}${tools.map(t=>tool(s,t)).join('')}
  ${s.config?`<details class="tool"><summary class="muted">MCP config</summary><pre>${esc(JSON.stringify({mcpServers:{[s.server]:s.config}},null,2))}</pre></details>`:''}</details>`}).join('');
 first=false}
async function tick(){try{const r=await fetch('/api/catalog',{cache:'no-store'});render(await r.json())}catch(e){}}
tick();setInterval(tick,2000);
</script></body></html>"""
