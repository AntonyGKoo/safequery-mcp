"""Policy tests. These need no database: the guard works on the parsed SQL alone."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_config
from safequery_mcp.guard import QueryRejected, check

BASE = Path(__file__).parent
KNOWN = frozenset({"customers", "products", "orders", "order_items", "invoices", "employees"})


def cfg_for(dialect: str):
    return make_config(BASE, dialect=dialect, url="sqlite:///unused.db")


def allowed(sql: str, dialect: str = "sqlite"):
    return check(sql, cfg_for(dialect), known_tables=KNOWN)


def rejected(sql: str, dialect: str = "sqlite") -> str:
    with pytest.raises(QueryRejected) as info:
        check(sql, cfg_for(dialect), known_tables=KNOWN)
    return info.value.code


# -- queries that must pass ------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT id, name FROM products",
    "SELECT COUNT(*) FROM customers",
    "SELECT segment, COUNT(*) AS n FROM customers GROUP BY segment",
    "SELECT c.name, SUM(oi.quantity * oi.unit_price) AS revenue FROM customers c "
    "JOIN orders o ON o.customer_id = c.id JOIN order_items oi ON oi.order_id = o.id "
    "GROUP BY c.name ORDER BY revenue DESC LIMIT 5",
    "WITH late AS (SELECT order_id FROM invoices WHERE paid_at IS NULL) SELECT COUNT(*) FROM late",
    "SELECT name FROM products WHERE id IN (SELECT product_id FROM order_items)",
    "SELECT sku FROM products UNION SELECT status FROM orders",
    "SELECT * FROM orders",
    "SELECT julianday(paid_at) - julianday(issued_at) FROM invoices",
    "SELECT 1;",
])
def test_read_queries_pass(sql):
    assert allowed(sql).sql


def test_verdict_lists_real_tables_not_ctes():
    verdict = allowed("WITH x AS (SELECT id FROM orders) SELECT COUNT(*) FROM x JOIN products p ON 1 = 1")
    assert set(verdict.tables) == {"orders", "products"}


def test_executed_sql_is_rerendered_without_comments():
    verdict = allowed("/* note */ SELECT id FROM orders -- trailing")
    assert verdict.sql == "SELECT id FROM orders"


# -- writes and commands ---------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "DELETE FROM orders",
    "UPDATE orders SET status = 'x'",
    "INSERT INTO orders VALUES (1, 1, 'x', 'x')",
    "DROP TABLE orders",
    "CREATE TABLE t (a INT)",
    "ALTER TABLE orders ADD COLUMN x INT",
    "PRAGMA table_info(customers)",
    "ATTACH DATABASE 'other.db' AS other",
    "EXPLAIN SELECT 1",
    "VACUUM",
])
def test_non_select_statements_are_rejected(sql):
    assert rejected(sql) == "not_a_select"


def test_multiple_statements_are_rejected():
    assert rejected("SELECT 1; DROP TABLE orders") == "multiple_statements"
    assert rejected("SELECT 1; SELECT 2") == "multiple_statements"


def test_empty_and_oversized_input():
    assert rejected("   ") == "empty"
    assert rejected("SELECT " + "1," * 20_000 + "1") == "too_long"


def test_garbage_is_rejected():
    assert rejected("SELEC id FRM orders") in {"parse_error", "not_a_select"}


def test_analysis_failure_rejects_instead_of_passing():
    # Deep nesting overflows the parser's recursion. That must be a rejection, not a crash
    # and never a pass.
    assert rejected("SELECT " + "(" * 3000 + "1" + ")" * 3000) == "too_complex"


def test_trailing_comment_after_semicolon_is_not_a_second_statement():
    assert allowed("SELECT id FROM orders; -- done").sql == "SELECT id FROM orders"
    assert rejected("SELECT id FROM orders; -- done\nDROP TABLE orders") == "multiple_statements"


# -- tables ----------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM employees",
    "SELECT salary FROM employees",
    "SELECT o.id FROM orders o JOIN employees e ON e.id = o.id",
    "SELECT id FROM orders WHERE id IN (SELECT id FROM employees)",
    "SELECT * FROM sqlite_master",
    "SELECT * FROM main.employees",
    "SELECT * FROM main.orders",          # qualified names must be allowlisted as written
    "SELECT * FROM other.main.orders",
])
def test_tables_outside_the_allowlist_are_rejected(sql):
    assert rejected(sql) == "table_not_allowed"


@pytest.mark.parametrize("sql", [
    # In SQL a CTE is not visible to the CTEs before it, so `employees` in `a`
    # is the real payroll table. A naive "skip CTE names" check would let it through.
    "WITH a AS (SELECT * FROM employees), employees AS (SELECT 1 AS x) SELECT * FROM a",
    "WITH employees AS (SELECT 1 AS x) SELECT x FROM employees",
    "WITH customers AS (SELECT 1 AS x) SELECT x FROM customers",
    "WITH sqlite_master AS (SELECT 1 AS a) SELECT a FROM sqlite_master",
])
def test_cte_cannot_shadow_a_real_table(sql):
    assert rejected(sql) == "cte_name_conflict"


def test_unknown_functions_are_rejected():
    assert rejected("SELECT load_extension('evil')") == "function_not_allowed"
    assert rejected("SELECT randomblob(1000000000)") == "function_not_allowed"


# -- restricted columns ----------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT email FROM customers",
    "SELECT EMAIL FROM customers",
    'SELECT "email" FROM customers',
    "SELECT [email] FROM customers",
    "SELECT c.email AS e FROM customers c",
    "SELECT substr(phone, 1, 3) FROM customers",
    "SELECT id FROM customers WHERE email LIKE 'a%'",       # no probing through filters
    "SELECT id FROM customers ORDER BY tax_id",             # or through ordering
    "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.phone = '1'",
    "WITH x AS (SELECT email FROM customers) SELECT * FROM x",
])
def test_restricted_columns_cannot_be_referenced(sql):
    assert rejected(sql) in {"restricted_column", "star_on_restricted_table"}


@pytest.mark.parametrize("sql", [
    "SELECT * FROM customers",
    "SELECT c.* FROM customers c",
    "SELECT * FROM (SELECT * FROM customers) t",
    "SELECT o.*, c.name FROM orders o JOIN customers c ON c.id = o.customer_id",
])
def test_star_is_rejected_when_a_restricted_table_is_read(sql):
    assert rejected(sql) == "star_on_restricted_table"


def test_rejection_message_lists_the_readable_columns():
    columns = {"customers": ["id", "name", "segment", "country", "email", "phone", "tax_id", "created_at"]}
    with pytest.raises(QueryRejected) as info:
        check("SELECT * FROM customers", cfg_for("sqlite"), known_tables=KNOWN, columns=columns)
    assert "id, name, segment, country, created_at" in info.value.message
    assert "email" not in info.value.message.split("Readable columns:")[1]


# -- other dialects (parsing only; no server database is available in CI) --------

@pytest.mark.parametrize("sql,code", [
    ("WITH d AS (DELETE FROM orders RETURNING *) SELECT * FROM d", "write_or_command"),
    ("SELECT * INTO backup FROM orders", "write_or_command"),
    ("SELECT id FROM orders FOR UPDATE", "write_or_command"),
    ("COPY orders TO '/tmp/out.csv'", "not_a_select"),
    ("SELECT pg_read_file('/etc/passwd')", "function_not_allowed"),
    ("SELECT pg_sleep(600)", "function_not_allowed"),
    ("SELECT query_to_xml('select * from employees', true, true, '')", "function_not_allowed"),
    ("SELECT row_to_json(o) FROM orders o", "function_not_allowed"),
    ("SELECT c FROM customers c", "whole_row_reference"),
    ("SELECT (c).email FROM customers c", "restricted_column"),
    ("SELECT * FROM pg_catalog.pg_shadow", "table_not_allowed"),
    ("SELECT * FROM information_schema.tables", "table_not_allowed"),
    ("SELECT * FROM pg_user", "table_not_allowed"),
])
def test_postgres_escapes_are_rejected(sql, code):
    assert rejected(sql, "postgres") == code


def test_postgres_unicode_identifier_cannot_reach_the_database_intact():
    # U&"\0065mail" is Postgres syntax for the identifier `email`. Whatever the
    # parser makes of it, the re-rendered statement must not preserve that form.
    try:
        verdict = allowed('SELECT U&"\\0065mail" FROM customers', "postgres")
    except QueryRejected:
        return
    assert 'U&"' not in verdict.sql


@pytest.mark.parametrize("sql,code", [
    ("EXEC xp_cmdshell 'dir'", "not_a_select"),
    ("SELECT name INTO #scratch FROM products", "write_or_command"),
    ("SELECT * FROM sys.tables", "table_not_allowed"),
    ("SELECT * FROM sysobjects", "table_not_allowed"),
    ("SELECT @@VERSION", "expression_not_allowed"),
])
def test_sqlserver_escapes_are_rejected(sql, code):
    assert rejected(sql, "tsql") == code


def test_sqlserver_openrowset_is_rejected():
    assert rejected("SELECT * FROM OPENROWSET('SQLNCLI', 'x', 'select 1')", "tsql") in {
        "function_not_allowed", "table_not_allowed",
    }


def test_sqlserver_top_and_brackets_pass():
    assert allowed("SELECT TOP 5 [name] FROM products ORDER BY id", "tsql").sql


@pytest.mark.parametrize("sql,code", [
    ("SELECT load_file('/etc/passwd')", "function_not_allowed"),
    ("SELECT sleep(600)", "function_not_allowed"),
    ("SELECT * FROM mysql.user", "table_not_allowed"),
])
def test_mysql_escapes_are_rejected(sql, code):
    assert rejected(sql, "mysql") == code


def test_mysql_conditional_comment_is_not_executed():
    # MySQL runs the body of /*! ... */ comments. The parser sees a comment, so the
    # body must not survive into the statement that is sent to the database.
    verdict = allowed("SELECT id FROM orders /*!50000 UNION SELECT salary FROM employees */", "mysql")
    assert "employees" not in verdict.sql
