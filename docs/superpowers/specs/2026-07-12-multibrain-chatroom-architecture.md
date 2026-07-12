# Multi-brain collaboration + chatrooms + review agents

**Date:** 2026-07-12
**Status:** Approved (build all phases; approach delegated → incremental, substrate-first)

## Vision

Genesis should coordinate work like a small firm:

- **Two brains** act as senior peers:
  - Claude brain — Opus 4.8
  - Codex brain — GPT-5.6-sol (high)
  They meet in a **local chatroom**, discuss/ideate, and converge on a plan.
- Brains open **worker chatrooms** with **specialized workers**
  (Sonnet-5, GPT-5.5 high, GPT-5.6-terra high). Workers read context, then
  write code, each specializing in an area.
- **Dedicated review agents** enforce fully-tested code — nothing is accepted
  unless review passes and tests are green.
- Reviewed work goes back to the **brains**, who verify, then **commit and push**.
- Chatrooms are **invisible by default**. When a run launches, Genesis exposes a
  **localhost link** where the user watches the agents communicate live
  (read-only observation).

## Relationship to current architecture

Today `orchestrator.py` has a single brain (`self.agent`) that plans, reviews
its own workers' output, runs verification, and commits/pushes. Communication is
single-shot request/response; there is no agent-to-agent dialogue and no web UI.

This design is an **evolution**, not a rewrite. We keep planning, review,
verification, git, state/resume, and worktree isolation, and layer on:
a chatroom transport, a second collaborating brain, specialized workers,
dedicated reviewers, and a web viewer.

Model identifiers (opus-4.8, gpt-5.6-sol, sonnet-5, gpt-5.5, gpt-5.6-terra) are
`model` strings assigned to roles in config — a thin concern. The substantial
work is coordination, the chatroom substrate, and the viewer.

## Design principles

- **Dependency-light:** the viewer uses the Python standard library
  (`http.server` + Server-Sent Events), no new web framework, matching the
  project's lean footprint.
- **Bounded cost:** every multi-agent discussion has a max-rounds cap and an
  explicit termination/consensus rule so brains cannot loop forever.
- **Isolated, testable units:** the chatroom substrate knows nothing about
  agents or orchestration; the collaboration layer knows nothing about HTTP.
- **Observability first:** all coordination flows through the chatroom, so the
  viewer shows the real conversation, not a reconstruction.

## Phased plan

Each phase is its own spec → plan → implementation cycle. Later-phase detail is
intentionally light; we refine each after learning from the previous one.

### Phase 1 — Chatroom substrate + localhost viewer  ← detailed below
The foundation. Deliver a working, observable message bus wired to the EXISTING
single-orchestrator flow, so the current plan/assign/review/commit events stream
to a localhost page. Proves the substrate before any new agents exist.

### Phase 2 — Dual-brain collaboration
Add the Codex brain as a peer. Brains hold a bounded discussion in a chatroom and
emit a shared plan. Needs a moderator/turn protocol and a consensus rule
(agreement signal or max-rounds → arbitration by the Claude brain for JSON
reliability). Replaces the single `plan()` call.

### Phase 3 — Worker chatrooms + specialization
Brains open a room per work item with assigned specialist workers. Worker roles
carry a specialty (e.g. backend, tests, frontend) that shapes their system
prompt and which steps they're preferred for. Evolves `_assign_worker`.

### Phase 4 — Dedicated review agents + test-gated acceptance
Reviewers become their own agents (separate from brains). A step is accepted only
if the reviewer approves AND verification/tests pass. Evolves `review()` +
verification.

### Phase 5 — Brain verify → commit/push
Brains consume review outcomes, do a final verify, and commit/push. Evolves
`_execute_plan_isolated`'s commit stage; brains own the git decision.

---

## Phase 1 — detailed design

### New package: `genesis/chatroom/`

**`models.py`**
- `RoomKind` (Enum): `brain_room`, `worker_room`, `review_room`, `system`.
- `ChatMessage` (dataclass, frozen): `id: str`, `room_id: str`, `seq: int`,
  `sender: str` (agent name), `role: str` (brain|worker|reviewer|system),
  `kind: str` (message|code|decision|tool|status), `content: str`,
  `ts: float` (epoch seconds). `to_dict()` for JSON/SSE.
