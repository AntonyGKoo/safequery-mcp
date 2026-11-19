"""SQL policy check: decide whether a statement may run, before it reaches the database.

The check works on the parsed syntax tree, never on the raw text, and the
statement that is executed afterwards is re-rendered from that same tree. A
comment or quoting trick that makes the database read the text differently from
the parser therefore cannot smuggle anything past the check.

This is defence in depth. The database account the gateway connects with should
itself be read-only and limited to the same tables; see the README.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

from .config import Config

# sqlglot warns when it falls back to an opaque "Command" node. We reject those
# anyway, and nothing may be written to stdout/stderr noise during stdio serving.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class QueryRejected(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Verdict:
    sql: str                  # the single statement that will actually be executed
    tables: tuple[str, ...]   # real tables it reads


def _classes(*names: str) -> tuple[type, ...]:
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


# Anything that writes, changes schema, changes session state, or is opaque to the parser.
_FORBIDDEN_NODES = _classes(
    "Insert", "Update", "Delete", "Merge", "Create", "Drop", "Alter", "TruncateTable",
    "Command", "Into", "Lock", "Copy", "LoadData", "Returning", "Pragma", "Attach", "Detach",
    "Execute", "Set", "Use", "Grant", "Revoke", "Transaction", "Commit", "Rollback", "Kill",
    "Analyze", "Describe", "Show", "Comment", "Cache", "Uncache", "Refresh", "Export",
    "Declare", "Put", "Get", "Install", "Summarize",
)
# Recognised functions that read files, and constructs that need values we never supply.
_FORBIDDEN_EXPRESSIONS = _classes(
    "ReadCSV", "ReadParquet", "Parameter", "SessionParameter", "Placeholder", "NextValueFor"
)
_EMPTY_STATEMENTS = _classes("Semicolon")

# A CTE may not take a name that could also be a real or system relation.
_RESERVED_CTE_PREFIXES = ("sqlite_", "pg_", "sys", "information_schema", "mysql", "#")


def check(
    sql: str,
    cfg: Config,
    known_tables: frozenset[str] = frozenset(),
    columns: dict[str, list[str]] | None = None,
) -> Verdict:
    """Return the statement to execute, or raise QueryRejected.

    known_tables: every table and view that exists in the database (lower case),
        allowed or not. Used to stop a CTE from shadowing a real relation.
    columns: column names per allowed table, used only for helpful error text.

    Fails closed: if the analysis itself breaks, the statement is rejected.
    """
    try:
        return _check(sql, cfg, known_tables, columns)
    except QueryRejected:
        raise
    except RecursionError as e:
        raise QueryRejected("too_complex", "The query is nested too deeply to analyse.") from e
    except Exception as e:
        raise QueryRejected(
            "analysis_failed", f"The query could not be analysed safely ({type(e).__name__})."
        ) from e


def _check(
    sql: str,
    cfg: Config,
    known_tables: frozenset[str],
    columns: dict[str, list[str]] | None,
) -> Verdict:
    access = cfg.access
    if not isinstance(sql, str) or not sql.strip():
        raise QueryRejected("empty", "No SQL was provided.")
    if len(sql) > cfg.limits.max_sql_chars:
        raise QueryRejected("too_long", f"SQL is longer than {cfg.limits.max_sql_chars} characters.")

    try:
        # A trailing "; -- comment" parses as an empty Semicolon statement; it carries
        # no SQL and is dropped, since only the one real statement is re-rendered.
        statements = [
            s for s in sqlglot.parse(sql, read=cfg.dialect)
            if s is not None and not isinstance(s, _EMPTY_STATEMENTS)
        ]
    except SqlglotError as e:
        first_line = str(e).splitlines()[0] if str(e) else "syntax error"
        raise QueryRejected("parse_error", f"Could not parse the SQL: {first_line}") from e

    if len(statements) != 1:
        raise QueryRejected(
            "multiple_statements",
            f"Exactly one statement is allowed per call; received {len(statements)}.",
        )
    root = statements[0]

    if not isinstance(root, (exp.Select, exp.SetOperation)):
        raise QueryRejected(
            "not_a_select",
            f"Only SELECT queries are allowed; received a {type(root).__name__.upper()} statement.",
        )

    for node in root.walk():
        if isinstance(node, _FORBIDDEN_NODES):
            raise QueryRejected(
                "write_or_command",
                f"The query contains a {type(node).__name__.upper()} clause, which is not read-only.",
            )
        if isinstance(node, _FORBIDDEN_EXPRESSIONS):
            raise QueryRejected(
                "expression_not_allowed",
                f"{type(node).__name__} expressions are not allowed.",
            )

    for fn in root.find_all(exp.Anonymous):
        name = (fn.name or "").lower()
        if name not in access.allowed_functions:
            raise QueryRejected(
                "function_not_allowed",
                f"Function '{name}' is not on the allowlist. Use standard SQL functions "
                f"(COUNT, SUM, AVG, MIN, MAX, COALESCE, CAST, SUBSTRING, ROUND, ...).",
            )

    cte_names = {cte.alias_or_name.lower() for cte in root.find_all(exp.CTE)}
    for name in cte_names:
        if name in known_tables or name in access.allowed_tables or name.startswith(_RESERVED_CTE_PREFIXES):
            raise QueryRejected(
                "cte_name_conflict",
                f"The WITH name '{name}' collides with an existing or reserved table name. "
                f"Choose a different name.",
            )

    available = ", ".join(sorted(access.allowed_tables))
    touched: list[str] = []
    aliases: set[str] = set()
    for table in root.find_all(exp.Table):
        name = (table.name or "").lower()
        if not name:
            raise QueryRejected("table_not_allowed", "Table-valued functions are not allowed.")
        if table.catalog:
            raise QueryRejected("table_not_allowed", "Cross-database references are not allowed.")
        if table.alias:
            aliases.add(table.alias.lower())
        if not table.db and name in cte_names:
            continue
        key = f"{table.db.lower()}.{name}" if table.db else name
        if key not in access.allowed_tables:
            raise QueryRejected(
                "table_not_allowed",
                f"Table '{key}' is not available through this gateway. Available tables: {available}.",
            )
        aliases.add(name)
        if key not in touched:
            touched.append(key)

    blocked: dict[str, frozenset[str]] = {
        t: access.restricted_columns[t] for t in touched if access.restricted_columns.get(t)
    }
    if blocked:
        blocked_names = frozenset().union(*blocked.values())
        hint = _allowed_columns_hint(blocked, columns)

        for star in root.find_all(exp.Star):
            if not isinstance(star.parent, exp.Count):
                raise QueryRejected(
                    "star_on_restricted_table",
                    f"SELECT * is not allowed in a query that reads {_names(blocked)}, because "
                    f"some of its columns are restricted. List the columns you need.{hint}",
                )
        for ident in root.find_all(exp.Identifier):
            if ident.name.lower() in blocked_names:
                raise QueryRejected(
                    "restricted_column",
                    f"Column '{ident.name.lower()}' is restricted and cannot be read or used "
                    f"in a query that touches {_names(blocked)}.{hint}",
                )
        for col in root.find_all(exp.Column):
            if not col.table and col.name.lower() in aliases:
                raise QueryRejected(
                    "whole_row_reference",
                    f"'{col.name}' refers to a whole table row, which would include restricted "
                    f"columns. List the columns you need.{hint}",
                )

    return Verdict(sql=root.sql(dialect=cfg.dialect, comments=False), tables=tuple(touched))


def _names(blocked: dict[str, frozenset[str]]) -> str:
    return ", ".join(sorted(blocked))


def _allowed_columns_hint(blocked: dict[str, frozenset[str]], columns: dict[str, list[str]] | None) -> str:
    if not columns:
        return ""
    parts = []
    for table in sorted(blocked):
        allowed = [c for c in columns.get(table, []) if c.lower() not in blocked[table]]
        if allowed:
            parts.append(f"{table}: {', '.join(allowed)}")
    return f" Readable columns: {'; '.join(parts)}." if parts else ""
