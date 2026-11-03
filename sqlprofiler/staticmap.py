"""Static analysis: which tables and attributes an application uses, from files alone.

Input: a folder of .sql files (the dump of the database) and a .csv log with the columns event_time, command_type and
query. Output: the tables divided by operation (SELECT, INSERT, UPDATE, DELETE), each with its columns and the ones the
log uses, plus the data of the diagram of their links. Nothing is captured or stored, and no database is contacted.
"""
import csv
import re
from pathlib import Path

from .analyzer import QueryAnalyzer, _fallback_fingerprint, first_keyword
from .fkinfer import add_inferred_links
from .schema import from_ddl_files, norm, open_text

OPERATIONS = ("select", "insert", "update", "delete")
_WRITE_OP = {"INSERT": "insert", "REPLACE": "insert", "UPDATE": "update", "MERGE": "update", "DELETE": "delete",
             "TRUNCATE": "delete"}
_ANALYSED_TYPES = {"SELECT", *_WRITE_OP}
# values of the type column that do not carry a statement to analyse. Prepare is followed by Execute, with the values.
_NOT_STATEMENTS = {"connect", "quit", "init db", "prepare", "close stmt", "reset stmt", "statistics", "ping", "field list",
                   "change user", "binlog dump", "set option", "sleep", "refresh", "shutdown", "processlist", "kill",
                   "debug", "long data"}
_SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
# mysql.general_log calls them event_time, command_type and argument; other exports use other names
_COLUMNS = {"time": ("event_time", "time", "timestamp"), "type": ("command_type", "event_type", "command"),
            "query": ("query", "argument", "sql_text", "sql")}
_MAX_FIELD = 2 ** 31 - 1                                 # sys.maxsize does not fit a C long on Windows
_MAX_PARSED_LENGTH = 200_000                             # longer statements are nearly always bulk INSERTs
_MAX_EXAMPLES = 20                                       # different statements kept for each table
_MAX_EXAMPLE_LENGTH = 2000                               # characters kept of each of them

_STRINGS_AND_COMMENTS = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|/\*.*?\*/|--[^\n]*|#[^\n]*", re.S)
_NAME = r"((?:`[^`]+`|[\w$]+)(?:\s*\.\s*(?:`[^`]+`|[\w$]+))?)"
_READ_NAME = re.compile(r"\b(?:FROM|JOIN)\s+" + _NAME, re.I)
_WRITE_NAME = re.compile(r"^\s*(?:(?:INSERT|REPLACE)\b.*?\bINTO|UPDATE(?:\s+LOW_PRIORITY|\s+IGNORE)*|TRUNCATE(?:\s+TABLE)?)\s+" + _NAME, re.I | re.S)
_CALL = re.compile(r"^\s*CALL\s+" + _NAME, re.I)
_MODIFIERS = re.compile(r"\b(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY)\b", re.I)
_INSERT_HEAD = re.compile(r"^\s*(?:INSERT|REPLACE)\s+(?:(?:IGNORE)\s+)?(?:INTO\s+)?" + _NAME + r"\s*(?:\(([^()]*)\))?", re.I)
_INSERT_SET = re.compile(r"^\s*SET\b", re.I)
_UPDATE_HEAD = re.compile(r"^\s*UPDATE\s+(?:IGNORE\s+)?" + _NAME + r"\s+SET\s+(.*?)(?:\bWHERE\b|\bORDER\s+BY\b|\bLIMIT\b|$)", re.I | re.S)
_ASSIGNED = re.compile(r"(?:^|,)\s*((?:`[^`]+`|[\w$]+)(?:\.(?:`[^`]+`|[\w$]+))?)\s*=")
_CHARSET_PREFIX = re.compile(r"\b_[a-z0-9]+\s*(?='')|\bb(?='')|\bx(?='')", re.I)


# ====================================================================== the CSV log

def _find_columns(names):
    """Position of each kind of column in the first row; None when it is not there."""
    names = [n.strip().strip('"').lower() for n in names]
    return {role: next((names.index(n) for n in choices if n in names), None) for role, choices in _COLUMNS.items()}, names


