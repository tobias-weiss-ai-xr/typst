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


if __name__ == "__main__":
    unittest.main()
