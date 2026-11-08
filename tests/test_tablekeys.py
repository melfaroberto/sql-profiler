"""Static analysis, what comes after the tables used: keys and areas, suggestions, CREATE script and dump of those tables."""
import asyncio
import csv
import io

import pytest
from aiohttp.test_utils import TestClient, TestServer

from sqlprofiler import schemaout, staticmap
from sqlprofiler.app import Config, Profiler
from sqlprofiler.server import build_app
from sqlprofiler.tablekeys import name_groups

DUMP = """-- MySQL dump 10.13
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;

--
-- Table structure for table `clienti`
--

DROP TABLE IF EXISTS `clienti`;
CREATE TABLE `clienti` (
  `id_cliente` int NOT NULL,
  `nome` varchar(50) DEFAULT NULL,
  `citta` varchar(50) DEFAULT NULL,
  `telefono1` varchar(20), `telefono2` varchar(20),
  `id_categoria` int DEFAULT NULL,
  PRIMARY KEY (`id_cliente`)
) ENGINE=InnoDB;

LOCK TABLES `clienti` WRITE;
/*!40000 ALTER TABLE `clienti` DISABLE KEYS */;
INSERT INTO `clienti` VALUES (1,'Rossi; srl','Roma','1','2',1),(2,'Bianchi','Milano',NULL,NULL,2);
/*!40000 ALTER TABLE `clienti` ENABLE KEYS */;
UNLOCK TABLES;

DROP TABLE IF EXISTS `categorie`;
CREATE TABLE `categorie` (`id_categoria` int NOT NULL, `descrizione` varchar(50), PRIMARY KEY (`id_categoria`)) ENGINE=InnoDB;
INSERT INTO `categorie` VALUES (1,'A');

DROP TABLE IF EXISTS `ordini`;
CREATE TABLE `ordini` (
  `id_ordine` int NOT NULL, `id_cliente` int NOT NULL, `citta` varchar(50), `stato` char(1),
  `data_ins` datetime, `data_mod` datetime,
  PRIMARY KEY (`id_ordine`),
  CONSTRAINT `fk1` FOREIGN KEY (`id_cliente`) REFERENCES `clienti` (`id_cliente`)
) ENGINE=InnoDB;
INSERT INTO `ordini` VALUES (1,1,'Roma','A',NULL,NULL);

DROP TABLE IF EXISTS `ordini_righe`;
CREATE TABLE `ordini_righe` (`id_ordine` int NOT NULL, `num_riga` int NOT NULL, `id_articolo` int, `qta` int,
  PRIMARY KEY (`id_ordine`,`num_riga`)) ENGINE=InnoDB;

DROP TABLE IF EXISTS `articoli`;
CREATE TABLE `articoli` (`id_articolo` int NOT NULL, `descrizione` varchar(80), `prezzo` decimal(9,2),
  PRIMARY KEY (`id_articolo`)) ENGINE=InnoDB;

DROP TABLE IF EXISTS `log_accessi`;
CREATE TABLE `log_accessi` (`id` int NOT NULL, `utente` varchar(20), PRIMARY KEY (`id`)) ENGINE=InnoDB;

DROP TABLE IF EXISTS `clienti_bak`;
CREATE TABLE `clienti_bak` (`id_cliente` int NOT NULL, `nome` varchar(50), `citta` varchar(50), `telefono1` varchar(20),
  `telefono2` varchar(20), `id_categoria` int, PRIMARY KEY (`id_cliente`)) ENGINE=InnoDB;
LOCK TABLES `clienti_bak` WRITE;
INSERT INTO `clienti_bak` VALUES (1,'x','y','1','2',1);
UNLOCK TABLES;

DELIMITER ;;
/*!50003 CREATE*/ /*!50003 TRIGGER `t1` BEFORE INSERT ON `ordini` FOR EACH ROW BEGIN
  INSERT INTO clienti_bak VALUES (1,'a','b','c','d',1);
END */;;
DELIMITER ;
/*!50001 CREATE VIEW `v_clienti` AS select 1 AS `a` */;
"""


