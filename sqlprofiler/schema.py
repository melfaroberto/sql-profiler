"""Static schema metadata (tables, columns, primary and foreign keys) to cross-reference captured queries.

Sources, in order of preference:
  * the JSON model exported by sql-fk-finder (Modello relazionale -> json, or
    `python -m sqlfk.modello <folder> --formato json`): declared FKs plus the ones it inferred;
  * a folder of .sql dumps analysed by the sql-fk-finder package itself (--sqlfk-path), same result;
  * a folder of .sql dumps or a single .sql file read by the built-in DDL reader: declared FKs only.
"""
import json
import logging
import re
import sys
from pathlib import Path

import sqlglot
from sqlglot import exp

log = logging.getLogger("sqlprofiler.schema")


def norm(name):
    return str(name).strip().strip('`"[]').lower()


class SchemaCatalog:
    def __init__(self, tables=None, links=None, source=""):
        self.tables = {}            # normalized name -> {"name", "columns": [...], "group", "area"}
        self.links = []             # {"from", "from_cols", "to", "to_cols", "kind"}
        self.source = source
        for t in tables or []:
            self.add_table(t)
        for l in links or []:
            self.links.append(l)

    def __bool__(self):
        return bool(self.tables)

    def add_table(self, t):
        self.tables[norm(t["name"])] = t

    def table(self, name):
        """Table info by name, with or without the schema/database qualifier."""
        if not name:
            return None
        key = norm(name)
        return self.tables.get(key) or self.tables.get(key.rsplit(".", 1)[-1])

    def canonical(self, name):
        t = self.table(name)
        return t["name"] if t else name

    def has_column(self, table, column):
        t = self.table(table)
        return bool(t) and any(norm(c["name"]) == norm(column) for c in t["columns"])

    def links_between(self, names):
        """Schema links whose two ends are both in names."""
        wanted = {norm(self.canonical(n)) for n in names}
        return [l for l in self.links if norm(l["from"]) in wanted and norm(l["to"]) in wanted]

    def has_link(self, t1, c1, t2, c2):
        a, b = (norm(self.canonical(t1)), norm(c1)), (norm(self.canonical(t2)), norm(c2))
        for l in self.links:
            for fc, tc in zip(l["from_cols"], l["to_cols"]):
                ends = ((norm(l["from"]), norm(fc)), (norm(l["to"]), norm(tc)))
                if ends in ((a, b), (b, a)):
                    return True
        return False

    def summary(self):
        kinds = {}
        for l in self.links:
            kinds[l["kind"]] = kinds.get(l["kind"], 0) + 1
        return {"source": self.source, "tables": len(self.tables), "links": len(self.links), "link_kinds": kinds}


# ====================================================================== loaders

def load_schema(path, dialect="mysql", sqlfk_path=None):
    """Picks the loader from what path is (see module docstring). Returns an empty catalog when path is None."""
    if not path:
        return SchemaCatalog()
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"schema source not found: {p}")
    if p.suffix.lower() == ".json":
        return from_sqlfk_model(json.loads(p.read_text(encoding="utf-8")), source=str(p))
    if sqlfk_path and p.is_dir():
        return from_sqlfk_package(p, sqlfk_path, dialect)
    return from_ddl(p, dialect)


def from_sqlfk_model(model, source="sql-fk-finder model"):
    """The structure produced by sqlfk.modello.struttura(): {"tabelle": [...], "collegamenti": [...]}"""
    tables = []
    for t in model.get("tabelle", []):
        tables.append({
            "name": t["nome"], "group": t.get("gruppo", ""), "area": t.get("area", ""),
            "pk_guessed": t.get("pk_indovinata", False),
            "columns": [{"name": c["nome"], "type": c.get("tipo", ""), "pk": c.get("pk", False),
                         "fk": c.get("fk", False), "refs": c.get("verso", []), "unique": c.get("unique", False),
                         "not_null": c.get("not_null", False)} for c in t.get("colonne", [])]})
    links = [{"from": l["da"], "from_cols": l["colonne_da"], "to": l["a"], "to_cols": l["colonne_a"],
              "kind": "declared" if l.get("tipo") == "dichiarata" else f"inferred-{l.get('tipo', '')}",
              "relation": l.get("relazione", "")} for l in model.get("collegamenti", [])]
    cat = SchemaCatalog(tables, links, source)
    log.info("schema: %d tables, %d links from %s", len(cat.tables), len(cat.links), source)
    return cat


