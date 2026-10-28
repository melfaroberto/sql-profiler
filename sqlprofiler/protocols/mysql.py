"""MySQL / MariaDB client-server protocol, decoded just enough to extract statements and outcomes.

The decoder clears a few capability bits in the server greeting so that the client does not switch to
TLS or compression (both would make the stream unreadable) and uses the classic EOF framing. Every
other byte is forwarded untouched.

Captured commands:
  COM_QUERY          text statements
  COM_STMT_PREPARE   remembered (statement id -> SQL); logged only if it fails
  COM_STMT_EXECUTE   the prepared SQL plus the decoded binary parameters
  COM_INIT_DB        logged as "USE `db`"
"""
import struct
from collections import deque
from datetime import date, datetime, timedelta

from ..events import CapturedQuery
from .base import Decoder, Framer

CLIENT_CONNECT_WITH_DB = 0x00000008
CLIENT_COMPRESS = 0x00000020
CLIENT_PROTOCOL_41 = 0x00000200
CLIENT_SSL = 0x00000800
CLIENT_SECURE_CONNECTION = 0x00008000
CLIENT_PLUGIN_AUTH_LENENC_DATA = 0x00200000
CLIENT_DEPRECATE_EOF = 0x01000000
CLIENT_OPTIONAL_RESULTSET_METADATA = 0x02000000
CLIENT_ZSTD_COMPRESSION = 0x04000000
CLIENT_QUERY_ATTRIBUTES = 0x08000000

# capabilities removed from the server greeting
STRIPPED = (CLIENT_SSL | CLIENT_COMPRESS | CLIENT_ZSTD_COMPRESSION | CLIENT_DEPRECATE_EOF
            | CLIENT_QUERY_ATTRIBUTES | CLIENT_OPTIONAL_RESULTSET_METADATA)

SERVER_MORE_RESULTS_EXISTS = 0x0008
SERVER_STATUS_CURSOR_EXISTS = 0x0040

COM_QUIT, COM_INIT_DB, COM_QUERY, COM_FIELD_LIST = 0x01, 0x02, 0x03, 0x04
COM_STATISTICS, COM_CHANGE_USER = 0x09, 0x11
COM_BINLOG_DUMP, COM_BINLOG_DUMP_GTID = 0x12, 0x1e
COM_STMT_PREPARE, COM_STMT_EXECUTE, COM_STMT_SEND_LONG_DATA = 0x16, 0x17, 0x18
COM_STMT_CLOSE, COM_STMT_RESET, COM_STMT_FETCH = 0x19, 0x1a, 0x1c

MAX_PACKET = 0xFFFFFF

# binary protocol column types
T_DECIMAL, T_TINY, T_SHORT, T_LONG, T_FLOAT, T_DOUBLE, T_NULL, T_TIMESTAMP = 0, 1, 2, 3, 4, 5, 6, 7
T_LONGLONG, T_INT24, T_DATE, T_TIME, T_DATETIME, T_YEAR = 8, 9, 10, 11, 12, 13


class MySQLFramer(Framer):
    header_size = 4

    def parse_header(self, h):
        return None, h[3], h[0] | h[1] << 8 | h[2] << 16


def lenenc_int(buf, pos):
    """Length-encoded integer -> (value, new position); value is None for the NULL marker 0xFB."""
    b = buf[pos]
    if b < 0xFB:
        return b, pos + 1
    if b == 0xFB:
        return None, pos + 1
    size = {0xFC: 2, 0xFD: 3, 0xFE: 8}[b]
    if pos + 1 + size > len(buf):
        raise IndexError("truncated length-encoded integer")
    return int.from_bytes(buf[pos + 1:pos + 1 + size], "little"), pos + 1 + size


def lenenc_bytes(buf, pos):
    n, pos = lenenc_int(buf, pos)
    if n is None:
        return None, pos
    if pos + n > len(buf):
        raise IndexError("truncated string")
    return bytes(buf[pos:pos + n]), pos + n


