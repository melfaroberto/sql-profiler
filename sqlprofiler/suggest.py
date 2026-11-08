"""Suggestions: tables and attributes that look irrelevant or repetitive, judged from the log and the dump together.

Every suggestion says why and how sure it is (high / medium / low). They are hints to check, never certainties: a log
covers a period of time, and procedures, triggers, reports or other programs can use what this log does not show.

Irrelevant
  unused_table     a table of the dump that the log never uses (surer when it is isolated or looks like a copy)
  unused_columns   attributes of a used table that no statement names (weaker when the table is read with SELECT *)
  write_only       attributes written but never read nor used in a filter
  write_only_table a table that is only written: a log or audit trail, or data nobody reads
  constant         an attribute that every INSERT / UPDATE writes with the same value (often NULL: never given one)
Repetitive
  same_values      two attributes of a table that receive the same value in every execution
  copied_column    an attribute that repeats one of the table it points to (third normal form)
  repeated_group   numbered attributes: telefono1, telefono2... (first normal form)
  similar_tables   tables with (nearly) the same attributes: copies, backups, tables split by year
  redundant_link   a foreign key that repeats a path already there (X -> Z when X -> Y -> Z)
"""
import re
from itertools import combinations

from .schema import norm
from .tablekeys import GENERIC, name_parts

KINDS = {
    "unused_table": "Table never used by the log",
    "unused_columns": "Attributes never used",
    "write_only": "Attributes written but never read",
    "write_only_table": "Table only written, never read",
    "constant": "Attribute always written with the same value",
    "same_values": "Attributes that always receive the same value",
    "copied_column": "Attribute copied from a linked table",
    "repeated_group": "Repeated group of attributes",
    "similar_tables": "Tables with the same structure",
    "redundant_link": "Redundant link",
}
FAMILY = {k: "repetitive" if k in ("same_values", "copied_column", "repeated_group", "similar_tables", "redundant_link")
          else "irrelevant" for k in KINDS}
_COPY_WORDS = {"bak", "backup", "old", "tmp", "temp", "copy", "copia", "test", "prova", "vecchio", "vecchia", "storico",
               "archive", "archivio", "deleted", "del", "orig", "save", "salva"}
_TECHNICAL = GENERIC | {"timestamp", "versione", "version", "rowversion"}
_NOT_A_VALUE = {"(from SELECT)"}
_FUNCTION = re.compile(r"^(?![\'\"]).*[\w)]\s*[(+\-*/]|^(?:CURRENT_\w+|LOCALTIME\w*|SYSDATE)$", re.I)
_RANK = {"high": 0, "medium": 1, "low": 2}


def _squeeze(name):
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _looks_like_copy(name):
    return bool(set(name_parts(name)) & _COPY_WORDS) or bool(re.search(r"(?:^|_)(?:19|20)\d{2}(?:\d{2}){0,2}$", name))


def _by_column(groups):
    """{table key: {"name", "ops", "queries", "star", "columns": {key: {...}}}} across the four operations."""
    out = {}
    for op, tables in groups.items():
        for t in tables:
            if not t["in_dump"]:
                continue
            info = out.setdefault(norm(t["table"]), {"name": t["table"], "ops": {}, "star": set(), "columns": {},
                                                     "variants": []})
            info["ops"][op] = t["queries"]
            if t["all_columns"]:
                info["star"].add(op)             # SELECT *, or INSERT without the list of attributes
            if op in ("insert", "update"):
                info["variants"] += [row for st in t.get("variants", []) for row in st["rows"]]
            for c in t["columns"]:
                col = info["columns"].setdefault(norm(c["name"]), {"name": c["name"], "pk": c["pk"], "fk": c["fk"],
                                                                   "in_dump": c["in_dump"], "named": set(),
                                                                   "values": {}, "more": False})
                col["named"] |= set(c.get("named", []))
                if t["all_columns"] and op in ("insert", "update"):
                    col["named"].add("write")    # INSERT INTO t VALUES (...): every attribute is written
                if op in ("insert", "update"):
                    for value, n in c.get("values", []):
                        col["values"][value] = col["values"].get(value, 0) + n
                    col["more"] = col["more"] or c.get("more_values", False)
    return out


def _confidence(executions, weak=False):
    level = "high" if executions >= 20 else "medium" if executions >= 3 else "low"
    if weak:
        level = {"high": "medium", "medium": "low", "low": "low"}[level]
    return level


