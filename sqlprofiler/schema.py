"""Static schema metadata (tables, columns, primary and foreign keys) to cross-reference captured queries.

Sources, in order of preference:
  * the JSON model exported by sql-fk-finder (Modello relazionale -> json, or
    `python -m sqlfk.modello <folder> --formato json`): declared FKs plus the ones it inferred;
  * a folder of .sql dumps analysed by the sql-fk-finder package itself (--sqlfk-path), same result;
  * a folder of .sql dumps or a single .sql file read by the built-in DDL reader: declared FKs only.
"""
import gzip
import io
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
        self.problems = []          # what the loader could not read
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


_DDL = re.compile(r"^\s*(CREATE\s+(TEMPORARY\s+)?TABLE|ALTER\s+TABLE)\b", re.I)
_ALTER_TABLE = re.compile(r"^\s*ALTER\s+TABLE\s+(?:ONLY\s+)?(?P<name>[`\"\w.$]+)", re.I)
_ALTER_PK = re.compile(r"\bADD\s+(?:CONSTRAINT\s+[`\"\w$]*\s+)?PRIMARY\s+KEY\s*(?:USING\s+\w+\s*)?\((?P<cols>[^)]*)\)", re.I)
_ALTER_FK = re.compile(r"\bFOREIGN\s+KEY\s*[`\"\w$]*\s*\((?P<cols>[^)]*)\)\s*REFERENCES\s+(?P<to>[`\"\w.$]+)\s*(?:\((?P<to_cols>[^)]*)\))?", re.I)


# Dumps of tens of GB: big read buffers, and lines read in pieces of bounded size, so that a single huge INSERT line
# (extended inserts, BLOBs) never has to be held in memory whole.
READ_BUFFER = 16 * 1024 * 1024                          # bytes asked to the disk at a time
_PIECE = 4 * 1024 * 1024                                 # longest piece of a line read at a time
_MAX_DDL = 64 * 1024 * 1024                              # a "CREATE TABLE" longer than this is not one: it is data
_DDL_BYTES = re.compile(rb"^[ \t\r]*(CREATE\s+(TEMPORARY\s+)?TABLE|ALTER\s+TABLE)\b", re.I)
_UTF8_BOM = b"\xef\xbb\xbf"


def _is_utf16(path):
    with open(path, "rb") as f:
        return f.read(2) in (b"\xff\xfe", b"\xfe\xff")


def open_binary(path):
    """A file for reading as bytes with a big buffer, gzip compressed or not."""
    path = Path(path)
    if path.suffix.lower() == ".gz":
        return io.BufferedReader(gzip.open(path, "rb"), buffer_size=READ_BUFFER)
    return open(path, "rb", buffering=READ_BUFFER)


def open_text(path):
    """A text file for reading, whatever a server or Windows left it as: gzip compressed, UTF-16 with BOM, or broken bytes."""
    path = Path(path)
    if path.suffix.lower() == ".gz":
        return io.TextIOWrapper(open_binary(path), encoding="utf-8-sig", errors="replace")
    return open(path, encoding="utf-16" if _is_utf16(path) else "utf-8-sig", errors="replace", buffering=READ_BUFFER)


def _ddl_statements(path):
    """CREATE TABLE and ALTER TABLE statements of a dump file, read line by line (dumps can be huge: data lines are
    skipped). ALTER TABLE matters because some tools, like phpMyAdmin, export their keys that way."""
    path = Path(path)
    if path.suffix.lower() != ".gz" and _is_utf16(path):
        yield from _ddl_statements_text(path)
        return
    block, size, line_start, first = None, 0, True, True
    with open_binary(path) as f:
        while True:
            piece = f.readline(_PIECE)                   # a whole line, or the next piece of a long one
            if not piece:
                break
            if first:
                piece, first = piece.removeprefix(_UTF8_BOM), False
            starts, line_start = line_start, piece.endswith(b"\n")
            if block is None:
                if not starts or not _DDL_BYTES.match(piece):
                    continue                             # data: skipped without being decoded
                block, size = [], 0
            block.append(piece)
            size += len(piece)
            if line_start and piece.rstrip().endswith(b";"):
                yield b"".join(block).decode("utf-8", "replace").rstrip().rstrip(";")
                block = None
            elif size > _MAX_DDL:
                log.warning("schema: %s: a CREATE/ALTER TABLE without its ; after %d MB, skipped", path.name, size >> 20)
                block = None


