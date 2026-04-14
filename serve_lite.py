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

HERMES_HOME = os.path.expanduser("~/.hermes")
AGENT_DIR = os.path.join(HERMES_HOME, "hermes-agent")
DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3333

if AGENT_DIR not in sys.path:
    sys.path.insert(0, AGENT_DIR)

# Also add the agent's venv site-packages so all dependencies (hindsight, etc.) are available
import glob
_venv_site = glob.glob(os.path.join(AGENT_DIR, "venv", "lib", "python*", "site-packages"))
for _sp in _venv_site:
    if _sp not in sys.path:
        sys.path.insert(1, _sp)

_AIAgent = None

def _get_ai_agent():
    global _AIAgent
    if _AIAgent is None:
        try:
            from run_agent import AIAgent
            _AIAgent = AIAgent
        except ImportError as e:
            print(f"[serve] WARNING: Cannot import AIAgent: {e}", flush=True)
    return _AIAgent


def _resolve_model_and_credentials():
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


STREAMS = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS = {}
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()


def _get_or_create_session(session_id):
    with SESSIONS_LOCK:
        if session_id not in SESSIONS:
            SESSIONS[session_id] = {"messages": [], "model": None}
        return SESSIONS[session_id]


def _run_agent_streaming(session_id, messages, stream_id):
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

    try:
        AgentClass = _get_ai_agent()
        if AgentClass is None:
            put("error", {"message": "AIAgent not available"})
            return

        model, provider, base_url, api_key = _resolve_model_and_credentials()

        _session_db = None
        try:
            from hermes_state import SessionDB
            _session_db = SessionDB()
        except Exception:
            pass

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

        def on_token(text):
            nonlocal full_text
            if text is None:
                return
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
                put("tool_complete", {"name": name, "preview": preview, "args": args_snap, "duration": kwargs.get("duration")})

        agent = AgentClass(
            model=model, provider=provider, base_url=base_url, api_key=api_key,
            platform="cli", quiet_mode=True, enabled_toolsets=toolsets,
            session_id=session_id, session_db=_session_db,
            stream_delta_callback=on_token, reasoning_callback=on_reasoning,
            tool_progress_callback=on_tool,
        )

        user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                user_msg = m.get("content", "")
                break

        history = []
        for m in messages:
            if m.get("role") in ("user", "assistant") and m.get("content"):
                history.append({"role": m["role"], "content": m["content"]})
        if history and history[-1]["role"] == "user":
            history.pop()

        safe_keys = {"role", "content", "tool_calls", "tool_call_id", "name"}
        clean_history = [{k: v for k, v in m.items() if k in safe_keys} for m in history]

        result = agent.run_conversation(
            user_message=user_msg, conversation_history=clean_history, task_id=session_id,
        )

        session = _get_or_create_session(session_id)
        session["messages"] = result.get("messages", session["messages"])

        input_tokens = getattr(agent, "session_prompt_tokens", 0) or 0
        output_tokens = getattr(agent, "session_completion_tokens", 0) or 0
        estimated_cost = getattr(agent, "session_estimated_cost_usd", None)

        put("done", {
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "estimated_cost": estimated_cost},
            "full_text": full_text,
        })

    except Exception as e:
        print(f"[serve] stream error:\n{traceback.format_exc()}", flush=True)
        put("error", {"message": str(e)})
    finally:
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)


class HermesDirectServer(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_chat(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        stream = body.get("stream", True)
        session_id = self.headers.get("X-Hermes-Session-Id") or f"web_{uuid.uuid4().hex[:12]}"
        if not messages:
            return self._json({"error": "No messages provided"}, 400)
        if not stream:
            return self._handle_chat_sync(messages, session_id)

        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q
        thr = threading.Thread(target=_run_agent_streaming, args=(session_id, messages, stream_id), daemon=True)
        thr.start()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Hermes-Session-Id")
        self.send_header("X-Hermes-Session-Id", session_id)
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
                    chunk = {"choices": [{"delta": {"content": data["text"]}, "index": 0}]}
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                elif event == "reasoning":
                    chunk = {"choices": [{"delta": {"reasoning": data["text"]}, "index": 0}]}
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
                        done_chunk = {"choices": [{"delta": {}, "index": 0, "finish_reason": "stop"}], "usage": usage}
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
        try:
            AgentClass = _get_ai_agent()
            if AgentClass is None:
                return self._json({"error": "AIAgent not available"}, 500)
            model, provider, base_url, api_key = _resolve_model_and_credentials()
            agent = AgentClass(model=model, provider=provider, base_url=base_url, api_key=api_key, platform="cli", quiet_mode=True, session_id=session_id)
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
            return self._json({"choices": [{"message": {"role": "assistant", "content": assistant_text}, "index": 0}]})
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def _handle_health(self):
        agent_ok = _get_ai_agent() is not None
        model, provider, _, _ = _resolve_model_and_credentials() if agent_ok else ("?", "?", None, None)
        self._json({"status": "ok" if agent_ok else "degraded", "agent": agent_ok, "model": model, "provider": provider, "uptime": int(time.time() - _START_TIME)})

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

    def _terminal_exec(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        cmd = body.get("command", "")
        if not cmd:
            self._json({"error": "No command"}, 400)
            return
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120, cwd=os.path.expanduser("~"))
            self._json({"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode})
        except subprocess.TimeoutExpired:
            self._json({"error": "Command timed out (120s)"}, 504)
        except Exception as e:
            self._json({"error": str(e)}, 500)

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
                entries.append({"name": name, "type": "dir" if os.path.isdir(fp) else "file", "size": os.path.getsize(fp) if os.path.isfile(fp) else 0})
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
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        elif self.path == "/api/ui-conversations":
            self._conversations_save()
        elif self.path.startswith("/writefile"):
            self._write_file()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        elif self.path.startswith("/server/pull-restart"):
            self._server_pull_restart()
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

    def _server_pull_restart(self):
        """Git pull hermes-ui repo, then restart."""
        pull_output = ""
        try:
            result = subprocess.run(
                ["git", "pull"], capture_output=True, text=True, timeout=30, cwd=DIR
            )
            pull_output = (result.stdout + result.stderr).strip()
        except Exception as e:
            pull_output = str(e)
        self._json({"ok": True, "pull": pull_output, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()

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

    agent_ok = _get_ai_agent() is not None
    model_name = "?"
    provider_name = "?"
    if agent_ok:
        try:
            model_name, provider_name, _, _ = _resolve_model_and_credentials()
        except Exception:
            pass

    print(f"\u256d\u2500 Hermes UI Direct \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256e")
    print(f"\u2502  UI:       http://127.0.0.1:{args.port:<5}    \u2502")
    print(f"\u2502  Agent:    {chr(10003) + ' loaded' if agent_ok else chr(10007) + ' not found':25s} \u2502")
    print(f"\u2502  Model:    {str(model_name)[:25]:25s} \u2502")
    print(f"\u2502  Provider: {str(provider_name)[:25]:25s} \u2502")
    print(f"\u2502  Mode:     Direct (no gateway)       \u2502")
    print(f"\u2570\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u256f")

    if not agent_ok:
        print(f"\n[!!] WARNING: Could not import AIAgent from {AGENT_DIR}")
        print(f"     Chat will not work. Check that hermes-agent is installed.")

    server = ThreadedServer(("0.0.0.0", args.port), HermesDirectServer)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
