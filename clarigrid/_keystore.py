"""Config store for API keys.

Location: ~/.config/clarigrid/.env  (chmod 600, owner read/write only)

Resolution order (highest to lowest priority):
  1. os.environ
  2. ~/.config/clarigrid/.env

Keys must never appear in repr(), logs, or exception tracebacks.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

# Overrideable for tests via CLARIGRID_CONFIG_DIR.
_CONFIG_DIR: Path = Path(
    os.environ.get("CLARIGRID_CONFIG_DIR", str(Path.home() / ".config" / "clarigrid"))
)
CONFIG_PATH: Path = _CONFIG_DIR / ".env"


def _get_config_path() -> Path:
    """Return config path, respecting CLARIGRID_CONFIG_DIR env override."""
    env_override = os.environ.get("CLARIGRID_CONFIG_DIR")
    if env_override:
        return Path(env_override) / ".env"
    return CONFIG_PATH


def _secure_write(path: Path, content: str) -> None:
    """Write *content* to *path*; create with mode 600, enforce after every write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Use os.open to guarantee 0o600 on creation without a race window.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)
    # Re-apply in case file pre-existed with looser permissions.
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR)


def read_config() -> dict[str, str]:
    """Load ~/.config/clarigrid/.env.  Returns empty dict if file absent."""
    path = _get_config_path()
    if not path.exists():
        return {}
    try:
        from dotenv import dotenv_values  # type: ignore[import]
        loaded = dotenv_values(path)
        return {k: v for k, v in loaded.items() if v is not None}
    except ImportError:
        pass

    # Manual fallback — no deps required.
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            v = v.strip().strip('"').strip("'")
            result[k.strip()] = v
    return result


def write_config(keys: dict[str, str]) -> None:
    """Merge *keys* into the existing config without overwriting unrelated entries.

    Creates the file and parent directories if they do not exist.
    Sets file permissions to 600 on creation and after every write.
    """
    path = _get_config_path()
    existing = read_config()
    merged = {**existing, **keys}
    lines = [f'{k}="{v}"' for k, v in merged.items()]
    _secure_write(path, "\n".join(lines) + "\n")


def delete_key(env_var: str) -> bool:
    """Remove *env_var* from the .env file.  Returns True if it existed."""
    path = _get_config_path()
    if not path.exists():
        return False
    existing = read_config()
    if env_var not in existing:
        return False
    del existing[env_var]
    if existing:
        lines = [f'{k}="{v}"' for k, v in existing.items()]
        _secure_write(path, "\n".join(lines) + "\n")
    else:
        # Overwrite with empty file (keep the file so _check_first_run stays quiet)
        _secure_write(path, "")
    return True


def get_key(env_var: str) -> str | None:
    """Resolve an API key: os.environ first, then .env file.

    The returned value is the raw key string.  Never log or repr() it.
    """
    val = os.environ.get(env_var)
    if val:
        return val
    return read_config().get(env_var)
