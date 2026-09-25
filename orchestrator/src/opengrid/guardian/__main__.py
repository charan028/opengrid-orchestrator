"""`python -m opengrid.guardian <command>` -- CLI entry, distinct from the `og-guardian` process
(`python -m opengrid.guardian.main`). BUILD.md: "A keygen CLI writes the public key file the fleet
simulator uses: `python -m opengrid.guardian keygen --out /var/lib/opengrid/keys/`"."""

from __future__ import annotations

import argparse
import sys

from opengrid.guardian.keys import keygen


def _cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m opengrid.guardian")
    subparsers = parser.add_subparsers(dest="command", required=True)

    keygen_parser = subparsers.add_parser("keygen", help="Generate a fresh guardian Ed25519 keypair")
    keygen_parser.add_argument("--out", required=True, help="Output directory for the key files")
    keygen_parser.add_argument("--key-id", default="guardian-2026a", help="key_id used on signed envelopes")

    args = parser.parse_args(argv)
    if args.command == "keygen":
        private_path, public_path = keygen(args.out, key_id=args.key_id)
        print(f"Wrote private seed: {private_path}")
        print(f"Wrote public key:   {public_path}")
        return 0
    return 1  # pragma: no cover -- unreachable while "keygen" is the only subcommand


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv[1:]))
