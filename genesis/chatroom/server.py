"""
ChatroomServer — a dependency-light localhost viewer for the chatroom bus.

Uses only the standard library: a threaded HTTP server plus a Server-Sent
Events endpoint. Chatrooms are invisible by default; when a run starts, the REPL
calls `start()` and prints the returned URL so the user can watch the agents
communicate live. Observation only — the page has no input controls.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from genesis.chatroom.bus import ChatroomManager

logger = logging.getLogger(__name__)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Genesis · Chatrooms</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
         background:#0d1117; color:#e6edf3; display:flex; height:100vh; }
  #sidebar { width:240px; flex:0 0 240px; border-right:1px solid #21262d; overflow-y:auto; }
  #sidebar h1 { font-size:13px; text-transform:uppercase; letter-spacing:.08em;
                color:#7d8590; padding:14px 16px 8px; margin:0; }
  .room { padding:9px 16px; cursor:pointer; border-left:3px solid transparent; color:#c9d1d9; }
  .room:hover { background:#161b22; }
  .room.active { background:#161b22; border-left-color:#58a6ff; }
  .room .k { font-size:11px; color:#7d8590; }
  #main { flex:1; display:flex; flex-direction:column; min-width:0; }
  #head { padding:14px 20px; border-bottom:1px solid #21262d; font-weight:600; }
  #feed { flex:1; overflow-y:auto; padding:16px 20px; }
  .msg { margin:0 0 14px; padding-left:12px; border-left:3px solid #30363d; }
  .msg .meta { font-size:12px; color:#7d8590; margin-bottom:2px; }
  .msg .sender { font-weight:600; }
  .msg .body { white-space:pre-wrap; word-break:break-word; }
  .role-brain    { border-left-color:#a371f7; } .role-brain .sender    { color:#d2a8ff; }
  .role-worker   { border-left-color:#3fb950; } .role-worker .sender   { color:#7ee787; }
  .role-reviewer { border-left-color:#d29922; } .role-reviewer .sender { color:#e3b341; }
  .role-system   { border-left-color:#30363d; } .role-system .sender   { color:#7d8590; }
  .kind-code .body { background:#161b22; padding:8px 10px; border-radius:6px; }
  #empty { color:#7d8590; padding:40px 20px; }
  #dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:#3fb950; margin-right:6px; }
</style></head><body>
<div id="sidebar"><h1><span id="dot"></span>Rooms</h1><div id="rooms"></div></div>
<div id="main"><div id="head">Genesis Chatrooms</div>
  <div id="feed"><div id="empty">Waiting for agents to start talking…</div></div></div>
<script>
const rooms = new Map();       // id -> room
const messages = new Map();    // id -> [msgs]
let active = null;

function el(tag, cls, txt){ const e=document.createElement(tag); if(cls)e.className=cls;
  if(txt!=null)e.textContent=txt; return e; }

function renderRooms(){
  const box = document.getElementById('rooms'); box.innerHTML='';
  for(const r of rooms.values()){
    const d = el('div','room'+(r.id===active?' active':''));
    d.appendChild(el('div','t', r.title));
    d.appendChild(el('div','k', r.kind));
    d.onclick = ()=>{ active=r.id; renderRooms(); renderFeed(); };
    box.appendChild(d);
  }
}
function renderFeed(){
  const feed = document.getElementById('feed'); feed.innerHTML='';
  document.getElementById('head').textContent =
    active && rooms.has(active) ? rooms.get(active).title : 'Genesis Chatrooms';
  const list = (active && messages.get(active)) || [];
  if(!list.length){ feed.appendChild(el('div','empty','No messages yet.')); return; }
  for(const m of list){
    const w = el('div','msg role-'+m.role+' kind-'+m.kind);
    const meta = el('div','meta');
    meta.appendChild(el('span','sender', m.sender));
    meta.appendChild(el('span',null,'  ·  '+m.kind));
    w.appendChild(meta);
    w.appendChild(el('div','body', m.content));
    feed.appendChild(w);
  }
  feed.scrollTop = feed.scrollHeight;
}
function addMessage(m){
  if(!messages.has(m.room_id)) messages.set(m.room_id, []);
  messages.get(m.room_id).push(m);
  if(active===null) active = m.room_id;
  if(m.room_id===active) renderFeed();
}
async function refreshRooms(){
  const r = await fetch('/api/rooms'); const data = await r.json();
  for(const rm of data){ rooms.set(rm.id, rm); }
  renderRooms();
}
const es = new EventSource('/events');
es.addEventListener('snapshot', e=>{
  const s = JSON.parse(e.data);
  for(const rm of s.rooms){ rooms.set(rm.id, rm); }
  for(const m of s.messages){ addMessage(m); }
  if(active===null && s.rooms.length){ active = s.rooms[0].id; }
  renderRooms(); renderFeed();
});
es.addEventListener('message', async e=>{
  const m = JSON.parse(e.data);
  if(!rooms.has(m.room_id)){ await refreshRooms(); }
  addMessage(m);
});
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    # Silence the default per-request stderr logging.
    def log_message(self, *args) -> None:  # noqa: D401
        return

    @property
    def _manager(self) -> ChatroomManager:
        return self.server.manager  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send_html(_PAGE)
        elif path == "/api/rooms":
            self._send_json([r.to_dict() for r in self._manager.rooms()])
        elif path.startswith("/api/rooms/"):
            room_id = path.rsplit("/", 1)[-1]
            self._send_json([m.to_dict() for m in self._manager.history(room_id)])
        elif path == "/events":
            self._serve_events()
        else:
            self.send_error(404)

    # ── responders ──────────────────────────────────────────────────────────

    def _send_html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, obj) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        manager = self._manager
        q = manager.subscribe()
        try:
            snapshot = {
                "rooms": [r.to_dict() for r in manager.rooms()],
                "messages": [m.to_dict() for m in manager.all_messages()],
            }
            self._write_event("snapshot", snapshot)
            while True:
                try:
                    msg = q.get(timeout=15)
                    self._write_event("message", msg.to_dict())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            manager.unsubscribe(q)

    def _write_event(self, event: str, obj) -> None:
        payload = f"event: {event}\ndata: {json.dumps(obj)}\n\n".encode("utf-8")
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
        httpd.daemon_threads = True          # don't let SSE clients block shutdown
        httpd.manager = self.manager         # type: ignore[attr-defined]
        self._httpd = httpd
        self.port = httpd.server_address[1]  # resolve ephemeral port
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
