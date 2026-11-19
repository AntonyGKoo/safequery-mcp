"""Build a small synthetic ERP database for the demo and the tests.

All data is generated from a fixed seed. No real people or companies.
Run:  python demo/build_demo_db.py
"""

from __future__ import annotations

import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE customers (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    segment     TEXT NOT NULL,
    country     TEXT NOT NULL,
    email       TEXT NOT NULL,
    phone       TEXT NOT NULL,
    tax_id      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE products (
    id          INTEGER PRIMARY KEY,
    sku         TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    unit_price  REAL NOT NULL,
    unit_cost   REAL NOT NULL
);
CREATE TABLE orders (
    id          INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    order_date  TEXT NOT NULL,
    status      TEXT NOT NULL
);
CREATE TABLE order_items (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    product_id  INTEGER NOT NULL REFERENCES products(id),
    quantity    INTEGER NOT NULL,
    unit_price  REAL NOT NULL,
    discount    REAL NOT NULL DEFAULT 0
);
CREATE TABLE invoices (
    id          INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL REFERENCES orders(id),
    issued_at   TEXT NOT NULL,
    due_at      TEXT NOT NULL,
    paid_at     TEXT,
    amount      REAL NOT NULL
);
-- Payroll lives in the same database but is NOT on the gateway allowlist.
CREATE TABLE employees (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    department  TEXT NOT NULL,
    salary      REAL NOT NULL,
    national_id TEXT NOT NULL
);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_invoices_order ON invoices(order_id);
"""

SEGMENTS = ["Enterprise", "Mid-market", "Small business"]
COUNTRIES = ["US", "UK", "DE", "FR", "NL", "SG", "AU", "CA"]
CATEGORIES = {
    "Hardware": (120, 900),
    "Software": (40, 400),
    "Services": (200, 1500),
    "Accessories": (8, 60),
}
STATUSES = ["completed"] * 8 + ["cancelled", "pending"]
DEPARTMENTS = ["Sales", "Finance", "Operations", "Engineering"]


def build(path: Path, seed: int = 7) -> None:
    rng = random.Random(seed)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    customers = []
    for i in range(1, 121):
        created = date(2024, 1, 1) + timedelta(days=rng.randrange(0, 600))
        customers.append((
            i,
            f"Customer {i:03d} Ltd",
            rng.choice(SEGMENTS),
            rng.choice(COUNTRIES),
            f"billing{i:03d}@customer{i:03d}.example",
            f"+1-555-{rng.randrange(100, 999)}-{rng.randrange(1000, 9999)}",
            f"TX-{rng.randrange(10**8, 10**9)}",
            created.isoformat(),
        ))
    conn.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)

    products = []
    pid = 1
    for category, (low, high) in CATEGORIES.items():
        for n in range(1, 11):
            price = round(rng.uniform(low, high), 2)
            cost = round(price * rng.uniform(0.35, 0.75), 2)
            products.append((pid, f"{category[:3].upper()}-{n:03d}", f"{category} item {n}", category, price, cost))
            pid += 1
    conn.executemany("INSERT INTO products VALUES (?,?,?,?,?,?)", products)

    orders, items, invoices = [], [], []
    item_id = invoice_id = 1
    start = date(2025, 1, 1)
    for oid in range(1, 1501):
        customer_id = rng.randrange(1, 121)
        order_date = start + timedelta(days=rng.randrange(0, 680))
        status = rng.choice(STATUSES)
        orders.append((oid, customer_id, order_date.isoformat(), status))
        total = 0.0
        for _ in range(rng.randrange(1, 6)):
            product = rng.choice(products)
            qty = rng.randrange(1, 12)
            discount = rng.choice([0, 0, 0, 0.05, 0.1, 0.15])
            items.append((item_id, oid, product[0], qty, product[4], discount))
            total += qty * product[4] * (1 - discount)
            item_id += 1
        if status == "completed":
            issued = order_date + timedelta(days=rng.randrange(0, 4))
            due = issued + timedelta(days=30)
            paid = None
            if rng.random() < 0.82:
                paid = (issued + timedelta(days=rng.randrange(3, 75))).isoformat()
            invoices.append((invoice_id, oid, issued.isoformat(), due.isoformat(), paid, round(total, 2)))
            invoice_id += 1
    conn.executemany("INSERT INTO orders VALUES (?,?,?,?)", orders)
    conn.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
    conn.executemany("INSERT INTO invoices VALUES (?,?,?,?,?,?)", invoices)

    employees = [
        (i, f"Employee {i:02d}", rng.choice(DEPARTMENTS), round(rng.uniform(48_000, 160_000), 2),
         f"ID-{rng.randrange(10**8, 10**9)}")
        for i in range(1, 26)
    ]
    conn.executemany("INSERT INTO employees VALUES (?,?,?,?,?)", employees)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "erp_demo.db"
    build(target)
    print(f"built {target}")
