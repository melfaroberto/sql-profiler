"""Creates the demo database `shop_demo` (directly on MySQL, not through the profiler) with sample data.

    python demo/setup_db.py                 # 127.0.0.1:3306, user shop / password Shop-Demo-2026 (see mysql_user.sql)
    python demo/setup_db.py --port 3307 --user me --password secret
"""
import argparse
import hashlib
import random
from datetime import datetime, timedelta
from pathlib import Path

import mysql.connector

SCHEMA = Path(__file__).resolve().parent / "schema.sql"

USERS = [("admin", "admin", "Roberto Admin", "admin"), ("enrico", "enrico", "Enrico Clerk", "clerk"),
         ("guest", "guest", "Guest User", "viewer")]
CATEGORIES = [("PAPER", "Paper and notebooks"), ("PEN", "Pens and markers"), ("DESK", "Desk accessories")]
PRODUCTS = [("P-100", "A4 paper 500 sheets", "PAPER", 5.90, 40), ("P-101", "Notebook squared", "PAPER", 2.50, 3),
            ("P-200", "Blue ballpoint pen", "PEN", 0.90, 120), ("P-201", "Highlighter set", "PEN", 4.20, 8),
            ("P-300", "Stapler", "DESK", 12.00, 2), ("P-301", "Desk lamp", "DESK", 29.90, 6)]
CUSTOMERS = [("Rossi Srl", "info@rossi.it", "Roma"), ("Bianchi SpA", "acquisti@bianchi.it", "Milano"),
             ("Verdi & Co", "verdi@example.com", "Torino"), ("Studio Neri", "neri@example.com", "Roma"),
             ("Gialli Snc", "gialli@example.com", "Napoli")]


def password_hash(pw):
    return hashlib.sha256(pw.encode()).hexdigest()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=3306)
    p.add_argument("--user", default="shop")
    p.add_argument("--password", default="Shop-Demo-2026")
    p.add_argument("--database", default="shop_demo")
    a = p.parse_args()

    conn = mysql.connector.connect(host=a.host, port=a.port, user=a.user, password=a.password)
    cur = conn.cursor()
    cur.execute(f"DROP DATABASE IF EXISTS `{a.database}`")
    cur.execute(f"CREATE DATABASE `{a.database}` DEFAULT CHARSET utf8mb4")
    cur.execute(f"USE `{a.database}`")
    for statement in SCHEMA.read_text().split(";"):
        lines = [l for l in statement.splitlines() if not l.strip().startswith("--")]
        if "".join(lines).strip():
            cur.execute("\n".join(lines))

    cur.executemany("INSERT INTO users (username, password_hash, full_name, role) VALUES (%s, %s, %s, %s)",
                    [(u, password_hash(pw), n, r) for u, pw, n, r in USERS])
    cur.executemany("INSERT INTO categories VALUES (%s, %s)", CATEGORIES)
    cur.executemany("INSERT INTO products (sku, name, category_code, price, stock) VALUES (%s, %s, %s, %s, %s)", PRODUCTS)
    now = datetime.now().replace(microsecond=0)
    cur.executemany("INSERT INTO customers (name, email, city, created_at) VALUES (%s, %s, %s, %s)",
                    [(n, e, c, now - timedelta(days=90)) for n, e, c in CUSTOMERS])

    # some order history for the reports
    rnd = random.Random(7)
    for i in range(12):
        when = now - timedelta(days=rnd.randint(1, 80))
        cur.execute("INSERT INTO orders (customer_id, created_by, created_at, status) VALUES (%s, %s, %s, 'shipped')",
                    (rnd.randint(1, len(CUSTOMERS)), rnd.randint(1, 2), when))
        oid, total = cur.lastrowid, 0
        for line in range(1, rnd.randint(2, 3) + 1):
            pid = rnd.randint(1, len(PRODUCTS))
            qty, price = rnd.randint(1, 5), PRODUCTS[pid - 1][3]
            cur.execute("INSERT INTO order_lines VALUES (%s, %s, %s, %s, %s)", (oid, line, pid, qty, price))
            total += qty * price
        cur.execute("UPDATE orders SET total = %s WHERE id = %s", (round(total, 2), oid))
    conn.commit()
    conn.close()
    print(f"database `{a.database}` ready on {a.host}:{a.port} — log in to the app as admin/admin or enrico/enrico")


if __name__ == "__main__":
    main()
