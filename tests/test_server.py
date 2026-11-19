"""End-to-end tests through the MCP protocol, with an in-process client."""

from __future__ import annotations

import asyncio
import json

from mcp import Client

from safequery_mcp.audit import verify
from safequery_mcp.server import build_server


def call(cfg, *calls):
    """Open one client session, run the given (tool, arguments) calls, return the results."""

    async def run():
        async with Client(build_server(cfg)) as client:
            return [await client.call_tool(name, args) for name, args in calls]

    return asyncio.run(run())


def test_exposes_exactly_three_read_only_tools(cfg):
    async def run():
        async with Client(build_server(cfg)) as client:
            return (await client.list_tools()).tools

    tools = asyncio.run(run())
    assert {t.name for t in tools} == {"list_tables", "describe_table", "run_query"}
    assert all(t.annotations.read_only_hint is True for t in tools)
    assert all(t.annotations.destructive_hint is False for t in tools)


def test_list_tables_hides_tables_outside_the_allowlist(cfg):
    (result,) = call(cfg, ("list_tables", {}))
    names = [t["table"] for t in result.structured_content["tables"]]
    assert names == ["customers", "invoices", "order_items", "orders", "products"]
    assert "employees" not in json.dumps(result.structured_content)


def test_describe_table_marks_restricted_columns(cfg):
    (result,) = call(cfg, ("describe_table", {"table": "customers"}))
    restricted = {c["name"] for c in result.structured_content["columns"] if c["restricted"]}
    assert restricted == {"email", "phone", "tax_id"}


def test_describe_table_refuses_hidden_tables(cfg):
    (result,) = call(cfg, ("describe_table", {"table": "employees"}))
    assert result.is_error
    assert "[table_not_allowed]" in result.content[0].text


def test_run_query_returns_structured_rows(cfg):
    sql = (
        "SELECT p.category, ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)), 2) AS revenue "
        "FROM order_items oi JOIN products p ON p.id = oi.product_id "
        "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed' "
        "GROUP BY p.category ORDER BY revenue DESC"
    )
    (result,) = call(cfg, ("run_query", {"sql": sql}))
    assert not result.is_error
    data = result.structured_content
    assert data["columns"] == ["category", "revenue"]
    assert data["row_count"] == 4 and data["truncated"] is False
    assert data["rows"][0][1] > data["rows"][-1][1]


def test_blocked_queries_come_back_as_tool_errors_with_a_reason(cfg):
    results = call(
        cfg,
        ("run_query", {"sql": "DELETE FROM orders"}),
        ("run_query", {"sql": "SELECT * FROM employees"}),
        ("run_query", {"sql": "SELECT email FROM customers"}),
        ("run_query", {"sql": "SELECT 1; DROP TABLE orders"}),
    )
    assert all(r.is_error for r in results)
    codes = [r.content[0].text.split("[", 1)[1].split("]", 1)[0] for r in results]
    assert codes == ["not_a_select", "table_not_allowed", "restricted_column", "multiple_statements"]


def test_large_results_are_capped(cfg):
    (result,) = call(cfg, ("run_query", {"sql": "SELECT id, order_id FROM order_items"}))
    assert result.structured_content["row_count"] == 50
    assert result.structured_content["truncated"] is True


def test_database_errors_are_reported_cleanly(cfg):
    (result,) = call(cfg, ("run_query", {"sql": "SELECT no_such_column FROM orders"}))
    assert result.is_error
    assert "[database_error]" in result.content[0].text
    assert "no_such_column" in result.content[0].text


def test_every_call_is_audited_including_blocked_ones(cfg):
    call(
        cfg,
        ("list_tables", {}),
        ("describe_table", {"table": "orders"}),
        ("run_query", {"sql": "SELECT COUNT(*) FROM orders"}),
        ("run_query", {"sql": "SELECT salary FROM employees"}),
        ("run_query", {"sql": "SELECT no_such_column FROM orders"}),
    )
    entries = [json.loads(line) for line in cfg.audit_path.read_text().splitlines()]
    assert [(e["tool"], e["decision"]) for e in entries] == [
        ("list_tables", "allowed"),
        ("describe_table", "allowed"),
        ("run_query", "allowed"),
        ("run_query", "blocked"),
        ("run_query", "error"),
    ]
    assert entries[2]["rows"] == 1 and entries[2]["tables"] == ["orders"]
    assert entries[3]["code"] == "table_not_allowed" and "salary" in entries[3]["sql"]
    assert verify(cfg.audit_path).ok
