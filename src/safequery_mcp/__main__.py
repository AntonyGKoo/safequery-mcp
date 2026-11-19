"""Command line entry point.

    safequery-mcp serve        --config config.yaml   # run the MCP server on stdio
    safequery-mcp check        --config config.yaml "SELECT ..."   # dry-run the policy
    safequery-mcp verify-audit --config config.yaml   # check the audit log's hash chain

In `serve` mode stdout belongs to the MCP protocol, so diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import sys

from .audit import verify
from .config import ConfigError, load_config
from .engine import Database
from .guard import QueryRejected, check


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="safequery-mcp", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the MCP server on stdio")
    p_serve.add_argument("--config", required=True)

    p_check = sub.add_parser("check", help="show whether a statement would be allowed, without running it")
    p_check.add_argument("--config", required=True)
    p_check.add_argument("sql")

    p_verify = sub.add_parser("verify-audit", help="verify the audit log's hash chain")
    p_verify.add_argument("--config", required=True)

    args = parser.parse_args(argv)

    try:
        cfg = load_config(args.config)

        if args.command == "serve":
            from .server import build_server

            build_server(cfg).run("stdio")
            return 0

        if args.command == "check":
            db = Database(cfg)
            db.validate_config()
            columns = {t: [c.name for c in db.tables[t].columns] for t in cfg.access.allowed_tables}
            try:
                verdict = check(args.sql, cfg, known_tables=db.known_tables, columns=columns)
            except QueryRejected as e:
                print(f"BLOCKED [{e.code}] {e.message}")
                return 1
            print(f"ALLOWED tables={list(verdict.tables)}\n{verdict.sql}")
            return 0

        if args.command == "verify-audit":
            result = verify(cfg.audit_path)
            if result.ok:
                print(f"OK: {result.entries} entries, chain intact ({cfg.audit_path})")
                return 0
            print(f"TAMPERED at line {result.bad_line}: {result.reason} "
                  f"({result.entries} entries verified before it)")
            return 1

    except ConfigError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
