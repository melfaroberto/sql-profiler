"""Files made from the tables a log uses: the CREATE TABLE script with the keys found, and a dump of those tables only.

* create_script: the original CREATE TABLE statements of the dump (types, ENGINE, COMMENT stay as they are), divided by
  area, with the inferred foreign keys added (active when high confidence, commented with -- otherwise) and the ALTER
  TABLE statements the dump had for them (phpMyAdmin writes its keys that way).
* dump_chunks: the dump files again, keeping only what belongs to the chosen tables (structure, data, keys, locks) and
  the global settings. It is read in pieces like the analysis, so dumps of tens of GB work too; the data is copied
  byte for byte, never decoded.
"""
import re
from datetime import datetime

from .schema import _PIECE, _UTF8_BOM, _without_table_options, norm, open_binary

_MAX_NAME = 64                                           # MySQL identifier length


def _quote(name):
    return "`" + str(name).replace("`", "``") + "`"


def _constraint_name(table, columns, target):
    name = re.sub(r"\W+", "_", f"fk_{table}_{'_'.join(columns)}_{target}")
    return name[:_MAX_NAME]


def _blocked(t):
    """Why MySQL would refuse a foreign key on this table, or ""."""
    tail = t.get("ddl", "")[len(_without_table_options(t.get("ddl", ""))):]
    if re.search(r"\bPARTITION\s+BY\b", tail, re.I):
        return f"{t['name']} is partitioned: MySQL does not accept foreign keys on it"
    if re.search(r"\bENGINE\s*=\s*MyISAM\b", tail, re.I):
        return f"{t['name']} is MyISAM, which has no foreign keys: ENGINE=InnoDB is needed"
    return ""


def _creation_order(names, depends):
    """The tables pointed to first (topological order); the ones in a cycle at the end."""
    missing = {t: {d for d in depends.get(t, ()) if d in names and d != t} for t in names}
    order = []
    while True:
        ready = sorted((t for t, d in missing.items() if not d), key=str.lower)
        if not ready:
            break
        for t in ready:
            order.append(t)
            del missing[t]
        for d in missing.values():
            d.difference_update(ready)
    return order, sorted(missing, key=str.lower)


def chosen_tables(catalog, keys, area=None):
    """Names of the tables used that are in the dump: all of them, or those of one area."""
    rows = [r for r in keys["tables"] if not area or r["area"] == area]
    return [catalog.table(r["table"])["name"] for r in rows if catalog.table(r["table"])]


def create_script(catalog, keys, names, title=""):
    """The text of the .sql file that creates the tables called names."""
    wanted = {norm(n): catalog.table(n) for n in names if catalog.table(n)}
    info = {norm(r["table"]): r for r in keys["tables"]}
    additions, depends = {k: [] for k in wanted}, {k: set() for k in wanted}
    active = commented = 0
    for k, t in wanted.items():
        blocked = _blocked(t)
        for f in info.get(k, {}).get("fk_out", []):
            target = norm(f["to"])
            if f["kind"] == "declared":
                if target in wanted:
                    depends[k].add(target)
                else:
                    additions[k].append(f"  -- note: declared foreign key ({', '.join(f['from_cols'])}) → {f['to']}, "
                                        "a table that is not in this file")
                continue
            text = (f"CONSTRAINT {_quote(_constraint_name(t['name'], f['from_cols'], f['to']))} FOREIGN KEY "
                    f"({', '.join(map(_quote, f['from_cols']))}) REFERENCES {_quote(f['to'])} "
                    f"({', '.join(map(_quote, f['to_cols'])) or '?'})")
            if f["kind"] != "inferred-high":
                why = "to check: medium confidence (the name points to the table)"
            elif not f["types_ok"]:
                why = f"to check: different types ({f['types']})"
            elif target not in wanted:
                why = f"to check: {f['to']} is not in this file"
            elif f["redundant"]:
                why = f"to check: probably redundant, there is already {f['redundant']}"
            elif blocked or _blocked(wanted[target]):
                why = "to check: " + (blocked or _blocked(wanted[target]))
            else:
                why = ""
            if why:
                additions[k].append(f"  -- ,{text}   -- {why}")
                commented += 1
            else:
                additions[k].append(f"  ,{text}   -- inferred from the names, high confidence")
                depends[k].add(target)
                active += 1
        row = info.get(k, {})
        if row.get("pk_guessed") and row.get("pk"):
            additions[k].append(f"  -- ,PRIMARY KEY ({', '.join(map(_quote, row['pk']))})   -- to check: no primary key "
                                "declared, this column looks like it")
        elif not row.get("pk"):
            additions[k].append(f"  -- ,{_quote('id_' + t['name'])} INT NOT NULL AUTO_INCREMENT PRIMARY KEY   -- to check: "
                                "no primary key (entity integrity), an artificial key is proposed")

    areas = {}
    for k in wanted:
        areas.setdefault(info.get(k, {}).get("area", ""), []).append(k)
    area_order = [a["area"] for a in keys["areas"] if a["area"] in areas] + [a for a in areas if a not in
                                                                             {x["area"] for x in keys["areas"]}]
    lines = [f"-- CREATE TABLE of the {len(wanted)} tables used{' by ' + title if title else ''}",
             f"-- Generated by SQL Profiler on {datetime.now():%Y-%m-%d %H:%M}, from the dump.",
             f"-- {active} foreign keys inferred and active, {commented} to check (commented with --: remove the two",
             "-- dashes at the start of the line to activate one). The declared ones are already in the statements.",
             "-- Tables divided by area (tables closely linked by foreign keys); in each area the tables pointed to come first.",
             "", "SET FOREIGN_KEY_CHECKS = 0;", ""]
    for area in area_order:
        members = areas[area]
        order, cycles = _creation_order(members, depends)
        lines += [f"-- {'=' * 70}", f"-- Area: {area or '-'}  ({len(members)} table{'s' if len(members) != 1 else ''})",
                  f"-- {'=' * 70}", ""]
        for k in order + cycles:
            t = wanted[k]
            row = info.get(k, {})
            lines.append(f"-- {t['name']}: {row.get('category', '')}, subgroup {row.get('subgroup', '')}")
            ddl = t.get("ddl") or ""
            if not ddl:
                lines += [f"-- the CREATE TABLE of {t['name']} could not be read from the dump", ""]
                continue
            head = _without_table_options(ddl)
            body, tail = head[:-1].rstrip(), ddl[len(head):]
            if additions[k]:
                lines.append(body + "\n" + "\n".join(additions[k]) + "\n)" + tail + ";")
            else:
                lines.append(ddl + ";")
            lines.append("")
    alters = [(wanted[k]["name"], a) for k in wanted for a in wanted[k].get("alters", [])]
    if alters:
        lines += ["-- " + "=" * 70, "-- Keys and indexes the dump adds with ALTER TABLE", "-- " + "=" * 70, ""]
        lines += [a + ";\n" for _, a in alters]
    lines += ["SET FOREIGN_KEY_CHECKS = 1;", ""]
    return "\n".join(lines)


