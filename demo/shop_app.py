"""Mini Shop: a small order-management web application used to try the SQL Profiler on a "real" app.

It talks to MySQL like a typical legacy application: a permission check on every page, an audit log,
a mix of prepared statements (binary protocol) and plain queries, and a multi-statement transaction
when an order is saved.

    python demo/shop_app.py                      # http://127.0.0.1:8090, database through the profiler on :3317
    python demo/shop_app.py --db-port 3306       # straight to MySQL, bypassing the profiler

Log in as admin/admin or enrico/enrico.
"""
import argparse
import asyncio
import hashlib
import html
import secrets
import time
from datetime import datetime
from decimal import Decimal

import mysql.connector
from mysql.connector import pooling
from aiohttp import web

POOL = None
SESSIONS = {}           # cookie -> user id


# ====================================================================== database

class Db:
    """One pooled connection for the duration of a request."""

    def __enter__(self):
        self.conn = POOL.get_connection()
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.conn.rollback()
        self.conn.close()            # back to the pool (the pool resets the session)

    def one(self, sql, args=(), prepared=True):
        rows = self.all(sql, args, prepared)
        return rows[0] if rows else None

    def all(self, sql, args=(), prepared=True):
        cur = self.conn.cursor(prepared=True) if prepared else self.conn.cursor()
        cur.execute(sql, args)
        cols = [c[0] for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        cur.close()
        return rows

    def run(self, sql, args=(), prepared=True):
        cur = self.conn.cursor(prepared=True) if prepared else self.conn.cursor()
        cur.execute(sql, args)
        last, count = cur.lastrowid, cur.rowcount
        cur.close()
        return last, count

    def audit(self, user_id, action, entity=None, entity_id=None, details=None):
        self.run("INSERT INTO audit_log (ts, user_id, action, entity, entity_id, details) VALUES (?, ?, ?, ?, ?, ?)",
                 (datetime.now().replace(microsecond=0), user_id, action, entity, entity_id, details))


def blocking(fn):
    """Runs a handler body (which uses the blocking driver) in a thread."""
    async def handler(request):
        return await asyncio.to_thread(fn, request, await request.post() if request.method == "POST" else {})
    return handler


def current_user(request, db):
    uid = SESSIONS.get(request.cookies.get("shop_session", ""))
    if uid is None:
        raise web.HTTPFound("/login")
    # like many legacy apps: reload the user and its role on every page
    user = db.one("SELECT id, username, full_name, role FROM users WHERE id = ?", (uid,))
    if user is None:
        raise web.HTTPFound("/login")
    return user


# ====================================================================== HTML

STYLE = """
body{font-family:system-ui,sans-serif;margin:0;background:#f4f5f7;color:#222}
header{background:#1f4e79;color:#fff;padding:10px 20px;display:flex;gap:18px;align-items:center}
header a{color:#fff;text-decoration:none}header .sp{flex:1}
main{max-width:1000px;margin:20px auto;background:#fff;padding:20px 26px;border-radius:8px}
table{border-collapse:collapse;width:100%;margin:10px 0}td,th{border-bottom:1px solid #ddd;padding:6px;text-align:left}
th{background:#f0f2f5}input,select,button{font:inherit;padding:5px 8px}button{cursor:pointer}
.err{background:#fdecea;color:#b42318;padding:8px 12px;border-radius:6px}.ok{background:#e8f5e9;color:#2e7d32;padding:8px 12px;border-radius:6px}
.cards{display:flex;gap:14px}.card{flex:1;background:#e7eef6;border-radius:8px;padding:14px;font-size:14px}.card b{font-size:26px;display:block}
"""


def esc(v):
    return html.escape("" if v is None else str(v))


def page(title, body, user=None, msg=None, error=None):
    nav = ""
    if user:
        nav = (f'<a href="/">Dashboard</a><a href="/customers">Customers</a><a href="/orders/new">New order</a>'
               f'<a href="/reports">Reports</a><span class="sp"></span><span>{esc(user["full_name"])} ({esc(user["role"])})</span>'
               f'<a href="/logout">Logout</a>')
    note = (f'<p class="ok">{esc(msg)}</p>' if msg else "") + (f'<p class="err">{esc(error)}</p>' if error else "")
    return web.Response(content_type="text/html", text=f"""<!doctype html><html><head><meta charset="utf-8">
<title>{esc(title)} – Mini Shop</title><style>{STYLE}</style></head>
<body><header><b>🛒 Mini Shop</b>{nav}</header><main><h2>{esc(title)}</h2>{note}{body}</main></body></html>""")


def table(rows, cols, links=None):
    head = "".join(f"<th>{esc(c)}</th>" for c in cols)
    body = ""
    for r in rows:
        cells = []
        for c in cols:
            v = esc(r[c])
            if links and c in links:
                v = f'<a href="{links[c](r)}">{v}</a>'
            cells.append(f"<td>{v}</td>")
        body += "<tr>" + "".join(cells) + "</tr>"
    return f"<table><tr>{head}</tr>{body or '<tr><td>No rows</td></tr>'}</table>"


# ====================================================================== pages

@blocking
def login_form(request, form):
    return page("Login", """<form method="post">
        <p><input name="username" placeholder="username" autofocus></p>
        <p><input name="password" type="password" placeholder="password"></p>
        <button>Log in</button></form><p>Try admin/admin or enrico/enrico.</p>""")


@blocking
def login(request, form):
    with Db() as db:
        user = db.one("SELECT id, password_hash, role FROM users WHERE username = ?", (form.get("username", ""),))
        if not user or user["password_hash"] != hashlib.sha256(form.get("password", "").encode()).hexdigest():
            db.audit(None, "LOGIN_FAILED", "users", None, form.get("username", ""))
            db.conn.commit()
            return page("Login", '<p><a href="/login">Try again</a></p>', error="Wrong user name or password")
        db.run("UPDATE users SET last_login = NOW() WHERE id = ?", (user["id"],))
        db.audit(user["id"], "LOGIN", "users", user["id"])
        db.conn.commit()
    token = secrets.token_hex(16)
    SESSIONS[token] = user["id"]
    resp = web.HTTPFound("/")
    resp.set_cookie("shop_session", token)
    raise resp


@blocking
def logout(request, form):
    with Db() as db:
        uid = SESSIONS.pop(request.cookies.get("shop_session", ""), None)
        if uid:
            db.audit(uid, "LOGOUT", "users", uid)
            db.conn.commit()
    raise web.HTTPFound("/login")


@blocking
def dashboard(request, form):
    with Db() as db:
        user = current_user(request, db)
        counts = db.one("""SELECT (SELECT COUNT(*) FROM customers) AS customers,
                                  (SELECT COUNT(*) FROM orders WHERE status = 'open') AS open_orders,
                                  (SELECT COUNT(*) FROM products WHERE stock < reorder_level) AS low_stock""", prepared=False)
        recent = db.all("""SELECT o.id, o.created_at, c.name AS customer, o.status, o.total
                           FROM orders o JOIN customers c ON c.id = o.customer_id
                           ORDER BY o.created_at DESC LIMIT 8""", prepared=False)
    cards = "".join(f'<div class="card"><b>{counts[k]}</b>{label}</div>' for k, label in
                    (("customers", "customers"), ("open_orders", "open orders"), ("low_stock", "products to reorder")))
    return page("Dashboard", f'<div class="cards">{cards}</div><h3>Recent orders</h3>'
                + table(recent, ["id", "created_at", "customer", "status", "total"], {"id": lambda r: f"/orders/{r['id']}"}),
                user, request.query.get("msg"))


@blocking
def customers(request, form):
    q = request.query.get("q", "")
    with Db() as db:
        user = current_user(request, db)
        if q:
            rows = db.all("SELECT id, name, email, city FROM customers WHERE name LIKE ? OR city LIKE ? ORDER BY name",
                          (f"%{q}%", f"%{q}%"))
        else:
            rows = db.all("SELECT id, name, email, city FROM customers ORDER BY name", prepared=False)
    return page("Customers", f"""<form><input name="q" value="{esc(q)}" placeholder="search name or city">
        <button>Search</button></form>""" + table(rows, ["id", "name", "email", "city"],
                                                    {"name": lambda r: f"/customers/{r['id']}"})
                + """<h3>New customer</h3><form method="post" action="/customers/new">
        <input name="name" placeholder="name" required> <input name="email" placeholder="email">
        <input name="city" placeholder="city"> <button>Add</button></form>""", user)


@blocking
def customer_new(request, form):
    with Db() as db:
        user = current_user(request, db)
        cid, _ = db.run("INSERT INTO customers (name, email, city, created_at) VALUES (?, ?, ?, NOW())",
                        (form["name"], form.get("email") or None, form.get("city") or None))
        db.audit(user["id"], "CUSTOMER_CREATED", "customers", cid, form["name"])
        db.conn.commit()
    raise web.HTTPFound(f"/customers/{cid}")


@blocking
def customer_card(request, form):
    cid = int(request.match_info["id"])
    with Db() as db:
        user = current_user(request, db)
        c = db.one("SELECT * FROM customers WHERE id = ?", (cid,))
        if c is None:
            raise web.HTTPNotFound()
        orders = db.all("SELECT id, created_at, status, total FROM orders WHERE customer_id = ? ORDER BY created_at DESC", (cid,))
        stats = db.one("""SELECT COUNT(DISTINCT o.id) AS orders, COALESCE(SUM(l.qty * l.unit_price), 0) AS spent,
                                 MAX(o.created_at) AS last_order
                          FROM orders o JOIN order_lines l ON l.order_id = o.id
                          WHERE o.customer_id = ? AND o.status <> 'cancelled'""", (cid,))
        db.audit(user["id"], "CUSTOMER_VIEWED", "customers", cid)
        db.conn.commit()
    return page(c["name"], f"""<p>{esc(c['email'])} · {esc(c['city'])} · customer since {esc(c['created_at'])}</p>
        <p><b>{stats['orders']}</b> orders, <b>{stats['spent']}</b> € spent, last order {esc(stats['last_order'])}</p>
        <p><a href="/orders/new?customer={cid}">New order for this customer</a></p>"""
                + table(orders, ["id", "created_at", "status", "total"], {"id": lambda r: f"/orders/{r['id']}"}), user)


@blocking
def order_form(request, form):
    with Db() as db:
        user = current_user(request, db)
        custs = db.all("SELECT id, name FROM customers ORDER BY name", prepared=False)
        prods = db.all("""SELECT p.id, p.sku, p.name, c.description AS category, p.price, p.stock
                          FROM products p JOIN categories c ON c.code = p.category_code ORDER BY p.sku""", prepared=False)
    selected = request.query.get("customer", "")
    options = "".join(f'<option value="{c["id"]}" {"selected" if str(c["id"]) == selected else ""}>{esc(c["name"])}</option>'
                      for c in custs)
    lines = "".join(f'<tr><td>{esc(p["sku"])}</td><td>{esc(p["name"])}</td><td>{esc(p["category"])}</td>'
                    f'<td>{p["price"]}</td><td>{p["stock"]}</td><td><input name="qty_{p["id"]}" type="number" min="0" value="0" style="width:70px"></td></tr>'
                    for p in prods)
    return page("New order", f"""<form method="post"><p>Customer <select name="customer">{options}</select></p>
        <table><tr><th>SKU</th><th>Product</th><th>Category</th><th>Price</th><th>Stock</th><th>Qty</th></tr>{lines}</table>
        <button>Save order</button></form>""", user, error=request.query.get("error"))


@blocking
def order_save(request, form):
    """The interesting one: a transaction with reads, locks, inserts and updates on several tables."""
    with Db() as db:
        user = current_user(request, db)
        items = [(int(k[4:]), int(v)) for k, v in form.items() if k.startswith("qty_") and v and int(v) > 0]
        if not items:
            raise web.HTTPFound("/orders/new?error=Choose+at+least+one+product")
        db.conn.start_transaction()
        oid, _ = db.run("INSERT INTO orders (customer_id, created_by, created_at, status) VALUES (?, ?, NOW(), 'open')",
                        (int(form["customer"]), user["id"]))
        total = Decimal(0)
        for line_no, (pid, qty) in enumerate(items, 1):
            p = db.one("SELECT price, stock, name FROM products WHERE id = ? FOR UPDATE", (pid,))
            if p["stock"] < qty:
                db.conn.rollback()
                raise web.HTTPFound(f"/orders/new?error=Not+enough+stock+for+{p['name'].replace(' ', '+')}")
            db.run("INSERT INTO order_lines (order_id, line_no, product_id, qty, unit_price) VALUES (?, ?, ?, ?, ?)",
                   (oid, line_no, pid, qty, p["price"]))
            db.run("UPDATE products SET stock = stock - ? WHERE id = ?", (qty, pid))
            total += p["price"] * qty
        db.run("UPDATE orders SET total = ? WHERE id = ?", (total, oid))
        db.audit(user["id"], "ORDER_CREATED", "orders", oid, f"{len(items)} lines, total {total}")
        db.conn.commit()
    raise web.HTTPFound(f"/orders/{oid}?msg=Order+saved")


@blocking
def order_detail(request, form):
    oid = int(request.match_info["id"])
    with Db() as db:
        user = current_user(request, db)
        o = db.one("""SELECT o.*, c.name AS customer, u.full_name AS clerk
                      FROM orders o JOIN customers c ON c.id = o.customer_id
                      LEFT JOIN users u ON u.id = o.created_by WHERE o.id = ?""", (oid,))
        if o is None:
            raise web.HTTPNotFound()
        lines = db.all("""SELECT l.line_no, p.sku, p.name, l.qty, l.unit_price, l.qty * l.unit_price AS amount
                          FROM order_lines l JOIN products p ON p.id = l.product_id
                          WHERE l.order_id = ? ORDER BY l.line_no""", (oid,))
    cancel = ('<form method="post" action="/orders/%d/cancel"><button>Cancel order</button></form>' % oid
              if o["status"] == "open" else "")
    return page(f"Order #{oid}", f"""<p>Customer <a href="/customers/{o['customer_id']}">{esc(o['customer'])}</a> ·
        created {esc(o['created_at'])} by {esc(o['clerk'])} · status <b>{esc(o['status'])}</b> · total <b>{o['total']}</b> €</p>"""
                + table(lines, ["line_no", "sku", "name", "qty", "unit_price", "amount"]) + cancel,
                user, request.query.get("msg"))


@blocking
def order_cancel(request, form):
    oid = int(request.match_info["id"])
    with Db() as db:
        user = current_user(request, db)
        if user["role"] not in ("admin", "clerk"):
            db.audit(user["id"], "PERMISSION_DENIED", "orders", oid)
            db.conn.commit()
            raise web.HTTPFound(f"/orders/{oid}?msg=Not+allowed")
        db.conn.start_transaction()
        # restore the stock with a multi-table UPDATE, MySQL style
        db.run("""UPDATE products p JOIN order_lines l ON l.product_id = p.id
                  SET p.stock = p.stock + l.qty WHERE l.order_id = ?""", (oid,))
        db.run("UPDATE orders SET status = 'cancelled' WHERE id = ? AND status = 'open'", (oid,))
        db.audit(user["id"], "ORDER_CANCELLED", "orders", oid)
        db.conn.commit()
    raise web.HTTPFound(f"/orders/{oid}?msg=Order+cancelled")


@blocking
def reports(request, form):
    with Db() as db:
        user = current_user(request, db)
        low = db.all("""SELECT p.sku, p.name, c.description AS category, p.stock, p.reorder_level
                        FROM products p JOIN categories c ON c.code = p.category_code
                        WHERE p.stock < p.reorder_level ORDER BY p.stock""", prepared=False)
        sales = db.all("""SELECT DATE_FORMAT(o.created_at, '%Y-%m') AS month, COUNT(DISTINCT o.id) AS orders,
                                 SUM(l.qty * l.unit_price) AS revenue
                          FROM orders o JOIN order_lines l ON l.order_id = o.id
                          WHERE o.status <> 'cancelled' GROUP BY month ORDER BY month DESC""", prepared=False)
        top = db.all("""SELECT u.full_name AS clerk, COUNT(*) AS actions, MAX(a.ts) AS last_action
                        FROM audit_log a JOIN users u ON u.id = a.user_id
                        GROUP BY u.full_name ORDER BY actions DESC""", prepared=False)
    return page("Reports", "<h3>Products to reorder</h3>" + table(low, ["sku", "name", "category", "stock", "reorder_level"])
                + "<h3>Sales by month</h3>" + table(sales, ["month", "orders", "revenue"])
                + "<h3>Activity by user</h3>" + table(top, ["clerk", "actions", "last_action"]), user)


def main():
    global POOL
    p = argparse.ArgumentParser(description="Mini Shop demo application")
    p.add_argument("--port", type=int, default=8090, help="web port of the app")
    p.add_argument("--db-host", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=3317, help="3317 = through the profiler, 3306 = MySQL directly")
    p.add_argument("--db-user", default="shop")
    p.add_argument("--db-password", default="Shop-Demo-2026")
    p.add_argument("--database", default="shop_demo")
    a = p.parse_args()
    for attempt in range(30):
        try:
            POOL = pooling.MySQLConnectionPool(pool_name="shop", pool_size=4, host=a.db_host, port=a.db_port,
                                               user=a.db_user, password=a.db_password, database=a.database,
                                               autocommit=True)
            break
        except mysql.connector.errors.DatabaseError as e:
            if attempt == 0:
                print(f"waiting for the database at {a.db_host}:{a.db_port} (is the profiler running?) – {e}")
            time.sleep(1)
    else:
        raise SystemExit(f"cannot connect to {a.db_host}:{a.db_port}")
    app = web.Application()
    app.add_routes([
        web.get("/login", login_form), web.post("/login", login), web.get("/logout", logout),
        web.get("/", dashboard), web.get("/customers", customers), web.post("/customers/new", customer_new),
        web.get(r"/customers/{id:\d+}", customer_card), web.get("/orders/new", order_form),
        web.post("/orders/new", order_save), web.get(r"/orders/{id:\d+}", order_detail),
        web.post(r"/orders/{id:\d+}/cancel", order_cancel), web.get("/reports", reports),
    ])
    print(f"Mini Shop on http://127.0.0.1:{a.port}  (database {a.db_host}:{a.db_port})")
    web.run_app(app, host="127.0.0.1", port=a.port, print=None)


if __name__ == "__main__":
    main()
