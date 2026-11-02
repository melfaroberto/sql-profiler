"""Static analysis: dump folder + CSV log -> tables and attributes divided by operation."""
import asyncio
import csv
import io

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sqlprofiler import staticmap
from sqlprofiler.app import Config, Profiler
from sqlprofiler.server import build_app

CLIENTI = """CREATE TABLE `clienti` (
  `id` int NOT NULL, `nome` varchar(50), `citta` varchar(50), PRIMARY KEY (`id`)) ENGINE=InnoDB;
INSERT INTO `clienti` VALUES (1,'x','y');
"""
ORDINI = """CREATE TABLE `ordini` (
  `id` int NOT NULL, `id_cliente` int, `stato` varchar(9), `totale` decimal(9,2), PRIMARY KEY (`id`),
  CONSTRAINT f FOREIGN KEY (`id_cliente`) REFERENCES `clienti` (`id`)) ENGINE=InnoDB;
"""
RIGHE = "CREATE TABLE `righe` (`id` int NOT NULL, `id_ordine` int, `qta` int, PRIMARY KEY (`id`)) ENGINE=InnoDB;\n"
MAI = "CREATE TABLE `mai_usata` (`id` int NOT NULL, PRIMARY KEY (`id`)) ENGINE=InnoDB;\n"


@pytest.fixture
def dump(tmp_path):
    folder = tmp_path / "Dump20261015"
    folder.mkdir()
    for name, text in (("db_clienti", CLIENTI), ("db_ordini", ORDINI), ("db_righe", RIGHE), ("db_mai", MAI)):
        (folder / f"{name}.sql").write_text(text, encoding="utf-8")
    return folder


def log(tmp_path, rows, delimiter=",", header=("event_time", "command_type", "query")):
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=delimiter)
    w.writerow(header)
    w.writerows(rows)
    p = tmp_path / "log.csv"
    p.write_text(buf.getvalue(), encoding="utf-8")
    return p


def q(sql, kind="Query", when="2026-10-26 09:00:00"):
    return (when, kind, sql)


def table(result, op, name):
    return next(t for t in result["groups"][op] if t["table"] == name)


def used(t):
    return {c["name"]: c["usage"] for c in t["columns"] if c["usage"]}


def names(result, op):
    return {t["table"] for t in result["groups"][op]}


def test_divided_by_operation_with_attributes(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [
        q("SELECT c.nome FROM clienti c JOIN ordini o ON o.id_cliente = c.id WHERE o.stato = 'A'"),
        q("INSERT INTO righe (id_ordine, qta) VALUES (1, 2)"),
        q("UPDATE ordini SET totale = 5 WHERE id = 1"),
        q("DELETE FROM righe WHERE id = 3"),
    ]))
    assert names(r, "select") == {"clienti", "ordini"}
    assert names(r, "insert") == {"righe"}
    assert names(r, "update") == {"ordini"}
    assert names(r, "delete") == {"righe"}
    assert used(table(r, "select", "clienti")) == {"nome": ["read"], "id": ["filter"]}
    assert used(table(r, "select", "ordini")) == {"id_cliente": ["filter"], "stato": ["filter"]}
    assert used(table(r, "insert", "righe")) == {"id_ordine": ["write"], "qta": ["write"]}
    assert used(table(r, "update", "ordini")) == {"totale": ["write"], "id": ["filter"]}
    # every column of the table is there, not only the used ones
    assert [c["name"] for c in table(r, "update", "ordini")["columns"]] == ["id", "id_cliente", "stato", "totale"]
    assert r["unused"] == ["mai_usata"]
    assert r["summary"]["counts"] == {"select": 2, "insert": 1, "update": 1, "delete": 1}


def test_source_of_insert_select_and_of_update_join_count_as_select(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [
        q("INSERT INTO righe (id_ordine) SELECT id FROM ordini WHERE stato = 'A'"),
        q("UPDATE ordini o JOIN clienti c ON c.id = o.id_cliente SET o.stato = 'B' WHERE c.citta = 'Roma'"),
    ]))
    assert names(r, "insert") == {"righe"} and names(r, "update") == {"ordini"}
    assert names(r, "select") == {"ordini", "clienti"}
    assert used(table(r, "insert", "righe")) == {"id_ordine": ["write"]}
    assert "id" in used(table(r, "select", "ordini"))
    assert used(table(r, "select", "clienti"))["citta"] == ["filter"]


