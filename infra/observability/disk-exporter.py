"""Expose free space on the Docker volume filesystem to Prometheus."""

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


class DiskMetricsHandler(BaseHTTPRequestHandler):
    """Serve only aggregate capacity for the dedicated sample volume."""

    timeout = 2

    def log_message(self, format: str, *args: Any) -> None:
        """Keep request data out of exporter logs."""

    def do_GET(self) -> None:
        """Return an unavailable scrape if the filesystem cannot be measured."""
        if self.path != "/metrics":
            self.send_error(404)
            return
        try:
            disk = os.statvfs("/sample")
        except OSError:
            self.send_error(503)
            return
        body = (
            "# HELP trip_docker_volume_available_bytes Bytes available to unprivileged processes.\n"
            "# TYPE trip_docker_volume_available_bytes gauge\n"
            f"trip_docker_volume_available_bytes {disk.f_bavail * disk.f_frsize}\n"
        ).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    address = ("0.0.0.0", 9100)  # noqa: S104  # Only the private Compose network is attached.
    HTTPServer(address, DiskMetricsHandler).serve_forever()
