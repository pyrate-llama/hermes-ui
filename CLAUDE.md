# Project Instructions

## Tool Usage
Do NOT use sandbox tools (Bash, Read, Write, Edit, Grep, Glob). Use Desktop Commander MCP tools for all file and command operations — they have direct access to the real filesystem. No sandbox needed.

## Important Rules
- NEVER restart Hermes WebAPI, kill hermes processes, or kill claude-code-bridge processes without asking first
- The hermes-ui project lives in the checked-out repository directory.
- Always read context-keep memories at the start of a session for project context

## Architecture
- hermes-ui.html: Single-file React 18 + Babel standalone app
- serve_lite.py: Python proxy server on port 3333 (stdlib only) — canonical server
- serve.py: Deprecation shim that execs serve_lite.py (kept for backwards compatibility with old systemd units)
- Hermes Agent runs at localhost:8642
- GitHub: pyrate-llama/hermes-ui