def test_star_and_insert_without_columns_mean_every_column(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT * FROM clienti"), q("INSERT INTO righe VALUES (1, 2, 3)"),
                                               q("SELECT COUNT(*) FROM ordini")]))
    assert set(used(table(r, "select", "clienti"))) == {"id", "nome", "citta"}
    assert set(used(table(r, "insert", "righe"))) == {"id", "id_ordine", "qta"}
    assert used(table(r, "select", "ordini")) == {}             # COUNT(*) reads no attribute


def test_event_types_and_counts(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [
        q("leo@localhost on shop", "Connect", "2026-10-26 09:00:00"),
        q("SELECT * FROM clienti WHERE id=?", "Prepare"),
        q("SELECT * FROM clienti WHERE id=1", "Execute"),
        q("SELECT * FROM clienti WHERE id=2", "Query", "2026-10-26 10:00:00"),
        q("SET NAMES utf8"), q("SELECT @@version"), q("", "Quit"),
        q("SELECT * FROM mysql.user"), q("CREATE TABLE z (a int)"), q("CALL ricalcola(1)"),
    ]))
    assert table(r, "select", "clienti")["queries"] == 2
    assert len(table(r, "select", "clienti")["examples"]) == 1
    assert r["summary"]["ignored"] == {"Connect": 1, "Prepare": 1, "Quit": 1}
    assert r["summary"]["rows"] == 10 and r["summary"]["statements"] == 2
    assert (r["summary"]["first"], r["summary"]["last"]) == ("2026-10-26 09:00:00", "2026-10-26 10:00:00")
    assert r["summary"]["other_types"] == {"CREATE": 1}
    assert any("ricalcola" in p for p in r["problems"])
    assert names(r, "select") == {"clienti"}


def test_csv_variants(tmp_path, dump):
    # Excel with ; a different column order and upper case names, a multi-line query with commas
    p = log(tmp_path, [("SELECT nome,\n citta FROM clienti", "Query", "t")], ";", ("QUERY", "Command_Type", "event_time"))
    assert names(staticmap.analyse(dump, p), "select") == {"clienti"}


@pytest.mark.parametrize("header", [
    ("event_time", "event_type", "query"),                       # the other name of the type column
    ("event_time", "user_host", "thread_id", "server_id", "command_type", "argument"),   # mysql.general_log itself
])
def test_other_column_names(tmp_path, dump, header):
    row = ["x"] * len(header)
    row[0], row[-2], row[-1] = "t", "Connect", "leo@localhost on shop"
    good = list(row)
    good[-2], good[-1] = "Query", "SELECT nome FROM clienti"
    r = staticmap.analyse(dump, log(tmp_path, [row, good], header=header))
    assert names(r, "select") == {"clienti"} and r["summary"]["ignored"] == {"Connect": 1}


def test_only_the_query_column_is_indispensable(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [("SELECT nome FROM clienti",)], header=("query",)))
    assert names(r, "select") == {"clienti"} and r["summary"]["first"] is None


def test_csv_with_unquoted_commas_in_the_query(tmp_path, dump):
    p = tmp_path / "raw.csv"
    p.write_text("event_time,command_type,query\nt,Query,SELECT nome, citta FROM clienti\n", encoding="utf-8")
    assert set(used(table(staticmap.analyse(dump, p), "select", "clienti"))) == {"nome", "citta"}


