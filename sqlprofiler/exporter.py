"""Export of recorded sessions to JSON or Markdown, to document how the target software works internally."""
import csv
import io
import json
import time

from .analyzer import interpolate
from .schema import norm

DIALECTS = {"mysql": "mysql", "mariadb": "mysql", "postgres": "postgres", "postgresql": "postgres"}


def summarize(queries):
    """What a group of queries touches: tables (and how), written columns, joins and distinct statements."""
    tables, written, joins, distinct = {}, {}, {}, {}
    for q in queries:
        for t in q.get("tables") or []:
            info = tables.setdefault(t["name"], {"table": t["name"], "access": set(), "statements": 0,
                                                 "known": t.get("known", False)})
            info["access"] |= set(t["access"].split("/"))
            info["statements"] += 1
        for c in q.get("columns") or []:
            if "write" in c["usage"] and c["table"]:
                written.setdefault(c["table"], set()).add(c["column"])
        for j in q.get("joins") or []:
            key = (j["left_table"], j["left_column"], j["right_table"], j["right_column"])
            info = joins.setdefault(key, dict(j, count=0))
            info["count"] += 1
        d = distinct.setdefault(q.get("fingerprint") or q["sql"], {"fingerprint": q.get("fingerprint"),
                                                                  "query_type": q.get("query_type"), "count": 0,
                                                                  "sql": q.get("formatted") or q["sql"],
                                                                  "total_ms": 0.0, "errors": 0})
        d["count"] += 1
        d["total_ms"] += q.get("duration_ms") or 0
        d["errors"] += 1 if q.get("error") else 0
    return {
        "tables": [dict(v, access=sorted(v["access"])) for v in sorted(tables.values(), key=lambda v: v["table"])],
        "written_columns": {t: sorted(c) for t, c in sorted(written.items())},
        "joins": sorted(joins.values(), key=lambda j: -j["count"]),
        "distinct_statements": sorted(distinct.values(), key=lambda d: -d["count"]),
    }


def session_document(storage, session_id, include_noise=False):
    session = storage.get_session(session_id)
    if session is None:
        raise KeyError(session_id)
    # the summary covers the sub-sessions too; the sequence only the queries of this session (the
    # sub-sessions have their own section)
    every = storage.list_queries(session=session_id, limit=1_000_000, hide_noise=not include_noise, full=True)
    queries = [q for q in every if q["session_id"] == session_id]
    for q in queries:
        q["sql_with_values"] = interpolate(q["sql"], q.get("params"), DIALECTS.get(q.get("protocol"), "mysql"))
    tree = SessionTree(storage)
    session = dict(session, path_label=tree.label(session_id), depth=tree.depth(session_id))
    subs = [tree.by_id[c] for c in tree.children.get(session_id, [])]
    return {"session": session, "summary": summarize(every), "queries": queries,
            "sub_sessions": [{"id": s["id"], "label": s["label"], "queries": s["queries"]} for s in subs]}


class SessionTree:
    """The sessions of a history as a tree (sub-sessions at any depth)."""

    def __init__(self, storage):
        sessions = sorted(storage.list_sessions(), key=lambda s: s["id"])
        self.by_id = {s["id"]: s for s in sessions}
        self.children = {}
        self.roots = []
        for s in sessions:
            pid = s.get("parent_id")
            (self.children.setdefault(pid, []) if pid in self.by_id else self.roots).append(s["id"])

    def path(self, sid):
        out = []
        while sid in self.by_id:
            out.insert(0, self.by_id[sid])
            sid = self.by_id[sid].get("parent_id")
        return out

    def label(self, sid):
        return " › ".join(s["label"] for s in self.path(sid))

    def depth(self, sid):
        return len(self.path(sid)) - 1

    def walk(self, sid):
        """sid and all its sub-sessions, depth first."""
        yield sid
        for c in self.children.get(sid, []):
            yield from self.walk(c)


def ordered_sessions(storage, session_ids=None):
    """Session ids in chronological order, each session followed by its sub-sessions (depth first).
    The sub-sessions of a requested session are included."""
    tree = SessionTree(storage)
    wanted = None if session_ids is None else {int(i) for i in session_ids}
    out, seen = [], set()

    def visit(sid, included):
        included = included or wanted is None or sid in wanted
        if included and sid not in seen:
            out.append(sid)
            seen.add(sid)
        for c in tree.children.get(sid, []):
            visit(c, included)
    for r in tree.roots:
        visit(r, False)
    return out


