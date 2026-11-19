"""End-to-end demo: launch the gateway as a subprocess, the way Claude does,
talk to it over stdio with a real MCP client, and print what happens.

Run:  python demo/run_demo.py
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.yaml"
AUDIT = HERE / "audit" / "audit.jsonl"

QUESTIONS = [
    ("Revenue by product category (completed orders)",
     "SELECT p.category, ROUND(SUM(oi.quantity * oi.unit_price * (1 - oi.discount)), 2) AS revenue "
     "FROM order_items oi JOIN products p ON p.id = oi.product_id "
     "JOIN orders o ON o.id = oi.order_id WHERE o.status = 'completed' "
     "GROUP BY p.category ORDER BY revenue DESC"),
    ("Top 5 customers by unpaid invoices",
     "SELECT c.name, c.segment, COUNT(*) AS unpaid, ROUND(SUM(i.amount), 2) AS outstanding "
     "FROM invoices i JOIN orders o ON o.id = i.order_id JOIN customers c ON c.id = o.customer_id "
     "WHERE i.paid_at IS NULL GROUP BY c.name, c.segment ORDER BY outstanding DESC LIMIT 5"),
]

ATTEMPTS = [
    ("Delete data", "DELETE FROM orders"),
    ("Smuggle a second statement", "SELECT 1; DROP TABLE orders"),
    ("Read the payroll table", "SELECT name, salary FROM employees"),
    ("Reach payroll through a subquery", "SELECT id FROM orders WHERE id IN (SELECT id FROM employees)"),
    ("Read a restricted column", "SELECT name, email FROM customers"),
    ("Take everything with SELECT *", "SELECT * FROM customers"),
    ("Hide the table behind a WITH name",
     "WITH a AS (SELECT * FROM employees), employees AS (SELECT 1 AS x) SELECT * FROM a"),
    ("Read the database's own catalogue", "SELECT sql FROM sqlite_master"),
]


def show_table(data: dict) -> None:
    columns, rows = data["columns"], data["rows"]
    widths = [max(len(str(c)), *(len(str(r[i])) for r in rows)) for i, c in enumerate(columns)]
    print("    " + "  ".join(str(c).ljust(w) for c, w in zip(columns, widths)))
    print("    " + "  ".join("-" * w for w in widths))
    for row in rows:
        print("    " + "  ".join(str(v).ljust(w) for v, w in zip(row, widths)))


async def main() -> int:
    if AUDIT.parent.exists():
        shutil.rmtree(AUDIT.parent)   # start the demo from an empty audit log

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "safequery_mcp", "serve", "--config", str(CONFIG)],
    )
    leaks = 0
    async with Client(params) as client:
        tools = (await client.list_tools()).tools
        print(f"Connected over stdio. Tools: {', '.join(t.name for t in tools)}\n")

        print("1. What can the assistant see?")
        listing = (await client.call_tool("list_tables", {})).structured_content
        for t in listing["tables"]:
            flag = "  (has restricted columns)" if t["has_restricted_columns"] else ""
            print(f"    {t['table']:<12} {t['description']}{flag}")
        print("    -> `employees` exists in the database and is not listed.\n")

        described = (await client.call_tool("describe_table", {"table": "customers"})).structured_content
        restricted = [c["name"] for c in described["columns"] if c["restricted"]]
        print(f"    customers: restricted columns = {', '.join(restricted)}\n")

        print("2. Ordinary business questions work")
        for title, sql in QUESTIONS:
            result = await client.call_tool("run_query", {"sql": sql})
            data = result.structured_content
            print(f"  {title}  ({data['elapsed_ms']} ms)")
            show_table(data)
            print()

        print("3. A large result is capped, and says so")
        data = (await client.call_tool("run_query", {"sql": "SELECT id, order_id, quantity FROM order_items"})).structured_content
        print(f"    4,421 rows in the table -> {data['row_count']} returned, truncated={data['truncated']}\n")

        print("4. Everything else is refused, with a reason the assistant can act on")
        for title, sql in ATTEMPTS:
            result = await client.call_tool("run_query", {"sql": sql})
            message = result.content[0].text.replace("Error executing tool run_query: ", "")
            if not result.is_error:
                leaks += 1
                message = "!! NOT BLOCKED"
            first_sentence = message.split(". ")[0].rstrip(".")
            print(f"  {title}\n    SQL: {sql}\n    ->   {first_sentence}.")
        print()

    entries = [json.loads(line) for line in AUDIT.read_text().splitlines()]
    counts: dict[str, int] = {}
    for e in entries:
        counts[e["decision"]] = counts.get(e["decision"], 0) + 1
    print(f"5. Audit log: {len(entries)} entries {counts}")
    last = entries[-1]
    print(f"    last entry: {last['ts']}  {last['tool']}  {last['decision']}  [{last.get('code', '')}]")

    from safequery_mcp.audit import verify

    print(f"    hash chain intact: {verify(AUDIT).ok}")

    tampered = AUDIT.read_text().replace("DELETE FROM orders", "SELECT 1", 1)
    scratch = AUDIT.with_name("tampered-copy.jsonl")
    scratch.write_text(tampered)
    result = verify(scratch)
    print(f"    after editing one entry in a copy: intact={result.ok}, first bad line={result.bad_line}")
    scratch.unlink()

    return 1 if leaks else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