def test_multiple_statements_in_a_row(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT nome FROM clienti; UPDATE ordini SET stato='A'")]))
    assert names(r, "select") == {"clienti"} and names(r, "update") == {"ordini"}
    assert used(table(r, "update", "ordini")) == {"stato": ["write"]}


def test_unparsable_statement_still_gives_its_table(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT nome FROM clienti WHERE ))) nonsense (((")]))
    assert names(r, "select") == {"clienti"}
    assert any("not fully read" in p for p in r["problems"])
    assert table(r, "select", "clienti")["partial"] == 1        # the page must not show it as "no attribute used"


def test_huge_insert_keeps_table_and_columns(tmp_path, dump):
    values = ",".join(f"({i},2)" for i in range(60_000))
    r = staticmap.analyse(dump, log(tmp_path, [q(f"INSERT INTO righe (id_ordine, qta) VALUES {values}")]))
    assert used(table(r, "insert", "righe")) == {"id_ordine": ["write"], "qta": ["write"]}


def test_table_not_in_dump_is_reported(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT x FROM sconosciuta")]))
    t = table(r, "select", "sconosciuta")
    assert not t["in_dump"] and t["columns"][0]["in_dump"] is False
    assert any("sconosciuta" in p for p in r["problems"])


def test_diagram_has_the_links(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT * FROM clienti"), q("UPDATE ordini SET stato='A'"),
                                               q("INSERT INTO righe (id_ordine) VALUES (1)")]))
    assert {(l["from"], l["to"], l["kind"]) for l in r["er"]["links"]} == {
        ("ordini", "clienti", "declared"), ("righe", "ordini", "inferred-media")}


@pytest.mark.parametrize("dump_arg,log_arg,message", [
    ("", "x.csv", "Choose both"),
    ("/no/such/folder", "x.csv", "Dump folder not found"),
])
def test_input_errors(dump_arg, log_arg, message):
    with pytest.raises(ValueError, match=message):
        staticmap.analyse(dump_arg, log_arg)


def test_wrong_columns_and_empty_folder(tmp_path, dump):
    with pytest.raises(ValueError, match="column with the statements .* Found: event_time, foo"):
        staticmap.analyse(dump, log(tmp_path, [], header=("event_time", "foo")))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No .sql files"):
        staticmap.analyse(empty, log(tmp_path, [q("SELECT 1")]))
    with pytest.raises(ValueError, match="no statements"):
        staticmap.analyse(dump, log(tmp_path, [q("SET NAMES utf8")]))


def test_endpoint_stores_nothing(tmp_path, dump):
    p = log(tmp_path, [q("SELECT nome FROM clienti")])

    async def go():
        profiler = Profiler(Config(db_path=str(tmp_path / "h.sqlite")))
        client = TestClient(TestServer(build_app(profiler)))
        await client.start_server()
        try:
            ok = await client.post("/api/static", json={"dump": str(dump), "log": str(p)})
            bad = await client.post("/api/static", json={})
            return ok.status, await ok.json(), bad.status, profiler.storage.counts()["total"]
        finally:
            await client.close()
            profiler.storage.close()

    status, data, bad_status, stored = asyncio.run(go())
    assert status == 200 and data["summary"]["counts"]["select"] == 1
    assert bad_status == 400 and stored == 0


@pytest.mark.parametrize("sql,expected", [
    ("INSERT LOW_PRIORITY INTO righe (id_ordine, qta) VALUES (1, 2)", {"id_ordine", "qta"}),
    ("INSERT DELAYED IGNORE INTO righe VALUES (1, 2, 3)", {"id", "id_ordine", "qta"}),
    ("INSERT INTO righe (id_ordine, qta) VALUES (1, 'cut by the lo", {"id_ordine", "qta"}),
    ("INSERT INTO righe VALUES (1, 2, 'cut by the lo", {"id", "id_ordine", "qta"}),
    ("INSERT INTO `db`.`righe` (`id_ordine`,`qta`) VALUES (1, )", {"id_ordine", "qta"}),
    ("INSERT INTO righe SET id_ordine = 1, qta = 'cut by the", {"id_ordine", "qta"}),
    ("REPLACE LOW_PRIORITY INTO righe (qta) VALUES (1)", {"qta"}),
    ("SET @a = 1; INSERT INTO righe (qta) VALUES (1)", {"qta"}),
    ("INSERT INTO righe (qta) VALUES ('information_schema.tables')", {"qta"}),
    ("INSERT INTO RIGHE (ID_ORDINE) VALUES (1)", {"id_ordine"}),
])
def test_inserted_attributes_are_never_lost(tmp_path, dump, sql, expected):
    t = table(staticmap.analyse(dump, log(tmp_path, [q(sql)])), "insert", "righe")
    assert set(used(t)) == expected
    assert all(u == ["write"] for u in used(t).values())
    assert t["partial"] == 0


def test_insert_with_a_broken_select_says_that_it_is_incomplete(tmp_path, dump):
    t = table(staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO righe (qta) SELECT ))) FROM ordini")])), "insert", "righe")
    assert set(used(t)) == {"qta"} and t["partial"] == 1


def test_update_cut_by_the_log_keeps_what_it_writes(tmp_path, dump):
    t = table(staticmap.analyse(dump, log(tmp_path, [q("UPDATE ordini SET stato = 'A', totale = 'cut by the")])), "update", "ordini")
    assert set(used(t)) == {"stato", "totale"} and t["partial"] == 1