def read_log(path):
    """(rows of (time, type, query), counters). Only the query column is indispensable: without a type every row is
    analysed, without a time the period is not shown. Excel's ; and tabs are accepted as separators too."""
    csv.field_size_limit(_MAX_FIELD)
    with open_text(path) as f:
        header_line = f.readline()
    delimiter = max(",;\t|", key=header_line.count)
    info = {"rows": 0, "first": None, "last": None, "ignored": {}, "badly_quoted": 0}
    at, found = _find_columns(next(csv.reader([header_line], delimiter=delimiter), []))
    if at["query"] is None:
        raise ValueError(f"{Path(path).name}: the first row must name the column with the statements "
                         f"({' or '.join(_COLUMNS['query'])}). Found: {', '.join(found) or 'nothing'}.")
    known = [i for i in at.values() if i is not None]

    def field(row, role):
        return row[at[role]].strip() if at[role] is not None else ""

    def rows():
        with open_text(path) as f:
            reader = csv.reader(f, delimiter=delimiter)
            next(reader, None)
            for row in reader:
                if not row:
                    continue
                info["rows"] += 1
                if len(row) <= max(known):
                    info["badly_quoted"] += 1
                    continue
                # a query with unquoted separators, in the last column, is put back together
                query = delimiter.join(row[at["query"]:]) if at["query"] == len(found) - 1 else row[at["query"]]
                when = field(row, "time")
                if when:
                    info["first"] = min(info["first"] or when, when)
                    info["last"] = max(info["last"] or when, when)
                yield when, field(row, "type"), query
    return rows(), info


# ====================================================================== one statement -> what it uses, by operation

def _bare(name):
    """`shop` . `orders` -> (shop, orders); orders -> ("", orders)"""
    parts = [p.strip().strip("`") for p in name.split(".")]
    return (parts[0], parts[-1]) if len(parts) > 1 else ("", parts[0])


def _is_system(schema_name, table):
    return norm(schema_name) in _SYSTEM_SCHEMAS or norm(table) == "dual"


def _masked(sql):
    """The statement without its values and comments: what is left of a broken value (a log that cuts long statements
    leaves a string open) is removed too. Values are what the parser fails on, and they do not matter here."""
    text = _STRINGS_AND_COMMENTS.sub("''", sql)
    text = _MODIFIERS.sub("", _CHARSET_PREFIX.sub("", text))
    open_string = re.search(r"'(?!')", re.sub(r"''", "", text))
    if open_string:                                      # cut at the quote that has no partner
        kept = 0
        for m in re.finditer(r"''|'", text):
            if m.group() == "'":
                kept = m.start()
                break
        text = text[:kept]
    return text


def _rebuilt_head(text, keyword):
    """(a statement that can be parsed, complete?) from the beginning of an INSERT or UPDATE, or (None, False)."""
    if keyword in ("INSERT", "REPLACE"):
        m = _INSERT_HEAD.match(text)
        if not m:
            return None, False
        rest = text[m.end():]
        if not m.group(2) and _INSERT_SET.match(rest):
            columns = ", ".join(f"{c} = 1" for c in _ASSIGNED.findall(rest[rest.upper().index("SET") + 3:]))
            return f"INSERT INTO {m.group(1)} SET {columns}", True
        columns = f" ({m.group(2)})" if m.group(2) else ""
        return f"INSERT INTO {m.group(1)}{columns} VALUES (1)", not re.search(r"\bSELECT\b", rest, re.I)
    if keyword == "UPDATE":
        m = _UPDATE_HEAD.match(text)
        if m and _ASSIGNED.search(m.group(2)):
            columns = ", ".join(f"{c} = 1" for c in _ASSIGNED.findall(m.group(2)))
            return f"UPDATE {m.group(1)} SET {columns}", False       # what follows SET may be lost: filters too
    return None, False


def _empty_use():
    return {"tables": {}, "joins": [], "partial": False, "kind": None, "unresolved": {}}


def _touch(use, op, table):
    return use["tables"].setdefault((op, table), {"columns": {}, "all": set()})


def _from_analysis(result):
    """{(operation, table): {"columns": {name: usages}, "all": usages of the whole table}} for one parsed statement."""
    use = _empty_use()
    kind = result["query_type"]
    use["kind"] = kind
    write_op = _WRITE_OP.get(kind)
    ops_of, excluded = {}, set()
    for t in result["tables"]:
        if _is_system(t.get("schema", ""), t["name"]):
            excluded.add(norm(t["name"]))
            continue
        ops = ops_of.setdefault(t["name"], set())
        for access in t["access"].split("/"):
            op = "select" if access == "read" else "delete" if access == "delete" else write_op
            if op:
                ops.add(op)
                _touch(use, op, t["name"])

    def group_for(table, usage):
        ops = ops_of.get(table, set())
        written = next((o for o in ops if o != "select"), None)
        if usage == "write":
            return written
        if written and (write_op in ("update", "delete") or "select" not in ops):
            return written
        return "select" if "select" in ops else None

    for c in result["columns"]:
        if not c["table"]:
            use["unresolved"].setdefault(c["column"], set()).update(ops_of)     # the tables of its statement
            continue
        if norm(c["table"]) in excluded:
            continue
        table = next((t for t in ops_of if norm(t) == norm(c["table"])), None)
        for usage in c["usage"]:
            op = group_for(table, usage) if table else None
            if op:
                _touch(use, op, table)["columns"].setdefault(c["column"], set()).add(usage)
    for a in result.get("all_columns", []):
        table = next((t for t in ops_of if norm(t) == norm(a["table"])), None)
        op = group_for(table, a["usage"]) if table else None
        if op:
            _touch(use, op, table)["all"].add(a["usage"])
    use["joins"] = [j for j in result["joins"]
                    if norm(j["left_table"]) not in excluded and norm(j["right_table"]) not in excluded]
    return use


