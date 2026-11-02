"""Foreign keys that a schema does not declare, deduced from the names of tables and columns.

The rules come from sql-fk-finder (sqlfk/analisi.py, trova_fk), rewritten without its pandas/scipy dependencies:
  A  same name as the single-column primary key of another table      -> "alta" (high confidence)
  B  the column name points to a table: id_cliente, customer_id, ...  -> "media" (medium confidence)
  C  the table contains every column of a composite primary key       -> "alta"
Differently from sql-fk-finder, generic names (codice, nome, ...) are never a link, and the two columns must have
compatible types: the result goes straight into a picture, with no review step in between.
"""
import re
import unicodedata

from .schema import norm

GENERIC = {"id", "nome", "descrizione", "descr", "note", "data", "stato", "tipo", "codice", "valore", "importo",
           "createdat", "updatedat", "deletedat", "datains", "datamod", "utenteins", "utentemod", "attivo", "flag"}

_TYPE_SYNONYMS = {
    "INTEGER": "INT", "INT4": "INT", "SERIAL": "INT", "MEDIUMINT": "INT", "INT8": "BIGINT", "BIGSERIAL": "BIGINT",
    "INT2": "SMALLINT", "CHARACTER": "CHAR", "NCHAR": "CHAR", "NVARCHAR": "VARCHAR", "BOOL": "BOOLEAN",
    "NUMERIC": "DECIMAL", "DATETIME": "TIMESTAMP", "REAL": "FLOAT",
}
_INTEGERS = {"TINYINT", "SMALLINT", "INT", "BIGINT"}
_IRREGULAR = {"people": "person", "children": "child", "statuses": "status", "addresses": "address"}
_ITALIAN = (("chi", ("co", "chio")), ("ghi", ("go", "ghio")), ("che", ("ca",)), ("ghe", ("ga",)), ("cie", ("cia",)),
            ("gie", ("gia",)), ("ce", ("cia", "ca")), ("ge", ("gia", "ga")), ("ci", ("cio", "co", "ce")),
            ("gi", ("gio", "go", "ge")), ("i", ("o", "e", "io", "a")), ("e", ("a",)))


def squeeze(name):
    """COD_ART, CodArt, cod-art -> codart"""
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def base_type(sql_type):
    """INT(11) -> INT; integer -> INT; character varying(80) -> VARCHAR"""
    text = str(sql_type or "").strip().upper()
    if re.match(r"(CHARACTER|CHAR)\s+VARYING\b", text):
        return "VARCHAR"
    m = re.match(r"\w+", text)
    word = m.group(0) if m else ""
    return _TYPE_SYNONYMS.get(word, word)


def compatible(type_a, type_b):
    a, b = base_type(type_a), base_type(type_b)
    return not a or not b or a == b or (a in _INTEGERS and b in _INTEGERS)


def singulars(word):
    """Possible singular forms, Italian and English, including the word itself."""
    w = "".join(c for c in unicodedata.normalize("NFKD", str(word)) if not unicodedata.combining(c))
    w = re.sub(r"[^a-z0-9]", "", w.lower())
    forms = {w}
    if len(w) < 3:
        return forms
    if w in _IRREGULAR:
        forms.add(_IRREGULAR[w])
    for end, replacements in _ITALIAN:
        if w.endswith(end):
            forms.update(w[:-len(end)] + r for r in replacements)
            break
    if w.endswith("ies"):
        forms.add(w[:-3] + "y")
    if re.search(r"(s|x|z|ch|sh)es$", w):
        forms.add(w[:-2])
    if w.endswith("s") and not w.endswith("ss"):
        forms.add(w[:-1])
    return forms


def entity_keys(table):
    """Two names mean the same entity when they share a key: the singulars, without schema and technical prefixes."""
    name = str(table).lower().split(".")[-1]
    return singulars(re.sub(r"^(tbl|tab|tb|t)_", "", name))


