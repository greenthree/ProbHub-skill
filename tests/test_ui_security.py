import importlib.util
import html
import io
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from probhub.process_control import run_managed_to_files
from werkzeug.test import EnvironBuilder


ROOT = Path(__file__).resolve().parents[1]


def find_headless_browser():
    candidates = [
        shutil.which("google-chrome"),
        shutil.which("chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        shutil.which("msedge"),
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
    ]
    return next((str(candidate) for candidate in candidates if candidate and Path(candidate).is_file()), None)


HEADLESS_BROWSER = find_headless_browser()


def remove_tree_after_handle_release(path, attempts=20, delay=0.05):
    """Retry Windows cleanup while a just-exited browser releases log handles."""
    path = Path(path)
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt + 1 == attempts:
                raise
            time.sleep(delay)


def load_ui():
    spec = importlib.util.spec_from_file_location("security_ui", ROOT / "scripts" / "ui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class UiSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ui = load_ui()
        cls.client = cls.ui.app.test_client()

    @property
    def csrf_headers(self):
        return {"X-ProbHub-CSRF": self.ui.WEBUI_CSRF_TOKEN}

    def test_index_injects_startup_token_and_uses_external_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(
            f'<meta name="probhub-csrf-token" content="{self.ui.WEBUI_CSRF_TOKEN}">',
            html,
        )
        self.assertEqual(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>", html), [])
        self.assertNotIn("<style", html)
        for asset in self.ui.WEBUI_PUBLIC_ASSETS:
            self.assertIn(f'/webui/assets/{asset}', html)

    def test_only_declared_package_assets_are_served(self):
        expected_types = {
            "app.css": {"text/css"},
            "app.js": {"application/javascript", "text/javascript"},
            "mathjax-config.js": {"application/javascript", "text/javascript"},
            "tailwind-config.js": {"application/javascript", "text/javascript"},
            "theme.js": {"application/javascript", "text/javascript"},
        }
        for name, content_types in expected_types.items():
            with self.subTest(name=name):
                response = self.client.get(f"/webui/assets/{name}")
                try:
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_data(), (self.ui.WEBUI_ASSET_DIR / name).read_bytes())
                    self.assertIn(response.mimetype, content_types)
                finally:
                    response.close()
        self.assertEqual(self.client.get("/webui/assets/index.html").status_code, 404)
        self.assertEqual(self.client.get("/webui/assets/unknown.js").status_code, 404)

    def test_write_routes_reject_missing_or_invalid_csrf_token(self):
        requests = [
            ("/api/data", {}),
            ("/api/compile", {}),
            ("/api/distribute", {}),
            ("/api/sandbox/run", {}),
            ("/api/config/Contest", {}),
            ("/api/submission/job/" + "0" * 32 + "/cancel", None),
        ]
        for path, payload in requests:
            with self.subTest(path=path):
                response = self.client.post(path, json=payload)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["code"], "csrf_failed")

        response = self.client.post(
            "/api/data",
            json={},
            headers={"X-ProbHub-CSRF": "not-the-token"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "csrf_failed")

    def test_loopback_same_origin_request_is_allowed(self):
        response = self.client.post(
            "/api/data",
            json={},
            headers={
                **self.csrf_headers,
                "Origin": "http://localhost",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.get_json().get("code"), "csrf_failed")

    def test_host_origin_referer_and_fetch_metadata_reject_cross_site_requests(self):
        response = self.client.get("/api/subtitles", headers={"Host": "attacker.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "invalid_host")

        for header, value in (
            ("Origin", "https://attacker.example"),
            ("Referer", "https://attacker.example/form"),
            ("Sec-Fetch-Site", "cross-site"),
        ):
            with self.subTest(header=header):
                response = self.client.post(
                    "/api/data",
                    json={},
                    headers={**self.csrf_headers, header: value},
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json()["code"], "cross_origin")

    def test_responses_include_browser_security_headers(self):
        response = self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        csp = response.headers["Content-Security-Policy"]
        self.assertIn("object-src 'none'", csp)
        self.assertIn("base-uri 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        script_policy = csp.split("script-src ", 1)[1].split(";", 1)[0]
        self.assertIn("'nonce-", script_policy)
        self.assertNotIn("'unsafe-inline'", script_policy)

    def test_oversized_upload_returns_structured_413(self):
        response = self.client.post(
            "/api/submission/run",
            data=b"x" * (self.ui.MAX_SUBMISSION_REQUEST_BYTES + 1),
            content_type="application/octet-stream",
            headers=self.csrf_headers,
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["code"], "request_too_large")
        self.assertEqual(
            response.get_json()["max_bytes"],
            self.ui.MAX_SUBMISSION_REQUEST_BYTES,
        )

    def test_chunked_oversized_multipart_upload_returns_structured_413(self):
        boundary = "probhub-security-boundary"
        source = b"x" * (self.ui.MAX_SUBMISSION_REQUEST_BYTES + 1)
        body = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"subtitle\"\r\n\r\nContest\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"index\"\r\n\r\n0\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"source\"; filename=\"answer.cpp\"\r\n"
            "Content-Type: text/plain\r\n\r\n"
        ).encode("ascii") + source + f"\r\n--{boundary}--\r\n".encode("ascii")
        builder = EnvironBuilder(
            path="/api/submission/run",
            method="POST",
            base_url="http://localhost/",
            input_stream=io.BytesIO(body),
            content_type=f"multipart/form-data; boundary={boundary}",
            headers=self.csrf_headers,
        )
        environ = builder.get_environ()
        environ.pop("CONTENT_LENGTH", None)
        environ["wsgi.input_terminated"] = True
        response = self.client.open(environ)
        self.assertEqual(response.status_code, 413, response.get_data(as_text=True))
        self.assertEqual(response.get_json()["code"], "request_too_large")

    def test_markdown_preview_routes_rendered_html_through_sanitizer(self):
        html = (self.ui.WEBUI_ASSET_DIR / "app.js").read_text(encoding="utf-8")
        self.assertIn("function sanitizeRenderedMarkdown(html)", html)
        self.assertIn("el.innerHTML = sanitizeRenderedMarkdown(marked.parse(text));", html)
        self.assertNotIn("el.innerHTML = marked.parse(text);", html)

    @unittest.skipUnless(HEADLESS_BROWSER, "Edge, Chrome, or Chromium is required for DOM sanitizer tests")
    def test_markdown_sanitizer_executes_against_malicious_dom(self):
        template = (self.ui.WEBUI_ASSET_DIR / "app.js").read_text(encoding="utf-8")
        start = template.index("const PROBHUB_MARKDOWN_TAGS")
        end = template.index("document.addEventListener('alpine:init'", start)
        sanitizer = template[start:end]
        malicious = (
            '<img src="https://example.com/image.png" onerror="alert(1)">'
            '<a href="javascript:alert(1)" style="position:fixed">x</a>'
            '<svg><script>alert(1)</script></svg>'
            '<iframe srcdoc="<script>alert(1)</script>"></iframe>'
        )
        encoded = json.dumps(malicious).replace("</", "<\\/")
        page = (
            "<!doctype html><meta charset=\"utf-8\"><div id=\"result\"></div><script>"
            + sanitizer
            + f"document.getElementById('result').textContent = sanitizeRenderedMarkdown({encoded});"
            + "</script>"
        )
        root = Path(tempfile.mkdtemp())
        try:
            page_path = root / "sanitizer.html"
            profile = root / "profile"
            stdout_path = root / "browser.stdout"
            stderr_path = root / "browser.stderr"
            page_path.write_text(page, encoding="utf-8")
            completed = run_managed_to_files(
                [
                    HEADLESS_BROWSER,
                    "--headless=new",
                    "--disable-gpu",
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-default-apps",
                    "--disable-extensions",
                    "--no-first-run",
                    f"--user-data-dir={profile}",
                    "--dump-dom",
                    page_path.as_uri(),
                ],
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                timeout=60,
                output_limit_bytes=16 * 1024 * 1024,
                process_limit=64,
            )
            browser_stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
            browser_stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        finally:
            remove_tree_after_handle_release(root)
        self.assertEqual(completed["reason"], "completed", browser_stderr[-2000:])
        self.assertEqual(completed["returncode"], 0, browser_stderr[-2000:])
        match = re.search(r'<div id="result">(.*?)</div>', browser_stdout, re.DOTALL)
        self.assertIsNotNone(match, browser_stdout[-2000:])
        sanitized = html.unescape(match.group(1))
        self.assertEqual(
            sanitized,
            '<img src="https://example.com/image.png"><a>x</a>',
        )
        for forbidden in ("onerror", "javascript:", "style=", "<svg", "<script", "<iframe", "srcdoc"):
            self.assertNotIn(forbidden, sanitized.lower())


if __name__ == "__main__":
    unittest.main()