def _from_regex(sql, keyword, catalog):
    """Tables of a statement that could not be parsed. FROM is also in EXTRACT(x FROM y): only real tables count."""
    use = _empty_use()
    use["kind"], use["partial"] = keyword, True
    text = _STRINGS_AND_COMMENTS.sub("''", sql)
    write_op = _WRITE_OP.get(keyword)
    m = _WRITE_NAME.match(text)
    found = [(m.group(1), write_op)] if m and write_op else []
    reads = _READ_NAME.findall(text)
    if keyword == "DELETE" and reads:
        found.append((reads.pop(0), "delete"))
    found += [(name, "select") for name in reads]
    for name, op in found:
        db, table = _bare(name)
        known = catalog.table(table)
        if known and not _is_system(db, table):
            _touch(use, op, known["name"])
    return use


# ====================================================================== values written by INSERT and UPDATE

_MAX_VALUE = 120                                         # characters kept of one value
_MAX_DISTINCT = 20                                       # different values kept for one attribute
_MAX_ROWS = 200                                          # rows read of one INSERT
_MAX_VALUE_EXECUTIONS = 200                              # executions read of statements that differ only by their values
_MAX_VARIANTS = 50                                       # different sets of values kept for one statement
_MAX_VARIANT_SQL = 1000                                  # characters kept of the text of one execution
_STRUCTURE = re.compile(r"[(),]")
_LEAD = r"^[\s\0]*"
_VALUE_INSERT = re.compile(_LEAD + r"(?:INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*(?:INTO\s+)?"
                           + _NAME + r"\s*(?:\(([^()]*)\))?\s*", re.I)
_VALUE_UPDATE = re.compile(_LEAD + r"UPDATE\s+(?:(?:LOW_PRIORITY|IGNORE)\s+)*" + _NAME + r"(?:\s+(?:AS\s+)?(?!SET\b)([\w$]+))?", re.I)
_SET_WORD = re.compile(r"\bSET\b", re.I)
_VALUE_DELETE = re.compile(_LEAD + r"DELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*(?P<targets>[^\0]*?)\bFROM\s+" + _NAME
                           + r"(?:\s+(?:AS\s+)?(?!(?:WHERE|USING|JOIN|INNER|LEFT|RIGHT|CROSS|STRAIGHT_JOIN|ORDER|LIMIT|PARTITION)\b)([\w$]+))?", re.I)
_VALUE_TRUNCATE = re.compile(_LEAD + r"TRUNCATE\s+(?:TABLE\s+)?" + _NAME, re.I)
_WHERE_WORD = re.compile(r"\bWHERE\b", re.I)
_WHERE_END = re.compile(r"\b(?:ORDER\s+BY|LIMIT|GROUP\s+BY|FOR\s+UPDATE|LOCK\s+IN)\b", re.I)
_CONDITION_SPLIT = re.compile(r"[()]|\b(?:AND|OR|XOR)\b|&&|\|\|", re.I)
_BETWEEN = re.compile(r"\bBETWEEN\b", re.I)
_CONDITION = re.compile(r"[\s\0(]*((?:`[^`]+`|[\w$]+)(?:\s*\.\s*(?:`[^`]+`|[\w$]+))?)\s*"
                        r"(<=>|<>|!=|<=|>=|=|<|>|NOT\s+IN\b|IN\b|NOT\s+LIKE\b|LIKE\b|IS\s+NOT\b|IS\b|NOT\s+BETWEEN\b|BETWEEN\b"
                        r"|NOT\s+REGEXP\b|REGEXP\b|RLIKE\b)", re.I)
_UPDATE_END = re.compile(r"\b(?:WHERE|ORDER\s+BY|LIMIT)\b", re.I)


def _same_length_mask(sql):
    """(the statement with strings and comments blanked, position where a string stays open or None). Offsets in the
    result are valid in sql: the mask finds the structure, the values are then cut from the original text."""
    mask = _STRINGS_AND_COMMENTS.sub(lambda m: "\0" * len(m.group()), sql)
    cut = mask.find("'")
    if cut >= 0:
        mask = mask[:cut] + "\0" * (len(mask) - cut)
    return mask, (cut if cut >= 0 else None)


def _value(text, cut_off=False):
    text = " ".join(text.split())
    if len(text) > _MAX_VALUE:
        return text[:_MAX_VALUE] + "…"
    return text + "…" if cut_off else text