- `Room` (dataclass): `id: str`, `kind: RoomKind`, `title: str`,
  `participants: list[str]`, `created_ts: float`.

**`bus.py`** — `ChatroomManager`
- Thread-safe (single `threading.Lock`; Genesis streams on threads).
- `create_room(kind, title, participants) -> Room` (uuid id, monotonic seq).
- `post(room_id, sender, role, content, kind="message") -> ChatMessage`
  (assigns seq, appends to in-memory history, fans out to subscriber queues,
  and — if persistence enabled — appends one JSON line to
  `.genesis/state/chatrooms/<room_id>.jsonl`).
- `history(room_id) -> list[ChatMessage]`, `rooms() -> list[Room]`.
- `subscribe() -> queue.Queue` / `unsubscribe(q)` for the SSE server; each new
  message (all rooms) is pushed to every subscriber queue.
- No agent/HTTP knowledge — pure transport + storage.

**`server.py`** — `ChatroomServer`
- Wraps a `ThreadingHTTPServer` on `host` (default 127.0.0.1) and `port`
  (0 = ephemeral; actual port read back from the socket).
- Routes:
  - `GET /` → self-contained HTML page (inline CSS/JS; no external assets).
  - `GET /api/rooms` → JSON list of rooms.
  - `GET /api/rooms/<id>` → JSON history for a room.
  - `GET /events` → SSE stream; on connect replays current rooms+history, then
    streams new messages from a fresh subscriber queue as `data: {json}\n\n`.
- `start() -> str` returns the URL (`http://host:port`); `stop()` shuts down.
- Runs the server loop on a daemon thread so it never blocks the REPL.

**`viewer` HTML** (inline in `server.py` or a sibling `viewer.py` string)
- Left: room list. Right: live transcript of the selected room.
- Connects to `/events` via `EventSource`; appends messages; colors by role
  (brain / worker / reviewer / system). Read-only — no input controls.
- Theme-neutral, minimal, works offline (everything inlined).

### Config (`genesis/config.py`)

New `ChatroomConfig`:
- `enabled: bool = True`
- `host: str = "127.0.0.1"`
- `port: int = 0`            # 0 = ephemeral
- `persist: bool = True`     # write per-room JSONL under .genesis/state/chatrooms
- `open_browser: bool = False`
Parsed in `load_config` under `[chatroom]`; added to `GenesisConfig`.

### Integration (`genesis/repl.py` + `orchestrator.py`)

- The REPL owns a `ChatroomManager` and (lazily) a `ChatroomServer`.
- When a task run starts (and `chatroom.enabled`), start the server if not
  running and print: `Watch the agents: http://127.0.0.1:PORT`.
- Bridge existing orchestrator callbacks into chatroom posts: create a
  `system`/`brain_room` for the run and post `on_plan`, `on_step_start`,
  `on_review`, `on_commit`, `on_status`, `on_error` as messages. This makes the
  CURRENT single-brain flow fully observable in the viewer with zero new agents.
- Server lifecycle: started on first run, stopped on REPL exit.

### Tests (`tests/test_chatroom.py`)

- Bus: `post` assigns increasing `seq`; `history` returns in order; a subscriber
  queue receives posted messages; unsubscribed queues stop receiving.
- Manager: `create_room` registers a room; `rooms()` lists it; persistence writes
  a JSONL line per message when enabled (temp dir).
- Server smoke: `start()` on port 0 yields a URL; `GET /api/rooms` returns JSON;
  after posting a message an `/events` client receives it. Use `urllib` against
  the ephemeral port; keep it fast and hermetic.

### Non-goals (Phase 1)

- No second brain, specialized workers, or dedicated reviewers yet.
- No user→agent input from the viewer (observation only).
- No authentication (localhost bind only).

## Risks / notes

- SSE over stdlib `http.server` needs careful thread handling (daemon server
  thread, per-client generator, drop slow clients) — covered by the smoke test.
- Bounded-cost discussion rules land in Phase 2; Phase 1 has no agent loops.
- Model-name strings must exist as valid CLI models at run time; wiring is config
  only and validated when those phases land.
