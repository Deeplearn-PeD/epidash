"""Control-key management CLI.

Usage (inside the container / on the server):

    python -m epidash.cli add-key "epidbot"
    python -m epidash.cli list-keys
    python -m epidash.cli revoke 2
"""

import argparse
import secrets
import sys

from epidash.config import get_settings
from epidash.logging import get_logger
from epidash.store import Store

logger = get_logger(__name__)


def _store() -> Store:
    return Store(get_settings().db_path)


def _generate_key() -> str:
    return "dk_" + secrets.token_urlsafe(24)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="epidash.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add-key", help="create a new control key (raw key shown once)")
    p_add.add_argument("name", help="human-readable name, e.g. 'epidbot'")

    sub.add_parser("list-keys", help="list control keys (hashes only)")

    p_revoke = sub.add_parser("revoke", help="revoke a key by id")
    p_revoke.add_argument("key_id", type=int)

    args = parser.parse_args(argv)
    store = _store()

    if args.command == "add-key":
        raw = _generate_key()
        meta = store.add_key(raw, name=args.name)
        print(f"Key created (id={meta['id']}, name={meta['name']!s},"
              f" prefix={meta['key_prefix']}...).")
        print("Store it now — it is shown only once:")
        print(raw)
        return 0

    if args.command == "list-keys":
        keys = store.list_keys()
        if not keys:
            print("No control keys.")
            return 0
        for k in keys:
            status = "revoked" if k["revoked"] else "active"
            last = k["last_used_at"] or "never"
            print(f"id={k['id']}  {k['key_prefix']}...  {status:<8} "
                  f"name={k['name']}  last_used={last}")
        return 0

    if args.command == "revoke":
        if store.revoke_key(args.key_id):
            print(f"Key {args.key_id} revoked.")
            return 0
        print(f"Key {args.key_id} not found.", file=sys.stderr)
        return 1

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