def _rows_of_values(sql, mask, start, open_at):
    """([one list of values per row], the last row was cut by the log?)"""
    rows, depth, begin, row, end_of_row = [], 0, 0, [], start
    for m in _STRUCTURE.finditer(mask, start):
        ch = m.group()
        if depth == 0 and mask[end_of_row:m.start()].strip(" ,\t\r\n\0"):
            return rows, False                           # ON DUPLICATE KEY UPDATE and so on
        if ch == "(":
            depth += 1
            if depth == 1:
                begin, row = m.end(), []
        elif ch == "," and depth == 1:
            row.append(_value(sql[begin:m.start()]))
            begin = m.end()
        elif ch == ")":
            depth -= 1
            if depth == 0:
                last = sql[begin:m.start()]
                rows.append(row + [_value(last)] if last.strip() or row else [])
                end_of_row = m.end()
                if len(rows) >= _MAX_ROWS:
                    return rows, False
    if depth > 0:                                        # the log stopped in the middle of this row
        tail = sql[begin:]
        rows.append(row + ([_value(tail, cut_off=True)] if tail.strip() else []))
        return rows, True
    return rows, False


def _assignments(sql, mask, start, end, open_at):
    """[(column as written, value)] of  a = 1, b = NOW()  between start and end."""
    pieces, depth, begin = [], 0, start
    for m in _STRUCTURE.finditer(mask, start, end):
        ch = m.group()
        depth += 1 if ch == "(" else -1 if ch == ")" else 0
        if ch == "," and depth == 0:
            pieces.append((begin, m.start()))
            begin = m.end()
    pieces.append((begin, end))
    out = []
    for a, b in pieces:
        equal = mask.find("=", a, b)
        if equal >= 0:
            out.append((sql[a:equal].strip(), _value(sql[equal + 1:b], cut_off=open_at is not None and b >= open_at)))
    return out


def _depth0(mask, pattern, start, end=None):
    """The first match of pattern from start that is not inside parentheses (a subquery), or None."""
    end = len(mask) if end is None else end
    for m in pattern.finditer(mask, start, end):
        between = mask[start:m.start()]
        if between.count("(") == between.count(")"):
            return m
    return None


def _where_region(mask, start):
    """(begin, end) of the conditions of the WHERE that follows start, or None when there is no WHERE."""
    where = _depth0(mask, _WHERE_WORD, start)
    if not where:
        return None
    stop = _depth0(mask, _WHERE_END, where.end())
    return where.end(), stop.start() if stop else len(mask)


def _wrapped(mask, a, b):
    """(begin, end) inside the parentheses when the text between a and b is all in one pair of them, else None."""
    text = mask[a:b]
    left, right = len(text) - len(text.lstrip(" \t\r\n\0")), len(text.rstrip(" \t\r\n\0"))
    if right - left < 2 or text[left] != "(" or text[right - 1] != ")":
        return None
    depth = 0
    for i in range(left, right):
        depth += 1 if text[i] == "(" else -1 if text[i] == ")" else 0
        if depth == 0 and i < right - 1:
            return None                                  # (a) = (b): two pairs
    return a + left + 1, a + right - 1


def _conditions(sql, mask, region, open_at, names):
    """{column key: "= 5"} of the WHERE between region: one entry per comparison of a column of the table (owner in
    names, or no owner). Conditions on the same column are joined with " / "."""
    if not region:
        return {}
    start, end = region
    pieces, depth, begin, between_done = [], 0, start, False
    for m in _CONDITION_SPLIT.finditer(mask, start, end):
        word = m.group()
        if word == "(":
            depth += 1
        elif word == ")":
            depth -= 1
        elif depth == 0:
            if word.upper() == "AND" and not between_done and _BETWEEN.search(mask, begin, m.start()):
                between_done = True                      # the AND of  x BETWEEN 1 AND 5
                continue
            pieces.append((begin, m.start()))
            begin, between_done = m.end(), False
    pieces.append((begin, end))
    out = {}
    for a, b in pieces:
        inner = _wrapped(mask, a, b)
        if inner:                                        # (a = 1 OR b = 2): the conditions inside
            for key, text in _conditions(sql, mask, inner, open_at, names).items():
                out[key] = f"{out[key]} / {text}" if key in out and text not in out[key].split(" / ") else text
            continue
        m = _CONDITION.match(mask, a, b)
        if not m:
            continue
        owner = _bare(m.group(1))[0]
        if owner and norm(owner) not in names:
            continue
        operator = " ".join(m.group(2).upper().split())
        value = sql[m.end():b].strip()
        while value.endswith(")") and value.count(")") > value.count("("):
            value = value[:-1].rstrip()                  # the parenthesis closed around the whole condition
        text = f"{operator} {_value(value, cut_off=open_at is not None and b >= open_at)}".strip()
        key = _column_key(m.group(1))
        out[key] = f"{out[key]} / {text}" if key in out and text not in out[key].split(" / ") else text
    return out