def error_text(head):
    """ERR packet -> "1146 (42S02): Table 'x' doesn't exist" """
    code = int.from_bytes(head[1:3], "little")
    if head[3:4] == b"#":
        state, msg = head[4:9].decode("ascii", "replace"), head[9:]
        return f"{code} ({state}): {msg.decode('utf-8', 'replace')}"
    return f"{code}: {head[3:].decode('utf-8', 'replace')}"


def decode_binary_value(typ, unsigned, buf, pos):
    """One value of the binary protocol -> (python value, new position)."""
    if typ == T_NULL:
        return None, pos
    if typ == T_TINY:
        return struct.unpack_from("<B" if unsigned else "<b", buf, pos)[0], pos + 1
    if typ in (T_SHORT, T_YEAR):
        return struct.unpack_from("<H" if unsigned else "<h", buf, pos)[0], pos + 2
    if typ in (T_LONG, T_INT24):
        return struct.unpack_from("<I" if unsigned else "<i", buf, pos)[0], pos + 4
    if typ == T_LONGLONG:
        return struct.unpack_from("<Q" if unsigned else "<q", buf, pos)[0], pos + 8
    if typ == T_FLOAT:
        return round(struct.unpack_from("<f", buf, pos)[0], 7), pos + 4
    if typ == T_DOUBLE:
        return struct.unpack_from("<d", buf, pos)[0], pos + 8
    if typ in (T_DATE, T_DATETIME, T_TIMESTAMP):
        n = buf[pos]
        p = pos + 1
        if n == 0:
            return ("0000-00-00" if typ == T_DATE else "0000-00-00 00:00:00"), p
        year, month, day = struct.unpack_from("<HBB", buf, p)
        hour = minute = second = micro = 0
        if n >= 7:
            hour, minute, second = buf[p + 4], buf[p + 5], buf[p + 6]
        if n >= 11:
            micro = struct.unpack_from("<I", buf, p + 7)[0]
        try:
            value = date(year, month, day) if typ == T_DATE else datetime(year, month, day, hour, minute, second, micro)
        except ValueError:          # zero dates and other values Python cannot represent
            value = f"{year:04}-{month:02}-{day:02}" + ("" if typ == T_DATE else f" {hour:02}:{minute:02}:{second:02}")
        return value, p + n
    if typ == T_TIME:
        n = buf[pos]
        p = pos + 1
        if n == 0:
            return timedelta(0), p
        negative, days, hour, minute, second = struct.unpack_from("<BIBBB", buf, p)
        micro = struct.unpack_from("<I", buf, p + 8)[0] if n >= 12 else 0
        value = timedelta(days=days, hours=hour, minutes=minute, seconds=second, microseconds=micro)
        return (-value if negative else value), p + n
    # DECIMAL, VARCHAR, JSON, ENUM, SET, BLOBs, STRING, GEOMETRY, BIT: length-encoded bytes
    raw, pos = lenenc_bytes(buf, pos)
    if typ in (T_DECIMAL, 246):
        return raw.decode("ascii", "replace"), pos
    try:
        return raw.decode("utf-8"), pos
    except UnicodeDecodeError:
        return raw, pos


