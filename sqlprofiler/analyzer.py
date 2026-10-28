"""The Parser & Analyzer: classifies captured SQL with sqlglot and links it to the static schema."""
import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from functools import lru_cache

import sqlglot
from sqlglot import exp

from .schema import SchemaCatalog, norm

logging.getLogger("sqlglot").setLevel(logging.ERROR)

WRITE_TYPES = {"INSERT", "REPLACE", "UPDATE", "DELETE", "MERGE", "TRUNCATE"}
TX_TYPES = {"BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT"}
COLUMN_TYPES = {"SELECT", "INSERT", "REPLACE", "UPDATE", "DELETE", "MERGE"}

_TYPES = [
    (exp.Union, "SELECT"), (exp.Intersect, "SELECT"), (exp.Except, "SELECT"), (exp.Select, "SELECT"),
    (exp.Insert, "INSERT"), (exp.Update, "UPDATE"), (exp.Delete, "DELETE"), (exp.Merge, "MERGE"),
    (exp.Create, "CREATE"), (exp.Drop, "DROP"), (exp.Alter, "ALTER"), (exp.TruncateTable, "TRUNCATE"),
    (exp.Set, "SET"), (exp.Show, "SHOW"), (exp.Use, "USE"), (exp.Describe, "DESCRIBE"),
    (exp.Transaction, "BEGIN"), (exp.Commit, "COMMIT"), (exp.Rollback, "ROLLBACK"),
]

# statements issued by drivers and connection pools rather than by the application logic
_NOISE = re.compile(
    r"^\s*(SET\s|SHOW\s|USE\s|SELECT\s+(@@|DATABASE\(\)|VERSION\(\)|CURRENT_SCHEMA|CURRENT_USER|1\s*$|1\s+FROM\s+DUAL)"
    r"|/\*\s*ping\s*\*/|DEALLOCATE\s|DISCARD\s|RESET\s)", re.I)
# catalog introspection done by drivers (asyncpg type lookups, JDBC metadata, ...)
_CATALOG = re.compile(r"\b(pg_catalog\.|pg_type\b|information_schema\.|current_setting\s*\(|set_config\s*\()", re.I)

_FIRST_WORD = re.compile(r"^\s*(?:/\*.*?\*/\s*|--[^\n]*\n\s*|#[^\n]*\n\s*)*([A-Za-z]+)", re.S)


def first_keyword(sql):
    m = _FIRST_WORD.match(sql)
    word = m.group(1).upper() if m else "OTHER"
    return {"START": "BEGIN", "WITH": "SELECT", "EXEC": "EXECUTE"}.get(word, word)


def statement_type(tree, sql):
    for cls, name in _TYPES:
        if isinstance(tree, cls):
            return name
    return first_keyword(sql)


