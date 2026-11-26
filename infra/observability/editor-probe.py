"""Check a private synthetic editor workspace through Chromium and expose the latest result."""

import json
import os
import socket
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from time import monotonic, sleep
from urllib.parse import urlsplit
from uuid import UUID

from playwright.sync_api import Route, expect, sync_playwright

INTERVAL_SECONDS = 300
TIMEOUT_MS = 12_000
SECRET = Path("/run/secrets/editor_probe_config")
METRIC = "trip_editor_probe_success"
latest: tuple[float, int] | None = None


def _check(origin: str, config: dict[str, str]) -> bool:
    url = urlsplit(origin)
    if url.scheme != "https" and origin != "http://caddy":
        raise ValueError("The editor probe requires HTTPS outside the local Compose edge")
    if not url.hostname or url.username or url.password or origin != f"{url.scheme}://{url.netloc}":
        raise ValueError("The editor probe requires a bare origin")
    project_id = UUID(config["project_id"])
    login_url = f"{origin}/api/v1/auth/login"
    logout_url = f"{origin}/api/v1/auth/logout"
    editor_url = f"{origin}/projects/{project_id}"
    caddy_ip = socket.gethostbyname("caddy")

    def restrict_request(route: Route) -> None:
        request = route.request
        same_origin = request.url.startswith(f"{origin}/")
        if same_origin and (
            request.method in {"GET", "HEAD"}
            or (request.method == "POST" and request.url in {login_url, logout_url})
        ):
            route.continue_()
        else:
            route.abort()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--no-proxy-server",
                f"--host-resolver-rules=MAP {url.hostname} {caddy_ip}, MAP * ~NOTFOUND",
            ],
            timeout=TIMEOUT_MS,
        )
        try:
            context = browser.new_context(service_workers="block")
            context.route("**/*", restrict_request)
            page = context.new_page()
            page.set_default_timeout(TIMEOUT_MS)
            page.goto(f"{origin}/login", wait_until="domcontentloaded")
            page.locator('input[type="email"]').fill(config["email"])
            page.locator('input[autocomplete="current-password"]').fill(config["password"])
            with page.expect_response(lambda response: response.url == login_url) as response:
                page.locator('button[type="submit"]').click()
            login = response.value
            if not login.ok:
                return False
            csrf = login.header_value("x-csrf-token")
            try:
                page.wait_for_url(f"{origin}/")
                page.goto(editor_url, wait_until="domcontentloaded")
                expect(page.get_by_role("textbox", name="Project title", exact=True)).to_have_value(
                    config["project_title"], timeout=TIMEOUT_MS
                )
            finally:
                if not csrf or not page.evaluate(
                    """async ([url, csrf, timeout]) => {
                        const response = await fetch(url, {
                            method: 'POST', headers: {'X-CSRF-Token': csrf},
                            mode: 'same-origin', redirect: 'error',
                            signal: AbortSignal.timeout(timeout),
                        });
                        return response.ok;
                    }""",
                    [logout_url, csrf, TIMEOUT_MS],
                ):
                    raise RuntimeError("Could not revoke the synthetic browser session")
            return True
        finally:
            browser.close()


def _run(config_text: str) -> None:
    global latest
    while True:
        started_at = monotonic()
        try:
            result = int(_check(os.environ["EDITOR_PROBE_ORIGIN"], json.loads(config_text)))
        except Exception:
            # Browser and HTTP exceptions can include private URLs and input values.
            result = 0
        latest = (monotonic(), result)
        sleep(max(0, INTERVAL_SECONDS - (monotonic() - started_at)))


class MetricsHandler(BaseHTTPRequestHandler):
    """Serve a fresh completed result, never a default success or failure."""

    timeout = 2

    def log_message(self, format: str, *args: object) -> None:
        """Skip access logs for internal scrapes."""

    def do_GET(self) -> None:
        """Serve only the internal Prometheus endpoint."""
        if self.path != "/metrics":
            self.send_error(404)
            return
        sample = latest
        value = ""
        if sample is not None and monotonic() - sample[0] < INTERVAL_SECONDS + 90:
            value = f"# TYPE {METRIC} gauge\n{METRIC} {sample[1]}\n"
        body = value.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    if SECRET.is_file() and (config_text := SECRET.read_text().strip()):
        Thread(target=_run, args=(config_text,), daemon=True).start()
    address = ("0.0.0.0", 9100)  # noqa: S104  # No port is published on the host.
    HTTPServer(address, MetricsHandler).serve_forever()
