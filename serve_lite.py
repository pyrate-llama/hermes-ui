#!/usr/bin/env python3
"""
Hermes UI — Lite Server v2
Talks directly to Hermes AIAgent (like hermes-webui) — no gateway proxy.
SSE streaming, vanilla Python, zero build step.

Usage:
    python3 serve_lite.py              # http://127.0.0.1:3333
    python3 serve_lite.py --port 8080
"""
import http.server
import json
import os
import signal
import sys
import queue
import threading
import subprocess
import time
import pathlib
import uuid
import traceback
import urllib.parse

# ── Hermes agent discovery ──────────────────────────────────────────────────
HERMES_HOME = os.path.expanduser("~/.hermes")
AGENT_DIR = os.path.join(HERMES_HOME, "hermes-agent")
DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3333

# Add hermes-agent to sys.path so we can import AIAgent directly
if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

# Add hermes-agent venv site-packages so dependencies are available
import glob
_venv_site = glob.glob(os.path.join(AGENT_DIR, "venv", "lib", "python*", "site-packages"))
for _sp in _venv_site:
    if _sp not in sys.path:
        sys.path.insert(1, _sp)

# Lazy-loaded agent class
_AIAgent = None

def _get_ai_agent():
    """Import AIAgent from hermes-agent, retrying if needed."""
    global _AIAgent
    if _AIAgent is None:
        try:
            from run_agent import AIAgent
            _AIAgent = AIAgent
        except ImportError as e:
            print(f"[serve] WARNING: Cannot import AIAgent: {e}", flush=True)
    return _AIAgent


def _resolve_model_and_credentials():
    """Read model/provider from config.yaml and resolve API credentials."""
    import yaml
    config_path = os.path.join(HERMES_HOME, "config.yaml")
    model = "MiniMax-M2.7"
    provider = None
    base_url = None

    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            model = model_cfg.get("default", model)
            provider = model_cfg.get("provider")
            base_url = model_cfg.get("base_url")
        except Exception as e:
            print(f"[serve] WARNING: Failed to read config.yaml: {e}", flush=True)

    # Use Hermes runtime provider to resolve API key
    api_key = None
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        rt = resolve_runtime_provider(requested=provider)
        api_key = rt.get("api_key")
        if not provider:
            provider = rt.get("provider")
        if not base_url:
            base_url = rt.get("base_url")
    except Exception as e:
        print(f"[serve] WARNING: resolve_runtime_provider failed: {e}", flush=True)

    return model, provider, base_url, api_key


# ── Concurrency infrastructure ─────────────────────────────────────────────
# Global lock for os.environ writes — prevents concurrent agent threads from
# clobbering each other's env vars (HERMES_SESSION_KEY, TERMINAL_CWD, etc.)
_ENV_LOCK = threading.Lock()

# Thread-local env context so each agent thread has its own env snapshot
_thread_ctx = threading.local()

def _set_thread_env(**kwargs):
    _thread_ctx.env = kwargs

def _clear_thread_env():
    _thread_ctx.env = {}

# Per-session agent locks — prevents two requests on the SAME session
# from running concurrently (different sessions can still run in parallel)
_SESSION_LOCKS = {}
_SESSION_LOCKS_LOCK = threading.Lock()

def _get_session_lock(session_id):
    with _SESSION_LOCKS_LOCK:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = threading.Lock()
        return _SESSION_LOCKS[session_id]


# ── Streaming infrastructure ────────────────────────────────────────────────
# stream_id -> queue.Queue of (event, data) tuples
STREAMS = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS = {}   # stream_id -> threading.Event
AGENT_INSTANCES = {} # stream_id -> agent instance (for cancel/interrupt)

# session_id -> dict (in-memory cache, persisted to disk like webui)
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()

