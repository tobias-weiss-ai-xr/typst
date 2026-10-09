"""Unit tests for validation (pure logic, no HTTP).

Integration check against a real typst binary:
    TYPST_INTEGRATION=1 python3 test_app.py
"""

import os
import unittest

from app import FORMATS, MAX_ASSETS, MAX_SOURCE, compile_source, validate_payload


class ValidationTest(unittest.TestCase):
    def test_ok_minimal(self):
        norm, err = validate_payload({"source": "= Hi"})
        self.assertIsNone(err)
        self.assertEqual(norm["format"], "pdf")
        self.assertEqual(norm["assets"], {})

    def test_ok_full(self):
        norm, err = validate_payload(
            {"source": "#image('pic.png')", "format": "png", "assets": {"pic.png": "x"}}
        )
        self.assertIsNone(err)

    def test_rejects_non_object(self):
        self.assertIsNotNone(validate_payload(["x"])[1])

    def test_rejects_missing_or_blank_source(self):
        self.assertIsNotNone(validate_payload({})[1])
        self.assertIsNotNone(validate_payload({"source": "   "})[1])

    def test_rejects_oversized_source(self):
        self.assertIsNotNone(validate_payload({"source": "a" * (MAX_SOURCE + 1)})[1])

    def test_rejects_bad_format(self):
        self.assertIsNotNone(validate_payload({"source": "x", "format": "docx"})[1])

    def test_rejects_path_traversal_asset_names(self):
        for bad in ["../etc/passwd", "sub/dir.png", ".hidden", "", "a" * 129]:
            self.assertIsNotNone(
                validate_payload({"source": "x", "assets": {bad: "y"}})[1], bad
            )

    def test_rejects_non_string_asset(self):
        self.assertIsNotNone(validate_payload({"source": "x", "assets": {"a.txt": 5}})[1])

    def test_rejects_too_many_assets(self):
        assets = {f"f{i}.txt": "y" for i in range(MAX_ASSETS + 1)}
        self.assertIsNotNone(validate_payload({"source": "x", "assets": assets})[1])

    def test_formats_content_types(self):
        self.assertEqual(FORMATS["pdf"], "application/pdf")
        self.assertEqual(FORMATS["svg"], "image/svg+xml")


@unittest.skipUnless(os.environ.get("TYPST_INTEGRATION") == "1", "needs typst binary")
class IntegrationTest(unittest.TestCase):
    def test_pdf_render(self):
        data, ctype, err = compile_source("= Hello\nWorld", "pdf", {})
        self.assertIsNone(err)
        self.assertEqual(ctype, "application/pdf")
        self.assertTrue(data.startswith(b"%PDF"))

    def test_typ_error_is_reported(self):
        _, _, err = compile_source("#does-not-exist", "pdf", {})
        self.assertIsNotNone(err)

    def test_http_end_to_end(self):
        """Regression: exercise the real HTTP handler path (it must accept
        the exact payload shape validate_payload produces)."""
        import json as _json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        import app as app_mod

        app_mod._VERSION = "test"
        server = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/render"
            body = _json.dumps(
                {"source": "= Hi", "format": "pdf", "assets": {}},
            ).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers["Content-Type"], "application/pdf")
                self.assertTrue(resp.read().startswith(b"%PDF"))
        finally:
            server.shutdown()


class LandingClientTest(unittest.TestCase):
    """Client-side guards for the landed UI."""

    def test_response_body_read_exactly_once(self):
        """Regression: reading a Response stream twice (blob()/text()/
        json()) throws 'body stream already read'. The editor must read the
        body exactly once (arrayBuffer) and derive preview + download from it."""
        import app as app_mod

        script = app_mod._LANDING
        self.assertIn("await r.arrayBuffer()", script)
        self.assertNotIn("await r.blob()", script)
        self.assertNotIn("await r.text()", script)
        # Erfolgspfad liest genau einmal (arrayBuffer); r.json() nur im
        # Error-Zweig auf der FEHLER-Response (anderer Body, ok).
        self.assertEqual(script.count("arrayBuffer"), 1)

    def test_landing_has_editor_ctrls(self):
        import app as app_mod

        script = app_mod._LANDING
        for needle in ('id="src"', 'id="fmt"', 'id="go"', 'id="dl"', 'id="out"', 'id="err"'):
            self.assertIn(needle, script)


if __name__ == "__main__":
    unittest.main()



class LandingPageTest(unittest.TestCase):
    """GET / must serve the browser landing page instead of a 404 JSON dump
    (the URL is oauth2-proxy-gated → user-facing)."""

    def _server(self):
        import app as app_mod
        import threading
        from http.server import ThreadingHTTPServer
        app_mod._VERSION = "test"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app_mod.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_get_root_serves_html(self):
        import urllib.request
        base = self._server()
        with urllib.request.urlopen(base + "/") as r:
            body = r.read().decode("utf-8", "replace")
            self.assertIn("text/html", r.headers["Content-Type"])
            self.assertIn("<title>Typst Render", body)
            self.assertNotIn('"error"', body)

    def test_healthz_still_json(self):
        import urllib.request
        base = self._server()
        with urllib.request.urlopen(base + "/healthz") as r:
            self.assertIn("application/json", r.headers["Content-Type"])
            self.assertIn('"status": "ok"', r.read().decode())

    def test_unknown_get_still_json_404(self):
        import urllib.error
        import urllib.request
        base = self._server()
        try:
            urllib.request.urlopen(base + "/nope")
            self.fail("expected 404")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 404)
            self.assertIn(b'"error"', e.read())

    def test_landing_contains_form(self):
        from app import _LANDING
        for token in ("/render", "<textarea", "svg", "pdf"):
            self.assertIn(token, _LANDING)