def from_sqlfk_package(folder, sqlfk_path, dialect="mysql"):
    """Runs the sql-fk-finder analysis (declared + inferred keys) on a folder of dumps."""
    sys.path.insert(0, str(Path(sqlfk_path).resolve()))
    try:
        from sqlfk import analisi as A, modello as MR
    except ImportError as e:
        raise RuntimeError(f"cannot import sqlfk from {sqlfk_path}: {e}") from e
    imp = A.Impostazioni(dialetto=dialect)
    df, pk, fk, _ = A.leggi_cartella(str(folder), imp.dialetto)
    if df.empty:
        raise RuntimeError(f"no CREATE TABLE found in {folder}")
    R = A.analizza(df, pk, fk_dichiarate=fk, imp=imp)
    _, gruppo_di = A.raggruppa_tabelle(df["tabella"].unique(), imp)
    return from_sqlfk_model(MR.struttura(R, gruppo_di, "collegamenti"), source=f"sql-fk-finder analysis of {folder}")


_CREATE = re.compile(r"^\s*CREATE\s+(TEMPORARY\s+)?TABLE\b", re.I)


def _create_statements(path):
    """CREATE TABLE statements of a dump file, read line by line (dumps can be huge: data lines are skipped)."""
    block = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if block is None:
                if not _CREATE.match(line):
                    continue
                block = []
            block.append(line)
            if line.rstrip().endswith(";"):
                yield "".join(block).rstrip().rstrip(";")
                block = None


def from_ddl(path, dialect="mysql"):
    """Built-in reader: columns, types, primary keys and declared foreign keys."""
    p = Path(path)
    files = sorted(p.glob("*.sql")) if p.is_dir() else [p]
    tables, links = [], []
    for file in files:
        for ddl in _create_statements(file):
            try:
                tree = sqlglot.parse_one(ddl, read=dialect)
            except sqlglot.errors.ParseError as e:
                log.warning("schema: cannot parse a CREATE TABLE in %s: %s", file.name, str(e).splitlines()[0])
                continue
            t, l = _table_from_create(tree, dialect)
            if t:
                tables.append(t)
                links += l
    by_name = {norm(t["name"]): t for t in tables}
    for l in links:
        src = by_name.get(norm(l["from"]))
        for c in src["columns"] if src else []:
            if norm(c["name"]) in {norm(x) for x in l["from_cols"]}:
                c["fk"] = True
                c["refs"] = sorted(set(c["refs"]) | {l["to"]})
    cat = SchemaCatalog(tables, links, f"DDL in {p}")
    log.info("schema: %d tables, %d declared foreign keys from %s", len(cat.tables), len(cat.links), p)
    return cat


def _table_from_create(tree, dialect):
    schema = tree.this if isinstance(tree, exp.Create) else None
    if not isinstance(schema, exp.Schema) or not isinstance(schema.this, exp.Table):
        return None, []
    name = schema.this.name
    columns, pk, links = [], set(), []
    for item in schema.expressions:
        if isinstance(item, exp.ColumnDef):
            kinds = [c.args.get("kind") for c in item.args.get("constraints") or []]
            if any(isinstance(k, exp.PrimaryKeyColumnConstraint) for k in kinds):
                pk.add(norm(item.name))
            columns.append({"name": item.name, "type": item.args["kind"].sql(dialect) if item.args.get("kind") else "",
                            "pk": False, "fk": False, "refs": [],
                            "unique": any(isinstance(k, exp.UniqueColumnConstraint) for k in kinds),
                            "not_null": any(isinstance(k, exp.NotNullColumnConstraint) and not k.args.get("allow_null")
                                            for k in kinds)})
        for node in item.find_all(exp.PrimaryKey, exp.ForeignKey):
            if isinstance(node, exp.PrimaryKey):
                pk |= {norm(e.name) for e in node.expressions}
            elif isinstance(node, exp.ForeignKey):
                ref = node.args.get("reference")
                target = ref.this if ref else None
                if isinstance(target, exp.Schema) and isinstance(target.this, exp.Table):
                    links.append({"from": name, "from_cols": [e.name for e in node.expressions],
                                  "to": target.this.name, "to_cols": [e.name for e in target.expressions],
                                  "kind": "declared", "relation": ""})
    for c in columns:
        c["pk"] = norm(c["name"]) in pk
    return {"name": name, "group": "", "area": "", "pk_guessed": False, "columns": columns}, _dedupe(links)


def _dedupe(links):
    seen, out = set(), []
    for l in links:
        key = (l["from"], tuple(l["from_cols"]), l["to"])
        if key not in seen:
            seen.add(key)
            out.append(l)
    return out
