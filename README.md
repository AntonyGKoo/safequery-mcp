# safequery-mcp

Let an AI assistant answer questions from your business database without being able to change it, see the tables you keep private, or act without leaving a record.

`safequery-mcp` is a [Model Context Protocol](https://modelcontextprotocol.io) server that sits between an assistant such as Claude and a SQL database. The assistant asks in SQL; the gateway decides whether the question is allowed, runs it on a read-only connection, caps the answer, and writes every call to an audit log.

It exists for one recurring request: *"connect Claude to our ERP database, safely."*

## What it guarantees

| Guarantee | How it is enforced |
|---|---|
| Nothing can be written or changed | Only a single `SELECT` passes the policy check, and the connection itself is opened read-only, so a write fails even if the check were bypassed |
| Only the tables you list are visible | An explicit allowlist. The gateway refuses to start without one. Subqueries, joins and `WITH` clauses are checked too |
| Sensitive columns stay private | Per-table restricted columns cannot be selected, filtered on, sorted by or reached through `SELECT *` |
| Answers are bounded | Row cap, per-query time limit, and a cap on the size of each value |
| Every call is on the record | Allowed, blocked and failed calls are appended to a hash-chained audit log; editing or deleting an entry is detectable |

## See it run

`demo/run_demo.py` starts the gateway as a subprocess, the way Claude does, and talks to it over stdio with a real MCP client. The database is a synthetic ERP (120 customers, 1,500 orders, 4,421 order lines, 1,196 invoices) with a payroll table that is deliberately left off the allowlist. This is its actual output, shortened:

```text
Connected over stdio. Tools: list_tables, describe_table, run_query

1. What can the assistant see?
    customers    One row per customer account.  (has restricted columns)
    invoices     One invoice per completed order. paid_at is NULL while unpaid.
    order_items  Order lines. Line revenue = quantity * unit_price * (1 - discount).
    orders       Order headers. status is completed, pending or cancelled.
    products     Product catalogue with list price and unit cost.
    -> `employees` exists in the database and is not listed.

    customers: restricted columns = email, phone, tax_id

2. Ordinary business questions work
  Top 5 customers by unpaid invoices  (0.8 ms)
    name              segment     unpaid  outstanding
    ----------------  ----------  ------  -----------
    Customer 037 Ltd  Mid-market  3       48576.72
    Customer 045 Ltd  Enterprise  5       46080.39
    Customer 020 Ltd  Mid-market  4       41696.68
    Customer 066 Ltd  Enterprise  7       41216.86
    Customer 100 Ltd  Mid-market  5       41103.58

3. A large result is capped, and says so
    4,421 rows in the table -> 50 returned, truncated=True

4. Everything else is refused, with a reason the assistant can act on
  Delete data
    SQL: DELETE FROM orders
    ->   [not_a_select] Only SELECT queries are allowed; received a DELETE statement.
  Smuggle a second statement
    SQL: SELECT 1; DROP TABLE orders
    ->   [multiple_statements] Exactly one statement is allowed per call; received 2.
  Read the payroll table
    SQL: SELECT name, salary FROM employees
    ->   [table_not_allowed] Table 'employees' is not available through this gateway.
  Reach payroll through a subquery
    SQL: SELECT id FROM orders WHERE id IN (SELECT id FROM employees)
    ->   [table_not_allowed] Table 'employees' is not available through this gateway.
  Read a restricted column
    SQL: SELECT name, email FROM customers
    ->   [restricted_column] Column 'email' is restricted and cannot be read or used in a query that touches customers.
  Take everything with SELECT *
    SQL: SELECT * FROM customers
    ->   [star_on_restricted_table] SELECT * is not allowed in a query that reads customers, because some of its columns are restricted.
  Hide the table behind a WITH name
    SQL: WITH a AS (SELECT * FROM employees), employees AS (SELECT 1 AS x) SELECT * FROM a
    ->   [cte_name_conflict] The WITH name 'employees' collides with an existing or reserved table name.
  Read the database's own catalogue
    SQL: SELECT sql FROM sqlite_master
    ->   [table_not_allowed] Table 'sqlite_master' is not available through this gateway.

5. Audit log: 13 entries {'allowed': 5, 'blocked': 8}
    hash chain intact: True
    after editing one entry in a copy: intact=False, first bad line=6
```

## How it works

```text
assistant ──MCP/stdio──▶ 1. policy check ──▶ 2. read-only connection ──▶ database
                            on parsed SQL        row cap, time limit
                                │                        │
                                └────── audit log ◀──────┘   (every call, allowed or not)
```

**Layer 1, the policy check**, parses the SQL into a syntax tree and inspects the tree, never the raw text. It requires exactly one `SELECT`, rejects any writing or session-changing construct anywhere in the tree, checks every table reference against the allowlist, refuses functions it does not recognise, and applies the column restrictions.

The statement that is then executed is re-rendered from that same tree, with comments removed. A comment or quoting trick that makes the database read the text differently from the parser, such as MySQL's executable `/*! ... */` comments, does not survive into the statement that is sent.

**Layer 2, the connection**, is opened read-only. Rows are fetched up to the cap plus one, so the gateway knows whether to flag `truncated` without pulling the full result. A timer cancels queries that run past the limit.

If the policy check itself fails, for example on pathologically nested input, the statement is rejected. Analysis errors never let SQL through.

## Quick start

```bash
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python demo/build_demo_db.py     # builds demo/erp_demo.db from a fixed seed
.venv/bin/python demo/run_demo.py          # the transcript above
.venv/bin/python -m pytest                 # 108 tests
```

Connect it to Claude Code:

```bash
claude mcp add safequery -- /absolute/path/.venv/bin/safequery-mcp serve --config /absolute/path/demo/config.yaml
```

Or to Claude Desktop, in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "safequery": {
      "command": "/absolute/path/.venv/bin/safequery-mcp",
      "args": ["serve", "--config", "/absolute/path/demo/config.yaml"]
    }
  }
}
```

Then ask, for example: *"Which customer segment has the most unpaid invoices, and how much is outstanding?"*

## Configuration

One YAML file. See [`config.example.yaml`](config.example.yaml) for every option.

```yaml
database:
  url: postgresql+psycopg://readonly_user:password@host:5432/erp
  dialect: postgres            # sqlite | postgres | tsql | mysql

