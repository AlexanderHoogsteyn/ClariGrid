"""ClarigGrid command-line interface.

Requires: pip install clarigrid[auth]  (provides click)

Commands
--------
  clarigrid setup                   Run the interactive setup wizard
  clarigrid connect <source>        Connect / authenticate a data source
  clarigrid auth --show             Print configured sources (keys masked)
  clarigrid auth --clear <source>   Remove key for a specific source
  clarigrid auth --clear --all      Remove all keys

  # Legacy key management (still supported)
  clarigrid keys list
  clarigrid keys set <provider> <key>
  clarigrid keys delete <provider>
  clarigrid keys file
"""

from __future__ import annotations

import sys


def _require_click() -> "click":  # type: ignore[name-defined]
    try:
        import click  # type: ignore[import]
        return click
    except ImportError:
        print(
            "The ClarigGrid CLI requires 'click'.\n"
            "Install it with: pip install clarigrid[auth]",
            file=sys.stderr,
        )
        sys.exit(1)


# ── Click CLI ──────────────────────────────────────────────────────────────

def _build_cli() -> "click.Group":
    click = _require_click()

    @click.group()
    def cli() -> None:
        """ClarigGrid — unified European energy market data SDK."""

    # ── setup ──────────────────────────────────────────────────────────────

    @cli.command()
    def setup() -> None:
        """Interactive wizard to configure data source connections."""
        from clarigrid._auth import run_setup_wizard
        run_setup_wizard()

    # ── connect ────────────────────────────────────────────────────────────

    @cli.command()
    @click.argument("source")
    def connect(source: str) -> None:
        """Authenticate and connect a data source (opens browser if needed)."""
        from clarigrid._auth import ensure_authenticated
        from clarigrid.core.exceptions import ConfigurationError, InvalidKeyError

        try:
            ensure_authenticated(source)
            click.echo(f"Connected to '{source}' — key is valid.")
        except (ConfigurationError, InvalidKeyError) as exc:
            click.echo(str(exc), err=True)
            sys.exit(1)

    # ── auth ───────────────────────────────────────────────────────────────

    @cli.command()
    @click.option("--show", is_flag=True, help="Print configured sources with masked keys.")
    @click.option(
        "--clear",
        metavar="SOURCE",
        default=None,
        help="Remove key for SOURCE (use with --all to remove everything).",
    )
    @click.option("--all", "clear_all", is_flag=True, help="Remove ALL keys.")
    def auth(show: bool, clear: str | None, clear_all: bool) -> None:
        """Manage stored API keys."""
        from clarigrid._auth import PROVIDER_AUTH
        from clarigrid._keystore import delete_key, read_config

        if show:
            config = read_config()
            if not config:
                click.echo("No keys configured. Run: clarigrid setup")
                return
            click.echo("Configured sources:\n")
            known_vars = {cfg["env_var"]: src for src, cfg in PROVIDER_AUTH.items()}
            shown: set[str] = set()
            for src, cfg in PROVIDER_AUTH.items():
                env_var = cfg["env_var"]
                val = config.get(env_var)
                if val:
                    masked = _mask(val)
                    click.echo(f"  {src:<14} {env_var}={masked}")
                    shown.add(env_var)
            for k, v in config.items():
                if k not in shown:
                    click.echo(f"  {'(unknown)':<14} {k}={_mask(v)}")
            return

        if clear_all:
            click.confirm(
                "Remove ALL keys from ~/.config/clarigrid/.env?", abort=True
            )
            config = read_config()
            removed = 0
            for k in list(config.keys()):
                if delete_key(k):
                    removed += 1
            click.echo(f"Removed {removed} key(s).")
            return

        if clear:
            source = clear
            cfg = PROVIDER_AUTH.get(source)
            if cfg is None:
                click.echo(f"Unknown source '{source}'.", err=True)
                sys.exit(1)
            removed = delete_key(cfg["env_var"])
            if removed:
                click.echo(f"Key for '{source}' removed.")
            else:
                click.echo(f"No key stored for '{source}'.")
            return

        # No flags — print help.
        click.echo(auth.get_help(click.get_current_context()))

    return cli


def _mask(val: str) -> str:
    """Return a masked representation: first 4 chars + ****."""
    if len(val) <= 4:
        return "****"
    return val[:4] + "****"


# ── Legacy keys sub-command (backward compat) ──────────────────────────────

def _keys(args: list[str]) -> None:
    from clarigrid.core import config as cfg

    sub = args[0] if args else "list"

    if sub == "list":
        keys = cfg.list_api_keys()
        if not keys:
            print("No API keys stored.")
            print(f"Edit {cfg.keys_file()} or run: clarigrid keys set <provider> <key>")
            return
        print(f"Keys in {cfg.keys_file()}:")
        for provider, masked in keys.items():
            print(f"  {provider:<16} {masked}")

    elif sub == "set":
        if len(args) < 3:
            print("Usage: clarigrid keys set <provider> <api_key>", file=sys.stderr)
            sys.exit(1)
        provider, key = args[1], args[2]
        cfg.set_api_key(provider, key)
        print(f"Key for '{provider}' saved to {cfg.keys_file()}")

    elif sub == "delete":
        if len(args) < 2:
            print("Usage: clarigrid keys delete <provider>", file=sys.stderr)
            sys.exit(1)
        provider = args[1]
        if cfg.delete_api_key(provider):
            print(f"Key for '{provider}' removed.")
        else:
            print(f"No key found for '{provider}'.", file=sys.stderr)
            sys.exit(1)

    elif sub == "file":
        print(cfg.keys_file())

    else:
        print(f"Unknown subcommand: {sub}", file=sys.stderr)
        print("Available: list, set, delete, file", file=sys.stderr)
        sys.exit(1)


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    # If the first argument is the legacy 'keys' command, handle without click.
    raw = sys.argv[1:]
    if raw and raw[0] == "keys":
        _keys(raw[1:])
        return

    # Otherwise hand off to the click CLI (requires click installed).
    cli = _build_cli()
    cli()


if __name__ == "__main__":
    main()