def _column_key(written):
    return norm(_bare(written)[1])


def _values_of_insert(sql, mask, head, open_at, catalog):
    """[{"table", "values": {column key: value}}], one per row. Columns left out of the INSERT are NULL; a row cut by the
    log says nothing about the columns after the cut."""
    table = _bare(head.group(1))[1]
    known = catalog.table(table)
    all_columns = [norm(c["name"]) for c in known["columns"]] if known else []
    listed = [_column_key(c) for c in head.group(2).split(",") if c.strip()] if head.group(2) is not None else None
    rest = mask[head.end():]
    word = re.match(r"(VALUES?\b|SET\b)?", rest, re.I).group(1) or ""
    if word.upper() == "SET":
        given = {_column_key(c): v for c, v in _assignments(sql, mask, head.end() + 3, len(mask), open_at)}
        return [{"table": table, "values": {**{c: "NULL" for c in all_columns}, **given}}]
    if not word:                                         # INSERT ... SELECT: the values come from the query
        columns = listed if listed is not None else all_columns
        return [{"table": table, "values": {**{c: "NULL" for c in all_columns}, **{c: "(from SELECT)" for c in columns}}}]
    rows, cut = _rows_of_values(sql, mask, head.end() + len(word), open_at)
    columns = listed if listed is not None else all_columns
    out = []
    for i, row in enumerate(rows):
        values = dict(zip(columns, row))
        if not (cut and i == len(rows) - 1):
            values = {**{c: "NULL" for c in all_columns}, **values}
        elif listed is not None:                         # cut, but the list says which columns are left out
            values = {**{c: "NULL" for c in all_columns if c not in listed}, **values}
        out.append({"table": table, "values": values})
    return out


def _values_of_update(sql, mask, head, open_at):
    table, alias = _bare(head.group(1))[1], head.group(2)
    where_set = _SET_WORD.search(mask, head.end() - (len(head.group(2)) if head.group(2) else 0))
    if not where_set:
        return []
    names = {norm(table), norm(alias or table)}
    end = len(mask)
    for m in _UPDATE_END.finditer(mask, where_set.end()):
        between = mask[where_set.end():m.start()]
        if between.count("(") == between.count(")"):     # not the WHERE of a subquery
            end = m.start()
            break
    values = {}
    for written, value in _assignments(sql, mask, where_set.end(), end, open_at):
        owner = _bare(written)[0]
        if not owner or norm(owner) in names:            # UPDATE a JOIN b SET b.x = 1 is not a value of a
            values[_column_key(written)] = value
    where = _conditions(sql, mask, _where_region(mask, end), open_at, names)
    return [{"table": table, "values": values, "where": where}] if values else []


def _values_of_delete(sql, mask, head, open_at):
    """The conditions that choose the rows deleted. DELETE a FROM a JOIN b ...: only the conditions of a."""
    table, alias = _bare(head.group(2))[1], head.group(3)
    targets = [_bare(t)[1] for t in head.group("targets").replace(".*", "").split(",") if t.strip()]
    names = {norm(table), norm(alias or table)}
    if targets and not names & {norm(t) for t in targets}:
        return []                                        # the rows deleted are of another table of the JOIN
    region = _where_region(mask, head.end())
    return [{"table": table, "values": {}, "where": _conditions(sql, mask, region, open_at, names), "all_rows": not region}]


def written_values(sql, catalog):
    """{"insert": [{"table", "values"}...], "update": [{"table", "values", "where"}...], "delete": [{"table", "where",
    "all_rows"}...]}, read from the text: also when the parser cannot read it."""
    out = {"insert": [], "update": [], "delete": []}
    whole, _ = _same_length_mask(sql)
    begin = 0
    for stop in [m.start() for m in re.finditer(";", whole)] + [len(sql)]:
        part = sql[begin:stop]
        begin = stop + 1
        mask, open_at = _same_length_mask(part)
        head = _VALUE_INSERT.match(mask)
        if head:
            out["insert"] += _values_of_insert(part, mask, head, open_at, catalog)
            continue
        head = _VALUE_UPDATE.match(mask)
        if head:
            out["update"] += _values_of_update(part, mask, head, open_at)
            continue
        head = _VALUE_DELETE.match(mask)
        if head:
            out["delete"] += _values_of_delete(part, mask, head, open_at)
            continue
        head = _VALUE_TRUNCATE.match(mask)
        if head:
            out["delete"].append({"table": _bare(head.group(1))[1], "values": {}, "where": {}, "all_rows": True})
    return out


# ====================================================================== the whole log

