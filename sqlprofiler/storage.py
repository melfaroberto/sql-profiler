"""Local SQLite database with every captured query and the recording sessions."""
import json
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    notes       TEXT NOT NULL DEFAULT '',
    started_at  REAL NOT NULL,
    ended_at    REAL,
    parent_id   INTEGER REFERENCES sessions(id) ON DELETE CASCADE      -- set for a sub-session (any depth)
);
CREATE TABLE IF NOT EXISTS queries (
    id           INTEGER PRIMARY KEY,
    ts           REAL NOT NULL,
    session_id   INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
    conn_id      INTEGER,
    proxy        TEXT,
    protocol     TEXT,
    client       TEXT,
    db_user      TEXT,
    database     TEXT,
    kind         TEXT,
    sql          TEXT NOT NULL,
    params       TEXT,
    sql_full     TEXT,
    truncated    INTEGER NOT NULL DEFAULT 0,
    duration_ms  REAL,
    rows         INTEGER,
    affected     INTEGER,
    error        TEXT,
    query_type   TEXT,
    tables       TEXT,
    columns      TEXT,
    joins        TEXT,
    fingerprint  TEXT,
    formatted    TEXT,
    parse_error  TEXT,
    noise        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS queries_session ON queries(session_id, id);
CREATE INDEX IF NOT EXISTS queries_fingerprint ON queries(fingerprint);
"""

JSON_FIELDS = ("params", "tables", "columns", "joins")
LIST_FIELDS = ("id", "ts", "session_id", "conn_id", "proxy", "protocol", "client", "db_user", "database", "kind",
               "substr(sql, 1, 400) AS sql", "truncated", "duration_ms", "rows", "affected", "error", "query_type",
               "tables", "fingerprint", "noise")


class Storage:
    """Thread-safe: queries are written by the analysis worker thread and read by the web server."""

    def __init__(self, path):
        self.path = str(path)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.executescript(SCHEMA)
            if "parent_id" not in [r[1] for r in self._db.execute("PRAGMA table_info(sessions)")]:
                # history created before sub-sessions existed
                self._db.execute("ALTER TABLE sessions ADD COLUMN parent_id INTEGER "
                                 "REFERENCES sessions(id) ON DELETE CASCADE")
            # a session left open by a crash is closed at its last query
            self._db.execute("""UPDATE sessions SET ended_at = COALESCE(
                                    (SELECT MAX(ts) FROM queries WHERE session_id = sessions.id), started_at)
                                WHERE ended_at IS NULL""")
            self._db.commit()

    def close(self):
        with self._lock:
            self._db.close()

    def _rows(self, sql, args=()):
        with self._lock:
            return [_decode(dict(r)) for r in self._db.execute(sql, args).fetchall()]

    def _write(self, sql, args=()):
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    # ------------------------------------------------------------------ queries

    def insert_query(self, q):
        data = {k: (json.dumps(v, ensure_ascii=False) if k in JSON_FIELDS and v is not None else v) for k, v in q.items()}
        cols = [c for c in data if c != "id"]
        cur = self._write(f"INSERT INTO queries ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                          [data[c] for c in cols])
        return cur.lastrowid

    def get_query(self, query_id):
        rows = self._rows("SELECT * FROM queries WHERE id = ?", (query_id,))
        return rows[0] if rows else None

    def list_queries(self, session=None, after_id=None, before_id=None, limit=500, search=None, query_type=None,
                     table=None, hide_noise=False, full=False, fingerprint=None):
        """session: None = all, "none" = queries outside any session, or a session id (with all its sub-sessions).
        Returns the most recent `limit` matches in chronological order."""
        where, args = self._session_filter(session)
        if after_id:
            where.append("id > ?")
            args.append(int(after_id))
        if before_id:
            where.append("id < ?")
            args.append(int(before_id))
        if search:
            where.append("(sql LIKE ? OR error LIKE ?)")
            args += [f"%{search}%"] * 2
        if query_type:
            where.append("query_type = ?")
            args.append(query_type)
        if table:
            where.append("lower(tables) LIKE ?")
            args.append(f'%"name": "{table.lower()}"%')
        if fingerprint:
            where.append("fingerprint = ?")
            args.append(fingerprint)
        if hide_noise:
            where.append("noise = 0")
        fields = "*" if full else ", ".join(LIST_FIELDS)
        sql = (f"SELECT {fields} FROM queries {'WHERE ' + ' AND '.join(where) if where else ''} "
               f"ORDER BY id DESC LIMIT ?")
        rows = self._rows(sql, args + [int(limit)])
        rows.reverse()
        return rows

    @staticmethod
    def _session_filter(session):
        if session == "none":
            return ["session_id IS NULL"], []
        if session in (None, "", "all"):
            return [], []
        return [f"session_id IN ({_TREE})"], [int(session)]

    def table_usage(self, session=None):
        """Tables touched by the queries of session (same values as list_queries), merged by name ignoring case:
        [{"table", "access": [...], "statements", "sessions": number of sessions that use it, "last_ts",
          "columns": [{"column", "usage": ["read", "filter", "write"], "statements"}]}]"""
        where, args = self._session_filter(session)
        cols = f"""SELECT json_extract(c.value, '$.table') AS tbl, json_extract(c.value, '$.column') AS col,
                          json_extract(c.value, '$.usage') AS usage, COUNT(*) AS n
                   FROM queries q, json_each(q.columns) c
                   WHERE q.columns IS NOT NULL AND q.columns != '[]' {''.join(' AND ' + w for w in where)}
                   GROUP BY tbl, col, usage"""
        with self._lock:
            column_rows = self._db.execute(cols, args).fetchall()
        columns = {}
        for tbl, col, usage, n in column_rows:
            if not tbl or not col:
                continue
            info = columns.setdefault(tbl.lower(), {}).setdefault(col.lower(), {"column": col, "usage": set(), "statements": 0})
            try:
                info["usage"] |= set(json.loads(usage or "[]"))
            except ValueError:
                pass
            info["statements"] += n
        sql = f"""SELECT json_extract(t.value, '$.name') AS name, json_extract(t.value, '$.access') AS access,
                         COUNT(*) AS n, MAX(q.ts) AS last_ts, group_concat(DISTINCT q.session_id) AS sessions
                  FROM queries q, json_each(q.tables) t
                  WHERE q.tables IS NOT NULL AND q.tables != '[]' {''.join(' AND ' + w for w in where)}
                  GROUP BY name, access"""
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        merged = {}
        for name, access, n, last_ts, sessions in rows:
            if not name:
                continue
            info = merged.setdefault(name.lower(), {"table": name, "access": set(), "statements": 0,
                                                    "sessions": set(), "last_ts": 0})
            info["access"] |= set((access or "").split("/")) - {""}
            info["statements"] += n
            info["sessions"] |= set((sessions or "").split(",")) - {""}
            info["last_ts"] = max(info["last_ts"], last_ts or 0)
        order = {"read": 0, "filter": 1, "write": 2}
        return [dict(v, access=sorted(v["access"]), sessions=len(v["sessions"]),
                     columns=[dict(c, usage=sorted(c["usage"], key=lambda u: order.get(u, 9)))
                              for c in sorted(columns.get(key, {}).values(), key=lambda c: c["column"].lower())])
                for key, v in sorted(merged.items(), key=lambda kv: kv[1]["table"].lower())]

    def joins_usage(self, session=None):
        """Distinct joins observed in the queries of session: [{"left_table", "left_column", "right_table",
        "right_column", "count"}]"""
        where, args = self._session_filter(session)
        sql = f"""SELECT json_extract(j.value, '$.left_table'), json_extract(j.value, '$.left_column'),
                         json_extract(j.value, '$.right_table'), json_extract(j.value, '$.right_column'), COUNT(*)
                  FROM queries q, json_each(q.joins) j
                  WHERE q.joins IS NOT NULL AND q.joins != '[]' {''.join(' AND ' + w for w in where)}
                  GROUP BY 1, 2, 3, 4"""
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        merged = {}
        for lt, lc, rt, rc, n in rows:
            if not (lt and lc and rt and rc):
                continue
            a, b = (lt, lc), (rt, rc)
            key = tuple(x.lower() for x in (min(a, b) + max(a, b)))      # a = b is the same join as b = a
            info = merged.setdefault(key, {"left_table": lt, "left_column": lc, "right_table": rt,
                                           "right_column": rc, "count": 0})
            info["count"] += n
        return sorted(merged.values(), key=lambda j: -j["count"])

    def assign_queries(self, ids, session_id):
        ids = [int(i) for i in ids]
        if ids:
            self._write(f"UPDATE queries SET session_id = ? WHERE id IN ({', '.join('?' * len(ids))})",
                        [session_id] + ids)

    def delete_queries(self, session=None):
        if session == "none":
            self._write("DELETE FROM queries WHERE session_id IS NULL")
        elif session in (None, "all"):
            self._write("DELETE FROM queries")
        else:
            where, args = self._session_filter(session)
            self._write(f"DELETE FROM queries WHERE {where[0]}", args)

    def counts(self):
        with self._lock:
            total, unassigned = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(session_id IS NULL), 0) FROM queries").fetchone()
        return {"total": total, "unassigned": unassigned}

    # ------------------------------------------------------------------ sessions

    def create_session(self, label, started_at=None, ended_at=None, parent_id=None):
        cur = self._write("INSERT INTO sessions (label, started_at, ended_at, parent_id) VALUES (?, ?, ?, ?)",
                          (label, started_at or time.time(), ended_at, parent_id))
        return self.get_session(cur.lastrowid)

    def end_session(self, session_id, ended_at=None):
        self._write("UPDATE sessions SET ended_at = ? WHERE id = ?", (ended_at or time.time(), session_id))
        return self.get_session(session_id)

    def update_session(self, session_id, label=None, notes=None):
        if label is not None:
            self._write("UPDATE sessions SET label = ? WHERE id = ?", (label, session_id))
        if notes is not None:
            self._write("UPDATE sessions SET notes = ? WHERE id = ?", (notes, session_id))
        return self.get_session(session_id)

    def delete_session(self, session_id, keep_queries=True):
        """Deletes the session and its sub-sessions; their queries become unassigned unless keep_queries is False."""
        if not keep_queries:
            self.delete_queries(session_id)
        self._write("DELETE FROM sessions WHERE id = ?", (session_id,))

    def delete_all_sessions(self, keep_queries=True):
        if not keep_queries:
            self._write("DELETE FROM queries WHERE session_id IS NOT NULL")
        self._write("DELETE FROM sessions")

    def get_session(self, session_id):
        rows = self._rows(_SESSION_SQL + " WHERE s.id = ? GROUP BY s.id", (session_id,))
        return rows[0] if rows else None

    def list_sessions(self):
        return self._rows(_SESSION_SQL + " GROUP BY s.id ORDER BY s.id DESC")


# the ids of a session and of all its sub-sessions, at any depth (one ? = the session id)
_TREE = """WITH RECURSIVE tree(id) AS (SELECT ? UNION ALL SELECT s.id FROM sessions s JOIN tree ON s.parent_id = tree.id)
           SELECT id FROM tree"""

# the counts of a session include all its sub-sessions
_SESSION_SQL = """WITH RECURSIVE tree(root, id) AS (
                      SELECT id, id FROM sessions
                      UNION ALL SELECT tree.root, s.id FROM sessions s JOIN tree ON s.parent_id = tree.id)
                  SELECT s.*, COUNT(q.id) AS queries, COALESCE(SUM(q.noise = 0), 0) AS relevant,
                         COALESCE(SUM(q.error IS NOT NULL), 0) AS errors
                  FROM sessions s JOIN tree ON tree.root = s.id
                  LEFT JOIN queries q ON q.session_id = tree.id"""


def _decode(row):
    for k in JSON_FIELDS:
        if k in row and isinstance(row[k], str):
            try:
                row[k] = json.loads(row[k])
            except ValueError:
                pass
    return row
