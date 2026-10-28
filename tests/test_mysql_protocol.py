import struct

import pytest

from sqlprofiler.events import ConnectionInfo
from sqlprofiler.protocols import mysql as M
from wire import (com_execute, com_prepare, com_query, err, greeting, greeting_caps, handshake_response, lstr, ok,
                  pkt, prepare_ok, resultset, ssl_request, CLIENT_CAPS)


class Harness:
    """Feeds a MySQL conversation to the decoder, optionally in tiny chunks."""

    def __init__(self, chunk=None, client_caps=CLIENT_CAPS, max_sql_bytes=1 << 20):
        self.events = []
        self.conn = ConnectionInfo(proxy="mysql:0", protocol="mysql", client="test:1")
        self.d = M.MySQLDecoder(self.conn, self.events.append, max_sql_bytes)
        self.chunk = chunk
        self.forwarded_greeting = self.server(greeting())
        self.client(handshake_response(caps=client_caps))
        self.server(ok(seq=2))

    def _split(self, data):
        if not self.chunk:
            return [data]
        return [data[i:i + self.chunk] for i in range(0, len(data), self.chunk)]

    def server(self, data):
        return b"".join(self.d.feed_server(part) for part in self._split(data))

    def client(self, data):
        out = b""
        for part in self._split(data):
            to_server, to_client = self.d.feed_client(part)
            assert to_client == b""
            out += to_server
        return out


@pytest.fixture(params=[None, 1, 7], ids=["whole", "byte-by-byte", "chunks-of-7"])
def h(request):
    return Harness(chunk=request.param)


def test_greeting_capabilities_are_stripped(h):
    caps = greeting_caps(h.forwarded_greeting)
    assert not caps & M.CLIENT_SSL
    assert not caps & M.CLIENT_COMPRESS
    assert not caps & M.CLIENT_DEPRECATE_EOF
    assert caps & M.CLIENT_PROTOCOL_41
    assert len(h.forwarded_greeting) == len(greeting())


def test_handshake_user_and_database(h):
    assert h.d.phase == "command"
    assert h.conn.user == "app"
    assert h.conn.database == "shop"


def test_text_query_with_result_set(h):
    assert h.client(com_query("SELECT * FROM orders")) == com_query("SELECT * FROM orders")
    h.server(resultset([(1, "a"), (2, "b"), (3, "c")], ncols=2))
    [e] = h.events
    assert e.sql == "SELECT * FROM orders"
    assert e.kind == "query"
    assert e.rows == 3
    assert e.error is None
    assert e.duration_ms >= 0


def test_ok_with_affected_rows(h):
    h.client(com_query("UPDATE orders SET paid = 1"))
    h.server(ok(seq=1, affected=4))
    [e] = h.events
    assert e.affected == 4 and e.rows is None


def test_error(h):
    h.client(com_query("SELECT * FROM nope"))
    h.server(err())
    [e] = h.events
    assert e.error == "1146 (42S02): Table 'shop.nope' doesn't exist"


def test_init_db_updates_connection(h):
    h.client(pkt(0, b"\x02other"))
    h.server(ok(seq=1))
    [e] = h.events
    assert e.sql == "USE `other`" and e.kind == "use"
    assert h.conn.database == "other"


def test_ping_and_quit_are_not_logged(h):
    h.client(pkt(0, b"\x0e"))
    h.server(ok(seq=1))
    h.client(com_query("SELECT 1"))
    h.server(resultset([(1,)]))
    h.client(pkt(0, b"\x01"))
    assert [e.sql for e in h.events] == ["SELECT 1"]


def test_multiple_result_sets(h):
    h.client(com_query("CALL report()"))
    h.server(resultset([(1,)], status=0x000A) + resultset([(1,), (2,)], seq=5, status=0x000A) + ok(seq=10, status=0x0002))
    [e] = h.events
    assert e.rows == 3