class _Totals:
    def __init__(self, analyzer):
        self.analyzer = analyzer
        self.by_op = {op: {} for op in OPERATIONS}      # table -> {"queries", "columns": {name: {usage, count}}, ...}
        self.joins = {}
        self.calls, self.other_types = {}, {}
        self.cache = {}
        self.value_reads = {}
        self.statements = self.partial = 0
        self.values_capped = False
        self.partial_examples = []
        self.unresolved = {}                             # column -> tables of the statements that use it

    def _analyse(self, sql, keyword):
        if len(sql) <= _MAX_PARSED_LENGTH:
            result = self.analyzer.analyze(sql)
            if result["query_type"] == "MULTI":              # SET @a = 1; INSERT ...
                return self._analyse_many(sql)
            if result["noise"]:
                return None
            if result["query_type"] in _ANALYSED_TYPES and result["tables"] and not result["parse_error"]:
                use = _from_analysis(result)
                return use if use["tables"] else None        # only system tables
            error = result["parse_error"]
            if keyword not in _ANALYSED_TYPES:
                return {"other": result["query_type"]} if not error else None
            if not error and result["query_type"] in _ANALYSED_TYPES:
                return None                                  # SELECT 1 + 1: no tables
        else:
            error = "too long to parse"
            if keyword not in _ANALYSED_TYPES:
                return None
        return self._recover(sql, keyword, error)

    def _recover(self, sql, keyword, error):
        """The parser gave up on this statement. In order: without its values; its INSERT or UPDATE head alone (for an
        INSERT that is all it takes); only the table names. The last two say that attributes may be missing."""
        text = _masked(sql)
        if len(text) <= _MAX_PARSED_LENGTH:
            result = self.analyzer.analyze(text)
            if not result["parse_error"] and result["query_type"] in _ANALYSED_TYPES and result["tables"]:
                return _from_analysis(result)
        rebuilt, complete = _rebuilt_head(text, keyword)
        if rebuilt:
            result = self.analyzer.analyze(rebuilt)
            if not result["parse_error"] and result["tables"]:
                use = _from_analysis(result)
                use["partial"], use["error"] = not complete, error
                if use["tables"]:
                    return use
        use = _from_regex(sql, keyword, self.analyzer.schema)
        use["error"] = error
        return use if use["tables"] else None

    def _analyse_many(self, sql):
        """Several statements in one log row: each is analysed on its own, the result is merged."""
        import sqlglot
        merged = _empty_use()
        merged["kind"] = "MULTI"
        for tree in sqlglot.parse(sql, read=self.analyzer.dialect):
            if tree is None:
                continue
            one = self._analyse(tree.sql(self.analyzer.dialect), first_keyword(tree.sql(self.analyzer.dialect)))
            if not one or "other" in one:
                continue
            merged["partial"] = merged["partial"] or one["partial"]
            for column, tables in one["unresolved"].items():
                merged["unresolved"].setdefault(column, set()).update(tables)
            merged["joins"] += one["joins"]
            for key, value in one["tables"].items():
                into = merged["tables"].setdefault(key, {"columns": {}, "all": set()})
                into["all"] |= value["all"]
                for column, usages in value["columns"].items():
                    into["columns"].setdefault(column, set()).update(usages)
        return merged if merged["tables"] else None

    def add(self, sql):
        call = _CALL.match(sql)
        if call:
            name = _bare(call.group(1))[1]
            self.calls[norm(name)] = name
            return
        key = _fallback_fingerprint(sql[:_MAX_PARSED_LENGTH])
        if key not in self.cache:
            self.cache[key] = self._analyse(sql, first_keyword(sql))
            first_time = True
        else:
            first_time = False
        use = self.cache[key]
        if not use:
            return
        if "other" in use:
            self.other_types[use["other"]] = self.other_types.get(use["other"], 0) + 1
            return
        self.statements += 1
        if first_time:
            for column, tables in use["unresolved"].items():
                self.unresolved.setdefault(column, set()).update(tables)
            if use["partial"]:
                self.partial += 1
                if len(self.partial_examples) < 3:
                    self.partial_examples.append(" ".join(sql[:120].split()))
        for j in use["joins"]:
            self.joins.setdefault((norm(j["left_table"]), norm(j["left_column"]),
                                   norm(j["right_table"]), norm(j["right_column"])), j)
        example = " ".join(sql[:_MAX_EXAMPLE_LENGTH].split()) + ("…" if len(sql) > _MAX_EXAMPLE_LENGTH else "")
        for (op, table), value in use["tables"].items():
            info = self.by_op[op].setdefault(table, {"queries": 0, "partial": 0, "columns": {}, "all": {}, "examples": {},
                                                     "values": {}, "variants": {}, "all_rows": 0})
            info["queries"] += 1
            if use["partial"]:
                info["partial"] += 1
            if key in info["examples"]:
                info["examples"][key]["count"] += 1
            elif len(info["examples"]) < _MAX_EXAMPLES:
                info["examples"][key] = {"sql": example, "count": 1}
            for column, usages in value["columns"].items():
                c = info["columns"].setdefault(norm(column), {"name": column, "usage": set(), "queries": 0})
                c["usage"] |= usages
                c["queries"] += 1
            for usage in value["all"]:
                info["all"][usage] = info["all"].get(usage, 0) + 1
            if op != "select" and (key in info["variants"] or len(info["variants"]) < _MAX_EXAMPLES):
                info["variants"].setdefault(key, {"executions": 0, "rows": {}, "more": False})["executions"] += 1
        if any(op != "select" for op, _ in use["tables"]):
            self._add_values(sql, key)

    def _add_values(self, sql, key):
        seen = self.value_reads[key] = self.value_reads.get(key, 0) + 1
        if seen > _MAX_VALUE_EXECUTIONS:
            self.values_capped = True
            return
        catalog = self.analyzer.schema
        text = " ".join(sql[:_MAX_VARIANT_SQL].split()) + ("…" if len(sql) > _MAX_VARIANT_SQL else "")
        for op, rows in written_values(sql, catalog).items():
            by_key = {norm(t): i for t, i in self.by_op[op].items()}
            for row in rows:
                info = by_key.get(norm(catalog.canonical(row["table"])))
                if not info:
                    continue
                if row.get("all_rows"):
                    info["all_rows"] += 1
                statement = info["variants"].get(key)
                if statement is not None:                # one execution: its values stay together
                    where = row.get("where", {})
                    signature = (tuple(sorted(row["values"].items())), tuple(sorted(where.items())), bool(row.get("all_rows")))
                    if signature in statement["rows"]:
                        statement["rows"][signature]["count"] += 1
                    elif len(statement["rows"]) < _MAX_VARIANTS:
                        statement["rows"][signature] = {"values": row["values"], "where": where, "count": 1, "sql": text,
                                                        "all_rows": bool(row.get("all_rows"))}
                    else:
                        statement["more"] = True
                shown = row["values"] if op != "delete" else row.get("where", {})
                for column, value in shown.items():
                    v = info["values"].setdefault(column, {"last": value, "distinct": {}, "more": False})
                    v["last"] = value
                    if value in v["distinct"] or len(v["distinct"]) < _MAX_DISTINCT:
                        v["distinct"][value] = v["distinct"].get(value, 0) + 1
                    else:
                        v["more"] = True


