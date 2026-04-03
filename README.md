# Hermes UI

A sleek, glassmorphic web interface for [Hermes Agent](https://github.com/pyrate-llama/hermes-agent) — your self-hosted AI assistant.

Built as a single-file HTML application with React 18, Hermes UI provides a full-featured chat interface, real-time log streaming, file browsing, memory inspection, and more — all through a lightweight Python proxy server.

![Dark glassmorphic UI with ambient glow effects](https://img.shields.io/badge/theme-glassmorphism-7c6fe0?style=for-the-badge)
![Single file HTML](https://img.shields.io/badge/architecture-single_file-44d88a?style=for-the-badge)
![React 18](https://img.shields.io/badge/react-18.2-61dafb?style=for-the-badge)

### Midnight (default)
![Midnight Theme](screenshots/screenshot-midnight.png)

### Twilight
![Twilight Theme](screenshots/screenshot-twilight.png)

### Dawn
![Dawn Theme](screenshots/screenshot-dawn.png)

---

## Features

**Chat Interface**
- SSE streaming with real-time token display
- Tool call visualization with expandable results
- Message editing and re-sending
- Image paste/drop with Gemini vision analysis
- Pause, interject, and stop controls mid-stream
- Multiple personality modes (default, technical, creative, pirate, kawaii, and more)
- PDF and HTML chat export
- Markdown rendering with syntax-highlighted code blocks

**Dashboard**
- Live auto-refreshing stats (sessions, messages, tools, tokens)
- System info panel (model, provider, uptime)
- Hermes configuration overview

**Terminal**
- Tabbed interface: Live Logs, Shell, Hermes Chat, Claude Code
- Real-time gateway and error log streaming via SSE
- Shell command execution
- Hermes chat with persistent sessions
- Claude Code CLI integration with conversation continuity

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
- System status bar (connection, model, memory count, sessions)
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

# Start the proxy server
python3 serve.py

# Or specify a custom port
python3 serve.py 8080
```

Open **http://localhost:3333/hermes-ui.html** in your browser.

That's it — no `npm install`, no build step, no dependencies beyond Python's standard library.

### Configuration

The proxy server connects to Hermes at `http://127.0.0.1:8642` by default. To change this, edit the `HERMES` variable at the top of `serve.py`.

For image analysis (paste/drop images in chat), add your Gemini API key in the Settings modal within the UI.

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
┌─────────────┐    ┌──────────────┐    ┌──────────────────┐
│  Browser     │───▶│  serve.py    │───▶│  Hermes Agent    │
│  (React 18)  │    │  port 3333   │    │  port 8642       │
│              │◀───│  proxy +     │◀───│  (WebAPI)        │
│  Single HTML │    │  log stream  │    │                  │
└─────────────┘    └──────────────┘    └──────────────────┘
```

- **`hermes-ui.html`** — The entire frontend in a single file: React components, CSS, and markup. Uses Babel standalone for JSX compilation in the browser.
- **`serve.py`** — A lightweight Python proxy (stdlib only, no pip dependencies) that serves static files, proxies API calls to Hermes, streams logs via SSE, provides shell/Claude CLI execution, and enables file browsing/editing within `~/.hermes`.

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

## License

MIT — see [LICENSE](LICENSE).

---

## Credits

Built by [Pyrate Llama](https://pyrate-llama.com) with help from Claude (Anthropic).

Powered by [Hermes Agent](https://github.com/pyrate-llama/hermes-agent).
