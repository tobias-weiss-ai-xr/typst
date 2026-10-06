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


_LANDING = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Typst Render — openEduSuite</title>
<style>
  :root{--bg:#0f172a;--card:#1e293b;--ink:#e2e8f0;--mut:#94a3b8;--acc:#38bdf8;--ok:#34d399;--err:#f87171}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,sans-serif;display:flex;flex-direction:column;align-items:center;padding:32px 16px}
  h1{font-size:22px;margin:0 0 4px} p.tag{color:var(--mut);margin:0 0 24px;max-width:640px;text-align:center}
  .card{width:min(920px,100%);background:var(--card);border:1px solid #334155;border-radius:12px;padding:16px;display:flex;flex-direction:column;gap:12px}
  textarea{width:100%;background:#0b1220;color:var(--ink);border:1px solid #334155;border-radius:8px;padding:10px;font:13px/1.5 ui-monospace,monospace;resize:vertical;min-height:180px}
  .row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}
  select,button{padding:8px 12px;border-radius:8px;border:1px solid #334155;background:#0b1220;color:var(--ink);font-size:14px}
  button{background:var(--acc);color:#082f49;border:none;font-weight:600;cursor:pointer}
  button:disabled{opacity:.6;cursor:wait}
  .err{color:var(--err);white-space:pre-wrap;font:12px/1.4 ui-monospace,monospace}
  img{max-width:100%;border:1px solid #334155;border-radius:8px} a.dl{color:var(--acc)}
  #basic{font:13px/1.6 ui-monospace,monospace;color:var(--mut);background:#0b1220;border:1px solid #334155;border-radius:8px;padding:10px;margin:0}
</style>
</head>
<body>
<h1>Typst Render</h1>
<p class="tag">Compile Typst documents to PDF / PNG / SVG. Source in the box, pick a format, hit Render.</p>

<div class="card">
  <textarea id="src" spellcheck="false">= Welcome to Typst Render

We compile *live* in the browser via the render API.

- #strong[Fast]: subprocess kept cold and bounded
- #emph[Safe]: flat asset names, hard timeouts

== Try it
Pick a format and press Render. Use: *bold*, `code`.
</textarea>
  <pre id="basic">endpoint: POST /render   body: {\"source\": \"...\", \"format\": \"pdf|png|svg\"}</pre>
  <div class="row">
    <select id="fmt"><option value="svg">SVG</option><option value="png">PNG</option><option value="pdf">PDF</option></select>
    <button id="go">Render</button>
    <a class="dl" id="dl" hidden>Download output</a>
  </div>
  <pre class="err" id="err" hidden></pre>
  <div id="out"></div>
</div>

<script>
const $=id=>document.getElementById(id);
$('go').onclick=async()=>{
  const b=$('go'),fmt=$('fmt').value;
  b.disabled=true;$('err').hidden=true;$('out').innerHTML='';$('dl').hidden=true;
  try{
    const r=await fetch('/render',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({source:$('src').value,format:fmt})});
    const ct=r.headers.get('Content-Type')||'';
    if(r.status!==200){const e=await r.json();throw new Error(e.error||('HTTP '+r.status));}
    $('dl').hidden=false;$('dl').href=URL.createObjectURL(await r.blob());$('dl').download='out.'+fmt;
    if(ct.includes('svg')){$('out').innerHTML=await r.text();$('out').querySelectorAll('svg').forEach(s=>s.style.maxWidth='100%');}
    else if(ct.includes('pdf')){}else{const u=URL.createObjectURL(await r.blob());$('out').innerHTML='<img src="'+u+'">';}
  }catch(e){$('err').hidden=false;$('err').textContent='Error: '+e.message;}
  finally{b.disabled=false;}
};
</script>
</body>
</html>
"""


_VERSION = typst_version()

if __name__ == "__main__":
    port = PORT
    print(f"listening on :{port}, {_VERSION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
