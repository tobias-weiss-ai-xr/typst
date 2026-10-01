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


def compile_source(source, fmt, assets):
    """Compile in an isolated tempdir. Returns (bytes, content_type, error)."""
    with tempfile.TemporaryDirectory(prefix="typst-render-") as tmp:
        src = os.path.join(tmp, "main.typ")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(source)
        for name, content in (assets or {}).items():
            with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
                fh.write(content)
        out = os.path.join(tmp, f"out.{fmt}")
        cmd = ["typst", "compile", "--format", fmt, "--root", tmp, src, out]
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
            return fh.read(), FORMATS[fmt], None


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

    def do_GET(self):
        if self.path == "/healthz":
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
            return compile_source(**normalized)

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


_VERSION = typst_version()

if __name__ == "__main__":
    port = PORT
    print(f"listening on :{port}, {_VERSION}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