# ====================================================================== dump of some tables

_QNAME = rb"((?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[\w$]+)(?:\s*\.\s*(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[\w$]+))?)"
_TABLE_STATEMENT = re.compile(
    rb"^[ \t\r]*(?:/\*!\d+\s+)?(?:CREATE\s+(?:TEMPORARY\s+)?TABLE(?:\s+IF\s+NOT\s+EXISTS)?|DROP\s+TABLE(?:\s+IF\s+EXISTS)?"
    rb"|(?:INSERT|REPLACE)(?:\s+(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE))*(?:\s+INTO)?|LOCK\s+TABLES"
    rb"|ALTER\s+TABLE(?:\s+ONLY)?(?:\s+IF\s+EXISTS)?|TRUNCATE(?:\s+TABLE)?|COPY"
    rb"|CREATE\s+(?:UNIQUE\s+)?INDEX\s+[^\n]*?\bON(?:\s+ONLY)?)\s+" + _QNAME, re.I)
_ROUTINE = re.compile(rb"^[ \t\r]*(?:/\*!\d+\s+)?(?:CREATE|DROP)\b[^\n]*?\b(?:VIEW|PROCEDURE|FUNCTION|TRIGGER|EVENT)\b", re.I)
_UNLOCK = re.compile(rb"^[ \t\r]*UNLOCK\s+TABLES", re.I)
_DELIMITER = re.compile(rb"^[ \t\r]*DELIMITER\s+(\S+)", re.I)
_TABLE_COMMENT = re.compile(rb"^[ \t\r]*--[^\n]*\btable\s+" + _QNAME + rb"\s*$", re.I)   # the last "table x"
_COMMENT = re.compile(rb"^[ \t\r]*(?:--|#)")
_COPY_FROM_STDIN = re.compile(rb"\bFROM\s+stdin\s*;\s*$", re.I)
_CHUNK = 1024 * 1024


def _table_key(raw):
    name = raw.decode("utf-8", "replace").split(".")[-1].strip().strip('`"[]')
    return name.lower()


