"""The MCP server: three read-only tools over one database."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import __version__
from .audit import AuditLog
from .config import Config
from .engine import Database, QueryFailed, QueryTimeout
from .guard import QueryRejected, check

INSTRUCTIONS = """\
This server gives read-only access to a business database.

Workflow: call list_tables, then describe_table for the tables you need, then run_query.

Rules enforced by the server:
- Only a single SELECT statement per call. Nothing can be written or changed.
- Only the tables returned by list_tables exist as far as you are concerned.
- Some columns are restricted (describe_table marks them). A query that touches
  such a table must list its columns explicitly and may not mention restricted ones.
- Results are capped; `truncated: true` means there were more rows. Prefer
  aggregates (COUNT, SUM, GROUP BY) over pulling raw rows.
- Every call is recorded in an audit log.

If a query is rejected, the error says why. Fix the query; do not retry it unchanged.
"""

READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


def build_server(cfg: Config) -> MCPServer:
    db = Database(cfg)
    db.validate_config()
    audit = AuditLog(cfg.audit_path)
    access = cfg.access
    columns = {t: [c.name for c in db.tables[t].columns] for t in access.allowed_tables}

    server = MCPServer("safequery", instructions=INSTRUCTIONS, version=__version__)

    @server.tool(title="List tables", annotations=READ_ONLY)
    def list_tables() -> dict[str, Any]:
        """List the tables available through this gateway, with a short description of each."""
        tables = [
            {
                "table": name,
                "description": cfg.table_descriptions.get(name, ""),
                "columns": len(db.tables[name].columns),
                "has_restricted_columns": bool(access.restricted_columns.get(name)),
            }
            for name in sorted(access.allowed_tables)
        ]
        audit.record(tool="list_tables", decision="allowed")
        return {"dialect": cfg.dialect, "max_rows_per_query": cfg.limits.max_rows, "tables": tables}

    @server.tool(title="Describe table", annotations=READ_ONLY)
    def describe_table(table: str) -> dict[str, Any]:
        """Show a table's columns, types, keys, and which columns are restricted."""
        name = table.strip().lower()
        if name not in access.allowed_tables:
            audit.record(tool="describe_table", table=name, decision="blocked", code="table_not_allowed")
            raise ToolError(
                f"[table_not_allowed] Table '{name}' is not available through this gateway. "
                f"Available tables: {', '.join(sorted(access.allowed_tables))}."
            )
        info = db.tables[name]
        restricted = access.restricted_columns.get(name, frozenset())
        audit.record(tool="describe_table", table=name, decision="allowed")
        return {
            "table": name,
            "description": cfg.table_descriptions.get(name, ""),
            "columns": [
                {
                    "name": c.name,
                    "type": c.type,
                    "nullable": c.nullable,
                    "primary_key": c.primary_key,
                    "restricted": c.name.lower() in restricted,
                }
                for c in info.columns
            ],
            "foreign_keys": list(info.foreign_keys),
        }

    @server.tool(title="Run read-only query", annotations=READ_ONLY)
    def run_query(sql: str) -> dict[str, Any]:
        """Run one SELECT statement and return its rows.

        Only allowed tables and unrestricted columns can be read. The result is
        capped at the server's row limit; `truncated` tells you if rows were cut off.
        """
        try:
            verdict = check(sql, cfg, known_tables=db.known_tables, columns=columns)
        except QueryRejected as e:
            audit.record(tool="run_query", sql=sql, decision="blocked", code=e.code, reason=e.message)
            raise ToolError(f"[{e.code}] {e.message}") from e

        try:
            result = db.execute(verdict.sql)
        except QueryTimeout as e:
            audit.record(tool="run_query", sql=sql, executed_sql=verdict.sql, decision="error",
                         code="timeout", reason=str(e))
            raise ToolError(f"[timeout] {e}") from e
        except QueryFailed as e:
            audit.record(tool="run_query", sql=sql, executed_sql=verdict.sql, decision="error",
                         code="database_error", reason=str(e))
            raise ToolError(f"[database_error] {e}") from e

        audit.record(
            tool="run_query", sql=sql, executed_sql=verdict.sql, decision="allowed",
            tables=list(verdict.tables), rows=len(result.rows), truncated=result.truncated,
            elapsed_ms=result.elapsed_ms,
        )
        return {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": len(result.rows),
            "truncated": result.truncated,
            "elapsed_ms": result.elapsed_ms,
        }

    return server