# Disk persistence — matches webui SESSION_DIR pattern
SESSION_DIR = os.path.join(HERMES_HOME, "hermes-ui", "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)


def _save_session(session_id, session_data):
    """Persist session to disk as JSON (matches webui Session.save())."""
    try:
        path = os.path.join(SESSION_DIR, f"{session_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session_data, f, ensure_ascii=False)
    except Exception as e:
        print(f"[serve] WARNING: Failed to save session {session_id}: {e}", flush=True)


def _load_session(session_id):
    """Load session from disk (matches webui Session.load())."""
    if not session_id or not all(c in "0123456789abcdefghijklmnopqrstuvwxyz_" for c in session_id):
        return None
    path = os.path.join(SESSION_DIR, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[serve] WARNING: Failed to load session {session_id}: {e}", flush=True)
        return None


def _flush_all_sessions():
    """Save all in-memory sessions to disk. Called on shutdown/restart."""
    with SESSIONS_LOCK:
        ids = list(SESSIONS.keys())
    saved = 0
    for sid in ids:
        try:
            with SESSIONS_LOCK:
                data = SESSIONS.get(sid)
            if data:
                _save_session(sid, data)
                saved += 1
        except Exception as e:
            print(f"[serve] WARNING: flush failed for {sid}: {e}", flush=True)
    if saved:
        print(f"[serve] Flushed {saved} session(s) to disk.", flush=True)

def _get_or_create_session(session_id):
    """Get or create session — checks memory first, then disk (matches webui get_session())."""
    with SESSIONS_LOCK:
        if session_id in SESSIONS:
            return SESSIONS[session_id]
    # Try loading from disk
    loaded = _load_session(session_id)
    if loaded:
        with SESSIONS_LOCK:
            SESSIONS[session_id] = loaded
        return loaded
    # Create new
    new_session = {"messages": [], "model": None}
    with SESSIONS_LOCK:
        SESSIONS[session_id] = new_session
    return new_session


def _run_agent_streaming(session_id, messages, stream_id, base_system_prompt=""):
    """Run AIAgent in a background thread, pushing SSE events to the queue."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event

    def put(event, data):
        if cancel_event.is_set() and event not in ("cancel", "error"):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            pass

    # Set thread-local env context for this agent thread
    _set_thread_env(
        TERMINAL_CWD=os.path.expanduser("~"),
        HERMES_SESSION_KEY=session_id,
    )

    # Save and set process-level env vars under lock
    with _ENV_LOCK:
        old_cwd = os.environ.get("TERMINAL_CWD")
        old_exec_ask = os.environ.get("HERMES_EXEC_ASK")
        old_session_key = os.environ.get("HERMES_SESSION_KEY")
        os.environ["TERMINAL_CWD"] = os.path.expanduser("~")
        os.environ.pop("HERMES_EXEC_ASK", None)
        os.environ["HERMES_SESSION_KEY"] = session_id

    _approval_registered = False
    _unreg_notify = None

    try:
        # Check for pre-flight cancel
        if cancel_event.is_set():
            put("cancel", {"message": "Cancelled before start"})
            return

        AgentClass = _get_ai_agent()
        if AgentClass is None:
            put("error", {"message": "AIAgent not available — check hermes-agent installation"})
            return

        model, provider, base_url, api_key = _resolve_model_and_credentials()

        # Initialize SessionDB for session_search
        _session_db = None
        try:
            from hermes_state import SessionDB
            _session_db = SessionDB()
        except Exception:
            pass

        # Resolve toolsets via the agent's own function so MCP server toolsets
        # are included — matches nesquena/hermes-webui api/streaming.py.
        # Our previous raw config read returned ['hermes-cli'] which skipped MCP
        # discovery entirely, so the model had no MCP tools to call and narrated
        # tool use instead of emitting tool_calls.
        try:
            from hermes_cli.tools_config import _get_platform_tools
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()  # idempotent; lazy MCP server init
            import yaml
            cfg_path = os.path.join(HERMES_HOME, "config.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            toolsets = list(_get_platform_tools(cfg, "cli"))
            print(f"[serve] resolved cli toolsets ({len(toolsets)}): {toolsets}", flush=True)
        except Exception as _e:
            print(f"[serve] WARNING: toolset resolution fallback ({_e})", flush=True)
            toolsets = ["hermes-cli"]

        full_text = ""
        _token_sent = False

        def on_token(text):
            nonlocal full_text, _token_sent
            if text is None:
                return
            _token_sent = True
            full_text += text
            put("token", {"text": text})

        def on_reasoning(text):
            if text is None:
                return
            put("reasoning", {"text": str(text)})

        def on_tool(event_type=None, name=None, preview=None, args=None, **kwargs):
            if isinstance(event_type, str) and event_type in ("reasoning.available", "_thinking"):
                reason_text = name if event_type == "_thinking" else preview
                if reason_text:
                    put("reasoning", {"text": str(reason_text)})
                return

            args_snap = {}
            if isinstance(args, dict):
                for k, v in list(args.items())[:4]:
                    s = str(v)
                    args_snap[k] = s[:120] + ("..." if len(s) > 120 else "")

            if event_type in (None, "tool.started"):
                put("tool", {"name": name, "preview": preview, "args": args_snap})
            elif event_type == "tool.completed":
                put("tool_complete", {
                    "name": name, "preview": preview, "args": args_snap,
                    "duration": kwargs.get("duration"),
                })

        # Build the agent
        agent = AgentClass(
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            platform="cli",
            quiet_mode=True,
            enabled_toolsets=toolsets,
            session_id=session_id,
            session_db=_session_db,
            stream_delta_callback=on_token,
            reasoning_callback=on_reasoning,
            tool_progress_callback=on_tool,
        )

        # User-configurable base system prompt from Settings → General.
        # Passed via agent.ephemeral_system_prompt — the library's sanctioned
        # slot for per-session personality/style injection.  Matches the
        # personality-injection pattern in nesquena/hermes-webui api/streaming.py
        # (which pulls from config.yaml agent.personalities; we read from a
        # UI field instead, but use the same agent attribute).
        if base_system_prompt:
            agent.ephemeral_system_prompt = base_system_prompt

        # Store agent instance for cancel/interrupt
        with STREAMS_LOCK:
            AGENT_INSTANCES[stream_id] = agent
            if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
                try:
                    agent.interrupt("Cancelled before start")
                except Exception:
                    pass
                put("cancel", {"message": "Cancelled by user"})
                return

        # Register approval callback so dangerous tool calls don't hang forever
        # Without this, the agent blocks waiting for approval the UI never shows
        _approval_registered = False
        _unreg_notify = None
        try:
            from tools.approval import (
                register_gateway_notify as _reg_notify,
                unregister_gateway_notify as _unreg_notify_fn,
            )
            _unreg_notify = _unreg_notify_fn
            def _approval_notify_cb(approval_data):
                put("approval", approval_data)
            _reg_notify(session_id, _approval_notify_cb)
            _approval_registered = True
        except ImportError:
            pass

        # Extract just the latest user message text
        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        # Workspace context prefix (matches hermes-webui behaviour)
        _workspace = os.path.expanduser("~")
        workspace_ctx = f"[Workspace: {_workspace}]\n"
        workspace_system_msg = (
            f"Active workspace: {_workspace}\n"
            "Every user message is prefixed with [Workspace: /path] indicating the "
            "active workspace. Use this as the default working directory for all "
            "file operations.\n"
            "\n"
            "── Tool-use policy (strict) ──\n"
            "When the user asks you to test, check, query, show, list, fetch, "
            "retrieve, search, verify, or otherwise inspect anything that maps "
            "to a tool you have — YOU MUST EMIT A tool_call. Do not narrate, "
            "summarize, predict, or invent tool outputs in your thinking or "
            "your reply. Producing fabricated output in reasoning tokens and "
            "claiming it came from a tool is a critical failure.\n"
            "\n"
            "If unsure which tool to call, pick the closest match and call it — "
            "the user will correct you. One real tool_call beats ten sentences "
            "of speculation.\n"
            "\n"
            "Available MCP tool namespaces (use the full prefixed name):\n"
            "  • mcp_heygen_*            — HeyGen: video/avatar creation, "
            "account info, credits\n"
            "  • mcp_claude_code_*       — full Claude Code coding assistant\n"
            "  • mcp_nano_claude_code_*  — lightweight Claude Code for simple "
            "tasks\n"
            "  • mcp_context_keep_*      — persistent memory: store / retrieve "
            "/ search\n"
            "Examples of correct names: mcp_heygen_get_current_user, "
            "mcp_heygen_list_videos, mcp_context_keep_list_all_memories, "
            "mcp_context_keep_search_memories. Never invent short aliases like "
            "'get_current_user' or 'list_videos' — those do not exist."
        )

        # Build conversation history from SERVER-SIDE session (not frontend messages).
        # The server session includes tool_calls and tool results from prior turns;
        # the frontend only sends {role, content} pairs which strips tool context
        # and makes the agent forget it has tools.  Matches webui's approach:
        #   conversation_history=_sanitize_messages_for_api(s.messages)
        session = _get_or_create_session(session_id)
        safe_keys = {"role", "content", "tool_calls", "tool_call_id", "name", "refusal"}

        # Use server-side session messages; fall back to frontend messages if
        # server has none (e.g. first run after adding persistence, or old sessions).
        # Also prefer frontend messages when they have more content — this handles
        # cases where the server session was compacted/truncated (e.g. after a
        # restart) but the frontend still has the full conversation.
        server_msgs = session.get("messages") or []
        frontend_msgs = messages or []
        server_user_count = sum(1 for m in server_msgs if isinstance(m, dict) and m.get("role") == "user")
        frontend_user_count = sum(1 for m in frontend_msgs if isinstance(m, dict) and m.get("role") == "user")
        if frontend_user_count > server_user_count:
            history_source = frontend_msgs
        elif server_msgs:
            history_source = server_msgs
        else:
            history_source = frontend_msgs

        clean_history = []
        for m in history_source:
            if not isinstance(m, dict):
                continue
            sanitized = {k: v for k, v in m.items() if k in safe_keys}
            if sanitized.get("role"):
                clean_history.append(sanitized)
        # Remove the last user message — it goes in user_message param instead
        if clean_history and clean_history[-1].get("role") == "user":
            clean_history.pop()

        result = agent.run_conversation(
            user_message=workspace_ctx + user_msg,
            system_message=workspace_system_msg,
            conversation_history=clean_history,
            task_id=session_id,
            persist_user_message=user_msg,
        )

        # Update session with agent's messages (includes tool_calls + tool results)
        session["messages"] = result.get("messages", session["messages"])
        _save_session(session_id, session)  # Persist to disk (matches webui s.save())

        # Detect silent agent failure (no assistant reply produced)
        _assistant_added = any(
            m.get("role") == "assistant" and str(m.get("content") or "").strip()
            for m in (result.get("messages") or [])
        )
        if not _assistant_added and not _token_sent:
            _last_err = getattr(agent, "_last_error", None) or result.get("error") or ""
            _err_str = str(_last_err) if _last_err else ""
            _is_auth = (
                "401" in _err_str
                or "authentication" in _err_str.lower()
                or "unauthorized" in _err_str.lower()
                or "invalid api key" in _err_str.lower()
            )
            if _is_auth:
                put("apperror", {
                    "message": _err_str or "Authentication failed — check your API key.",
                    "type": "auth_mismatch",
                })
            else:
                put("apperror", {
                    "message": _err_str or "The agent returned no response. Check your API key and model selection.",
                    "type": "no_response",
                })
            return  # Don't send done — apperror already closes the stream

        # ── Handle context compression side effects ──
        # Mirrors nesquena/hermes-webui api/streaming.py lines 1160-1192.
        # If compression fired inside run_conversation, the agent rotated its
        # session_id. Rename the session file and remap SESSIONS so subsequent
        # turns keep writing to the correct file. Also emit a 'compressed'
        # SSE event so the frontend can show a toast.
        _agent_sid = getattr(agent, "session_id", None)
        _compressed = False
        if _agent_sid and _agent_sid != session_id:
            old_sid, new_sid = session_id, _agent_sid
            old_path = os.path.join(SESSION_DIR, f"{old_sid}.json")
            new_path = os.path.join(SESSION_DIR, f"{new_sid}.json")
            with SESSIONS_LOCK:
                if old_sid in SESSIONS:
                    SESSIONS[new_sid] = SESSIONS.pop(old_sid)
            if os.path.exists(old_path) and not os.path.exists(new_path):
                try:
                    os.rename(old_path, new_path)
                except OSError:
                    print(f"[serve] WARNING: rename {old_sid}->{new_sid} failed", flush=True)
            session_id = new_sid  # so 'done' event reports the new id
            _compressed = True
        if not _compressed:
            _compressor = getattr(agent, "context_compressor", None)
            if _compressor and getattr(_compressor, "compression_count", 0) > 0:
                _compressed = True
        if _compressed:
            put("compressed", {"message": "Context auto-compressed to continue the conversation"})

        # Gather usage stats
        input_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
        output_tokens = getattr(agent, "session_completion_tokens", 0) or 0
        estimated_cost = getattr(agent, "session_estimated_cost_usd", None)

        put("done", {
            "session": {
                "session_id": session_id,
                "messages": session.get("messages", []),
            },
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost": estimated_cost,
            },
        })

    except Exception as e:
        print(f"[serve] stream error:\n{traceback.format_exc()}", flush=True)
        put("error", {"message": str(e)})
    finally:
        # Unregister approval callback
        if _approval_registered and _unreg_notify is not None:
            try:
                _unreg_notify(session_id)
            except Exception:
                pass
        # Restore env vars under lock
        with _ENV_LOCK:
            if old_cwd is None: os.environ.pop("TERMINAL_CWD", None)
            else: os.environ["TERMINAL_CWD"] = old_cwd
            if old_exec_ask is None: os.environ.pop("HERMES_EXEC_ASK", None)
            else: os.environ["HERMES_EXEC_ASK"] = old_exec_ask
            if old_session_key is None: os.environ.pop("HERMES_SESSION_KEY", None)
            else: os.environ["HERMES_SESSION_KEY"] = old_session_key
        _clear_thread_env()
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            AGENT_INSTANCES.pop(stream_id, None)


def cancel_stream(stream_id):
    """Signal an in-flight stream to cancel. Returns True if the stream existed."""
    with STREAMS_LOCK:
        if stream_id not in STREAMS:
            return False
        flag = CANCEL_FLAGS.get(stream_id)
        if flag:
            flag.set()
        agent = AGENT_INSTANCES.get(stream_id)
        if agent:
            try:
                agent.interrupt("Cancelled by user")
            except Exception:
                pass
        q = STREAMS.get(stream_id)
        if q:
            try:
                q.put_nowait(("cancel", {"message": "Cancelled by user"}))
            except Exception:
                pass
    return True


# ── HTTP Server ─────────────────────────────────────────────────────────────

class HermesDirectServer(http.server.SimpleHTTPRequestHandler):
    """Serves hermes-ui.html and runs Hermes agent directly (no gateway)."""

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    # ── JSON helper ──
    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_event(self, event, data):
        """Write one SSE event."""
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    # ── Chat: two-step flow matching hermes-webui ──
    # Step 1: POST /api/chat/start → returns {stream_id, session_id}
    # Step 2: GET  /api/chat/stream?stream_id=X → SSE with named events

    def _handle_chat_start(self):
        """Start agent in background thread, return stream_id."""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        session_id = body.get("session_id") or self.headers.get("X-Hermes-Session-Id") or f"web_{uuid.uuid4().hex[:12]}"
        # User-configurable base system prompt from Settings → General
        base_system_prompt = (body.get("base_system_prompt") or "").strip()

        if not messages:
            return self._json({"error": "No messages provided"}, 400)

        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        thr = threading.Thread(
            target=_run_agent_streaming,
            args=(session_id, messages, stream_id, base_system_prompt),
            daemon=True,
        )
        thr.start()

        self._json({"stream_id": stream_id, "session_id": session_id})

    def _handle_chat_stream(self):
        """SSE endpoint — forwards ALL events from the queue via named events."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        q = STREAMS.get(stream_id)
        if q is None:
            return self._json({"error": "stream not found"}, 404)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            while True:
                try:
                    event, data = q.get(timeout=30)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    continue

                # Forward ALL events as named SSE events (matches webui _sse())
                self._sse_event(event, data)

                if event in ("done", "error", "apperror", "cancel"):
                    break
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_chat_stream_status(self):
        """Check if a stream is still active."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        self._json({"active": stream_id in STREAMS, "stream_id": stream_id})

    def _handle_chat_sync(self, messages, session_id):
        """Synchronous chat fallback (no streaming)."""
        try:
            AgentClass = _get_ai_agent()
            if AgentClass is None:
                return self._json({"error": "AIAgent not available"}, 500)
            model, provider, base_url, api_key = _resolve_model_and_credentials()
            agent = AgentClass(
                model=model, provider=provider, base_url=base_url,
                api_key=api_key, platform="cli", quiet_mode=True,
                session_id=session_id,
            )
            user_msg = ""
            for m in reversed(messages):
                if m.get("role") == "user":
                    user_msg = m["content"]
                    break
            result = agent.run_conversation(user_message=user_msg, task_id=session_id)
            assistant_text = ""
            for m in reversed(result.get("messages", [])):
                if m.get("role") == "assistant" and m.get("content"):
                    assistant_text = m["content"] if isinstance(m["content"], str) else str(m["content"])
                    break
            return self._json({
                "choices": [{"message": {"role": "assistant", "content": assistant_text}, "index": 0}],
            })
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # ── Cancel endpoint ──
    def _handle_cancel(self):
        """Cancel a stream. Accepts GET ?stream_id=X or POST {stream_id}."""
        from urllib.parse import urlparse, parse_qs
        if self.command == "GET":
            parsed = urlparse(self.path)
            stream_id = parse_qs(parsed.query).get("stream_id", [""])[0]
        else:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            stream_id = body.get("stream_id", "")
        if not stream_id:
            return self._json({"error": "stream_id required"}, 400)
        ok = cancel_stream(stream_id)
        self._json({"ok": True, "cancelled": ok, "stream_id": stream_id})

    # ── Health endpoint ──
    def _handle_health(self):
        agent_ok = _get_ai_agent() is not None
        model, provider, _, _ = _resolve_model_and_credentials() if agent_ok else ("?", "?", None, None)
        self._json({
            "status": "ok" if agent_ok else "degraded",
            "agent": agent_ok,
            "model": model,
            "provider": provider,
            "uptime": int(time.time() - _START_TIME),
        })

    # ── UI conversations (local JSON file) ──
    CONV_PATH = os.path.join(HERMES_HOME, "ui-conversations.json")

    def _conversations_load(self):
        try:
            data = json.load(open(self.CONV_PATH)) if os.path.exists(self.CONV_PATH) else []
        except Exception:
            data = []
        self._json(data)

    def _conversations_save(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            data = json.loads(body)
            json.dump(data, open(self.CONV_PATH, "w"), indent=2)
            self._json({"ok": True})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ── Local file access ──
    def _read_local_file(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        fpath = params.get("path", [""])[0]
        if not fpath or not os.path.isfile(fpath):
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(open(fpath, "rb").read())

    def _serve_image(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        fpath = params.get("path", [""])[0]
        if not fpath or not os.path.isfile(fpath):
            self.send_error(404, "Image not found")
            return
        import mimetypes
        ct = mimetypes.guess_type(fpath)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(open(fpath, "rb").read())

    # ── Log streaming ──
    def _stream_logs(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        logs_str = params.get("logs", ["gateway"])[0]
        tail = int(params.get("tail", ["80"])[0])
        log_names = [n.strip() for n in logs_str.split(",") if n.strip()]

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        log_dir = os.path.join(HERMES_HOME, "logs")
        positions = {}
        for name in log_names:
            fpath = os.path.join(log_dir, f"{name}.log")
            if os.path.isfile(fpath):
                try:
                    lines = open(fpath).readlines()[-tail:]
                    for line in lines:
                        self.wfile.write(f"data: {json.dumps({'log': name, 'line': line.rstrip()})}\n\n".encode())
                    self.wfile.flush()
                except Exception:
                    pass
                try:
                    positions[name] = os.path.getsize(fpath)
                except Exception:
                    positions[name] = 0
        try:
            while True:
                for name in log_names:
                    fpath = os.path.join(log_dir, f"{name}.log")
                    try:
                        size = os.path.getsize(fpath)
                        if size > positions.get(name, 0):
                            with open(fpath) as f:
                                f.seek(positions[name])
                                new_data = f.read()
                                for line in new_data.splitlines():
                                    self.wfile.write(f"data: {json.dumps({'log': name, 'line': line})}\n\n".encode())
                                self.wfile.flush()
                            positions[name] = size
                        elif size < positions.get(name, 0):
                            positions[name] = 0
                    except Exception:
                        pass
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ── Terminal exec ──
    def _terminal_exec(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        cmd = body.get("command", "")
        if not cmd:
            self._json({"error": "No command"}, 400)
            return
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=120,
                cwd=os.path.expanduser("~"),
            )
            self._json({
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.returncode,
            })
        except subprocess.TimeoutExpired:
            self._json({"error": "Command timed out (120s)"}, 504)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ── Browse / Read / Write files ──
    def _browse_dir(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        browse_path = params.get("path", [""])[0]
        base = HERMES_HOME
        full = os.path.normpath(os.path.join(base, browse_path)) if browse_path else base
        if not full.startswith(base):
            self.send_error(403, "Access denied")
            return
        if not os.path.isdir(full):
            self._json({"entries": [], "error": "Not a directory"})
            return
        entries = []
        try:
            for name in sorted(os.listdir(full)):
                fp = os.path.join(full, name)
                entries.append({
                    "name": name,
                    "type": "dir" if os.path.isdir(fp) else "file",
                    "size": os.path.getsize(fp) if os.path.isfile(fp) else 0,
                })
        except Exception as e:
            entries = [{"error": str(e)}]
        self._json({"items": entries, "path": browse_path})

    def _read_file(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        fpath = params.get("path", [""])[0]
        full = os.path.normpath(os.path.join(HERMES_HOME, fpath)) if fpath else ""
        if not full or not full.startswith(HERMES_HOME) or not os.path.isfile(full):
            self._json({"content": "(File not found)", "name": "", "path": fpath, "size": 0, "type": "text"}, 404)
            return
        try:
            content = open(full, "r", encoding="utf-8", errors="replace").read()
        except Exception:
            content = "(Could not read file)"
        name = os.path.basename(full)
        size = os.path.getsize(full)
        ext = os.path.splitext(name)[1].lower()
        ftype = "image" if ext in (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp") else "text"
        self._json({"content": content, "name": name, "path": fpath, "size": size, "type": ftype})

    def _write_file(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        fpath = body.get("path", "")
        content = body.get("content", "")
        full = os.path.normpath(os.path.join(HERMES_HOME, fpath)) if fpath else ""
        if not full or not full.startswith(HERMES_HOME):
            self.send_error(403, "Access denied")
            return
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write(content)
        self._json({"ok": True})

    # ── Memory API (reads ~/.hermes/memories/MEMORY.md & USER.md) ──
    MEMORY_DIR = os.path.join(HERMES_HOME, "memories")

    def _build_memory_targets(self):
        """Build targets array from memory files for the frontend."""
        targets = []
        for target_name, fname in [("memory", "MEMORY.md"), ("user", "USER.md")]:
            fpath = os.path.join(self.MEMORY_DIR, fname)
            content = ""
            if os.path.isfile(fpath):
                try:
                    content = open(fpath, "r", encoding="utf-8", errors="replace").read()
                except Exception:
                    content = ""
            # Split by --- separators into entries (matching frontend expectations)
            if content.strip():
                entries = [e.strip() for e in content.split("\n---\n") if e.strip()]
            else:
                entries = []
            usage = f"{len(content)} chars, {len(entries)} entries"
            targets.append({"target": target_name, "entries": entries, "usage": usage})
        return targets

    def _handle_memory(self):
        self._json({"targets": self._build_memory_targets()})

    def _handle_memory_write(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        os.makedirs(self.MEMORY_DIR, exist_ok=True)
        # PUT format: {memory: "...", user_profile: "..."}
        if "memory" in body:
            open(os.path.join(self.MEMORY_DIR, "MEMORY.md"), "w", encoding="utf-8").write(body["memory"])
        if "user_profile" in body:
            open(os.path.join(self.MEMORY_DIR, "USER.md"), "w", encoding="utf-8").write(body["user_profile"])
        # Also support legacy POST format: {filename, content}
        if "filename" in body and "content" in body:
            fname = body["filename"]
            if fname in ("MEMORY.md", "USER.md"):
                open(os.path.join(self.MEMORY_DIR, fname), "w", encoding="utf-8").write(body["content"])
        self._json({"targets": self._build_memory_targets()})

    # ── Cron API ──
    def _handle_cron_list(self):
        try:
            from cron.jobs import list_jobs
            jobs = list_jobs(include_disabled=True)
            self._json({"jobs": jobs})
        except ImportError:
            self._json({"jobs": [], "error": "cron module not available"})
        except Exception as e:
            self._json({"jobs": [], "error": str(e)})

    # ── Skills API ──
    def _handle_skills(self):
        try:
            from tools.skills_tool import skills_list
            raw = skills_list()
            data = json.loads(raw) if isinstance(raw, str) else raw
            self._json(data)
        except ImportError:
            self._json({"skills": []})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_skill_content(self):
        """GET /api/skills/content?name=X — load skill SKILL.md (matches webui)."""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        name = qs.get("name", [""])[0]
        if not name:
            return self._json({"error": "name required"}, 400)
        try:
            from tools.skills_tool import skill_view
            raw = skill_view(name)
            data = json.loads(raw) if isinstance(raw, str) else raw
            self._json(data)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_skill_save(self):
        """POST /api/skills/save — save skill content (matches webui)."""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        name = body.get("name", "").strip().lower().replace(" ", "-")
        content = body.get("content", "")
        if not name or "/" in name or ".." in name:
            return self._json({"error": "Invalid skill name"}, 400)
        try:
            from tools.skills_tool import SKILLS_DIR
            category = body.get("category", "").strip()
            if category:
                skill_dir = SKILLS_DIR / category / name
            else:
                skill_dir = SKILLS_DIR / name
            skill_dir.resolve().relative_to(SKILLS_DIR.resolve())
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(content, encoding="utf-8")
            self._json({"success": True, "name": name, "path": str(skill_file)})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    # ── Skills dates API ──
    def _handle_skills_dates(self):
        """GET /skills/dates — return mtime of each SKILL.md for newest-first sorting."""
        try:
            from tools.skills_tool import SKILLS_DIR
            dates = {}
            if SKILLS_DIR.exists():
                for skill_md in SKILLS_DIR.rglob("SKILL.md"):
                    try:
                        content = skill_md.read_text(encoding="utf-8")[:500]
                        # Extract name from frontmatter
                        name = skill_md.parent.name
                        if content.startswith("---"):
                            for line in content.split("\n")[1:]:
                                if line.strip() == "---":
                                    break
                                if line.startswith("name:"):
                                    parsed = line.split(":", 1)[1].strip().strip('"').strip("'")
                                    if parsed:
                                        name = parsed
                        dates[name] = int(skill_md.stat().st_mtime)
                    except Exception:
                        continue
            self._json({"dates": dates})
        except Exception as e:
            self._json({"dates": {}})

    # ── Request routing ──
    def do_GET(self):
        if self.path == "/health":
            self._handle_health()
        elif self.path.startswith("/api/chat/stream/status"):
            self._handle_chat_stream_status()
        elif self.path.startswith("/api/chat/stream"):
            self._handle_chat_stream()
        elif self.path.startswith("/api/chat/cancel"):
            self._handle_cancel()
        elif self.path == "/api/ui-conversations":
            self._conversations_load()
        elif self.path.startswith("/logs/stream"):
            self._stream_logs()
        elif self.path.startswith("/browse"):
            self._browse_dir()
        elif self.path.startswith("/readfile"):
            self._read_file()
        elif self.path.startswith("/api/localfile"):
            self._read_local_file()
        elif self.path.startswith("/api/image"):
            self._serve_image()
        elif self.path == "/api/memory" or self.path.startswith("/api/memory?"):
            self._handle_memory()
        elif self.path == "/skills/dates" or self.path.startswith("/skills/dates?"):
            self._handle_skills_dates()
        elif self.path.startswith("/api/skills/content"):
            self._handle_skill_content()
        elif self.path == "/api/skills" or self.path.startswith("/api/skills?"):
            self._handle_skills()
        elif self.path == "/cron/list":
            self._handle_cron_list()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/chat/start":
            self._handle_chat_start()
        elif self.path == "/api/chat/cancel":
            self._handle_cancel()
        elif self.path == "/v1/chat/completions" or self.path == "/api/chat":
            self._handle_chat_start()  # backwards compat — same two-step flow
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        elif self.path == "/api/ui-conversations":
            self._conversations_save()
        elif self.path.startswith("/writefile"):
            self._write_file()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        elif self.path == "/api/skills/save":
            self._handle_skill_save()
        elif self.path.startswith("/server/pull-restart"):
            self._server_pull_restart()
        elif self.path.startswith("/server/restart"):
            self._server_restart()
        else:
            self._json({"error": "Not found"}, 404)

    def do_PUT(self):
        if self.path == "/api/memory":
            self._handle_memory_write()
        else:
            self._json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hermes-Session-Id")
        self.end_headers()

    def _server_restart(self):
        self._json({"ok": True, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
            _flush_all_sessions()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()

    def _server_pull_restart(self):
        """Run `git pull --ff-only` in the hermes-ui repo, then restart.
        Response includes the pull output so the UI can display it."""
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        try:
            result = subprocess.run(
                ["git", "-C", repo_dir, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=30,
            )
            pull_output = ((result.stdout or "") + (result.stderr or "")).strip() or "ok"
            if result.returncode != 0:
                pull_output = f"error (rc={result.returncode}): {pull_output}"
        except Exception as e:
            pull_output = f"error: {e}"
        self._json({"ok": True, "pull": pull_output, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
            _flush_all_sessions()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()

    def log_message(self, fmt, *args):
        if args and isinstance(args[0], str) and args[0].startswith("2"):
            return
        super().log_message(fmt, *args)


class ThreadedServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


_START_TIME = time.time()

def _shutdown_handler(signum, frame):
    """Graceful shutdown — flush sessions before exiting."""
    sig_name = signal.Signals(signum).name if hasattr(signal, 'Signals') else str(signum)
    print(f"\n[serve] Received {sig_name}, flushing sessions...", flush=True)
    _flush_all_sessions()
    print("[serve] Goodbye.", flush=True)
    sys.exit(0)

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Hermes UI Direct Server")
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    # Verify agent is importable
    agent_ok = _get_ai_agent() is not None
    model_name = "?"
    provider_name = "?"
    if agent_ok:
        try:
            model_name, provider_name, _, _ = _resolve_model_and_credentials()
        except Exception:
            pass

    print(f"╭─ Hermes UI Direct ───────────────────╮")
    print(f"│  UI:       http://127.0.0.1:{args.port:<5}    │")
    print(f"│  Agent:    {'✓ loaded' if agent_ok else '✗ not found':<25s} │")
    print(f"│  Model:    {str(model_name)[:25]:<25s} │")
    print(f"│  Provider: {str(provider_name)[:25]:<25s} │")
    print(f"│  Mode:     Direct (no gateway)       │")
    print(f"╰───────────────────────────────────────╯")

    if not agent_ok:
        print(f"\n[!!] WARNING: Could not import AIAgent from {AGENT_DIR}")
        print(f"     Chat will not work. Check that hermes-agent is installed.")

    server = ThreadedServer(("0.0.0.0", args.port), HermesDirectServer)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
