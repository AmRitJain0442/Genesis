"""Dependency-light localhost viewer for the Genesis chatroom bus.

The server intentionally uses only the Python standard library.  It serves one
self-contained, offline HTML document and a Server-Sent Events stream.  The
viewer is observation-only; there are no mutation routes.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from genesis.chatroom.bus import ChatroomManager
from genesis.chatroom.viewer import PAGE

logger = logging.getLogger(__name__)

# Kept as a private alias for compatibility with callers that imported the old
# inline document during development.
_PAGE = PAGE


class _Handler(BaseHTTPRequestHandler):
    """Serve the read-only viewer, snapshots, room history, and live events."""

    # Silence the default per-request stderr logging.
    def log_message(self, *args) -> None:  # noqa: D401
        return

    @property
    def _manager(self) -> ChatroomManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send_html(PAGE)
        elif path == "/api/rooms":
            self._send_json([room.to_dict() for room in self._manager.rooms()])
        elif path == "/api/snapshot":
            self._send_json(self._snapshot())
        elif path.startswith("/api/rooms/"):
            room_id = path.rsplit("/", 1)[-1]
            self._send_json([message.to_dict() for message in self._manager.history(room_id)])
        elif path == "/events":
            self._serve_events()
        else:
            self.send_error(404)

    def _snapshot(self, after_seq: int = 0) -> dict:
        history = self._manager.all_messages()
        last_seq = history[-1].seq if history else 0
        # A cursor ahead of this manager's sequence means the server restarted
        # while a browser retained its EventSource state.  Replay from zero and
        # tell the client to discard sequence-based de-duplication state.
        reset = after_seq > last_seq
        effective_after = 0 if reset else after_seq
        messages = [message for message in history if message.seq > effective_after]
        return {
            "rooms": [room.to_dict() for room in self._manager.rooms()],
            "messages": [message.to_dict() for message in messages],
            "last_seq": last_seq,
            "reset": reset,
        }

    # -- responders ---------------------------------------------------------

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_common_headers(html=True)
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_common_headers(
        self,
        *,
        html: bool = False,
        cache_control: str | None = "no-store",
    ) -> None:
        if cache_control:
            self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        )
        if html:
            # The document is deliberately self-contained, so inline CSS/JS is
            # allowed while every network source except this server is denied.
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; font-src 'self'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            )

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._send_common_headers(cache_control=None)
        self.end_headers()

        manager = self._manager
        subscriber = manager.subscribe()
        try:
            last_seen = self._last_event_id()
            snapshot = self._snapshot(after_seq=last_seen)
            cursor = int(snapshot["last_seq"])
            self._write_event("snapshot", snapshot, event_id=cursor, retry_ms=2000)

            while True:
                try:
                    message = subscriber.get(timeout=15)
                    # subscribe-before-snapshot prevents missed messages.  A
                    # concurrent post can consequently appear in both places;
                    # the cursor suppresses that duplicate on the wire.
                    if message.seq <= cursor:
                        continue
                    self._write_event("message", message.to_dict(), event_id=message.seq)
                    cursor = message.seq
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            manager.unsubscribe(subscriber)

    def _last_event_id(self) -> int:
        raw = self.headers.get("Last-Event-ID", "").strip()
        try:
            return max(0, int(raw))
        except (TypeError, ValueError):
            return 0

    def _write_event(
        self,
        event: str,
        obj,
        *,
        event_id: int | None = None,
        retry_ms: int | None = None,
    ) -> None:
        fields: list[str] = []
        if event_id is not None:
            fields.append(f"id: {event_id}")
        if retry_ms is not None:
            fields.append(f"retry: {retry_ms}")
        fields.append(f"event: {event}")
        fields.append(f"data: {json.dumps(obj)}")
        payload = ("\n".join(fields) + "\n\n").encode("utf-8")
        self.wfile.write(payload)
        self.wfile.flush()


class ChatroomServer:
    def __init__(self, manager: ChatroomManager, host: str = "127.0.0.1", port: int = 0) -> None:
        self.manager = manager
        self.host = host
        self.port = port
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> str:
        if self._httpd is not None:
            return self.url
        httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        httpd.daemon_threads = True  # SSE clients must never block shutdown.
        httpd.manager = self.manager  # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]  # resolve an ephemeral port
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        logger.debug("Chatroom viewer at %s", self.url)
        return self.url

    @property
    def url(self) -> str:
        host = "127.0.0.1" if self.host in ("", "0.0.0.0") else self.host
        return f"http://{host}:{self.port}"

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        finally:
            self._httpd = None
            self._thread = None