def test_prepared_statement_with_binary_parameters(h):
    h.client(com_prepare("SELECT * FROM orders WHERE id = ? AND code = ? AND note = ? AND created > ? AND qty = ?"))
    h.server(prepare_ok(stmt_id=5, nparams=5, ncols=2))
    assert h.events == []                     # a successful PREPARE is not logged by itself
    dt = struct.pack("<HBBBBB", 2026, 10, 27, 9, 30, 15)
    h.client(com_execute(5, [(M.T_LONG, False, struct.pack("<i", -42)),
                             (253, False, lstr("ABè")),
                             (253, False, None),
                             (M.T_DATETIME, False, bytes([len(dt)]) + dt),
                             (M.T_LONGLONG, True, struct.pack("<Q", 2 ** 63 + 1))]))
    h.server(resultset([(1, 2)], ncols=2))
    [e] = h.events
    assert e.kind == "execute"
    assert e.sql.startswith("SELECT * FROM orders WHERE id = ?")
    assert e.params[0] == -42
    assert e.params[1] == "ABè"
    assert e.params[2] is None
    assert str(e.params[3]) == "2026-10-27 09:30:15"
    assert e.params[4] == 2 ** 63 + 1
    assert e.rows == 1


def test_execute_reuses_types_from_previous_execution(h):
    h.client(com_prepare("SELECT ?"))
    h.server(prepare_ok(1, 1, 1))
    h.client(com_execute(1, [(M.T_LONG, False, struct.pack("<i", 1))]))
    h.server(resultset([(1,)]))
    # second execution: new-params-bound flag = 0, no types
    h.client(pkt(0, b"\x17" + (1).to_bytes(4, "little") + b"\x00" + (1).to_bytes(4, "little") + b"\x00" + b"\x00"
                 + struct.pack("<i", 77)))
    h.server(resultset([(77,)]))
    assert [e.params for e in h.events] == [[1], [77]]


def test_failed_prepare_is_logged(h):
    h.client(com_prepare("SELECT * FROM nope WHERE id = ?"))
    h.server(err(seq=1))
    [e] = h.events
    assert e.kind == "prepare" and e.error.startswith("1146")


def test_unknown_statement_id(h):
    h.client(com_execute(99, []))
    h.server(ok(seq=1))
    [e] = h.events
    assert "#99" in e.sql


def test_tls_request_switches_to_passthrough():
    events = []
    conn = ConnectionInfo(proxy="mysql:0", protocol="mysql", client="t")
    d = M.MySQLDecoder(conn, events.append)
    d.feed_server(greeting())
    data = ssl_request()
    assert d.feed_client(data) == (data, b"")
    assert conn.encrypted and d.passthrough
    assert d.feed_client(b"\x16\x03\x01garbage") == (b"\x16\x03\x01garbage", b"")


def test_capabilities_not_offered_are_cleared_in_the_client_response():
    # libmysqlclient announces DEPRECATE_EOF / QUERY_ATTRIBUTES even if the greeting did not offer them
    events = []
    d = M.MySQLDecoder(ConnectionInfo(proxy="mysql:0", protocol="mysql", client="t"), events.append)
    d.feed_server(greeting())
    response = handshake_response(caps=CLIENT_CAPS | M.CLIENT_DEPRECATE_EOF | M.CLIENT_QUERY_ATTRIBUTES)
    forwarded = d.feed_client(response[:10])[0] + d.feed_client(response[10:])[0]
    assert len(forwarded) == len(response)
    caps = int.from_bytes(forwarded[4:8], "little")
    assert not caps & (M.CLIENT_DEPRECATE_EOF | M.CLIENT_QUERY_ATTRIBUTES)
    assert caps & M.CLIENT_PROTOCOL_41
    d.feed_server(ok(seq=2))
    d.feed_client(com_query("SELECT a FROM t"))
    d.feed_server(resultset([(1,), (2,)]))
    assert [(e.sql, e.rows) for e in events] == [("SELECT a FROM t", 2)]


def test_packets_split_at_16mb_and_truncation():
    h = Harness(max_sql_bytes=1000)
    sql = b"INSERT INTO blobs VALUES ('" + b"x" * (M.MAX_PACKET + 10) + b"')"
    payload = b"\x03" + sql
    data = pkt(0, payload[:M.MAX_PACKET]) + pkt(1, payload[M.MAX_PACKET:])
    assert h.client(data) == data
    h.server(ok(seq=2, affected=1))
    [e] = h.events
    assert e.truncated
    assert e.sql.startswith("INSERT INTO blobs") and len(e.sql) <= 1100


def test_decoder_errors_never_break_forwarding(h):
    h.d.pending.append({"broken": True})      # corrupt the internal state on purpose
    data = ok(seq=1)
    assert h.server(data) == data
    assert h.d.passthrough