class QueryAnalyzer:
    """Turns raw SQL into {type, tables, columns, joins, formatted SQL, fingerprint}.

    The analysis only depends on the statement text, so it is cached: applications repeat the same
    statements (usually with placeholders) over and over."""

    def __init__(self, dialect="mysql", schema=None, cache_size=4096):
        self.dialect = dialect
        self.schema = schema or SchemaCatalog()
        self.analyze = lru_cache(maxsize=cache_size)(self._analyze)

    # ------------------------------------------------------------------ main entry

    def _analyze(self, sql):
        result = {"query_type": first_keyword(sql), "statements": 1, "tables": [], "columns": [], "joins": [],
                  "formatted": sql, "fingerprint": _fallback_fingerprint(sql), "parse_error": None,
                  "noise": bool(_NOISE.match(sql) or _CATALOG.search(sql))}
        text, replace = sql, False
        if result["query_type"] == "REPLACE":            # sqlglot does not parse MySQL REPLACE INTO
            text, replace = re.sub(r"^\s*REPLACE", "INSERT", sql, count=1, flags=re.I), True
        try:
            trees = [t for t in sqlglot.parse(text, read=self.dialect) if t is not None]
        except (sqlglot.errors.ParseError, sqlglot.errors.TokenError) as e:
            result["parse_error"] = str(e).splitlines()[0][:300]
            return result
        if not trees:
            return result

        types = [statement_type(t, text) for t in trees]
        if replace:
            types[0] = "REPLACE"
        result["query_type"] = types[0] if len(set(types)) == 1 else "MULTI"
        result["statements"] = len(trees)

        tables, columns, joins = {}, {}, {}
        for tree, typ in zip(trees, types):
            if isinstance(tree, exp.Command):
                continue
            self._collect(tree, typ, tables, columns, joins)
        result["tables"] = list(tables.values())
        result["columns"] = list(columns.values())
        result["joins"] = list(joins.values())
        if result["query_type"] in TX_TYPES:
            result["noise"] = False
        try:
            result["formatted"] = ";\n\n".join(t.sql(self.dialect, pretty=True) for t in trees)
            if replace:
                result["formatted"] = re.sub(r"^INSERT", "REPLACE", result["formatted"], count=1)
            result["fingerprint"] = _fingerprint(trees, self.dialect)
        except Exception:                                # formatting is cosmetic: keep the raw text
            pass
        return result

    # ------------------------------------------------------------------ AST walk

    def _collect(self, tree, typ, tables, columns, joins):
        cte_names = {norm(c.alias) for c in tree.find_all(exp.CTE)}
        aliases = {}                                     # alias or name -> canonical table name
        written = self._write_targets(tree, typ)

        for t in tree.find_all(exp.Table) if typ != "USE" else ():
            if not t.name or (not t.db and norm(t.name) in cte_names):
                continue
            known = self.schema.table(f"{t.db}.{t.name}" if t.db else t.name)
            name = known["name"] if known else t.name
            info = tables.setdefault(norm(name), {"name": name, "schema": t.db or "", "access": "",
                                                  "known": bool(known), "aliases": []})
            if id(t) in written:
                access = "delete" if typ in ("DELETE", "TRUNCATE") else "write"
            else:
                access = "read"
            info["access"] = "/".join(sorted(set(filter(None, info["access"].split("/"))) | {access}))
            aliases[norm(t.alias_or_name)] = name
            aliases[norm(t.name)] = name
            if t.alias and t.alias not in info["aliases"]:
                info["aliases"].append(t.alias)

        if typ not in COLUMN_TYPES:
            return
        real_tables = [v["name"] for v in tables.values()]
        set_columns = self._set_columns(tree, typ)
        in_scope = {}                                    # id(SELECT) -> tables in its FROM/JOINs
        for t in tree.find_all(exp.Table):
            if norm(t.alias_or_name) in aliases:
                scope = t.find_ancestor(exp.Select)
                in_scope.setdefault(id(scope) if scope else None, []).append(aliases[norm(t.alias_or_name)])

        def resolve(col):
            if col.table:
                return aliases.get(norm(col.table))
            scope = col.find_ancestor(exp.Select)
            for candidates in (in_scope.get(id(scope) if scope else None, []), real_tables):
                candidates = list(dict.fromkeys(candidates))
                if len(candidates) == 1:
                    return candidates[0]
                owners = [t for t in candidates if self.schema.has_column(t, col.name)]
                if len(owners) == 1:
                    return owners[0]
            return None

        def add_column(table, name, usage):
            info = columns.setdefault((norm(table or ""), norm(name)), {"table": table, "column": name, "usage": []})
            if usage not in info["usage"]:
                info["usage"].append(usage)

        if isinstance(tree, exp.Insert) and isinstance(tree.this, exp.Schema) and isinstance(tree.this.this, exp.Table):
            target = aliases.get(norm(tree.this.this.name))
            for ident in tree.this.expressions:
                add_column(target, ident.name, "write")

        for col in tree.find_all(exp.Column):
            if not col.name or isinstance(col.this, exp.Star):
                continue
            table = resolve(col)
            if col.table and table is None:
                continue                                 # a derived table or a CTE column
            usage = "write" if id(col) in set_columns else (
                "filter" if col.find_ancestor(exp.Where, exp.Join, exp.Having) else "read")
            add_column(table, col.name, usage)

        # equality predicates between columns of two different tables: joins observed at runtime
        for eq in tree.find_all(exp.EQ):
            left, right = eq.this, eq.expression
            if not (isinstance(left, exp.Column) and isinstance(right, exp.Column)):
                continue
            lt, rt = resolve(left), resolve(right)
            if not lt or not rt or norm(lt) == norm(rt):
                continue
            a, b = sorted([(lt, left.name), (rt, right.name)], key=lambda x: (norm(x[0]), norm(x[1])))
            key = (norm(a[0]), norm(a[1]), norm(b[0]), norm(b[1]))
            joins.setdefault(key, {"left_table": a[0], "left_column": a[1], "right_table": b[0], "right_column": b[1],
                                   "in_schema": self.schema.has_link(a[0], a[1], b[0], b[1])})

    @staticmethod
    def _write_targets(tree, typ):
        """ids of the Table nodes that a DML statement modifies."""
        targets = []
        if isinstance(tree, (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.TruncateTable)):
            node = tree.this
            if isinstance(node, exp.Schema):
                node = node.this
            if isinstance(node, exp.Table):
                targets.append(node)
            targets += [t for t in tree.args.get("tables") or [] if isinstance(t, exp.Table)]
            if isinstance(tree, exp.TruncateTable):
                targets += [t for t in tree.expressions if isinstance(t, exp.Table)]
        return {id(t) for t in targets}

    @staticmethod
    def _set_columns(tree, typ):
        """ids of the Column nodes written by UPDATE ... SET or ON DUPLICATE KEY UPDATE."""
        out = set()
        for e in (tree.args.get("conflict") and tree.args["conflict"].args.get("expressions")) or []:
            if isinstance(e, exp.EQ) and isinstance(e.this, exp.Column):
                out.add(id(e.this))
        if isinstance(tree, exp.Update):
            for e in tree.expressions:
                if isinstance(e, exp.EQ) and isinstance(e.this, exp.Column):
                    out.add(id(e.this))
        return out

    # ------------------------------------------------------------------ ER subgraph

    def er_subgraph(self, analysis):
        """Tables involved in one query, with their key columns and the columns the query uses,
        the schema links among them and the joins observed in the query."""
        used = {}
        for c in analysis.get("columns", []):
            if c["table"]:
                used.setdefault(norm(c["table"]), {})[norm(c["column"])] = c["usage"]
        tables = []
        for t in analysis.get("tables", []):
            info = self.schema.table(t["name"])
            mine = used.get(norm(t["name"]), {})
            if info:
                cols = [dict(c, used=mine.get(norm(c["name"]), [])) for c in info["columns"]]
                shown = [c for c in cols if c["pk"] or c["fk"] or c["used"]]
                tables.append({"name": info["name"], "known": True, "access": t["access"], "columns": shown,
                               "hidden": len(cols) - len(shown), "group": info.get("group", "")})
            else:
                cols = [{"name": c["column"], "type": "", "pk": False, "fk": False, "refs": [], "used": c["usage"]}
                        for c in analysis.get("columns", []) if c["table"] and norm(c["table"]) == norm(t["name"])]
                tables.append({"name": t["name"], "known": False, "access": t["access"], "columns": cols,
                               "hidden": 0, "group": ""})
        names = [t["name"] for t in tables]
        links = [dict(l) for l in self.schema.links_between(names)]
        observed = [j for j in analysis.get("joins", []) if not j["in_schema"]]
        return {"tables": tables, "links": links, "observed": observed}


