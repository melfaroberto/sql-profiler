from sqlprofiler.analyzer import QueryAnalyzer, interpolate
from sqlprofiler.schema import from_ddl, from_sqlfk_model

DDL = """
CREATE TABLE `customers` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;
INSERT INTO `customers` VALUES (1,'a'),(2,'b');
CREATE TABLE `orders` (
  `id` int(11) NOT NULL,
  `customer_id` int(11) NOT NULL,
  `total` decimal(10,2),
  PRIMARY KEY (`id`),
  CONSTRAINT `fk_c` FOREIGN KEY (`customer_id`) REFERENCES `customers` (`id`)
) ENGINE=InnoDB;
CREATE TABLE `order_lines` (
  `order_id` int(11) NOT NULL,
  `line` int(11) NOT NULL,
  `qty` int,
  PRIMARY KEY (`order_id`, `line`)
);
"""


def schema(tmp_path):
    f = tmp_path / "dump.sql"
    f.write_text(DDL)
    return from_ddl(f)


def test_ddl_reader(tmp_path):
    s = schema(tmp_path)
    assert set(s.tables) == {"customers", "orders", "order_lines"}
    orders = s.table("ORDERS")
    assert [c["name"] for c in orders["columns"] if c["pk"]] == ["id"]
    assert [c["name"] for c in orders["columns"] if c["fk"]] == ["customer_id"]
    assert s.links == [{"from": "orders", "from_cols": ["customer_id"], "to": "customers", "to_cols": ["id"],
                        "kind": "declared", "relation": ""}]
    assert [c["name"] for c in s.table("shop.order_lines")["columns"] if c["pk"]] == ["order_id", "line"]


def test_sqlfk_model():
    model = {"tabelle": [{"nome": "a", "colonne": [{"nome": "id", "tipo": "INT", "pk": True, "fk": False, "verso": []}]},
                         {"nome": "b", "colonne": [{"nome": "a_id", "tipo": "INT", "pk": False, "fk": True, "verso": ["a"]}]}],
             "collegamenti": [{"da": "b", "colonne_da": ["a_id"], "a": "a", "colonne_a": ["id"], "tipo": "alta",
                               "relazione": "N:1", "obbligatoria": True}]}
    s = from_sqlfk_model(model)
    assert s.links[0]["kind"] == "inferred-alta"
    assert s.has_link("a", "id", "b", "a_id")


def test_select_with_join(tmp_path):
    a = QueryAnalyzer("mysql", schema(tmp_path))
    r = a.analyze("SELECT o.id, c.name FROM Orders o JOIN customers c ON c.id = o.customer_id WHERE o.total > ?")
    assert r["query_type"] == "SELECT"
    assert [(t["name"], t["access"], t["known"]) for t in r["tables"]] == [("orders", "read", True), ("customers", "read", True)]
    cols = {(c["table"], c["column"]): c["usage"] for c in r["columns"]}
    assert cols[("orders", "total")] == ["filter"]
    assert cols[("customers", "name")] == ["read"]
    assert r["joins"] == [{"left_table": "customers", "left_column": "id", "right_table": "orders",
                           "right_column": "customer_id", "in_schema": True}]
    assert "JOIN customers AS c" in r["formatted"]


def test_join_not_in_schema_is_a_candidate(tmp_path):
    a = QueryAnalyzer("mysql", schema(tmp_path))
    r = a.analyze("SELECT * FROM order_lines l JOIN orders o ON o.id = l.order_id")
    assert r["joins"][0]["in_schema"] is False
    er = a.er_subgraph(r)
    assert {t["name"] for t in er["tables"]} == {"order_lines", "orders"}
    assert len(er["observed"]) == 1


def test_write_statements(tmp_path):
    a = QueryAnalyzer("mysql", schema(tmp_path))
    r = a.analyze("UPDATE orders SET total = ? WHERE id = ?")
    assert r["query_type"] == "UPDATE" and r["tables"][0]["access"] == "write"
    assert {c["column"]: c["usage"] for c in r["columns"]} == {"total": ["write"], "id": ["filter"]}
    r = a.analyze("INSERT INTO order_lines (order_id, line, qty) SELECT id, 1, 1 FROM orders")
    assert {t["name"]: t["access"] for t in r["tables"]} == {"order_lines": "write", "orders": "read"}
    r = a.analyze("DELETE FROM customers WHERE id = 3")
    assert r["tables"][0]["access"] == "delete"
    r = a.analyze("REPLACE INTO customers (id, name) VALUES (1, 'x')")
    assert r["query_type"] == "REPLACE" and r["tables"][0]["access"] == "write"
    assert r["formatted"].startswith("REPLACE")


def test_noise_and_transactions():
    a = QueryAnalyzer("mysql")
    assert a.analyze("SET NAMES utf8mb4")["noise"]
    assert a.analyze("SELECT @@session.transaction_isolation")["noise"]
    assert a.analyze("SHOW WARNINGS")["noise"]
    assert not a.analyze("START TRANSACTION")["noise"]
    assert a.analyze("START TRANSACTION")["query_type"] == "BEGIN"
    assert not a.analyze("SELECT * FROM t")["noise"]
    assert a.analyze("SELECT t.oid FROM pg_catalog.pg_type t WHERE t.oid = ANY($1)")["noise"]
    assert a.analyze("SELECT COLUMN_NAME FROM information_schema.COLUMNS")["noise"]


def test_fingerprint_ignores_literals():
    a = QueryAnalyzer("mysql")
    f = lambda s: a.analyze(s)["fingerprint"]
    assert f("SELECT * FROM t WHERE id = 1") == f("select *  from t where id = 25")
    assert f("SELECT * FROM t WHERE id IN (1, 2)") == f("SELECT * FROM t WHERE id IN (3, 4, 5)")
    assert f("INSERT INTO t VALUES (1, 'a')") == f("INSERT INTO t VALUES (2, 'b'), (3, 'c')")
    assert f("SELECT * FROM t WHERE id = 1") != f("SELECT * FROM u WHERE id = 1")


def test_cte_names_are_not_tables():
    r = QueryAnalyzer("postgres").analyze("WITH recent AS (SELECT * FROM orders) SELECT * FROM recent WHERE id = $1")
    assert [t["name"] for t in r["tables"]] == ["orders"]


def test_unparsable_sql_is_kept():
    r = QueryAnalyzer("mysql").analyze("SELEC broken FROM")
    assert r["formatted"] == "SELEC broken FROM"
    assert r["fingerprint"]


def test_interpolate():
    assert interpolate("SELECT '?', ? -- ?\n, ?", ["it's", None]) == "SELECT '?', 'it''s' -- ?\n, NULL"
    assert interpolate("SELECT $2, $1, '$1'", [1, "a"], "postgres") == "SELECT 'a', 1, '$1'"
    assert interpolate("SELECT ?", None) == "SELECT ?"
    assert interpolate("SELECT * FROM t WHERE id = ANY($1)", [[1, 2]], "postgres") == "SELECT * FROM t WHERE id = ANY(ARRAY[1, 2])"