def export_json(storage, session_ids, include_noise=False):
    docs = [session_document(storage, i, include_noise) for i in session_ids]
    return json.dumps({"generator": "sql-profiler", "exported_at": _iso(time.time()), "sessions": docs},
                      ensure_ascii=False, indent=2, default=str)


def export_markdown(storage, session_ids, include_noise=False):
    out = ["# SQL Profiler — recorded actions", "", f"Exported {_iso(time.time())}.", ""]
    docs = [session_document(storage, i, include_noise) for i in session_ids]
    if len(docs) > 1:
        out += ["| # | Action | Started | Queries | Tables written |", "|---|---|---|---|---|"]
        for d in docs:
            s, writes = d["session"], [t["table"] for t in d["summary"]["tables"] if set(t["access"]) - {"read"}]
            label = "↳ " * s.get("depth", 0) + s["label"]
            out.append(f"| {s['id']} | {_cell(label)} | {_iso(s['started_at'])} | {len(d['queries'])} "
                       f"| {_cell(', '.join(writes)) or '—'} |")
        out.append("")
    for d in docs:
        out += _session_markdown(d)
    return "\n".join(out).rstrip() + "\n"


def _session_markdown(doc):
    s, summary, queries = doc["session"], doc["summary"], doc["queries"]
    ended = _iso(s["ended_at"]) if s.get("ended_at") else "still recording"
    title = s.get("path_label") or s["label"]
    out = [f"## {title}", "", f"- {'Sub-action' if s.get('parent_id') else 'Session'} #{s['id']}, "
                              f"{_iso(s['started_at'])} → {ended}",
           f"- {len(queries)} statements, {sum(1 for q in queries if q.get('error'))} errors"]
    if doc.get("sub_sessions"):
        out.append("- Sub-actions (each in its own section; the tables below include them): " +
                   ", ".join(f"{_cell(c['label'])} ({c['queries']} statements)" for c in doc["sub_sessions"]))
    if s.get("notes"):
        out += ["", s["notes"]]
    out.append("")

    if summary["tables"]:
        out += ["### Tables involved", "", "| Table | Access | Statements | In schema? |", "|---|---|---|---|"]
        for t in summary["tables"]:
            out.append(f"| `{t['table']}` | {', '.join(t['access'])} | {t['statements']} | {'yes' if t['known'] else 'no'} |")
        out.append("")
    if summary["written_columns"]:
        out += ["### Columns written", ""]
        out += [f"- `{t}`: {', '.join(f'`{c}`' for c in cols)}" for t, cols in summary["written_columns"].items()]
        out.append("")
    if summary["joins"]:
        out += ["### Joins observed", "", "| Join | Times | Declared in schema |", "|---|---|---|"]
        for j in summary["joins"]:
            out.append(f"| `{j['left_table']}.{j['left_column']}` = `{j['right_table']}.{j['right_column']}` "
                       f"| {j['count']} | {'yes' if j['in_schema'] else '**no — candidate relationship**'} |")
        out.append("")
    if any(d["count"] > 1 for d in summary["distinct_statements"]):
        out += ["### Distinct statements", "", "| Type | Times | Total ms | Statement |", "|---|---|---|---|"]
        for d in summary["distinct_statements"]:
            out.append(f"| {d['query_type']} | {d['count']} | {d['total_ms']:.1f} | `{_cell(_one_line(d['sql'], 120))}` |")
        out.append("")

    out += ["### Sequence", ""]
    t0 = queries[0]["ts"] if queries else 0
    for n, q in enumerate(queries, 1):
        facts = [f"+{q['ts'] - t0:.3f}s", q.get("query_type") or "", f"{q.get('duration_ms') or 0:.1f} ms"]
        if q.get("rows") is not None:
            facts.append(f"{q['rows']} rows")
        if q.get("affected") is not None:
            facts.append(f"{q['affected']} affected")
        facts.append(f"conn {q.get('conn_id')}")
        out.append(f"{n}. " + " · ".join(f for f in facts if f))
        out.append("")
        sql = interpolate(q.get("formatted") or q["sql"], q.get("params"), DIALECTS.get(q.get("protocol"), "mysql"))
        out +=["   ```sql", *("   " + line for line in sql.splitlines()), "   ```", ""]
        if q.get("params"):
            out += [f"   Parameters: `{json.dumps(q['params'], ensure_ascii=False, default=str)}`", ""]
        if q.get("error"):
            out += [f"   **Error:** {q['error']}", ""]
    return out


