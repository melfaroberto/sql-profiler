"""PostgreSQL frontend/backend protocol (v3), decoded just enough to extract statements and outcomes.

SSL and GSS encryption requests are answered "N" by the proxy itself, so a client configured with
sslmode=prefer/allow/disable continues in clear text (sslmode=require cannot be intercepted).

Captured messages:
  Query (simple protocol)                       one event per Query message, until ReadyForQuery
  Parse / Bind / Execute (extended protocol)    one event per Execute, with the bound parameters
"""
import struct
import uuid
from collections import deque

from ..events import CapturedQuery
from .base import Decoder, Framer

SSL_REQUEST, GSSENC_REQUEST, CANCEL_REQUEST = 80877103, 80877104, 80877102

# parameter type OIDs whose binary format is decoded
OID_BOOL, OID_INT8, OID_INT2, OID_INT4, OID_FLOAT4, OID_FLOAT8, OID_UUID = 16, 20, 21, 23, 700, 701, 2950
TEXT_OIDS = {18, 19, 25, 1042, 1043, 114, 3802}      # char, name, text, bpchar, varchar, json, jsonb
NUMERIC_OIDS = {20, 21, 23, 26, 700, 701, 1700}
ARRAY_OIDS = {1000: OID_BOOL, 1005: OID_INT2, 1007: OID_INT4, 1016: OID_INT8, 1021: OID_FLOAT4, 1022: OID_FLOAT8,
              1009: 25, 1015: 1043, 2951: OID_UUID}


class StartupFramer(Framer):
    """Untyped messages sent before the startup packet: length (int32) + body."""
    header_size = 4

    def parse_header(self, h):
        return None, 0, struct.unpack("!I", h)[0] - 4


class TypedFramer(Framer):
    """Regular messages: type byte + length (int32, includes itself) + body."""
    header_size = 5

    def parse_header(self, h):
        return chr(h[0]), 0, struct.unpack("!I", h[1:5])[0] - 4


def cstring(buf, pos):
    end = buf.index(b"\0", pos)
    return bytes(buf[pos:end]), end + 1


def decode_binary_param(oid, raw):
    try:
        if oid == OID_BOOL and len(raw) == 1:
            return bool(raw[0])
        if oid == OID_INT2 and len(raw) == 2:
            return struct.unpack("!h", raw)[0]
        if oid == OID_INT4 and len(raw) == 4:
            return struct.unpack("!i", raw)[0]
        if oid == OID_INT8 and len(raw) == 8:
            return struct.unpack("!q", raw)[0]
        if oid == OID_FLOAT4 and len(raw) == 4:
            return round(struct.unpack("!f", raw)[0], 7)
        if oid == OID_FLOAT8 and len(raw) == 8:
            return struct.unpack("!d", raw)[0]
        if oid == OID_UUID and len(raw) == 16:
            return str(uuid.UUID(bytes=raw))
        if oid in TEXT_OIDS:
            return raw.decode("utf-8")
        if oid in ARRAY_OIDS:
            return _array(ARRAY_OIDS[oid], raw)
    except (struct.error, UnicodeDecodeError, ValueError):
        pass
    return raw


def _array(element_oid, raw):
    """One-dimensional array in binary format -> list"""
    ndim, _, _ = struct.unpack_from("!iiI", raw, 0)
    if ndim == 0:
        return []
    if ndim != 1:
        raise ValueError("multi-dimensional array")
    count = struct.unpack_from("!i", raw, 12)[0]
    pos, out = 20, []
    for _ in range(count):
        size = struct.unpack_from("!i", raw, pos)[0]
        pos += 4
        if size < 0:
            out.append(None)
            continue
        out.append(decode_binary_param(element_oid, raw[pos:pos + size]))
        pos += size
    return out


def rows_from_tag(tag):
    """'SELECT 5' -> 5, 'INSERT 0 3' -> 3, 'CREATE TABLE' -> None"""
    last = tag.rsplit(" ", 1)[-1]
    return int(last) if last.isdigit() else None