class MySQLDecoder(Decoder):
    protocol = "mysql"
    dialect = "mysql"

    def __init__(self, conn, emit, max_sql_bytes=1 << 20):
        super().__init__(conn, emit, max_sql_bytes)
        self.phase = "greeting"            # greeting -> auth -> command (-> closed)
        self._hold = bytearray()
        self._cf = MySQLFramer(max_sql_bytes + 64)
        self._sf = MySQLFramer(64 * 1024)
        self._partial = {"client": None, "server": None}
        self.client_caps = 0
        self.got_response = False
        self.pending = deque()
        self.statements = {}               # statement id -> {"sql", "nparams", "types", "long"}

    # ------------------------------------------------------------------ framing

    def _logical(self, framer, data, side):
        """Merges packets split at 16 MB into one logical frame."""
        for f in framer.feed(data):
            part = self._partial[side]
            if part is not None:
                part.head += f.head[:max(framer.head_limit - len(part.head), 0)]
                part.length += f.length
                if f.length == MAX_PACKET:
                    continue
                self._partial[side] = None
                part.head = bytes(part.head)
                yield part
            elif f.length == MAX_PACKET:
                f.head = bytearray(f.head)
                self._partial[side] = f
            else:
                yield f

    # ------------------------------------------------------------------ directions

    def _server(self, data):
        if self.phase == "greeting":
            self._hold += data
            if len(self._hold) < 4:
                return b""
            length = self._hold[0] | self._hold[1] << 8 | self._hold[2] << 16
            if len(self._hold) < 4 + length:
                return b""
            out, self._hold = self._hold, bytearray()
            self._patch_greeting(out, length)
            self.phase = "auth"
            for f in self._logical(self._sf, bytes(out[4 + length:]), "server"):
                self._on_server(f)
            return bytes(out)
        for f in self._logical(self._sf, data, "server"):
            self._on_server(f)
        return data

    def _client(self, data):
        if not self.got_response:
            # Some clients (libmysqlclient 8/9) announce DEPRECATE_EOF or QUERY_ATTRIBUTES even when the
            # greeting did not offer them, and the real server would honour them: clear the same bits here
            # so that the client and the server agree on the framing.
            self._hold += data
            if len(self._hold) < 8:
                return b"", b""
            length = self._hold[0] | self._hold[1] << 8 | self._hold[2] << 16
            if len(self._hold) < 4 + length:
                return b"", b""
            data, self._hold = self._hold, bytearray()
            caps = int.from_bytes(data[4:8], "little")
            data[4:8] = (caps & ~(STRIPPED & ~CLIENT_SSL)).to_bytes(4, "little")
            data = bytes(data)
        for f in self._logical(self._cf, data, "client"):
            self._on_client(f)
            if self.passthrough:
                break
        return data, b""

    def _patch_greeting(self, packet, length):
        """Clears STRIPPED from the capability flags of a HandshakeV10 packet (in place)."""
        p = 4
        if packet[p] != 10:                # error packet ("too many connections") or unknown protocol
            return
        end = packet.index(0, p + 1)
        self.conn.note = "server " + packet[p + 1:end].decode("ascii", "replace")
        lo = end + 1 + 4 + 8 + 1           # connection id, auth data part 1, filler
        caps = int.from_bytes(packet[lo:lo + 2], "little")
        caps &= ~STRIPPED & 0xFFFF
        packet[lo:lo + 2] = caps.to_bytes(2, "little")
        hi = lo + 2 + 1 + 2                # charset, status flags
        if hi + 2 <= 4 + length:
            caps = int.from_bytes(packet[hi:hi + 2], "little")
            caps &= ~(STRIPPED >> 16) & 0xFFFF
            packet[hi:hi + 2] = caps.to_bytes(2, "little")

    # ------------------------------------------------------------------ client packets

    def _on_client(self, f):
        if self.phase == "auth":
            if not self.got_response:
                self._handshake_response(f)
            return
        if self.phase != "command" or f.seq != 0 or not f.head:
            return                         # LOCAL INFILE data and other continuations
        cmd, head = f.head[0], f.head
        wall, perf = self.now()
        p = {"cmd": cmd, "wall": wall, "perf": perf, "expect": "single", "state": "start",
             "emit": False, "sql": "", "kind": "query", "params": None, "rows": None, "affected": None,
             "error": None, "truncated": f.truncated}

        if cmd == COM_QUERY:
            p.update(expect="result", emit=True, sql=self._query_text(head))
        elif cmd == COM_STMT_PREPARE:
            p.update(expect="prepare", kind="prepare", sql=self.text(head[1:]))
        elif cmd == COM_STMT_EXECUTE:
            sql, params = self._execute(head)
            p.update(expect="result", emit=True, kind="execute", sql=sql, params=params)
        elif cmd == COM_INIT_DB:
            db = self.text(head[1:])
            p.update(emit=True, kind="use", sql=f"USE `{db}`", db=db)
        elif cmd == COM_STMT_CLOSE:
            self.statements.pop(int.from_bytes(head[1:5], "little"), None)
            return
        elif cmd == COM_STMT_SEND_LONG_DATA:
            st = self.statements.get(int.from_bytes(head[1:5], "little"))
            if st is not None:
                st["long"].add(int.from_bytes(head[5:7], "little"))
            return
        elif cmd == COM_QUIT:
            return
        elif cmd == COM_CHANGE_USER:
            self.phase = "auth"
            return
        elif cmd in (COM_BINLOG_DUMP, COM_BINLOG_DUMP_GTID):
            self.passthrough = True
            self.conn.note = "replication stream (not decoded)"
            return
        elif cmd == COM_FIELD_LIST:
            p["expect"] = "fields"
        elif cmd == COM_STMT_FETCH:
            p.update(expect="result", state="rows")
        self.pending.append(p)

    def _handshake_response(self, f):
        head = f.head
        self.got_response = True
        self.client_caps = caps = int.from_bytes(head[0:4], "little")
        if caps & CLIENT_SSL and f.length == 32:
            # the client insisted on TLS (e.g. ssl-mode=REQUIRED): keep forwarding, stop decoding
            self.conn.encrypted = True
            self.conn.note = "TLS requested by the client: queries cannot be captured"
            self.passthrough = True
            return
        if not caps & CLIENT_PROTOCOL_41:
            return
        end = head.find(b"\0", 32)
        if end < 0:
            return
        self.conn.user = self.text(head[32:end])
        p = end + 1
        try:
            if caps & CLIENT_PLUGIN_AUTH_LENENC_DATA:
                _, p = lenenc_bytes(head, p)
            elif caps & CLIENT_SECURE_CONNECTION:
                p += 1 + head[p]
            else:
                p = head.index(b"\0", p) + 1
            if caps & CLIENT_CONNECT_WITH_DB:
                end = head.find(b"\0", p)
                if end > p:
                    self.conn.database = self.text(head[p:end])
        except (IndexError, ValueError):
            pass

    def _query_text(self, head):
        pos = 1
        if self.client_caps & CLIENT_QUERY_ATTRIBUTES:
            count, pos = lenenc_int(head, pos)
            _, pos = lenenc_int(head, pos)        # parameter set count, always 1
            if count:
                _, pos = self._binary_params(head, pos, count, None, names=True)
        return self.text(head[pos:])

    def _execute(self, head):
        stmt_id = int.from_bytes(head[1:5], "little")
        flags = head[5]
        st = self.statements.get(stmt_id)
        if st is None:
            return f"/* prepared statement #{stmt_id} (prepared before the capture started) */", None
        n, pos = st["nparams"], 10
        if self.client_caps & CLIENT_QUERY_ATTRIBUTES and flags & 0x08:
            n, pos = lenenc_int(head, pos)
        params = None
        if n:
            params, _ = self._binary_params(head, pos, n, st, names=bool(self.client_caps & CLIENT_QUERY_ATTRIBUTES))
        st["long"] = set()
        return st["sql"], params

    def _binary_params(self, head, pos, n, st, names):
        """Null bitmap, new-params-bound flag, types and values -> (list of values, new position)."""
        values = []
        try:
            bitmap = head[pos:pos + (n + 7) // 8]
            pos += (n + 7) // 8
            bound = head[pos]
            pos += 1
            types = st["types"] if st else None
            if bound:
                types = [(head[pos + 2 * i], head[pos + 2 * i + 1]) for i in range(n)]
                pos += 2 * n
                if names:
                    for _ in range(n):
                        _, pos = lenenc_bytes(head, pos)
                if st is not None:
                    st["types"] = types
            if types is None:
                return ["?"] * n, pos
            for i in range(n):
                if bitmap[i // 8] & (1 << (i % 8)):
                    values.append(None)
                elif st is not None and i in st["long"]:
                    values.append("<long data>")
                else:
                    v, pos = decode_binary_value(types[i][0], types[i][1] & 0x80, head, pos)
                    values.append(v)
        except (IndexError, KeyError, struct.error):
            values += ["<truncated>"] * (n - len(values))
        return values, pos

    # ------------------------------------------------------------------ server packets

    def _on_server(self, f):
        b = f.head[0] if f.head else -1
        if self.phase == "auth":
            if b == 0x00:
                self.phase = "command"
            elif b == 0xFF:
                self.phase = "closed"
            return
        if self.phase != "command" or not self.pending:
            return
        p = self.pending[0]
        expect, state = p["expect"], p["state"]

        if b == 0xFF and not (expect == "prepare" and state == "skip"):
            p["error"] = error_text(f.head)
            return self._complete(p)

        if expect == "single":
            return self._complete(p)

        if expect == "fields":
            if b == 0xFE and f.length < 9:
                self._complete(p)
            return

        if expect == "prepare":
            if state == "start":
                stmt_id = int.from_bytes(f.head[1:5], "little")
                ncols = int.from_bytes(f.head[5:7], "little")
                nparams = int.from_bytes(f.head[7:9], "little")
                self.statements[stmt_id] = {"sql": p["sql"], "nparams": nparams, "types": None, "long": set()}
                eof = 0 if self._deprecate_eof else 1
                p["skip"] = nparams + ncols + (eof if nparams else 0) + (eof if ncols else 0)
                p["state"] = "skip"
                if not p["skip"]:
                    self._complete(p)
            else:
                p["skip"] -= 1
                if not p["skip"]:
                    self._complete(p)
            return

        # expect == "result": OK / ERR / LOCAL INFILE / result set(s)
        if state == "start":
            if b == 0x00:
                affected, pos = lenenc_int(f.head, 1)
                _, pos = lenenc_int(f.head, pos)
                status = int.from_bytes(f.head[pos:pos + 2], "little")
                p["affected"] = (p["affected"] or 0) + (affected or 0)
                if not status & SERVER_MORE_RESULTS_EXISTS:
                    self._complete(p)
            elif b == 0xFB:
                pass                       # LOCAL INFILE request: the OK/ERR comes after the file
            else:
                p["left"], _ = lenenc_int(f.head, 0)
                p["state"] = "cols"
                p["rows"] = p["rows"] or 0
        elif state == "cols":
            p["left"] -= 1
            if p["left"] <= 0:
                p["state"] = "rows" if self._deprecate_eof else "cols_eof"
        elif state == "cols_eof":
            status = int.from_bytes(f.head[3:5], "little")
            p["state"] = "rows"
            if status & SERVER_STATUS_CURSOR_EXISTS:
                self._complete(p)          # rows will come through COM_STMT_FETCH
        elif state == "rows":
            if b == 0xFE and f.length < MAX_PACKET:
                if self._deprecate_eof:
                    _, pos = lenenc_int(f.head, 1)
                    _, pos = lenenc_int(f.head, pos)
                    status = int.from_bytes(f.head[pos:pos + 2], "little")
                else:
                    status = int.from_bytes(f.head[3:5], "little")
                if status & SERVER_MORE_RESULTS_EXISTS:
                    p["state"] = "start"
                else:
                    self._complete(p)
            else:
                p["rows"] = (p["rows"] or 0) + 1

    @property
    def _deprecate_eof(self):
        return bool(self.client_caps & CLIENT_DEPRECATE_EOF)

    def _complete(self, p):
        self.pending.popleft()
        if p["cmd"] == COM_INIT_DB and not p["error"]:
            self.conn.database = p["db"]
        # a failed PREPARE is worth showing; a successful one is shown when it is executed
        if not (p["emit"] or (p["kind"] == "prepare" and p["error"])):
            return
        wall, perf = self.now()
        self.emit(CapturedQuery(
            conn=self.conn, sql=p["sql"], kind=p["kind"], params=p["params"], started_at=p["wall"],
            duration_ms=(perf - p["perf"]) * 1000, rows=p["rows"], affected=p["affected"],
            error=p["error"], truncated=p["truncated"]))
