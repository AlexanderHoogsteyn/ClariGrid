"""User configuration — API keys, defaults, feature flags.

Key resolution order (highest to lowest priority):
  1. Environment variable  CLARIGRID_<PROVIDER>_API_KEY
  2. ~/.clarigrid/keys.toml  [keys] section  ← edit this by hand
  3. ~/.clarigrid/config.json (programmatic / legacy)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

_CONFIG_DIR = Path(os.environ.get("CLARIGRID_HOME", Path.home() / ".clarigrid"))
_CONFIG_FILE = _CONFIG_DIR / "config.json"
_KEYS_FILE = _CONFIG_DIR / "keys.toml"

_KEYS_TOML_TEMPLATE = """\
# Clarigrid API Keys
# Edit this file to add provider credentials.
# Lines starting with # are comments.
#
# Alternatively set environment variables:
#   CLARIGRID_ENTSOE_API_KEY=your_key
#   CLARIGRID_FINGRID_API_KEY=your_key
#   CLARIGRID_GIE_API_KEY=your_key
#
# Key resolution order: env var > this file > config.json

[keys]
# entsoe = "YOUR_ENTSOE_API_KEY"
# fingrid = "YOUR_FINGRID_API_KEY"
# gie = "YOUR_GIE_API_KEY"
"""

_config: dict[str, Any] = {}
_keys_toml: dict[str, str] = {}
_loaded = False


# ---------------------------------------------------------------------------
# TOML helpers (stdlib tomllib in 3.11+, tomli fallback for 3.10)
# ---------------------------------------------------------------------------

def _read_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib  # Python 3.11+
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            # Neither available — parse the simple [keys] section ourselves.
            return _parse_keys_toml_fallback(path)
    with open(path, "rb") as f:
        return tomllib.load(f)


def _parse_keys_toml_fallback(path: Path) -> dict[str, Any]:
    """Minimal TOML parser for [keys] only — no deps required."""
    result: dict[str, Any] = {}
    section: str | None = None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            result.setdefault(section, {})
        elif "=" in line and section:
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            result[section][k] = v
    return result


def _write_toml_keys(keys: dict[str, str]) -> None:
    """Write [keys] section back to keys.toml, preserving header comment."""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Read existing file to preserve comments, or start from template.
    if _KEYS_FILE.exists():
        lines = _KEYS_FILE.read_text().splitlines()
    else:
        lines = _KEYS_TOML_TEMPLATE.splitlines()

    # Strip existing key lines under [keys] section, rewrite them.
    out: list[str] = []
    in_keys = False
    keys_written = set()
    for line in lines:
        stripped = line.strip()
        if stripped == "[keys]":
            in_keys = True
            out.append(line)
            # Emit all keys right after the section header.
            for k, v in keys.items():
                out.append(f'{k} = "{v}"')
                keys_written.add(k)
            continue
        if in_keys:
            # Skip old key lines (commented or not), keep other comments/blanks.
            if stripped.startswith("#") or stripped == "":
                out.append(line)
            elif "=" in stripped and not stripped.startswith("["):
                pass  # replaced above
            else:
                in_keys = False
                out.append(line)
        else:
            out.append(line)

    # If [keys] section never appeared, append it.
    if "[keys]" not in "\n".join(out):
        out.append("")
        out.append("[keys]")
        for k, v in keys.items():
            out.append(f'{k} = "{v}"')

    _KEYS_FILE.write_text("\n".join(out) + "\n")


# ---------------------------------------------------------------------------
# Internal load / save
# ---------------------------------------------------------------------------

def _ensure_loaded() -> None:
    global _config, _keys_toml, _loaded
    if _loaded:
        return

    # JSON config (programmatic / legacy).
    if _CONFIG_FILE.exists():
        with open(_CONFIG_FILE) as f:
            _config = json.load(f)

    # keys.toml — create template if missing.
    if not _KEYS_FILE.exists():
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _KEYS_FILE.write_text(_KEYS_TOML_TEMPLATE)

    raw = _read_toml(_KEYS_FILE)
    _keys_toml = raw.get("keys", {})
    _loaded = True


def _save_json() -> None:
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(_CONFIG_FILE, "w") as f:
        json.dump(_config, f, indent=2)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get(key: str, default: Any = None) -> Any:
    _ensure_loaded()
    return _config.get(key, default)


def set_value(key: str, value: Any) -> None:
    _ensure_loaded()
    _config[key] = value
    _save_json()


def set_api_key(provider: str, key: str) -> None:
    """Persist an API key for *provider* to keys.toml."""
    _ensure_loaded()
    _keys_toml[provider] = key
    _write_toml_keys(_keys_toml)


def delete_api_key(provider: str) -> bool:
    """Remove a key from keys.toml. Returns True if it existed."""
    _ensure_loaded()
    if provider not in _keys_toml:
        return False
    del _keys_toml[provider]
    _write_toml_keys(_keys_toml)
    return True


def get_api_key(provider: str) -> str | None:
    """Resolve API key. Priority: env var > keys.toml > config.json."""
    _ensure_loaded()
    env_val = os.environ.get(f"CLARIGRID_{provider.upper()}_API_KEY")
    if env_val:
        return env_val
    if provider in _keys_toml:
        return _keys_toml[provider]
    return _config.get("api_keys", {}).get(provider)


def list_api_keys() -> dict[str, str]:
    """Return all keys stored in keys.toml (values masked)."""
    _ensure_loaded()
    return {k: v[:4] + "****" for k, v in _keys_toml.items()}


def configure(path: str | Path | None = None) -> None:
    """Reload config, optionally from a custom JSON config path."""
    global _CONFIG_FILE, _loaded
    if path is not None:
        _CONFIG_FILE = Path(path)
    _loaded = False
    _ensure_loaded()


def keys_file() -> Path:
    return _KEYS_FILE


def config_path() -> Path:
    return _CONFIG_FILE