limits:
  max_rows: 200
  timeout_seconds: 10

access:
  allowed_tables: [customers, orders, order_items, products, invoices]
  restricted_columns:
    customers: [email, phone, tax_id]

audit:
  path: audit/audit.jsonl
```

The gateway checks the policy against the live schema at startup. A table or restricted column that does not exist is a startup error, because a typo in a restricted column name would otherwise leave that column exposed.

## Tools the assistant gets

| Tool | Purpose |
|---|---|
| `list_tables` | The allowed tables, each with a description and whether it has restricted columns |
| `describe_table` | Columns, types, keys, and which columns are restricted |
| `run_query` | Run one `SELECT`; returns columns, rows, `row_count`, `truncated`, `elapsed_ms` |

All three are annotated read-only. A refusal comes back as a tool error with a code and a sentence the assistant can act on, including the columns it *may* read.

## Command line

```bash
safequery-mcp serve        --config config.yaml              # run the server on stdio
safequery-mcp check        --config config.yaml "SELECT ..." # would this be allowed? (does not run it)
safequery-mcp verify-audit --config config.yaml              # check the audit log's hash chain
```

## What has been tested, and what has not

- **SQLite: tested end to end.** 108 tests cover the policy, the read-only connection (writes are attempted with the policy check skipped, and fail), the row cap, the time limit, the audit chain, and full round trips through the MCP protocol. The demo additionally runs over a real stdio subprocess.
- **PostgreSQL, SQL Server, MySQL: policy tested, connection not.** 25 policy cases cover dialect-specific escapes such as data-modifying `WITH`, `SELECT INTO`, `COPY`, `pg_read_file`, `query_to_xml`, whole-row references, `xp_cmdshell`, `OPENROWSET` and `load_file`. The code that connects to these servers and sets their session limits is written but has not been run against a live server. Treat it as unverified until it has been tested against yours.

## Limits you should know about

- **Use a read-only database login as well.** The policy check is one layer. Give the gateway a login that can only `SELECT` from the allowed tables. On PostgreSQL and SQL Server, column-level grants can enforce the restricted columns inside the database too.
- **Column restriction is by name, and deliberately blunt.** In a query that touches a table with restricted columns, any identifier with a restricted column's name is refused, even if it belongs to another table or is only an alias. This costs some legitimate queries and keeps the rule simple enough to trust.
- **It does not stop inference from allowed data.** Restricted columns cannot be filtered or sorted on, but the gateway does not reason about what can be deduced from the columns you do allow.
- **Unknown functions are refused.** Standard SQL functions work. Vendor-specific ones must be added to `allowed_functions`.
- **The row cap limits what is returned, not what the database computes.** The time limit is what bounds an expensive query.
- **The audit log is tamper-evident, not tamper-proof.** Someone who can rewrite the whole file can rebuild the chain. Ship the log somewhere the gateway cannot modify if that matters to you.
- **The audit log stores query text**, which can contain literal values the assistant typed.
- **stdio only.** The gateway runs on the same machine as the assistant client. A remote deployment over HTTP with authentication is not included.

## Layout

```text
src/safequery_mcp/
  config.py    what may be read, and how much
  guard.py     the policy check on parsed SQL
  engine.py    read-only connection, row cap, time limit, schema
  audit.py     hash-chained audit log
  server.py    the three MCP tools
tests/         108 tests
demo/          synthetic ERP database, config, end-to-end demo
```

## License

MIT