@pytest.fixture
def files(tmp_path):
    folder = tmp_path / "dump"
    folder.mkdir()
    (folder / "shop.sql").write_text(DUMP, encoding="utf-8")
    rows = [("event_time", "command_type", "query")]
    for i in range(12):
        rows += [
            ("2026-10-26 09:00:00", "Query", f"SELECT o.id_ordine, o.stato, c.nome FROM ordini o JOIN clienti c "
                                             f"ON c.id_cliente = o.id_cliente WHERE o.id_ordine = {i}"),
            ("2026-10-26 09:00:01", "Query", f"INSERT INTO ordini (id_ordine, id_cliente, citta, stato, data_ins, data_mod) "
                                             f"VALUES ({i}, {i % 3}, 'Roma', 'A', '2026-01-0{i % 9 + 1}', '2026-01-0{i % 9 + 1}')"),
            ("2026-10-26 09:00:02", "Query", f"INSERT INTO log_accessi (utente) VALUES ('u{i}')"),
            ("2026-10-26 09:00:03", "Query", f"SELECT r.qta, a.descrizione FROM ordini_righe r JOIN articoli a "
                                             f"ON a.id_articolo = r.id_articolo WHERE r.id_ordine = {i}"),
            ("2026-10-26 09:00:04", "Query", "SELECT * FROM categorie"),
        ]
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    log = tmp_path / "log.csv"
    log.write_text(buf.getvalue(), encoding="utf-8")
    return folder, log


@pytest.fixture
def result(files):
    return staticmap.analyse(*files)


def row(result, name):
    return next(t for t in result["keys"]["tables"] if t["table"] == name)


def hints(result, kind):
    return {(s["table"], tuple(s["columns"])): s for s in result["suggestions"] if s["kind"] == kind}


def test_keys_relations_areas_and_categories(result):
    k = result["keys"]
    righe = row(result, "ordini_righe")
    assert righe["pk"] == ["id_ordine", "num_riga"] and righe["category"] == "detail"
    assert {(tuple(f["from_cols"]), f["to"], f["kind"], f["relation"]) for f in righe["fk_out"]} == {
        (("id_ordine",), "ordini", "inferred-high", "1:N"), (("id_articolo",), "articoli", "inferred-high", "1:N")}
    ordini = row(result, "ordini")
    assert [(f["to"], f["kind"], f["mandatory"]) for f in ordini["fk_out"]] == [("clienti", "declared", True)]
    assert row(result, "categorie")["category"] == "lookup"
    assert row(result, "log_accessi")["category"] == "history"
    assert row(result, "log_accessi")["area"] == "(no links)"
    # orders with their rows and articles in one area, customers with their categories in another
    assert row(result, "ordini")["area"] == row(result, "ordini_righe")["area"] == row(result, "articoli")["area"]
    assert row(result, "clienti")["area"] == row(result, "categorie")["area"] != row(result, "ordini")["area"]
    assert k["summary"]["declared"] == 1 and k["summary"]["inferred_high"] == 3
    assert "clienti_bak" not in {t["table"] for t in k["tables"]}            # only the tables used


def test_name_groups():
    groups = name_groups(["ordini", "ordini_righe", "tbl_ordine", "clienti", "cliente_indirizzi", "zone"])
    assert groups["ordini"] == groups["ordini_righe"] == groups["tbl_ordine"]
    assert groups["clienti"] == groups["cliente_indirizzi"] != groups["ordini"]
    assert groups["zone"] not in (groups["ordini"], groups["clienti"])


def test_suggestions(result):
    assert hints(result, "unused_table")[("clienti_bak", ())]["confidence"] == "high"
    unused = hints(result, "unused_columns")
    assert ("articoli", ("prezzo",)) in unused
    assert unused[("categorie", ("descrizione",))]["confidence"] != "high"      # read by SELECT *
    assert ("log_accessi", ("utente",)) in hints(result, "write_only")
    assert ("log_accessi", ()) in hints(result, "write_only_table")
    constant = hints(result, "constant")
    assert ("ordini", ("citta",)) in constant and ("ordini", ("stato",)) in constant
    assert ("ordini", ("id_cliente",)) not in constant
    assert ("ordini", ("data_ins", "data_mod")) in hints(result, "same_values")
    assert ("clienti", ("telefono1", "telefono2")) in hints(result, "repeated_group")
    assert ("ordini", ("citta",)) in hints(result, "copied_column")
    similar = [s for s in result["suggestions"] if s["kind"] == "similar_tables"]
    assert any("clienti_bak" in s["reason"] for s in similar)