def dump_chunks(files, names, title=""):
    """Bytes of a .sql dump with only the tables called names, in chunks of about 1 MB."""
    wanted = {norm(n) for n in names}
    out = bytearray()
    header = [f"-- Dump of the {len(wanted)} tables used{' by ' + title if title else ''}",
              f"-- Generated by SQL Profiler on {datetime.now():%Y-%m-%d %H:%M}: the statements of the original dump that",
              "-- belong to these tables (structure, data, keys), plus the global settings. Views, procedures, functions",
              "-- and triggers are left out: they may use tables that are not here.", "",
              "SET FOREIGN_KEY_CHECKS = 0;", ""]
    out += "\n".join(header).encode() + b"\n"
    for path in files:
        out += f"\n-- ===== from {getattr(path, 'name', path)}\n".encode()
        keep, locked, inside, copying, delimiter = True, False, False, False, b";"
        line_start, first, line_keep, statement, copy_line, tail = True, True, True, False, False, b""
        with open_binary(path) as f:
            while True:
                piece = f.readline(_PIECE)
                if not piece:
                    break
                if first:
                    piece, first = piece.removeprefix(_UTF8_BOM), False
                starts, line_start = line_start, piece.endswith(b"\n")
                if starts:                               # what this line is, from its beginning
                    tail, statement, copy_line = b"", False, False
                    if copying:                          # COPY ... FROM stdin: rows up to \.
                        line_keep = keep
                        copying = piece.rstrip(b"\r\n") != b"\\."
                    elif delimiter != b";":              # DELIMITER ;; ... : procedures and triggers, left out
                        line_keep = False
                        m = _DELIMITER.match(piece)
                        if m and m.group(1) == b";":
                            delimiter = b";"
                    elif inside:                         # the next line of a statement
                        line_keep, statement = keep, True
                    elif _DELIMITER.match(piece):
                        delimiter, line_keep = _DELIMITER.match(piece).group(1), False
                    elif not piece.strip():
                        line_keep = keep
                    elif _COMMENT.match(piece):
                        m = _TABLE_COMMENT.match(piece)
                        if m:
                            keep = _table_key(m.group(1)) in wanted
                        line_keep = keep
                    else:                                # a new statement
                        statement = True
                        m = _TABLE_STATEMENT.match(piece)
                        if m:
                            keep = _table_key(m.group(1)) in wanted
                            copy_line = piece.lstrip().upper().startswith(b"COPY")
                            if re.match(rb"^[ \t\r]*LOCK\s+TABLES", piece, re.I):
                                locked = keep
                        elif _UNLOCK.match(piece):
                            keep = locked
                        elif _ROUTINE.match(piece):
                            keep = False
                        else:
                            keep = True                  # SET, USE, CREATE DATABASE...: global
                        line_keep = keep
                if line_keep:
                    out += piece
                if statement:
                    tail = (tail + piece[-512:])[-512:]
                    if line_start:                       # the line is over: is the statement over too?
                        text = tail.rstrip()
                        inside = not text.endswith(b";")
                        if not inside and copy_line and _COPY_FROM_STDIN.search(text):
                            copying = True
                if len(out) >= _CHUNK:
                    yield bytes(out)
                    out.clear()
        if not line_start:
            out += b"\n"
    out += b"\nSET FOREIGN_KEY_CHECKS = 1;\n"
    yield bytes(out)


# ====================================================================== the list of the tables and the report

def _cell(value):
    return '"' + str(value).replace('"', '""') + '"'


def tables_csv(result, catalog):
    """Every table of the dump, used or not, with area, category and keys: one row each (opens in Excel)."""
    keys = {norm(r["table"]): r for r in result["keys"]["tables"]}
    ops = {}
    for op, tables in result["groups"].items():
        for t in tables:
            ops.setdefault(norm(t["table"]), {})[op] = t["queries"]
    hints = {}
    for s in result.get("suggestions", []):
        hints.setdefault(norm(s["table"]), []).append(s["kind"])
    header = ["table", "used_by_log", "in_dump", "operations", "executions", "area", "subgroup", "category", "attributes",
              "primary_key", "foreign_keys", "pointed_to_by", "suggestions"]
    lines = [",".join(header)]
    names = sorted({*(t["name"] for t in catalog.tables.values()), *(t["table"] for g in result["groups"].values()
                                                                       for t in g)}, key=str.lower)
    for name in names:
        k = norm(name)
        t, row, used = catalog.table(name), keys.get(k, {}), ops.get(k, {})
        pk = row.get("pk") or ([c["name"] for c in t["columns"] if c.get("pk")] if t else [])
        fks = row.get("fk_out") or [{"from_cols": l["from_cols"], "to": l["to"], "kind": l["kind"]}
                                    for l in catalog.links if norm(catalog.canonical(l["from"])) == k]
        lines.append(",".join(_cell(v) for v in [
            name, "yes" if used else "no", "yes" if t else "no",
            " ".join(op.upper() for op in ("select", "insert", "update", "delete") if op in used),
            sum(used.values()), row.get("area", ""), row.get("subgroup", ""), row.get("category", ""),
            len(t["columns"]) if t else "", ", ".join(pk) + (" (guessed)" if row.get("pk_guessed") else ""),
            "; ".join(f"{', '.join(f['from_cols'])} → {f['to']} ({f['kind']})" for f in fks),
            ", ".join(sorted({f["from"] for f in row.get("fk_in", [])})), ", ".join(sorted(set(hints.get(k, []))))]))
    return "﻿" + "\r\n".join(lines) + "\r\n"                       # BOM: for Excel