# ====================================================================== fingerprints & literals

def _fingerprint(trees, dialect):
    """Same statement with different literal values -> same fingerprint."""
    parts = []
    for tree in trees:
        t = tree.copy()
        for lit in list(t.find_all(exp.Literal)):
            lit.replace(exp.Placeholder())
        for node in list(t.find_all(exp.In)):
            if node.expressions and all(isinstance(e, exp.Placeholder) for e in node.expressions):
                node.set("expressions", [exp.Placeholder()])
        for node in list(t.find_all(exp.Tuple)):
            if node.expressions and all(isinstance(e, exp.Placeholder) for e in node.expressions):
                node.set("expressions", [exp.Placeholder()])
        values = t.find(exp.Values)
        if values is not None and len(values.expressions) > 1:
            values.set("expressions", values.expressions[:1])
        parts.append(t.sql(dialect).lower())
    return hashlib.sha1(";".join(parts).encode()).hexdigest()[:12]


def _fallback_fingerprint(sql):
    text = re.sub(r"'(?:[^'\\]|\\.|'')*'", "?", sql)
    text = re.sub(r"\b\d+(\.\d+)?\b", "?", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha1(text.encode()).hexdigest()[:12]


def sql_literal(value, dialect="mysql"):
    """Python value -> SQL literal, for showing a prepared statement with its values in place."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        items = ", ".join(sql_literal(v, dialect) for v in value)
        return f"ARRAY[{items}]" if dialect == "postgres" else f"({items})"
    if isinstance(value, bytes):
        return ("X'" + value.hex() + "'") if dialect == "mysql" else ("'\\x" + value.hex() + "'")
    if isinstance(value, (datetime, date, timedelta)):
        value = str(value)
    text = str(value).replace("'", "''")
    if dialect == "mysql":
        text = text.replace("\\", "\\\\")
    return f"'{text}'"


_SKIP = r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.)*\"|`[^`]*`|--[^\n]*|/\*.*?\*/"


def interpolate(sql, params, dialect="mysql"):
    """Replaces ? (MySQL) or $n (PostgreSQL) placeholders with the bound values; strings and comments are skipped."""
    if not params:
        return sql
    literals = [sql_literal(p, dialect) for p in params]
    if dialect == "mysql":
        it = iter(literals)
        pattern = re.compile(_SKIP + r"|\?", re.S)
        return pattern.sub(lambda m: next(it, "?") if m.group(0) == "?" else m.group(0), sql)
    pattern = re.compile(_SKIP + r"|\$(\d+)", re.S)

    def repl(m):
        if m.group(1):
            i = int(m.group(1)) - 1
            return literals[i] if 0 <= i < len(literals) else m.group(0)
        return m.group(0)
    return pattern.sub(repl, sql)


def json_safe(value):
    """Parameter values -> JSON-friendly values."""
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    if isinstance(value, bytes):
        try:
            text = value.decode("utf-8")
            if text.isprintable():
                return text
        except UnicodeDecodeError:
            pass
        return "0x" + value.hex()
    if isinstance(value, (datetime, date, timedelta)):
        return str(value)
    return value
