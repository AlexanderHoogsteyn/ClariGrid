"""Clarigrid command-line interface.

Usage:
    clarigrid keys list
    clarigrid keys set <provider> <api_key>
    clarigrid keys delete <provider>
    clarigrid keys file
"""

from __future__ import annotations

import sys


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


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "keys":
        _keys(args[1:])
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Available commands: keys", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