def suggest(groups, catalog, used_names, unused_names, keys, calls=()):
    out = []

    def add(kind, confidence, table, columns, reason):
        out.append({"kind": kind, "family": FAMILY[kind], "confidence": confidence, "table": table,
                    "columns": list(columns), "reason": reason})

    tables = _by_column(groups)
    linked = {}                                          # table key -> tables it is linked to by a key
    for l in catalog.links:
        a, b = norm(catalog.canonical(l["from"])), norm(catalog.canonical(l["to"]))
        linked.setdefault(a, set()).add(b)
        linked.setdefault(b, set()).add(a)
    used_keys = {norm(n) for n in used_names}

    # ---- tables never used
    for name in unused_names:
        k = norm(name)
        to_used = sorted(catalog.table(x)["name"] for x in linked.get(k, ()) if x in used_keys and catalog.table(x))
        if _looks_like_copy(name):
            add("unused_table", "high", name, [], "never used by the log, and the name looks like a copy, a backup or a "
                                                  "test (" + ", ".join(sorted(set(name_parts(name)) & _COPY_WORDS) or ["a date"]) + ")")
        elif to_used:
            add("unused_table", "low", name, [], "never used by the log, but linked by a key to tables it uses ("
                + ", ".join(to_used[:4]) + "): it may be used by procedures, triggers or in another period")
        elif linked.get(k):
            add("unused_table", "medium", name, [], "never used by the log; linked only to other tables the log does not use")
        else:
            add("unused_table", "high" if not calls else "medium", name, [],
                "never used by the log and not linked to any table" + (" (the log calls stored procedures: check them)"
                                                                       if calls else ""))

    for k, t in sorted(tables.items()):
        executions = sum(t["ops"].values())
        cols = list(t["columns"].values())
        known = [c for c in cols if c["in_dump"]]
        read_star = "select" in t["star"]

        # ---- attributes never named
        never = [c for c in known if not c["named"] and not c["pk"]]
        if never:
            weak = read_star or executions < 3
            share = f"{len(never)} of {len(known)} attributes"
            reason = (f"{share} are never named by a statement in {executions} executions"
                      + (" — the table is read with SELECT *, so they are read but maybe never needed" if read_star else ""))
            fks = [c["name"] for c in never if c["fk"]]
            if fks:
                reason += f"; foreign keys among them: {', '.join(fks)}"
            add("unused_columns", _confidence(executions, weak), t["name"], [c["name"] for c in never], reason)

        # ---- written, never read
        if not read_star:
            write_only = [c for c in known if c["named"] and c["named"] <= {"write"} and not c["pk"]]
            if write_only:
                add("write_only", _confidence(executions, weak=executions < 20), t["name"], [c["name"] for c in write_only],
                    "written by INSERT / UPDATE but never read nor used in a WHERE or JOIN: kept for an audit, by "
                    "another program, or no longer needed")

        # ---- table only written
        if set(t["ops"]) <= {"insert", "update", "delete"} and "insert" in t["ops"]:
            add("write_only_table", _confidence(executions), t["name"], [],
                f"{executions} writes ({', '.join(f'{op.upper()} {n}' for op, n in t['ops'].items())}) and no SELECT: a "
                "log or audit trail, or data that this program never reads back")

        # ---- always the same value
        for c in known:
            values = {v: n for v, n in c["values"].items() if v not in _NOT_A_VALUE and not v.endswith("…")}
            if any(_FUNCTION.match(v) for v in values):
                continue                         # NOW(), UUID(), id + 1: a new value every time
            total = sum(values.values())
            if len(values) == 1 and not c["more"] and total >= 3 and not c["pk"]:
                value = next(iter(values))
                add("constant", "high" if total >= 10 else "medium", t["name"], [c["name"]],
                    (f"never given a value: NULL (or its default) in all {total} writes" if value == "NULL"
                     else f"written as {value} in all {total} writes"))

        # ---- two attributes with the same value in every execution
        rows = [r for r in t["variants"] if r.get("values")]
        names = sorted({key for r in rows for key in r["values"]})[:60]
        for a, b in combinations(names, 2):
            agree = differ = 0
            for r in rows:
                va, vb = r["values"].get(a), r["values"].get(b)
                if va is None or vb is None or "NULL" in (va, vb) or va.endswith("…") or vb.endswith("…") \
                        or va in _NOT_A_VALUE:
                    continue
                if va == vb:
                    agree += r["count"]
                else:
                    differ += 1
                    break
            if agree >= 3 and not differ:
                ca, cb = t["columns"].get(a), t["columns"].get(b)
                add("same_values", "high" if agree >= 10 else "medium", t["name"],
                    [ca["name"] if ca else a, cb["name"] if cb else b],
                    f"they receive the same value in all the {agree} executions where both are written: one of the "
                    "two may be redundant")

        # ---- numbered attributes
        groups_of = {}
        for c in known:
            m = re.fullmatch(r"(.*?[a-z])_?(\d+)", c["name"].lower())
            if m and len(m.group(1)) >= 3:
                groups_of.setdefault(m.group(1), []).append(c)
        for root, members in groups_of.items():
            if len(members) >= 2:
                unused = [c["name"] for c in members if not c["named"]]
                add("repeated_group", "medium" if unused else "low", t["name"], [c["name"] for c in members],
                    "the same piece of information in numbered attributes (first normal form): a related table with one "
                    "row per value would replace them" + (f"; never used by the log: {', '.join(unused)}" if unused else ""))

    # ---- attributes copied from the linked table (third normal form)
    seen = set()
    for f in keys["fks"]:
        if not f["target_used"] or f["kind"] == "inferred-medium" or norm(f["from"]) == norm(f["to"]):
            continue
        src, dst = catalog.table(f["from"]), catalog.table(f["to"])
        if not src or not dst:
            continue
        fk_cols = {_squeeze(c) for c in f["from_cols"]}
        dst_cols = {_squeeze(c["name"]): c for c in dst["columns"] if not c.get("pk")}
        usage = tables.get(norm(src["name"]), {}).get("columns", {})
        for c in src["columns"]:
            n = _squeeze(c["name"])
            if c.get("pk") or c.get("fk") or n in fk_cols or n in _TECHNICAL or n not in dst_cols or (src["name"], n) in seen:
                continue
            seen.add((src["name"], n))
            named = usage.get(norm(c["name"]), {}).get("named", set())
            add("copied_column", "medium" if not named else "low", src["name"], [c["name"]],
                f"also in {dst['name']}, which {src['name']} points to through {', '.join(f['from_cols'])}: probably a copy "
                f"(transitive dependency) that a join would give" + ("; the log never uses it here" if not named else ""))

    # ---- tables with the same structure
    signature = {}
    for t in catalog.tables.values():
        cols = frozenset(_squeeze(c["name"]) for c in t["columns"])
        if len(cols) >= 3:
            signature.setdefault(cols, []).append(t["name"])
    reported = set()
    for cols, names in signature.items():
        if len(names) > 1:
            names = sorted(names, key=lambda n: (norm(n) not in used_keys, n.lower()))
            for other in names[1:]:
                reported.add(frozenset((names[0], other)))
            used_here = [n for n in names if norm(n) in used_keys]
            add("similar_tables", "high" if len(used_here) < len(names) else "medium", names[0], [],
                f"{', '.join(names)} have exactly the same {len(cols)} attributes"
                + (f"; the log uses only {', '.join(used_here)}" if used_here and len(used_here) < len(names)
                   else "; the log uses none of them" if not used_here else ""))
    used_tables = [catalog.table(n) for n in used_names if catalog.table(n)]
    for a in used_tables:
        ca = frozenset(_squeeze(c["name"]) for c in a["columns"])
        if len(ca) < 4:
            continue
        for b in catalog.tables.values():
            if b is a or frozenset((a["name"], b["name"])) in reported:
                continue
            cb = frozenset(_squeeze(c["name"]) for c in b["columns"])
            if len(cb) < 4 or abs(len(ca) - len(cb)) > max(len(ca), len(cb)) * 0.2:
                continue
            similarity = len(ca & cb) / len(ca | cb)
            if similarity >= 0.8:
                reported.add(frozenset((a["name"], b["name"])))
                add("similar_tables", "medium" if norm(b["name"]) not in used_keys else "low", a["name"], [],
                    f"{a['name']} and {b['name']} share {len(ca & cb)} of {len(ca | cb)} attributes ({similarity:.0%})"
                    + ("; the log does not use " + b["name"] if norm(b["name"]) not in used_keys else ""))

    # ---- redundant links
    for f in keys["fks"]:
        if f["redundant"]:
            add("redundant_link", "low", f["from"], f["from_cols"],
                f"the link to {f['to']} repeats the path {f['redundant']}: probably redundant")

    out.sort(key=lambda s: (list(KINDS).index(s["kind"]), _RANK[s["confidence"]], s["table"].lower()))
    return out