class PostgresDecoder(Decoder):
    protocol = "postgres"
    dialect = "postgres"

    def __init__(self, conn, emit, max_sql_bytes=1 << 20):
        super().__init__(conn, emit, max_sql_bytes)
        self.startup = True
        self._hold = bytearray()
        self._cf = TypedFramer(max_sql_bytes + 64)
        self._sf = TypedFramer(64 * 1024)
        self.statements = {}       # name -> {"sql", "oids"}
        self.portals = {}          # name -> {"sql", "params"}
        self.pending = deque()     # {"type": "simple"|"exec"|"sync", ...}
        self.describes = deque()   # statements whose ParameterDescription the server will send

    # ------------------------------------------------------------------ client

    def _client(self, data):
        if not self.startup:
            for f in self._cf.feed(data):
                self._on_client(f)
            return data, b""

        self._hold += data
        to_server, to_client = bytearray(), bytearray()
        while self.startup and len(self._hold) >= 8:
            length, code = struct.unpack("!II", self._hold[:8])
            if len(self._hold) < length:
                break
            msg, self._hold = bytes(self._hold[:length]), self._hold[length:]
            if code in (SSL_REQUEST, GSSENC_REQUEST):
                to_client += b"N"          # "not supported": the client goes on without encryption
                continue
            to_server += msg
            if code == CANCEL_REQUEST:
                self.passthrough = True
                self.conn.note = "cancel request"
                break
            self._startup_params(msg[8:])
            self.startup = False
        if not self.startup and self._hold:
            rest, self._hold = bytes(self._hold), bytearray()
            to_server += rest
            if not self.passthrough:
                for f in self._cf.feed(rest):
                    self._on_client(f)
        return bytes(to_server), bytes(to_client)

    def _startup_params(self, body):
        parts = body.split(b"\0")
        params = {self.text(k): self.text(v) for k, v in zip(parts[0::2], parts[1::2]) if k}
        self.conn.user = params.get("user", "")
        self.conn.database = params.get("database", self.conn.user)

    def _on_client(self, f):
        t, head = f.kind, f.head
        if t == "Q":
            wall, perf = self.now()
            self.pending.append({"type": "simple", "sql": self.text(head.rstrip(b"\0")), "wall": wall, "perf": perf,
                                 "rows": None, "affected": None, "error": None, "truncated": f.truncated})
        elif t == "P":
            name, pos = cstring(head, 0)
            sql, pos = cstring(head, pos)
            n = struct.unpack_from("!h", head, pos)[0] if pos + 2 <= len(head) else 0
            oids = list(struct.unpack_from(f"!{n}I", head, pos + 2)) if n else []
            self.statements[name] = {"sql": self.text(sql), "oids": oids, "truncated": f.truncated}
        elif t == "B":
            self._bind(head, f.truncated)
        elif t == "E":
            portal, _ = cstring(head, 0)
            info = self.portals.get(portal, {"sql": "/* statement prepared before the capture started */",
                                             "params": None, "truncated": False})
            wall, perf = self.now()
            self.pending.append({"type": "exec", "sql": info["sql"], "params": info["params"], "wall": wall,
                                 "perf": perf, "rows": None, "affected": None, "error": None,
                                 "truncated": info["truncated"]})
        elif t == "D" and head[:1] == b"S":
            name, _ = cstring(head, 1)
            self.describes.append(name)
        elif t == "S":
            self.pending.append({"type": "sync"})
        elif t == "C":                      # Close statement/portal
            name, _ = cstring(head, 1)
            (self.statements if head[:1] == b"S" else self.portals).pop(name, None)

    def _bind(self, head, truncated):
        portal, pos = cstring(head, 0)
        name, pos = cstring(head, pos)
        st = self.statements.get(name, {"sql": "/* statement prepared before the capture started */",
                                        "oids": [], "truncated": False})
        params = []
        try:
            nfmt = struct.unpack_from("!h", head, pos)[0]
            formats = struct.unpack_from(f"!{nfmt}h", head, pos + 2)
            pos += 2 + 2 * nfmt
            n = struct.unpack_from("!h", head, pos)[0]
            pos += 2
            for i in range(n):
                size = struct.unpack_from("!i", head, pos)[0]
                pos += 4
                if size < 0:
                    params.append(None)
                    continue
                raw = bytes(head[pos:pos + size])
                if len(raw) < size:
                    raise IndexError
                pos += size
                fmt = formats[0] if nfmt == 1 else (formats[i] if nfmt else 0)
                oid = st["oids"][i] if i < len(st["oids"]) else 0
                if fmt == 0:
                    value = self.text(raw)
                    if oid in NUMERIC_OIDS:
                        value = _number(value)
                else:
                    value = decode_binary_param(oid, raw)
                params.append(value)
        except (IndexError, struct.error):
            params.append("<truncated>")
        self.portals[portal] = {"sql": st["sql"], "params": params or None, "truncated": st["truncated"] or truncated}

    # ------------------------------------------------------------------ server

    def _server(self, data):
        for f in self._sf.feed(data):
            self._on_server(f)
        return data

    def _current(self):
        for p in self.pending:
            if p["type"] != "sync":
                return p
            return None
        return None

    def _on_server(self, f):
        t = f.kind
        if t == "t" and self.describes:
            # the server tells the parameter types the client left unspecified (asyncpg, JDBC, ...)
            st = self.statements.get(self.describes.popleft())
            n = struct.unpack_from("!h", f.head, 0)[0]
            if st is not None and len(f.head) >= 2 + 4 * n:
                st["oids"] = list(struct.unpack_from(f"!{n}I", f.head, 2))
            return
        if t in ("C", "I", "s"):
            p = self._current()
            if p is None:
                return
            if t == "C":
                tag = self.text(f.head.rstrip(b"\0"))
                n = rows_from_tag(tag)
                if n is not None:
                    key = "rows" if tag.split(" ", 1)[0] in ("SELECT", "FETCH", "SHOW") else "affected"
                    p[key] = (p[key] or 0) + n
            if p["type"] == "exec":
                self.pending.remove(p)
                self._emit(p, "execute")
        elif t == "E":
            message = self._error(f.head)
            p = self._current()
            if p is None:
                return
            p["error"] = message
            if p["type"] == "exec":
                # after an error the server skips everything until Sync: drop those executes
                self.pending.remove(p)
                while self.pending and self.pending[0]["type"] == "exec":
                    self.pending.popleft()
                self._emit(p, "execute")
        elif t == "Z":
            self.describes.clear()
            while self.pending:
                p = self.pending.popleft()
                if p["type"] == "simple":
                    self._emit(p, "query")
                    break
                if p["type"] == "sync":
                    break

    def _error(self, head):
        fields, pos = {}, 0
        while pos < len(head) and head[pos] != 0:
            code = chr(head[pos])
            try:
                value, pos = cstring(head, pos + 1)
            except ValueError:
                break
            fields[code] = self.text(value)
        return f"{fields.get('C', '')}: {fields.get('M', 'error')}".strip(": ")

    def _emit(self, p, kind):
        _, perf = self.now()
        self.emit(CapturedQuery(conn=self.conn, sql=p["sql"], kind=kind, params=p.get("params"),
                                started_at=p["wall"], duration_ms=(perf - p["perf"]) * 1000, rows=p["rows"],
                                affected=p["affected"], error=p["error"], truncated=p["truncated"]))


def _number(text):
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text