def test_unassigned_attributes_are_reported(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("SELECT misteriosa FROM clienti c JOIN ordini o ON o.id_cliente = c.id")]))
    assert any("misteriosa" in p for p in r["problems"])


def values(result, op, name):
    return {c["name"]: c.get("value") for c in table(result, op, name)["columns"]}


def test_insert_values_and_null_for_what_is_left_out(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO ordini (id, stato) VALUES (5, 'a, b (c)')")]))
    assert values(r, "insert", "ordini") == {"id": "5", "id_cliente": "NULL", "stato": "'a, b (c)'", "totale": "NULL"}


def test_insert_without_list_explicit_null_and_expressions(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO ordini VALUES (1, NULL, CONCAT('a', 'b'), 2.50)")]))
    assert values(r, "insert", "ordini") == {"id": "1", "id_cliente": "NULL", "stato": "CONCAT('a', 'b')", "totale": "2.50"}


def test_several_rows_and_executions_are_all_kept(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO righe (id, qta) VALUES (1, 10), (2, 20)"),
                                               q("INSERT INTO righe (id, qta) VALUES (3, 20) ON DUPLICATE KEY UPDATE qta = (1)")]))
    c = next(c for c in table(r, "insert", "righe")["columns"] if c["name"] == "qta")
    assert c["value"] == "20" and dict(c["values"]) == {"20": 2, "10": 1}
    assert values(r, "insert", "righe")["id_ordine"] == "NULL"


def test_insert_set_replace_modifier_and_select(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO righe SET qta = 7")]))
    assert values(r, "insert", "righe") == {"id": "NULL", "id_ordine": "NULL", "qta": "7"}
    r = staticmap.analyse(dump, log(tmp_path, [q("REPLACE LOW_PRIORITY INTO righe (qta) VALUES (8)")]))
    assert values(r, "insert", "righe")["qta"] == "8"
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO righe (qta) SELECT id FROM ordini")]))
    assert values(r, "insert", "righe") == {"id": "NULL", "id_ordine": "NULL", "qta": "(from SELECT)"}


def test_insert_cut_by_the_log_does_not_invent_nulls(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO ordini VALUES (1, 2, 'cut by the lo")]))
    assert values(r, "insert", "ordini") == {"id": "1", "id_cliente": "2", "stato": "'cut by the lo…", "totale": None}


def test_update_values(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("UPDATE ordini SET stato = 'x=1, y', totale = totale + 1 WHERE id IN (SELECT id FROM righe)")]))
    assert values(r, "update", "ordini") == {"id": None, "id_cliente": None, "stato": "'x=1, y'", "totale": "totale + 1"}


def test_update_with_join_only_gives_the_values_of_the_target(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("UPDATE ordini o JOIN clienti c ON c.id = o.id_cliente SET o.stato = 'B', c.citta = 'Roma' WHERE c.id = 1")]))
    assert values(r, "update", "ordini")["stato"] == "'B'"
    assert values(r, "update", "ordini")["totale"] is None


def test_update_cut_by_the_log_and_two_statements_in_a_row(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("UPDATE ordini SET stato = 'A', totale = 'cut by")]))
    assert values(r, "update", "ordini")["stato"] == "'A'" and values(r, "update", "ordini")["totale"] == "'cut by…"
    r = staticmap.analyse(dump, log(tmp_path, [q("SET @a = 1; INSERT INTO righe (qta) VALUES (9); UPDATE ordini SET stato = 'Z'")]))
    assert values(r, "insert", "righe")["qta"] == "9" and values(r, "update", "ordini")["stato"] == "'Z'"


def test_long_values_are_shortened_and_selects_have_none(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO righe (qta) VALUES ('" + "x" * 500 + "')"), q("SELECT qta FROM righe")]))
    assert len(values(r, "insert", "righe")["qta"]) == 121
    assert values(r, "select", "righe")["qta"] is None


def test_values_of_other_tables_do_not_mix_and_case_is_ignored(tmp_path, dump):
    r = staticmap.analyse(dump, log(tmp_path, [q("INSERT INTO RIGHE (QTA) VALUES (4)"), q("INSERT INTO clienti (nome) VALUES ('n')")]))
    assert values(r, "insert", "righe")["qta"] == "4" and "qta" not in values(r, "insert", "clienti")
