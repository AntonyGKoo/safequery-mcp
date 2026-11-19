"""Database-level limits, tested against SQLite.

Several tests call `Database.execute` directly, skipping the SQL guard, to show
that the second layer holds on its own.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import time

import pytest

from conftest import make_config
from safequery_mcp.config import Access, ConfigError
from safequery_mcp.engine import Database, QueryFailed, QueryTimeout, _cell


def test_select_returns_rows(cfg):
    result = Database(cfg).execute("SELECT status, COUNT(*) AS n FROM orders GROUP BY status ORDER BY n DESC")
    assert result.columns == ["status", "n"]
    assert result.rows[0] == ["completed", 1196]
    assert result.truncated is False


def test_row_cap_is_enforced_and_flagged(cfg):
    result = Database(cfg).execute("SELECT id FROM order_items")
    assert len(result.rows) == cfg.limits.max_rows == 50
    assert result.truncated is True


def test_exact_fit_is_not_flagged_as_truncated(cfg):
    result = Database(cfg).execute("SELECT id FROM order_items LIMIT 50")
    assert len(result.rows) == 50
    assert result.truncated is False


@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET status = 'hacked'",
    "INSERT INTO products VALUES (999, 'X', 'x', 'x', 1, 1)",
    "DROP TABLE invoices",
    "CREATE TABLE pwned (a INT)",
])
def test_connection_is_read_only_even_without_the_guard(cfg, demo_dir, sql):
    db = Database(cfg)
    with pytest.raises(QueryFailed) as info:
        db.execute(sql)
    assert "readonly" in str(info.value).lower() or "read-only" in str(info.value).lower()

    raw = sqlite3.connect(demo_dir / "erp.db")
    try:
        assert raw.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1500
        assert raw.execute("SELECT COUNT(*) FROM orders WHERE status = 'hacked'").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 1196
        assert raw.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'pwned'").fetchone()[0] == 0
    finally:
        raw.close()


def test_runaway_query_is_cancelled(demo_dir, tmp_path):
    cfg = dataclasses.replace(make_config(demo_dir, timeout_seconds=0.5), audit_path=tmp_path / "a.jsonl")
    db = Database(cfg)
    started = time.perf_counter()
    with pytest.raises(QueryTimeout):
        db.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n")
    assert time.perf_counter() - started < 3

    # The gateway is still usable afterwards.
    assert db.execute("SELECT COUNT(*) FROM products").rows == [[40]]


def test_schema_covers_tables_outside_the_allowlist(cfg):
    db = Database(cfg)
    assert "employees" in db.known_tables          # known, so a CTE cannot shadow it
    assert [c.name for c in db.tables["customers"].columns][:3] == ["id", "name", "segment"]
    assert db.tables["orders"].foreign_keys == ("customer_id -> customers(id)",)


def test_startup_fails_on_a_misspelled_restricted_column(cfg):
    access = Access(
        allowed_tables=cfg.access.allowed_tables,
        restricted_columns={"customers": frozenset({"emial"})},
        allowed_functions=cfg.access.allowed_functions,
    )
    with pytest.raises(ConfigError, match="emial"):
        Database(dataclasses.replace(cfg, access=access)).validate_config()


def test_startup_fails_on_an_unknown_allowed_table(cfg):
    access = dataclasses.replace(cfg.access, allowed_tables=cfg.access.allowed_tables | {"ordres"})
    with pytest.raises(ConfigError, match="ordres"):
        Database(dataclasses.replace(cfg, access=access)).validate_config()


def test_config_without_an_allowlist_is_refused(demo_dir):
    from safequery_mcp.config import parse_config

    with pytest.raises(ConfigError, match="allowed_tables"):
        parse_config({"database": {"url": "sqlite:///erp.db", "dialect": "sqlite"}}, base=demo_dir)


def test_cells_are_json_safe():
    assert _cell(b"\x00\x01\x02", 100) == "<3 bytes>"
    assert _cell("x" * 20, 10) == "x" * 10 + "...[truncated]"
    assert _cell(float("nan"), 10) == "nan"
    assert _cell(None, 10) is None
