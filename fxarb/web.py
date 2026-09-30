"""Petit tableau de bord web (stdlib uniquement) : / (HTML), /api/state (JSON), /health."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .engine import Engine

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Forex Arbitrage</title>
<style>body{font-family:system-ui,sans-serif;margin:1.5rem;max-width:960px}
table{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}td,th{border:1px solid #8884;padding:.35rem .6rem;text-align:right}
th:first-child,td:first-child{text-align:left}.opp{background:#2a92;font-weight:600}.err{color:#c33}small{opacity:.7}</style></head>
<body><h1>Forex Arbitrage</h1><small id=meta>chargement…</small>
<h2>Opportunités</h2><table id=opps></table><h2>Cotations</h2><table id=quotes></table>
<h2>Erreurs</h2><div id=errs class=err></div><h2>Historique</h2><table id=hist></table>
<script>
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const t=x=>new Date(x*1000).toLocaleTimeString();
function rows(h,r){return '<tr>'+h.map(x=>'<th>'+x+'</th>').join('')+'</tr>'+r.join('')}
async function tick(){try{const s=await (await fetch('api/state')).json();
meta.textContent=s.updated?`cycle ${s.cycles} · maj ${t(s.updated)} · frais ${s.params.fee_bps} bps · seuil ${s.params.min_net_bps} bps`:'en attente du 1er cycle…';
const O=s.opportunities||[];opps.innerHTML=O.length?rows(['Paire','Achat','Prix','Vente','Prix','Brut bps','Net bps'],
O.map(o=>`<tr class=opp><td>${esc(o.pair)}</td><td>${esc(o.buy_provider)}</td><td>${o.buy_price.toFixed(5)}</td><td>${esc(o.sell_provider)}</td><td>${o.sell_price.toFixed(5)}</td><td>${o.gross_bps.toFixed(2)}</td><td>${o.net_bps.toFixed(2)}</td></tr>`)):'<tr><td>Aucune opportunité</td></tr>';
const Q=[];for(const [p,qs] of Object.entries(s.quotes||{}))for(const q of qs)Q.push(`<tr><td>${esc(p)}</td><td>${esc(q.provider)}</td><td>${q.bid.toFixed(5)}</td><td>${q.ask.toFixed(5)}</td><td>${t(q.timestamp)}</td></tr>`);
quotes.innerHTML=rows(['Paire','Source','Bid','Ask','Heure'],Q);
errs.textContent=Object.entries(s.errors||{}).map(([k,v])=>k+': '+v).join(' | ');
hist.innerHTML=rows(['Heure','Paire','Achat','Vente','Net bps'],(s.history||[]).slice().reverse().slice(0,30).map(o=>`<tr><td>${t(o.timestamp)}</td><td>${esc(o.pair)}</td><td>${esc(o.buy_provider)}</td><td>${esc(o.sell_provider)}</td><td>${o.net_bps.toFixed(2)}</td></tr>`));
}catch(e){meta.textContent='erreur: '+e}}
tick();setInterval(tick,3000);
</script></body></html>"""


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            elif path == "/api/state":
                self._send(200, json.dumps(engine.snapshot()).encode(), "application/json")
            elif path == "/health":
                self._send(200, b'{"status":"ok"}', "application/json")
            else:
                self._send(404, b'{"error":"not found"}', "application/json")

        def log_message(self, *a):  # silence
            pass

    return ThreadingHTTPServer((host, port), Handler)


def serve_in_thread(engine: Engine, host: str, port: int) -> ThreadingHTTPServer:
    srv = make_server(engine, host, port)
    threading.Thread(target=srv.serve_forever, daemon=True, name="fxarb-web").start()
    return srv