def _dump_files(folder):
    p = Path(folder)
    if p.is_file():
        return [p]
    if not p.is_dir():
        raise ValueError(f"Dump folder not found: {p}")
    files = sorted(f for f in p.rglob("*") if f.is_file() and f.name.lower().endswith((".sql", ".sql.gz")))
    if not files:
        raise ValueError(f"No .sql files in the folder {p}")
    return files


def _table_result(name, info, catalog):
    """A table in one operation: all its columns, those used by the log marked; used ones missing from the dump last."""
    known = catalog.table(name)
    all_usage = sorted(info["all"])
    used = dict(info["columns"])
    columns = []

    def with_values(column, key):
        v = info["values"].get(key)
        if v:
            column.update(value=v["last"], values=sorted(v["distinct"].items(), key=lambda x: -x[1]), more_values=v["more"])
        return column

    for c in (known["columns"] if known else []):
        u = used.pop(norm(c["name"]), None)
        usage = sorted(set(all_usage) | (u["usage"] if u else set()))
        columns.append(with_values({"name": c["name"], "type": c.get("type", ""), "pk": bool(c.get("pk")), "fk": bool(c.get("fk")),
                                    "usage": usage, "whole_table": bool(all_usage), "queries": u["queries"] if u else 0,
                                    "in_dump": True}, norm(c["name"])))
    for key, u in used.items():
        columns.append(with_values({"name": u["name"], "type": "", "pk": False, "fk": False, "usage": sorted(u["usage"]),
                                    "whole_table": False, "queries": u["queries"], "in_dump": False}, key))
    examples = sorted(info["examples"].values(), key=lambda e: -e["count"])
    variants = [{"executions": v["executions"], "more": v["more"], "rows": list(v["rows"].values())}
                for v in sorted(info.get("variants", {}).values(), key=lambda v: -v["executions"]) if v["rows"]]
    return {"table": name, "in_dump": bool(known), "queries": info["queries"], "partial": info["partial"], "columns": columns,
            "all_columns": bool(all_usage), "used": sum(1 for c in columns if c["usage"]), "examples": examples,
            "variants": variants, "all_rows": info.get("all_rows", 0)}