# ====================================================================== tables

def table_report(storage, schema, session=None):
    """Tables used by session and all its sub-sessions (default: the whole profile), with the columns used,
    and the schema tables never used:
    {"tables": [{"table", "access", "statements", "sessions", "last_ts", "in_schema",
                 "columns": [{"column", "usage", "statements", "in_schema"}]}],
     "unused": [names] (None without a schema), "schema_tables": n}"""
    used, seen = storage.table_usage(session), set()
    for t in used:
        info = schema.table(t["table"]) if schema else None
        t["in_schema"] = info is not None
        if info:
            seen.add(norm(info["name"]))
        for c in t["columns"]:
            c["in_schema"] = bool(info) and schema.has_column(info["name"], c["column"])
    unused = None
    if schema:
        unused = sorted((t["name"] for t in schema.tables.values() if norm(t["name"]) not in seen), key=str.lower)
    return {"tables": used, "unused": unused, "schema_tables": len(schema.tables) if schema else 0}


def er_diagram(storage, analyzer, session=None):
    """ER subgraph (as for one query) of every table used by session and its sub-sessions (default: the whole
    profile): their key columns, the columns used, the schema links among them and the joins observed."""
    tables = storage.table_usage(session)
    analysis = {
        "tables": [{"name": t["table"], "access": "/".join(t["access"])} for t in tables],
        "columns": [{"table": t["table"], "column": c["column"], "usage": c["usage"]} for t in tables for c in t["columns"]],
        "joins": [dict(j, in_schema=analyzer.schema.has_link(j["left_table"], j["left_column"],
                                                             j["right_table"], j["right_column"]))
                  for j in storage.joins_usage(session)],
    }
    return analyzer.er_subgraph(analysis)


def _scopes(storage, session=None):
    """(kind, session, path label, depth) for the per-session part of the table exports."""
    tree = SessionTree(storage)
    ids = ordered_sessions(storage, None if session is None else [int(session)])
    out = [("sub-session" if tree.by_id[i].get("parent_id") else "session", tree.by_id[i], tree.label(i), tree.depth(i))
           for i in ids]
    if session is None and storage.counts()["unassigned"]:
        out.append(("outside sessions", None, "Outside sessions", 0))
    return out


def _attributes(t, marks=True):
    """`name` (read, filter) … — the columns of a table used by the queries."""
    if not t["columns"]:
        return ""
    return ", ".join(f"`{c['column']}`" + (f" ({', '.join(c['usage'])})" if marks else "")
                     + ("" if c["in_schema"] or not t["in_schema"] else " ⚠") for c in t["columns"])


def tables_markdown(storage, schema, session=None, profile=None):
    report = table_report(storage, schema, session)
    if session is None:
        title = f"# Tables used — {profile}" if profile else "# Tables used"
    else:
        title = f"# Tables used — {SessionTree(storage).label(int(session))}"
    out = [title, "", f"Exported {_iso(time.time())}" + (f" from profile “{profile}”." if profile else "."), ""]
    used = report["tables"]
    if report["unused"] is not None:
        out.append(f"- {sum(t['in_schema'] for t in used)} of the {report['schema_tables']} tables of the schema used"
                   + (f", {len(report['unused'])} never used" if session is None else ""))
        missing = [t["table"] for t in used if not t["in_schema"]]
        if missing:
            out.append(f"- Used but not in the schema: {', '.join(f'`{m}`' for m in missing)}")
    else:
        out.append("- No schema loaded: “In schema?” and the unused tables are not available")
    out += ["- Attributes: the columns the queries use — *read* (selected), *filter* (WHERE / JOIN / HAVING), "
            "*write* (INSERT / UPDATE); ⚠ = column not in the schema", ""]

    out += ["## Used tables", ""] + _tables_md(used, with_sessions=session is None)
    if session is None and report["unused"] is not None:
        out += ["## Unused tables", ""]
        out += [f"- `{name}`" for name in report["unused"]] or ["Every table of the schema was used."]
        out.append("")
    out += ["## By session", ""]
    for kind, s, label, depth in _scopes(storage, session):
        tables = table_report(storage, schema, s["id"] if s else "none")["tables"]
        heading = "#" * min(3 + depth, 6)
        out += [f"{heading} {label}", ""]
        if s:
            out += [f"{kind.capitalize()} #{s['id']}, {_iso(s['started_at'])}" +
                    (" — includes its sub-actions" if any(x.get("parent_id") == s["id"] for x in storage.list_sessions()) else ""), ""]
        out += _tables_md(tables)
    return "\n".join(out).rstrip() + "\n"


