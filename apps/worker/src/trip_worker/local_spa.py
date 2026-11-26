from __future__ import annotations

import mimetypes
import posixpath
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from urllib.parse import unquote, urlsplit


def _make_spa_handler(root_dir: Path) -> type[BaseHTTPRequestHandler]:
    resolved_root = root_dir.resolve()
    index_path = resolved_root / "index.html"

    class _SpaRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            _ = (format, args)

        def _resolve_target(self) -> Path | None:
            raw_path = unquote(urlsplit(self.path).path or "/")
            normalized = posixpath.normpath(raw_path)
            if normalized == "/api" or normalized.startswith("/api/"):
                return None
            rel = normalized.lstrip("/")
            if rel and ".." not in Path(rel).parts:
                candidate = (resolved_root / rel).resolve()
                if candidate.is_relative_to(resolved_root) and candidate.is_file():
                    return candidate
            if index_path.is_file():
                return index_path
            return None

        def do_GET(self) -> None:
            target = self._resolve_target()
            if target is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            data = target.read_bytes()
            mime, _ = mimetypes.guess_type(str(target))
            content_type = mime or "application/octet-stream"
            if content_type.startswith("text/") or content_type == "application/javascript":
                content_type = f"{content_type}; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_HEAD(self) -> None:
            target = self._resolve_target()
            if target is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            size = target.stat().st_size
            mime, _ = mimetypes.guess_type(str(target))
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()

    return _SpaRequestHandler


class LocalSpaServer:
    def __init__(self, dist_dir: Path) -> None:
        self.dist_dir = dist_dir.resolve()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.base_url: str = ""

    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        if not self.dist_dir.is_dir():
            raise FileNotFoundError(f"SPA dist directory not found: {self.dist_dir}")
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_spa_handler(self.dist_dir))
        server.daemon_threads = True
        port = int(server.server_address[1])
        self.base_url = f"http://127.0.0.1:{port}"
        thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="trip-local-spa",
            daemon=True,
        )
        thread.start()
        self._server = server
        self._thread = thread
        return self.base_url

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> LocalSpaServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _ = (exc_type, exc, tb)
        self.close()
