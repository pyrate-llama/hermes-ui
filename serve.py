#!/usr/bin/env python3
"""
Hermes UI Proxy Server
======================

Lightweight proxy that sits between the browser and Hermes Agent WebAPI.
No external dependencies — uses only Python's standard library.

Features:
  - Static file serving (hermes-ui.html)
  - API proxy to Hermes Agent (localhost:8642)
  - SSE log streaming (gateway + error logs)
  - File browsing / editing within ~/.hermes
  - Shell command execution
  - Claude Code CLI integration with conversation continuity

Usage:
  python3 serve.py          # Start on default port 3333
  python3 serve.py 8080     # Start on custom port

Configuration:
  HERMES  — Hermes Agent WebAPI URL (default: http://127.0.0.1:8642)
  PORT    — Server port (default: 3333, or first CLI argument)

Routes:
  GET  /hermes-ui.html    — Main UI
  GET  /api/*             — Proxy to Hermes API
  GET  /v1/*              — Proxy to Hermes v1 API
  GET  /health            — Proxy to Hermes health check
  GET  /logs/stream       — SSE log tail (?logs=gateway,errors&tail=80)
  GET  /browse            — List directory contents in ~/.hermes
  GET  /readfile          — Read file content from ~/.hermes
  POST /writefile         — Write file content to ~/.hermes
  GET  /skills/dates      — Skill modification timestamps from ~/.hermes/skills
  POST /terminal/exec     — Execute shell or Claude Code commands

Security:
  This server is designed for LOCAL development use only (localhost).
  The /terminal/exec endpoint allows arbitrary shell command execution.
  The /browse, /readfile, /writefile endpoints are sandboxed to ~/.hermes.
  DO NOT expose this server to the public internet.
"""
import http.server
import urllib.request
import urllib.error
import sys
import os
import time
import json
import threading
from pathlib import Path

HERMES = "http://127.0.0.1:8642"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3333
DIR = os.path.dirname(os.path.abspath(__file__))

# Log files to watch
HERMES_HOME = Path.home() / ".hermes"
LOG_FILES = {
    "gateway": HERMES_HOME / "logs" / "gateway.log",
    "errors": HERMES_HOME / "logs" / "errors.log",
}


