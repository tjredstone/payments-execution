from __future__ import annotations

import argparse
import json

from secret_store import (
    delete_secret,
    keychain_service_name,
    read_secret_with_source,
    write_secret,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage advisory secrets in macOS Keychain via keyring.")
    sub = parser.add_subparsers(dest="command", required=True)

    set_parser = sub.add_parser("set", help="Set a secret in Keychain.")
    set_parser.add_argument("name")
    set_parser.add_argument("value")

    get_parser = sub.add_parser("get", help="Read a secret (env first, then Keychain).")
    get_parser.add_argument("name")
    get_parser.add_argument("--json", action="store_true", help="Return metadata as JSON.")

    delete_parser = sub.add_parser("delete", help="Delete a secret from Keychain.")
    delete_parser.add_argument("name")

    args = parser.parse_args()

    if args.command == "set":
        write_secret(args.name, args.value)
        print(f"Stored secret in Keychain: service={keychain_service_name()} account={args.name}")
        return

    if args.command == "get":
        value, source = read_secret_with_source(args.name, required=False)
        if args.json:
            print(
                json.dumps(
                    {
                        "name": args.name,
                        "service": keychain_service_name(),
                        "source": source,
                        "exists": value is not None,
                    },
                    ensure_ascii=True,
                    indent=2,
                )
            )
        else:
            print(value or "")
        return

    if args.command == "delete":
        deleted = delete_secret(args.name)
        print("Deleted." if deleted else "Not found.")


if __name__ == "__main__":
    main()
