"""Helpers that build protocol messages for the tests."""
import struct

from sqlprofiler.protocols import mysql as M


# ---------------------------------------------------------------------- MySQL

def pkt(seq, payload):
    return len(payload).to_bytes(3, "little") + bytes([seq & 0xFF]) + payload


def lenenc(n):
    if n < 0xFB:
        return bytes([n])
    if n < 1 << 16:
        return b"\xfc" + n.to_bytes(2, "little")
    if n < 1 << 24:
        return b"\xfd" + n.to_bytes(3, "little")
    return b"\xfe" + n.to_bytes(8, "little")


def lstr(b):
    b = b.encode() if isinstance(b, str) else b
    return lenenc(len(b)) + b


SERVER_CAPS = (0x00000001 | 0x00000008 | 0x00000020 | M.CLIENT_PROTOCOL_41 | M.CLIENT_SSL | M.CLIENT_SECURE_CONNECTION
               | 0x00080000 | M.CLIENT_PLUGIN_AUTH_LENENC_DATA | M.CLIENT_DEPRECATE_EOF | 0x00010000 | 0x00020000)


def greeting(caps=SERVER_CAPS, version=b"5.7.24-test"):
    payload = (b"\x0a" + version + b"\0" + (7).to_bytes(4, "little") + b"abcdefgh" + b"\0"
               + (caps & 0xFFFF).to_bytes(2, "little") + b"\x21" + b"\x02\x00" + (caps >> 16).to_bytes(2, "little")
               + bytes([21]) + b"\0" * 10 + b"ijklmnopqrst\0" + b"mysql_native_password\0")
    return pkt(0, payload)


def greeting_caps(raw):
    """Capability flags of a greeting packet (as forwarded to the client)."""
    p = 4
    end = raw.index(0, p + 1)
    lo = end + 1 + 4 + 8 + 1
    return int.from_bytes(raw[lo:lo + 2], "little") | int.from_bytes(raw[lo + 5:lo + 7], "little") << 16


CLIENT_CAPS = (0x00000001 | 0x00000008 | M.CLIENT_PROTOCOL_41 | M.CLIENT_SECURE_CONNECTION | 0x00080000
               | 0x00010000 | 0x00020000)


def handshake_response(user="app", db="shop", caps=CLIENT_CAPS):
    auth = b"\x01" * 20
    payload = (caps.to_bytes(4, "little") + (1 << 24).to_bytes(4, "little") + b"\x21" + b"\0" * 23
               + user.encode() + b"\0" + bytes([len(auth)]) + auth)
    if caps & 0x08:
        payload += db.encode() + b"\0"
    payload += b"mysql_native_password\0"
    return pkt(1, payload)


def ssl_request(caps=CLIENT_CAPS | M.CLIENT_SSL):
    return pkt(1, caps.to_bytes(4, "little") + (1 << 24).to_bytes(4, "little") + b"\x21" + b"\0" * 23)


def ok(seq=2, affected=0, insert_id=0, status=0x0002):
    return pkt(seq, b"\x00" + lenenc(affected) + lenenc(insert_id) + status.to_bytes(2, "little") + b"\0\0")


def err(seq=1, code=1146, state=b"42S02", msg=b"Table 'shop.nope' doesn't exist"):
    return pkt(seq, b"\xff" + code.to_bytes(2, "little") + b"#" + state + msg)


def eof(seq, status=0x0002):
    return pkt(seq, b"\xfe\x00\x00" + status.to_bytes(2, "little"))


def coldef(seq, name="c"):
    return pkt(seq, lstr("def") + lstr("shop") + lstr("t") + lstr("t") + lstr(name) + lstr(name)
               + b"\x0c" + b"\x21\x00" + b"\xff\x00\x00\x00" + b"\xfd" + b"\x00\x00" + b"\x00" + b"\x00\x00")


