#!/usr/bin/env python3
"""
Hermes UI — Lite Server v2
Talks directly to Hermes AIAgent (like the reference UI) — no gateway proxy.
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
import calendar
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
import re
import secrets
import tempfile
import uuid
import traceback
import urllib.parse
import mimetypes
from contextlib import contextmanager

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent
UPLOAD_MAX_BYTES = 750 * 1024 * 1024
UPLOAD_DIR_NAME = "uploads"

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
DEFAULT_HERMES_HOME = HERMES_HOME
AGENT_DIR = os.path.join(DEFAULT_HERMES_HOME, "hermes-agent")
DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 3333
WORKSPACES_FILE = pathlib.Path(HERMES_HOME) / "ui-workspaces.json"
LAST_WORKSPACE_FILE = pathlib.Path(HERMES_HOME) / "ui-last-workspace.txt"
AUTH_PASSWORD = os.environ.get("HERMES_UI_PASSWORD") or os.environ.get("HERMES_WEBUI_PASSWORD")
AUTH_COOKIE_NAME = "hermes_ui_auth"
AUTH_SECRET_FILE = pathlib.Path(HERMES_HOME) / "ui-auth-secret"

def _import_hermes_profiles():
    try:
        from hermes_cli import profiles as _profiles
        return _profiles
    except Exception as exc:
        print(f"[serve] WARNING: hermes_cli.profiles unavailable: {exc!r}", flush=True)
        return None

def _active_hermes_profile():
    profiles = _import_hermes_profiles()
    if profiles:
        try:
            return str(profiles.get_active_profile() or "default")
        except Exception as exc:
            print(f"[serve] WARNING: get_active_profile failed: {exc!r}", flush=True)
    return "default"

def _profile_home(profile_name=None):
    name = str(profile_name or _active_hermes_profile() or "default").strip() or "default"
    profiles = _import_hermes_profiles()
    if profiles:
        try:
            return pathlib.Path(profiles.resolve_profile_env(name)).expanduser(), name
        except Exception as exc:
            print(f"[serve] WARNING: resolve_profile_env({name!r}) failed: {exc!r}", flush=True)
            if name != "default":
                raise
    return pathlib.Path(DEFAULT_HERMES_HOME).expanduser(), "default"

def _read_yaml_file(path):
    try:
        import yaml
        p = pathlib.Path(path)
        if p.exists():
            return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        print(f"[serve] WARNING: failed to read {path}: {exc}", flush=True)
    return {}

def _load_profile_env_values(profile_name=None):
    try:
        home, _ = _profile_home(profile_name)
    except Exception:
        home = pathlib.Path(DEFAULT_HERMES_HOME)
    env_path = home / ".env"
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

def _apply_active_profile_environment(profile_name=None):
    """Make this local UI process follow the same active profile as Hermes CLI."""
    home, name = _profile_home(profile_name)
    env_values = _load_profile_env_values(name)
    with _ENV_LOCK:
        os.environ["HERMES_HOME"] = str(home)
        for key, value in env_values.items():
            if key:
                os.environ[key] = value
    return str(home), name

@contextmanager
def _profile_env_context(profile_name=None):
    """Temporarily expose a profile's HERMES_HOME/.env to Hermes runtime helpers."""
    home, name = _profile_home(profile_name)
    env_values = _load_profile_env_values(name)
    with _ENV_LOCK:
        old_home = os.environ.get("HERMES_HOME")
        old_values = {key: os.environ.get(key) for key in env_values}
        os.environ["HERMES_HOME"] = str(home)
        for key, value in env_values.items():
            if key:
                os.environ[key] = value
    try:
        yield str(home), name
    finally:
        with _ENV_LOCK:
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

def _list_hermes_profiles_payload():
    profiles_mod = _import_hermes_profiles()
    active = _active_hermes_profile()
    items = []
    if profiles_mod:
        try:
            for item in profiles_mod.list_profiles():
                data = {
                    "name": str(getattr(item, "name", "") or "default"),
                    "path": str(getattr(item, "path", "") or ""),
                    "is_default": bool(getattr(item, "is_default", False)),
                    "is_active": bool(getattr(item, "is_active", False)),
                    "gateway_running": bool(getattr(item, "gateway_running", False)),
                    "model": str(getattr(item, "model", "") or ""),
                    "provider": str(getattr(item, "provider", "") or ""),
                    "has_env": bool(getattr(item, "has_env", False)),
                    "skill_count": int(getattr(item, "skill_count", 0) or 0),
                }
                if data["name"] == active:
                    data["is_active"] = True
                items.append(data)
        except Exception as exc:
            print(f"[serve] WARNING: list_profiles failed: {exc!r}", flush=True)
    if not items:
        home = pathlib.Path(DEFAULT_HERMES_HOME)
        cfg = _read_yaml_file(home / "config.yaml")
        model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
        items.append({
            "name": "default",
            "path": str(home),
            "is_default": True,
            "is_active": active == "default",
            "gateway_running": False,
            "model": str(model_cfg.get("default") or ""),
            "provider": str(model_cfg.get("provider") or ""),
            "has_env": (home / ".env").exists(),
            "skill_count": 0,
        })
        active = "default"
    active_item = next((p for p in items if p.get("name") == active), items[0])
    return {
        "ok": True,
        "active": active_item.get("name") or active,
        "profiles": items,
        "default_model": active_item.get("model") or "",
        "default_model_provider": active_item.get("provider") or "",
    }

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
# GitHub. Keep in sync with the git tag (e.g. "3.3" corresponds to v3.3).
__version__ = "3.3.20"
_GITHUB_RELEASES_API = "https://api.github.com/repos/pyrate-llama/hermes-ui/releases/latest"
_HERMES_AGENT_RELEASES_API = "https://api.github.com/repos/NousResearch/hermes-agent/releases/latest"

# Cache for the latest-release lookup so we don't hammer GitHub. Stores
# (timestamp, payload_dict). TTL of 1 hour is plenty for an update-nag.
_latest_release_cache = {"ts": 0.0, "data": None}
_agent_release_cache = {"ts": 0.0, "data": None}

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

