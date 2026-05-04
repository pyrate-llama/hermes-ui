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
import base64
import hashlib
import hmac
import importlib.util
import json
import os
import signal
import shlex
import shutil
import sys
import queue
import threading
import subprocess
import time
import pathlib
import secrets
import tempfile
import uuid
import traceback
import urllib.parse

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent

# ── Python interpreter sanity check ─────────────────────────────────────────
# serve_lite.py depends on hermes-agent's compiled C extensions (pydantic_core
# and friends) which are built against a specific Python minor version. Running
# with the wrong interpreter silently fails the AIAgent import and breaks chat
# with a 404 on /api/chat/stream ("Stream ended without a completion event").
# Detect the mismatch early and print a clear fix.
def _check_interpreter_matches_venv():
    import glob, re
    agent_dir = os.path.expanduser("~/.hermes/hermes-agent")
    venv_sitepkgs = glob.glob(os.path.join(agent_dir, "venv", "lib", "python*", "site-packages"))
    if not venv_sitepkgs:
        return  # No venv installed — fresh checkout; let normal Python rules apply.
    vm = re.search(r"python(\d+\.\d+)", venv_sitepkgs[0])
    if not vm:
        return
    venv_ver = vm.group(1)
    cur_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if cur_ver != venv_ver:
        venv_py = os.path.join(agent_dir, "venv", "bin", "python3")
        reexec_flag = "HERMES_UI_REEXECED_MATCHING_PYTHON"
        if os.path.exists(venv_py) and not os.environ.get(reexec_flag):
            print(
                f"[serve] Python {cur_ver} does not match hermes-agent venv Python {venv_ver}. "
                f"Re-launching with {venv_py}...",
                file=sys.stderr,
                flush=True,
            )
            env = os.environ.copy()
            env[reexec_flag] = "1"
            os.execve(venv_py, [venv_py] + sys.argv, env)
        bar = "=" * 72
        print(
            f"\n{bar}\n"
            f"  ERROR: serve_lite.py was started with Python {cur_ver}\n"
            f"  ({sys.executable})\n"
            f"  but hermes-agent's venv was built for Python {venv_ver}.\n"
            f"\n"
            f"  Compiled C extensions (pydantic_core, etc.) are ABI-specific\n"
            f"  and will fail to import under the wrong Python. Chat will break\n"
            f"  silently with 'Stream ended without a completion event'.\n"
            f"\n"
            f"  Fix: run serve_lite.py with the matching interpreter:\n"
            f"    {venv_py} serve_lite.py\n"
            f"{bar}\n",
            file=sys.stderr,
            flush=True,
        )
        sys.exit(1)

_check_interpreter_matches_venv()

# ── Hermes agent discovery ──────────────────────────────────────────────────
HERMES_HOME = os.path.expanduser("~/.hermes")
AGENT_DIR = os.path.join(HERMES_HOME, "hermes-agent")
DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3333
WORKSPACES_FILE = pathlib.Path(HERMES_HOME) / "ui-workspaces.json"
LAST_WORKSPACE_FILE = pathlib.Path(HERMES_HOME) / "ui-last-workspace.txt"
AUTH_PASSWORD = os.environ.get("HERMES_UI_PASSWORD") or os.environ.get("HERMES_WEBUI_PASSWORD")
AUTH_COOKIE_NAME = "hermes_ui_auth"
AUTH_SECRET_FILE = pathlib.Path(HERMES_HOME) / "ui-auth-secret"

def _workspace_label(path):
    p = pathlib.Path(path)
    name = p.name or str(p)
    return "Home" if str(p.resolve()) == str(PROJECT_ROOT.resolve()) else name

def _workspace_picker_roots():
    home = pathlib.Path.home().resolve()
    defaults = [
        {"name": "Home", "path": str(home)},
        {"name": "Desktop", "path": str((home / "Desktop").resolve())},
        {"name": "Documents", "path": str((home / "Documents").resolve())},
        {"name": "Downloads", "path": str((home / "Downloads").resolve())},
        {"name": "Hermes UI", "path": str(PROJECT_ROOT.resolve())},
    ]
    for env_name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        one_drive = os.environ.get(env_name)
        if one_drive:
            base = pathlib.Path(one_drive)
            defaults.extend([
                {"name": "OneDrive", "path": str(base.resolve())},
                {"name": "OneDrive Desktop", "path": str((base / "Desktop").resolve())},
                {"name": "OneDrive Documents", "path": str((base / "Documents").resolve())},
            ])
    if os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = pathlib.Path(f"{letter}:\\")
            if drive.exists():
                defaults.append({"name": f"{letter}:", "path": str(drive.resolve())})
    else:
        defaults.append({"name": "Computer", "path": str(pathlib.Path("/").resolve())})
    roots = []
    seen = set()
    for item in defaults:
        path = pathlib.Path(item["path"])
        if not path.exists() or not path.is_dir():
            continue
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append({"name": item["name"], "path": resolved})
    return roots

def _path_is_within_any(path, roots):
    for root in roots:
        try:
            if os.path.commonpath([root, path]) == root:
                return True
        except ValueError:
            continue
    return False

def _resolve_workspace_picker_path(path=None):
    roots = _workspace_picker_roots()
    fallback = roots[0]["path"] if roots else str(pathlib.Path.home().resolve())
    raw = str(path or "").strip()
    resolved = pathlib.Path(os.path.expanduser(raw or fallback)).resolve()
    resolved_str = str(resolved)
    allowed = [root["path"] for root in roots] or [fallback]
    if not _path_is_within_any(resolved_str, allowed):
        raise ValueError(f"Folder picker access denied: {resolved_str}")
    if not resolved.is_dir():
        raise ValueError(f"Folder does not exist: {raw or resolved_str}")
    return resolved_str

def _resolve_workspace(path=None):
    raw = str(path or "").strip()
    if not raw:
        raw = _get_last_workspace()
    p = pathlib.Path(os.path.expanduser(raw)).resolve()
    if not p.is_dir():
        raise ValueError(f"Workspace does not exist: {raw}")
    return str(p)

def _workspace_target(workspace, rel_path, require_existing_parent=True):
    base = _resolve_workspace(workspace)
    raw = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    if not raw or raw in (".", "..") or raw.startswith("../"):
        raise ValueError("A file or folder path is required")
    full = os.path.normpath(os.path.join(base, raw))
    if os.path.commonpath([base, full]) != base:
        raise PermissionError("Access denied")
    if require_existing_parent and not os.path.isdir(os.path.dirname(full)):
        raise FileNotFoundError("Parent folder does not exist")
    return base, raw, full

def _auth_enabled():
    return bool(AUTH_PASSWORD)

def _auth_secret():
    AUTH_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if AUTH_SECRET_FILE.exists():
        return AUTH_SECRET_FILE.read_bytes().strip()
    secret = secrets.token_bytes(32)
    AUTH_SECRET_FILE.write_bytes(base64.urlsafe_b64encode(secret))
    try:
        os.chmod(AUTH_SECRET_FILE, 0o600)
    except Exception:
        pass
    return AUTH_SECRET_FILE.read_bytes().strip()

def _auth_sign(value):
    return hmac.new(_auth_secret(), value.encode("utf-8"), hashlib.sha256).hexdigest()

def _auth_make_token():
    value = secrets.token_urlsafe(24)
    return value + "." + _auth_sign(value)

def _auth_verify_token(token):
    try:
        value, signature = str(token or "").split(".", 1)
    except ValueError:
        return False
    if not value or not signature:
        return False
    return hmac.compare_digest(signature, _auth_sign(value))

def _default_workspaces():
    root = str(PROJECT_ROOT.resolve())
    return [{"name": "Hermes UI", "path": root}]

