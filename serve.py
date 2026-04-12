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
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
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
        for h in ("Content-Type", "Authorization", "Accept", "X-Hermes-Session-Id"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e

        is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")

        self.send_response(resp.status)
        for h in ("Content-Type", "Cache-Control", "X-Hermes-Session-Id"):
            v = resp.headers.get(h)
            if v:
                self.send_header(h, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Hermes-Session-Id")
        self.end_headers()

        if is_sse:
            # Use readline() for unbuffered SSE streaming — resp.read(n) buffers
            # and holds data until n bytes arrive or the connection closes, which
            # causes the UI to hang until Hermes finishes its entire response.
            # No socket timeout here — Hermes may run long tool calls with no
            # SSE output for minutes. The browser-side stall monitor handles
            # user-facing timeouts; serve.py just faithfully relays.
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                # Always close the Hermes connection explicitly. Without this,
                # Python holds the socket open until GC, leaving Hermes mid-stream
                # with no reader — which can corrupt session state and cause the
                # next request to hang.
                try:
                    resp.close()
                except Exception:
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


    def _memory_status(self):
        """Return memory provider status from config.yaml and plugin directory."""
        config_path = HERMES_HOME / "config.yaml"
        result = {
            "builtin": {"active": True, "memory_enabled": True, "user_profile_enabled": True},
            "provider": None,
            "installed_providers": [],
        }

        # Read config.yaml for memory section (simple parser, no PyYAML needed)
        if config_path.exists():
            try:
                import re
                text = config_path.read_text(encoding="utf-8")
                # Find the memory: block and extract indented keys
                in_memory = False
                for line in text.splitlines():
                    if re.match(r'^memory:', line):
                        in_memory = True
                        continue
                    if in_memory:
                        if line and not line[0].isspace():
                            break  # left new top-level block
                        m = re.match(r'\s+(\w+):\s*(.+)', line)
                        if m:
                            key, val = m.group(1), m.group(2).strip()
                            # Strip quotes
                            if val.startswith("'") and val.endswith("'"): val = val[1:-1]
                            if val.startswith('"') and val.endswith('"'): val = val[1:-1]
                            if key == "memory_enabled": result["builtin"]["memory_enabled"] = val.lower() == "true"
                            elif key == "user_profile_enabled": result["builtin"]["user_profile_enabled"] = val.lower() == "true"
                            elif key == "memory_char_limit": result["builtin"]["memory_char_limit"] = int(val)
                            elif key == "user_char_limit": result["builtin"]["user_char_limit"] = int(val)
                            elif key == "provider" and val:
                                result["provider"] = {"name": val}
            except Exception:
                pass

        # Scan plugin directory for installed providers
        plugins_dir = Path.home() / ".hermes" / "hermes-agent" / "plugins" / "memory"
        if plugins_dir.exists():
            for d in sorted(plugins_dir.iterdir()):
                if d.is_dir() and not d.name.startswith("_"):
                    info = {"name": d.name, "installed": True}
                    plugin_yaml = d / "plugin.yaml"
                    if plugin_yaml.exists():
                        try:
                            import re as _re
                            ptxt = plugin_yaml.read_text(encoding="utf-8")
                            # Simple YAML key extraction
                            for pline in ptxt.splitlines():
                                pm = _re.match(r'(\w+):\s*"?([^"]*)"?', pline)
                                if pm:
                                    pk, pv = pm.group(1), pm.group(2).strip()
                                    if pk == "description": info["description"] = pv
                                    elif pk == "version": info["version"] = pv
                            # Extract hooks list
                            hooks = []
                            in_hooks = False
                            for pline in ptxt.splitlines():
                                if _re.match(r'hooks:', pline):
                                    in_hooks = True
                                    continue
                                if in_hooks:
                                    hm = _re.match(r'\s+-\s+(\S+)', pline)
                                    if hm:
                                        hooks.append(hm.group(1))
                                    elif pline.strip() and not pline[0].isspace():
                                        break
                            info["hooks"] = hooks
                        except Exception:
                            pass
                    readme = d / "README.md"
                    if readme.exists():
                        try:
                            info["readme"] = readme.read_text(encoding="utf-8")[:4000]
                        except Exception:
                            pass
                    info["active"] = (result["provider"] or {}).get("name") == d.name
                    result["installed_providers"].append(info)

        # Enrich active provider with its config (redact secrets)
        if result["provider"]:
            pname = result["provider"]["name"]
            provider_config_dir = HERMES_HOME / pname
            if provider_config_dir.exists():
                config_file = provider_config_dir / "config.json"
                if config_file.exists():
                    try:
                        pcfg = json.loads(config_file.read_text())
                        safe_cfg = {}
                        for k, v in pcfg.items():
                            if any(s in k.lower() for s in ["key", "token", "secret", "password"]):
                                safe_cfg[k] = "***" if v else ""
                            else:
                                safe_cfg[k] = v
                        result["provider"]["config"] = safe_cfg
                    except Exception:
                        pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

    def _config_fallback(self):
        """Return config data by reading config.yaml directly.
        
        The /api/config endpoint on the WebAPI returns a 500 in v0.7.0 due to
        Pydantic model restructuring.  This fallback reads config.yaml and builds
        a minimal response that the UI needs (mainly mcp_tools list).
        """
        import re
        config_path = HERMES_HOME / "config.yaml"
        result = {"mcp_tools": [], "mcp_servers": {}}
        try:
            if config_path.exists():
                text = config_path.read_text(encoding="utf-8")
                # Parse mcp_servers block
                in_mcp = False
                current_server = None
                servers = {}
                for line in text.splitlines():
                    if re.match(r'^mcp_servers:', line):
                        in_mcp = True
                        continue
                    if in_mcp:
                        if line and not line[0].isspace():
                            break  # left top-level block
                        m_server = re.match(r'^  (\S+):', line)
                        if m_server:
                            current_server = m_server.group(1)
                            servers[current_server] = {"enabled": True}
                            continue
                        if current_server:
                            m_kv = re.match(r'^\s+(\w+):\s*(.+)', line)
                            if m_kv:
                                key, val = m_kv.group(1), m_kv.group(2).strip()
                                if key == "enabled":
                                    servers[current_server]["enabled"] = val.lower() == "true"
                                elif key == "command":
                                    servers[current_server]["command"] = val
                result["mcp_servers"] = servers
                # Build mcp_tools list from server names
                for name, info in servers.items():
                    if info.get("enabled", True):
                        result["mcp_tools"].append({
                            "name": name,
                            "description": f"MCP server: {name}",
                            "status": "connected"
                        })
        except Exception as e:
            pass  # return empty result on any error
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(result).encode())

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

    def _read_local_file(self):
        """Read any local file for the artifact panel (restricted to safe text/code extensions)."""
        from urllib.parse import urlparse, parse_qs
        import os
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        file_path = params.get("path", [""])[0]

        # Expand ~ to home directory
        file_path = os.path.expanduser(file_path)
        file_path = os.path.abspath(file_path)

        # Restrict to safe file extensions
        SAFE_EXTS = {'.html', '.htm', '.svg', '.css', '.js', '.jsx', '.ts', '.tsx',
                     '.py', '.json', '.xml', '.md', '.txt', '.yaml', '.yml',
                     '.sh', '.bash', '.rs', '.go', '.java', '.c', '.cpp', '.h',
                     '.rb', '.php', '.toml', '.ini', '.cfg', '.conf', '.log', '.csv'}
        ext = os.path.splitext(file_path)[1].lower()
        if ext not in SAFE_EXTS:
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"File type '{ext}' not allowed"}).encode())
            return

        if not os.path.isfile(file_path):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "File not found"}).encode())
            return

        try:
            size = os.path.getsize(file_path)
            if size > 2_000_000:
                self.send_response(413)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": f"File too large ({size:,} bytes, max 2MB)"}).encode())
                return

            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()

            file_type = "html" if ext in {'.html', '.htm'} else "svg" if ext == '.svg' else "code"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({
                "content": content,
                "path": file_path,
                "name": os.path.basename(file_path),
                "size": size,
                "type": file_type,
                "extension": ext,
            }).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _sessions_list(self):
        """List sessions — stub for UI compatibility."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"sessions": [], "total": 0}).encode())

    def _sessions_create(self):
        """Create a new session — generates a session ID and returns it."""
        import uuid
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"session": {"id": session_id}, "id": session_id}).encode())

    def _sessions_chat(self, session_id):
        """Handle chat request — forward to /v1/chat/completions and adapt response."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except:
            body = b'{}'
            data = {}

        message = data.get("message", data.get("content", ""))

        # Forward to Hermes Agent's /v1/chat/completions
        forward_body = json.dumps({
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": message}],
            "stream": False
        }).encode()

        req = urllib.request.Request(
            HERMES + "/v1/chat/completions",
            data=forward_body,
            method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")

        try:
            resp = urllib.request.urlopen(req, timeout=120)
            resp_data = json.loads(resp.read())
            choices = resp_data.get("choices", [])
            final_response = ""
            if choices and len(choices) > 0:
                final_response = choices[0].get("message", {}).get("content", "")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"final_response": final_response}).encode())
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def _sessions_chat_stream(self, session_id):
        """Handle streaming chat request — forward to /v1/chat/completions (stream:true) and emit SSE.

        Uses http.client for proper HTTP handling and makefile() for line-buffered
        reads — each SSE line is forwarded the instant it arrives, no select() polling.
        """
        import http.client
        from urllib.parse import urlparse

        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except:
            data = {}

        message = data.get("message", data.get("content", ""))

        parsed = urlparse(HERMES)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80

        forward_body = json.dumps({
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": message}],
            "stream": True
        }).encode()

        try:
            conn = http.client.HTTPConnection(host, port, timeout=300)
            hdrs = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "X-Hermes-Session-Id": session_id,
            }
            conn.request("POST", "/v1/chat/completions", body=forward_body, headers=hdrs)
            resp = conn.getresponse()
        except Exception as e:
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Hermes connection failed: {e}"}).encode())
            return

        self.send_response(resp.status)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        # Forward session header if Hermes sent one
        sid_hdr = resp.getheader("X-Hermes-Session-Id")
        if sid_hdr:
            self.send_header("X-Hermes-Session-Id", sid_hdr)
        self.end_headers()

        # Use makefile for line-buffered reads — each line flushes immediately
        # instead of waiting for a socket buffer or select() timeout.
        sock = resp.fp  # the raw socket file object
        full_content = ""

        try:
            while True:
                line = sock.readline()
                if not line:
                    break
                line = line.rstrip(b"\r\n")
                if not line.startswith(b"data:"):
                    # Forward raw SSE lines (event:, comments, blank lines for framing)
                    self.wfile.write(line + b"\n")
                    if line == b"":
                        self.wfile.flush()
                    continue

                data_str = line[5:].strip().decode("utf-8", errors="replace")

                if data_str == "[DONE]":
                    done_data = json.dumps({"content": full_content})
                    self.wfile.write(f"event: assistant.completed\ndata: {done_data}\n\n".encode())
                    self.wfile.flush()
                    break

                try:
                    d = json.loads(data_str)
                    delta = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                    if delta:
                        full_content += delta
                        event_data = json.dumps({"delta": delta})
                        self.wfile.write(f"event: assistant.delta\ndata: {event_data}\n\n".encode())
                        self.wfile.flush()
                    # Forward tool_calls deltas if present
                    tool_delta = d.get("choices", [{}])[0].get("delta", {}).get("tool_calls")
                    if tool_delta:
                        event_data = json.dumps({"tool_calls": tool_delta})
                        self.wfile.write(f"event: assistant.tool\ndata: {event_data}\n\n".encode())
                        self.wfile.flush()
                except json.JSONDecodeError:
                    # Forward unparseable data lines raw
                    self.wfile.write(line + b"\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

        # Send completion if we didn't already
        try:
            if not full_content or data_str != "[DONE]":
                done_data = json.dumps({"content": full_content})
                self.wfile.write(f"event: assistant.completed\ndata: {done_data}\n\n".encode())
                self.wfile.flush()
        except Exception:
            pass

    def _skills_list(self):
        """List all installed skills by scanning ~/.hermes/skills/ directories.

        Scans category/skill and also category/skill/subskill (3 levels)
        to match how Hermes itself counts skills.
        """
        skills_dir = HERMES_HOME / "skills"
        skills = []

        def _read_desc(skill_md):
            """Extract first non-heading line from SKILL.md as description."""
            if not skill_md.exists():
                return None
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line[:200]
            except Exception:
                pass
            return None

        if skills_dir.exists():
            for category_dir in sorted(skills_dir.iterdir()):
                if not category_dir.is_dir() or category_dir.name.startswith("."):
                    continue
                for skill_dir in sorted(category_dir.iterdir()):
                    if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                        continue
                    # Check for sub-skills (depth 3)
                    sub_skills = [
                        s for s in skill_dir.iterdir()
                        if s.is_dir() and not s.name.startswith(".") and (s / "SKILL.md").exists()
                    ]
                    if sub_skills:
                        # Parent is a skill group — add each sub-skill
                        for sub in sorted(sub_skills):
                            skill = {
                                "name": sub.name,
                                "category": category_dir.name,
                                "group": skill_dir.name,
                                "disabled": False,
                            }
                            desc = _read_desc(sub / "SKILL.md")
                            if desc:
                                skill["description"] = desc
                            skills.append(skill)
                    # Also add the parent if it has its own SKILL.md
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.exists():
                        skill = {
                            "name": skill_dir.name,
                            "category": category_dir.name,
                            "disabled": False,
                        }
                        desc = _read_desc(skill_md)
                        if desc:
                            skill["description"] = desc
                        skills.append(skill)
                    elif not sub_skills:
                        # No SKILL.md and no sub-skills — still list it
                        skills.append({
                            "name": skill_dir.name,
                            "category": category_dir.name,
                            "disabled": False,
                        })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"skills": skills, "total": len(skills)}).encode())

    def _skill_detail(self, skill_name):
        """Return details for a single skill including its SKILL.md content."""
        skills_dir = HERMES_HOME / "skills"
        # Search for the skill in any category
        if skills_dir.exists():
            for category_dir in skills_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                skill_dir = category_dir / skill_name
                if skill_dir.exists() and skill_dir.is_dir():
                    result = {
                        "name": skill_name,
                        "category": category_dir.name,
                        "success": True,
                    }
                    skill_md = skill_dir / "SKILL.md"
                    if skill_md.exists():
                        try:
                            result["content"] = skill_md.read_text(encoding="utf-8", errors="replace")[:10000]
                        except Exception:
                            result["content"] = "(Could not read SKILL.md)"
                    # List other files in the skill directory
                    result["files"] = [f.name for f in skill_dir.iterdir() if f.is_file()]
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(json.dumps(result).encode())
                    return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"success": False, "error": "Skill not found"}).encode())

    def _skill_update(self):
        """Update a skill's SKILL.md content via PUT /api/skills/{name}."""
        from urllib.parse import unquote
        skill_name = self.path.split("/api/skills/")[1].split("?")[0]
        skill_name = unquote(skill_name)
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        try:
            data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"success": False, "error": "Invalid JSON"}).encode())
            return

        new_content = data.get("content", "")
        skills_dir = HERMES_HOME / "skills"
        if skills_dir.exists():
            for category_dir in skills_dir.iterdir():
                if not category_dir.is_dir():
                    continue
                skill_dir = category_dir / skill_name
                if skill_dir.exists() and skill_dir.is_dir():
                    skill_md = skill_dir / "SKILL.md"
                    try:
                        skill_md.write_text(new_content, encoding="utf-8")
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": True}).encode())
                    except Exception as e:
                        self.send_response(500)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Access-Control-Allow-Origin", "*")
                        self.end_headers()
                        self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode())
                    return
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"success": False, "error": "Skill not found"}).encode())

    def _memory_list(self):
        """Return memory entries from ~/.hermes/memories/ files."""
        memories_dir = HERMES_HOME / "memories"
        targets = []
        for target_name, filename in [("memory", "MEMORY.md"), ("user", "USER.md")]:
            filepath = memories_dir / filename
            entries = []
            usage = ""
            if filepath.exists():
                try:
                    text = filepath.read_text(encoding="utf-8", errors="replace").strip()
                    if text:
                        # Split on --- separators or double newlines
                        parts = [p.strip() for p in text.split("\n---\n") if p.strip()]
                        if len(parts) <= 1:
                            parts = [p.strip() for p in text.split("\n\n") if p.strip()]
                        entries = parts
                        char_count = len(text)
                        limit = 4000 if target_name == "memory" else 1375
                        usage = f"{char_count}/{limit} chars"
                except Exception:
                    pass
            targets.append({
                "target": target_name,
                "entries": entries,
                "usage": usage,
            })
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"targets": targets}).encode())

    def _memory_update(self):
        """Update memory entries — write back to ~/.hermes/memories/ files."""
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)
        try:
            data = json.loads(body)
        except Exception:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "Invalid JSON"}).encode())
            return

        memories_dir = HERMES_HOME / "memories"
        memories_dir.mkdir(parents=True, exist_ok=True)

        if "memory" in data:
            (memories_dir / "MEMORY.md").write_text(data["memory"], encoding="utf-8")
        if "user_profile" in data:
            (memories_dir / "USER.md").write_text(data["user_profile"], encoding="utf-8")

        # Return updated targets
        self._memory_list()


    # ── UI Conversation Persistence ──────────────────────────────────────────
    CONVERSATIONS_PATH = None  # set at class level below

    def _conversations_load(self):
        import os, json
        path = os.path.expanduser('~/.hermes/ui-conversations.json')
        try:
            if os.path.exists(path):
                with open(path) as f:
                    data = f.read()
            else:
                data = '[]'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(data.encode())
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def _conversations_save(self):
        import os, json
        path = os.path.expanduser('~/.hermes/ui-conversations.json')
        try:
            length = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(length)
            # Validate JSON before saving
            json.loads(body)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(body)
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'error': str(e)}).encode())

    def do_GET(self):
        if self.path.startswith("/memory/status"):
            self._memory_status()
        elif self.path == "/api/sessions":
            self._sessions_list()
        elif self.path == "/api/skills":
            self._skills_list()
        elif self.path.startswith("/api/skills/"):
            # /api/skills/{skill_name}
            skill_name = self.path.split("/api/skills/")[1].split("?")[0]
            from urllib.parse import unquote
            self._skill_detail(unquote(skill_name))
        elif self.path == "/api/memory":
            self._memory_list()
        elif self.path.startswith("/api/memory"):
            self._memory_list()
        elif self.path.startswith("/skills/dates"):
            self._skill_dates()
        elif self.path.startswith("/browse"):
            self._browse_dir()
        elif self.path.startswith("/readfile"):
            self._read_file()
        elif self.path.startswith("/api/localfile"):
            self._read_local_file()
        elif self.path.startswith("/cron/list"):
            self._cron_list()
        elif self.path.startswith("/api/config"):
            self._config_fallback()
        elif self.path == "/api/ui-conversations":
            self._conversations_load()
        elif self.path.startswith("/health"):
            self._health_with_model()
        elif self.path.startswith("/v1/") or self.path.startswith("/api/"):
            self._proxy()
        elif self.path.startswith("/logs/stream"):
            self._stream_logs()
        else:
            super().do_GET()

    def _health_with_model(self):
        """Proxy /health and inject the configured model name.

        The Hermes WebAPI /health response doesn't include the model,
        so the UI falls back to a hardcoded default. We enrich the
        response with the model from config.yaml so the status bar
        shows the correct model.
        """
        # Proxy the real health check
        url = HERMES + self.path
        try:
            resp = urllib.request.urlopen(url, timeout=5)
            data = json.loads(resp.read())
        except Exception:
            data = {"status": "ok"}

        # Inject model if missing
        if "model" not in data or isinstance(data["model"], dict):
            resolved = self._resolve_model()
            if resolved:
                data["model"] = resolved

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _cron_list(self):
        """Read cron jobs from jobs.json directly — includes prompts, scripts, and all metadata."""
        jobs_file = HERMES_HOME / "cron" / "jobs.json"
        try:
            if not jobs_file.exists():
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(json.dumps({"jobs": []}).encode())
                return

            raw = json.loads(jobs_file.read_text(encoding="utf-8"))
            raw_jobs = raw.get("jobs", [])

            # Normalize to the shape the UI expects
            jobs = []
            for j in raw_jobs:
                sched = j.get("schedule", {})
                sched_expr = sched.get("expr", "") if isinstance(sched, dict) else str(sched)
                job = {
                    "id": j.get("id", ""),
                    "name": j.get("name", ""),
                    "status": "active" if j.get("enabled") else "disabled",
                    "state": j.get("state", ""),
                    "schedule": sched_expr,
                    "prompt": j.get("prompt", ""),
                    "script": j.get("script", ""),
                    "skills": j.get("skill", "") or ", ".join(j.get("skills", [])),
                    "deliver": j.get("deliver", ""),
                    "next_run": j.get("next_run_at", ""),
                    "last_run": j.get("last_run_at", ""),
                    "last_status": j.get("last_status", ""),
                    "last_error": j.get("last_error", ""),
                    "repeat": str(j.get("repeat", {}).get("times", "")) if isinstance(j.get("repeat"), dict) else str(j.get("repeat", "")),
                }
                jobs.append(job)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"jobs": jobs}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e), "jobs": []}).encode())

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

    def _resolve_model(self):
        """Read the default model string from config.yaml."""
        import re
        config_path = HERMES_HOME / "config.yaml"
        try:
            text = config_path.read_text(encoding="utf-8")
            m = re.search(r'^model:\s*\n\s+default:\s*(\S+)', text, re.MULTILINE)
            if m:
                return m.group(1)
        except Exception:
            pass
        return None

    def _chat_inject_model(self):
        """Proxy a chat/stream POST, injecting 'model' if missing.

        v0.7.0 stores model as a dict {default, provider} in config.yaml.
        The WebAPI chat handler calls .lower() on it and crashes with
        "'dict' object has no attribute 'lower'".  Injecting the model
        as a plain string in the request body sidesteps the bug.
        """
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length) if content_length else b"{}"
        try:
            data = json.loads(raw)
        except Exception:
            data = {}

        if "model" not in data or isinstance(data["model"], dict):
            resolved = self._resolve_model()
            if resolved:
                data["model"] = resolved

        new_body = json.dumps(data).encode()

        url = HERMES + self.path
        req = urllib.request.Request(url, data=new_body, method="POST")
        req.add_header("Content-Type", "application/json")
        for h in ("Authorization", "Accept"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])

        try:
            resp = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            resp = e

        is_sse = "text/event-stream" in (resp.headers.get("Content-Type") or "")

        self.send_response(resp.status)
        for h in ("Content-Type", "Cache-Control", "X-Hermes-Session-Id"):
            v = resp.headers.get(h)
            if v:
                self.send_header(h, v)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "X-Hermes-Session-Id")
        self.end_headers()

        if is_sse:
            try:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        else:
            self.wfile.write(resp.read())

    def do_POST(self):
        if self.path == "/server/restart":
            self._server_restart()
        elif self.path == "/server/pull-restart":
            self._server_pull_restart()
        elif self.path == "/api/ui-conversations":
            self._conversations_save()
        elif self.path.startswith("/writefile"):
            self._write_file()
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        elif self.path == "/api/sessions":
            self._sessions_create()
        elif self.path.startswith("/api/sessions/") and "/chat/stream" in self.path:
            # Streaming: /api/sessions/{session_id}/chat/stream
            parts = self.path.split("/")
            session_id = parts[3] if len(parts) >= 4 else "unknown"
            self._sessions_chat_stream(session_id)
        elif self.path.startswith("/api/sessions/") and "/chat" in self.path:
            # Extract session_id from /api/sessions/{session_id}/chat
            parts = self.path.split("/")
            session_id = parts[3] if len(parts) >= 4 else "unknown"
            self._sessions_chat(session_id)
        elif self.path == "/api/sessions":
            self._sessions_create()
        elif "/chat" in self.path and self.path.startswith("/api/sessions/"):
            self._chat_inject_model()
        else:
            self._proxy()

    def do_PUT(self):
        if self.path.startswith("/api/memory"):
            self._memory_update()
        elif self.path.startswith("/api/skills/"):
            self._skill_update()
        else:
            self._proxy()

    def do_DELETE(self):
        self._proxy()

    def do_PATCH(self):
        self._proxy()

    def _server_restart(self):
        """Restart serve.py by re-executing the current process."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "restarting"}).encode())

        def _do_restart():
            time.sleep(0.5)  # let the response flush
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=_do_restart, daemon=True).start()

    def _server_pull_restart(self):
        """Git pull then restart serve.py."""
        import subprocess
        try:
            result = subprocess.run(
                ["git", "pull", "--rebase"],
                cwd=DIR,
                capture_output=True,
                text=True,
                timeout=30,
            )
            pull_output = (result.stdout + result.stderr).strip()
        except Exception as e:
            pull_output = f"git pull failed: {e}"

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "restarting", "pull": pull_output}).encode())

        def _do_restart():
            time.sleep(0.5)
            os.execv(sys.executable, [sys.executable] + sys.argv)

        threading.Thread(target=_do_restart, daemon=True).start()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Allow", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hermes-Session-Id")
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