def reference(column):
    """id_articolo / articolo_id / articoloId / cod_articolo -> 'articolo'"""
    c = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(column)).lower()
    if c in ("id", "cod", "codice", "fk", "num"):
        return None
    m = re.fullmatch(r"(?:id|cod|codice|fk|num)_?(.+)", c) or re.fullmatch(r"(.+?)_?(?:id|cod|codice|fk)", c)
    return m.group(1) if m else None


def _guess_pk(table):
    """When no primary key is declared: a column called id, or one that names the table itself."""
    for c in table["columns"]:
        ref = reference(c["name"])
        if c["name"].lower() == "id" or (ref and entity_keys(ref) & entity_keys(table["name"])):
            return [c]
    return []


def add_inferred_links(catalog, names):
    """Adds to catalog the links deduced among the tables called names, and returns how many.

    Only these tables are compared: a column gets at most one link (the best one), which must not end up on a table
    that is not in the picture. Guessed primary keys and the new foreign keys are marked on the catalog's columns."""
    tables = {norm(t["name"]): t for t in (catalog.table(n) for n in names) if t}
    pks = {}
    for key, t in tables.items():
        pk = [c for c in t["columns"] if c.get("pk")]
        if not pk:
            pk = _guess_pk(t)
            for c in pk:
                c["pk"] = True
            t["pk_guessed"] = bool(pk)
        pks[key] = pk

    declared = {(norm(l["from"]), squeeze(c)) for l in catalog.links for c in l["from_cols"]}
    pk_of = {}                                           # squeezed column -> tables where it is the whole primary key
    by_entity = {}
    for key, t in sorted(tables.items()):
        if len(pks[key]) == 1:
            pk_of.setdefault(squeeze(pks[key][0]["name"]), []).append(key)
        for k in entity_keys(t["name"]):
            by_entity.setdefault(k, []).append(key)

    found, taken = [], set()

    def add(source, columns, target, target_columns, confidence):
        found.append({"from": tables[source]["name"], "from_cols": [c["name"] for c in columns],
                      "to": tables[target]["name"], "to_cols": [c["name"] for c in target_columns],
                      "kind": f"inferred-{confidence}", "relation": ""})
        for c in columns:
            taken.add((source, squeeze(c["name"])))
            c["fk"] = True
            c["refs"] = sorted(set(c.get("refs") or []) | {tables[target]["name"]})

    # C: all the columns of a composite primary key
    for target, pk in sorted(pks.items()):
        names_pk = [squeeze(c["name"]) for c in pk]
        if len(pk) < 2 or all(n in GENERIC for n in names_pk):
            continue
        for source, t in sorted(tables.items()):
            own = {squeeze(c["name"]): c for c in t["columns"]}
            if source == target or not all(n in own for n in names_pk):
                continue
            columns = [own[n] for n in names_pk]
            if any((source, n) in declared | taken for n in names_pk):
                continue
            if all(compatible(a.get("type"), b.get("type")) for a, b in zip(columns, pk)):
                add(source, columns, target, pk, "alta")

    def free(source, column):
        n = squeeze(column["name"])
        own_pk = pks[source]
        return (source, n) not in declared and (source, n) not in taken and not (
            len(own_pk) == 1 and squeeze(own_pk[0]["name"]) == n)

    # A: the name of another table's primary key
    for source, t in sorted(tables.items()):
        for c in t["columns"]:
            n = squeeze(c["name"])
            if n in GENERIC or not free(source, c):
                continue
            for target in pk_of.get(n, []):
                if target != source and compatible(c.get("type"), pks[target][0].get("type")):
                    add(source, [c], target, pks[target], "alta")
                    break

    # B: the name points to a table
    for source, t in sorted(tables.items()):
        for c in t["columns"]:
            ref = reference(c["name"])
            if not ref or not free(source, c):
                continue
            targets = sorted({k for e in entity_keys(ref) for k in by_entity.get(e, [])})
            for target in targets:
                if target != source and len(pks[target]) == 1 and compatible(c.get("type"), pks[target][0].get("type")):
                    add(source, [c], target, pks[target], "media")
                    break

    catalog.links.extend(found)
    return len(found)
