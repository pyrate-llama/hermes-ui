# Hermes UI

**English** · [简体中文](README.zh-CN.md)

The command center for [Hermes Agent](https://github.com/pyrate-llama/hermes-agent) — chat, steer, browse files, manage skills, and monitor everything from a single glassmorphic HTML app.

Built as a single-file HTML application with React 18, Hermes UI provides a full-featured chat interface, real-time log streaming, file browsing, memory inspection, and more — all through a lightweight Python proxy server.

![v3.0](https://img.shields.io/badge/version-3.0-ef4444?style=for-the-badge)
![Single file HTML](https://img.shields.io/badge/architecture-single_file-44d88a?style=for-the-badge)
![React 18](https://img.shields.io/badge/react-18.2-61dafb?style=for-the-badge)

### Chat
![Chat with inline image generation and live terminal panel](screenshots/chat.png)
![Chat with split log panel and streaming response visibility](screenshots/chat-logs.png)

### Dashboard
![Dashboard with live stats, recent activity, and installed skills](screenshots/dashboard.png)

### Skills Browser
![Browse and manage installed Hermes skills](screenshots/skills.png)

### Cron Jobs
![Scheduled cron jobs with edit, pause, and run-now controls](screenshots/cron-jobs.png)

### MCP Tools
![Connected MCP servers grouped by category](screenshots/mcp-tools.png)

### File Browser
![Live file browser for the ~/.hermes directory with inline preview](screenshots/files.png)

### Terminal
![Built-in terminal area with Hermes and Claude Code tabs](screenshots/terminal.png)

---

## What's new in v3.0

Hermes UI 3.0 is the release where the web client catches up with the bigger Hermes Agent 0.11 world instead of feeling like a thin wrapper around chat.

**Hermes Agent 0.11 features now surfaced in the UI**
- **Provider and API-key management in Settings** — save local provider keys without hand-editing config files
- **Live model capability labels** — see at a glance whether the active model supports vision, steer, reasoning, tools, and live model discovery
- **Reasoning effort control in the composer** — `Off`, `Low`, `Med`, `High`, `XHigh`, or `Auto`
- **Steer during streaming** — send guidance into a running turn without pausing or canceling it
- **Native image path for vision-capable models** — pasted images go straight through when the current model supports vision, with Gemini still available as the fallback path for MiniMax-style setups
- **Provider and model status surfaced across the dashboard, composer, and status bar** so 0.11's expanded routing/provider surface is visible instead of hidden

**Big UX additions in this release**
- **Search across all chat sessions** — title and content search from the global search entrypoint
- **Redo / retry on older prompts without deleting newer turns** — retries append as a new branch at the bottom instead of wiping history underneath
- **Resizable layout** — left and right columns can be dragged tighter or wider
- **Token / context meter** — simple context pressure shown in the bottom status bar
- **Sidebar attention indicators** — active, unread, and recent activity are much easier to spot
- **Streaming polish** — better incremental markdown rendering, cleaner tool-output ordering, and stronger compression/session-rotation handling

**Everything good from the earlier builds stays**
- Document upload in chat
- Artifact panel for HTML, SVG, PDF, and CSV previews
- MCP Tools browser
- Base System Prompt support and `extra_system_prompt.md`
- Built-in shell / Hermes / Claude Code terminal area
- File browser, memory tools, cron jobs, skills browser, themes, and mobile layout support

---

## Features

**Chat Interface**
- SSE streaming with incremental markdown rendering
- Tool call visualization with expandable results
- Message editing and re-sending
- Retry / redo from older prompts without deleting later messages
- Session search across titles and message content
- Image paste/drop with native vision passthrough for supported models, plus Gemini fallback when needed
- Document upload in the composer (.txt, .md, .pdf, .json, .csv, .py, .js, .ts) — RTF auto-converts to plain text
- Pause, steer, and stop controls mid-stream
- Reasoning effort selector in the composer
- Command search (`Ctrl/Cmd+K`) for jumping into session search fast
- Multiple personality modes (default, technical, creative, pirate, kawaii, and more)
- Base System Prompt field in Settings — write your own persona or instructions
- PDF and HTML chat export
- Markdown rendering with syntax-highlighted code blocks

**Dashboard**
- Live auto-refreshing stats (sessions, messages, tools, tokens)
- System info panel (model, provider, uptime, capabilities)
- Hermes configuration overview

**Artifact Panel**
- Dedicated tab in the live right panel (alongside Errors, Web UI, All)
- Auto-detects HTML, SVG, PDF, and CSV output in Hermes responses and renders them live
- Auto-detects file paths Hermes saves to disk (e.g. `~/Desktop/page.html`) and loads them automatically — no need to copy-paste code
- Panel dynamically widens from 320px to 600px when Artifacts tab is active
- Sandboxed iframe rendering for HTML/SVG with full animation and JavaScript support
- Syntax-highlighted code blocks for Python, JS, CSS, and other languages
- Per-artifact Copy and close (✕) buttons
- Manual "Load File" button to open any local HTML/SVG/code file directly in the panel
- Scroll position preserved when switching between tabs

**Terminal**
- Tabbed interface for Shell, Hermes, and Claude Code flows
- Real-time log streaming views for Gateway, Errors, Web UI, and All
- Live connection indicator with line count

**File Browser**
- Browse `~/.hermes` directory tree
- View and edit config files, logs, and memory files in-place
- Image preview support

**Memory Inspector**
- View and edit Hermes internal memory (MEMORY.md, USER.md)
- Live memory usage stats

**Skills Browser**
- Search and browse all installed Hermes skills
- Sort by newest, oldest, or name — see what Hermes has been creating
- Relative timestamps on each skill (e.g. "2h ago", "3d ago")
- Edit and delete installed skills from the UI
- View skill descriptions, tags, and trigger phrases

**Jobs Monitor**
- Track active and recent Hermes sessions
- Message, tool call, and token counts per session
- Auto-refresh every 10 seconds

**MCP Tool Browser**
- Browse all connected MCP servers and their tools
- View tool descriptions and status

**UI/UX**
- Glassmorphism design with ambient animated glow
- Collapsible sidebar and right panel
- Resizable left and right columns
- System status bar with connection, model, capability pills, memory count, sessions, and context pressure
- Sidebar activity indicators for streaming, unread, and recent chats
- Inter + JetBrains Mono typography
- Keyboard shortcuts
- Theme switcher (Midnight, Twilight, Dawn)
- Responsive layout for tablets and mobile phones
- Bottom navigation bar on small screens with quick access to key views
- Touch-optimized targets and safe-area inset support for notched devices

---

## Quick Start

### Prerequisites

- Python 3.8+
- A running [Hermes Agent](https://github.com/pyrate-llama/hermes-agent) instance on `localhost:8642`
- (Optional) [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) for the Claude terminal tab

### Install & Run

```bash
# Clone the repo
git clone https://github.com/pyrate-llama/hermes-ui.git
cd hermes-ui

# Start the proxy server with the Hermes venv interpreter
~/.hermes/hermes-agent/venv/bin/python3 serve_lite.py

# Or specify a custom port
~/.hermes/hermes-agent/venv/bin/python3 serve_lite.py --port 8080
```

> **Note:** `serve.py` still exists as a backwards-compatibility shim that prints a deprecation notice and execs `serve_lite.py`. Existing systemd units and launchers that reference `serve.py` will keep working, but new setups should invoke `serve_lite.py` directly.
>
> If you accidentally start `serve_lite.py` with the wrong Python, it will try to re-launch itself with the Hermes venv interpreter automatically.

Open **http://localhost:3333/hermes-ui.html** in your browser.

That's it — no `npm install`, no build step, no dependencies beyond Python's standard library.

### Configuration

The proxy server connects to Hermes at `http://127.0.0.1:8642` by default. To change this, edit the `HERMES` variable at the top of `serve_lite.py`.

Provider keys can be managed directly in **Settings**, including local API-key storage for supported providers. For image analysis fallback on non-vision setups, add your Gemini API key there as well.

### Using OpenRouter or Custom Inference Endpoints

Hermes supports any OpenAI-compatible API endpoint, which means you can use [OpenRouter](https://openrouter.ai) to access Claude, GPT-4, Llama, Mistral, and dozens of other models through a single API key.

In your `~/.hermes/config.yaml`, set your inference endpoint and API key:

```yaml
inference:
  base_url: https://openrouter.ai/api/v1
  api_key: sk-or-v1-your-openrouter-key
  model: anthropic/claude-sonnet-4-20250514
```

This also works with other compatible providers like [LiteLLM](https://github.com/BerriAI/litellm) (self-hosted proxy), [Ollama](https://ollama.ai) (`http://localhost:11434/v1`), or any endpoint that speaks the OpenAI chat completions format.

---

## Remote Access (Tailscale)

Access Hermes UI from your phone, tablet, or any device using [Tailscale](https://tailscale.com) — a zero-config mesh VPN built on WireGuard. No ports exposed to the internet, no DNS to configure, all traffic encrypted end-to-end.

1. **Install Tailscale on your server** (the machine running Hermes):
   ```bash
   brew install tailscale    # macOS
   # or: curl -fsSL https://tailscale.com/install.sh | sh   # Linux
   tailscale up
   ```

2. **Install Tailscale on your phone/other devices** — download the app (iOS/Android) and sign in with the same account.

3. **Connect** — find your server's Tailscale IP (`tailscale ip`) and open:
   ```
   http://100.x.x.x:3333/hermes-ui.html
   ```

4. **Optional: HTTPS via Tailscale Serve** — get a real certificate and clean URL:
   ```bash
   tailscale serve --bg 3333
   # Accessible at https://your-machine.tail1234.ts.net
   ```

A built-in setup guide is also available in the app under **Settings > Remote Access**.

---

## Architecture

```
┌─────────────┐    ┌────────────────┐    ┌──────────────────┐
│  Browser     │───▶│  serve_lite.py │───▶│  Hermes Agent    │
│  (React 18)  │    │  port 3333     │    │  port 8642       │
│              │◀───│  proxy +       │◀───│  (WebAPI)        │
│  Single HTML │    │  log stream    │    │                  │
└─────────────┘    └────────────────┘    └──────────────────┘
```

- **`hermes-ui.html`** — The entire frontend in a single file: React components, CSS, and markup. Uses Babel standalone for JSX compilation in the browser.
- **`serve_lite.py`** — A lightweight Python proxy (stdlib only, no pip dependencies) that serves static files, proxies the `/api/chat/*` two-step SSE flow to the Hermes agent, streams logs via SSE, provides shell/Claude CLI execution, and enables file browsing/editing within `~/.hermes`. This is the canonical server.
- **`serve.py`** — Backwards-compatibility shim. Prints a deprecation notice and execs `serve_lite.py`. Kept so existing systemd units and launchers don't break.

### CDN Dependencies

All loaded from cdnjs.cloudflare.com at runtime:

| Library | Version | Purpose |
|---------|---------|---------|
| React | 18.2.0 | UI framework |
| React DOM | 18.2.0 | DOM rendering |
| Babel Standalone | 7.23.9 | JSX compilation |
| marked | 11.1.1 | Markdown parsing |
| highlight.js | 11.9.0 | Code syntax highlighting |
| Inter | — | UI typography (Google Fonts) |
| JetBrains Mono | — | Code/terminal typography (Google Fonts) |

---

## Keyboard Shortcuts

| Shortcut | Action |
|----------|--------|
| `Enter` | Send message |
| `Shift+Enter` | New line in input |
| `?` | Show keyboard shortcuts |
| `Ctrl/Cmd+K` | Focus search |
| `Ctrl/Cmd+N` | New chat |
| `Ctrl/Cmd+\` | Toggle sidebar |
| `Ctrl/Cmd+E` | Export chat as markdown |
| `Escape` | Close modals / dismiss |

---

## Themes

Hermes UI ships with three built-in themes, accessible via the theme switcher in the header:

- **Midnight** (default) — Deep indigo/purple glassmorphism with ambient purple and green glow
- **Twilight** — Warm amber/gold tones with copper accents
- **Dawn** — Soft light theme with blue-gray tones for daytime use

---

## Troubleshooting

**Hermes stops responding / hangs after a few messages**

If Hermes responds once or twice then goes silent, check your `~/.hermes/config.yaml` for this bug in the context compression config:

```yaml
compression:
  summary_base_url: null   # ← this causes a 404 and hangs the agent
```

Fix it by setting `summary_base_url` to match your inference provider's base URL. For MiniMax:

```yaml
compression:
  summary_base_url: https://api.minimax.io/anthropic
```

Then restart Hermes: `hermes restart`

---

**Chat hangs, times out silently, or returns 404 on `/api/chat/start`**

Two common causes:

1. **You're running the old `serve.py` directly from a stale checkout or a systemd unit.** The current client (`hermes-ui.html`) talks to the two-step `/api/chat/*` SSE API, which only `serve_lite.py` implements. If your launcher calls `python3 serve.py`, pull the repo — the new `serve.py` is a shim that forwards to `serve_lite.py` and will keep working. If you're on an older checkout, update your unit to call `serve_lite.py` directly:

   ```
   ExecStart=/usr/bin/python3 /path/to/hermes-ui/serve_lite.py
   ```

2. **The Hermes agent itself (port 8642) isn't reachable.** `serve_lite.py` on 3333 is only a proxy — it needs the agent running on 8642. Check `curl http://127.0.0.1:8642/health`.

If you still see silent hangs, open the browser console — the client now surfaces SSE errors as visible chat messages rather than stalling.

---

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

Built by [Pyrate Llama](https://pyrate-llama.com) with help from Claude (Anthropic).

Powered by [Hermes Agent](https://github.com/pyrate-llama/hermes-agent).
