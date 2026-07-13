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
  :root {
    color-scheme: light dark;
    --bg:#eef1f6; --sidebar:#e7ebf2; --panel:#f7f9fc; --feed:#eceff5;
    --line:#d3dae5; --ink:#1a2230; --ink-soft:#5b6676; --ink-faint:#8a94a6;
    --bubble:#ffffff; --bubble-line:#dde3ec; --chip:#dfe4ee;
    --accent:#2f7de1; --accent-soft:#e4eefb;
    --brain:#7c4dff; --worker:#1f9d55; --reviewer:#c07800; --system:#6b7688;
    --brain-bg:#efe9ff; --worker-bg:#e4f6ea; --reviewer-bg:#fdf1dd; --system-bg:#e6e9f0;
    --shadow:0 1px 2px rgba(20,30,50,.06),0 1px 8px rgba(20,30,50,.04);
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --mono:ui-monospace,SFMono-Regular,"Cascadia Code",Consolas,Menlo,monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#0b0f15; --sidebar:#0d1220; --panel:#111826; --feed:#0e141f;
      --line:#1e2634; --ink:#e6edf6; --ink-soft:#9aa6b8; --ink-faint:#68748a;
      --bubble:#18202e; --bubble-line:#232d3d; --chip:#1a2231;
      --accent:#4d9fff; --accent-soft:#16283f;
      --brain:#b98bff; --worker:#4ade80; --reviewer:#fbbf24; --system:#8592a6;
      --brain-bg:#241b3a; --worker-bg:#13291d; --reviewer-bg:#2c2412; --system-bg:#1a2130;
      --shadow:0 1px 2px rgba(0,0,0,.3);
    }
  }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; font-family:var(--sans); font-size:14px; line-height:1.5;
         background:var(--bg); color:var(--ink); display:flex;
         -webkit-font-smoothing:antialiased; }

  /* ── Sidebar ─────────────────────────────────────────── */
  #sidebar { width:288px; flex:0 0 288px; background:var(--sidebar);
             border-right:1px solid var(--line); display:flex; flex-direction:column; min-height:0; }
  .brand { display:flex; align-items:center; gap:10px; padding:16px 18px;
           border-bottom:1px solid var(--line); }
  .brand .logo { width:34px; height:34px; border-radius:10px; flex:0 0 34px;
                 background:linear-gradient(135deg,var(--accent),var(--brain));
                 display:grid; place-items:center; color:#fff; font-weight:700; font-size:16px; }
  .brand .name { font-weight:700; font-size:15px; letter-spacing:-.01em; }
  .brand .sub { font-size:11px; color:var(--ink-faint); display:flex; align-items:center; gap:5px; }
  .live-dot { width:7px; height:7px; border-radius:50%; background:var(--worker);
              box-shadow:0 0 0 0 rgba(74,222,128,.5); animation:pulse 2s infinite; }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 rgba(74,222,128,.45);} 70%{box-shadow:0 0 0 6px rgba(74,222,128,0);} 100%{box-shadow:0 0 0 0 rgba(74,222,128,0);} }
  .rooms-label { font-size:11px; text-transform:uppercase; letter-spacing:.09em;
                 color:var(--ink-faint); padding:14px 18px 6px; font-weight:600; }
  #rooms { overflow-y:auto; flex:1; padding:0 8px 12px; }
  .room { display:flex; gap:11px; align-items:center; padding:9px 10px; border-radius:11px;
          cursor:pointer; margin-bottom:2px; }
  .room:hover { background:var(--panel); }
  .room.active { background:var(--accent-soft); }
  .room .ricon { width:38px; height:38px; border-radius:11px; flex:0 0 38px; display:grid;
                 place-items:center; font-size:18px; background:var(--chip); }
  .room .rbody { min-width:0; flex:1; }
  .room .rtop { display:flex; justify-content:space-between; gap:8px; align-items:baseline; }
  .room .rtitle { font-weight:600; font-size:13.5px; white-space:nowrap; overflow:hidden;
                  text-overflow:ellipsis; }
  .room .rtime { font-size:11px; color:var(--ink-faint); flex:0 0 auto; font-variant-numeric:tabular-nums; }
  .room .rprev { font-size:12px; color:var(--ink-soft); white-space:nowrap; overflow:hidden;
                 text-overflow:ellipsis; margin-top:1px; }
  .badge { min-width:19px; height:19px; padding:0 6px; border-radius:10px; background:var(--accent);
           color:#fff; font-size:11px; font-weight:700; display:grid; place-items:center; flex:0 0 auto; }

  /* ── Conversation ────────────────────────────────────── */
  #main { flex:1; display:flex; flex-direction:column; min-width:0; background:var(--feed); }
  #head { display:flex; align-items:center; gap:12px; padding:12px 22px; background:var(--panel);
          border-bottom:1px solid var(--line); min-height:62px; }
  #head .hicon { width:40px; height:40px; border-radius:12px; display:grid; place-items:center;
                 font-size:19px; background:var(--chip); flex:0 0 40px; }
  #head .htext { min-width:0; flex:1; }
  #head .htitle { font-weight:700; font-size:15px; white-space:nowrap; overflow:hidden;
                  text-overflow:ellipsis; }
  #head .hsub { font-size:12px; color:var(--ink-soft); display:flex; align-items:center; gap:7px; margin-top:1px; }
  .kind-badge { font-size:10.5px; font-weight:600; text-transform:uppercase; letter-spacing:.05em;
                padding:2px 8px; border-radius:999px; background:var(--chip); color:var(--ink-soft); }
  .parts { display:flex; align-items:center; }
  .parts .pav { width:26px; height:26px; border-radius:50%; margin-left:-7px; border:2px solid var(--panel);
                display:grid; place-items:center; font-size:10px; font-weight:700; color:#fff; }
  .parts .pav:first-child { margin-left:0; }

  #feed { flex:1; overflow-y:auto; padding:20px 22px 8px; display:flex; flex-direction:column; gap:2px; }
  .day { align-self:center; font-size:11px; color:var(--ink-faint); background:var(--chip);
         padding:3px 12px; border-radius:999px; margin:12px 0; font-weight:600; }

  .row { display:flex; gap:11px; align-items:flex-start; padding:1px 0; }
  .row.grouped { margin-top:0; }
  .row.grouped .avatar { visibility:hidden; height:0; }
  .row .avatar { width:36px; height:36px; border-radius:50%; flex:0 0 36px; display:grid;
                 place-items:center; color:#fff; font-weight:700; font-size:13px; margin-top:16px; }
  .row.grouped .avatar { margin-top:0; }
  .stack { min-width:0; flex:1; display:flex; flex-direction:column; }
  .byline { display:flex; align-items:baseline; gap:8px; margin:8px 0 3px; }
  .row.grouped .byline { display:none; }
  .byline .who { font-weight:650; font-size:13px; }
  .byline .role-tag { font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.04em;
                      padding:1px 6px; border-radius:6px; }
  .byline .time { font-size:11px; color:var(--ink-faint); font-variant-numeric:tabular-nums; }
  .bubble { position:relative; max-width:min(680px,82%); align-self:flex-start;
            background:var(--bubble); border:1px solid var(--bubble-line); border-radius:4px 14px 14px 14px;
            padding:8px 12px; box-shadow:var(--shadow); }
  .row.grouped .bubble { border-radius:14px; }
  .bubble .txt { white-space:pre-wrap; overflow-wrap:anywhere; }
  .bubble .btime { display:none; float:right; font-size:10px; color:var(--ink-faint);
                   margin:3px 0 -2px 12px; font-variant-numeric:tabular-nums; }
  .row.grouped .bubble .btime { display:block; }

  /* role accents on the avatar + left edge of bubble */
  .role-brain    .avatar{background:linear-gradient(135deg,var(--brain),#5b34c9);}
  .role-worker   .avatar{background:linear-gradient(135deg,var(--worker),#0f7a3d);}
  .role-reviewer .avatar{background:linear-gradient(135deg,var(--reviewer),#a35d00);}
  .role-system   .avatar{background:linear-gradient(135deg,var(--system),#4a5568);}
  .role-brain    .bubble{border-left:3px solid var(--brain);}   .role-brain    .who{color:var(--brain);}
  .role-worker   .bubble{border-left:3px solid var(--worker);}  .role-worker   .who{color:var(--worker);}
  .role-reviewer .bubble{border-left:3px solid var(--reviewer);} .role-reviewer .who{color:var(--reviewer);}
  .role-system   .bubble{border-left:3px solid var(--system);}  .role-system   .who{color:var(--system);}
  .role-brain    .role-tag{background:var(--brain-bg);color:var(--brain);}
  .role-worker   .role-tag{background:var(--worker-bg);color:var(--worker);}
  .role-reviewer .role-tag{background:var(--reviewer-bg);color:var(--reviewer);}
  .role-system   .role-tag{background:var(--system-bg);color:var(--system);}

  /* kind styling */
  .kind-code .bubble .txt { font-family:var(--mono); font-size:12.5px; line-height:1.5;
                            background:var(--feed); border-radius:8px; padding:9px 11px; overflow-x:auto; }
  .kind-decision .bubble { border-left-width:3px; }
  .kind-decision .bubble .txt::before { content:"✓ "; color:var(--worker); font-weight:700; }

  /* system / status events render as a centered chip, chat-app style */
  .sysline { align-self:center; max-width:82%; text-align:center; color:var(--ink-soft);
             font-size:12px; background:var(--chip); padding:5px 14px; border-radius:999px; margin:6px 0; }
  .sysline b { color:var(--ink); font-weight:600; }

  /* empty states */
  .empty { margin:auto; text-align:center; color:var(--ink-faint); padding:40px; }
  .empty .big { font-size:34px; margin-bottom:10px; }
  .empty .t { font-weight:600; color:var(--ink-soft); }

  /* read-only composer */
  #composer { padding:12px 22px 16px; background:var(--panel); border-top:1px solid var(--line); }
  #composer .box { display:flex; align-items:center; gap:10px; background:var(--feed);
                   border:1px solid var(--line); border-radius:999px; padding:10px 16px; color:var(--ink-faint); }
  #composer .box .lock { font-size:13px; }

  ::-webkit-scrollbar { width:10px; height:10px; }
  ::-webkit-scrollbar-thumb { background:var(--line); border-radius:6px; border:2px solid transparent;
                              background-clip:content-box; }
  @media (prefers-reduced-motion: reduce) { .live-dot { animation:none; } }
</style></head><body>
<aside id="sidebar">
  <div class="brand">
    <div class="logo">G</div>
    <div>
      <div class="name">Genesis</div>
      <div class="sub"><span class="live-dot"></span><span id="roomcount">connecting…</span></div>
    </div>
  </div>
  <div class="rooms-label">Chatrooms</div>
  <div id="rooms"></div>
</aside>
<main id="main">
  <div id="head"><div class="htext"><div class="htitle">Genesis Chatrooms</div>
    <div class="hsub">Watching the agents work, live</div></div></div>
  <div id="feed"><div class="empty"><div class="big">💬</div>
    <div class="t">Waiting for the agents to start talking…</div>
    <div>Rooms will appear on the left as the run begins.</div></div></div>
  <div id="composer"><div class="box"><span class="lock">🔒</span>
    <span>You're observing live — this view is read-only</span></div></div>
</main>
<script>
const rooms = new Map();       // id -> room
const messages = new Map();    // id -> [msgs]
const unread = new Map();      // id -> count
let active = null;

const ROOM_ICON = { brain_room:"🧠", worker_room:"🛠️", review_room:"🔍", system:"⚙️" };
const ROLE_TAG  = { brain:"Brain", worker:"Worker", reviewer:"Reviewer", system:"System" };

function el(tag, cls, txt){ const e=document.createElement(tag); if(cls)e.className=cls;
  if(txt!=null)e.textContent=txt; return e; }
const STOP = new Set(["cli","orchestrator","worker","agent","main","the","bot"]);
function initials(name){
  const toks = (name||"?").replace(/[_\\-]/g," ").trim().split(/\\s+/).filter(Boolean);
  const meaningful = toks.filter(t=>!STOP.has(t.toLowerCase()));
  const use = meaningful.length ? meaningful : toks;
  const s = use.length>=2 ? use[0][0]+use[1][0] : (use[0]||"?").slice(0,2);
  return s.toUpperCase();
}
function fmtTime(ts){ return new Date(ts*1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }
function dayKey(ts){ return new Date(ts*1000).toDateString(); }
function fmtDay(ts){
  const d=new Date(ts*1000), today=new Date();
  const y=new Date(today); y.setDate(today.getDate()-1);
  if(d.toDateString()===today.toDateString()) return "Today";
  if(d.toDateString()===y.toDateString()) return "Yesterday";
  return d.toLocaleDateString([], {month:"short", day:"numeric"});
}
function relTime(ts){
  const d=new Date(ts*1000);
  if(d.toDateString()===new Date().toDateString()) return fmtTime(ts);
  return d.toLocaleDateString([], {month:"short", day:"numeric"});
}

function renderRooms(){
  const box = document.getElementById('rooms'); box.innerHTML='';
  const rc = document.getElementById('roomcount');
  rc.textContent = rooms.size ? rooms.size+" room"+(rooms.size>1?"s":"")+" · live" : "waiting for agents";
  for(const r of rooms.values()){
    const d = el('div','room'+(r.id===active?' active':''));
    d.appendChild(el('div','ricon', ROOM_ICON[r.kind]||"💬"));
    const body = el('div','rbody');
    const top = el('div','rtop');
    top.appendChild(el('div','rtitle', r.title));
    const list = messages.get(r.id)||[];
    const last = list[list.length-1];
    top.appendChild(el('div','rtime', last?relTime(last.ts):""));
    body.appendChild(top);
    const prevRow = el('div','rtop');
    const prev = el('div','rprev', last ? last.sender+": "+last.content.replace(/\\s+/g," ") : "No messages yet");
    prevRow.appendChild(prev);
    const u = unread.get(r.id)||0;
    if(u>0 && r.id!==active) prevRow.appendChild(el('div','badge', u>99?"99+":String(u)));
    body.appendChild(prevRow);
    d.appendChild(body);
    d.onclick = ()=>{ active=r.id; unread.set(r.id,0); renderRooms(); renderFeed(); };
    box.appendChild(d);
  }
}

function partAvatars(room){
  const wrap = el('div','parts');
  const ps = (room.participants||[]).slice(0,5);
  for(const name of ps){
    const a = el('div','pav', initials(name));
    a.style.background = "linear-gradient(135deg,#6b7688,#495468)";
    a.title = name;
    wrap.appendChild(a);
  }
  return wrap;
}

function renderHead(){
  const head = document.getElementById('head'); head.innerHTML='';
  if(!active || !rooms.has(active)){
    head.appendChild(el('div','htext')).innerHTML =
      '<div class="htitle">Genesis Chatrooms</div><div class="hsub">Watching the agents work, live</div>';
    return;
  }
  const r = rooms.get(active);
  head.appendChild(el('div','hicon', ROOM_ICON[r.kind]||"💬"));
  const txt = el('div','htext');
  txt.appendChild(el('div','htitle', r.title));
  const sub = el('div','hsub');
  const kb = el('span','kind-badge', (r.kind||"").replace("_"," "));
  sub.appendChild(kb);
  const n = (r.participants||[]).length;
  if(n) sub.appendChild(el('span',null, n+" participant"+(n>1?"s":"")));
  txt.appendChild(sub);
  head.appendChild(txt);
  if(n) head.appendChild(partAvatars(r));
}

function renderFeed(){
  renderHead();
  const feed = document.getElementById('feed'); feed.innerHTML='';
  const list = (active && messages.get(active)) || [];
  if(!list.length){
    const e = el('div','empty');
    e.innerHTML = '<div class="big">💬</div><div class="t">No messages yet</div>'+
                  '<div>This room is quiet — messages will stream in as agents talk.</div>';
    feed.appendChild(e); return;
  }
  let lastDay=null, prev=null;
  for(const m of list){
    const dk = dayKey(m.ts);
    if(dk!==lastDay){ feed.appendChild(el('div','day', fmtDay(m.ts))); lastDay=dk; prev=null; }

    if(m.role==='system' || m.kind==='status'){
      const s = el('div','sysline');
      s.innerHTML = '<b>'+escapeHtml(m.sender)+'</b> '+escapeHtml(m.content)+
                    ' · '+fmtTime(m.ts);
      feed.appendChild(s); prev=null; continue;
    }

    const grouped = prev && prev.sender===m.sender && prev.role===m.role &&
                    (m.ts - prev.ts) < 300 && prev.kind!=='status';
    const row = el('div','row role-'+m.role+' kind-'+m.kind+(grouped?' grouped':''));
    const av = el('div','avatar', initials(m.sender)); row.appendChild(av);
    const stack = el('div','stack');
    const by = el('div','byline');
    by.appendChild(el('span','who', m.sender));
    by.appendChild(el('span','role-tag', ROLE_TAG[m.role]||m.role));
    by.appendChild(el('span','time', fmtTime(m.ts)));
    stack.appendChild(by);
    const bub = el('div','bubble');
    bub.appendChild(el('div','txt', m.content));
    bub.appendChild(el('span','btime', fmtTime(m.ts)));
    stack.appendChild(bub);
    row.appendChild(stack);
    feed.appendChild(row);
    prev = m;
  }
  feed.scrollTop = feed.scrollHeight;
}
function escapeHtml(s){ return (s||"").replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function addMessage(m, fromSnapshot){
  if(!messages.has(m.room_id)) messages.set(m.room_id, []);
  messages.get(m.room_id).push(m);
  if(active===null) active = m.room_id;
  if(!fromSnapshot && m.room_id!==active) unread.set(m.room_id, (unread.get(m.room_id)||0)+1);
  if(m.room_id===active) renderFeed();
  renderRooms();
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
  for(const m of s.messages){ addMessage(m, true); }
  if(active===null && s.rooms.length){ active = s.rooms[0].id; }
  renderRooms(); renderFeed();
});
es.addEventListener('message', async e=>{
  const m = JSON.parse(e.data);
  if(!rooms.has(m.room_id)){ await refreshRooms(); }
  addMessage(m, false);
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