def _tables_md(tables, with_sessions=False):
    if not tables:
        return ["No tables.", ""]
    head = "| Table | Access | Statements |" + (" Sessions |" if with_sessions else "") + " In schema? | Attributes used |"
    out = [head, "|" + "---|" * head.count(" |")]
    for t in tables:
        out.append(f"| `{t['table']}` | {', '.join(t['access'])} | {t['statements']} |"
                   + (f" {t['sessions']} |" if with_sessions else "")
                   + f" {'yes' if t['in_schema'] else '**no**'} | {_attributes(t)} |")
    return out + [""]


def tables_csv(storage, schema, session=None, profile=None):
    """One row per (scope, table, attribute): the whole profile, each session and sub-session (at any depth,
    with its sub-sessions), the queries outside sessions and the unused tables. Opens in Excel."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["profile", "scope", "session_id", "session_path", "depth", "table", "table_access", "table_statements",
                "table_in_schema", "attribute", "attribute_usage", "attribute_statements", "attribute_in_schema"])
    yes = lambda b: "yes" if b else "no"

    def rows(scope, s, label, depth, tables):
        for t in tables:
            base = [profile or "", scope, s["id"] if s else "", label, depth, t["table"], "/".join(t["access"]),
                    t["statements"], yes(t["in_schema"])]
            if not t["columns"]:
                w.writerow(base + ["", "", "", ""])
            for c in t["columns"]:
                w.writerow(base + [c["column"], "/".join(c["usage"]), c["statements"], yes(c["in_schema"])])
    if session is None:
        report = table_report(storage, schema)
        rows("profile", None, "", "", report["tables"])
        for name in report["unused"] or []:
            w.writerow([profile or "", "unused", "", "", "", name, "", 0, "yes", "", "", "", ""])
    for kind, s, label, depth in _scopes(storage, session):
        rows(kind, s, label if s else "", depth if s else "", table_report(storage, schema, s["id"] if s else "none")["tables"])
    return buf.getvalue()


def er_html(storage, analyzer, schema, session=None, profile=None):
    """A standalone page (works offline) with the ER diagram of the tables used, their links, and the list of
    tables and attributes."""
    from pathlib import Path
    web = Path(__file__).resolve().parent / "web"
    er = er_diagram(storage, analyzer, session)
    report = table_report(storage, schema, session)
    title = SessionTree(storage).label(int(session)) if session else (profile or "Profile")
    rows = "".join(
        f"<tr><td><b>{_h(t['table'])}</b>{'' if t['in_schema'] else ' <i>(not in the schema)</i>'}</td>"
        f"<td>{_h(', '.join(t['access']))}</td><td class='num'>{t['statements']}</td>"
        f"<td>{'yes' if t['in_schema'] else '<b>no</b>'}</td>"
        f"<td>{', '.join(_h(c['column']) + ' <small>(' + _h(', '.join(c['usage'])) + ')</small>' for c in t['columns'])}</td></tr>"
        for t in report["tables"])
    unused = ""
    if session is None and report["unused"]:
        unused = f"<h2>Unused tables ({len(report['unused'])})</h2><p class='mono'>{_h(', '.join(report['unused']))}</p>"
    data = json.dumps(er, ensure_ascii=False, default=str).replace("</", "<\\/")
    return _ER_PAGE.format(
        title=_h(title), exported=_iso(time.time()), rows=rows, unused=unused,
        profile_note=f" from profile “{_h(profile)}”" if profile and session else "",
        tables=len(report["tables"]), links=len(er["links"]), observed=len(er["observed"]),
        css=_ER_CSS, script=(web / "er.js").read_text(encoding="utf-8"), data=data)


def _h(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


_ER_CSS = """
:root { --bg:#f5f6f8; --panel:#fff; --text:#1d2330; --muted:#5f6b7a; --border:#dde1e7; --primary:#1f4e79;
  --primary-soft:#e7eef6; --danger:#b42318; --danger-soft:#fdecea; --ok:#2e7d32; --warn:#c25e00; --code-bg:#f7f8fa;
  --t-other:#6b7480; --mono: ui-monospace, Menlo, Consolas, monospace;
  font: 13px -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; color: var(--text); background: var(--bg); }
