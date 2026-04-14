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

# session_id -> list of message dicts (in-memory conversation store)
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def _get_or_create_session(session_id):
    """Get or create an in-memory session with conversation history."""
    with SESSIONS_LOCK:
        if session_id not in SESSIONS:
            SESSIONS[session_id] = {"messages": [], "model": None}
        return SESSIONS[session_id]


def _run_agent_streaming(session_id, messages, stream_id):
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
        HERMES_EXEC_ASK="1",
        HERMES_SESSION_KEY=session_id,
    )

    # Save and set process-level env vars under lock
    with _ENV_LOCK:
        old_cwd = os.environ.get("TERMINAL_CWD")
        old_exec_ask = os.environ.get("HERMES_EXEC_ASK")
        old_session_key = os.environ.get("HERMES_SESSION_KEY")
        os.environ["TERMINAL_CWD"] = os.path.expanduser("~")
        os.environ["HERMES_EXEC_ASK"] = "1"
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

        # Read toolsets from config
        toolsets = ["cli"]
        try:
            import yaml
            cfg_path = os.path.join(HERMES_HOME, "config.yaml")
            if os.path.exists(cfg_path):
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
                pt = cfg.get("platform_toolsets", {})
                if isinstance(pt, dict) and "cli" in pt:
                    toolsets = pt["cli"]
        except Exception:
            pass

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
            "file operations."
        )

        # Build conversation history (everything except the last user message)
        history = []
        for m in messages:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})
        if history and history[-1]["role"] == "user":
            history.pop()

        safe_keys = {"role", "content", "tool_calls", "tool_call_id", "name"}
        clean_history = [{k: v for k, v in m.items() if k in safe_keys} for m in history]

        result = agent.run_conversation(
            user_message=workspace_ctx + user_msg,
            system_message=workspace_system_msg,
            conversation_history=clean_history,
            task_id=session_id,
            persist_user_message=user_msg,
        )

        # Update session with agent's messages
        session = _get_or_create_session(session_id)
        session["messages"] = result.get("messages", session["messages"])

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
                put("error", {"message": _err_str or "Authentication failed — check your API key."})
            else:
                put("error", {"message": _err_str or "The agent returned no response. Check your API key and model selection."})
            return  # Don't send done — error already closes the stream

        # Gather usage stats
        input_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
        output_tokens = getattr(agent, "session_completion_tokens", 0) or 0
        estimated_cost = getattr(agent, "session_estimated_cost_usd", None)

        put("done", {
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost": estimated_cost,
            },
            "full_text": full_text,
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

    # ── Chat: POST /v1/chat/completions (OpenAI-compatible SSE) ──
    def _handle_chat(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        stream = body.get("stream", True)
        session_id = self.headers.get("X-Hermes-Session-Id") or f"web_{uuid.uuid4().hex[:12]}"

        if not messages:
            return self._json({"error": "No messages provided"}, 400)

        if not stream:
            return self._handle_chat_sync(messages, session_id)

        # Create a stream queue and start the agent thread
        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        thr = threading.Thread(
            target=_run_agent_streaming,
            args=(session_id, messages, stream_id),
            daemon=True,
        )
        thr.start()

        # Stream SSE response in OpenAI format
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Hermes-Session-Id, X-Stream-Id")
        self.send_header("X-Hermes-Session-Id", session_id)
        self.send_header("X-Stream-Id", stream_id)
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

                if event == "token":
                    chunk = {
                        "choices": [{
                            "delta": {"content": data["text"]},
                            "index": 0,
                        }]
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()

                elif event == "reasoning":
                    chunk = {
                        "choices": [{
                            "delta": {"reasoning": data["text"]},
                            "index": 0,
                        }]
                    }
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()

                elif event == "tool":
                    self.wfile.write(f"event: tool\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

                elif event == "tool_complete":
                    self.wfile.write(f"event: tool_complete\ndata: {json.dumps(data)}\n\n".encode())
                    self.wfile.flush()

                elif event == "done":
                    usage = data.get("usage", {})
                    if usage:
                        done_chunk = {
                            "choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}],
                            "usage": usage,
                        }
                        self.wfile.write(f"data: {json.dumps(done_chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    break

                elif event == "error":
                    err_chunk = {"error": {"message": data.get("message", "Unknown error")}}
                    self.wfile.write(f"data: {json.dumps(err_chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    break

                elif event == "cancel":
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    break

        except (BrokenPipeError, ConnectionResetError):
            pass

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
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        stream_id = body.get("stream_id", "")
        if not stream_id:
            return self._json({"error": "No stream_id"}, 400)
        ok = cancel_stream(stream_id)
        self._json({"cancelled": ok})

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
        self._json({"entries": entries, "path": browse_path})

    def _read_file(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        fpath = params.get("path", [""])[0]
        full = os.path.normpath(os.path.join(HERMES_HOME, fpath)) if fpath else ""
        if not full or not full.startswith(HERMES_HOME) or not os.path.isfile(full):
            self.send_error(404, "File not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(open(full, "rb").read())

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

    # ── Memory API (reads MEMORY.md / USER.md) ──
    def _handle_memory(self):
        memory = {}
        for fname in ("MEMORY.md", "USER.md"):
            fpath = os.path.join(HERMES_HOME, fname)
            if os.path.isfile(fpath):
                try:
                    memory[fname] = open(fpath).read()
                except Exception:
                    memory[fname] = ""
        self._json(memory)

    def _handle_memory_write(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        filename = body.get("filename", "")
        content = body.get("content", "")
        if filename not in ("MEMORY.md", "USER.md"):
            return self._json({"error": "Invalid filename"}, 400)
        fpath = os.path.join(HERMES_HOME, filename)
        open(fpath, "w").write(content)
        self._json({"ok": True})

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

    # ── Request routing ──
    def do_GET(self):
        if self.path == "/health":
            self._handle_health()
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
        elif self.path == "/api/skills" or self.path.startswith("/api/skills?"):
            self._handle_skills()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/v1/chat/completions" or self.path == "/api/chat":
            self._handle_chat()
        elif self.path == "/api/chat/cancel":
            self._handle_cancel()
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        elif self.path == "/api/ui-conversations":
            self._conversations_save()
        elif self.path.startswith("/writefile"):
            self._write_file()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        elif self.path.startswith("/server/restart"):
            self._server_restart()
        else:
            self._json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hermes-Session-Id")
        self.end_headers()

    def _server_restart(self):
        self._json({"ok": True, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
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
