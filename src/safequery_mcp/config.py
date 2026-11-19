"""Configuration: what the assistant may read, and how much of it.

Everything here fails closed. A config without an explicit table allowlist is
rejected at startup rather than being treated as "allow everything".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy.engine import make_url

SUPPORTED_DIALECTS = {"sqlite", "postgres", "tsql", "mysql"}

# Functions the SQL parser does not recognise are rejected unless listed here.
# Standard functions (COUNT, SUM, COALESCE, CAST, SUBSTRING, DATE_TRUNC, ...)
# are recognised by the parser and need no entry.
DEFAULT_ALLOWED_FUNCTIONS: dict[str, frozenset[str]] = {
    "sqlite": frozenset({"julianday", "printf", "format", "typeof", "total", "unixepoch", "iif"}),
    "postgres": frozenset({"age", "date_part", "to_char", "width_bucket"}),
    "tsql": frozenset({"datename", "eomonth", "isnumeric"}),
    "mysql": frozenset({"datediff", "period_diff"}),
}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_rows: int = 200
    timeout_seconds: float = 10.0
    max_cell_chars: int = 2000
    max_sql_chars: int = 20_000


@dataclass(frozen=True)
class Access:
    allowed_tables: frozenset[str]
    restricted_columns: dict[str, frozenset[str]] = field(default_factory=dict)
    allowed_functions: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Config:
    url: str
    dialect: str
    limits: Limits
    access: Access
    audit_path: Path
    table_descriptions: dict[str, str] = field(default_factory=dict)


def _lower_set(values: Any, where: str) -> frozenset[str]:
    if values is None:
        return frozenset()
    if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
        raise ConfigError(f"{where} must be a list of strings")
    return frozenset(v.strip().lower() for v in values)


def _resolve_url(url: str, base: Path) -> str:
    """Make a relative SQLite path relative to the config file, not the cwd.

    MCP clients launch servers from an arbitrary working directory, so a
    relative database path must not depend on it.
    """
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database:
        db_path = Path(parsed.database)
        if not db_path.is_absolute():
            db_path = (base / db_path).resolve()
        return parsed.set(database=str(db_path)).render_as_string(hide_password=False)
    return url


def load_config(path: str | Path) -> Config:
    path = Path(path).resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return parse_config(raw, base=path.parent)


def parse_config(raw: dict[str, Any], base: Path) -> Config:
    db = raw.get("database") or {}
    url = db.get("url")
    if not url or not isinstance(url, str):
        raise ConfigError("database.url is required")
    dialect = str(db.get("dialect", "")).lower()
    if dialect not in SUPPORTED_DIALECTS:
        raise ConfigError(f"database.dialect must be one of {sorted(SUPPORTED_DIALECTS)}")

    lim = raw.get("limits") or {}
    limits = Limits(
        max_rows=int(lim.get("max_rows", Limits.max_rows)),
        timeout_seconds=float(lim.get("timeout_seconds", Limits.timeout_seconds)),
        max_cell_chars=int(lim.get("max_cell_chars", Limits.max_cell_chars)),
        max_sql_chars=int(lim.get("max_sql_chars", Limits.max_sql_chars)),
    )
    if limits.max_rows < 1 or limits.timeout_seconds <= 0 or limits.max_cell_chars < 1:
        raise ConfigError("limits must be positive")

    acc = raw.get("access") or {}
    allowed_tables = _lower_set(acc.get("allowed_tables"), "access.allowed_tables")
    if not allowed_tables:
        raise ConfigError(
            "access.allowed_tables is required and must not be empty: "
            "the gateway never defaults to exposing every table"
        )

    restricted_raw = acc.get("restricted_columns") or {}
    if not isinstance(restricted_raw, dict):
        raise ConfigError("access.restricted_columns must map table -> list of columns")
    restricted = {
        str(table).strip().lower(): _lower_set(cols, f"access.restricted_columns.{table}")
        for table, cols in restricted_raw.items()
    }
    unknown = set(restricted) - allowed_tables
    if unknown:
        raise ConfigError(
            f"restricted_columns names tables that are not in allowed_tables: {sorted(unknown)}"
        )

    extra_functions = _lower_set(acc.get("allowed_functions"), "access.allowed_functions")
    access = Access(
        allowed_tables=allowed_tables,
        restricted_columns=restricted,
        allowed_functions=DEFAULT_ALLOWED_FUNCTIONS[dialect] | extra_functions,
    )

    audit = raw.get("audit") or {}
    audit_path = Path(audit.get("path", "audit/audit.jsonl"))
    if not audit_path.is_absolute():
        audit_path = (base / audit_path).resolve()

    descriptions = raw.get("table_descriptions") or {}
    if not isinstance(descriptions, dict):
        raise ConfigError("table_descriptions must map table -> text")

    return Config(
        url=_resolve_url(url, base),
        dialect=dialect,
        limits=limits,
        access=access,
        audit_path=audit_path,
        table_descriptions={str(k).lower(): str(v) for k, v in descriptions.items()},
    )