def _load_workspaces():
    items = []
    if WORKSPACES_FILE.exists():
        try:
            raw = json.loads(WORKSPACES_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                items = raw
        except Exception:
            items = []
    cleaned = []
    seen = set()
    for item in items + _default_workspaces():
        if not isinstance(item, dict):
            continue
        path = item.get("path")
        if not path:
            continue
        try:
            resolved = _resolve_workspace(path)
        except Exception:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        cleaned.append({
            "name": str(item.get("name") or _workspace_label(resolved)),
            "path": resolved,
        })
    return cleaned or _default_workspaces()

def _save_workspaces(workspaces):
    WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
    WORKSPACES_FILE.write_text(json.dumps(workspaces, indent=2), encoding="utf-8")

def _get_last_workspace():
    if LAST_WORKSPACE_FILE.exists():
        try:
            saved = LAST_WORKSPACE_FILE.read_text(encoding="utf-8").strip()
            if saved and pathlib.Path(os.path.expanduser(saved)).is_dir():
                return str(pathlib.Path(os.path.expanduser(saved)).resolve())
        except Exception:
            pass
    return str(PROJECT_ROOT.resolve())

def _set_last_workspace(path):
    resolved = _resolve_workspace(path)
    LAST_WORKSPACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_WORKSPACE_FILE.write_text(resolved, encoding="utf-8")
    return resolved

# Current hermes-ui release version. Bump on every tagged release so the
# /api/version endpoint can tell the UI when a newer release is available on
# GitHub. Keep in sync with the git tag (e.g. "3.1" corresponds to v3.1).
__version__ = "3.1"
_GITHUB_RELEASES_API = "https://api.github.com/repos/pyrate-llama/hermes-ui/releases/latest"

# Cache for the latest-release lookup so we don't hammer GitHub. Stores
# (timestamp, payload_dict). TTL of 1 hour is plenty for an update-nag.
_latest_release_cache = {"ts": 0.0, "data": None}

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

_PROVIDER_ENV_VARS = {
    "openrouter": "OPENROUTER_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "google": "GOOGLE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "x-ai": "XAI_API_KEY",
    "mistralai": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "zai": "GLM_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
    "opencode-zen": "OPENCODE_ZEN_API_KEY",
    "opencode-go": "OPENCODE_GO_API_KEY",
    "ollama": "OLLAMA_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
}

_PROVIDER_DISPLAY = {
    "openrouter": "OpenRouter",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "google": "Google AI",
    "gemini": "Gemini",
    "deepseek": "DeepSeek",
    "x-ai": "xAI",
    "mistralai": "Mistral",
    "minimax": "MiniMax",
    "zai": "Z.AI",
    "kimi-coding": "Kimi",
    "opencode-zen": "OpenCode Zen",
    "opencode-go": "OpenCode Go",
    "ollama": "Ollama",
    "ollama-cloud": "Ollama Cloud",
    "nous": "Nous",
    "openai-codex": "OpenAI Codex",
    "copilot": "GitHub Copilot",
    "qwen-oauth": "Qwen OAuth",
}

_OAUTH_PROVIDERS = {"nous", "openai-codex", "copilot", "qwen-oauth"}

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


def _resolve_model_and_credentials(model_override=None):
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

    override = str(model_override or "").strip()
    if override:
        model = override
    return model, provider, base_url, api_key


def _configured_model_options(current_model=None):
    """Return model choices configured for the UI model switcher."""
    raw = (
        os.environ.get("HERMES_UI_MODELS")
        or os.environ.get("HERMES_MODEL_OPTIONS")
        or os.environ.get("HERMES_MODELS")
        or ""
    )
    items = []
    for part in raw.replace("\n", ",").split(","):
        value = part.strip()
        if value and value not in items:
            items.append(value)
    current = str(current_model or "").strip()
    if current and current not in items:
        items.insert(0, current)
    return items


def _resolve_delegation_credentials():
    """Read delegation.* from config.yaml and resolve credentials.

    Returns (model, provider, base_url, api_key). Any can be None if unset.
    Used by the UI's direct-to-delegation-model sidechannel so users can chat
    with their configured delegation model (Qwen, DeepSeek, whatever) without
    paying MiniMax's orchestration tokens on every turn.
    """
    import yaml
    config_path = os.path.join(HERMES_HOME, "config.yaml")
    model = None
    provider = None
    base_url = None
    api_key = None

    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            d = cfg.get("delegation", {}) or {}
            model = (str(d.get("model") or "").strip()) or None
            provider = (str(d.get("provider") or "").strip()) or None
            base_url = (str(d.get("base_url") or "").strip()) or None
            api_key = (str(d.get("api_key") or "").strip()) or None
        except Exception as e:
            print(f"[serve] WARNING: Failed to read delegation config: {e}", flush=True)

    # Fill in whatever the config didn't specify via Hermes' provider resolver.
    if provider and (not api_key or not base_url):
        try:
            from hermes_cli.runtime_provider import resolve_runtime_provider
            rt = resolve_runtime_provider(requested=provider)
            api_key = api_key or rt.get("api_key")
            base_url = base_url or rt.get("base_url")
        except Exception as e:
            print(f"[serve] WARNING: delegation resolve_runtime_provider failed: {e}", flush=True)

    return model, provider, base_url, api_key

def _load_env_values():
    env_path = pathlib.Path(HERMES_HOME) / ".env"
    values = {}
    if not env_path.exists():
        return values
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception:
        return {}
    return values

def _write_env_update(env_var, value):
    env_path = pathlib.Path(HERMES_HOME) / ".env"
    clean = (str(value or "").strip()) or None
    if clean and ("\n" in clean or "\r" in clean):
        raise ValueError("API key must not contain newline characters.")

    with _ENV_LOCK:
        lines = []
        seen = False
        if env_path.exists():
            try:
                lines = env_path.read_text(encoding="utf-8").splitlines()
            except Exception:
                lines = []

        next_lines = []
        for raw in lines:
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key == env_var:
                    seen = True
                    if clean:
                        next_lines.append(f"{env_var}={clean}")
                    continue
            next_lines.append(raw)

        if clean and not seen:
            if next_lines and next_lines[-1].strip():
                next_lines.append("")
            next_lines.append(f"{env_var}={clean}")

        env_path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o600
        fd = os.open(str(env_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(next_lines) + ("\n" if next_lines else ""))
        try:
            env_path.chmod(mode)
        except OSError:
            pass

        if clean:
            os.environ[env_var] = clean
        else:
            os.environ.pop(env_var, None)

def _read_provider_config_status():
    import yaml

    cfg = {}
    cfg_path = pathlib.Path(HERMES_HOME) / "config.yaml"
    if cfg_path.exists():
        try:
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except Exception:
            cfg = {}

    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    providers_cfg = cfg.get("providers", {}) if isinstance(cfg.get("providers"), dict) else {}
    active_provider = str(model_cfg.get("provider") or "").strip().lower()
    env_values = _load_env_values()
    known = set(_PROVIDER_ENV_VARS) | set(_OAUTH_PROVIDERS) | set(providers_cfg)
    if active_provider:
        known.add(active_provider)

    providers = []
    for pid in sorted(known):
        env_var = _PROVIDER_ENV_VARS.get(pid)
        is_oauth = pid in _OAUTH_PROVIDERS
        key_source = "none"
        has_key = False
        if is_oauth:
            key_source = "oauth"
            try:
                from hermes_cli.auth import get_auth_status
                auth_status = get_auth_status(pid)
                has_key = bool(isinstance(auth_status, dict) and auth_status.get("logged_in"))
            except Exception:
                has_key = False
        elif env_var and env_values.get(env_var):
            has_key = True
            key_source = "env_file"
        elif env_var and os.environ.get(env_var):
            has_key = True
            key_source = "env_var"
        else:
            provider_cfg = providers_cfg.get(pid, {}) if isinstance(providers_cfg, dict) else {}
            provider_key = provider_cfg.get("api_key") if isinstance(provider_cfg, dict) else None
            model_key = model_cfg.get("api_key") if active_provider == pid else None
            if str(provider_key or model_key or "").strip():
                has_key = True
                key_source = "config_yaml"

        providers.append({
            "id": pid,
            "display_name": _PROVIDER_DISPLAY.get(pid, pid.replace("-", " ").title()),
            "env_var": env_var or "",
            "has_key": has_key,
            "configurable": bool(env_var and not is_oauth),
            "key_source": key_source,
            "active": pid == active_provider,
        })

    return {
        "providers": providers,
        "active_provider": active_provider,
        "default_model": model_cfg.get("default", ""),
    }

def _model_capabilities(model, provider=None, agent_ok=False):
    name = str(model or "").lower()
    provider_id = str(provider or "").lower()

    vision = False
    if name:
        if "minimax" in name:
            vision = False
        elif "glm-4" in name and "v" not in name:
            vision = False
        elif any(s in name for s in (
            "gpt-4o", "gpt-4-vision", "gpt-4.1", "claude-3", "claude-4",
            "claude-opus", "claude-sonnet", "claude-haiku", "gemini-",
            "llava", "pixtral", "-vision", "-vl-"
        )):
            vision = True
        elif name.endswith("-vl") or ("qwen" in name and "vl" in name):
            vision = True
        elif provider_id in {"anthropic", "google", "gemini"} and "minimax" not in name:
            vision = True

    reasoning = bool(name and (
        "gpt-5" in name or "o3" in name or "o4" in name or
        "claude" in name or "gemini" in name or "deepseek-r1" in name or
        "qwen3" in name or "grok" in name
    ))

    steer = False
    if agent_ok:
        try:
            agent_cls = _get_ai_agent()
            steer = bool(agent_cls and callable(getattr(agent_cls, "steer", None)))
        except Exception:
            steer = False

    return {
        "vision": vision,
        "image_mode": "native" if vision else "gemini_fallback",
        "steer": steer,
        "reasoning": reasoning,
        "tools": bool(agent_ok),
        "oauth": provider_id in _OAUTH_PROVIDERS,
        "live_models": provider_id in {
            "anthropic", "openai", "openai-codex", "google", "gemini",
            "openrouter", "ollama-cloud", "mistralai", "x-ai", "deepseek",
            "minimax", "zai", "kimi-coding", "qwen-oauth"
        },
    }


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


_REASONING_EFFORTS = {"none", "low", "medium", "high", "xhigh"}


def _parse_reasoning_effort(value):
    """Return a Hermes agent reasoning_config for a UI-selected effort."""
    effort = str(value or "").strip().lower()
    if not effort or effort == "auto":
        return None, ""
    if effort not in _REASONING_EFFORTS:
        return None, ""
    try:
        from hermes_constants import parse_reasoning_effort
        return parse_reasoning_effort(effort), effort
    except Exception as exc:
        print(f"[serve] WARNING: reasoning effort parse fallback ({exc!r})", flush=True)
        if effort == "none":
            return {"enabled": False}, effort
        return {"enabled": True, "effort": effort}, effort

# Per-session agent locks — prevents two requests on the SAME session
# from running concurrently (different sessions can still run in parallel)
_SESSION_LOCKS = {}
_SESSION_LOCKS_LOCK = threading.Lock()

def _get_session_lock(session_id):
    with _SESSION_LOCKS_LOCK:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = threading.Lock()
        return _SESSION_LOCKS[session_id]


# ── API-safe message sanitization (ported from nesquena/hermes-webui) ──────
# Matches api/streaming.py: _API_SAFE_MSG_KEYS, _sanitize_messages_for_api,
# _restore_reasoning_metadata. Keeps tool_calls/tool_call_id intact so weak
# tool-callers (MiniMax) keep seeing real tool-use precedent in history.
_API_SAFE_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name", "refusal"}


def _sanitize_messages_for_api(messages):
    """Return a list of messages with only API-safe fields, dropping orphaned tool results.

    Strictly-conformant providers (Mercury-2, newer OpenAI) 400 when a tool-role
    message has no matching assistant tool_call_id, so we drop orphans before send.
    """
    if not messages:
        return []
    valid_tool_call_ids = set()
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tid = tc.get("id") or tc.get("call_id") or ""
                    if tid:
                        valid_tool_call_ids.add(tid)
    clean = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        # Skip persisted error markers — never send them to the LLM as prior
        # context. Matches nesq _sanitize_messages_for_api (closes error-loop
        # feedback where a failed turn would be replayed to the model).
        if msg.get("_error"):
            continue
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id") or ""
            if not tid or tid not in valid_tool_call_ids:
                continue  # orphaned tool result — drop
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get("role"):
            clean.append(sanitized)
    return clean


def _restore_reasoning_metadata(previous_messages, updated_messages):
    """Carry forward assistant `reasoning` lost during API-safe sanitization.

    The provider-facing history strips WebUI-only fields like `reasoning`. When the
    agent returns its new full message history, prior assistant messages come back
    without that metadata unless we merge it back in by position.
    """
    if not previous_messages or not updated_messages:
        return list(updated_messages) if updated_messages else []
    updated = list(updated_messages)
    prev_safe = [m for m in previous_messages
                 if isinstance(m, dict) and m.get("role") in ("user", "assistant", "tool")]
    for i, cur in enumerate(updated):
        if i >= len(prev_safe):
            break
        prev = prev_safe[i]
        if not isinstance(prev, dict) or not isinstance(cur, dict):
            continue
        if prev.get("role") != cur.get("role"):
            continue
        if (prev.get("role") == "assistant"
                and prev.get("reasoning")
                and not cur.get("reasoning")):
            cur["reasoning"] = prev["reasoning"]
    return updated


# ── Streaming infrastructure ────────────────────────────────────────────────
# stream_id -> queue.Queue of (event, data) tuples
STREAMS = {}
STREAMS_LOCK = threading.Lock()
CANCEL_FLAGS = {}   # stream_id -> threading.Event
AGENT_INSTANCES = {} # stream_id -> agent instance (for cancel/interrupt)
STREAM_PARTIAL_TEXT = {}  # stream_id -> str, accumulated tokens for cancel-preserve (nesq #893)
STREAM_SESSIONS = {}  # stream_id -> session_id, so cancel_stream can persist partial content
STREAM_STEER_STATE = {}  # stream_id -> {"next_id": int, "pending": [steer_record, ...]}

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


def _steer_preview(text, limit=80):
    """Return a compact one-line preview for UI/log payloads."""
    s = " ".join(str(text or "").split())
    if len(s) > limit:
        return s[:limit - 3] + "..."
    return s


def _ensure_stream_steer_state(stream_id):
    """Return the per-stream steer bookkeeping dict, creating it if needed."""
    state = STREAM_STEER_STATE.get(stream_id)
    if state is None:
        state = {"next_id": 1, "pending": []}
        STREAM_STEER_STATE[stream_id] = state
    return state


def _queue_stream_steer(stream_id, text):
    """Record one accepted /steer so UI feedback can track its lifecycle."""
    with STREAMS_LOCK:
        state = _ensure_stream_steer_state(stream_id)
        steer_id = state["next_id"]
        state["next_id"] += 1
        record = {
            "id": steer_id,
            "text": text,
            "preview": _steer_preview(text),
            "accepted_at": time.time(),
        }
        state["pending"].append(record)
        return dict(record)


def _drain_stream_steers(stream_id):
    """Pop all still-pending steer records for this stream."""
    with STREAMS_LOCK:
        state = STREAM_STEER_STATE.get(stream_id)
        if not state or not state.get("pending"):
            return []
        pending = list(state["pending"])
        state["pending"].clear()
        return pending


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


def _run_agent_streaming(session_id, messages, stream_id, base_system_prompt="", reasoning_effort="", workspace=None, model_override=""):
    """Run AIAgent in a background thread, pushing SSE events to the queue."""
    q = STREAMS.get(stream_id)
    if q is None:
        return

    cancel_event = threading.Event()
    with STREAMS_LOCK:
        CANCEL_FLAGS[stream_id] = cancel_event
        STREAM_PARTIAL_TEXT[stream_id] = ''
        STREAM_SESSIONS[stream_id] = session_id
        _ensure_stream_steer_state(stream_id)

    def put(event, data):
        if cancel_event.is_set() and event not in ("cancel", "error"):
            return
        try:
            q.put_nowait((event, data))
        except Exception:
            pass

    # Set thread-local env context for this agent thread
    try:
        workspace_dir = _resolve_workspace(workspace)
    except Exception as e:
        print(f"[serve] WARNING: invalid workspace {workspace!r}: {e}; using project root", flush=True)
        workspace_dir = str(PROJECT_ROOT.resolve())

    _set_thread_env(
        TERMINAL_CWD=workspace_dir,
        HERMES_SESSION_KEY=session_id,
    )

    # Save and set process-level env vars under lock
    with _ENV_LOCK:
        old_cwd = os.environ.get("TERMINAL_CWD")
        old_exec_ask = os.environ.get("HERMES_EXEC_ASK")
        old_session_key = os.environ.get("HERMES_SESSION_KEY")
        os.environ["TERMINAL_CWD"] = workspace_dir
        os.environ.pop("HERMES_EXEC_ASK", None)
        os.environ["HERMES_SESSION_KEY"] = session_id

    _approval_registered = False
    _unreg_notify = None
    # Initialised here (before any code that may raise) so the outer `finally`
    # block can safely check `if _checkpoint_stop is not None` even when an
    # exception fires before the checkpoint thread is created.
    # Matches nesquena/hermes-webui api/streaming.py (Issue #765).
    _checkpoint_stop = None
    _checkpoint_activity = [0]

    try:
        # Check for pre-flight cancel
        if cancel_event.is_set():
            put("cancel", {"message": "Cancelled before start"})
            return

        AgentClass = _get_ai_agent()
        if AgentClass is None:
            put("error", {"message": "AIAgent not available — check hermes-agent installation"})
            return

        model, provider, base_url, api_key = _resolve_model_and_credentials(model_override)
        if model_override:
            print(f"[serve] model_override={model}", flush=True)

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
            # Accumulate for cancel-preserve (nesq #893) — so if the user hits
            # Stop mid-stream we can persist what was generated so far.
            try:
                with STREAMS_LOCK:
                    if stream_id in STREAM_PARTIAL_TEXT:
                        STREAM_PARTIAL_TEXT[stream_id] += str(text)
            except Exception:
                pass
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

            def _snap_tool_arg(value, depth=0):
                if depth >= 3:
                    s = str(value)
                    return s[:120] + ("..." if len(s) > 120 else "")
                if isinstance(value, dict):
                    out = {}
                    for k, v in list(value.items())[:8]:
                        out[str(k)] = _snap_tool_arg(v, depth + 1)
                    return out
                if isinstance(value, list):
                    return [_snap_tool_arg(v, depth + 1) for v in value[:6]]
                s = str(value)
                return s[:240] + ("..." if len(s) > 240 else "")

            args_snap = {}
            if isinstance(args, dict):
                for k, v in list(args.items())[:8]:
                    args_snap[str(k)] = _snap_tool_arg(v)

            if event_type in (None, "tool.started"):
                put("tool", {"name": name, "preview": preview, "args": args_snap})
            elif event_type == "tool.completed":
                result_snap = None
                if "result" in kwargs:
                    result_snap = _snap_tool_arg(kwargs.get("result"))
                elif "output" in kwargs:
                    result_snap = _snap_tool_arg(kwargs.get("output"))
                put("tool_complete", {
                    "name": name, "preview": preview, "args": args_snap,
                    "duration": kwargs.get("duration"), "result": result_snap,
                })
                # Signal the periodic checkpoint thread that real progress has
                # been made (Issue #765). The agent works on an internal copy
                # of s.messages during run_conversation, so watching
                # message-count would never trigger — tool completions are
                # the first reliable mid-run signal.
                _checkpoint_activity[0] += 1

        def on_step(api_call_count, prev_tools):
            # /steer is actually applied at the start of the NEXT agent
            # iteration, right before the next API call is built. The agent
            # only has something to inject into if the previous iteration
            # produced tool results, so use prev_tools as the guard.
            if not prev_tools or not any(t.get("result") is not None for t in prev_tools):
                return
            applied = _drain_stream_steers(stream_id)
            if not applied:
                return
            put("steer_applied", {
                "count": len(applied),
                "api_call": api_call_count,
                "items": [
                    {"id": item.get("id"), "preview": item.get("preview", "")}
                    for item in applied
                ],
            })

        # Build the agent.
        # Guard newer AIAgent params via signature introspection so we degrade
        # gracefully on older hermes-agent builds (matches nesquena/hermes-webui
        # api/streaming.py pattern — issue #772 in their repo).
        import inspect as _inspect
        _agent_params = set(_inspect.signature(AgentClass.__init__).parameters)

        _agent_kwargs = dict(
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
            step_callback=on_step,
        )

        # Pin Honcho memory sessions to the stable WebUI session ID. Without
        # this, the 'per-session' Honcho strategy creates a fresh Honcho session
        # on EVERY streaming request because HonchoSessionManager is
        # re-instantiated each turn — which is why the agent kept losing memory
        # mid-chat despite no compaction firing. Fix ported from nesquena
        # /hermes-webui issue #855.
        if 'gateway_session_key' in _agent_params:
            _agent_kwargs['gateway_session_key'] = session_id

        _reasoning_config, _reasoning_label = _parse_reasoning_effort(reasoning_effort)
        if _reasoning_config is not None and 'reasoning_config' in _agent_params:
            _agent_kwargs['reasoning_config'] = _reasoning_config
            print(f"[serve] reasoning_effort={_reasoning_label}", flush=True)
        elif _reasoning_label:
            print(
                f"[serve] reasoning_effort={_reasoning_label} ignored; "
                "agent lacks reasoning_config",
                flush=True,
            )

        agent = AgentClass(**_agent_kwargs)

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

        # Extract just the latest user message text, plus any attached images
        # (dataURL or http URL) that the frontend marked for the native
        # multimodal path. Images are only honored on the FINAL user message
        # — older turns are persisted text-only to keep session.json small
        # and to avoid replaying huge base64 blobs on every turn.
        user_msg = ""
        user_images = []
        for m in reversed(messages):
            if m.get("role") == "user":
                _raw_content = m.get("content", "")
                if isinstance(_raw_content, str):
                    user_msg = _raw_content
                elif isinstance(_raw_content, list):
                    # Frontend sent pre-built multimodal blocks — rejoin text for
                    # persistence + keep images as-is for the model.
                    _text_parts = []
                    for _blk in _raw_content:
                        if isinstance(_blk, dict):
                            if _blk.get("type") == "text":
                                _text_parts.append(str(_blk.get("text") or ""))
                            elif _blk.get("type") in ("image_url", "input_image"):
                                user_images.append(_blk)
                    user_msg = "\n".join(_p for _p in _text_parts if _p)
                # Sidecar images array (our UI convention — easier than
                # asking the frontend to build OpenAI content blocks).
                _sidecar = m.get("images") or []
                if isinstance(_sidecar, list):
                    for _im in _sidecar:
                        if not isinstance(_im, dict):
                            continue
                        _url = _im.get("dataUrl") or _im.get("url")
                        if not _url or not isinstance(_url, str):
                            continue
                        user_images.append({
                            "type": "image_url",
                            "image_url": {"url": _url},
                        })
                break

        # Workspace context prefix (matches hermes-webui behaviour)
        _workspace = workspace_dir
        workspace_ctx = f"[Workspace: {_workspace}]\n"
        workspace_system_msg = (
            f"Active workspace: {_workspace}\n"
            "Every user message is prefixed with [Workspace: /path] indicating the "
            "active workspace. Use this as the default working directory for all "
            "file operations. For code searches, stay inside this workspace and "
            "prefer ripgrep-style targeted searches. Do not recursively search "
            "the user's home directory or parent directories unless the user "
            "explicitly asks for that broader scope."
        )

        # Build conversation history from SERVER-SIDE session only — always.
        # Matches nesquena/hermes-webui api/streaming.py:1109-1118:
        #   conversation_history=_sanitize_messages_for_api(s.messages)
        #
        # We previously had a frontend-fallback branch ("prefer frontend when it
        # has more user messages") to handle server-side compaction/restart edge
        # cases, but frontend messages only carry {role, content} pairs. Selecting
        # them permanently strips tool_calls/tool_result from the saved session
        # once result.get('messages') gets written back. MiniMax (and any weak
        # tool-caller) then loses all tool-use precedent and falls into narration
        # mode for the rest of the session. Always using server history prevents
        # that cascade.
        session = _get_or_create_session(session_id)
        _previous_messages = list(session.get("messages") or [])
        clean_history = _sanitize_messages_for_api(_previous_messages)
        # Remove the last user message — it goes in user_message param instead
        if clean_history and clean_history[-1].get("role") == "user":
            clean_history.pop()

        # Persist the incoming user message BEFORE run_conversation so a server
        # crash mid-turn doesn't silently drop what the user just typed.
        # Mirrors nesquena/hermes-webui `s.pending_user_message` pattern
        # (Issue #765). The final _save_session at end of stream is
        # authoritative; this is a durability floor.
        _pending_msgs = list(_previous_messages)
        if user_msg and not (_pending_msgs and _pending_msgs[-1].get("role") == "user"
                             and _pending_msgs[-1].get("content") == user_msg):
            _pending_msgs.append({"role": "user", "content": user_msg})
        session["messages"] = _pending_msgs
        session["model"] = model
        _save_session(session_id, session)

        # ── Periodic checkpoint thread (Issue #765) ──
        # Save the session every 15s while a tool is actively completing.
        # Worst case on server restart: up to 15s of tool-call progress lost
        # rather than the entire conversation turn.
        def _periodic_checkpoint():
            last_saved_activity = 0
            while not _checkpoint_stop.wait(15):
                try:
                    cur = _checkpoint_activity[0]
                    if cur > last_saved_activity:
                        _save_session(session_id, session)
                        last_saved_activity = cur
                except Exception as _ckpt_err:
                    print(f"[serve] checkpoint save failed: {_ckpt_err}", flush=True)
        _checkpoint_stop = threading.Event()
        _ckpt_thread = threading.Thread(
            target=_periodic_checkpoint, daemon=True,
            name=f"ckpt-{session_id[:8]}",
        )
        _ckpt_thread.start()

        # When the user attached images for a vision-capable model, pass
        # them as native multimodal content blocks to run_conversation —
        # the agent (v0.11.0+) handles ``image_url`` + ``input_image``
        # blocks natively. ``persist_user_message`` stays plain-text so
        # transcripts don't bloat with base64 blobs. Non-vision models
        # never reach this branch because the UI routes them through the
        # Gemini-describe fallback and strips .images before POSTing.
        if user_images:
            _agent_user_msg = [
                {"type": "text", "text": workspace_ctx + user_msg},
                *user_images,
            ]
            print(
                f"[serve] run_conversation multimodal: text_len={len(user_msg)} "
                f"images={len(user_images)}",
                flush=True,
            )
        else:
            _agent_user_msg = workspace_ctx + user_msg

        result = agent.run_conversation(
            user_message=_agent_user_msg,
            system_message=workspace_system_msg,
            conversation_history=clean_history,
            task_id=session_id,
            persist_user_message=user_msg,
        )

        # Update session with agent's messages (includes tool_calls + tool results).
        # Merge reasoning metadata back from the prior turn, since API-safe
        # sanitization stripped it before send (matches webui's _restore_reasoning_metadata).
        #
        # Skip this if the user cancelled — cancel_stream() already wrote the
        # preserved `_partial` + `_error` markers to the session, and
        # overwriting here would replace them with the agent's
        # partial/empty run_conversation result (nesq #893 companion fix).
        if not cancel_event.is_set():
            _merged = _restore_reasoning_metadata(
                _previous_messages,
                result.get("messages") or session.get("messages") or [],
            )
            # Collapse any multimodal user content back to plain text before
            # persisting. The agent saw the image in-turn; we don't want to
            # replay 2MB+ of base64 on every subsequent turn of this chat —
            # users can re-paste if they need the model to re-see it. The
            # `_image_count` tag lets the UI render an attachment indicator
            # without keeping the bytes.
            for _m in _merged:
                if not isinstance(_m, dict):
                    continue
                if _m.get("role") != "user":
                    continue
                _c = _m.get("content")
                if not isinstance(_c, list):
                    continue
                _texts, _img_count = [], 0
                for _blk in _c:
                    if isinstance(_blk, dict):
                        if _blk.get("type") == "text":
                            _texts.append(str(_blk.get("text") or ""))
                        elif _blk.get("type") in ("image_url", "input_image"):
                            _img_count += 1
                _m["content"] = "\n".join(_t for _t in _texts if _t)
                if _img_count:
                    _m["_image_count"] = _img_count
            session["messages"] = _merged
            session["model"] = model
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
        _late_steer = result.get("pending_steer")
        if _late_steer:
            late_items = _drain_stream_steers(stream_id)
            if not late_items:
                late_items = [{
                    "id": None,
                    "text": str(_late_steer),
                    "preview": _steer_preview(_late_steer),
                    "accepted_at": time.time(),
                }]
            put("steer_late", {
                "text": str(_late_steer),
                "count": len(late_items),
                "items": [
                    {"id": item.get("id"), "preview": item.get("preview", "")}
                    for item in late_items
                ],
            })

        put("done", {
            "session": {
                "session_id": session_id,
                "messages": session.get("messages", []),
                "model": session.get("model") or model,
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
        # Stop periodic checkpoint thread if it was started (Issue #765)
        if _checkpoint_stop is not None:
            _checkpoint_stop.set()
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
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_SESSIONS.pop(stream_id, None)
            STREAM_STEER_STATE.pop(stream_id, None)


def cancel_stream(stream_id):
    """Signal an in-flight stream to cancel. Returns True if the stream existed.

    Also preserves any partial streamed content as a `_partial: True` message on
    the session, followed by a `*Task cancelled.*` `_error: True` marker — so
    users can see what the agent had generated before they hit Stop, rather
    than losing the whole turn. Ported from nesquena/hermes-webui #893.
    """
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
        # Snapshot partial text + session_id under lock, then release before
        # doing any session I/O (which takes a different lock).
        _cancel_partial_text = STREAM_PARTIAL_TEXT.get(stream_id, '')
        _cancel_session_id = STREAM_SESSIONS.get(stream_id)
        q = STREAMS.get(stream_id)

    # Persist partial content (outside STREAMS_LOCK) so the user doesn't lose
    # what the model had already generated.
    try:
        partial_text = _cancel_partial_text.strip() if _cancel_partial_text else ''
        if partial_text and _cancel_session_id:
            import re as _re
            # Strip both well-formed and unclosed <think>/<thinking> blocks so
            # raw chain-of-thought never leaks into saved messages.
            _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>',
                                '', partial_text,
                                flags=_re.DOTALL | _re.IGNORECASE).strip()
            _stripped = _re.sub(r'<think(?:ing)?\b[^>]*>.*',
                                '', _stripped,
                                flags=_re.DOTALL | _re.IGNORECASE).strip()
            if _stripped:
                sess = _get_or_create_session(_cancel_session_id)
                with SESSIONS_LOCK:
                    sess.setdefault('messages', []).append({
                        'role': 'assistant',
                        'content': _stripped,
                        '_partial': True,
                        'timestamp': int(time.time()),
                    })
                    sess['messages'].append({
                        'role': 'assistant',
                        'content': '*Task cancelled.*',
                        '_error': True,
                        'timestamp': int(time.time()),
                    })
                _save_session(_cancel_session_id, sess)
    except Exception as _e:
        print(f"[serve] WARNING: cancel-preserve failed for {stream_id}: {_e}", flush=True)

    # Push the cancel event last, bundling the updated session so the
    # frontend can render the preserved _partial + _error messages without
    # a separate re-fetch (nesq #882 cancel-message ordering fix).
    _session_payload = None
    if _cancel_session_id:
        try:
            _sess = _get_or_create_session(_cancel_session_id)
            _session_payload = {
                "session_id": _cancel_session_id,
                "messages": list(_sess.get("messages", [])),
                "model": _sess.get("model"),
            }
        except Exception:
            _session_payload = None
    if q:
        try:
            q.put_nowait(("cancel", {
                "message": "Cancelled by user",
                "session": _session_payload,
            }))
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

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _auth_cookie_value(self):
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, sep, value = part.strip().partition("=")
            if sep and name == AUTH_COOKIE_NAME:
                return value
        return ""

    def _is_authenticated(self):
        return (not _auth_enabled()) or _auth_verify_token(self._auth_cookie_value())

    def _auth_required(self):
        return self._json({"ok": False, "error": "Authentication required"}, 401)

    def _route_requires_auth(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in ("/health", "/api/auth/status", "/api/auth/login", "/api/auth/logout"):
            return False
        static_exts = (".html", ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".woff", ".woff2")
        if path == "/" or path.endswith(static_exts):
            return False
        return True

    def _auth_status(self):
        return self._json({"enabled": _auth_enabled(), "authenticated": self._is_authenticated()})

    def _auth_login(self):
        if not _auth_enabled():
            return self._json({"ok": True, "authenticated": True, "enabled": False})
        try:
            body = self._read_json_body()
        except Exception:
            return self._json({"ok": False, "error": "Invalid login request"}, 400)
        password = str(body.get("password") or "")
        if not hmac.compare_digest(password, AUTH_PASSWORD):
            return self._json({"ok": False, "error": "Wrong password"}, 401)
        token = _auth_make_token()
        payload = json.dumps({"ok": True, "authenticated": True, "enabled": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"{AUTH_COOKIE_NAME}={token}; Path=/; Max-Age=86400; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _auth_logout(self):
        payload = json.dumps({"ok": True, "authenticated": False}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", f"{AUTH_COOKIE_NAME}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        try:
            workspace = _resolve_workspace(body.get("workspace"))
            _set_last_workspace(workspace)
            session = _get_or_create_session(session_id)
            session["workspace"] = workspace
            _save_session(session_id, session)
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        # User-configurable base system prompt from Settings → General
        base_system_prompt = (body.get("base_system_prompt") or "").strip()
        reasoning_effort = str(body.get("reasoning_effort") or "").strip().lower()
        model_override = str(body.get("model") or "").strip()
        if reasoning_effort == "auto":
            reasoning_effort = ""
        if reasoning_effort and reasoning_effort not in _REASONING_EFFORTS:
            print(
                f"[serve] /api/chat/start invalid reasoning_effort={reasoning_effort!r} — ignoring",
                flush=True,
            )
            reasoning_effort = ""

        # Optional user-local prompt addon.  If ~/.hermes/extra_system_prompt.md
        # exists, prepend its contents to the base_system_prompt.  This lets
        # individual users inject site-specific instructions (e.g. "route X to
        # delegation model Y") without forking hermes-ui.  The file is NOT
        # part of this repo — it lives in the user's private ~/.hermes/ dir,
        # so this is a no-op for anyone who hasn't opted in.
        try:
            _extra_path = os.path.expanduser("~/.hermes/extra_system_prompt.md")
            if os.path.isfile(_extra_path):
                with open(_extra_path, "r", encoding="utf-8") as _ef:
                    _extra = _ef.read().strip()
                if _extra:
                    _extra_sep = "\n\n---\n\n" if base_system_prompt else ""
                    base_system_prompt = (_extra + _extra_sep + base_system_prompt).strip()
        except Exception as _extra_err:
            print(
                f"[serve] extra_system_prompt read failed: {_extra_err!r} — "
                f"skipping",
                flush=True,
            )

        if base_system_prompt:
            # Log only when a prompt is actually set, so the default-empty case
            # stays quiet.  Useful when debugging "is my personality arriving?".
            print(
                f"[serve] /api/chat/start base_system_prompt="
                f"{len(base_system_prompt)} chars: {base_system_prompt[:80]!r}",
                flush=True,
            )

        # Behavioral guidelines toggle from Settings → General.  When ON, we
        # read behavioral_guidelines.md fresh (no cache) and append it to the
        # base_system_prompt with a clear separator.  File lives alongside
        # serve_lite.py so it's version-controlled and editable without
        # touching code.  Changes take effect on new chats — existing chats
        # keep the setting they started with.
        if bool(body.get("apply_behavioral_guidelines")):
            guidelines_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "behavioral_guidelines.md",
            )
            try:
                with open(guidelines_path, "r", encoding="utf-8") as _gf:
                    _guidelines = _gf.read().strip()
                if _guidelines:
                    _separator = "\n\n---\n\n" if base_system_prompt else ""
                    base_system_prompt = (base_system_prompt + _separator + _guidelines).strip()
                    print(
                        f"[serve] /api/chat/start behavioral_guidelines appended "
                        f"({len(_guidelines)} chars)",
                        flush=True,
                    )
            except FileNotFoundError:
                print(
                    f"[serve] /api/chat/start apply_behavioral_guidelines=True "
                    f"but {guidelines_path} not found — skipping",
                    flush=True,
                )
            except Exception as _bg_err:
                print(
                    f"[serve] /api/chat/start behavioral_guidelines read failed: "
                    f"{_bg_err!r} — skipping",
                    flush=True,
                )

        if not messages:
            return self._json({"error": "No messages provided"}, 400)

        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q

        thr = threading.Thread(
            target=_run_agent_streaming,
            args=(session_id, messages, stream_id, base_system_prompt, reasoning_effort, workspace, model_override),
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

    def _handle_chat_steer(self):
        """Inject a no-pause nudge into the live agent turn.

        Calls ``agent.steer(text)`` which stashes the text into
        ``_pending_steer``; the agent loop appends it to the last tool
        result before the next API call. Unlike /api/chat/cancel, this
        does NOT interrupt the current LLM call — the model keeps running
        and the nudge lands on the next iteration.

        Requires hermes-agent v0.11.0+ (exposes ``AIAgent.steer``). On
        older builds we return 501 so the UI can fall back to the classic
        pause-then-interject flow.
        """
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or b"{}")
        except Exception as e:
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)
        stream_id = str(body.get("stream_id") or "").strip()
        text = str(body.get("text") or "").strip()
        if not stream_id:
            return self._json({"ok": False, "error": "stream_id required"}, 400)
        if not text:
            return self._json({"ok": False, "error": "text required"}, 400)
        with STREAMS_LOCK:
            agent = AGENT_INSTANCES.get(stream_id)
        if agent is None:
            # Stream already finished, or never existed. Frontend should
            # treat as "too late — send as a new message instead".
            return self._json({"ok": False, "error": "stream not active", "code": "not_active"}, 409)
        steer_fn = getattr(agent, "steer", None)
        if not callable(steer_fn):
            return self._json({
                "ok": False,
                "error": "agent build does not support steer — update hermes-agent to v0.11.0+",
                "code": "unsupported",
            }, 501)
        try:
            accepted = bool(steer_fn(text))
        except Exception as e:
            print(f"[serve] /api/chat/steer agent.steer() failed: {e!r}", flush=True)
            return self._json({"ok": False, "error": str(e)}, 500)
        steer_meta = None
        if accepted:
            steer_meta = _queue_stream_steer(stream_id, text)
        print(
            f"[serve] /api/chat/steer stream={stream_id[:8]} accepted={accepted} "
            f"len={len(text)}",
            flush=True,
        )
        payload = {"ok": True, "accepted": accepted}
        if steer_meta:
            payload["steer"] = {
                "id": steer_meta["id"],
                "preview": steer_meta["preview"],
            }
        return self._json(payload)

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
            "capabilities": _model_capabilities(model, provider, agent_ok=agent_ok),
            "uptime": int(time.time() - _START_TIME),
        })

    def _handle_providers(self):
        try:
            self._json(_read_provider_config_status())
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_models(self):
        """GET /api/models — return model choices for the chat model switcher."""
        try:
            model, provider, _, _ = _resolve_model_and_credentials()
            options = _configured_model_options(model)
            self._json({
                "current": model or "",
                "provider": provider or "",
                "models": [
                    {
                        "id": item,
                        "label": item.split("/")[-1] if "/" in item else item,
                        "active": item == model,
                    }
                    for item in options
                ],
                "configured": bool(
                    os.environ.get("HERMES_UI_MODELS")
                    or os.environ.get("HERMES_MODEL_OPTIONS")
                    or os.environ.get("HERMES_MODELS")
                ),
            })
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_provider_key_save(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            provider_id = str(body.get("provider") or "").strip().lower()
            api_key = body.get("api_key")
            if not provider_id:
                return self._json({"ok": False, "error": "provider is required"}, 400)
            if provider_id in _OAUTH_PROVIDERS:
                display = _PROVIDER_DISPLAY.get(provider_id, provider_id)
                return self._json({"ok": False, "error": f"{display} uses OAuth. Configure it with Hermes CLI."}, 400)
            env_var = _PROVIDER_ENV_VARS.get(provider_id)
            if not env_var:
                return self._json({"ok": False, "error": "No known API-key slot for this provider"}, 400)
            if api_key is not None:
                api_key = str(api_key).strip()
            if api_key and len(api_key) < 8:
                return self._json({"ok": False, "error": "API key appears too short"}, 400)
            _write_env_update(env_var, api_key)
            self._json({
                "ok": True,
                "provider": provider_id,
                "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
                "action": "updated" if api_key else "removed",
                "status": _read_provider_config_status(),
            })
        except ValueError as e:
            self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _handle_provider_key_delete(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            provider_id = str(body.get("provider") or "").strip().lower()
            if not provider_id:
                return self._json({"ok": False, "error": "provider is required"}, 400)
            env_var = _PROVIDER_ENV_VARS.get(provider_id)
            if not env_var:
                return self._json({"ok": False, "error": "No managed API key for this provider"}, 400)
            _write_env_update(env_var, None)
            self._json({
                "ok": True,
                "provider": provider_id,
                "display_name": _PROVIDER_DISPLAY.get(provider_id, provider_id),
                "action": "removed",
                "status": _read_provider_config_status(),
            })
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

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

    # ── Spaces / workspaces ──
    def _workspaces_load(self):
        workspaces = _load_workspaces()
        last = _get_last_workspace()
        if not any(w.get("path") == last for w in workspaces):
            last = workspaces[0]["path"]
        self._json({"workspaces": workspaces, "last": last})

    def _workspace_add(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = _resolve_workspace(body.get("path"))
            name = str(body.get("name") or _workspace_label(path)).strip() or _workspace_label(path)
            workspaces = _load_workspaces()
            if not any(w.get("path") == path for w in workspaces):
                workspaces.append({"name": name, "path": path})
            else:
                workspaces = [dict(w, name=name) if w.get("path") == path else w for w in workspaces]
            _save_workspaces(workspaces)
            _set_last_workspace(path)
            self._json({"ok": True, "workspaces": workspaces, "last": path})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _workspace_remove(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = _resolve_workspace(body.get("path"))
            workspaces = [w for w in _load_workspaces() if w.get("path") != path]
            if not workspaces:
                workspaces = _default_workspaces()
            _save_workspaces(workspaces)
            last = _get_last_workspace()
            if last == path:
                last = _set_last_workspace(workspaces[0]["path"])
            self._json({"ok": True, "workspaces": workspaces, "last": last})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _workspace_rename(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = _resolve_workspace(body.get("path"))
            name = str(body.get("name") or _workspace_label(path)).strip() or _workspace_label(path)
            workspaces = [dict(w, name=name) if w.get("path") == path else w for w in _load_workspaces()]
            _save_workspaces(workspaces)
            self._json({"ok": True, "workspaces": workspaces, "last": _get_last_workspace()})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _workspace_switch(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            path = _set_last_workspace(body.get("path"))
            self._json({"ok": True, "last": path, "workspaces": _load_workspaces()})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _workspace_browse(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        try:
            current = _resolve_workspace_picker_path(params.get("path", [""])[0])
            roots = _workspace_picker_roots()
            root_paths = [root["path"] for root in roots]
            parent = None
            candidate = str(pathlib.Path(current).parent.resolve())
            if candidate != current and _path_is_within_any(candidate, root_paths):
                parent = candidate
            items = []
            for name in sorted(os.listdir(current), key=lambda value: value.lower()):
                full = os.path.join(current, name)
                if os.path.isdir(full):
                    items.append({"name": name, "path": str(pathlib.Path(full).resolve())})
            self._json({"ok": True, "path": current, "parent": parent, "roots": roots, "items": items})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

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
                cwd=str(PROJECT_ROOT),
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
        try:
            base = _resolve_workspace(params.get("workspace", [""])[0])
        except Exception as e:
            return self._json({"entries": [], "error": str(e)}, 400)
        full = os.path.normpath(os.path.join(base, browse_path)) if browse_path else base
        if os.path.commonpath([base, full]) != base:
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
        self._json({"items": entries, "path": browse_path, "workspace": base})

    def _read_file(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        fpath = params.get("path", [""])[0]
        try:
            base = _resolve_workspace(params.get("workspace", [""])[0])
        except Exception as e:
            return self._json({"content": str(e), "name": "", "path": fpath, "size": 0, "type": "text"}, 400)
        full = os.path.normpath(os.path.join(base, fpath)) if fpath else ""
        if not full or os.path.commonpath([base, full]) != base or not os.path.isfile(full):
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
        try:
            base = _resolve_workspace(body.get("workspace"))
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        full = os.path.normpath(os.path.join(base, fpath)) if fpath else ""
        if not full or os.path.commonpath([base, full]) != base:
            self.send_error(403, "Access denied")
            return
        os.makedirs(os.path.dirname(full), exist_ok=True)
        open(full, "w").write(content)
        self._json({"ok": True, "success": True})

    def _file_create(self):
        try:
            body = self._read_json_body()
            base, rel_path, full = _workspace_target(body.get("workspace"), body.get("path"), require_existing_parent=True)
            if os.path.exists(full):
                return self._json({"ok": False, "error": "File already exists"}, 409)
            with open(full, "w", encoding="utf-8") as f:
                f.write(str(body.get("content") or ""))
            self._json({"ok": True, "path": rel_path, "workspace": base})
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _file_mkdir(self):
        try:
            body = self._read_json_body()
            base, rel_path, full = _workspace_target(body.get("workspace"), body.get("path"), require_existing_parent=True)
            if os.path.exists(full):
                return self._json({"ok": False, "error": "Folder already exists"}, 409)
            os.mkdir(full)
            self._json({"ok": True, "path": rel_path, "workspace": base})
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _file_rename(self):
        try:
            body = self._read_json_body()
            base, rel_path, full = _workspace_target(body.get("workspace"), body.get("path"), require_existing_parent=False)
            _, new_rel_path, new_full = _workspace_target(body.get("workspace"), body.get("new_path"), require_existing_parent=True)
            if not os.path.exists(full):
                return self._json({"ok": False, "error": "Item not found"}, 404)
            if os.path.exists(new_full):
                return self._json({"ok": False, "error": "Destination already exists"}, 409)
            os.rename(full, new_full)
            self._json({"ok": True, "path": new_rel_path, "old_path": rel_path, "workspace": base})
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    def _file_delete(self):
        try:
            body = self._read_json_body()
            base, rel_path, full = _workspace_target(body.get("workspace"), body.get("path"), require_existing_parent=False)
            if not os.path.exists(full):
                return self._json({"ok": False, "error": "Item not found"}, 404)
            if os.path.isdir(full):
                shutil.rmtree(full)
            else:
                os.remove(full)
            self._json({"ok": True, "path": rel_path, "workspace": base})
        except PermissionError as e:
            self._json({"ok": False, "error": str(e)}, 403)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    # ── RTF → plain-text conversion (macOS textutil) ──
    # Used by the composer drop/file-picker so users can attach Rich Text
    # Format files directly — Hermes reads the plain text, no RTF control
    # codes. Accepts raw RTF bytes in the request body, returns {ok,text}.
    def _handle_rtf_to_txt(self):
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length <= 0:
                return self._json({"error": "empty body"}, 400)
            if length > 10 * 1024 * 1024:  # 10 MB ceiling, matches UI
                return self._json({"error": "file too large"}, 413)
            raw = self.rfile.read(length)
            # Quick sniff — reject obvious non-RTF so we don't shell out on random bytes.
            if not raw.lstrip().startswith(b"{\\rtf"):
                return self._json({"error": "not an RTF file"}, 400)
            with tempfile.TemporaryDirectory() as td:
                src = os.path.join(td, "in.rtf")
                dst = os.path.join(td, "out.txt")
                with open(src, "wb") as fh:
                    fh.write(raw)
                try:
                    subprocess.run(
                        ["textutil", "-convert", "txt",
                         "-encoding", "UTF-8",
                         src, "-output", dst],
                        check=True,
                        timeout=20,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                except FileNotFoundError:
                    return self._json(
                        {"error": "textutil not available (macOS only)"}, 500)
                except subprocess.TimeoutExpired:
                    return self._json({"error": "conversion timed out"}, 504)
                except subprocess.CalledProcessError as e:
                    msg = (e.stderr or b"").decode("utf-8", "replace") or str(e)
                    return self._json({"error": f"textutil failed: {msg}"}, 500)
                with open(dst, "r", encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            self._json({"ok": True, "text": text, "bytes": len(raw)})
        except Exception as e:
            self._json({"error": str(e)}, 500)

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

    # ── Toolsets API (mirrors nesq webui /api/tools/toolsets) ──
    def _collect_toolsets(self):
        """Return per-toolset info matching nesq shape."""
        from hermes_cli.tools_config import (
            _get_effective_configurable_toolsets,
            _get_platform_tools,
            _toolset_has_keys,
        )
        from toolsets import resolve_toolset
        from hermes_cli.config import load_config
        cfg = load_config()
        enabled = _get_platform_tools(cfg, "cli", include_default_mcp_servers=False)
        result = []
        for name, label, desc in _get_effective_configurable_toolsets():
            try:
                tools = sorted(set(resolve_toolset(name)))
            except Exception:
                tools = []
            is_enabled = name in enabled
            result.append({
                "name": name, "label": label, "description": desc,
                "enabled": is_enabled, "available": is_enabled,
                "configured": _toolset_has_keys(name, cfg),
                "tools": tools,
            })
        return result

    def _handle_toolsets(self):
        """GET /api/tools/toolsets — return per-toolset info matching nesq shape."""
        try:
            result = self._collect_toolsets()
            self._json(result)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_web_extract_status(self):
        """GET /api/tools/web-extract — optional Scrapling integration status."""
        toolsets = []
        toolset_error = None
        try:
            toolsets = self._collect_toolsets()
        except Exception as e:
            toolset_error = str(e)

        def mentions_scrapling(value):
            if value is None:
                return False
            return "scrapling" in str(value).lower()

        scrapling_toolsets = []
        for ts in toolsets:
            fields = [ts.get("name"), ts.get("label"), ts.get("description")]
            fields.extend(ts.get("tools") or [])
            if any(mentions_scrapling(field) for field in fields):
                scrapling_toolsets.append(ts)

        package_available = importlib.util.find_spec("scrapling") is not None
        cli_available = shutil.which("scrapling") is not None
        uvx_available = shutil.which("uvx") is not None
        enabled = any(ts.get("enabled") for ts in scrapling_toolsets)
        configured = bool(scrapling_toolsets)
        installed = package_available or cli_available
        self._json({
            "name": "scrapling",
            "label": "Web Extract",
            "description": "Optional Scrapling-powered web extraction through Hermes MCP tools.",
            "available": installed or enabled,
            "enabled": enabled,
            "configured": configured,
            "installed": installed,
            "package_available": package_available,
            "cli_available": cli_available,
            "uvx_available": uvx_available,
            "toolsets": scrapling_toolsets,
            "toolset_error": toolset_error,
            "install_hint": "uvx scrapling mcp",
            "docs_url": "https://github.com/D4Vinci/Scrapling",
        })

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

    def _handle_skill_delete(self):
        """POST /api/skills/delete — remove a skill directory tree."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json({"error": "Invalid JSON body"}, 400)
        name = (body.get("name") or "").strip()
        if not name or "/" in name or ".." in name:
            return self._json({"error": "Invalid skill name"}, 400)
        try:
            import shutil
            from tools.skills_tool import SKILLS_DIR
            target = None
            if SKILLS_DIR.exists():
                direct = SKILLS_DIR / name
                if direct.exists() and direct.is_dir():
                    target = direct
                else:
                    for child in SKILLS_DIR.iterdir():
                        if child.is_dir():
                            nested = child / name
                            if nested.exists() and nested.is_dir():
                                target = nested
                                break
            if target is None:
                return self._json({"error": "Skill not found", "name": name}, 404)
            target.resolve().relative_to(SKILLS_DIR.resolve())
            shutil.rmtree(target)
            self._json({"success": True, "name": name, "path": str(target)})
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

    # ── Delegation direct-chat API ─────────────────────────────────────────
    # Lets the UI talk directly to the delegation model (bypassing MiniMax /
    # the main agent loop). Reads whatever delegation.* is in config.yaml so
    # it works for any fork — OpenRouter Qwen, Together, Groq, etc.
    def _handle_version_info(self):
        """GET /api/version — return {current, latest, update_available, html_url}.

        Hits GitHub's releases/latest endpoint but caches the result for an
        hour so we're not rate-limited. Designed to fail soft: if the network
        call fails we still return the current version so the UI doesn't
        break, and `update_available` is False / latest is None.
        """
        import time as _time
        import urllib.request as _ur

        current = __version__
        latest = None
        html_url = None
        error = None

        try:
            now = _time.time()
            cache = _latest_release_cache
            if cache["data"] and (now - cache["ts"]) < 3600:
                payload = cache["data"]
            else:
                req = _ur.Request(
                    _GITHUB_RELEASES_API,
                    headers={
                        "User-Agent": f"hermes-ui/{current}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                with _ur.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                cache["ts"] = now
                cache["data"] = payload

            # tag_name is like "v3.1" — strip the leading "v" for comparison.
            tag = (payload or {}).get("tag_name") or ""
            latest = tag.lstrip("v") or None
            html_url = (payload or {}).get("html_url") or None
        except Exception as e:
            error = str(e)

        def _ver_tuple(v):
            try:
                return tuple(int(x) for x in str(v).split("."))
            except Exception:
                return ()

        update_available = False
        if latest:
            update_available = _ver_tuple(latest) > _ver_tuple(current)

        out = {
            "current": current,
            "latest": latest,
            "update_available": update_available,
            "html_url": html_url,
        }
        if error:
            out["error"] = error
        self._json(out)

    def _handle_delegation_info(self):
        """GET /api/delegation/info — return {configured, model, label}."""
        try:
            model, provider, base_url, api_key = _resolve_delegation_credentials()
            configured = bool(model and (api_key or provider))
            # Pretty label: "qwen/qwen3.6-plus" → "qwen3.6-plus"
            label = (model or "").split("/")[-1] if model else ""
            self._json({
                "configured": configured,
                "model": model or "",
                "provider": provider or "",
                "label": label,
            })
        except Exception as e:
            self._json({"configured": False, "error": str(e)})

    def _handle_delegation_chat(self):
        """POST /api/delegation/chat — proxy a chat completion to delegation model.

        Request:  {"messages": [{"role": "...", "content": "..."}, ...]}
        Response: {"reply": "..."} or {"error": "..."}
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b""
            data = json.loads(body) if body else {}
        except Exception as e:
            self._json({"error": f"invalid request body: {e}"}, 400)
            return

        messages = data.get("messages") or []
        if not isinstance(messages, list) or not messages:
            self._json({"error": "messages required (non-empty list)"}, 400)
            return

        model, provider, base_url, api_key = _resolve_delegation_credentials()
        if not model:
            self._json({"error": "No delegation model configured. Set delegation.model in ~/.hermes/config.yaml"}, 400)
            return
        if not api_key:
            self._json({"error": f"No API key resolved for delegation provider '{provider or 'unknown'}'. Check provider credentials."}, 400)
            return
        if not base_url:
            # Sensible fallback for common provider
            if provider == "openrouter":
                base_url = "https://openrouter.ai/api/v1"
            else:
                self._json({"error": f"No base_url for delegation provider '{provider}'"}, 400)
                return

        # OpenAI-compatible chat completions (covers OpenRouter, Together, Groq,
        # DeepSeek, Fireworks, Ollama, LiteLLM, and most self-hosted endpoints).
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": messages}

        try:
            import urllib.request as _ur
            req = _ur.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with _ur.urlopen(req, timeout=180) as resp:
                resp_body = resp.read().decode("utf-8", errors="replace")
            resp_data = json.loads(resp_body)
            reply = ""
            choices = resp_data.get("choices") or []
            if choices:
                msg = (choices[0] or {}).get("message") or {}
                reply = msg.get("content") or ""
            self._json({"reply": reply, "model": model})
        except Exception as e:
            self._json({"error": f"delegation call failed: {e}"}, 502)

    # ── Request routing ──
    def do_GET(self):
        if self.path == "/api/auth/status":
            self._auth_status()
        elif _auth_enabled() and self._route_requires_auth() and not self._is_authenticated():
            self._auth_required()
        elif self.path == "/health":
            self._handle_health()
        elif self.path.startswith("/api/chat/stream/status"):
            self._handle_chat_stream_status()
        elif self.path.startswith("/api/chat/stream"):
            self._handle_chat_stream()
        elif self.path.startswith("/api/chat/cancel"):
            self._handle_cancel()
        elif self.path == "/api/ui-conversations":
            self._conversations_load()
        elif self.path == "/api/workspaces":
            self._workspaces_load()
        elif self.path.startswith("/api/workspaces/browse"):
            self._workspace_browse()
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
        elif self.path == "/api/tools/toolsets" or self.path.startswith("/api/tools/toolsets?"):
            self._handle_toolsets()
        elif self.path == "/api/tools/web-extract" or self.path.startswith("/api/tools/web-extract?"):
            self._handle_web_extract_status()
        elif self.path == "/api/providers":
            self._handle_providers()
        elif self.path == "/api/models":
            self._handle_models()
        elif self.path == "/cron/list":
            self._handle_cron_list()
        elif self.path == "/api/delegation/info":
            self._handle_delegation_info()
        elif self.path == "/api/version":
            self._handle_version_info()
        else:
            super().do_GET()

    def do_POST(self):
        if self.path == "/api/auth/login":
            self._auth_login()
        elif self.path == "/api/auth/logout":
            self._auth_logout()
        elif _auth_enabled() and self._route_requires_auth() and not self._is_authenticated():
            self._auth_required()
        elif self.path == "/api/chat/start":
            self._handle_chat_start()
        elif self.path == "/api/chat/cancel":
            self._handle_cancel()
        elif self.path == "/api/chat/steer":
            self._handle_chat_steer()
        elif self.path == "/v1/chat/completions" or self.path == "/api/chat":
            self._handle_chat_start()  # backwards compat — same two-step flow
        elif self.path.startswith("/terminal/exec"):
            self._terminal_exec()
        elif self.path == "/api/ui-conversations":
            self._conversations_save()
        elif self.path == "/api/workspaces/add":
            self._workspace_add()
        elif self.path == "/api/workspaces/remove":
            self._workspace_remove()
        elif self.path == "/api/workspaces/rename":
            self._workspace_rename()
        elif self.path == "/api/workspaces/switch":
            self._workspace_switch()
        elif self.path.startswith("/writefile"):
            self._write_file()
        elif self.path == "/api/files/create":
            self._file_create()
        elif self.path == "/api/files/mkdir":
            self._file_mkdir()
        elif self.path == "/api/files/rename":
            self._file_rename()
        elif self.path == "/api/files/delete":
            self._file_delete()
        elif self.path == "/api/convert/rtf-to-txt":
            self._handle_rtf_to_txt()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        elif self.path == "/api/skills/save":
            self._handle_skill_save()
        elif self.path == "/api/skills/delete":
            self._handle_skill_delete()
        elif self.path.startswith("/server/pull-full-restart"):
            self._server_pull_full_restart()
        elif self.path.startswith("/server/full-restart"):
            self._server_full_restart()
        elif self.path.startswith("/server/pull-restart"):
            self._server_pull_restart()
        elif self.path.startswith("/server/restart"):
            self._server_restart()
        elif self.path == "/api/delegation/chat":
            self._handle_delegation_chat()
        elif self.path == "/api/providers":
            self._handle_provider_key_save()
        elif self.path == "/api/providers/delete":
            self._handle_provider_key_delete()
        else:
            self._json({"error": "Not found"}, 404)

    def do_PUT(self):
        if _auth_enabled() and self._route_requires_auth() and not self._is_authenticated():
            self._auth_required()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        else:
            self._json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Hermes-Session-Id")
        self.end_headers()

    def _read_json_body_optional(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return {}

    def _run_update_pulls(self):
        ui_dir = os.path.dirname(os.path.abspath(__file__))
        agent_dir = os.path.expanduser("~/.hermes/hermes-agent")
        outputs = []
        for name, d, args in [
            ("hermes-ui", ui_dir, ["pull", "--ff-only"]),
            ("hermes-agent", agent_dir, ["pull", "--rebase", "--autostash"]),
        ]:
            try:
                result = subprocess.run(
                    ["git", "-C", d] + args,
                    capture_output=True, text=True, timeout=60,
                )
                out = ((result.stdout or "") + (result.stderr or "")).strip() or "ok"
                if result.returncode != 0:
                    out = f"error (rc={result.returncode}): {out}"
                outputs.append(f"{name}: {out}")
            except Exception as e:
                outputs.append(f"{name}: error: {e}")
        return "\n\n".join(outputs)

    def _launch_full_restart(self):
        port = str(getattr(self.server, "server_port", PORT) or PORT)
        ui_dir = os.path.dirname(os.path.abspath(__file__))
        start_cmd = " ".join(shlex.quote(arg) for arg in ([sys.executable] + sys.argv))
        gateway_py = os.path.expanduser("~/.hermes/hermes-agent/venv/bin/python")
        if not os.path.exists(gateway_py):
            gateway_py = sys.executable
        script = f"""#!/bin/bash
set +e
sleep 0.5

if [ -f ~/.hermes/.env ]; then
  set -a
  source ~/.hermes/.env
  set +a
fi

pkill -TERM -f "python3 serve_lite.py" 2>/dev/null
sleep 2
lsof -ti:{shlex.quote(port)} | xargs kill -9 2>/dev/null
sleep 1

{shlex.quote(gateway_py)} -m hermes_cli.main gateway restart 2>&1 | tail -5
for i in $(seq 1 60); do
  if nc -z localhost 8642 2>/dev/null; then
    break
  fi
  sleep 0.5
done

cd {shlex.quote(ui_dir)}
nohup {start_cmd} > /tmp/hermes-ui.log 2>&1 &

for i in $(seq 1 60); do
  if nc -z localhost {shlex.quote(port)} 2>/dev/null; then
    break
  fi
  sleep 0.5
done
"""
        subprocess.Popen(
            ["bash", "-lc", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _server_restart(self):
        body = self._read_json_body_optional()
        if body.get("confirm") != "restart":
            return self._json({
                "ok": False,
                "error": "Restart requires confirmation from the Settings UI.",
            }, 400)
        self._json({"ok": True, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
            _flush_all_sessions()
            # Restart hermes-agent gateway too — the UI-side restart on its own
            # only re-execs serve_lite and leaves the launchd-managed gateway
            # (port 8642) running the old code.  `hermes gateway restart` asks
            # launchd to stop + start the service cleanly.  Fire-and-forget;
            # launchd handles the respawn while we re-exec below.
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as _e:
                print(f"[serve] gateway restart failed: {_e!r}", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()

    def _server_full_restart(self):
        body = self._read_json_body_optional()
        if body.get("confirm") != "full-restart":
            return self._json({
                "ok": False,
                "error": "Full restart requires confirmation from the Settings UI.",
            }, 400)
        self._json({"ok": True, "message": "Full restart starting..."})
        def _do_restart():
            _flush_all_sessions()
            self._launch_full_restart()
        threading.Thread(target=_do_restart, daemon=True).start()

    def _server_pull_restart(self):
        """Pull hermes-ui and hermes-agent, then restart both.
        Response includes the pull output so the UI can display it.

        hermes-ui is fast-forward only (user shouldn't have local commits
        there — it's a clone of our repo).  hermes-agent uses --rebase
        --autostash since the user may have local cherry-picks on top of
        upstream main; that matches how the repo is actually maintained.
        """
        body = self._read_json_body_optional()
        if body.get("confirm") != "update-restart":
            return self._json({
                "ok": False,
                "error": "Update and restart requires confirmation from the Settings UI.",
            }, 400)
        pull_output = self._run_update_pulls()
        self._json({"ok": True, "pull": pull_output, "message": "Restarting..."})
        def _do_restart():
            time.sleep(0.5)
            _flush_all_sessions()
            # Restart hermes-agent gateway via launchd so the code we just
            # pulled takes effect.  Fire-and-forget — launchd handles the
            # respawn while serve_lite re-execs itself below.
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "hermes_cli.main", "gateway", "restart"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            except Exception as _e:
                print(f"[serve] gateway restart failed: {_e!r}", flush=True)
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()

    def _server_pull_full_restart(self):
        body = self._read_json_body_optional()
        if body.get("confirm") != "update-full-restart":
            return self._json({
                "ok": False,
                "error": "Update and full restart requires confirmation from the Settings UI.",
            }, 400)
        pull_output = self._run_update_pulls()
        self._json({"ok": True, "pull": pull_output, "message": "Full restart starting..."})
        def _do_restart():
            _flush_all_sessions()
            self._launch_full_restart()
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