class HermesProxy(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=DIR, **kw)

    def _proxy(self):
        url = HERMES + self.path
        body = None
        if self.headers.get("Content-Length"):
            body = self.rfile.read(int(self.headers["Content-Length"]))

        req = urllib.request.Request(url, data=body, method=self.command)
        for h in ("Content-Type", "Authorization", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e

        is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")

        self.send_response(resp.status)
        for h in ("Content-Type", "Cache-Control"):
            v = resp.headers.get(h)
            if v:
                self.send_header(h, v)
        self.end_headers()

        if is_sse:
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.wfile.write(resp.read())

    def _stream_logs(self):
        """SSE endpoint that tails log files in real-time."""
        # Parse query params: ?logs=gateway,errors&tail=50
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        requested = params.get("logs", ["gateway"])[0].split(",")
        tail_lines = int(params.get("tail", ["80"])[0])

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        # Collect log files to watch
        files_to_watch = {}
        for name in requested:
            path = LOG_FILES.get(name.strip())
            if path and path.exists():
                files_to_watch[name.strip()] = path

        if not files_to_watch:
            data = json.dumps({"log": "system", "line": "No log files found"})
            self.wfile.write(f"data: {data}\n\n".encode())
            self.wfile.flush()
            return

        # Send initial tail of each file
        for name, path in files_to_watch.items():
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                    for line in lines[-tail_lines:]:
                        line = line.rstrip("\n")
                        if line:
                            data = json.dumps({"log": name, "line": line})
                            self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()
            except Exception as e:
                data = json.dumps({"log": "system", "line": f"Error reading {name}: {e}"})
                self.wfile.write(f"data: {data}\n\n".encode())
                self.wfile.flush()

        # Send a separator
        data = json.dumps({"log": "system", "line": "--- live tail started ---"})
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

        # Now tail all files
        file_positions = {}
        for name, path in files_to_watch.items():
            try:
                f = open(path, "r", encoding="utf-8", errors="replace")
                f.seek(0, 2)  # Seek to end
                file_positions[name] = f
            except Exception:
                pass

        try:
            while True:
                had_data = False
                for name, f in file_positions.items():
                    line = f.readline()
                    while line:
                        had_data = True
                        line = line.rstrip("\n")
                        if line:
                            data = json.dumps({"log": name, "line": line})
                            self.wfile.write(f"data: {data}\n\n".encode())
                        line = f.readline()

                if had_data:
                    self.wfile.flush()
                else:
                    # Send keepalive comment every 15 seconds of inactivity
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    time.sleep(0.5)

                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            for f in file_positions.values():
                try:
                    f.close()
                except Exception:
                    pass

    def _skill_dates(self):
        """Return modification timestamps for all skills in ~/.hermes/skills."""
        skills_dir = HERMES_HOME / "skills"
        dates = {}
        if skills_dir.exists():
            for category_dir in skills_dir.iterdir():
                if category_dir.is_dir() and not category_dir.name.startswith("."):
                    for skill_dir in category_dir.iterdir():
                        if skill_dir.is_dir() and not skill_dir.name.startswith("."):
                            try:
                                # Use the most recent mtime of the skill folder or its SKILL.md
                                skill_md = skill_dir / "SKILL.md"
                                if skill_md.exists():
                                    mtime = skill_md.stat().st_mtime
                                else:
                                    mtime = skill_dir.stat().st_mtime
                                dates[skill_dir.name] = mtime
                            except Exception:
                                pass
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"dates": dates}).encode())

    def _browse_dir(self):
        """List directory contents for the Files view."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        rel_path = params.get("path", [""])[0]

        # Only allow browsing within ~/.hermes
        base = HERMES_HOME
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Access denied"}).encode())
            return

        if not target.exists():
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
            return

        items = []
        try:
            for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
                if entry.name.startswith("__pycache__") or entry.name.endswith(".pyc"):
                    continue
                info = {
                    "name": entry.name,
                    "type": "dir" if entry.is_dir() else "file",
                    "path": str(entry.relative_to(base)),
                }
                if entry.is_file():
                    try:
                        info["size"] = entry.stat().st_size
                    except:
                        info["size"] = 0
                items.append(info)
        except PermissionError:
            pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"items": items, "path": rel_path}).encode())

    def _read_file(self):
        """Read a file's content for the Files view."""
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        rel_path = params.get("path", [""])[0]

        base = HERMES_HOME
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Access denied"}).encode())
            return

        if not target.is_file():
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Not found"}).encode())
            return

        try:
            import base64, mimetypes
            size = target.stat().st_size
            suffix = target.suffix.lower()
            image_exts = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico'}
            is_image = suffix in image_exts

            if is_image:
                if size > 5_000_000:
                    content = f"(Image too large: {size:,} bytes)"
                    file_type = "text"
                else:
                    mime = mimetypes.guess_type(str(target))[0] or "image/png"
                    raw = target.read_bytes()
                    content = f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")
                    file_type = "image"
            elif size > 500_000:
                content = f"(File too large: {size:,} bytes)"
                file_type = "text"
            else:
                try:
                    content = target.read_text(encoding="utf-8", errors="replace")
                    file_type = "text"
                except:
                    content = "(Binary file — cannot display)"
                    file_type = "binary"
        except Exception as e:
            content = f"(Error reading file: {e})"
            file_type = "text"

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "content": content,
            "path": rel_path,
            "name": target.name,
            "size": target.stat().st_size if target.exists() else 0,
            "type": file_type,
        }).encode())

    def do_GET(self):
        if self.path.startswith("/skills/dates"):
            self._skill_dates()
        elif self.path.startswith("/browse"):
            self._browse_dir()
        elif self.path.startswith("/readfile"):
            self._read_file()
        elif self.path.startswith("/v1/") or self.path.startswith("/health") or self.path.startswith("/api/"):
            self._proxy()
        elif self.path.startswith("/logs/stream"):
            self._stream_logs()
        else:
            super().do_GET()

    def _write_file(self):
        """Write content to a file for the Files view editor."""
        from urllib.parse import urlparse, parse_qs
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        rel_path = data.get("path", "")
        content = data.get("content", "")

        base = HERMES_HOME
        target = (base / rel_path).resolve()
        if not str(target).startswith(str(base)):
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Access denied"}).encode())
            return

        try:
            target.write_text(content, encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True, "path": rel_path}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _terminal_exec(self):
        """Execute a shell command and return output."""
        import subprocess, shlex
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        command = data.get("command", "").strip()
        cwd = data.get("cwd") or str(Path.home())
        mode = data.get("mode", "shell")  # "shell" or "claude"
        continue_session = data.get("continue_session", False)
        # Validate cwd exists, fall back to home
        if not Path(cwd).is_dir():
            cwd = str(Path.home())

        if not command:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "No command provided"}).encode())
            return

        try:
            if mode == "claude":
                # Run command through Claude Code CLI
                import os as _os
                env = dict(_os.environ)
                env["NO_COLOR"] = "1"
                claude_cmd = ["claude"]
                if continue_session:
                    claude_cmd.append("--continue")
                claude_cmd.extend(["-p", command])
                proc = subprocess.run(
                    claude_cmd,
                    capture_output=True, text=True, timeout=120,
                    cwd=cwd, env=env
                )
            else:
                # Run as shell command
                proc = subprocess.run(
                    command, shell=True,
                    capture_output=True, text=True, timeout=60,
                    cwd=cwd
                )

            output = proc.stdout
            if proc.stderr:
                output = output + ("\n" if output else "") + proc.stderr

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "output": output,
                "exit_code": proc.returncode,
                "cwd": cwd,
            }).encode())
        except subprocess.TimeoutExpired:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "output": "(Command timed out)",
                "exit_code": -1,
                "cwd": cwd,
            }).encode())
        except FileNotFoundError as e:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            msg = str(e)
            if mode == "claude":
                msg = "Claude Code CLI not found. Make sure 'claude' is installed and in your PATH.\nInstall: npm install -g @anthropic-ai/claude-code"
            self.wfile.write(json.dumps({
                "output": msg,
                "exit_code": -1,
                "cwd": cwd,
            }).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def do_POST(self):
        if self.path.startswith("/writefile"):
            self._write_file()
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        else:
            self._proxy()

    def do_PUT(self):
        self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass  # quiet


# Use ThreadingHTTPServer so log streaming doesn't block other requests
class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


print(f"Hermes UI  → http://localhost:{PORT}/hermes-ui.html")
print(f"Proxying   → {HERMES}")
print(f"Log stream → http://localhost:{PORT}/logs/stream?logs=gateway,errors&tail=80")
ThreadedServer(("", PORT), HermesProxy).serve_forever()