def _ddl_statements_text(path):
    """The same for a UTF-16 dump, which cannot be scanned as bytes."""
    block = None
    with open_text(path) as f:
        for line in f:
            if block is None:
                if not _DDL.match(line):
                    continue
                block = []
            block.append(line)
            if line.rstrip().endswith(";"):
                yield "".join(block).rstrip().rstrip(";")
                block = None


def _without_table_options(ddl):
    """CREATE TABLE ... (...) without what follows the closing parenthesis (ENGINE, PARTITION BY, ...)."""
    depth, quote, i = 0, None, 0
    while i < len(ddl):
        ch = ddl[i]
        if quote:
            if ch == "\\" and quote != "`":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return ddl[:i + 1]
        i += 1
    return ddl


def _parse_create(ddl, dialect):
    try:
        tree = sqlglot.parse_one(ddl, read=dialect)
        if isinstance(tree, exp.Create) and isinstance(tree.this, exp.Schema):
            return tree                                  # options it does not know make it give up without an error
    except (sqlglot.errors.ParseError, sqlglot.errors.TokenError):
        pass
    return sqlglot.parse_one(_without_table_options(ddl), read=dialect)


def _apply_alter(ddl, by_name, links):
    """The keys added by an ALTER TABLE statement, read with regular expressions: the rest of it does not matter."""
    m = _ALTER_TABLE.match(ddl)
    table = by_name.get(norm(m.group("name")).rsplit(".", 1)[-1]) if m else None
    if not table:
        return
    split = lambda text: [norm(c.split("(")[0]) for c in text.split(",") if c.strip()]
    for pk in _ALTER_PK.finditer(ddl):
        wanted = set(split(pk.group("cols")))
        for c in table["columns"]:
            if norm(c["name"]) in wanted:
                c["pk"] = True
    for fk in _ALTER_FK.finditer(ddl):
        links.append({"from": table["name"], "from_cols": split(fk.group("cols")),
                      "to": norm(fk.group("to")).rsplit(".", 1)[-1], "to_cols": split(fk.group("to_cols") or ""),
                      "kind": "declared", "relation": ""})


def from_ddl(path, dialect="mysql"):
    """Built-in reader: columns, types, primary keys and declared foreign keys."""
    p = Path(path)
    return from_ddl_files(sorted(p.glob("*.sql")) if p.is_dir() else [p], dialect, f"DDL in {p}")


def from_ddl_files(files, dialect="mysql", source=None):
    """The same for a list of files. The statements that could not be read are in the catalog's .problems."""
    tables, links, alters, problems = [], [], [], []
    for file in files:
        for ddl in _ddl_statements(file):
            if _ALTER_TABLE.match(ddl):
                alters.append(ddl)
                continue
            try:
                tree = _parse_create(ddl, dialect)
            except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
                log.warning("schema: cannot parse a CREATE TABLE in %s: %s", Path(file).name, str(e).splitlines()[0])
                problems.append(f"{Path(file).name}: cannot read “{' '.join(ddl.split())[:80]}…”")
                continue
            t, l = _table_from_create(tree, dialect)
            if t:
                tables.append(t)
                links += l
            else:
                problems.append(f"{Path(file).name}: cannot read “{' '.join(ddl.split())[:80]}…”")
    by_name = {norm(t["name"]): t for t in tables}
    for ddl in alters:
        _apply_alter(ddl, by_name, links)
    links = _dedupe(links)
    for l in links:
        src = by_name.get(norm(l["from"]))
        for c in src["columns"] if src else []:
            if norm(c["name"]) in {norm(x) for x in l["from_cols"]}:
                c["fk"] = True
                c["refs"] = sorted(set(c["refs"]) | {l["to"]})
    cat = SchemaCatalog(tables, links, source or "DDL in " + ", ".join(Path(f).name for f in files))
    cat.problems = problems
    log.info("schema: %d tables, %d declared foreign keys from %s", len(cat.tables), len(cat.links), cat.source)
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
