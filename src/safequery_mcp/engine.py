"""Database access: a read-only connection, a row cap and a time limit.

The row cap is applied while fetching, not by rewriting the SQL, so it behaves
the same on every dialect. The time limit bounds how much work an uncapped
query can make the database do.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import NullPool

from .config import Config, ConfigError


class QueryTimeout(Exception):
    pass


class QueryFailed(Exception):
    pass


@dataclass(frozen=True)
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    primary_key: bool


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[str, ...]


@dataclass(frozen=True)
class QueryResult:
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    elapsed_ms: float


def _session_setup(dialect: str, timeout_ms: int) -> list[str]:
    """Per-connection statements for server databases.

    Not exercised by this repository's test suite, which runs on SQLite only.
    """
    if dialect == "postgres":
        return ["SET default_transaction_read_only = on", f"SET statement_timeout = {timeout_ms}"]
    if dialect == "mysql":
        return ["SET SESSION TRANSACTION READ ONLY", f"SET SESSION max_execution_time = {timeout_ms}"]
    if dialect == "tsql":
        # SQL Server has no session-level read-only switch; rely on a db_datareader-only login.
        return [f"SET LOCK_TIMEOUT {timeout_ms}"]
    return []


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.is_sqlite = make_url(cfg.url).drivername.startswith("sqlite")
        self.engine = self._make_engine()
        self.tables = self._introspect()
        self.known_tables = frozenset(self.tables)

    # -- setup -----------------------------------------------------------------

    def _make_engine(self) -> Engine:
        cfg = self.cfg
        if self.is_sqlite:
            path = make_url(cfg.url).database
            if not path or path == ":memory:":
                raise ConfigError("an on-disk SQLite database path is required")
            if not Path(path).is_file():
                raise ConfigError(f"SQLite database not found: {path}")
            uri = Path(path).as_uri() + "?mode=ro"

            def creator() -> sqlite3.Connection:
                # mode=ro opens the file read-only at the OS level; query_only
                # makes SQLite itself refuse writes even if that were bypassed.
                conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
                conn.execute("PRAGMA query_only = ON")
                return conn

            return create_engine("sqlite://", creator=creator, poolclass=NullPool)

        engine = create_engine(cfg.url, poolclass=NullPool)
        statements = _session_setup(cfg.dialect, int(cfg.limits.timeout_seconds * 1000))

        @event.listens_for(engine, "connect")
        def _on_connect(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            try:
                for stmt in statements:
                    cur.execute(stmt)
            finally:
                cur.close()

        return engine

    def _introspect(self) -> dict[str, TableInfo]:
        """Read the schema of every table and view, allowed or not."""
        insp = inspect(self.engine)
        schemas: list[str | None] = [None]
        schemas += sorted({t.split(".", 1)[0] for t in self.cfg.access.allowed_tables if "." in t})
        out: dict[str, TableInfo] = {}
        for schema in schemas:
            names = list(insp.get_table_names(schema=schema)) + list(insp.get_view_names(schema=schema))
            for name in names:
                key = f"{schema}.{name}".lower() if schema else name.lower()
                pk = set(insp.get_pk_constraint(name, schema=schema).get("constrained_columns") or [])
                cols = tuple(
                    ColumnInfo(
                        name=c["name"],
                        type=str(c["type"]),
                        nullable=bool(c.get("nullable", True)),
                        primary_key=c["name"] in pk,
                    )
                    for c in insp.get_columns(name, schema=schema)
                )
                fks = tuple(
                    f"{', '.join(fk['constrained_columns'])} -> "
                    f"{fk['referred_table']}({', '.join(fk['referred_columns'])})"
                    for fk in insp.get_foreign_keys(name, schema=schema)
                )
                out[key] = TableInfo(name=key, columns=cols, foreign_keys=fks)
        return out

    def validate_config(self) -> None:
        """Refuse to start if the policy names things that do not exist.

        A typo in a restricted column name would otherwise silently expose it.
        """
        access = self.cfg.access
        missing = sorted(access.allowed_tables - self.known_tables)
        if missing:
            raise ConfigError(f"allowed_tables not found in the database: {missing}")
        for table, cols in access.restricted_columns.items():
            existing = {c.name.lower() for c in self.tables[table].columns}
            unknown = sorted(cols - existing)
            if unknown:
                raise ConfigError(f"restricted_columns.{table} names unknown columns: {unknown}")

    # -- queries ---------------------------------------------------------------

    def execute(self, sql: str) -> QueryResult:
        limits = self.cfg.limits
        started = time.perf_counter()
        timed_out = threading.Event()
        timer: threading.Timer | None = None

        with self.engine.connect() as conn:
            if self.is_sqlite:
                raw = conn.connection.driver_connection

                def _interrupt() -> None:
                    timed_out.set()
                    try:
                        raw.interrupt()
                    except sqlite3.Error:
                        pass  # connection already closed: the query finished in time

                timer = threading.Timer(limits.timeout_seconds, _interrupt)
                timer.daemon = True
                timer.start()
            try:
                result = conn.exec_driver_sql(sql)
                columns = [str(c) for c in result.keys()]
                fetched = result.fetchmany(limits.max_rows + 1)
                result.close()
            except Exception as e:
                if timed_out.is_set() or _looks_like_timeout(e):
                    raise QueryTimeout(
                        f"The query was cancelled after {limits.timeout_seconds:g} seconds. "
                        f"Narrow it with a WHERE clause or aggregate less data."
                    ) from e
                raise QueryFailed(_short_error(e)) from e
            finally:
                if timer is not None:
                    timer.cancel()
                conn.rollback()

        truncated = len(fetched) > limits.max_rows
        rows = [[_cell(v, limits.max_cell_chars) for v in row] for row in fetched[: limits.max_rows]]
        return QueryResult(
            columns=columns,
            rows=rows,
            truncated=truncated,
            elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
        )


def _cell(value: Any, max_chars: int) -> Any:
    """Convert a database value into something JSON can carry."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else str(value)
    if isinstance(value, (datetime, date, dtime)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    text = str(value)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _looks_like_timeout(error: Exception) -> bool:
    text = str(error).lower()
    return any(s in text for s in ("statement timeout", "interrupted", "max_execution_time", "query timeout"))


def _short_error(error: Exception) -> str:
    """One line of the driver's message, without SQLAlchemy's SQL echo and help link."""
    original = getattr(error, "orig", None) or error
    line = str(original).strip().splitlines()[0] if str(original).strip() else type(original).__name__
    return line[:300]