def test_create_script(result):
    catalog, keys = result["_context"]["catalog"], result["keys"]
    text = schemaout.create_script(catalog, keys, schemaout.chosen_tables(catalog, keys), "log.csv")
    assert "CREATE TABLE `ordini_righe`" in text and "clienti_bak" not in text
    assert ("  ,CONSTRAINT `fk_ordini_righe_id_articolo_articoli` FOREIGN KEY (`id_articolo`) REFERENCES `articoli` "
            "(`id_articolo`)") in text
    assert text.index("-- Area: ") < text.index("CREATE TABLE `articoli`") < text.index("CREATE TABLE `ordini_righe`")
    assert "SET FOREIGN_KEY_CHECKS = 0;" in text and text.rstrip().endswith("SET FOREIGN_KEY_CHECKS = 1;")
    area = row(result, "clienti")["area"]
    one_area = schemaout.create_script(catalog, keys, schemaout.chosen_tables(catalog, keys, area))
    assert "CREATE TABLE `clienti`" in one_area and "CREATE TABLE `ordini`" not in one_area


def test_dump_of_the_tables_used(result, monkeypatch):
    monkeypatch.setattr(schemaout, "_PIECE", 64)              # long lines are read in pieces: the same result
    names = schemaout.chosen_tables(result["_context"]["catalog"], result["keys"])
    text = b"".join(schemaout.dump_chunks(result["_context"]["files"], names)).decode()
    assert "INSERT INTO `clienti` VALUES (1,'Rossi; srl','Roma','1','2',1),(2,'Bianchi','Milano',NULL,NULL,2);" in text
    assert "-- Table structure for table `clienti`" in text
    assert "/*!40000 ALTER TABLE `clienti` DISABLE KEYS */;" in text and "UNLOCK TABLES;" in text
    assert "INSERT INTO `ordini` VALUES" in text and "/*!40101 SET @OLD_CHARACTER_SET_CLIENT" in text
    assert "clienti_bak" not in text and "TRIGGER" not in text and "v_clienti" not in text
    assert text.count("UNLOCK TABLES;") == 1


def test_dump_postgres_copy(tmp_path):
    f = tmp_path / "pg.sql"
    f.write_text("CREATE TABLE public.a (id integer);\nCOPY public.a (id) FROM stdin;\n1\n2\n\\.\n\n"
                 "CREATE TABLE public.b (id integer);\nCOPY public.b (id) FROM stdin;\n3\n\\.\n"
                 "ALTER TABLE ONLY public.a ADD CONSTRAINT a_pkey PRIMARY KEY (id);\n", encoding="utf-8")
    text = b"".join(schemaout.dump_chunks([f], ["a"])).decode()
    assert "COPY public.a (id) FROM stdin;\n1\n2\n\\.\n" in text and "a_pkey" in text
    assert "public.b" not in text and "\n3\n" not in text


def test_export_endpoint(tmp_path, files):
    dump, log = files

    async def go():
        profiler = Profiler(Config(db_path=str(tmp_path / "h.sqlite")))
        client = TestClient(TestServer(build_app(profiler)))
        await client.start_server()
        try:
            before = (await client.get("/api/static/export?kind=create")).status
            data = await (await client.post("/api/static", json={"dump": str(dump), "log": str(log)})).json()
            out = {}
            for kind in ("tables", "report", "create", "dump"):
                res = await client.get(f"/api/static/export?kind={kind}")
                out[kind] = (res.status, res.headers.get("Content-Disposition", ""), await res.text())
            return before, data, out
        finally:
            await client.close()
            profiler.storage.close()

    before, data, out = asyncio.run(go())
    assert before == 409 and "_context" not in data and data["keys"]["tables"]
    assert all(status == 200 for status, _, _ in out.values())
    assert 'filename="static-log-create.sql"' in out["create"][1]
    lines = out["tables"][2].lstrip("﻿").splitlines()
    assert any(line.startswith('"clienti_bak","no"') for line in lines)
    assert any(line.startswith('"ordini","yes"') for line in lines)
    assert "## Areas" in out["report"][2] and "## Suggestions" in out["report"][2]
    assert "INSERT INTO `ordini` VALUES" in out["dump"][2]
