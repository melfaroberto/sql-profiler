import json
import time

from sqlprofiler import exporter
from sqlprofiler.analyzer import QueryAnalyzer
from sqlprofiler.sessions import SessionManager
from sqlprofiler.storage import Storage


def add(storage, sql, session_id=None, ts=None, **extra):
    a = QueryAnalyzer("mysql").analyze(sql)
    row = {"ts": ts or time.time(), "session_id": session_id, "conn_id": 1, "proxy": "mysql:3307", "protocol": "mysql",
           "client": "127.0.0.1:5000", "db_user": "app", "database": "shop", "kind": "query", "sql": sql,
           "params": None, "truncated": 0, "duration_ms": 1.5, "rows": None, "affected": None, "error": None,
           "query_type": a["query_type"], "tables": a["tables"], "columns": a["columns"], "joins": a["joins"],
           "fingerprint": a["fingerprint"], "formatted": a["formatted"], "parse_error": None, "noise": int(a["noise"])}
    row.update(extra)
    return storage.insert_query(row)


def test_recording_groups_queries(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    events = []
    sm = SessionManager(st, on_change=lambda s, e: events.append(e))
    add(st, "SELECT 1 FROM before_rec")
    s = sm.start_recording("Save Order")
    sid = sm.session_for(time.time())
    assert sid == s["id"]
    add(st, "SELECT * FROM orders WHERE id = 1", sid)
    add(st, "SET NAMES utf8", sid)
    sm.stop_recording()
    assert sm.session_for(time.time()) is None
    assert events == ["started", "stopped"]

    [session] = st.list_sessions()
    assert session["label"] == "Save Order" and session["queries"] == 2 and session["relevant"] == 1
    assert session["ended_at"] >= session["started_at"]
    assert len(st.list_queries(session="none")) == 1
    assert len(st.list_queries(session=sid, hide_noise=True)) == 1
    assert len(st.list_queries(table="ORDERS")) == 1


def test_starting_a_recording_closes_the_previous_one(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    sm = SessionManager(st)
    a = sm.start_recording("A")
    b = sm.start_recording("")
    assert st.get_session(a["id"])["ended_at"] is not None
    assert b["label"].startswith("Action ")


def test_group_existing_queries(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    sm = SessionManager(st)
    ids = [add(st, f"SELECT {i}", ts=1000 + i) for i in range(3)]
    s = sm.group("Login", ids[1:])
    assert s["queries"] == 2 and s["started_at"] == 1001 and s["ended_at"] == 1002
    assert [q["id"] for q in st.list_queries(session="none")] == ids[:1]


def test_open_session_is_closed_on_restart(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    s = st.create_session("crashed")
    st.close()
    assert Storage(tmp_path / "p.sqlite").get_session(s["id"])["ended_at"] is not None


def test_export(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    s = st.create_session("Save Invoice")
    add(st, "SELECT * FROM invoices i JOIN customers c ON c.id = i.customer_id WHERE i.id = ?", s["id"],
        kind="execute", params=[7], rows=1)
    add(st, "UPDATE invoices SET status = 'saved' WHERE id = 7", s["id"], affected=1)
    add(st, "UPDATE invoices SET status = 'saved' WHERE id = 8", s["id"], error="1213: Deadlock")
    add(st, "SET autocommit = 1", s["id"])
    st.update_session(s["id"], notes="Click on Save in the invoice form")

    doc = json.loads(exporter.export_json(st, [s["id"]]))
    [d] = doc["sessions"]
    assert len(d["queries"]) == 3                     # noise excluded by default
    assert d["queries"][0]["sql_with_values"].endswith("i.id = 7")
    summary = d["summary"]
    assert {t["table"]: t["access"] for t in summary["tables"]} == {"customers": ["read"], "invoices": ["read", "write"]}
    assert summary["written_columns"] == {"invoices": ["status"]}
    assert summary["distinct_statements"][0]["count"] == 2

    md = exporter.export_markdown(st, [s["id"]])
    assert "## Save Invoice" in md
    assert "Click on Save in the invoice form" in md
    assert "| `invoices` | read, write | 3 | no |" in md
    assert "candidate relationship" in md
    assert "i.id = 7" in md
    assert "**Error:** 1213: Deadlock" in md
    assert "autocommit" not in md
    assert "autocommit" in exporter.export_markdown(st, [s["id"]], include_noise=True)


def test_delete_all_sessions(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    a, b = st.create_session("A"), st.create_session("B")
    add(st, "SELECT 1 FROM t", a["id"])
    add(st, "SELECT 2 FROM t", b["id"])
    add(st, "SELECT 3 FROM t")
    st.delete_all_sessions()
    assert st.list_sessions() == [] and st.counts() == {"total": 3, "unassigned": 3}

    c = st.create_session("C")
    add(st, "SELECT 4 FROM t", c["id"])
    st.delete_all_sessions(keep_queries=False)
    assert st.list_sessions() == [] and st.counts() == {"total": 3, "unassigned": 3}


def test_sub_sessions(tmp_path):
    st = Storage(tmp_path / "p.sqlite")
    events = []
    sm = SessionManager(st, on_change=lambda s, e: events.append((s["label"], e)))
    top = sm.start_recording("Customers")
    add(st, "SELECT * FROM customers", sm.session_for(time.time()))
    search = sm.start_sub("Search")
    assert search["parent_id"] == top["id"] and sm.session_for(time.time()) == search["id"]
    add(st, "SELECT * FROM customers WHERE city = 'Roma'", sm.session_for(time.time()))
    deep = sm.start_sub("Advanced filters")                 # inside "Search"
    assert deep["parent_id"] == search["id"]
    add(st, "SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.total > 10",
        sm.session_for(time.time()))
    status = sm.status()
    assert [s["label"] for s in status["path"]] == ["Customers", "Search", "Advanced filters"]
    assert status["recording_ids"] == [top["id"], search["id"], deep["id"]]
    sm.stop_sub()
    assert sm.session_for(time.time()) == search["id"]
    sm.stop_sub()
    assert sm.session_for(time.time()) == top["id"] and sm.stop_sub() is None
    add_c = sm.start_sub("Add customer")
    add(st, "INSERT INTO customers (name) VALUES ('x')", sm.session_for(time.time()))
    add(st, "INSERT INTO audit_log (action) VALUES ('new')", sm.session_for(time.time()))
    sm.stop_recording()
    assert events[-2:] == [("Add customer", "stopped"), ("Customers", "stopped")]
    assert sm.status() == {"recording": False}

    # a sub-action added later inside a closed sub-session
    late = sm.start_sub("Export", parent_id=deep["id"])
    add(st, "SELECT * FROM customers", sm.session_for(time.time()))
    sm.stop_sub()
    assert st.get_session(top["id"])["ended_at"] >= late["started_at"]

    sessions = {s["label"]: s for s in st.list_sessions()}
    assert sessions["Customers"]["queries"] == 6 and sessions["Search"]["queries"] == 3
    assert sessions["Advanced filters"]["queries"] == 2
    assert len(st.list_queries(session=search["id"])) == 3
    usage = {t["table"]: t for t in st.table_usage(top["id"])}
    assert usage["customers"]["statements"] == 5 and usage["customers"]["access"] == ["read", "write"]
    assert usage["audit_log"]["access"] == ["write"] and "orders" in usage
    cols = {c["column"]: c["usage"] for c in usage["customers"]["columns"]}
    assert cols["city"] == ["filter"] and cols["name"] == ["read", "write"]
    assert [t["table"] for t in st.table_usage(search["id"])] == ["customers", "orders"]
    [join] = st.joins_usage(search["id"])
    assert {join["left_table"], join["right_table"]} == {"customers", "orders"} and join["count"] == 1

    # export: depth first, each session followed by its sub-sessions; queries are not repeated
    order = exporter.ordered_sessions(st)
    assert order == [top["id"], search["id"], deep["id"], late["id"], add_c["id"]]
    assert exporter.ordered_sessions(st, [search["id"]]) == [search["id"], deep["id"], late["id"]]
    md = exporter.export_markdown(st, order)
    assert "## Customers › Search › Advanced filters" in md and md.count("```sql\n   INSERT INTO customers") == 1

    st.delete_session(search["id"])                          # with its sub-sessions
    assert [s["label"] for s in st.list_sessions()] == ["Add customer", "Customers"]
    st.delete_session(top["id"])
    assert st.list_sessions() == [] and st.counts() == {"total": 6, "unassigned": 6}


def test_table_report(tmp_path):
    from sqlprofiler.analyzer import QueryAnalyzer
    from sqlprofiler.schema import SchemaCatalog
    st = Storage(tmp_path / "p.sqlite")
    col = lambda n, pk=False, fk=False: {"name": n, "type": "int", "pk": pk, "fk": fk, "refs": []}
    schema = SchemaCatalog(
        [{"name": "customers", "columns": [col("id", pk=True), col("name")]},
         {"name": "orders", "columns": [col("id", pk=True), col("customer_id", fk=True), col("total")]},
         {"name": "products", "columns": [col("id", pk=True)]}, {"name": "Audit_Log", "columns": [col("id", pk=True)]}],
        [{"from": "orders", "from_cols": ["customer_id"], "to": "customers", "to_cols": ["id"], "kind": "declared"}])
    s = st.create_session("Save order")
    child = st.create_session("Check customer", parent_id=s["id"])
    add(st, "SELECT name, nickname FROM CUSTOMERS WHERE id = 1", child["id"])
    add(st, "INSERT INTO orders (customer_id, total) VALUES (1, 2)", s["id"])
    add(st, "SELECT * FROM temp_import")
    report = exporter.table_report(st, schema)
    assert [(t["table"], t["in_schema"]) for t in report["tables"]] == [
        ("CUSTOMERS", True), ("orders", True), ("temp_import", False)]
    assert report["unused"] == ["Audit_Log", "products"] and report["schema_tables"] == 4
    cols = {c["column"]: (c["usage"], c["in_schema"]) for c in report["tables"][0]["columns"]}
    assert cols == {"id": (["filter"], True), "name": (["read"], True), "nickname": (["read"], False)}
    assert exporter.table_report(st, None)["unused"] is None
    # a session includes its sub-sessions
    assert [t["table"] for t in exporter.table_report(st, schema, s["id"])["tables"]] == ["CUSTOMERS", "orders"]

    md = exporter.tables_markdown(st, schema, profile="Shop")
    assert "# Tables used — Shop" in md and "2 of the 4 tables of the schema used, 2 never used" in md
    assert "- `products`" in md and "### Save order" in md and "#### Save order › Check customer" in md
    assert "### Outside sessions" in md and "`nickname` (read) ⚠" in md and "| `temp_import` | read | 1 | 0 | **no** |" in md
    lines = exporter.tables_csv(st, schema, profile="Shop").splitlines()
    assert lines[0].startswith("profile,scope,session_id,session_path,depth,table")
    assert "Shop,unused,,,,products,,0,yes,,,," in lines
    assert "Shop,sub-session,2,Save order › Check customer,1,CUSTOMERS,read,1,yes,nickname,read,1,no" in lines
    assert any(l.startswith("Shop,session,1,Save order,0,orders,write,1,yes,total,write") for l in lines)

    analyzer = QueryAnalyzer("mysql", schema)
    er = exporter.er_diagram(st, analyzer, s["id"])
    assert {t["name"] for t in er["tables"]} == {"customers", "orders"}
    assert [(l["from"], l["to"]) for l in er["links"]] == [("orders", "customers")]
    page = exporter.er_html(st, analyzer, schema, s["id"], "Shop")
    assert page.startswith("<!doctype html>") and "MiniER.render" in page and "Save order" in page
    assert "const MiniER" in page and "<b>orders</b>" in page
