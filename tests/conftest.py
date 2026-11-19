from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "demo"))

from build_demo_db import build  # noqa: E402

from safequery_mcp.config import Config, parse_config  # noqa: E402

ALLOWED = ["customers", "products", "orders", "order_items", "invoices"]
RESTRICTED = {"customers": ["email", "phone", "tax_id"]}


def make_config(base: Path, dialect: str = "sqlite", url: str | None = None, **limits) -> Config:
    return parse_config(
        {
            "database": {"url": url or "sqlite:///erp.db", "dialect": dialect},
            "limits": {"max_rows": 50, "timeout_seconds": 5, "max_cell_chars": 500, **limits},
            "access": {"allowed_tables": ALLOWED, "restricted_columns": RESTRICTED},
            "audit": {"path": "audit/audit.jsonl"},
        },
        base=base,
    )


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("erp")
    build(base / "erp.db")
    return base


@pytest.fixture
def cfg(demo_dir, tmp_path) -> Config:
    """SQLite config with a fresh audit log per test."""
    import dataclasses

    return dataclasses.replace(make_config(demo_dir), audit_path=tmp_path / "audit.jsonl")
