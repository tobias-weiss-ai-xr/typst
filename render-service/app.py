#!/usr/bin/env python3
"""openEduSuite Typst render service.

Stdlib-only HTTP wrapper around the typst CLI:
  POST /render   {"source": "...", "format": "pdf|png|svg", "assets": {"name": "text"}}
                   -> compiled document bytes
  GET  /healthz  -> {"status": "ok", "typst": "..."}

Constraints (trust boundary): body size cap, compile timeout, flat asset
names (no path traversal), subprocess arg-list (no shell), bounded compile
concurrency.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", str(5 * 1024 * 1024)))
MAX_SOURCE = int(os.environ.get("MAX_SOURCE_BYTES", str(2 * 1024 * 1024)))
TIMEOUT = int(os.environ.get("COMPILE_TIMEOUT_SECONDS", "60"))
MAX_ASSETS = 64

FORMATS = {
    "pdf": "application/pdf",
    "png": "image/png",
    "svg": "image/svg+xml",
}

# ponytail: process-global bound of concurrent compiles; queue unbounded —
# front with a real queue/limit at the ingress if throughput ever matters.
_pool = ThreadPoolExecutor(max_workers=int(os.environ.get("MAX_CONCURRENT_COMPILES", "4")))

_ASSET_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def typst_version():
    try:
        return subprocess.run(
            ["typst", "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except Exception as exc:  # pragma: no cover
        return f"unavailable ({exc})"


def validate_payload(payload):
    """Return (normalized, error). normalized is None on error."""
    if not isinstance(payload, dict):
        return None, "request body must be a JSON object"
    source = payload.get("source")
    if not isinstance(source, str) or not source.strip():
        return None, "'source' (non-empty string) is required"
    if len(source.encode("utf-8")) > MAX_SOURCE:
        return None, f"'source' exceeds {MAX_SOURCE} bytes"
    fmt = payload.get("format", "pdf")
    if fmt not in FORMATS:
        return None, f"'format' must be one of {sorted(FORMATS)}"
    assets = payload.get("assets", {})
    if not isinstance(assets, dict):
        return None, "'assets' must be an object of {name: text content}"
    if len(assets) > MAX_ASSETS:
        return None, f"too many assets (max {MAX_ASSETS})"
    for name, content in assets.items():
        if not _ASSET_NAME.match(name):
            return None, f"invalid asset name {name!r} (flat [A-Za-z0-9._-] only)"
        if not isinstance(content, str):
            return None, f"asset {name!r} must be a string"
    normalized = {"source": source, "format": fmt, "assets": assets}
    return normalized, None


def compile_source(source, format="pdf", assets=None):
    """Compile in an isolated tempdir. Returns (bytes, content_type, error)."""
    with tempfile.TemporaryDirectory(prefix="typst-render-") as tmp:
        src = os.path.join(tmp, "main.typ")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(source)
        for name, content in (assets or {}).items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        out = os.path.join(tmp, f"out.{format}")
        cmd = ["typst", "compile", "--format", format, "--root", tmp, src, out]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=tmp
            )
        except subprocess.TimeoutExpired:
            return None, None, f"compilation exceeded {TIMEOUT}s timeout"
        if proc.returncode != 0:
            diag = (proc.stderr or proc.stdout or "unknown error").strip()
            return None, None, diag[-4000:]
        with open(out, "rb") as fh:
            return fh.read(), FORMATS[format], None


class Handler(BaseHTTPRequestHandler):
    server_version = "openEduSuiteTypstRender/1.0"
    protocol_version = "HTTP/1.1"

    def _json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, code, text):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._html(200, _LANDING)
        elif self.path == "/healthz":
            self._json(200, {"status": "ok", "typst": _VERSION})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/render":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "invalid Content-Length"})
            return
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"error": f"body must be 1..{MAX_BODY} bytes"})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._json(400, {"error": f"invalid JSON: {exc}"})
            return
        normalized, err = validate_payload(payload)
        if err:
            self._json(400, {"error": err})
            return

        def work():
            return compile_source(
                source=normalized["source"],
                format=normalized["format"],
                assets=normalized["assets"],
            )

        data, ctype, err = _pool.submit(work).result()
        if err:
            self._json(422, {"error": err})
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):  # keep container logs one-line
        print(f"{self.address_string()} {fmt % args}", flush=True)


# ---------------------------------------------------------------- landing UI
# FOSS & dependency-free (vanilla JS, no CDN, offline-capable). The client
# reads the /render Response body EXACTLY ONCE (arrayBuffer) and derives both
# the inline preview and the download link from that single read — reading a
# Response stream twice throws "body stream already read" (plural blob()/text()
# calls is the classic regression; guard test in test_app.py).
_LANDING = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Typst — openEduSuite Web Editor</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--ink:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--ok:#34d399;--err:#f87171;--edge:#334155}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,sans-serif}
  header{display:flex;align-items:center;gap:14px;padding:14px 20px;border-bottom:1px solid var(--edge);flex-wrap:wrap}
  h1{font-size:18px;margin:0} h1 .t{color:var(--acc)}
  .tag{color:var(--mut);font-size:13px}
  .sp{flex:1}
  select,button,input{background:#0b1220;color:var(--ink);border:1px solid var(--edge);border-radius:8px;padding:7px 10px;font-size:13px}
  button{background:var(--acc);color:#082f49;border:none;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:wait}
  #status{font-size:12px;color:var(--mut);white-space:nowrap}
  main{display:grid;grid-template-columns:1fr 1fr;gap:0;height:calc(100vh - 57px)}
  @media (max-width:900px){main{grid-template-columns:1fr;grid-auto-rows:50vh}}
  .pane{display:flex;flex-direction:column;min-height:0}
  .pane+.pane{border-left:1px solid var(--edge)}
  @media (max-width:900px){.pane+.pane{border-left:none;border-top:1px solid var(--edge)}}
  .pane h2{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:0;padding:8px 14px;border-bottom:1px solid var(--edge);font-weight:600}
  textarea{flex:1;width:100%;background:#0b1220;color:var(--ink);border:none;outline:none;resize:none;padding:14px;font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;tab-size:2;min-height:200px}
  #out{margin:0;flex:1;overflow:auto;background:#fff;color:#111}
  #out img{max-width:100%} #out svg{max-width:100%;height:auto;display:block}
  #out.sand{display:flex;align-items:center;justify-content:center;color:#666;font:13px/1.5 system-ui,sans-serif;padding:20px;text-align:center}
  #err{display:none;background:#3b0d16;color:var(--err);border-top:1px solid #7f1d1d;font:12px/1.45 ui-monospace,monospace;padding:10px 14px;white-space:pre-wrap;max-height:140px;overflow:auto;margin:0}
  #obj{display:none;flex:1;border:none;width:100%}
  .dl{display:none;color:var(--acc);text-decoration:none;font-size:13px;font-weight:600}
  .dl:hover{text-decoration:underline}
  noscript{display:block;padding:20px}
</style>
</head>
<body>
<header>
  <h1>Typst <span class="t">Web Editor</span></h1>
  <span class="tag">openEduSuite · FOSS · compiles server-side</span>
  <span class="sp"></span>
  <span id="status">bereit</span>
  <select id="sample" title="Beispieldokument laden"><option value="">Sample…</option></select>
  <select id="fmt"><option value="svg">SVG</option><option value="png">PNG</option><option value="pdf">PDF</option></select>
  <a class="dl" id="dl" download>⬇ Download</a>
  <button id="go">Render</button>
</header>
<main>
  <section class="pane">
    <h2>Editor — Typst Markup</h2>
    <textarea id="src" spellcheck="false"></textarea>
  </section>
  <section class="pane">
    <h2>Vorschau</h2>
    <div id="out" class="sand">Noch nichts gerendert — tippe oder drücke Ctrl+Enter.</div>
    <iframe id="obj" title="Vorschau"></iframe>
    <pre id="err"></pre>
  </section>
</main>
<script>
const $=id=>document.getElementById(id);
const SAMPLES={
"Erste Schritte":`= Willkommen in Typst

Ein minimales Dokument mit *Fett*, _Kursiv_, \`Code\` und Listen:

- #emph[semantisch] statt bloß italik
- #strong[strukturiert] statt HTML-Knäuel

== Formel

Das Gauß-Integral:

$ integral_0^infinity e^(-x^2) dif x = sqrt(pi) / 2 $

== Tabelle

#table(
  columns: 3,
  [*Dienst*], [*Zweck*], [*SSO*],
  [Mail], [Stalwart], [ja],
  [Dokumente], [TOSS], [ja],
)
`,
"Seminararbeit":`#set page(paper: \"a4\", margin: 2.6cm)
#set text(font: \"New Computer Modern\", size: 11pt)
#set par(justify: true)

#align(center)[
  #text(size: 20pt, weight: \"bold\")[Digitale Hochschullehre mit openEduSuite]

  Einleitung einer Seminararbeit
]

== Motivation
Die openEduSuite bündelt #strong[Mail], #strong[Cloud], Projektarbeit und
Wissenschaftskommunikation in einer Open-Source-Plattform.

== Methodik
Wir vergleichen den Workflow vorher/nachher anhand von:

+ Diensteanbindung (Keycloak-SSO)
+ Automatisierung (BPMN-Fachregeln)
+ Nachhaltigkeit (Backup-Restores)

== Ergebnis
Die Einführung reduziert Einstiegshürden messbar. Als Render-Benchmark dient
das Gauß-Integral:

$ integral_0^infinity e^(-x^2) dif x = sqrt(pi) / 2 $
`,
"Poster / Handout":`#set page(paper: \"a4\", flipped: true, margin: 1.2cm)
#set text(size: 11pt)

#rect(fill: rgb(\"#0f766e\"), width: 100%, inset: 12pt)[
  #text(fill: white, size: 17pt, weight: \"bold\")[
    openEduSuite — Tag der offenen Tür 2026
  ]
]

#v(0.4cm)

#grid(
  columns: 2, gutter: 0.8cm,
  block(stroke: 0.6pt + gray, inset: 8pt)[
    == Was ist das?
    Eine #emph[vernetzte] Open-Source-Campus-IT:
    Mail, Cloud, Projektarbeit, Tickets, Workflows.
  ],
  block(stroke: 0.6pt + gray, inset: 8pt)[
    == Wie mitmachen?
    + Portal öffnen
    + SSO-Login nutzen
    + Loslegen — alles FOSS
  ],
)

#v(0.3cm)
#align(center)[#text(size: 9pt, fill: gray)[
  Formel des Tages: $ integral_0^infinity e^(-x^2) dif x = sqrt(pi) / 2 $
]]
`
};
// Sample-Setup
const sel=$('sample');
Object.keys(SAMPLES).forEach(k=>{const o=document.createElement('option');o.value=k;o.textContent=k;sel.appendChild(o);});
let timer=null, curURL=null;
function setStatus(t){$('status').textContent=t;}
function render(fmt){
  clearTimeout(timer);
  const body=JSON.stringify({source:$('src').value,format:fmt});
  setStatus('kompiliere…');$('go').disabled=true;$('err').style.display='none';
  fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},body})
    .then(async r=>{
      const ct=r.headers.get('Content-Type')||'';
      if(r.status!==200){let e={};try{e=await r.json()}catch(_){}
        throw new Error(e.error||('HTTP '+r.status));}
      // Einmaliger Body-Read: beide Vorschau- und Download-URLs aus demselben Puffer ableiten
      const buf=await r.arrayBuffer();
      if(curURL)URL.revokeObjectURL(curURL);
      curURL=URL.createObjectURL(new Blob([buf],{type:ct}));
      const dl=$('dl');dl.href=curURL;dl.download='typst-doc.'+fmt;dl.style.display='inline-block';
      const out=$('out'),obj=$('obj');
      out.style.display='none';obj.style.display='none';out.classList.remove('sand');
      if(ct.includes('svg')){
        out.innerHTML=new TextDecoder().decode(buf);
        out.querySelectorAll('svg').forEach(s=>{s.style.maxWidth='100%';s.style.height='auto';});
        out.style.display='block';
      }else if(ct.includes('pdf')){
        obj.src=curURL;obj.style.display='block';
      }else{
        out.innerHTML='<img src="'+curURL+'">';out.style.display='block';
      }
      setStatus('✓ '+fmt.toUpperCase()+' · '+(buf.byteLength/1024).toFixed(1)+' KB');
    })
    .catch(e=>{setStatus('Fehler');const er=$('err');er.style.display='block';er.textContent=e.message;})
    .finally(()=>{$('go').disabled=false;});
}
$('go').onclick=()=>render($('fmt').value);
$('src').addEventListener('input',()=>{setStatus('live…');clearTimeout(timer);timer=setTimeout(()=>render($('fmt').value),400);});
$('fmt').onchange=()=>render($('fmt').value);
$('sample').onchange=()=>{if($('sample').value){$('src').value=SAMPLES[$('sample').value];$('sample').value='';render($('fmt').value);}};
$('src').addEventListener('keydown',e=>{if((e.ctrlKey||e.metaKey)&&e.key==='Enter'){e.preventDefault();render($('fmt').value);}});
// Initialdokument
$('src').value=SAMPLES['Erste Schritte'];
render('svg');
</script>
</body>
</html>
"""


_VERSION = typst_version()

if __name__ == "__main__":
    port = PORT
    print(f"listening on :{port}, {_VERSION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
