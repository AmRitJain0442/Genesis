"""Self-contained HTML document for the read-only chatroom viewer.

Keeping the document in its own module makes the HTTP transport easy to inspect
without introducing a frontend build step or any network-loaded dependency.
"""

PAGE = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="dark light">
  <title>Genesis // Mission Log</title>
  <style>
    :root {
      color-scheme: dark;
      --canvas:#080c0f; --canvas-grid:rgba(117,232,207,.035);
      --rail:#0c1216; --panel:#11191e; --panel-2:#151f25; --raised:#1a252c;
      --line:#26333a; --line-strong:#3b4b53;
      --ink:#e8eee9; --muted:#9baaa5; --faint:#667570;
      --signal:#75e8cf; --signal-soft:rgba(117,232,207,.12);
      --amber:#f0b35a; --amber-soft:rgba(240,179,90,.12);
      --danger:#ff776d; --danger-soft:rgba(255,119,109,.12);
      --brain:#8fd5ff; --worker:#75e8a7; --reviewer:#f0b35a; --system:#a9b2b8;
      --shadow:0 24px 70px rgba(0,0,0,.32);
      --display:"Bahnschrift SemiCondensed","Bahnschrift","Aptos Display",sans-serif;
      --body:"Aptos","Segoe UI Variable Text","Segoe UI",sans-serif;
      --mono:"Cascadia Code","Cascadia Mono",Consolas,monospace;
    }
    @media (prefers-color-scheme: light) {
      :root {
        color-scheme: light;
        --canvas:#e9ede8; --canvas-grid:rgba(16,74,67,.055);
        --rail:#dce3de; --panel:#f5f7f3; --panel-2:#e9eee9; --raised:#ffffff;
        --line:#c5cfca; --line-strong:#9caaa4;
        --ink:#14201d; --muted:#52645e; --faint:#788882;
        --signal:#087e6d; --signal-soft:rgba(8,126,109,.11);
        --amber:#9b5d08; --amber-soft:rgba(155,93,8,.1);
        --danger:#b62f2a; --danger-soft:rgba(182,47,42,.1);
        --brain:#136c97; --worker:#167345; --reviewer:#956009; --system:#596965;
        --shadow:0 24px 70px rgba(25,45,38,.14);
      }
    }

    * { box-sizing:border-box; }
    html, body { height:100%; }
    body {
      margin:0; overflow:hidden; color:var(--ink); font:14px/1.5 var(--body);
      background-color:var(--canvas);
      background-image:
        linear-gradient(var(--canvas-grid) 1px, transparent 1px),
        linear-gradient(90deg, var(--canvas-grid) 1px, transparent 1px);
      background-size:32px 32px; -webkit-font-smoothing:antialiased;
    }
    button, input, select { font:inherit; }
    button { color:inherit; }
    .sr-only {
      position:absolute !important; width:1px !important; height:1px !important; padding:0 !important;
      margin:-1px !important; overflow:hidden !important; clip:rect(0,0,0,0) !important;
      white-space:nowrap !important; border:0 !important;
    }
    button:focus-visible, input:focus-visible, select:focus-visible, #feed:focus-visible {
      outline:2px solid var(--signal); outline-offset:2px;
    }
    .skip-link {
      position:fixed; z-index:100; top:8px; left:8px; transform:translateY(-150%);
      padding:8px 12px; color:var(--canvas); background:var(--signal); border-radius:4px;
    }
    .skip-link:focus { transform:none; }
    .app { height:100dvh; min-height:420px; display:flex; }

    /* Navigation rail ----------------------------------------------------- */
    #sidebar {
      width:320px; flex:0 0 320px; min-height:0; display:flex; flex-direction:column;
      background:color-mix(in srgb, var(--rail) 96%, transparent);
      border-right:1px solid var(--line); box-shadow:var(--shadow); z-index:20;
    }
    .brand {
      min-height:76px; display:flex; align-items:center; gap:12px; padding:15px 17px;
      border-bottom:1px solid var(--line); position:relative;
    }
    .brand::after {
      content:""; position:absolute; left:17px; right:17px; bottom:-1px; height:1px;
      background:linear-gradient(90deg,var(--signal),transparent 72%);
    }
    .mark {
      width:42px; height:42px; flex:0 0 42px; display:grid; place-items:center;
      border:1px solid var(--signal); border-radius:5px; color:var(--signal);
      background:var(--signal-soft); font:800 17px/1 var(--mono); letter-spacing:-.1em;
      box-shadow:inset 0 0 18px var(--signal-soft);
    }
    .brand-copy { min-width:0; }
    .eyebrow {
      color:var(--signal); font:700 10px/1.2 var(--mono); letter-spacing:.16em;
      text-transform:uppercase;
    }
    .brand h1 { margin:4px 0 0; font:700 19px/1 var(--display); letter-spacing:.035em; }
    .rail-tools { padding:14px 14px 10px; display:grid; gap:9px; }
    .field { position:relative; display:flex; align-items:center; }
    .field-icon { position:absolute; left:11px; color:var(--faint); pointer-events:none; font-family:var(--mono); }
    .control {
      width:100%; min-height:38px; border:1px solid var(--line); border-radius:5px;
      color:var(--ink); background:var(--panel); transition:border-color .18s, background .18s;
    }
    input.control { padding:8px 10px 8px 34px; }
    select.control { padding:7px 32px 7px 10px; cursor:pointer; }
    .control:hover { border-color:var(--line-strong); }
    .control::placeholder { color:var(--faint); }
    .section-label {
      display:flex; justify-content:space-between; align-items:center; padding:5px 17px 8px;
      color:var(--faint); font:700 10px/1 var(--mono); letter-spacing:.14em; text-transform:uppercase;
    }
    #room-total { color:var(--muted); letter-spacing:0; }
    #rooms { flex:1; overflow:auto; padding:0 9px 16px; scrollbar-gutter:stable; }
    .room {
      width:100%; display:grid; grid-template-columns:38px minmax(0,1fr) auto; gap:10px;
      align-items:center; border:1px solid transparent; border-radius:6px; padding:10px;
      margin:2px 0; text-align:left; cursor:pointer; background:transparent;
      transition:background .14s, border-color .14s, transform .14s;
    }
    .room:hover { background:var(--panel); border-color:var(--line); transform:translateX(2px); }
    .room[aria-current="true"] { background:var(--signal-soft); border-color:color-mix(in srgb,var(--signal) 45%,var(--line)); }
    .room-icon {
      width:38px; height:38px; display:grid; place-items:center; border:1px solid var(--line);
      border-radius:5px; color:var(--muted); background:var(--panel-2); font:700 13px/1 var(--mono);
    }
    .room[aria-current="true"] .room-icon { color:var(--signal); border-color:var(--signal); }
    .room-body { min-width:0; }
    .room-top { display:flex; align-items:baseline; gap:8px; }
    .room-title { min-width:0; flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:680; }
    .room-time { flex:0 0 auto; color:var(--faint); font:10px/1 var(--mono); }
    .room-preview { margin-top:3px; color:var(--muted); font-size:12px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    .unread {
      min-width:21px; height:21px; display:grid; place-items:center; padding:0 6px;
      border-radius:3px; color:var(--canvas); background:var(--signal); font:800 10px/1 var(--mono);
    }
    .rail-empty { margin:18px 8px; padding:18px; border:1px dashed var(--line); color:var(--faint); text-align:center; border-radius:6px; }

    /* Mission pane -------------------------------------------------------- */
    #main { min-width:0; flex:1; display:flex; flex-direction:column; background:color-mix(in srgb,var(--canvas) 84%,transparent); }
    .mission-head {
      min-height:76px; padding:13px 22px; display:flex; align-items:center; gap:14px;
      background:color-mix(in srgb,var(--panel) 94%,transparent); border-bottom:1px solid var(--line);
    }
    #nav-toggle {
      display:none; width:40px; height:40px; border:1px solid var(--line); border-radius:5px;
      background:var(--panel-2); cursor:pointer; font:700 18px/1 var(--mono);
    }
    .head-symbol {
      width:42px; height:42px; flex:0 0 42px; display:grid; place-items:center;
      border:1px solid var(--line); border-radius:5px; color:var(--signal); background:var(--panel-2);
      font:800 13px/1 var(--mono);
    }
    .head-copy { min-width:0; flex:1; }
    #room-title { margin:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font:700 20px/1.15 var(--display); letter-spacing:.015em; }
    .head-meta { display:flex; flex-wrap:wrap; align-items:center; gap:7px; margin-top:5px; color:var(--muted); font-size:12px; }
    .tag { padding:2px 7px; border:1px solid var(--line); border-radius:3px; font:700 9px/1.4 var(--mono); letter-spacing:.08em; text-transform:uppercase; }
    .participants { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
    #connection-status {
      flex:0 0 auto; display:flex; align-items:center; gap:8px; min-height:30px; padding:6px 9px;
      border:1px solid var(--line); border-radius:4px; color:var(--muted); background:var(--panel-2);
      font:700 10px/1 var(--mono); letter-spacing:.09em; text-transform:uppercase;
    }
    .connection-dot { width:7px; height:7px; border-radius:50%; background:var(--amber); }
    #connection-status[data-state="live"] { color:var(--signal); border-color:color-mix(in srgb,var(--signal) 42%,var(--line)); }
    #connection-status[data-state="live"] .connection-dot { background:var(--signal); box-shadow:0 0 0 4px var(--signal-soft); }
    #connection-status[data-state="offline"] { color:var(--danger); }
    #connection-status[data-state="offline"] .connection-dot { background:var(--danger); }
    #connection-status[data-state="reconnecting"] .connection-dot,
    #connection-status[data-state="syncing"] .connection-dot { animation:blink 1.15s steps(2,end) infinite; }

    .message-tools {
      min-height:53px; padding:8px 22px; display:grid; grid-template-columns:minmax(180px,420px) 160px auto;
      align-items:center; gap:9px; background:var(--rail); border-bottom:1px solid var(--line);
    }
    .message-count { justify-self:end; color:var(--faint); font:700 10px/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; }
    #feed {
      flex:1; overflow:auto; padding:22px clamp(14px,3vw,40px) 30px; scrollbar-gutter:stable;
      position:relative;
    }
    .feed-inner { width:min(100%,980px); margin:0 auto; }
    .day-marker {
      display:grid; grid-template-columns:1fr auto 1fr; align-items:center; gap:12px;
      margin:17px 0 12px; color:var(--faint); font:700 10px/1 var(--mono); letter-spacing:.12em; text-transform:uppercase;
    }
    .day-marker::before, .day-marker::after { content:""; height:1px; background:var(--line); }
    .history-notice {
      margin:4px 0 14px; padding:8px 11px; border:1px dashed var(--line);
      border-radius:4px; color:var(--faint); background:var(--panel-2);
      text-align:center; font:700 10px/1.35 var(--mono); letter-spacing:.05em;
      text-transform:uppercase;
    }
    .message {
      --role:var(--system); display:grid; grid-template-columns:58px minmax(0,1fr); gap:14px;
      position:relative; padding:8px 0 10px;
    }
    .message::before { content:""; position:absolute; left:56px; top:0; bottom:0; width:1px; background:var(--line); }
    .message.role-brain { --role:var(--brain); }
    .message.role-worker { --role:var(--worker); }
    .message.role-reviewer { --role:var(--reviewer); }
    .message.role-system { --role:var(--system); }
    .stamp { padding-top:11px; color:var(--faint); text-align:right; font:10px/1 var(--mono); }
    .record {
      min-width:0; position:relative; margin-left:10px; border:1px solid var(--line); border-left:3px solid var(--role);
      border-radius:5px; background:var(--panel); box-shadow:0 8px 28px rgba(0,0,0,.08);
    }
    .record::before {
      content:""; position:absolute; left:-19px; top:14px; width:7px; height:7px; border-radius:50%;
      background:var(--role); border:3px solid var(--canvas);
    }
    .record-head { min-height:36px; display:flex; align-items:center; gap:8px; padding:7px 9px 6px 11px; border-bottom:1px solid var(--line); }
    .sender { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--role); font-weight:720; }
    .role-label, .kind-label { color:var(--muted); font:700 9px/1 var(--mono); letter-spacing:.08em; text-transform:uppercase; }
    .kind-label { margin-left:auto; padding:3px 6px; border:1px solid var(--line); border-radius:3px; }
    .copy-button, .expand-button {
      min-height:27px; padding:4px 7px; border:1px solid var(--line); border-radius:3px;
      color:var(--muted); background:var(--panel-2); cursor:pointer; font:700 9px/1 var(--mono);
      letter-spacing:.05em; text-transform:uppercase;
    }
    .copy-button:hover, .expand-button:hover { color:var(--signal); border-color:var(--signal); }
    .message-content { padding:11px 13px 13px; white-space:pre-wrap; overflow-wrap:anywhere; }
    .message-content.clamped { max-height:15rem; overflow:hidden; mask-image:linear-gradient(to bottom,#000 72%,transparent); }
    .expand-wrap { padding:0 13px 11px; }
    .kind-code .record { background:color-mix(in srgb,var(--panel) 72%,var(--canvas)); }
    .kind-code pre { margin:0; padding:13px; overflow:auto; color:var(--ink); font:12.5px/1.55 var(--mono); tab-size:2; white-space:pre; }
    .kind-decision .record { border-color:color-mix(in srgb,var(--signal) 42%,var(--line)); }
    .kind-decision .kind-label { color:var(--signal); border-color:color-mix(in srgb,var(--signal) 45%,var(--line)); }
    .kind-tool .record { background:color-mix(in srgb,var(--amber-soft) 35%,var(--panel)); }
    .kind-status .record { background:var(--panel-2); box-shadow:none; }
    .severity-error .record { border-color:color-mix(in srgb,var(--danger) 55%,var(--line)); border-left-color:var(--danger); background:var(--danger-soft); }
    .severity-error .kind-label { color:var(--danger); }
    .message.grouped { padding-top:1px; }
    .message.grouped .stamp, .message.grouped .record-head { opacity:.72; }
    .empty-state {
      min-height:100%; display:grid; place-items:center; padding:44px 20px; text-align:center;
    }
    .empty-card { width:min(480px,100%); padding:34px; border:1px dashed var(--line-strong); border-radius:6px; background:color-mix(in srgb,var(--panel) 78%,transparent); }
    .empty-code { color:var(--signal); font:800 28px/1 var(--mono); }
    .empty-title { margin:13px 0 5px; font:700 18px/1.2 var(--display); }
    .empty-copy { color:var(--muted); }

    .observer-bar {
      min-height:55px; display:flex; align-items:center; gap:10px; padding:9px 22px;
      border-top:1px solid var(--line); background:var(--panel);
    }
    .observer-copy { min-width:0; flex:1; color:var(--muted); font-size:12px; }
    .observer-copy strong { color:var(--ink); font-family:var(--mono); font-size:10px; letter-spacing:.08em; text-transform:uppercase; }
    .bar-button {
      min-height:34px; padding:7px 10px; border:1px solid var(--line); border-radius:4px;
      color:var(--muted); background:var(--panel-2); cursor:pointer; font:700 10px/1 var(--mono); letter-spacing:.06em; text-transform:uppercase;
    }
    .bar-button[aria-pressed="true"] { color:var(--signal); border-color:var(--signal); background:var(--signal-soft); }
    #jump-latest { color:var(--canvas); border-color:var(--signal); background:var(--signal); }
    [hidden] { display:none !important; }
    #toast {
      position:fixed; right:18px; bottom:70px; z-index:80; padding:9px 12px; border-radius:4px;
      color:var(--canvas); background:var(--signal); box-shadow:var(--shadow); font:700 10px/1 var(--mono);
      text-transform:uppercase; letter-spacing:.08em; opacity:0; transform:translateY(8px); pointer-events:none;
      transition:opacity .18s, transform .18s;
    }
    #toast.visible { opacity:1; transform:none; }
    #scrim { display:none; }

    ::-webkit-scrollbar { width:11px; height:11px; }
    ::-webkit-scrollbar-track { background:transparent; }
    ::-webkit-scrollbar-thumb { background:var(--line-strong); border:3px solid transparent; border-radius:8px; background-clip:padding-box; }
    @keyframes blink { 50% { opacity:.25; } }

    @media (max-width:860px) {
      #sidebar {
        position:fixed; inset:0 auto 0 0; width:min(88vw,340px); transform:translateX(-105%);
        transition:transform .22s ease; visibility:hidden;
      }
      body.nav-open #sidebar { transform:none; visibility:visible; }
      #scrim { position:fixed; inset:0; z-index:15; border:0; background:rgba(0,0,0,.58); }
      body.nav-open #scrim { display:block; }
      #nav-toggle { display:grid; place-items:center; }
      .mission-head { padding-inline:13px; }
      .message-tools { padding-inline:13px; grid-template-columns:minmax(140px,1fr) 132px; }
      .message-count { display:none; }
      .observer-bar { padding-inline:13px; }
    }
    @media (max-width:560px) {
      .app { min-height:360px; }
      .mission-head { min-height:68px; gap:9px; }
      .head-symbol { display:none; }
      #room-title { font-size:17px; }
      #connection-status { padding:6px; }
      #connection-label { position:absolute; width:1px; height:1px; overflow:hidden; clip:rect(0 0 0 0); }
      .message-tools { min-height:94px; grid-template-columns:1fr; }
      #feed { padding:13px 10px 22px; }
      .message { grid-template-columns:42px minmax(0,1fr); gap:7px; }
      .message::before { left:40px; }
      .record { margin-left:8px; }
      .record::before { left:-16px; }
      .role-label { display:none; }
      .observer-copy { display:none; }
      .observer-bar { justify-content:flex-end; }
    }
    @media (prefers-reduced-motion:reduce) {
      *, *::before, *::after { scroll-behavior:auto !important; animation:none !important; transition:none !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#feed">Skip to mission log</a>
  <div class="app">
    <button id="scrim" type="button" aria-label="Close room navigation"></button>
    <aside id="sidebar" aria-label="Mission rooms">
      <header class="brand">
        <div class="mark" aria-hidden="true">G//</div>
        <div class="brand-copy">
          <div class="eyebrow">Local observer</div>
          <h1>GENESIS // MISSION LOG</h1>
        </div>
      </header>
      <div class="rail-tools">
        <label class="field" for="room-search">
          <span class="field-icon" aria-hidden="true">/</span>
          <span class="sr-only">Search rooms</span>
          <input id="room-search" class="control" type="search" placeholder="Search rooms" autocomplete="off" spellcheck="false">
        </label>
        <label for="room-filter">
          <span class="sr-only">Filter rooms by type</span>
          <select id="room-filter" class="control">
            <option value="all">All room types</option>
            <option value="brain_room">Brain rooms</option>
            <option value="worker_room">Worker rooms</option>
            <option value="review_room">Review rooms</option>
            <option value="system">Run rooms</option>
          </select>
        </label>
      </div>
      <div class="section-label"><span>Active channels</span><span id="room-total">0 rooms</span></div>
      <nav id="rooms" aria-label="Available chatrooms"></nav>
    </aside>

    <main id="main">
      <header class="mission-head">
        <button id="nav-toggle" type="button" aria-label="Open room navigation" aria-controls="sidebar" aria-expanded="false">≡</button>
        <div id="head-symbol" class="head-symbol" aria-hidden="true">SYS</div>
        <div class="head-copy">
          <h2 id="room-title">Awaiting mission traffic</h2>
          <div class="head-meta"><span id="room-kind" class="tag">Observer</span><span id="room-participants" class="participants">Read-only local stream</span></div>
        </div>
        <div id="connection-status" role="status" aria-live="polite" data-state="connecting">
          <span class="connection-dot" aria-hidden="true"></span><span id="connection-label">Connecting</span>
        </div>
      </header>

      <section class="message-tools" aria-label="Message filters">
        <label class="field" for="message-search">
          <span class="field-icon" aria-hidden="true">?</span>
          <span class="sr-only">Search messages</span>
          <input id="message-search" class="control" type="search" placeholder="Search this log" autocomplete="off" spellcheck="false">
        </label>
        <label for="message-filter">
          <span class="sr-only">Filter messages by type</span>
          <select id="message-filter" class="control">
            <option value="all">All events</option>
            <option value="decision">Decisions</option>
            <option value="code">Code & files</option>
            <option value="tool">Tool activity</option>
            <option value="status">Status</option>
            <option value="error">Errors only</option>
          </select>
        </label>
        <div id="message-count" class="message-count" aria-live="polite">0 events</div>
      </section>

      <section id="feed" role="log" aria-live="polite" aria-relevant="additions text" aria-busy="true" tabindex="0" aria-label="Selected room mission log">
        <div class="empty-state"><div class="empty-card"><div class="empty-code">00:00</div><div class="empty-title">Listening for agents</div><div class="empty-copy">Rooms and events appear here as the run begins.</div></div></div>
      </section>

      <footer class="observer-bar">
        <div class="observer-copy"><strong>Observer mode</strong> · localhost only · no commands are sent from this view</div>
        <button id="follow-live" class="bar-button" type="button" aria-pressed="true">Follow live</button>
        <button id="jump-latest" class="bar-button" type="button" hidden>Jump to latest</button>
      </footer>
    </main>
  </div>
  <div id="toast" role="status" aria-live="polite"></div>

  <script>
  (() => {
    "use strict";

    const rooms = new Map();
    const messages = new Map();
    const unread = new Map();
    const seenMessageIds = new Set();
    const seenMessageSeqs = new Set();
    let activeRoom = null;
    let lastSeq = 0;
    let followLive = true;
    let pendingVisible = 0;
    let resyncing = false;
    let roomRenderQueued = false;
    let toastTimer = null;

    const $ = (id) => document.getElementById(id);
    const feed = $("feed");
    const roomBox = $("rooms");
    const roomSearch = $("room-search");
    const roomFilter = $("room-filter");
    const messageSearch = $("message-search");
    const messageFilter = $("message-filter");
    const followButton = $("follow-live");
    const jumpButton = $("jump-latest");

    const ROOM_META = {
      brain_room:{symbol:"BRN", label:"Brain room"},
      worker_room:{symbol:"WRK", label:"Worker room"},
      review_room:{symbol:"REV", label:"Review room"},
      system:{symbol:"SYS", label:"Run room"}
    };
    const ROLE_LABEL = {brain:"Brain", worker:"Worker", reviewer:"Reviewer", system:"System"};
    const KNOWN_ROLES = new Set(Object.keys(ROLE_LABEL));
    const KNOWN_KINDS = new Set(["message", "code", "decision", "tool", "status"]);
    const MAX_RENDERED_EVENTS = 500;

    function make(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }

    function safeRole(role) { return KNOWN_ROLES.has(role) ? role : "system"; }
    function safeKind(kind) { return KNOWN_KINDS.has(kind) ? kind : "message"; }
    function roomMeta(kind) { return ROOM_META[kind] || {symbol:"LOG", label:"Room"}; }
    function fullTime(ts) { return new Date(Number(ts) * 1000).toLocaleString(); }
    function clockTime(ts) { return new Date(Number(ts) * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit"}); }
    function dayKey(ts) { return new Date(Number(ts) * 1000).toDateString(); }
    function dayLabel(ts) {
      const date = new Date(Number(ts) * 1000);
      const today = new Date();
      const yesterday = new Date(today); yesterday.setDate(today.getDate() - 1);
      if (date.toDateString() === today.toDateString()) return "Today";
      if (date.toDateString() === yesterday.toDateString()) return "Yesterday";
      return date.toLocaleDateString([], {year:"numeric", month:"short", day:"numeric"});
    }
    function relativeRoomTime(ts) {
      if (!ts) return "";
      const date = new Date(Number(ts) * 1000);
      return date.toDateString() === new Date().toDateString()
        ? clockTime(ts)
        : date.toLocaleDateString([], {month:"short", day:"numeric"});
    }
    function isError(message) {
      return /\b(error|failed|failure|blocked|timeout|denied)\b/i.test(message.content || "");
    }
    function roomMessages(roomId) { return messages.get(roomId) || []; }
    function latestMessage(roomId) {
      const list = roomMessages(roomId);
      return list.length ? list[list.length - 1] : null;
    }
    function roomActivity(room) {
      const latest = latestMessage(room.id);
      return latest ? Number(latest.ts || 0) : Number(room.created_ts || 0);
    }
    function nearBottom() { return feed.scrollHeight - feed.scrollTop - feed.clientHeight < 96; }

    function setConnection(state, label) {
      const status = $("connection-status");
      status.dataset.state = state;
      $("connection-label").textContent = label;
    }

    function setFollow(enabled, scroll = false) {
      followLive = Boolean(enabled);
      followButton.setAttribute("aria-pressed", String(followLive));
      followButton.textContent = followLive ? "Following live" : "Follow live";
      if (scroll) {
        feed.scrollTop = feed.scrollHeight;
        pendingVisible = 0;
      }
      updateJumpButton();
    }

    function updateJumpButton() {
      jumpButton.hidden = pendingVisible < 1;
      jumpButton.textContent = pendingVisible > 0
        ? `Jump to latest · ${pendingVisible > 99 ? "99+" : pendingVisible}`
        : "Jump to latest";
    }

    function showToast(text) {
      const toast = $("toast");
      toast.textContent = text;
      toast.classList.add("visible");
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toast.classList.remove("visible"), 1400);
    }

    async function copyText(text, button) {
      try {
        if (navigator.clipboard && window.isSecureContext) {
          await navigator.clipboard.writeText(text);
        } else {
          const area = make("textarea");
          area.value = text;
          area.setAttribute("readonly", "");
          area.style.position = "fixed";
          area.style.opacity = "0";
          document.body.appendChild(area);
          area.select();
          document.execCommand("copy");
          area.remove();
        }
        const old = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => { button.textContent = old; }, 1100);
        showToast("Copied to clipboard");
      } catch (_) {
        showToast("Copy unavailable");
      }
    }

    function storeMessage(message, detectGap = true) {
      if (!message || !message.room_id) return {added:false, gap:false, appended:false};
      const seq = Number(message.seq) || 0;
      if ((message.id && seenMessageIds.has(message.id)) || (seq && seenMessageSeqs.has(seq))) {
        return {added:false, gap:false, appended:false};
      }

      const previousSeq = lastSeq;
      if (message.id) seenMessageIds.add(message.id);
      if (seq) seenMessageSeqs.add(seq);
      if (!messages.has(message.room_id)) messages.set(message.room_id, []);
      const list = messages.get(message.room_id);
      const prior = list.length ? Number(list[list.length - 1].seq || 0) : -1;
      const appended = seq >= prior;
      list.push(message);
      if (!appended) list.sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
      lastSeq = Math.max(lastSeq, seq);
      return {added:true, gap:detectGap && previousSeq > 0 && seq > previousSeq + 1, appended};
    }

    function mergeRooms(incoming) {
      for (const room of incoming || []) {
        if (room && room.id) rooms.set(room.id, room);
      }
    }

    function sortedRooms() {
      return [...rooms.values()].sort((a, b) => {
        const activity = roomActivity(b) - roomActivity(a);
        return activity || String(a.title || "").localeCompare(String(b.title || ""));
      });
    }

    function roomMatches(room) {
      const query = roomSearch.value.trim().toLowerCase();
      const type = roomFilter.value;
      if (type !== "all" && room.kind !== type) return false;
      if (!query) return true;
      const haystack = [room.title, room.kind, ...(room.participants || [])].join(" ").toLowerCase();
      return haystack.includes(query);
    }

    function renderRooms() {
      roomRenderQueued = false;
      const visible = sortedRooms().filter(roomMatches);
      const fragment = document.createDocumentFragment();
      roomBox.replaceChildren();

      if (!visible.length) {
        const empty = make("div", "rail-empty", rooms.size ? "No rooms match this filter." : "Waiting for the first mission room…");
        fragment.appendChild(empty);
      } else {
        for (const room of visible) {
          const latest = latestMessage(room.id);
          const meta = roomMeta(room.kind);
          const button = make("button", "room");
          button.type = "button";
          button.dataset.roomId = room.id;
          button.setAttribute("aria-current", String(room.id === activeRoom));
          button.setAttribute("aria-label", `${room.title || "Untitled room"}, ${meta.label}`);

          const icon = make("span", "room-icon", meta.symbol);
          icon.setAttribute("aria-hidden", "true");
          button.appendChild(icon);
          const body = make("span", "room-body");
          const top = make("span", "room-top");
          top.appendChild(make("span", "room-title", room.title || "Untitled room"));
          const time = make("time", "room-time", latest ? relativeRoomTime(latest.ts) : "");
          if (latest) {
            time.dateTime = new Date(Number(latest.ts) * 1000).toISOString();
            time.title = fullTime(latest.ts);
          }
          top.appendChild(time);
          body.appendChild(top);
          body.appendChild(make(
            "span",
            "room-preview",
            latest ? `${latest.sender}: ${String(latest.content || "").replace(/\s+/g, " ")}` : "Channel open · no events yet"
          ));
          button.appendChild(body);
          const count = unread.get(room.id) || 0;
          if (count && room.id !== activeRoom) button.appendChild(make("span", "unread", count > 99 ? "99+" : String(count)));
          button.addEventListener("click", () => selectRoom(room.id));
          fragment.appendChild(button);
        }
      }
      roomBox.appendChild(fragment);
      $("room-total").textContent = `${visible.length}/${rooms.size} room${rooms.size === 1 ? "" : "s"}`;
    }

    function scheduleRoomRender() {
      if (roomRenderQueued) return;
      roomRenderQueued = true;
      requestAnimationFrame(renderRooms);
    }

    function selectRoom(roomId) {
      if (!rooms.has(roomId)) return;
      activeRoom = roomId;
      unread.set(roomId, 0);
      pendingVisible = 0;
      setFollow(true);
      renderRooms();
      renderHead();
      renderFeed({stick:true});
      closeNavigation();
    }

    function renderHead() {
      const room = activeRoom ? rooms.get(activeRoom) : null;
      if (!room) {
        $("head-symbol").textContent = "SYS";
        $("room-title").textContent = "Awaiting mission traffic";
        $("room-kind").textContent = "Observer";
        $("room-participants").textContent = "Read-only local stream";
        return;
      }
      const meta = roomMeta(room.kind);
      $("head-symbol").textContent = meta.symbol;
      $("room-title").textContent = room.title || "Untitled room";
      $("room-kind").textContent = meta.label;
      const participants = room.participants || [];
      $("room-participants").textContent = participants.length
        ? `${participants.length} participant${participants.length === 1 ? "" : "s"} · ${participants.join(" · ")}`
        : "No participants declared";
    }

    function messageMatches(message) {
      const query = messageSearch.value.trim().toLowerCase();
      const filter = messageFilter.value;
      if (filter === "error") {
        if (!isError(message)) return false;
      } else if (filter !== "all" && safeKind(message.kind) !== filter) {
        return false;
      }
      if (!query) return true;
      return [message.sender, message.role, message.kind, message.content].join(" ").toLowerCase().includes(query);
    }

    function visibleMessages() {
      return roomMessages(activeRoom).filter(messageMatches);
    }

    function renderWindow() {
      const matched = visibleMessages();
      const hidden = Math.max(0, matched.length - MAX_RENDERED_EVENTS);
      return {
        matched,
        visible: hidden ? matched.slice(hidden) : matched,
        hidden
      };
    }

    function updateMessageCount(total) {
      const shown = Math.min(total, MAX_RENDERED_EVENTS);
      $("message-count").textContent = total > shown
        ? `${shown} / ${total} events`
        : `${total} event${total === 1 ? "" : "s"}`;
    }

    function historyNotice(hidden) {
      return make(
        "div",
        "history-notice",
        `${hidden} earlier event${hidden === 1 ? "" : "s"} kept out of the DOM · refine search to inspect`
      );
    }

    function makeDayMarker(message) {
      return make("div", "day-marker", dayLabel(message.ts));
    }

    function createMessageNode(message, grouped = false) {
      const role = safeRole(message.role);
      const kind = safeKind(message.kind);
      const error = isError(message);
      const article = make("article", `message role-${role} kind-${kind}${error ? " severity-error" : ""}${grouped ? " grouped" : ""}`);
      article.dataset.messageId = message.id || String(message.seq || "");
      article.setAttribute("aria-label", `${ROLE_LABEL[role]} event from ${message.sender}`);

      const stamp = make("time", "stamp", clockTime(message.ts));
      stamp.dateTime = new Date(Number(message.ts) * 1000).toISOString();
      stamp.title = fullTime(message.ts);
      article.appendChild(stamp);

      const record = make("div", "record");
      const head = make("header", "record-head");
      head.appendChild(make("span", "sender", message.sender || "unknown"));
      head.appendChild(make("span", "role-label", ROLE_LABEL[role]));
      head.appendChild(make("span", "kind-label", error ? "Error" : kind));
      const copy = make("button", "copy-button", "Copy");
      copy.type = "button";
      copy.title = "Copy full event text";
      copy.setAttribute("aria-label", `Copy event from ${message.sender || "unknown"}`);
      copy.addEventListener("click", () => copyText(String(message.content || ""), copy));
      head.appendChild(copy);
      record.appendChild(head);

      if (kind === "code") {
        const pre = make("pre");
        pre.appendChild(make("code", "", message.content || ""));
        record.appendChild(pre);
      } else {
        const content = make("div", "message-content", message.content || "");
        const isLong = String(message.content || "").length > 900;
        if (isLong) content.classList.add("clamped");
        record.appendChild(content);
        if (isLong) {
          const wrap = make("div", "expand-wrap");
          const expand = make("button", "expand-button", "Expand event");
          expand.type = "button";
          expand.setAttribute("aria-expanded", "false");
          expand.addEventListener("click", () => {
            const open = content.classList.toggle("clamped") === false;
            expand.setAttribute("aria-expanded", String(open));
            expand.textContent = open ? "Collapse event" : "Expand event";
          });
          wrap.appendChild(expand);
          record.appendChild(wrap);
        }
      }
      article.appendChild(record);
      return article;
    }

    function emptyFeed(title, copy, code = "--:--") {
      const shell = make("div", "empty-state");
      const card = make("div", "empty-card");
      card.appendChild(make("div", "empty-code", code));
      card.appendChild(make("div", "empty-title", title));
      card.appendChild(make("div", "empty-copy", copy));
      shell.appendChild(card);
      return shell;
    }

    function renderFeed({stick = false, preserve = false} = {}) {
      const previousTop = feed.scrollTop;
      const windowed = renderWindow();
      const list = windowed.visible;
      feed.setAttribute("aria-busy", "true");
      feed.replaceChildren();
      updateMessageCount(windowed.matched.length);

      if (!activeRoom) {
        feed.appendChild(emptyFeed("Listening for agents", "Rooms and events appear here as the run begins.", "00:00"));
      } else if (!list.length) {
        const filtering = Boolean(messageSearch.value.trim()) || messageFilter.value !== "all";
        feed.appendChild(emptyFeed(
          filtering ? "No matching events" : "Channel is quiet",
          filtering ? "Adjust the search or event filter to reveal more entries." : "New agent activity will stream into this mission log."
        ));
      } else {
        const inner = make("div", "feed-inner");
        const fragment = document.createDocumentFragment();
        if (windowed.hidden) fragment.appendChild(historyNotice(windowed.hidden));
        let previous = null;
        let previousDay = null;
        for (const message of list) {
          const day = dayKey(message.ts);
          if (day !== previousDay) {
            fragment.appendChild(makeDayMarker(message));
            previousDay = day;
            previous = null;
          }
          const grouped = previous && previous.sender === message.sender && previous.role === message.role &&
            Number(message.ts) - Number(previous.ts) < 300;
          fragment.appendChild(createMessageNode(message, Boolean(grouped)));
          previous = message;
        }
        inner.appendChild(fragment);
        feed.appendChild(inner);
      }
      feed.setAttribute("aria-busy", "false");
      if (stick || followLive) feed.scrollTop = feed.scrollHeight;
      else if (preserve) feed.scrollTop = previousTop;
      updateJumpButton();
    }

    function trimRenderedWindow(inner, total) {
      let rendered = inner.querySelectorAll(".message");
      while (rendered.length > MAX_RENDERED_EVENTS) {
        const oldest = rendered[0];
        const marker = oldest.previousElementSibling;
        const next = oldest.nextElementSibling;
        oldest.remove();
        if (marker && marker.classList.contains("day-marker") &&
            (!next || next.classList.contains("day-marker"))) marker.remove();
        rendered = inner.querySelectorAll(".message");
      }

      const hidden = Math.max(0, total - rendered.length);
      let notice = inner.querySelector(".history-notice");
      if (hidden) {
        if (!notice) {
          notice = historyNotice(hidden);
          inner.prepend(notice);
        } else {
          notice.textContent = `${hidden} earlier event${hidden === 1 ? "" : "s"} kept out of the DOM · refine search to inspect`;
        }
      } else if (notice) {
        notice.remove();
      }
    }

    function appendVisibleMessage(message) {
      if (!messageMatches(message)) return;
      const shouldStick = followLive && nearBottom();
      let inner = feed.querySelector(".feed-inner");
      if (!inner) {
        renderFeed({stick:shouldStick});
        if (!shouldStick) { pendingVisible += 1; updateJumpButton(); }
        return;
      }
      const list = visibleMessages();
      const previous = list.length > 1 ? list[list.length - 2] : null;
      const previousDay = previous ? dayKey(previous.ts) : null;
      if (!previous || dayKey(message.ts) !== previousDay) inner.appendChild(makeDayMarker(message));
      const grouped = previous && previous.sender === message.sender && previous.role === message.role &&
        Number(message.ts) - Number(previous.ts) < 300 && dayKey(message.ts) === previousDay;
      inner.appendChild(createMessageNode(message, Boolean(grouped)));
      trimRenderedWindow(inner, list.length);
      updateMessageCount(list.length);
      if (shouldStick) {
        feed.scrollTop = feed.scrollHeight;
        pendingVisible = 0;
      } else {
        pendingVisible += 1;
      }
      updateJumpButton();
    }

    function acceptLiveMessage(message) {
      const result = storeMessage(message, true);
      if (!result.added) return;
      if (result.gap) scheduleResync();
      if (!rooms.has(message.room_id)) refreshRooms();

      if (activeRoom === null) activeRoom = message.room_id;
      if (message.room_id === activeRoom) {
        if (result.appended) appendVisibleMessage(message);
        else renderFeed({preserve:true});
      } else {
        unread.set(message.room_id, (unread.get(message.room_id) || 0) + 1);
      }
      scheduleRoomRender();
      renderHead();
    }

    function applySnapshot(snapshot, {initial = false, full = false} = {}) {
      if (snapshot.reset) {
        rooms.clear();
        messages.clear();
        unread.clear();
        seenMessageIds.clear();
        seenMessageSeqs.clear();
        activeRoom = null;
        lastSeq = 0;
        pendingVisible = 0;
      }
      const before = lastSeq;
      mergeRooms(snapshot.rooms);
      const incoming = [...(snapshot.messages || [])].sort((a, b) => Number(a.seq || 0) - Number(b.seq || 0));
      let expected = lastSeq;
      let gap = false;
      let added = 0;
      for (const message of incoming) {
        const seq = Number(message.seq || 0);
        if (expected > 0 && seq > expected + 1) gap = true;
        const result = storeMessage(message, false);
        if (result.added) added += 1;
        expected = Math.max(expected, seq);
      }
      const advertised = Number(snapshot.last_seq || 0);
      if (advertised > lastSeq && !incoming.length) gap = true;
      lastSeq = Math.max(lastSeq, advertised);

      if (!activeRoom || !rooms.has(activeRoom)) {
        const first = sortedRooms()[0];
        activeRoom = first ? first.id : null;
      }
      renderRooms();
      renderHead();
      renderFeed({stick:initial || followLive, preserve:!initial && !followLive});
      if (!initial && !followLive && added) {
        pendingVisible += added;
        updateJumpButton();
      }
      if (gap && !full && before > 0) scheduleResync();
    }

    async function fetchJSON(path) {
      const response = await fetch(path, {cache:"no-store", headers:{Accept:"application/json"}});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    }

    async function refreshRooms() {
      try {
        mergeRooms(await fetchJSON("/api/rooms"));
        if (!activeRoom) {
          const first = sortedRooms()[0];
          activeRoom = first ? first.id : null;
        }
        renderRooms();
        renderHead();
      } catch (_) {
        setConnection("reconnecting", "Reconnecting");
      }
    }

    async function scheduleResync() {
      if (resyncing) return;
      resyncing = true;
      setConnection("syncing", "Resyncing");
      try {
        const snapshot = await fetchJSON("/api/snapshot");
        applySnapshot(snapshot, {full:true});
        setConnection("live", "Live");
      } catch (_) {
        setConnection(navigator.onLine === false ? "offline" : "reconnecting", navigator.onLine === false ? "Offline" : "Reconnecting");
      } finally {
        resyncing = false;
      }
    }

    function closeNavigation() {
      document.body.classList.remove("nav-open");
      $("nav-toggle").setAttribute("aria-expanded", "false");
    }
    function openNavigation() {
      document.body.classList.add("nav-open");
      $("nav-toggle").setAttribute("aria-expanded", "true");
      requestAnimationFrame(() => (roomBox.querySelector('[aria-current="true"]') || roomSearch).focus());
    }

    roomSearch.addEventListener("input", renderRooms);
    roomFilter.addEventListener("change", renderRooms);
    messageSearch.addEventListener("input", () => { pendingVisible = 0; renderFeed({preserve:true}); });
    messageFilter.addEventListener("change", () => { pendingVisible = 0; renderFeed({preserve:true}); });
    followButton.addEventListener("click", () => setFollow(!followLive, !followLive));
    jumpButton.addEventListener("click", () => setFollow(true, true));
    $("nav-toggle").addEventListener("click", () => document.body.classList.contains("nav-open") ? closeNavigation() : openNavigation());
    $("scrim").addEventListener("click", closeNavigation);
    feed.addEventListener("scroll", () => {
      if (followLive && !nearBottom()) setFollow(false);
      if (nearBottom() && pendingVisible) { pendingVisible = 0; updateJumpButton(); }
    }, {passive:true});
    roomBox.addEventListener("keydown", (event) => {
      if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
      const buttons = [...roomBox.querySelectorAll("button.room")];
      if (!buttons.length) return;
      const index = buttons.indexOf(document.activeElement);
      let next = index;
      if (event.key === "ArrowDown") next = Math.min(buttons.length - 1, index + 1);
      if (event.key === "ArrowUp") next = Math.max(0, index - 1);
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = buttons.length - 1;
      event.preventDefault();
      buttons[next < 0 ? 0 : next].focus();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeNavigation();
      if (event.key === "/" && !["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement.tagName)) {
        event.preventDefault(); messageSearch.focus();
      }
    });
    window.addEventListener("offline", () => setConnection("offline", "Offline"));

    const events = new EventSource("/events");
    events.onopen = () => setConnection("live", "Live");
    events.onerror = () => setConnection(navigator.onLine === false ? "offline" : "reconnecting", navigator.onLine === false ? "Offline" : "Reconnecting");
    events.addEventListener("snapshot", (event) => {
      try {
        const initial = seenMessageIds.size === 0 && seenMessageSeqs.size === 0;
        applySnapshot(JSON.parse(event.data), {initial});
      } catch (_) {
        scheduleResync();
      }
    });
    events.addEventListener("message", (event) => {
      try { acceptLiveMessage(JSON.parse(event.data)); }
      catch (_) { scheduleResync(); }
    });

    // A room normally posts immediately after creation.  This low-frequency
    // refresh also reveals an intentionally quiet room without adding a second
    // event type to the transport.
    setInterval(refreshRooms, 15000);
  })();
  </script>
</body>
</html>
"""