def resultset(rows, ncols=1, seq=1, status=0x0002, deprecate_eof=False):
    out = pkt(seq, lenenc(ncols))
    seq += 1
    for i in range(ncols):
        out += coldef(seq, f"c{i}")
        seq += 1
    if not deprecate_eof:
        out += eof(seq)
        seq += 1
    for r in rows:
        out += pkt(seq, b"".join(lstr(str(v)) for v in r))
        seq += 1
    if deprecate_eof:
        out += pkt(seq, b"\xfe" + lenenc(0) + lenenc(0) + status.to_bytes(2, "little") + b"\0\0")
    else:
        out += eof(seq, status)
    return out


def com_query(sql):
    return pkt(0, b"\x03" + (sql.encode() if isinstance(sql, str) else sql))


def com_prepare(sql):
    return pkt(0, b"\x16" + sql.encode())


def prepare_ok(stmt_id, nparams, ncols):
    out = pkt(1, b"\x00" + stmt_id.to_bytes(4, "little") + ncols.to_bytes(2, "little")
              + nparams.to_bytes(2, "little") + b"\x00" + b"\x00\x00")
    seq = 2
    for _ in range(nparams):
        out += coldef(seq, "?")
        seq += 1
    if nparams:
        out += eof(seq)
        seq += 1
    for i in range(ncols):
        out += coldef(seq, f"c{i}")
        seq += 1
    if ncols:
        out += eof(seq)
    return out


def com_execute(stmt_id, params):
    """params: list of (type, unsigned_flag, encoded value bytes or None)"""
    n = len(params)
    bitmap = bytearray((n + 7) // 8)
    for i, (_, _, v) in enumerate(params):
        if v is None:
            bitmap[i // 8] |= 1 << (i % 8)
    payload = b"\x17" + stmt_id.to_bytes(4, "little") + b"\x00" + (1).to_bytes(4, "little")
    if n:
        payload += bytes(bitmap) + b"\x01"
        payload += b"".join(bytes([t, 0x80 if u else 0]) for t, u, _ in params)
        payload += b"".join(v for _, _, v in params if v is not None)
    return pkt(0, payload)


# ---------------------------------------------------------------------- PostgreSQL

def pg_startup(user="app", database="shop"):
    body = struct.pack("!I", 196608) + b"user\0" + user.encode() + b"\0database\0" + database.encode() + b"\0\0"
    return struct.pack("!I", len(body) + 4) + body


def pg_ssl_request():
    return struct.pack("!II", 8, 80877103)


def pg_msg(t, body=b""):
    return t.encode() + struct.pack("!I", len(body) + 4) + body


def pg_query(sql):
    return pg_msg("Q", sql.encode() + b"\0")


def pg_parse(sql, name="", oids=()):
    return pg_msg("P", name.encode() + b"\0" + sql.encode() + b"\0" + struct.pack("!h", len(oids))
                  + b"".join(struct.pack("!I", o) for o in oids))


def pg_bind(values, formats=(), portal="", name=""):
    body = portal.encode() + b"\0" + name.encode() + b"\0" + struct.pack("!h", len(formats))
    body += b"".join(struct.pack("!h", f) for f in formats)
    body += struct.pack("!h", len(values))
    for v in values:
        body += struct.pack("!i", -1) if v is None else struct.pack("!i", len(v)) + v
    return pg_msg("B", body + struct.pack("!h", 0))


def pg_execute(portal=""):
    return pg_msg("E", portal.encode() + b"\0" + struct.pack("!i", 0))


def pg_complete(tag):
    return pg_msg("C", tag.encode() + b"\0")


def pg_ready():
    return pg_msg("Z", b"I")


def pg_error(message="relation \"nope\" does not exist", code="42P01"):
    return pg_msg("E", b"SERROR\0C" + code.encode() + b"\0M" + message.encode() + b"\0\0")


def pg_datarow(*values):
    body = struct.pack("!h", len(values)) + b"".join(struct.pack("!i", len(v)) + v for v in values)
    return pg_msg("D", body)