_PROVIDER_MODELS = {
    "anthropic": [
        {"id": "claude-opus-4-6",            "label": "Claude Opus 4.6"},
        {"id": "claude-sonnet-4-6",          "label": "Claude Sonnet 4.6"},
        {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5"},
    ],
    "openai": [
        {"id": "gpt-4.1",      "label": "GPT-4.1"},
        {"id": "gpt-4.1-mini", "label": "GPT-4.1 Mini"},
        {"id": "gpt-4.1-nano", "label": "GPT-4.1 Nano"},
        {"id": "o3",           "label": "o3"},
        {"id": "o4-mini",      "label": "o4 Mini"},
    ],
    "google": [
        {"id": "gemini-2.5-pro",   "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    ],
    "gemini": [
        {"id": "gemini-2.5-pro",   "label": "Gemini 2.5 Pro"},
        {"id": "gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    ],
    "deepseek": [
        {"id": "deepseek-chat",     "label": "DeepSeek Chat"},
        {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner"},
    ],
    "x-ai": [
        {"id": "grok-3",      "label": "Grok 3"},
        {"id": "grok-3-mini", "label": "Grok 3 Mini"},
    ],
    "minimax": [
        {"id": "MiniMax-M1",  "label": "MiniMax M1"},
        {"id": "MiniMax-M2.7","label": "MiniMax M2.7"},
    ],
    "mistralai": [
        {"id": "mistral-large-latest", "label": "Mistral Large"},
        {"id": "codestral-latest",     "label": "Codestral"},
    ],
    "openrouter": [
        {"id": "anthropic/claude-sonnet-4-6", "label": "Claude Sonnet 4.6"},
        {"id": "openai/gpt-4.1",              "label": "GPT-4.1"},
        {"id": "google/gemini-2.5-pro",       "label": "Gemini 2.5 Pro"},
        {"id": "deepseek/deepseek-chat",      "label": "DeepSeek Chat"},
        {"id": "x-ai/grok-3",                 "label": "Grok 3"},
    ],
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


def _resolve_model_and_credentials(model_override=None, profile_name=None):
    """Read model/provider from config.yaml and resolve API credentials."""
    profile_home, active_profile = _profile_home(profile_name)
    config_path = profile_home / "config.yaml"
    model = "MiniMax-M2.7"
    provider = None
    base_url = None

    if config_path.exists():
        try:
            cfg = _read_yaml_file(config_path)
            model_cfg = cfg.get("model", {})
            model = model_cfg.get("default", model)
            provider = model_cfg.get("provider")
            base_url = model_cfg.get("base_url")
        except Exception as e:
            print(f"[serve] WARNING: Failed to read config.yaml for profile {active_profile}: {e}", flush=True)

    # Use Hermes runtime provider to resolve API key
    api_key = None
    with _profile_env_context(active_profile):
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


def _configured_model_options(current_model=None, provider=None):
    """Return model choices configured for the UI model switcher."""
    raw = (
        os.environ.get("HERMES_UI_MODELS")
        or os.environ.get("HERMES_MODEL_OPTIONS")
        or os.environ.get("HERMES_MODELS")
        or ""
    )
    def clean_model_id(value):
        value = str(value or "").strip()
        if value.lower().startswith("gtp-"):
            value = "gpt-" + value[4:]
        return value

    items = []
    seen = set()
    for part in raw.replace("\n", ",").split(","):
        value = clean_model_id(part)
        key = value.lower()
        if value and key not in seen:
            items.append(value)
            seen.add(key)
    # If no explicit config, add curated models for the active provider
    if not items and provider:
        provider_key = str(provider).strip().lower()
        curated = _PROVIDER_MODELS.get(provider_key, [])
        for entry in curated:
            # Support both dict {"id":..., "label":...} and bare string entries
            m = clean_model_id(entry["id"] if isinstance(entry, dict) else entry)
            key = m.lower()
            if m and key not in seen:
                items.append(m)
                seen.add(key)
    current = clean_model_id(current_model)
    if current and current.lower() not in seen:
        items.insert(0, current)
    return items


def _skill_summary_from_file(path):
    """Return lightweight skill metadata from a SKILL.md file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read(4096)
    except Exception:
        return {}
    meta = {}
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            for line in text[3:end].splitlines():
                if ":" not in line:
                    continue
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")
                if key in ("name", "description") and value:
                    meta[key] = value
    if not meta.get("description"):
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#") and line != "---":
                meta["description"] = line[:220]
                break
    return meta


def _discover_codex_plugin_skills():
    """Add Codex/plugin skill entries so slash commands see installed plugin skills."""
    roots = [
        os.path.expanduser("~/.codex/plugins/cache"),
        os.path.expanduser("~/.codex/skills"),
        os.path.expanduser("~/.agents/skills"),
    ]
    skills = []
    seen = set()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, filenames in os.walk(root):
            if "SKILL.md" not in filenames:
                continue
            path = os.path.join(dirpath, "SKILL.md")
            meta = _skill_summary_from_file(path)
            name = meta.get("name") or os.path.basename(dirpath)
            if "/plugins/cache/" in path:
                parts = path.split(os.sep)
                try:
                    cache_idx = parts.index("cache")
                    plugin = parts[cache_idx + 2] if parts[cache_idx + 1].startswith("openai-") else parts[cache_idx + 1]
                    if plugin and not str(name).startswith(f"{plugin}:"):
                        name = f"{plugin}:{name}"
                except Exception:
                    pass
            key = str(name).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            skills.append({
                "name": str(name).strip(),
                "description": meta.get("description", ""),
                "path": path,
                "source": "codex-plugin" if "/plugins/cache/" in path else "codex-skill",
            })
    return skills

def _infer_model_provider(model_id, fallback_provider=None):
    model = str(model_id or "").strip()
    if "/" in model:
        candidate = model.split("/", 1)[0].strip().lower()
        if candidate:
            return candidate
    return str(fallback_provider or "").strip().lower()

def _model_context_hint(model_id):
    name = str(model_id or "").lower()
    if any(s in name for s in ("gpt-5", "claude-opus-4", "claude-sonnet-4", "gemini-2.5")):
        return "large"
    if any(s in name for s in ("minimax", "qwen3", "glm-4.6", "kimi", "deepseek")):
        return "large"
    if any(s in name for s in ("gpt-4o", "gpt-4.1", "claude-3", "gemini-1.5")):
        return "128k+"
    return ""

def _get_installed_agent_version():
    try:
        import importlib.metadata as _md
        return _md.version("hermes-agent")
    except Exception:
        pass
    try:
        import tomllib
        pyproject = pathlib.Path(AGENT_DIR) / "pyproject.toml"
        if pyproject.exists():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            return str((data.get("project") or {}).get("version") or "")
    except Exception:
        pass
    return ""


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
    return _load_profile_env_values()

def _write_env_update(env_var, value):
    profile_home, _ = _profile_home()
    env_path = profile_home / ".env"
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

def _read_provider_config_status(profile_name=None):
    cfg = {}
    try:
        profile_home, active_profile = _profile_home(profile_name)
    except Exception:
        profile_home, active_profile = pathlib.Path(DEFAULT_HERMES_HOME), "default"
    cfg_path = profile_home / "config.yaml"
    if cfg_path.exists():
        cfg = _read_yaml_file(cfg_path)

    model_cfg = cfg.get("model", {}) if isinstance(cfg.get("model"), dict) else {}
    providers_cfg = cfg.get("providers", {}) if isinstance(cfg.get("providers"), dict) else {}
    active_provider = str(model_cfg.get("provider") or "").strip().lower()
    env_values = _load_profile_env_values(active_profile)
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
        "profile": active_profile,
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


# ── API-safe message sanitization (ported from the reference UI) ──────
# Matches api/streaming.py: _API_SAFE_MSG_KEYS, _sanitize_messages_for_api,
# _restore_reasoning_metadata. Keeps tool_calls/tool_call_id intact so weak
# tool-callers (MiniMax) keep seeing real tool-use precedent in history.
_API_SAFE_MSG_KEYS = {"role", "content", "tool_calls", "tool_call_id", "name", "refusal"}
_FALLBACK_CONTEXT_MESSAGE_LIMIT = 80
_MODEL_CONTEXT_MESSAGE_LIMIT = 60
_MODEL_CONTEXT_CHAR_LIMIT = 35000
_FULL_TRANSCRIPT_USER_CONTEXT_LIMIT = 40
_FULL_TRANSCRIPT_CHAR_LIMIT = 115000
_CONTEXT_TOOL_CONTENT_LIMIT = 2400
_CONTEXT_ASSISTANT_CONTENT_LIMIT = 12000


def _is_cancellation_marker_message(msg):
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    text = " ".join(str(_message_text(msg.get("content", "")) or "").split()).strip().lower()
    return text in ("*task cancelled.*", "task cancelled.")


def _drop_cancellation_marker_messages(messages):
    return [
        msg for msg in (messages or [])
        if not _is_cancellation_marker_message(msg)
    ]


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
        if msg.get("role") not in ("system", "user", "assistant", "tool"):
            continue
        # Skip persisted error markers — never send them to the LLM as prior
        # context. Matches reference _sanitize_messages_for_api (closes error-loop
        # feedback where a failed turn would be replayed to the model).
        if msg.get("_error") or _is_cancellation_marker_message(msg):
            continue
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id") or ""
            if not tid or tid not in valid_tool_call_ids:
                continue  # orphaned tool result — drop
        sanitized = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
        if sanitized.get("role"):
            clean.append(sanitized)
    return clean


def _limited_fallback_history(messages):
    """Last-resort browser-history repair, capped to avoid huge prompt bloat."""
    clean = _sanitize_messages_for_api(messages)
    if len(clean) <= _FALLBACK_CONTEXT_MESSAGE_LIMIT:
        return clean
    return _sanitize_messages_for_api(clean[-_FALLBACK_CONTEXT_MESSAGE_LIMIT:])


def _message_context_size(msg):
    if not isinstance(msg, dict):
        return 0
    try:
        return len(json.dumps(msg.get("content", ""), ensure_ascii=False))
    except Exception:
        return len(str(msg.get("content", "")))


def _truncate_context_content(content, limit):
    if limit <= 0:
        return content
    if isinstance(content, str):
        if len(content) <= limit:
            return content
        return (
            content[:limit]
            + f"\n\n[...truncated {len(content) - limit} chars for model context; full text remains in visible transcript...]"
        )
    if isinstance(content, list):
        out = []
        remaining = limit
        for item in content:
            if remaining <= 0:
                out.append({
                    "type": "text",
                    "text": "[...additional content truncated for model context; full text remains in visible transcript...]",
                })
                break
            if isinstance(item, dict):
                next_item = dict(item)
                text = next_item.get("text") or next_item.get("content")
                if isinstance(text, str) and len(text) > remaining:
                    truncated = (
                        text[:remaining]
                        + f"\n\n[...truncated {len(text) - remaining} chars for model context; full text remains in visible transcript...]"
                    )
                    if "text" in next_item:
                        next_item["text"] = truncated
                    else:
                        next_item["content"] = truncated
                    out.append(next_item)
                    remaining = 0
                    continue
                if isinstance(text, str):
                    remaining -= len(text)
                out.append(next_item)
            else:
                s = str(item)
                if len(s) > remaining:
                    out.append(s[:remaining] + "\n\n[...truncated for model context...]")
                    remaining = 0
                else:
                    out.append(item)
                    remaining -= len(s)
        return out
    return content


def _compact_message_for_full_context(msg):
    if not isinstance(msg, dict):
        return None
    clean = {k: v for k, v in msg.items() if k in _API_SAFE_MSG_KEYS}
    role = clean.get("role")
    if not role:
        return None
    content = clean.get("content", "")
    if role == "tool":
        clean["content"] = _truncate_context_content(content, _CONTEXT_TOOL_CONTENT_LIMIT)
    elif role == "assistant":
        text = _message_text(content)
        if _is_recovered_ui_tool_receipt(clean):
            clean["content"] = _truncate_context_content(content, _CONTEXT_TOOL_CONTENT_LIMIT)
        elif len(text) > _CONTEXT_ASSISTANT_CONTENT_LIMIT:
            clean["content"] = _truncate_context_content(content, _CONTEXT_ASSISTANT_CONTENT_LIMIT)
    return clean


def _full_transcript_context(messages, current_user_msg=""):
    """Reference-style: prefer full backend transcript when small enough.

    Heavy tool/output blobs are compacted, but user/assistant turn continuity is
    preserved. This is not compaction; it is provider-safe projection.
    """
    cleaned = _remove_promise_only_tail(_remove_contradicted_tool_denials(messages or []))
    cleaned = _drop_checkpointed_current_user_from_context(cleaned, current_user_msg)
    compacted = []
    valid_tool_call_ids = set()
    for raw in cleaned:
        msg = _compact_message_for_full_context(raw)
        if not msg:
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tid = tc.get("id") or tc.get("call_id") or ""
                    if tid:
                        valid_tool_call_ids.add(tid)
        compacted.append(msg)

    safe = []
    for msg in compacted:
        if msg.get("role") == "tool":
            tid = msg.get("tool_call_id") or ""
            if not tid or tid not in valid_tool_call_ids:
                continue
        safe.append(msg)
    return _sanitize_messages_for_api(safe)


def _context_char_size(messages):
    return sum(_message_context_size(msg) for msg in (messages or []))


def _should_use_full_transcript_context(messages):
    user_count = _count_role_messages(messages, "user")
    compacted_size = 0
    for raw in _remove_contradicted_tool_denials(messages or []):
        msg = _compact_message_for_full_context(raw)
        if msg:
            compacted_size += _message_context_size(msg)
    return (
        user_count <= _FULL_TRANSCRIPT_USER_CONTEXT_LIMIT
        and compacted_size <= _MODEL_CONTEXT_CHAR_LIMIT
    )


def _recent_visible_turns_context(
    messages,
    limit=14,
    max_chars=520,
    min_user_turns=5,
):
    """Compact UI turns while always preserving the latest user instructions.

    Tool-heavy or crash-recovery turns can create many assistant/status rows
    after the user's actual steering instruction.  A raw tail window then shows
    stale assistant chatter but drops the current task.  Keep the normal compact
    tail, but splice in the last few user turns even when assistant rows would
    otherwise push them out.
    """
    turns = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        if role == "assistant" and _is_recovered_ui_tool_receipt(msg):
            continue
        text = _strip_workspace_prefix(
            _message_text(msg.get("content") or ""),
            include_legacy=True,
        )
        text = " ".join((text or "").split())
        if not text:
            continue
        if role == "assistant" and _is_cancellation_marker_message(msg):
            continue
        if len(text) > max_chars:
            head_len = max(120, max_chars // 2)
            tail_len = max(120, max_chars - head_len)
            text = (
                text[:head_len].rstrip()
                + " ... "
                + text[-tail_len:].lstrip()
            )
        turns.append((role, text))
    if not turns:
        return ""
    limit = max(1, int(limit or 1))
    min_user_turns = max(0, int(min_user_turns or 0))
    selected_by_idx = {}
    tail_start = max(0, len(turns) - limit)
    for idx in range(tail_start, len(turns)):
        selected_by_idx[idx] = turns[idx]
    if min_user_turns:
        user_idxs = [
            idx for idx, (role, _text) in enumerate(turns)
            if role == "user"
        ]
        for idx in user_idxs[-min_user_turns:]:
            selected_by_idx[idx] = turns[idx]
    selected = [selected_by_idx[idx] for idx in sorted(selected_by_idx)]
    lines = [
        f"{idx}. {role}: {text}"
        for idx, (role, text) in enumerate(selected, 1)
    ]
    return "Recent visible turns:\n" + "\n".join(lines) + "\n"


def _message_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content or "")


def _strip_workspace_prefix(text, include_legacy=True):
    """Remove WebUI workspace/count prefixes when comparing visible turns."""
    s = str(text or "")
    if include_legacy:
        s = re.sub(r"^\[Workspace:[^\n]*\]\n", "", s)
    s = re.sub(r"^\[Workspace::v1:[^\n]*\]\n", "", s)
    s = re.sub(r"^\[Visible transcript:[^\n]*\]\n", "", s)
    return s


def _message_identity(msg):
    if not isinstance(msg, dict):
        return None
    role = str(msg.get("role") or "")
    text = _message_text(msg.get("content", ""))
    if role == "user":
        # Agent results can echo the workspace/count-prefixed prompt while the
        # visible UI bubble stores only the human text. Treat them as the same
        # turn for dedupe and compaction merge, matching the reference UI.
        text = _strip_workspace_prefix(text, include_legacy=True)
    if not text and not msg.get("tool_call_id") and not msg.get("tool_calls"):
        return None
    return (
        role,
        " ".join(str(text or "").split())[:500],
        str(msg.get("tool_call_id") or ""),
        json.dumps(msg.get("tool_calls") or [], sort_keys=True, ensure_ascii=False),
    )


def _messages_have_prefix(messages, prefix):
    if len(messages or []) < len(prefix or []):
        return False
    for idx, expected in enumerate(prefix or []):
        if _message_identity((messages or [])[idx]) != _message_identity(expected):
            return False
    return True


def _is_context_compression_marker(msg):
    """True only for the structural compaction-summary message the agent
    injects, not for any message that happens to *mention* compaction.

    The agent emits the marker as a user-role message whose content
    begins with ``[CONTEXT COMPACTION`` (or the legacy
    ``[CONTEXT COMPRESSION``).  The previous lenient substring match
    fired on tool results that happened to contain the phrase "active
    task list was preserved across context compression" inside their
    payload, and on assistant explanations of how compaction works —
    causing the model-facing context to be trimmed from a false-positive
    deep in the chat.  Symptom: an old explanation of compaction at
    position 597 became the "most recent marker" and we returned only
    the last 66 messages, dropping ~400 turns of real conversation.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "user":
        return False
    text = _message_text(msg.get("content", "")).lstrip()
    if not text:
        return False
    head = text[:32].lower()
    return head.startswith("[context compaction") or head.startswith("[context compression")


def _messages_have_context_compression_marker(messages):
    return any(_is_context_compression_marker(msg) for msg in (messages or []))


def _context_messages_for_session(session):
    """Return model-facing history with self-healing damage recovery.

    This is the fifth revision.  Read carefully before changing.

    Lifecycle the function targets:

      A. Fresh chat (no compaction yet, no context_messages saved):
         return ``messages`` as-is.  Small chats stay cheap.

      B. Healthy mid-chat (one compaction has happened, agent has been
         appending normally): trust ``context_messages`` (the agent's
         own compacted view) and backfill any display tail that landed
         after the last save — this preserves the per-turn perf win
         AND catches any save-path drift.

      C. Damaged chat (the ``0b3c8d8`` rachet — every turn re-derived
         context from display, agent recompacted every turn, leaving
         ``context_messages`` chopped to a tiny fraction of the
         post-compaction history): return ``display[last_marker:]``
         instead so the next turn rebuilds.  Agent will compact once
         (slow recovery turn), produce a clean compacted view, save
         it; subsequent turns return to the healthy path B.

      D. Healthy compacted-twice chat (agent ran compaction a second
         time mid-life; ``context_messages`` legitimately has a
         different/newer marker than ``display`` and a smaller user
         count): trust context.  Avoid forcing a re-compaction.

    Damage detection (heuristics, both required to be NOT triggered for
    context to be trusted):

      H1. ``context_messages`` contains MORE than one compaction marker.
          A healthy compacted view holds exactly one — the most recent
          summary covers everything before it.  Multiple markers are a
          fingerprint of the rachet (agent compacted N times, each save
          appended a new marker, none were collapsed).

      H2. ``context_messages`` has substantially fewer user prompts
          than ``display[last_marker:]`` AND its only marker matches
          display's latest marker.  That means no new compaction
          happened — context should have at least the post-marker user
          turns from display, but it's been trimmed.

    Either failure → return ``display[last_marker:]`` (the canonical
    floor) and let the agent re-compact properly next turn.  Otherwise
    trust ``context_messages`` and backfill its tail from display.

    Strict marker detector (``_is_context_compression_marker``) only
    matches role=user content starting with ``[CONTEXT COMPACTION`` /
    legacy ``[CONTEXT COMPRESSION``, so tool results / assistant
    explanations of compaction don't cause false positives.
    """
    if not isinstance(session, dict):
        return []
    display = session.get("messages") or []
    if not display:
        ctx0 = session.get("context_messages")
        return list(ctx0) if isinstance(ctx0, list) else []
    context = session.get("context_messages")

    # Locate the most recent real compaction marker in display.
    display_last_marker_idx = -1
    for i in range(len(display) - 1, -1, -1):
        if _is_context_compression_marker(display[i]):
            display_last_marker_idx = i
            break
    floor = (
        display[display_last_marker_idx:]
        if display_last_marker_idx >= 0
        else list(display)
    )
    floor_user_count = _count_role_messages(floor, "user")

    # Case A: no saved context → return floor.  Small chats: floor is
    # the whole transcript; agent doesn't have to compact unless real
    # threshold hit.
    if not isinstance(context, list) or not context:
        return list(floor)

    # Damage detection.  Distinguish two states:
    #
    #   (1) "Agent has compacted further" — context contains a marker
    #       whose content is DIFFERENT from display's most recent
    #       marker.  This is a fresh agent-generated summary, expected
    #       to be small.  Trust it.
    #
    #   (2) "Context is stale / chopped without a new compaction" —
    #       context's last marker matches display's last marker (or
    #       there's no marker on either side) AND context has fewer
    #       user prompts than the post-marker floor.  This means the
    #       previous turn(s) lost data without legitimate compression
    #       to justify it.  Rebuild from floor.
    #
    # The earlier "more than one marker → damaged" heuristic was wrong:
    # the agent legitimately preserves the prior marker in its output
    # while adding a new larger summary, so a fresh compaction lands as
    # TWO markers (old + new).  Counting markers can't distinguish that
    # from rachet damage.  Comparing content does.
    def _last_marker_content(messages):
        # Compare the WHOLE marker body, normalised for whitespace.
        # Truncated comparisons (we tried 300 chars) match on the
        # boilerplate prefix that every marker shares (`[CONTEXT
        # COMPACTION — REFERENCE ONLY] Earlier turns were compacted
        # into the summary below ...`) and miss the actual summary
        # content where the markers differ.
        for m in reversed(messages):
            if _is_context_compression_marker(m):
                return " ".join(
                    _message_text(m.get("content") or "").split()
                )
        return None

    ctx_last_marker_content = _last_marker_content(context)
    display_last_marker_content = (
        _last_marker_content(display) if display_last_marker_idx >= 0 else None
    )
    has_new_compaction = (
        ctx_last_marker_content is not None
        and ctx_last_marker_content != display_last_marker_content
    )

    ctx_user_count = _count_role_messages(context, "user")

    if has_new_compaction:
        # Agent compacted further; small context is legitimate.  Trust
        # it and backfill any display tail that landed after the save.
        pass
    elif (
        floor_user_count > 0
        and ctx_user_count + 2 < floor_user_count
    ):
        # No new compaction explains why context is smaller than the
        # post-marker floor — it's been chopped.  Rebuild.
        print(
            f"[serve] context_messages rebuild: "
            f"under-filled without new compaction "
            f"(ctx {ctx_user_count} users vs floor {floor_user_count}); "
            f"reverting to display[last_marker:] = {len(floor)} msgs",
            flush=True,
        )
        return list(floor)

    # Context looks healthy.  Hybrid: trust it, backfill display tail
    # so any post-save in-flight turns aren't lost.
    tail_msg = None
    for m in reversed(context):
        if isinstance(m, dict):
            tail_msg = m
            break
    if tail_msg is None:
        return list(context)
    tail_key = _message_identity(tail_msg)
    if tail_key is None:
        return list(context)
    anchor_idx = None
    for di in range(len(display) - 1, -1, -1):
        if _message_identity(display[di]) == tail_key:
            anchor_idx = di
            break
    if anchor_idx is None:
        return list(context)
    new_tail = display[anchor_idx + 1:]
    if not new_tail:
        return list(context)
    return list(context) + list(new_tail)


def _find_current_user_turn(messages, msg_text):
    needle = " ".join(str(msg_text or "").split())
    fallback = None
    for idx, msg in enumerate(messages or []):
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        fallback = idx
        text = " ".join(
            _strip_workspace_prefix(
                _message_text(msg.get("content", "")),
                include_legacy=True,
            ).split()
        )
        if needle and (needle in text or text in needle):
            return idx
    return fallback


def _drop_checkpointed_current_user_from_context(messages, msg_text):
    """Return model history without an eager-checkpointed current user turn."""
    history = list(messages or [])
    if not history:
        return history
    current_user_key = _message_identity({"role": "user", "content": msg_text})
    if current_user_key and _message_identity(history[-1]) == current_user_key:
        return history[:-1]
    return history


def _merge_display_messages_after_agent_result(previous_display, previous_context, result_messages, msg_text):
    """Keep the full visible chat while model-facing context stays compact."""
    previous_display = list(previous_display or [])
    previous_context = list(previous_context or [])
    result_messages = list(result_messages or [])
    if not result_messages:
        return previous_display

    if _messages_have_prefix(result_messages, previous_context):
        candidates = result_messages[len(previous_context):]
    else:
        current_user_idx = _find_current_user_turn(result_messages, msg_text)
        marker_candidates = [
            m for m in result_messages[:current_user_idx if current_user_idx is not None else len(result_messages)]
            if _is_context_compression_marker(m)
        ]
        turn_candidates = result_messages[current_user_idx:] if current_user_idx is not None else []
        candidates = marker_candidates + turn_candidates

    merged = list(previous_display)
    seen = {_message_identity(m) for m in merged}
    current_user_key = _message_identity({"role": "user", "content": msg_text})
    current_user_in_candidates = any(
        _message_identity(m) == current_user_key
        for m in candidates
    )
    current_user_already_checkpointed = bool(
        merged and _message_identity(merged[-1]) == current_user_key
    )
    if (
        current_user_key is not None
        and not current_user_in_candidates
        and not current_user_already_checkpointed
        and any(isinstance(m, dict) and m.get("role") in ("assistant", "tool") for m in candidates)
    ):
        current_user_msg = {"role": "user", "content": msg_text}
        insert_at = 0
        while insert_at < len(candidates) and _is_context_compression_marker(candidates[insert_at]):
            insert_at += 1
        candidates = candidates[:insert_at] + [current_user_msg] + candidates[insert_at:]

    for msg in candidates:
        ident = _message_identity(msg)
        if ident is None:
            continue
        if ident == current_user_key and merged and _message_identity(merged[-1]) == ident:
            continue
        if ident in seen:
            continue
        if _is_context_compression_marker(msg) and ident in seen:
            continue
        display_msg = msg
        if ident == current_user_key and isinstance(msg, dict) and msg.get("role") == "user":
            display_msg = dict(msg)
            display_msg["content"] = msg_text
        merged.append(display_msg)
        seen.add(ident)
    return merged


def _trim_model_history(messages):
    """Bound model-facing history while preserving the visible transcript separately."""
    clean = _sanitize_messages_for_api(messages)
    if not clean:
        return []
    kept = []
    total_chars = 0
    for msg in reversed(clean):
        size = _message_context_size(msg)
        if kept and (len(kept) >= _MODEL_CONTEXT_MESSAGE_LIMIT or total_chars + size > _MODEL_CONTEXT_CHAR_LIMIT):
            break
        kept.append(msg)
        total_chars += size
    return _sanitize_messages_for_api(list(reversed(kept)))


def _provider_history_from_transcript(messages, current_user_msg=""):
    """Return provider-safe history without silently compacting the transcript.

    The visible transcript remains complete in session["messages"]. This
    projection is only the model-facing payload, so it must stay bounded even
    when a repaired/compacted session has accumulated very large summaries or
    tool output.
    """
    if _should_use_full_transcript_context(messages or []):
        return _full_transcript_context(messages or [], current_user_msg)
    return _trim_model_history(_full_transcript_context(messages or [], current_user_msg))


def _messages_have_tool_evidence(messages):
    return any(
        isinstance(msg, dict)
        and (
            msg.get("role") == "tool"
            or msg.get("tool_calls")
            or msg.get("toolCalls")
        )
        for msg in (messages or [])
    )


def _format_ui_tool_evidence(tool_calls):
    parts = []
    for tc in (tool_calls or [])[:12]:
        if not isinstance(tc, dict):
            continue
        label = str(tc.get("label") or tc.get("toolName") or "tool")[:160]
        status = "done" if tc.get("done") else "running"
        result = tc.get("result")
        result_text = ""
        if result is not None:
            try:
                result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
            except Exception:
                result_text = str(result)
            result_text = " => " + result_text[:500]
        parts.append(f"{label} {status}{result_text}")
    if not parts:
        return ""
    return "[Hermes UI tool evidence: " + "; ".join(parts) + "]"


def _ui_tool_call_name(tool_call):
    raw = str(
        (tool_call or {}).get("toolName")
        or (tool_call or {}).get("label")
        or "hermes_ui_tool"
    )
    name = re.sub(r"[^A-Za-z0-9_]+", "_", raw).strip("_")[:64]
    if not name:
        name = "hermes_ui_tool"
    if not re.match(r"^[A-Za-z_]", name):
        name = "tool_" + name
    return name


def _recovered_ui_tool_receipt_message(tool_calls):
    receipts = []
    for tc in (tool_calls or [])[:12]:
        if not isinstance(tc, dict):
            continue
        # UI toolCalls are generated from stream tool events, but they are
        # display receipts, not provider-native assistant/tool message pairs.
        # Keep provenance explicit so we do not forge tool history.
        if not (tc.get("timestamp") and tc.get("toolName")):
            continue
        label = str(tc.get("label") or tc.get("toolName") or "tool")[:220]
        status = "done" if tc.get("done") else "running"
        duration = tc.get("duration")
        duration_text = f", duration={duration:.2f}s" if isinstance(duration, (int, float)) else ""
        result_note = "result_present" if tc.get("result") is not None else "result_not_captured"
        receipts.append(
            f"{tc.get('timestamp')} | {tc.get('toolName')} | {label} | {status}{duration_text} | {result_note}"
        )
    if not receipts:
        return None
    return {
        "role": "assistant",
        "content": (
            "[Recovered UI-observed tool receipts; not provider-native tool_calls. "
            "These mean Hermes UI observed tool events earlier. If result_not_captured, "
            "verify the artifact/status with tools before claiming final state.]\n"
            + "\n".join(receipts)
        ),
    }


def _enrich_messages_with_ui_tool_evidence(messages):
    """Convert browser-only toolCalls receipts into provenance-labeled context."""
    enriched = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        enriched.append(msg)
        receipt_msg = _recovered_ui_tool_receipt_message(msg.get("toolCalls"))
        if receipt_msg:
            enriched.append(receipt_msg)
    return enriched


def _assistant_denies_prior_tool_work(msg):
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    if msg.get("tool_calls"):
        return False
    text = " ".join(str(msg.get("content") or "").lower().split())
    if not text:
        return False
    return bool(re.search(
        r"(didn['’]?t actually run any tools|didn['’]?t run any tools|"
        r"haven['’]?t actually started production|hadn['’]?t actually started|"
        r"just confirming the schedule in my head|got ahead of myself|"
        r"need the topic first|what is episode\s*013 about)",
        text,
    ))


def _is_old_recovered_ui_tool_note(msg):
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    text = str(msg.get("content") or "")
    return (
        text.startswith("[Hermes UI tool evidence:")
        or text.startswith("Recovered Hermes UI tool receipts from the visible chat.")
    )


def _is_recovered_ui_tool_receipt(msg):
    return (
        isinstance(msg, dict)
        and msg.get("role") == "assistant"
        and str(msg.get("content") or "").startswith("[Recovered UI-observed tool receipts;")
    )


def _remove_contradicted_tool_denials(messages):
    """Drop bad self-corrections once recovered/prior tool evidence exists."""
    cleaned = []
    seen_tool_evidence = False
    for msg in messages or []:
        if _is_old_recovered_ui_tool_note(msg):
            continue
        if _messages_have_tool_evidence([msg]) or _is_recovered_ui_tool_receipt(msg):
            seen_tool_evidence = True
        if seen_tool_evidence and _assistant_denies_prior_tool_work(msg):
            continue
        cleaned.append(msg)
    return cleaned


# ── Backend-session ⇄ UI-conversation recovery helpers ─────────────────────
# The UI sidebar list lives in ``ui-conversations.json`` and is *separately*
# maintained from the canonical session files in ``SESSION_DIR``.  When the
# UI's local state shrinks for any reason (cleared localStorage, a stale tab
# racing a load, a refresh that POSTed before GETing) the POST handler used
# to blindly replace the on-disk list and silently drop chats whose
# conversation data still existed in the backend session files.
#
# These helpers let ``_conversations_load`` self-heal by folding in any
# backend session files missing from the UI list, and let
# ``_conversations_save`` defensively preserve any entry whose backend file
# still exists.  Source-of-truth on read becomes the session files; the UI
# list is treated as a rich metadata mirror (titles, reasoning trails, token
# counts) that can be regenerated when needed.

_RECOVERY_SKIP_PREFIXES = (
    "codex_", "test_", "verify_", "diag", "debug_", "direct_",
)


def _list_backend_session_files():
    """Yield (session_id, path) for every plausible chat session file."""
    if not SESSION_DIR or not os.path.isdir(SESSION_DIR):
        return
    try:
        names = os.listdir(SESSION_DIR)
    except OSError:
        return
    for fname in names:
        if not fname.endswith(".json"):
            continue
        sid = fname[:-5]
        if sid.startswith(_RECOVERY_SKIP_PREFIXES):
            continue
        yield sid, os.path.join(SESSION_DIR, fname)


def _backend_session_exists(session_id):
    """True if a backend session file exists for this id."""
    if not session_id or not SESSION_DIR:
        return False
    return os.path.isfile(os.path.join(SESSION_DIR, str(session_id) + ".json"))


def _convert_backend_messages_to_ui(backend_msgs):
    """Backend session messages → UI sidebar messages.

    Backend:  {role: user/assistant/tool, content, tool_calls?, tool_call_id?}
    UI:       {id, role, content, images, toolCalls, [thinking, streaming]}

    Tool result messages are folded into the assistant turn that issued the
    call; the UI renders results inline under their parent toolCall.
    """
    tool_results = {}
    for m in (backend_msgs or []):
        if isinstance(m, dict) and m.get("role") == "tool":
            tcid = m.get("tool_call_id")
            if tcid:
                tool_results[tcid] = _message_text(m.get("content"))

    def _ui_tool_call(tc):
        if not isinstance(tc, dict):
            return None
        fn = tc.get("function") or {}
        tool_name = fn.get("name") or tc.get("name") or "tool"
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                args = {"_raw": args}
        return {
            "toolName": tool_name,
            "label": tool_name,
            "args": args or {},
            "timestamp": "",
            "done": True,
            "result": tool_results.get(tc.get("id")),
        }

    def _flush_pending_tool_turn():
        nonlocal pending_tool_calls
        if not pending_tool_calls:
            return
        ui.append({
            "id": "thinking-" + str(base_ts + len(ui)),
            "role": "assistant",
            "content": "",
            "thinking": False,
            "streaming": False,
            "toolCalls": pending_tool_calls,
        })
        pending_tool_calls = []

    ui = []
    pending_tool_calls = []
    base_ts = int(time.time() * 1000)
    for i, m in enumerate(backend_msgs or []):
        if not isinstance(m, dict):
            continue
        if m.get("_error") or _is_cancellation_marker_message(m):
            continue
        role = m.get("role")
        if role == "tool":
            continue
        msg_id = str(base_ts + i)
        if role == "user":
            _flush_pending_tool_turn()
            ui.append({
                "id": msg_id,
                "role": "user",
                "content": _message_text(m.get("content")),
                "images": [],
                "toolCalls": [],
            })
        elif role == "assistant":
            content = _message_text(m.get("content"))
            tcs = [
                converted for converted in (_ui_tool_call(tc) for tc in (m.get("tool_calls") or []))
                if converted
            ]
            if tcs and not content.strip():
                pending_tool_calls.extend(tcs)
                continue
            if pending_tool_calls:
                tcs = pending_tool_calls + tcs
                pending_tool_calls = []
            ui.append({
                "id": "thinking-" + msg_id,
                "role": "assistant",
                "content": content,
                "thinking": False,
                "streaming": False,
                "toolCalls": tcs,
            })
    _flush_pending_tool_turn()
    return ui


def _backend_mtime_newer_than_ui(ui_entry, backend_path):
    """Cheap mtime-vs-last_active_at check to gate the expensive reconcile.

    Returns True when the backend file was modified after the UI entry's
    last_active_at — i.e. the UI list is missing turns that the agent has
    already persisted to disk.  Returns True on any parse failure so we
    err on the side of reconciling.
    """
    try:
        backend_mtime = os.path.getmtime(backend_path)
    except OSError:
        return False
    ui_last = ""
    if isinstance(ui_entry, dict):
        ui_last = ui_entry.get("last_active_at") or ""
    if not ui_last:
        return True
    try:
        # "2026-05-11T14:24:40.000Z" — UTC.
        parsed = time.strptime(ui_last[:19], "%Y-%m-%dT%H:%M:%S")
        ui_ts = calendar.timegm(parsed)
    except Exception:
        return True
    return backend_mtime > ui_ts + 1  # 1s slop for round-tripping


def _reconcile_ui_entry_with_backend(ui_entry, backend_path):
    """Repair a UI sidebar entry against the canonical backend session file.

    Two failure modes this handles:

      A. Tail drift — backend has newer turns than the UI list.  This
         happens when the UI never POSTed after the last stream
         completed (page refresh during/right after the assistant
         reply).  Fix: anchor on the last user message common to both,
         append the backend's tail in UI format.  Preserves the UI's
         rich metadata (reasoning trails, token counts, custom titles,
         streaming flags) for the earlier portion of the chat.

      B. Middle gap — backend has substantially more user prompts than
         the UI list, but the UI's tail matches the backend's tail.
         Empirically observed: the UI collapses a compaction event
         into a single "compaction" card and POSTs a list that omits
         the pre-compaction messages, even though they're intact on
         disk.  Anchor-match would find the tail but miss the dropped
         middle entirely.  Fix: full-rebuild the entry from backend.
         Loses UI-only metadata (reasoning trails, exact creation
         dates) but recovers the actual conversation content, which
         is the priority.

    Strategy: cheap mtime gate → tail backfill → middle-gap check →
    full rebuild fallback.  Each step exits early if no drift is
    detected, so healthy entries are returned unchanged.
    """
    if not isinstance(ui_entry, dict):
        return ui_entry
    if not _backend_mtime_newer_than_ui(ui_entry, backend_path):
        return ui_entry
    try:
        with open(backend_path, "r", encoding="utf-8") as f:
            backend = json.load(f)
    except Exception:
        return ui_entry
    b_msgs = backend.get("messages") or []
    if not b_msgs:
        return ui_entry

    backend_user_count = sum(
        1 for m in b_msgs if isinstance(m, dict) and m.get("role") == "user"
    )

    ui_msgs = ui_entry.get("messages") or []

    # Find anchor: last user message in the UI, normalised the same way
    # _message_identity normalises (strip workspace prefix, collapse
    # whitespace) so comparison against the backend is tolerant.
    last_user_text = None
    for m in reversed(ui_msgs):
        if isinstance(m, dict) and m.get("role") == "user":
            t = _message_text(m.get("content"))
            t = _strip_workspace_prefix(t, include_legacy=True)
            last_user_text = " ".join((t or "").split())
            break

    full_rebuild_reason = None

    if last_user_text is None:
        # UI has no user msgs at all — full rebuild.
        full_rebuild_reason = "ui has no user messages"
        new_msgs = _convert_backend_messages_to_ui(b_msgs)
    else:
        cutoff_idx = None
        for i in range(len(b_msgs) - 1, -1, -1):
            bm = b_msgs[i]
            if not isinstance(bm, dict) or bm.get("role") != "user":
                continue
            bt = _message_text(bm.get("content"))
            bt = _strip_workspace_prefix(bt, include_legacy=True)
            bt_norm = " ".join((bt or "").split())
            if bt_norm == last_user_text:
                cutoff_idx = i
                break

        if cutoff_idx is None:
            # Anchor missing — don't risk corrupting.  This can happen
            # right after a recovery or a backend rebuild.
            return ui_entry

        tail = b_msgs[cutoff_idx + 1:]
        tail_ui = _convert_backend_messages_to_ui(tail) if tail else []
        candidate_msgs = list(ui_msgs) + tail_ui

        # Middle-gap detection: after tail backfill, does the UI still
        # have far fewer user prompts than the backend?  Tail-backfill
        # only catches drift at the END of the conversation; if the UI
        # has been chopped in the MIDDLE (compaction-card collapse,
        # truncated POST during recovery, etc.) the user counts won't
        # match even after a successful anchor match.  Allow 2-msg
        # slop for in-flight turns.
        candidate_user_count = sum(
            1 for m in candidate_msgs
            if isinstance(m, dict) and m.get("role") == "user"
        )
        if backend_user_count > candidate_user_count + 2:
            full_rebuild_reason = (
                f"middle gap detected: backend has {backend_user_count} "
                f"user prompts vs candidate {candidate_user_count} "
                f"(anchor-match alone would leave the chat short by "
                f"{backend_user_count - candidate_user_count} prompts)"
            )
            new_msgs = _convert_backend_messages_to_ui(b_msgs)
        else:
            if not tail_ui:
                # No drift — anchor matches, no newer turns to append.
                return ui_entry
            new_msgs = candidate_msgs

    if not new_msgs:
        return ui_entry

    out = dict(ui_entry)
    out["messages"] = new_msgs
    out["message_count"] = len(new_msgs)
    out["user_prompt_count"] = sum(
        1 for m in new_msgs if isinstance(m, dict) and m.get("role") == "user"
    )
    out["tool_call_count"] = sum(
        len(m.get("toolCalls") or []) for m in new_msgs if isinstance(m, dict)
    )
    try:
        mtime = os.path.getmtime(backend_path)
        out["last_active_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(mtime)
        )
    except OSError:
        pass
    if full_rebuild_reason:
        out["_backend_full_rebuilt"] = True
        sid = ui_entry.get("id") or "?"
        print(
            f"[serve] reconcile {sid}: full-rebuild from backend — "
            f"{full_rebuild_reason}",
            flush=True,
        )
    else:
        out["_backend_tail_backfilled"] = True
    return out


def _backend_session_to_ui_entry(session_id, path):
    """Build a UI sidebar entry from a backend session file, or None if the
    session has no user prompts (test runs, malformed files, etc.).
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            backend = json.load(f)
    except Exception:
        return None
    msgs = backend.get("messages") or []
    if not msgs:
        return None
    user_count = sum(1 for m in msgs if isinstance(m, dict) and m.get("role") == "user")
    if user_count == 0:
        return None
    ui_msgs = _convert_backend_messages_to_ui(msgs)
    first_user = ""
    for m in ui_msgs:
        if m.get("role") == "user":
            first_user = (m.get("content") or "")[:60]
            break
    try:
        mtime = os.path.getmtime(path)
        ctime = os.path.getctime(path)
    except OSError:
        mtime = ctime = time.time()
    last_active = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(mtime))
    try:
        created = time.strftime("%-m/%-d/%Y, %-I:%M:%S %p", time.localtime(ctime))
    except Exception:
        created = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ctime))
    return {
        "id": session_id,
        "title": first_user or session_id,
        "created": created,
        "last_active_at": last_active,
        "unread_count": 0,
        "workspace": backend.get("workspace") or _get_last_workspace() or "",
        "messages": ui_msgs,
        "message_count": len(ui_msgs),
        "user_prompt_count": user_count,
        "tool_call_count": sum(len(m.get("toolCalls") or []) for m in ui_msgs),
        "input_tokens": 0,
        "output_tokens": 0,
        "has_compaction": any(
            "context compaction" in (m.get("content") or "").lower()
            or "context compression" in (m.get("content") or "").lower()
            for m in ui_msgs
        ),
        "_recovered_from_backend": True,
    }


def _ui_conversation_activity_ts(entry):
    if not isinstance(entry, dict):
        return 0
    raw = entry.get("last_active_at") or entry.get("updated_at") or entry.get("created") or ""
    if isinstance(raw, (int, float)):
        return float(raw)
    if not raw:
        return 0
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%m/%d/%Y, %I:%M:%S %p"):
        try:
            return calendar.timegm(time.strptime(str(raw)[:19], fmt)) if fmt.startswith("%Y") else time.mktime(time.strptime(str(raw), fmt))
        except Exception:
            pass
    return 0


def _ui_conversation_message_stats(messages):
    messages = messages if isinstance(messages, list) else []
    return {
        "message_count": len(messages),
        "user_prompt_count": sum(1 for m in messages if isinstance(m, dict) and m.get("role") == "user"),
        "tool_call_count": sum(len(m.get("toolCalls") or []) for m in messages if isinstance(m, dict)),
    }


def _better_ui_conversation_entry(a, b):
    """Merge two sidebar entries with the same id, preferring fuller content."""
    if not isinstance(a, dict):
        return b
    if not isinstance(b, dict):
        return a
    a_msgs = a.get("messages") if isinstance(a.get("messages"), list) else []
    b_msgs = b.get("messages") if isinstance(b.get("messages"), list) else []
    a_users = sum(1 for m in a_msgs if isinstance(m, dict) and m.get("role") == "user")
    b_users = sum(1 for m in b_msgs if isinstance(m, dict) and m.get("role") == "user")
    if (b_users, len(b_msgs), _ui_conversation_activity_ts(b)) > (a_users, len(a_msgs), _ui_conversation_activity_ts(a)):
        primary, secondary = dict(b), a
    else:
        primary, secondary = dict(a), b

    for key in ("title", "created", "workspace", "source"):
        if not primary.get(key) and secondary.get(key):
            primary[key] = secondary.get(key)
    for key in ("pinned", "archived", "has_compaction"):
        primary[key] = bool(primary.get(key) or secondary.get(key))
    primary["unread_count"] = max(int(primary.get("unread_count") or 0), int(secondary.get("unread_count") or 0))
    primary["input_tokens"] = max(int(primary.get("input_tokens") or 0), int(secondary.get("input_tokens") or 0))
    primary["output_tokens"] = max(int(primary.get("output_tokens") or 0), int(secondary.get("output_tokens") or 0))
    if _ui_conversation_activity_ts(secondary) > _ui_conversation_activity_ts(primary):
        primary["last_active_at"] = secondary.get("last_active_at") or secondary.get("updated_at") or primary.get("last_active_at")
    primary.update(_ui_conversation_message_stats(primary.get("messages") or []))
    return primary


def _dedupe_ui_conversations(entries):
    """Collapse duplicate sidebar rows by id before load/save/persist."""
    out = []
    index = {}
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "").strip()
        if not sid:
            continue
        entry = dict(entry)
        entry["id"] = sid
        if sid in index:
            out[index[sid]] = _better_ui_conversation_entry(out[index[sid]], entry)
        else:
            index[sid] = len(out)
            out.append(entry)
    out.sort(key=_ui_conversation_activity_ts, reverse=True)
    return out


def _load_ui_conversation_messages(session_id):
    try:
        if not os.path.exists(UI_CONVERSATIONS_FILE):
            return []
        with open(UI_CONVERSATIONS_FILE, "r", encoding="utf-8") as f:
            conversations = json.load(f)
        if not isinstance(conversations, list):
            return []
        for conv in conversations:
            if not isinstance(conv, dict):
                continue
            if conv.get("id") == session_id:
                messages = conv.get("messages")
                return messages if isinstance(messages, list) else []
    except Exception as e:
        print(f"[serve] ui conversation recovery failed for {session_id}: {e}", flush=True)
    return []


def _merge_browser_repair_messages(server_messages, client_messages):
    """Repair from browser transcript without erasing server-side tool evidence."""
    server_clean = _sanitize_messages_for_api(server_messages or [])
    client_clean = _sanitize_messages_for_api(
        _enrich_messages_with_ui_tool_evidence(client_messages or [])
    )
    if not server_clean:
        return client_clean
    if not client_clean:
        return server_clean
    if not _messages_have_tool_evidence(server_clean):
        return client_clean

    merged = list(server_clean)
    represented = {}
    for msg in merged:
        ident = _message_identity(msg)
        if ident is not None:
            represented[ident] = represented.get(ident, 0) + 1
    consumed = {}
    for msg in client_clean:
        ident = _message_identity(msg)
        if ident is None:
            continue
        used = consumed.get(ident, 0)
        already = represented.get(ident, 0)
        if used < already:
            consumed[ident] = used + 1
            continue
        merged.append(msg)
        represented[ident] = already + 1
        consumed[ident] = used + 1
    return _sanitize_messages_for_api(merged)


def _tail_identities(messages, limit=6):
    clean = _sanitize_messages_for_api(messages or [])
    out = []
    for msg in clean:
        ident = _message_identity(msg)
        if ident is not None:
            out.append(ident)
    return out[-max(1, int(limit or 1)):]


def _should_repair_from_client_transcript(server_messages, client_messages):
    """True when the browser transcript contains turns the server lacks.

    The reference UI keeps one authoritative server transcript, but this
    single-file UI also persists a browser/sidebar mirror. When the mirror is
    richer, user counts can still match while assistant/tool turns are missing
    from the backend. That is the short-chat "forgot after a few turns" failure.
    """
    server_clean = _sanitize_messages_for_api(server_messages or [])
    client_clean = _sanitize_messages_for_api(
        _enrich_messages_with_ui_tool_evidence(client_messages or [])
    )
    if not client_clean:
        return False, ""
    if not server_clean:
        return True, "server_empty"

    server_users = _count_role_messages(server_clean, "user")
    client_users = _count_role_messages(client_clean, "user")
    if client_users > server_users:
        return True, f"client_users>{server_users}"

    server_assistants = _count_role_messages(server_clean, "assistant")
    client_assistants = _count_role_messages(client_clean, "assistant")
    if client_users >= server_users and client_assistants > server_assistants:
        return True, f"client_assistants>{server_assistants}"

    if (
        client_users >= server_users
        and _messages_have_tool_evidence(client_clean)
        and not _messages_have_tool_evidence(server_clean)
    ):
        return True, "client_tool_evidence"

    if (
        client_users >= server_users
        and len(client_clean) > len(server_clean)
        and _tail_identities(client_clean) != _tail_identities(server_clean)
    ):
        return True, "tail_mismatch"

    return False, ""


def _assistant_claims_local_work_without_tools(msg):
    """Detect promise-only local work replies that should not steer context."""
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return False
    if msg.get("tool_calls"):
        return False
    text = " ".join(str(msg.get("content") or "").lower().split())
    if not text:
        return False
    local_noun = re.search(
        r"(file|folder|directory|path|video|image|audio|caption|thumbnail|"
        r"script|command|terminal|process|upload|download|ffmpeg|python|"
        r"crop|cut|render|transcrib|generate|saved?|created?|deleted?|moved?|copied?)",
        text,
    )
    work_claim = re.search(
        r"\b(i(?:'m| am|’m)?\s+(?:still\s+)?(?:cutting|cropping|rendering|"
        r"uploading|downloading|saving|writing|creating|deleting|moving|"
        r"copying|running|executing|checking|inspecting|generating|"
        r"transcribing|polling|waiting)|(?:cutting|cropping|rendering|"
        r"uploading|downloading|saving|writing|creating|deleting|moving|"
        r"copying|running|executing|checking|inspecting|generating|"
        r"transcribing|polling|waiting)\s+now|still\s+(?:cutting|cropping|"
        r"rendering|uploading|downloading|running|generating|transcribing|"
        r"polling|waiting)|let\s+me\s+(?:run|cut|crop|render|upload|"
        r"download|save|write|create|delete|move|copy|check|inspect|"
        r"generate|transcribe|poll|wait)|i(?:'ll| will|’ll)\s+(?:run|"
        r"cut|crop|render|upload|download|save|write|create|delete|move|"
        r"copy|check|inspect|generate|transcribe|poll|wait)|polling\s+for\s+"
        r"completion|waiting\s+for\s+(?:completion|it|them)|queued\.\s*"
        r"(?:polling|waiting))\b",
        text,
    )
    return bool(local_noun and work_claim)


def _remove_promise_only_tail(messages):
    """Do not let fake local-progress narration become next-turn context."""
    cleaned = list(messages or [])
    while cleaned and _assistant_claims_local_work_without_tools(cleaned[-1]):
        cleaned.pop()
    return cleaned


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

# ── Agent cache ──
# Reuse AIAgent instances across turns in the same session. This avoids
# re-initialising MCP discovery, tool registration, and model resolution
# on every single message — the primary cause of multi-second startup
# latency per turn.  Keyed by session_id, value is (agent, signature) so
# a model/toolset swap on the same session replaces the entry instead of
# accumulating two entries.  OrderedDict + LRU eviction caps memory; each
# cached agent retains MCP connections and SessionDB handles, so an
# unbounded dict would slowly exhaust file descriptors on a long-running
# server.  Matches the reference UI ``api/config.py``.
import collections as _collections
SESSION_AGENT_CACHE = _collections.OrderedDict()  # session_id -> (agent, sig)
SESSION_AGENT_CACHE_LOCK = threading.Lock()
SESSION_AGENT_CACHE_MAX = 50
AGENT_INSTANCES = {} # stream_id -> agent instance (for cancel/interrupt)
STREAM_PARTIAL_TEXT = {}  # stream_id -> str, accumulated tokens for cancel-preserve (reference #893)
STREAM_SESSIONS = {}  # stream_id -> session_id, so cancel_stream can persist partial content
STREAM_STATUS = {}  # stream_id -> recoverable stream state for SSE reconnect/status probes
SESSION_ACTIVE_STREAMS = {}  # session_id -> active stream_id for /steer lookups
STREAM_PENDING_STEERS = {}  # stream_id -> [text] accepted before agent construction finishes
STREAM_STEER_STATE = {}  # stream_id -> {"next_id": int, "pending": [steer_record, ...]}
WORK_ITEMS = {}  # stream_id -> server-backed live work item for Tasks UI
WORK_ITEMS_LOCK = threading.Lock()

# session_id -> dict (in-memory cache, persisted to disk like webui)
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
SESSION_ALIASES = {}

# Disk persistence — matches webui SESSION_DIR pattern
SESSION_DIR = os.path.join(HERMES_HOME, "hermes-ui", "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
SESSION_ALIASES_FILE = os.path.join(HERMES_HOME, "hermes-ui", "session-aliases.json")
WORK_ITEMS_FILE = os.path.join(HERMES_HOME, "hermes-ui", "work-items.json")
UI_CONVERSATIONS_FILE = os.path.join(HERMES_HOME, "ui-conversations.json")
WORK_ITEM_DONE_RETENTION_SEC = 2 * 60 * 60
WORK_ITEM_BLOCKED_RETENTION_SEC = 12 * 60 * 60
STREAM_STATUS_RETENTION_SEC = 10 * 60

_STREAM_TERMINAL_EVENTS = {"done", "error", "apperror", "cancel"}

def _prune_stream_status(now=None):
    now = now or time.time()
    stale = []
    for sid, status in list(STREAM_STATUS.items()):
        updated = float(status.get("updated_at") or status.get("started_at") or now)
        active = bool(status.get("active"))
        if not active and now - updated > STREAM_STATUS_RETENTION_SEC:
            stale.append(sid)
    for sid in stale:
        STREAM_STATUS.pop(sid, None)

def _record_stream_status(stream_id, event=None, data=None, *, active=None, session_id=None):
    now = time.time()
    with STREAMS_LOCK:
        _prune_stream_status(now)
        status = STREAM_STATUS.setdefault(stream_id, {
            "stream_id": stream_id,
            "started_at": now,
            "updated_at": now,
            "active": True,
            "session_id": session_id or STREAM_SESSIONS.get(stream_id, ""),
            "partial": "",
            "last_event": "",
            "terminal_event": "",
            "terminal_data": None,
        })
        status["updated_at"] = now
        if session_id:
            status["session_id"] = session_id
        if active is not None:
            status["active"] = bool(active)
        if stream_id in STREAM_PARTIAL_TEXT:
            status["partial"] = STREAM_PARTIAL_TEXT.get(stream_id, "")
        if event:
            status["last_event"] = event
            if event in _STREAM_TERMINAL_EVENTS:
                status["active"] = False
                status["terminal_event"] = event
                status["terminal_data"] = data
    return STREAM_STATUS.get(stream_id, {})


def _work_item_retention_seconds(item):
    """Return how long a completed/blocked task receipt should stay visible."""
    status = str((item or {}).get("status") or "")
    column = str((item or {}).get("column") or "")
    if status == "done" or column == "done":
        return WORK_ITEM_DONE_RETENTION_SEC
    return WORK_ITEM_BLOCKED_RETENTION_SEC


def _utc_now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _save_work_items():
    """Persist recent server-backed work items so the Tasks tab survives reloads."""
    try:
        os.makedirs(os.path.dirname(WORK_ITEMS_FILE), exist_ok=True)
        with WORK_ITEMS_LOCK:
            items = list(WORK_ITEMS.values())
        now = time.time()
        trimmed = []
        for item in items:
            updated_ts = item.get("_updated_ts") or now
            status = str(item.get("status") or "")
            # Keep active work plus short, useful receipts. Older "Done" and
            # "Needs You" items made the Tasks board feel stale.
            if status in ("running", "waiting") or (now - updated_ts) <= _work_item_retention_seconds(item):
                public = {k: v for k, v in item.items() if not str(k).startswith("_")}
                public["_updated_ts"] = updated_ts
                trimmed.append(public)
        with open(WORK_ITEMS_FILE, "w", encoding="utf-8") as f:
            json.dump(trimmed[-200:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[serve] WARNING: Failed to save work items: {e}", flush=True)


def _load_work_items():
    try:
        if not os.path.exists(WORK_ITEMS_FILE):
            return
        with open(WORK_ITEMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return
        now = time.time()
        with WORK_ITEMS_LOCK:
            WORK_ITEMS.clear()
            for raw in data[-200:]:
                if not isinstance(raw, dict):
                    continue
                stream_id = str(raw.get("stream_id") or "")
                if not stream_id:
                    continue
                updated_ts = float(raw.get("_updated_ts") or now)
                if (
                    str(raw.get("status") or "") not in ("running", "waiting")
                    and (now - updated_ts) > _work_item_retention_seconds(raw)
                ):
                    continue
                item = dict(raw)
                item["_updated_ts"] = updated_ts
                WORK_ITEMS[stream_id] = item
    except Exception as e:
        print(f"[serve] WARNING: Failed to load work items: {e}", flush=True)


def _work_item_public(item):
    return {k: v for k, v in (item or {}).items() if not str(k).startswith("_")}


def _work_item_start(stream_id, session_id, title="", detail=""):
    now = _utc_now_iso()
    with WORK_ITEMS_LOCK:
        WORK_ITEMS[stream_id] = {
            "id": f"server:{stream_id}",
            "stream_id": stream_id,
            "session_id": session_id,
            "kind": "Agent",
            "status": "running",
            "column": "active",
            "title": (title or "Hermes is working")[:140],
            "detail": (detail or "Live Hermes turn")[:240],
            "tool_count": 0,
            "created_at": now,
            "updated_at": now,
            "_updated_ts": time.time(),
        }
    _save_work_items()


def _work_item_update(stream_id, **updates):
    if not stream_id:
        return
    now = _utc_now_iso()
    changed = False
    with WORK_ITEMS_LOCK:
        item = WORK_ITEMS.get(stream_id)
        if item is None:
            session_id = STREAM_SESSIONS.get(stream_id, "")
            item = {
                "id": f"server:{stream_id}",
                "stream_id": stream_id,
                "session_id": session_id,
                "kind": "Agent",
                "status": "running",
                "column": "active",
                "title": "Hermes is working",
                "detail": "",
                "tool_count": 0,
                "created_at": now,
            }
            WORK_ITEMS[stream_id] = item
        for key, value in updates.items():
            if value is not None:
                item[key] = value
                changed = True
        item["updated_at"] = now
        item["_updated_ts"] = time.time()
    if changed:
        _save_work_items()


def _load_session_aliases():
    """Load old->new session id redirects created by context compression."""
    try:
        if not os.path.exists(SESSION_ALIASES_FILE):
            return
        with open(SESSION_ALIASES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            SESSION_ALIASES.update({
                str(k): str(v)
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
            })
    except Exception as e:
        print(f"[serve] WARNING: Failed to load session aliases: {e}", flush=True)


def _save_session_aliases():
    """Persist compression redirects so a proxy restart does not revive stale ids."""
    try:
        with open(SESSION_ALIASES_FILE, "w", encoding="utf-8") as f:
            json.dump(SESSION_ALIASES, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[serve] WARNING: Failed to save session aliases: {e}", flush=True)


def _resolve_session_id(session_id):
    """Resolve a possibly-stale session id to its compression-rotated canonical id."""
    sid = str(session_id or "")
    seen = set()
    while sid in SESSION_ALIASES and sid not in seen:
        seen.add(sid)
        sid = SESSION_ALIASES[sid]
    return sid


def _alias_session_id(old_sid, new_sid):
    """Remember that an older compressed session id should now use new_sid."""
    old_sid = str(old_sid or "")
    new_sid = str(new_sid or "")
    if not old_sid or not new_sid or old_sid == new_sid:
        return
    SESSION_ALIASES[old_sid] = new_sid
    _save_session_aliases()


_load_session_aliases()
_load_work_items()


def _save_session(session_id, session_data):
    """Persist session to disk as JSON (matches webui Session.save())."""
    try:
        session_id = _resolve_session_id(session_id)
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
    session_id = _resolve_session_id(session_id)
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
    session_id = _resolve_session_id(session_id)
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


def _count_role_messages(messages, role):
    return sum(
        1 for msg in (messages or [])
        if isinstance(msg, dict) and msg.get("role") == role
    )


def _session_health_snapshot(session_id, client_messages=None, repair_from_client=False):
    """Compare server and browser transcript counts, repairing safe browser-ahead drift."""
    session_id = _resolve_session_id(session_id)
    session = _get_or_create_session(session_id)
    server_messages = list(session.get("messages") or [])
    context_messages = list(_context_messages_for_session(session))
    if not isinstance(client_messages, list):
        client_messages = _load_ui_conversation_messages(session_id)
    client_enriched = _enrich_messages_with_ui_tool_evidence(client_messages or []) if isinstance(client_messages, list) else []
    client_clean = _sanitize_messages_for_api(client_enriched)
    server_user_count = _count_role_messages(server_messages, "user")
    client_user_count = _count_role_messages(client_clean, "user") if client_clean else None
    server_has_tool_evidence = _messages_have_tool_evidence(server_messages)
    client_has_tool_evidence = _messages_have_tool_evidence(client_enriched)
    repaired = False
    warning = ""

    if client_clean and client_user_count is not None:
        should_repair, repair_reason = _should_repair_from_client_transcript(
            server_messages,
            client_clean,
        )
        if should_repair:
            if repair_from_client:
                repaired_messages = _merge_browser_repair_messages(server_messages, client_clean)
                session["messages"] = repaired_messages
                session["context_messages"] = _provider_history_from_transcript(repaired_messages)
                session["recovered_at"] = _utc_now_iso()
                _save_session(session_id, session)
                server_messages = list(repaired_messages)
                context_messages = list(session.get("context_messages") or [])
                server_user_count = _count_role_messages(server_messages, "user")
                repaired = True
                warning = f"browser_transcript_repaired:{repair_reason}"
            else:
                warning = f"browser_transcript_drift:{repair_reason}"
        elif server_user_count > client_user_count + 1:
            warning = "server_ahead_of_browser"

    context_user_count = _count_role_messages(context_messages, "user")
    return {
        "ok": True,
        "session_id": session_id,
        "server_messages": len(server_messages),
        "server_user_prompts": server_user_count,
        "client_messages": len(client_clean) if client_clean else None,
        "client_user_prompts": client_user_count,
        "context_messages": len(context_messages),
        "context_user_prompts": context_user_count,
        "context_compacted": bool(server_user_count and context_user_count < server_user_count),
        "repaired": repaired,
        "warning": warning,
        "checked_at": _utc_now_iso(),
    }


def _run_agent_streaming(session_id, messages, stream_id, base_system_prompt="", reasoning_effort="", workspace=None, model_override="", hermes_profile=""):
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
    _record_stream_status(stream_id, "started", {"session_id": session_id}, active=True, session_id=session_id)

    def put(event, data):
        if cancel_event.is_set() and event not in ("cancel", "error"):
            return
        _record_stream_status(stream_id, event, data, session_id=session_id)
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
    try:
        profile_home, active_profile = _profile_home(hermes_profile or None)
    except Exception as e:
        put("error", {"message": f"Hermes profile error: {e}"})
        return

    _set_thread_env(
        TERMINAL_CWD=workspace_dir,
        HERMES_SESSION_KEY=session_id,
        HERMES_HOME=str(profile_home),
        HERMES_PROFILE=active_profile,
    )

    # Save and set process-level env vars under lock
    with _ENV_LOCK:
        old_cwd = os.environ.get("TERMINAL_CWD")
        old_exec_ask = os.environ.get("HERMES_EXEC_ASK")
        old_session_key = os.environ.get("HERMES_SESSION_KEY")
        old_hermes_home = os.environ.get("HERMES_HOME")
        os.environ["TERMINAL_CWD"] = workspace_dir
        os.environ.pop("HERMES_EXEC_ASK", None)
        os.environ["HERMES_SESSION_KEY"] = session_id
        os.environ["HERMES_HOME"] = str(profile_home)

    _approval_registered = False
    _unreg_notify = None
    # Initialised here (before any code that may raise) so the outer `finally`
    # block can safely check `if _checkpoint_stop is not None` even when an
    # exception fires before the checkpoint thread is created.
    # Matches the reference UI api/streaming.py (Issue #765).
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

        model, provider, base_url, api_key = _resolve_model_and_credentials(model_override, active_profile)
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
        # are included — matches the reference UI api/streaming.py.
        # Our previous raw config read returned ['hermes-cli'] which skipped MCP
        # discovery entirely, so the model had no MCP tools to call and narrated
        # tool use instead of emitting tool_calls.
        try:
            from hermes_cli.tools_config import _get_platform_tools
            from tools.mcp_tool import discover_mcp_tools
            discover_mcp_tools()  # idempotent; lazy MCP server init
            import yaml
            cfg_path = str(profile_home / "config.yaml")
            with open(cfg_path) as f:
                cfg = yaml.safe_load(f) or {}
            toolsets = list(_get_platform_tools(cfg, "cli"))
            print(f"[serve] resolved cli toolsets ({len(toolsets)}): {toolsets}", flush=True)
        except Exception as _e:
            print(f"[serve] WARNING: toolset resolution fallback ({_e})", flush=True)
            toolsets = ["hermes-cli"]

        if "scrapling" in toolsets:
            scrapling_guidance = (
                "Web extraction routing: Scrapling MCP tools are available. "
                "For URL extraction, page summarization, article reading, or structured data collection, "
                "use Scrapling first, preferably a single mcp_scrapling_fetch or mcp_scrapling_get call. "
                "Do not also use browser navigation/snapshot tools for the same URL unless Scrapling fails, "
                "the page requires interactive browser state, or the user explicitly asks for browser testing. "
                "If Scrapling cannot retrieve the needed content, fall back to Hermes web/browser tools and "
                "briefly say that a fallback was needed."
            )
            _separator = "\n\n---\n\n" if base_system_prompt else ""
            base_system_prompt = (base_system_prompt + _separator + scrapling_guidance).strip()
            print("[serve] Scrapling-first web extraction guidance appended", flush=True)

        full_text = ""
        _token_sent = False

        def on_token(text):
            nonlocal full_text, _token_sent
            if text is None:
                return
            _token_sent = True
            full_text += text
            # Accumulate for cancel-preserve (reference #893) — so if the user hits
            # Stop mid-stream we can persist what was generated so far.
            try:
                with STREAMS_LOCK:
                    if stream_id in STREAM_PARTIAL_TEXT:
                        STREAM_PARTIAL_TEXT[stream_id] += str(text)
                _record_stream_status(stream_id, "token", {"text": text}, session_id=session_id)
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
                _work_item_update(
                    stream_id,
                    kind="Tool",
                    status="running",
                    column="active",
                    title=str(preview or name or "Tool running")[:140],
                    detail=str(name or "")[:240],
                )
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
                with WORK_ITEMS_LOCK:
                    _next_tool_count = WORK_ITEMS.get(stream_id, {}).get("tool_count", 0) + 1
                _work_item_update(
                    stream_id,
                    kind="Tool",
                    status="running",
                    column="active",
                    title=str(preview or name or "Tool finished")[:140],
                    detail="Tool finished; Hermes is continuing",
                    tool_count=_next_tool_count,
                )
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
        # gracefully on older hermes-agent builds (matches the reference UI
        # api/streaming.py pattern — issue #772 in their repo).
        import inspect as _inspect
        _agent_params = set(_inspect.signature(AgentClass.__init__).parameters)

        def _agent_status_callback(kind, message):
            """Bridge Agent lifecycle compression status into SSE."""
            _message = str(message or '').strip()
            _kind = str(kind or '').strip().lower()
            if not _message:
                return
            _lower = _message.lower()
            _is_compression_start = (
                _kind == 'lifecycle'
                and (
                    'preflight compression' in _lower
                    or 'compressing' in _lower
                    or 'compacting context' in _lower
                    or 'context too large' in _lower
                )
            )
            if not _is_compression_start:
                return
            put('compressing', {
                'session_id': session_id,
                'message': 'Auto-compressing context to continue...',
            })

        _agent_kwargs = dict(
            model=model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            platform="webui",
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
        # mid-chat despite no compaction firing. Fix ported from reference
        # /the reference UI issue #855.
        if 'gateway_session_key' in _agent_params:
            _agent_kwargs['gateway_session_key'] = session_id
        if 'status_callback' in _agent_params:
            _agent_kwargs['status_callback'] = _agent_status_callback

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

        # ── Agent cache: reuse across turns in the same session ──
        # Reuse the session-scoped Agent instance. Avoids
        # re-initialising MCP discovery, tool registration and model
        # resolution on every turn.  Three correctness details over the
        # naive "dict of agents":
        #   1. Keyed by session_id (single entry per session) with the model
        #      signature stored alongside so a profile/model swap REPLACES
        #      the entry instead of leaking another agent.
        #   2. LRU OrderedDict with eviction at SESSION_AGENT_CACHE_MAX so
        #      long-running servers don't accumulate dead agents (each one
        #      holds MCP sockets / SessionDB handles).
        #   3. Reset transient agent state (_interrupted, _api_call_count)
        #      on reuse — without this, an agent that was interrupted on a
        #      prior cancel still believes it is interrupted on the next
        #      turn.
        import hashlib as _hashlib
        _cache_sig = _hashlib.sha256(
            f"{active_profile}|{profile_home}|{model}|{provider}|{base_url}|{','.join(sorted(toolsets or []))}".encode()
        ).hexdigest()[:16]
        agent = None
        with SESSION_AGENT_CACHE_LOCK:
            _cached = SESSION_AGENT_CACHE.get(session_id)
            if _cached and _cached[1] == _cache_sig:
                agent = _cached[0]
                SESSION_AGENT_CACHE.move_to_end(session_id)
        if agent is not None:
            # Refresh per-turn callbacks (they close over request-scoped
            # objects: the put queue, cancel_event, on_token capturing this
            # stream's state).
            agent.stream_delta_callback = on_token
            agent.reasoning_callback = on_reasoning
            agent.tool_progress_callback = on_tool
            if hasattr(agent, 'step_callback'):
                agent.step_callback = on_step
            if hasattr(agent, 'status_callback'):
                agent.status_callback = _agent_status_callback
            # Reset interrupt state so a prior cancel doesn't haunt the
            # reused agent.
            if hasattr(agent, '_interrupted'):
                agent._interrupted = False
            if hasattr(agent, '_interrupt_message'):
                agent._interrupt_message = None
            if hasattr(agent, '_api_call_count'):
                agent._api_call_count = 0
            print(f"[serve] reusing cached agent for {session_id}", flush=True)
        else:
            agent = AgentClass(**_agent_kwargs)
            with SESSION_AGENT_CACHE_LOCK:
                SESSION_AGENT_CACHE[session_id] = (agent, _cache_sig)
                SESSION_AGENT_CACHE.move_to_end(session_id)
                # Bounded LRU eviction.  When over capacity, drop the
                # oldest entry; without this the cache leaks one agent per
                # never-revisited session for the life of the server.
                while len(SESSION_AGENT_CACHE) > SESSION_AGENT_CACHE_MAX:
                    _evicted_sid, _evicted_entry = SESSION_AGENT_CACHE.popitem(last=False)
                    _evicted_agent = _evicted_entry[0] if isinstance(_evicted_entry, tuple) else None
                    # Best-effort close of any SessionDB the evicted agent
                    # is holding open, otherwise its WAL FD won't be
                    # released until GC finalises the agent — which on a
                    # long-lived server may be never.
                    try:
                        _ev_db = getattr(_evicted_agent, '_session_db', None)
                        if _ev_db is not None and hasattr(_ev_db, 'close'):
                            _ev_db.close()
                    except Exception:
                        pass
                    print(f"[serve] agent cache evicted {_evicted_sid}", flush=True)
            print(f"[serve] created new agent for {session_id}", flush=True)

        # User-configurable base system prompt from Settings → General.
        # Passed via agent.ephemeral_system_prompt — the library's sanctioned
        # slot for per-session personality/style injection.  Matches the
        # personality-injection pattern in the reference UI api/streaming.py
        # (which pulls from config.yaml agent.personalities; we read from a
        # UI field instead, but use the same agent attribute).
        if base_system_prompt:
            agent.ephemeral_system_prompt = base_system_prompt

        # Store agent instance for cancel/interrupt
        _pending_steer_texts = []
        with STREAMS_LOCK:
            AGENT_INSTANCES[stream_id] = agent
            SESSION_ACTIVE_STREAMS[session_id] = stream_id
            _pending_steer_texts = STREAM_PENDING_STEERS.pop(stream_id, [])
            if stream_id in CANCEL_FLAGS and CANCEL_FLAGS[stream_id].is_set():
                try:
                    agent.interrupt("Cancelled before start")
                except Exception:
                    pass
                put("cancel", {"message": "Cancelled by user"})
                return
        for _early_steer in _pending_steer_texts:
            try:
                agent.steer(_early_steer)
                print(
                    f"[serve] /api/chat/steer delivered early steer stream={stream_id[:8]} "
                    f"len={len(_early_steer)}",
                    flush=True,
                )
            except Exception as _steer_err:
                print(f"[serve] early steer delivery failed: {_steer_err!r}", flush=True)

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

        # Workspace context prefix (matches the reference UI behaviour)
        _workspace = workspace_dir
        workspace_ctx = f"[Workspace: {_workspace}]\n"
        workspace_system_msg = (
            f"Active workspace: {_workspace}\n"
            "Every user message is prefixed with [Workspace: /path] indicating the "
            "active workspace. Use this as the default working directory for all "
            "file operations. For code searches, stay inside this workspace and "
            "prefer ripgrep-style targeted searches. Do not recursively search "
            "the user's home directory or parent directories unless the user "
            "explicitly asks for that broader scope.\n"
            "Every user message is also prefixed with [Visible transcript: ...]. "
            "That line describes the full visible Web UI transcript, even when "
            "the model-facing context has been compacted. If the user asks how "
            "many prompts/messages are in this chat, use the Visible transcript "
            "counts instead of counting only the compact context you can see.\n"
            "The prefix may also include Recent visible turns from the UI transcript. "
            "Treat those turns as real conversation state. If they show that you "
            "offered to do a concrete action and the user accepted, continue with "
            "that action instead of saying you do not recall it or asking the user "
            "to confirm again. Recent user instructions override older transcript "
            "and memory when they conflict; never resume an older workflow that a "
            "recent user turn cancelled, abandoned, or replaced.\n"
            "Do not claim new local file/process work is complete unless you actually "
            "used a tool in this turn and observed the result. If the prior transcript "
            "says earlier work was completed but tool metadata is missing, do not "
            "contradict that transcript or ask for already-provided basics; treat it "
            "as unverified prior state, say you will verify the artifact/status, and "
            "use tools before continuing. Recovered UI-observed tool receipts are "
            "evidence that Hermes UI saw tool events in an earlier turn, but they are "
            "not provider-native tool messages; do not say no tools ran when those "
            "receipts exist, and verify any result_not_captured artifact before "
            "claiming final status."
        )

        # the reference UI keeps backend session context authoritative and lets
        # Hermes Agent perform real compression. Do not locally narrow the
        # conversation to a small tail window; that creates invisible fake
        # compaction where Hermes can count old prompts but cannot recall them.
        session = _get_or_create_session(session_id)
        _raw_previous_messages = list(session.get("messages") or [])
        _previous_messages = _drop_cancellation_marker_messages(_raw_previous_messages)
        if len(_previous_messages) != len(_raw_previous_messages):
            session["messages"] = _previous_messages
            session["context_messages"] = _provider_history_from_transcript(_previous_messages, user_msg)
            session["recovered_at"] = _utc_now_iso()
            _save_session(session_id, session)
        _browser_messages = _load_ui_conversation_messages(session_id)
        _browser_clean = _sanitize_messages_for_api(
            _enrich_messages_with_ui_tool_evidence(_browser_messages)
        ) if _browser_messages else []
        _server_user_count = sum(1 for _m in _previous_messages if _m.get("role") == "user")
        _browser_user_count = _count_role_messages(_browser_clean, "user") if _browser_clean else 0
        _repair_from_browser, _browser_repair_reason = _should_repair_from_client_transcript(
            _previous_messages,
            _browser_clean,
        )
        if _repair_from_browser:
            print(
                f"[serve] /api/chat/start browser transcript repair for {session_id}: "
                f"browser_users={_browser_user_count} server_users={_server_user_count}; "
                f"reason={_browser_repair_reason}; "
                "repairing before send",
                flush=True,
            )
            _previous_messages = _merge_browser_repair_messages(_previous_messages, _browser_clean)
            session["messages"] = _previous_messages
            session["context_messages"] = _provider_history_from_transcript(_previous_messages, user_msg)
            session["recovered_at"] = _utc_now_iso()
            _save_session(session_id, session)
        # Read-only: never mutate session state at read time.  The post-run
        # save block below is the only legitimate writer of context_messages.
        _session_ctx = _context_messages_for_session(session)
        _previous_context_messages = _provider_history_from_transcript(
            _session_ctx,
            user_msg,
        )
        _had_ctx = isinstance(session.get("context_messages"), list) and session["context_messages"]
        print(
            f"[serve] {session_id}: context={len(_previous_context_messages)} msgs, "
            f"context_chars={_context_char_size(_previous_context_messages)}, "
            f"display={len(_previous_messages)} msgs, "
            f"has_ctx={'yes' if _had_ctx else 'fallback'}",
            flush=True,
        )
        _server_user_count = sum(1 for _m in _previous_messages if _m.get("role") == "user")
        _client_user_count = sum(1 for _m in messages if isinstance(_m, dict) and _m.get("role") == "user")
        _use_client_history, _client_repair_reason = _should_repair_from_client_transcript(
            _previous_messages,
            messages,
        )
        if _use_client_history:
            print(
                f"[serve] /api/chat/start client transcript repair for {session_id}: "
                f"client_users={_client_user_count} server_users={_server_user_count}; "
                f"reason={_client_repair_reason}; "
                "repairing server transcript from frontend",
                flush=True,
            )
            _previous_messages = _merge_browser_repair_messages(_previous_messages, messages)
            _previous_context_messages = _provider_history_from_transcript(_previous_messages, user_msg)
        clean_history = list(_previous_context_messages)
        clean_history = _remove_promise_only_tail(clean_history)
        # Remove the trailing user message *only* if it is a duplicate of the
        # current user_msg (i.e. an eager checkpoint of this turn's user
        # message).  The previous unconditional pop was the root cause of the
        # "short chat forgot the first message" regression: when turn 1 left
        # a user message with no assistant reply (interrupt / crash / aborted
        # run), turn 2's clean_history ended with that prior turn's user
        # message, the unconditional pop dropped it, and the agent ran with
        # an empty history — losing all memory of turn 1.
        # _provider_history_from_transcript already calls
        # _drop_checkpointed_current_user_from_context with the correct
        # current_user_msg, so this branch is normally a no-op; keep it as a
        # belt-and-braces guard against double-appended duplicates.
        if clean_history and clean_history[-1].get("role") == "user":
            _tail_key = _message_identity(clean_history[-1])
            _current_key = _message_identity({"role": "user", "content": user_msg})
            if _tail_key is not None and _tail_key == _current_key:
                clean_history.pop()

        # Persist the incoming user message BEFORE run_conversation so a server
        # crash mid-turn doesn't silently drop what the user just typed.
        # Mirrors the reference UI `s.pending_user_message` pattern
        # (Issue #765). The final _save_session at end of stream is
        # authoritative; this is a durability floor.
        _pending_msgs = list(_previous_messages)
        if user_msg and not (_pending_msgs and _pending_msgs[-1].get("role") == "user"
                             and _pending_msgs[-1].get("content") == user_msg):
            _pending_msgs.append({"role": "user", "content": user_msg})
        _visible_user_prompts = sum(1 for _m in _pending_msgs if isinstance(_m, dict) and _m.get("role") == "user")
        _visible_assistant_replies = sum(1 for _m in _pending_msgs if isinstance(_m, dict) and _m.get("role") == "assistant")
        _visible_tool_results = sum(1 for _m in _pending_msgs if isinstance(_m, dict) and _m.get("role") == "tool")
        _recent_turns_ctx = _recent_visible_turns_context(_pending_msgs)
        transcript_ctx = (
            f"[Visible transcript: {_visible_user_prompts} user prompts, "
            f"{len(_pending_msgs)} total messages, "
            f"{_visible_assistant_replies} assistant replies, "
            f"{_visible_tool_results} tool results]\n"
            f"{_recent_turns_ctx}"
        )
        turn_ctx = workspace_ctx + transcript_ctx
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
                {"type": "text", "text": turn_ctx + user_msg},
                *user_images,
            ]
            print(
                f"[serve] run_conversation multimodal: text_len={len(user_msg)} "
                f"images={len(user_images)}",
                flush=True,
            )
        else:
            _agent_user_msg = turn_ctx + user_msg

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
        # partial/empty run_conversation result (reference #893 companion fix).
        if not cancel_event.is_set():
            _result_messages = result.get("messages") or session.get("messages") or []
            _merged = _merge_display_messages_after_agent_result(
                _previous_messages,
                _previous_context_messages,
                _restore_reasoning_metadata(_previous_messages, _result_messages),
                user_msg,
            )
            session["context_messages"] = _restore_reasoning_metadata(
                _previous_context_messages,
                _provider_history_from_transcript(_result_messages),
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
                _work_item_update(
                    stream_id,
                    kind="Agent",
                    status="error",
                    column="blocked",
                    title="Agent authentication failed",
                    detail=_err_str[:240] or "Check your API key.",
                )
                put("apperror", {
                    "message": _err_str or "Authentication failed — check your API key.",
                    "type": "auth_mismatch",
                })
            else:
                _work_item_update(
                    stream_id,
                    kind="Agent",
                    status="error",
                    column="blocked",
                    title="Agent returned no response",
                    detail=_err_str[:240] or "Check your model/API configuration.",
                )
                put("apperror", {
                    "message": _err_str or "The agent returned no response. Check your API key and model selection.",
                    "type": "no_response",
                })
            return  # Don't send done — apperror already closes the stream

        # ── Handle context compression side effects ──
        # Mirrors the reference UI api/streaming.py lines 1160-1192.
        # If compression fired inside run_conversation, the agent rotated its
        # session_id. Rename the session file and remap SESSIONS so subsequent
        # turns keep writing to the correct file. Also emit a 'compressed'
        # SSE event so the frontend can show a toast.
        _agent_sid = getattr(agent, "session_id", None)
        _compressed = False
        if _agent_sid and _agent_sid != session_id:
            old_sid, new_sid = session_id, _agent_sid
            _alias_session_id(old_sid, new_sid)
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
            put("compressed", {
                "message": "Context auto-compressed to continue the conversation",
                "session_id": session_id,
            })

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

        _work_item_update(
            stream_id,
            kind="Agent",
            status="done",
            column="done",
            title="Reply complete",
            detail=f"{input_tokens or 0} input tokens, {output_tokens or 0} output tokens",
            completed_at=_utc_now_iso(),
        )
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
        _work_item_update(
            stream_id,
            kind="Agent",
            status="error",
            column="blocked",
            title="Stream error",
            detail=str(e)[:240],
        )
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
            if old_hermes_home is None: os.environ.pop("HERMES_HOME", None)
            else: os.environ["HERMES_HOME"] = old_hermes_home
        _clear_thread_env()
        _record_stream_status(stream_id, active=False, session_id=session_id)
        with STREAMS_LOCK:
            STREAMS.pop(stream_id, None)
            CANCEL_FLAGS.pop(stream_id, None)
            AGENT_INSTANCES.pop(stream_id, None)
            STREAM_PARTIAL_TEXT.pop(stream_id, None)
            STREAM_SESSIONS.pop(stream_id, None)
            if SESSION_ACTIVE_STREAMS.get(session_id) == stream_id:
                SESSION_ACTIVE_STREAMS.pop(session_id, None)
            STREAM_PENDING_STEERS.pop(stream_id, None)
            STREAM_STEER_STATE.pop(stream_id, None)


def cancel_stream(stream_id):
    """Signal an in-flight stream to cancel. Returns True if the stream existed.

    Preserve any partial streamed content as a `_partial: True` message on the
    session so users can see what the agent had generated before they hit Stop.
    Cancellation itself is a control event, not assistant conversation content.
    """
    with STREAMS_LOCK:
        if stream_id not in STREAMS:
            return False
        flag = CANCEL_FLAGS.get(stream_id)
        if flag and flag.is_set():
            return True
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
                _save_session(_cancel_session_id, sess)
    except Exception as _e:
        print(f"[serve] WARNING: cancel-preserve failed for {stream_id}: {_e}", flush=True)

    _work_item_update(
        stream_id,
        kind="Agent",
        status="cancelled",
        column="blocked",
        title="Task cancelled",
        detail="Stopped by user",
        completed_at=_utc_now_iso(),
    )

    # Push the cancel event last, bundling the updated session so the
    # frontend can render the preserved _partial message without a separate
    # re-fetch.
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
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Pragma", "no-cache")
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

    # ── Chat: two-step flow matching the reference UI ──
    # Step 1: POST /api/chat/start → returns {stream_id, session_id}
    # Step 2: GET  /api/chat/stream?stream_id=X → SSE with named events

    def _handle_chat_start(self):
        """Start agent in background thread, return stream_id."""
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        messages = body.get("messages", [])
        requested_session_id = body.get("session_id") or self.headers.get("X-Hermes-Session-Id") or f"web_{uuid.uuid4().hex[:12]}"
        session_id = _resolve_session_id(requested_session_id)
        hermes_profile = str(
            body.get("hermes_profile")
            or body.get("runtime_profile")
            or body.get("hermesProfile")
            or ""
        ).strip()
        try:
            profile_home, active_profile = _profile_home(hermes_profile or None)
            if hermes_profile:
                _apply_active_profile_environment(active_profile)
        except Exception as e:
            return self._json({"error": f"Hermes profile error: {e}"}, 400)
        if requested_session_id != session_id:
            print(
                f"[serve] /api/chat/start resolved session alias: "
                f"{requested_session_id}->{session_id}",
                flush=True,
            )
        try:
            workspace = _resolve_workspace(body.get("workspace"))
            _set_last_workspace(workspace)
            session = _get_or_create_session(session_id)
            session["workspace"] = workspace
            session["hermes_profile"] = active_profile
            session["hermes_home"] = str(profile_home)
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

        # ── Server-side context mode ──
        # The client may send just a `message` string instead
        # of the full `messages` array.  When that happens, build a minimal
        # messages list with just the new user turn — the server-side session
        # store provides the conversation history via _get_or_create_session().
        _inline_message = (body.get("message") or "").strip()
        _inline_images = body.get("images") or []
        if not messages and _inline_message:
            _user_turn = {"role": "user", "content": _inline_message}
            if _inline_images:
                _user_turn["images"] = _inline_images
            messages = [_user_turn]
        elif messages and _inline_message:
            # The browser may send its visible transcript as repair context.
            # Keep the current user turn aligned with `message`, which can
            # include generated image descriptions or other send-time text that
            # is not yet reflected in the visible bubble.
            for _m in reversed(messages):
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _m["content"] = _inline_message
                    if _inline_images:
                        _m["images"] = _inline_images
                    break

        if not messages:
            return self._json({"error": "No messages provided"}, 400)

        stream_id = uuid.uuid4().hex
        q = queue.Queue()
        with STREAMS_LOCK:
            STREAMS[stream_id] = q
        try:
            last_user = ""
            for _m in reversed(messages):
                if isinstance(_m, dict) and _m.get("role") == "user":
                    last_user = str(_m.get("content") or "")
                    break
            _work_item_start(
                stream_id,
                session_id,
                title=(last_user[:120] or "Hermes is working"),
                detail="Live chat turn",
            )
        except Exception:
            pass

        thr = threading.Thread(
            target=_run_agent_streaming,
            args=(session_id, messages, stream_id, base_system_prompt, reasoning_effort, workspace, model_override, active_profile),
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
        status = STREAM_STATUS.get(stream_id) or {}
        if q is None and not status.get("terminal_event"):
            return self._json({"error": "stream not found"}, 404)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            if q is None and status.get("terminal_event"):
                self._sse_event(status.get("terminal_event"), status.get("terminal_data") or {})
                return
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    event, data = q.get(timeout=5)
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
        with STREAMS_LOCK:
            _prune_stream_status()
            status = dict(STREAM_STATUS.get(stream_id) or {})
            active = stream_id in STREAMS
            partial = STREAM_PARTIAL_TEXT.get(stream_id, status.get("partial") or "")
        self._json({
            "active": active,
            "stream_id": stream_id,
            "session_id": status.get("session_id") or STREAM_SESSIONS.get(stream_id, ""),
            "last_event": status.get("last_event") or "",
            "terminal_event": status.get("terminal_event") or "",
            "terminal_data": status.get("terminal_data"),
            "partial": partial,
            "updated_at": status.get("updated_at"),
        })

    def _handle_work_items(self):
        """Return recent server-backed live/background work for the Tasks tab."""
        now = time.time()
        items = []
        changed = False
        with WORK_ITEMS_LOCK:
            for stream_id, item in list(WORK_ITEMS.items()):
                updated_ts = item.get("_updated_ts") or now
                status = str(item.get("status") or "")
                if status not in ("running", "waiting") and (now - updated_ts) > _work_item_retention_seconds(item):
                    WORK_ITEMS.pop(stream_id, None)
                    changed = True
                    continue
                items.append(_work_item_public(item))
        if changed:
            _save_work_items()
        items.sort(key=lambda item: item.get("updated_at") or item.get("created_at") or "", reverse=True)
        self._json({"ok": True, "items": items[:200]})

    def _handle_session_health(self):
        """Report and optionally repair browser/server transcript drift."""
        body = self._read_json_body_optional()
        session_id = body.get("session_id") or self.headers.get("X-Hermes-Session-Id") or ""
        if not session_id:
            try:
                parsed = urllib.parse.urlparse(self.path)
                session_id = (urllib.parse.parse_qs(parsed.query).get("session_id") or [""])[0]
            except Exception:
                session_id = ""
        if not session_id:
            return self._json({"ok": False, "error": "session_id required"}, 400)
        messages = body.get("messages")
        repair = bool(body.get("repair"))
        snapshot = _session_health_snapshot(session_id, messages, repair_from_client=repair)
        self._json(snapshot)

    def _handle_work_item_update(self):
        """Manual Tasks board controls: dismiss or mark a server work item done."""
        body = self._read_json_body_optional()
        stream_id = str(body.get("stream_id") or "").strip()
        action = str(body.get("action") or "").strip().lower()
        if not stream_id:
            return self._json({"ok": False, "error": "stream_id required"}, 400)
        if action == "dismiss":
            with WORK_ITEMS_LOCK:
                removed = WORK_ITEMS.pop(stream_id, None) is not None
            _save_work_items()
            return self._json({"ok": True, "dismissed": removed})
        if action == "done":
            _work_item_update(
                stream_id,
                status="done",
                column="done",
                title=str(body.get("title") or "Marked done")[:140],
                detail=str(body.get("detail") or "Marked done from Tasks")[:240],
                completed_at=_utc_now_iso(),
            )
            with WORK_ITEMS_LOCK:
                item = _work_item_public(WORK_ITEMS.get(stream_id))
            return self._json({"ok": True, "item": item})
        self._json({"ok": False, "error": "Unsupported action"}, 400)

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
        session_id = _resolve_session_id(str(body.get("session_id") or "").strip())
        text = str(body.get("text") or "").strip()
        if not stream_id and session_id:
            with STREAMS_LOCK:
                stream_id = SESSION_ACTIVE_STREAMS.get(session_id, "")
        if not stream_id:
            return self._json({"ok": False, "error": "stream_id or session_id required", "code": "not_active"}, 409)
        if not text:
            return self._json({"ok": False, "error": "text required"}, 400)
        queued_for_agent = False
        with STREAMS_LOCK:
            agent = AGENT_INSTANCES.get(stream_id)
            stream_alive = stream_id in STREAMS
            if agent is None and stream_alive:
                STREAM_PENDING_STEERS.setdefault(stream_id, []).append(text)
                queued_for_agent = True
        if agent is None:
            if queued_for_agent:
                steer_meta = _queue_stream_steer(stream_id, text)
                print(
                    f"[serve] /api/chat/steer stream={stream_id[:8]} accepted=True "
                    f"pending_agent=True len={len(text)}",
                    flush=True,
                )
                return self._json({
                    "ok": True,
                    "accepted": True,
                    "pending_agent": True,
                    "steer": {
                        "id": steer_meta["id"],
                        "preview": steer_meta["preview"],
                    },
                })
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
            "agent_version": _get_installed_agent_version(),
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
            options = _configured_model_options(model, provider)
            # Build label lookup from curated _PROVIDER_MODELS dicts
            _label_map = {}
            for entries in _PROVIDER_MODELS.values():
                for entry in entries:
                    if isinstance(entry, dict) and "id" in entry and "label" in entry:
                        _label_map[entry["id"]] = entry["label"]
            self._json({
                "current": model or "",
                "provider": provider or "",
                "models": [
                    {
                        "id": item,
                        "label": _label_map.get(item, item.split("/")[-1] if "/" in item else item),
                        "provider": _infer_model_provider(item, provider),
                        "provider_label": _PROVIDER_DISPLAY.get(_infer_model_provider(item, provider), _infer_model_provider(item, provider) or "Default"),
                        "context_hint": _model_context_hint(item),
                        "capabilities": _model_capabilities(item, _infer_model_provider(item, provider), agent_ok=True),
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

    def _handle_hermes_profiles(self):
        """GET /api/hermes-profiles — list Hermes runtime profiles without secrets."""
        try:
            self._json(_list_hermes_profiles_payload())
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _handle_hermes_profile_switch(self):
        """POST /api/hermes-profile/switch — mirror `hermes profile use`."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            name = str(body.get("name") or body.get("profile") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "profile name is required"}, 400)
            profiles_mod = _import_hermes_profiles()
            if not profiles_mod:
                if name != "default":
                    return self._json({"ok": False, "error": "Hermes profile support is unavailable"}, 400)
            else:
                profiles_mod.set_active_profile(name)
            home, active = _apply_active_profile_environment(name)
            payload = _list_hermes_profiles_payload()
            payload.update({"ok": True, "active": active, "home": home})
            self._json(payload)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

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
    # The on-disk ``ui-conversations.json`` is a UI-managed mirror of the
    # sidebar list.  The canonical conversation data lives in the backend
    # session files under SESSION_DIR.  Both handlers below reconcile the
    # two stores so a UI-side wipe can't silently drop chats whose
    # conversation data still exists on disk.  See the helpers near
    # _load_ui_conversation_messages for the conversion details.
    CONV_PATH = UI_CONVERSATIONS_FILE

    def _conversations_load(self):
        """GET /api/ui-conversations — self-healing.

        Returns the UI sidebar list, folding in any backend session files
        that aren't already represented.  A UI wipe (cleared localStorage,
        stale tab, etc.) is therefore recoverable on next page load
        without manual intervention.
        """
        try:
            data = json.load(open(self.CONV_PATH)) if os.path.exists(self.CONV_PATH) else []
            if not isinstance(data, list):
                data = []
        except Exception:
            data = []
        before_dedupe = len(data)
        data = _dedupe_ui_conversations(data)
        deduped = before_dedupe - len(data)

        # Pass 1: backfill any entry whose backend session file has newer
        # turns than the UI's mirror.  This catches the "user refreshed
        # before the post-stream POST landed and the assistant reply is
        # missing from the sidebar" case.
        backfilled = 0
        for i, entry in enumerate(data):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("id")
            if not sid:
                continue
            path = os.path.join(SESSION_DIR, str(sid) + ".json") if SESSION_DIR else None
            if not path or not os.path.isfile(path):
                continue
            reconciled = _reconcile_ui_entry_with_backend(entry, path)
            if reconciled is not entry:
                data[i] = reconciled
                backfilled += 1

        # Pass 2: fold in any backend session files that aren't in the UI
        # list at all (true id drift — chats the UI doesn't know about).
        existing_ids = {c.get("id") for c in data if isinstance(c, dict)}
        recovered = 0
        for sid, path in _list_backend_session_files():
            if sid in existing_ids:
                continue
            entry = _backend_session_to_ui_entry(sid, path)
            if entry is None:
                continue
            data.append(entry)
            recovered += 1

        if recovered:
            data = _dedupe_ui_conversations(data)

        if recovered or backfilled or deduped:
            data.sort(
                key=lambda c: (c.get("last_active_at") or "") if isinstance(c, dict) else "",
                reverse=True,
            )
            print(
                f"[serve] /api/ui-conversations: recovered={recovered} "
                f"backfilled={backfilled} deduped={deduped}",
                flush=True,
            )
            try:
                json.dump(data, open(self.CONV_PATH, "w"), indent=2)
            except Exception as e:
                print(f"[serve] /api/ui-conversations dedupe persist failed: {e}", flush=True)

        self._json(data)

    def _conversations_save(self):
        """POST /api/ui-conversations — defensive.

        Replaces the UI list with the incoming payload, but preserves any
        existing entry whose backend session file still exists on disk.
        This stops a UI bug or stale-tab race from silently wiping chats
        whose conversation data is intact.  Real deletions (where the
        backend session file is gone) flow through normally.
        """
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            incoming = json.loads(body) if body else []
            if not isinstance(incoming, list):
                return self._json({"error": "expected list"}, 400)

            try:
                existing = json.load(open(self.CONV_PATH)) if os.path.exists(self.CONV_PATH) else []
                if not isinstance(existing, list):
                    existing = []
            except Exception:
                existing = []
            incoming_before_dedupe = len(incoming)
            incoming = _dedupe_ui_conversations(incoming)
            incoming_deduped = incoming_before_dedupe - len(incoming)
            existing = _dedupe_ui_conversations(existing)

            # Pass A: backfill any incoming entry whose backend file has
            # newer turns than the UI sent.  Same drift that the GET
            # handler heals, but applied to writes too — without this,
            # the UI's stale POST during/after a refresh would rewind
            # the on-disk list past assistant replies we just persisted.
            backfilled = 0
            for i, entry in enumerate(incoming):
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("id")
                if not sid:
                    continue
                path = os.path.join(SESSION_DIR, str(sid) + ".json") if SESSION_DIR else None
                if not path or not os.path.isfile(path):
                    continue
                reconciled = _reconcile_ui_entry_with_backend(entry, path)
                if reconciled is not entry:
                    incoming[i] = reconciled
                    backfilled += 1

            # Pass B: preserve any existing on-disk entry the UI dropped
            # but whose backend session file still exists.
            incoming_ids = {c.get("id") for c in incoming if isinstance(c, dict)}
            preserved = 0
            for entry in existing:
                if not isinstance(entry, dict):
                    continue
                sid = entry.get("id")
                if not sid or sid in incoming_ids:
                    continue
                if _backend_session_exists(sid):
                    incoming.append(entry)
                    preserved += 1

            if preserved:
                incoming = _dedupe_ui_conversations(incoming)

            if preserved or backfilled or incoming_deduped:
                incoming.sort(
                    key=lambda c: (c.get("last_active_at") or "") if isinstance(c, dict) else "",
                    reverse=True,
                )
                print(
                    f"[serve] /api/ui-conversations POST: preserved={preserved} "
                    f"backfilled={backfilled} deduped={incoming_deduped}",
                    flush=True,
                )

            json.dump(incoming, open(self.CONV_PATH, "w"), indent=2)
            self._json({"ok": True, "preserved": preserved, "backfilled": backfilled, "deduped": incoming_deduped})
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

    def _safe_upload_filename(self, name):
        base = os.path.basename(str(name or "upload"))
        base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(" .")
        return base[:180] or f"upload-{uuid.uuid4().hex[:8]}"

    def _handle_upload(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except Exception:
            length = 0
        if length <= 0:
            return self._json({"ok": False, "error": "empty upload"}, 400)
        if length > UPLOAD_MAX_BYTES:
            return self._json({"ok": False, "error": "upload too large"}, 413)
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self._json({"ok": False, "error": "multipart/form-data required"}, 400)
        try:
            import cgi
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": content_type,
                    "CONTENT_LENGTH": str(length),
                },
                keep_blank_values=True,
            )
            workspace = _resolve_workspace(form.getfirst("workspace"))
            upload_dir = os.path.join(workspace, UPLOAD_DIR_NAME)
            os.makedirs(upload_dir, exist_ok=True)
            saved = []
            items = form["file"] if "file" in form else []
            if not isinstance(items, list):
                items = [items]
            for item in items:
                if not getattr(item, "filename", None) or not getattr(item, "file", None):
                    continue
                filename = self._safe_upload_filename(item.filename)
                stem, ext = os.path.splitext(filename)
                target = os.path.join(upload_dir, filename)
                n = 1
                while os.path.exists(target):
                    target = os.path.join(upload_dir, f"{stem}-{n}{ext}")
                    n += 1
                with open(target, "wb") as f:
                    shutil.copyfileobj(item.file, f)
                saved.append({
                    "name": filename,
                    "path": str(pathlib.Path(target).resolve()),
                    "size": os.path.getsize(target),
                    "type": item.type or mimetypes.guess_type(filename)[0] or "application/octet-stream",
                })
            if not saved:
                return self._json({"ok": False, "error": "file field required"}, 400)
            self._json({"ok": True, "files": saved})
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 400)

    # ── Log streaming ──
    _LOG_TAIL_ALLOWLIST = {
        "agent": "agent.log",
        "errors": "errors.log",
        "gateway": "gateway.log",
        "mcp": "mcp.log",
        "mcp-stderr": "mcp-stderr.log",
        "server": "server.log",
    }
    _SECRET_PATTERNS = [
        re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
        re.compile(r"(?i)(api[_-]?key['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
        re.compile(r"(?i)(token['\"]?\s*[:=]\s*['\"]?)[^'\"\s,}]+"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    ]

    def _redact_log_line(self, line):
        text = str(line or "").rstrip()
        for pattern in self._SECRET_PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[redacted]", text)
        return text

    def _read_recent_logs(self, log_names, tail):
        try:
            tail = max(1, min(int(tail), 200))
        except Exception:
            tail = 80
        log_dir = os.path.join(HERMES_HOME, "logs")
        result = {}
        for name in log_names:
            safe_name = str(name or "").strip()
            filename = self._LOG_TAIL_ALLOWLIST.get(safe_name)
            if not filename:
                continue
            fpath = os.path.join(log_dir, filename)
            entry = {"name": safe_name, "exists": os.path.isfile(fpath), "lines": [], "size": 0, "updated": None}
            if os.path.isfile(fpath):
                try:
                    entry["size"] = os.path.getsize(fpath)
                    entry["updated"] = int(os.path.getmtime(fpath))
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        entry["lines"] = [self._redact_log_line(line) for line in f.readlines()[-tail:]]
                except Exception as e:
                    entry["error"] = str(e)
            result[safe_name] = entry
        return result

    def _handle_recent_logs(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        logs_str = params.get("logs", ["errors,gateway,agent,mcp,mcp-stderr"])[0]
        tail = params.get("tail", ["80"])[0]
        log_names = [n.strip() for n in logs_str.split(",") if n.strip()]
        if not log_names:
            log_names = ["errors", "gateway", "agent", "mcp", "mcp-stderr"]
        self._json({
            "logs": self._read_recent_logs(log_names, tail),
            "allowlist": sorted(self._LOG_TAIL_ALLOWLIST.keys()),
        })

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

    # ── Toolsets API (mirrors reference webui /api/tools/toolsets) ──
    def _collect_toolsets(self):
        """Return per-toolset info matching reference shape."""
        from hermes_cli.tools_config import (
            _get_effective_configurable_toolsets,
            _get_platform_tools,
            _toolset_has_keys,
        )
        from toolsets import resolve_toolset
        from hermes_cli.config import load_config
        cfg = load_config()
        enabled = _get_platform_tools(cfg, "cli")
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
        mcp_servers = cfg.get("mcp_servers") or {}
        try:
            from tools.mcp_tool import discover_mcp_tools
            discovered_mcp_tools = sorted(set(discover_mcp_tools()))
        except Exception:
            discovered_mcp_tools = []
        for name, server_cfg in mcp_servers.items():
            if not isinstance(server_cfg, dict):
                continue
            server_name = str(name)
            slug = server_name.replace("-", "_")
            tools = [tool for tool in discovered_mcp_tools if tool.startswith(f"mcp_{slug}_")]
            is_enabled = server_name in enabled
            result.append({
                "name": server_name,
                "label": f"MCP: {server_name.replace('-', ' ').title()}",
                "description": f"{server_name} MCP server",
                "enabled": is_enabled,
                "available": bool(tools) or is_enabled,
                "configured": True,
                "tools": tools,
            })
        return result

    def _handle_toolsets(self):
        """GET /api/tools/toolsets — return per-toolset info matching reference shape."""
        try:
            result = self._collect_toolsets()
            self._json(result)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_web_extract_status(self):
        """GET /api/tools/web-extract — detect Hermes web extraction support."""
        toolsets = []
        toolset_error = None
        try:
            toolsets = self._collect_toolsets()
        except Exception as e:
            toolset_error = str(e)

        def mentions_scrapling(value):
            if value is None:
                return False
            text = str(value).lower()
            return "scrapling" in text

        def mentions_web_extract(value):
            if value is None:
                return False
            text = str(value).lower()
            return (
                "web_extract" in text
                or "web extract" in text
                or "scrape" in text
                or "scraping" in text
            )

        web_extract_toolsets = []
        scrapling_toolsets = []
        for ts in toolsets:
            fields = [ts.get("name"), ts.get("label"), ts.get("description")]
            fields.extend(ts.get("tools") or [])
            if any(mentions_scrapling(field) for field in fields):
                scrapling_toolsets.append(ts)
            if any(mentions_web_extract(field) for field in fields):
                web_extract_toolsets.append(ts)

        package_available = importlib.util.find_spec("scrapling") is not None
        cli_available = shutil.which("scrapling") is not None
        uvx_available = shutil.which("uvx") is not None
        enabled = any(ts.get("enabled") for ts in web_extract_toolsets)
        configured = bool(web_extract_toolsets)
        scrapling_enabled = any(ts.get("enabled") for ts in scrapling_toolsets)
        scrapling_configured = bool(scrapling_toolsets)
        installed = package_available or cli_available
        backend = "scrapling" if scrapling_enabled else ("hermes-web" if enabled else "none")
        self._json({
            "name": "web_extract",
            "label": "Web Extract",
            "description": "Web extraction status, including whether the preferred Scrapling backend is active.",
            "available": configured or installed,
            "enabled": enabled,
            "configured": configured,
            "installed": installed,
            "backend": backend,
            "scrapling_enabled": scrapling_enabled,
            "scrapling_configured": scrapling_configured,
            "package_available": package_available,
            "cli_available": cli_available,
            "uvx_available": uvx_available,
            "toolsets": web_extract_toolsets,
            "scrapling_toolsets": scrapling_toolsets,
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
            if not isinstance(data, dict):
                data = {"skills": []}
            existing = data.get("skills") if isinstance(data.get("skills"), list) else []
            seen = set()
            merged = []
            for skill in existing + _discover_codex_plugin_skills():
                if not isinstance(skill, dict):
                    continue
                name = str(skill.get("name") or "").strip()
                if not name:
                    continue
                key = name.lower()
                if key in seen:
                    continue
                seen.add(key)
                merged.append(skill)
            data["skills"] = merged
            self._json(data)
        except ImportError:
            self._json({"skills": _discover_codex_plugin_skills()})
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
        """POST /api/skills/delete — move a skill directory tree into local trash."""
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
            trash_root = pathlib.Path.home() / ".hermes-ui" / "skill-trash"
            trash_root.mkdir(parents=True, exist_ok=True)
            rel_parent = target.parent.resolve().relative_to(SKILLS_DIR.resolve())
            stamp = time.strftime("%Y%m%d-%H%M%S")
            trash_id = f"{stamp}-{target.name}-{uuid.uuid4().hex[:8]}"
            trash_entry = trash_root / trash_id
            trash_entry.mkdir(parents=True, exist_ok=False)
            trashed_skill = trash_entry / target.name
            shutil.move(str(target), str(trashed_skill))
            metadata = {
                "id": trash_id,
                "name": name,
                "folder_name": target.name,
                "deleted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "original_parent": "." if str(rel_parent) == "." else str(rel_parent),
                "original_path": str(target),
                "trash_path": str(trashed_skill),
            }
            (trash_entry / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            self._json({"success": True, "name": name, "trashed": True, "trash_id": trash_id, "path": str(trashed_skill)})
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def _handle_skills_trash(self):
        """GET /api/skills/trash — list locally trashed skills that can be restored."""
        trash_root = pathlib.Path.home() / ".hermes-ui" / "skill-trash"
        entries = []
        try:
            if trash_root.exists():
                for child in sorted(trash_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    if not child.is_dir():
                        continue
                    meta_file = child / "metadata.json"
                    if not meta_file.exists():
                        continue
                    try:
                        meta = json.loads(meta_file.read_text(encoding="utf-8"))
                        skill_path = child / meta.get("folder_name", meta.get("name", ""))
                        if skill_path.exists():
                            entries.append(meta)
                    except Exception:
                        continue
            self._json({"trash": entries})
        except Exception as e:
            self._json({"error": str(e), "trash": []}, 500)

    def _handle_skill_restore(self):
        """POST /api/skills/restore — restore a skill moved to local trash."""
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except Exception:
            return self._json({"error": "Invalid JSON body"}, 400)
        trash_id = (body.get("id") or "").strip()
        if not trash_id or "/" in trash_id or ".." in trash_id:
            return self._json({"error": "Invalid trash id"}, 400)
        try:
            from tools.skills_tool import SKILLS_DIR
            trash_root = pathlib.Path.home() / ".hermes-ui" / "skill-trash"
            trash_entry = trash_root / trash_id
            meta_file = trash_entry / "metadata.json"
            if not meta_file.exists():
                return self._json({"error": "Trashed skill not found"}, 404)
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            folder_name = meta.get("folder_name") or meta.get("name")
            original_parent = meta.get("original_parent") or "."
            source = trash_entry / folder_name
            dest_parent = (SKILLS_DIR / original_parent).resolve()
            dest_parent.relative_to(SKILLS_DIR.resolve())
            dest = dest_parent / folder_name
            if not source.exists():
                return self._json({"error": "Trashed skill folder is missing"}, 404)
            if dest.exists():
                return self._json({"error": f"Cannot restore: {dest} already exists"}, 409)
            dest_parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(dest))
            shutil.rmtree(trash_entry, ignore_errors=True)
            self._json({"success": True, "name": meta.get("name") or folder_name, "path": str(dest)})
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
        agent_installed = _get_installed_agent_version()
        agent_latest = None
        agent_latest_name = None
        agent_html_url = None
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

        try:
            now = _time.time()
            cache = _agent_release_cache
            if cache["data"] and (now - cache["ts"]) < 3600:
                payload = cache["data"]
            else:
                req = _ur.Request(
                    _HERMES_AGENT_RELEASES_API,
                    headers={
                        "User-Agent": f"hermes-ui/{current}",
                        "Accept": "application/vnd.github+json",
                    },
                )
                with _ur.urlopen(req, timeout=5) as resp:
                    payload = json.loads(resp.read().decode("utf-8", errors="replace"))
                cache["ts"] = now
                cache["data"] = payload
            agent_latest = (payload or {}).get("tag_name") or None
            agent_latest_name = (payload or {}).get("name") or None
            agent_html_url = (payload or {}).get("html_url") or None
        except Exception:
            pass

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
            "agent": {
                "installed": agent_installed,
                "latest": agent_latest,
                "latest_name": agent_latest_name,
                "html_url": agent_html_url,
                "update_available": bool(agent_installed and agent_latest and agent_installed not in str(agent_latest_name or agent_latest)),
                "repo": "NousResearch/hermes-agent",
            },
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
        elif self.path == "/api/work-items" or self.path.startswith("/api/work-items?"):
            self._handle_work_items()
        elif self.path == "/api/session/health" or self.path.startswith("/api/session/health?"):
            self._handle_session_health()
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
        elif self.path.startswith("/api/logs/recent"):
            self._handle_recent_logs()
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
        elif self.path == "/api/skills/trash":
            self._handle_skills_trash()
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
        elif self.path == "/api/hermes-profiles" or self.path == "/api/profiles":
            self._handle_hermes_profiles()
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
        elif self.path == "/api/work-items":
            self._handle_work_item_update()
        elif self.path == "/api/session/health":
            self._handle_session_health()
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
        elif self.path == "/api/upload":
            self._handle_upload()
        elif self.path == "/api/memory":
            self._handle_memory_write()
        elif self.path == "/api/skills/save":
            self._handle_skill_save()
        elif self.path == "/api/skills/delete":
            self._handle_skill_delete()
        elif self.path == "/api/skills/restore":
            self._handle_skill_restore()
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
        elif self.path == "/api/hermes-profile/switch" or self.path == "/api/profile/switch":
            self._handle_hermes_profile_switch()
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