def report_md(result):
    """Keys, relations, areas and suggestions of the tables used, as Markdown."""
    from .suggest import KINDS
    k, s = result["keys"], result["summary"]
    ks = k["summary"]
    out = [f"# Tables used by {result['log']}", "",
           f"- {s['tables']} tables used out of the {s['dump_tables']} of the dump; {len(result['unused'])} never used",
           f"- {s['statements']} statements" + (f", from {s['first']} to {s['last']}" if s.get("first") else ""),
           f"- primary keys: {ks['with_pk']} tables have one, {ks['no_pk']} do not (or it is guessed)",
           f"- foreign keys: {ks['declared']} declared, {ks['inferred_high']} inferred with high confidence, "
           f"{ks['inferred_medium']} with medium confidence (to check)",
           f"- relations: {ks['one_to_one']} 1:1, {ks['one_to_many']} 1:N, {ks['bridges']} N:M (bridge tables)",
           f"- {ks['areas']} areas, {ks['redundant']} links probably redundant, {ks['type_mismatch']} with different types, "
           f"{ks['to_unused']} towards tables the log does not use", ""]
    by_table = {r["table"]: r for r in k["tables"]}
    out += ["## Areas", ""]
    for a in k["areas"]:
        cats = ", ".join(f"{c} {n}" for c, n in a["categories"].items())
        out += [f"### {a['area']} — {len(a['tables'])} tables", "", f"Categories: {cats}."
                + (f" Central tables: {', '.join(a['central'])}." if a["central"] else "")
                + (f" Linked to the areas: {', '.join(a['linked_to'])}." if a["linked_to"] else ""), ""]
        for g in a["subgroups"]:
            out.append(f"- **{g['subgroup']}**: " + ", ".join(f"{t} ({by_table[t]['category']})" for t in g["tables"]))
        out.append("")
    if k["area_links"]:
        out += ["### Links between areas", "", "| areas | links | through |", "|---|---|---|"]
        out += [f"| {' ↔ '.join(v['areas'])} | {v['links']} | {'; '.join(v['through'][:6])}{' …' if len(v['through']) > 6 else ''} |"
                for v in k["area_links"]]
        out.append("")
    out += ["## Tables: primary and foreign keys", ""]
    for r in k["tables"]:
        out += [f"### {r['table']}", "",
                f"- area {r['area']}, subgroup {r['subgroup']}, category **{r['category']}** — {k['categories'][r['category']]}",
                f"- primary key: {', '.join(r['pk']) or 'none'}{' (guessed: not declared)' if r['pk_guessed'] else ''}"]
        for f in r["fk_out"]:
            notes = [f["kind"], f["relation"], "mandatory" if f["mandatory"] else "optional"]
            if not f["types_ok"]:
                notes.append(f"types {f['types']}" if f["target_in_dump"] else "table not in the dump")
            if f["target_in_dump"] and not f["target_used"]:
                notes.append("table not used by the log")
            if f["redundant"]:
                notes.append(f"redundant: {f['redundant']}")
            out.append(f"- FK ({', '.join(f['from_cols'])}) → {f['to']} ({', '.join(f['to_cols'])}) — {', '.join(notes)}")
        if r["fk_in"]:
            out.append("- pointed to by: " + ", ".join(sorted({f"{f['from']} ({', '.join(f['from_cols'])})" for f in r["fk_in"]})))
        if r["bridge"]:
            out.append(f"- bridge N:M between {' and '.join(r['bridge']['links'])}")
        out.append("")
    if k["no_pk"]:
        out += ["## Tables without a primary key", "", "| table | guessed key | suggestion |", "|---|---|---|"]
        out += [f"| {r['table']} | {r['guessed'] or '-'} | {r['suggestion']} |" for r in k["no_pk"]]
        out.append("")
    sug = result.get("suggestions", [])
    if sug:
        out += ["## Suggestions: irrelevant or repetitive", "",
                "Hints to check: the log covers a period, and procedures, triggers or other programs can use what it does not show.", ""]
        for kind, label in KINDS.items():
            items = [x for x in sug if x["kind"] == kind]
            if items:
                out += [f"### {label} ({len(items)})", ""]
                out += [f"- [{x['confidence']}] **{x['table']}**" + (f" — {', '.join(x['columns'])}" if x["columns"] else "")
                        + f": {x['reason']}" for x in items]
                out.append("")
    out += ["## Tables never used by the log", "", ", ".join(result["unused"]) or "none", ""]
    return "\n".join(out)