body { margin: 0; padding: 20px 24px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 22px 0 8px; }
.muted { color: var(--muted); } .mono { font-family: var(--mono); }
.er-box { position: relative; height: 70vh; min-height: 420px; background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
.er-box svg { width: 100%; height: 100%; cursor: grab; display: block; } .er-box svg.drag { cursor: grabbing; }
.tools { position: absolute; right: 8px; top: 8px; display: flex; gap: 6px; }
button { font: inherit; background: var(--panel); border: 1px solid var(--border); border-radius: 6px; padding: 4px 10px; cursor: pointer; }
.legend { display: flex; flex-wrap: wrap; gap: 6px 18px; color: var(--muted); font-size: 12px; margin: 8px 0; }
.legend i { display: inline-block; width: 24px; border-top: 2px solid; vertical-align: middle; margin-right: 5px; }
table.list { border-collapse: collapse; width: 100%; background: var(--panel); border: 1px solid var(--border); }
table.list th, table.list td { border-bottom: 1px solid var(--border); padding: 5px 8px; text-align: left; vertical-align: top; }
table.list th { color: var(--muted); } td.num { text-align: right; }
.er-table rect.body { fill: var(--panel); stroke: var(--border); } .er-table rect.head { fill: var(--primary); }
.er-table.unknown rect.head { fill: var(--t-other); }
.er-table text { font-family: var(--mono); font-size: 11.5px; fill: var(--text); }
.er-table text.title { fill: #fff; font-weight: 700; font-family: inherit; font-size: 12px; }
.er-table text.acc { fill: #fff; font-size: 10px; font-weight: 700; } .er-table text.type { fill: var(--muted); font-size: 10.5px; }
.er-table text.key { font-size: 9.5px; font-weight: 700; fill: var(--warn); }
.er-table text.more { fill: var(--muted); font-style: italic; font-family: inherit; }
.er-table rect.used { fill: var(--primary-soft); } .er-table rect.used.write { fill: var(--danger-soft); }
.er-table text.used { font-weight: 700; }
.er-link { fill: none; stroke-width: 1.6; } .er-link.declared { stroke: var(--primary); }
.er-link.inferred { stroke: var(--ok); stroke-dasharray: 5 3; } .er-link.observed { stroke: var(--warn); stroke-dasharray: 2 3; stroke-width: 2; }
@media print { .tools { display: none; } .er-box { height: auto; min-height: 0; border: 0; } }
"""

_ER_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tables and links — {title}</title><style>{css}</style></head>
<body>
<h1>Tables and links — {title}</h1>
<p class="muted">Exported by SQL Profiler on {exported}{profile_note}. {tables} tables used, {links} links from the
schema, {observed} joins observed that are not in the schema. Wheel = zoom, drag = move.</p>
<div class="er-box"><div id="er" style="height:100%"></div><div class="tools"><button id="fit">Fit</button>
<button onclick="window.print()">Print / PDF</button></div></div>
<div class="legend"><span><i style="border-color:var(--primary)"></i>declared foreign key</span>
<span><i style="border-color:var(--ok);border-top-style:dashed"></i>inferred foreign key (sql-fk-finder)</span>
<span><i style="border-color:var(--warn);border-top-style:dotted"></i>join observed, not in the schema: candidate relationship</span>
<span>R / W / D = read / write / delete · highlighted rows = attributes used (red = written) · grey header = table not in the schema</span></div>
<h2>Tables and attributes used</h2>
<table class="list"><tr><th>Table</th><th>Access</th><th>Statements</th><th>In schema?</th><th>Attributes used</th></tr>{rows}</table>
{unused}
<script>{script}</script>
<script>
const view = MiniER.render(document.getElementById("er"), {data});
document.getElementById("fit").onclick = () => view && view.reset();
</script>
</body></html>
"""


def _iso(ts):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else ""


def _one_line(sql, limit):
    text = " ".join(sql.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _cell(text):
    return str(text).replace("|", "\\|").replace("`", "'")