def analyse(dump_folder, log_file, dialect="mysql"):
    """Everything the Static analysis section shows. ValueError, with a message for the user, when it cannot be done."""
    dump_folder, log_file = str(dump_folder or "").strip(), str(log_file or "").strip()
    if not dump_folder or not log_file:
        raise ValueError("Choose both the dump folder and the .csv log.")
    files = _dump_files(dump_folder)
    log_path = Path(log_file)
    if not log_path.is_file():
        raise ValueError(f"Log not found: {log_path}")
    catalog = from_ddl_files(files, dialect, dump_folder)
    if not catalog:
        raise ValueError(f"No CREATE TABLE found in the {len(files)} .sql files of {dump_folder}: "
                         "a dump with the structure of the tables is needed.")
    rows, info = read_log(log_path)
    totals = _Totals(QueryAnalyzer(dialect, catalog))
    problems = list(catalog.problems)
    try:
        for _, command, query in rows:
            if command.lower() in _NOT_STATEMENTS:
                info["ignored"][command] = info["ignored"].get(command, 0) + 1
            elif query.strip():
                totals.add(query)
    except csv.Error as e:
        problems.append(f"The log could not be read to the end (after row {info['rows']}): {e}.")

    used_names = {t for tables in totals.by_op.values() for t in tables}
    if not used_names:
        raise ValueError(f"{log_path.name}: no statements that use tables in {info['rows']} rows.")
    add_inferred_links(catalog, used_names)                  # marks the FK columns too: before reading the columns

    groups = {op: sorted((_table_result(t, i, catalog) for t, i in totals.by_op[op].items()),
                         key=lambda r: (-r["queries"], r["table"].lower()))
              for op in OPERATIONS}
    er = _diagram(totals, catalog)

    if info["badly_quoted"]:
        problems.append(f"{info['badly_quoted']} rows of the log have too few columns and were skipped.")
    if totals.calls:
        problems.append("The log calls stored procedures, whose tables cannot be seen from the log: "
                        + ", ".join(sorted(totals.calls.values())) + ".")
    if totals.partial:
        problems.append(f"Different statements not fully read: {totals.partial}. Their tables are listed, but "
                        "attributes may be missing (those tables say so). For example: "
                        + " | ".join(totals.partial_examples))
    if totals.unresolved:
        shown = sorted(totals.unresolved, key=str.lower)

        def where(column):
            tables = sorted(totals.unresolved[column], key=str.lower)
            return (f"{column} (in queries on {', '.join(tables[:4])}{'…' if len(tables) > 4 else ''})" if tables
                    else f"{column} (in queries without tables)")
        problems.append(f"{len(shown)} attributes could not be assigned to a table (no table name in the query, and not "
                        f"found in the dump): {', '.join(where(c) for c in shown[:12])}{'…' if len(shown) > 12 else ''}.")
    if totals.values_capped:
        problems.append(f"Values: only the first {_MAX_VALUE_EXECUTIONS} executions of each statement are read; "
                        "tables and attributes count them all.")
    wipes = [f"{t['table']} ({t['all_rows']}×)" for t in groups["delete"] if t["all_rows"]]
    if wipes:
        problems.append("DELETE without WHERE or TRUNCATE (every row of the table is deleted): " + ", ".join(wipes) + ".")
    missing = sorted(n for n in used_names if not catalog.table(n))
    if missing:
        problems.append(f"Tables used in the log but not in the dump: {', '.join(missing)}.")
    seen = {norm(n) for n in used_names}
    unused = sorted((t["name"] for t in catalog.tables.values() if norm(t["name"]) not in seen), key=str.lower)
    return {"groups": groups, "er": er, "problems": problems, "unused": unused, "log": log_path.name, "summary": {
        "rows": info["rows"], "statements": totals.statements, "first": info["first"], "last": info["last"],
        "ignored": info["ignored"], "other_types": totals.other_types, "tables": len(used_names),
        "dump_tables": len(catalog.tables), "dump_files": len(files),
        "counts": {op: len(groups[op]) for op in OPERATIONS}}}


def _diagram(totals, catalog):
    access, columns = {}, {}
    for op, tables in totals.by_op.items():
        word = {"select": "read", "delete": "delete"}.get(op, "write")
        for table, info in tables.items():
            access.setdefault(table, set()).add(word)
            for c in info["columns"].values():
                entry = columns.setdefault((norm(table), norm(c["name"])), {"table": table, "column": c["name"], "usage": []})
                entry["usage"] += [u for u in sorted(c["usage"]) if u not in entry["usage"]]
    analysis = {
        "tables": [{"name": t, "access": "/".join(sorted(a))} for t, a in access.items()],
        "columns": list(columns.values()),
        # checked now: the links deduced after the statements were analysed count too
        "joins": [dict(j, in_schema=catalog.has_link(j["left_table"], j["left_column"], j["right_table"], j["right_column"]))
                  for j in totals.joins.values()],
    }
    return totals.analyzer.er_subgraph(analysis)
