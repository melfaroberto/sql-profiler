import struct

import pytest

from sqlprofiler.events import ConnectionInfo
from sqlprofiler.protocols.postgres import PostgresDecoder
from wire import (pg_bind, pg_complete, pg_datarow, pg_error, pg_execute, pg_msg, pg_parse, pg_query, pg_ready,
                  pg_ssl_request, pg_startup)


class Harness:
    def __init__(self, chunk=None):
        self.events = []
        self.conn = ConnectionInfo(proxy="postgres:0", protocol="postgres", client="t")
        self.d = PostgresDecoder(self.conn, self.events.append)
        self.chunk = chunk

    def _split(self, data):
        if not self.chunk:
            return [data]
        return [data[i:i + self.chunk] for i in range(0, len(data), self.chunk)]

    def client(self, data):
        to_server, to_client = b"", b""
        for part in self._split(data):
            s, c = self.d.feed_client(part)
            to_server += s
            to_client += c
        return to_server, to_client

    def server(self, data):
        return b"".join(self.d.feed_server(p) for p in self._split(data))

    def login(self):
        self.client(pg_startup())
        self.server(pg_msg("R", struct.pack("!I", 0)) + pg_msg("S", b"server_version\x0016\x00") + pg_ready())


@pytest.fixture(params=[None, 1, 5], ids=["whole", "byte-by-byte", "chunks-of-5"])
def h(request):
    hh = Harness(request.param)
    hh.login()
    return hh


def test_ssl_request_is_refused_by_the_proxy():
    h = Harness()
    to_server, to_client = h.client(pg_ssl_request())
    assert to_server == b"" and to_client == b"N"
    to_server, to_client = h.client(pg_startup("bob", "crm"))
    assert to_server == pg_startup("bob", "crm") and to_client == b""
    assert h.conn.user == "bob" and h.conn.database == "crm"


def test_ssl_request_and_startup_in_one_chunk():
    h = Harness()
    to_server, to_client = h.client(pg_ssl_request() + pg_startup())
    assert to_client == b"N" and to_server == pg_startup()


def test_simple_query(h):
    h.client(pg_query("SELECT * FROM customers"))
    h.server(pg_msg("T", b"\x00\x00") + pg_datarow(b"1") + pg_datarow(b"2") + pg_complete("SELECT 2") + pg_ready())
    [e] = h.events
    assert e.sql == "SELECT * FROM customers" and e.kind == "query" and e.rows == 2 and e.error is None


def test_simple_query_with_several_statements_and_error(h):
    h.client(pg_query("INSERT INTO a VALUES (1); SELECT * FROM nope"))
    h.server(pg_complete("INSERT 0 1") + pg_error() + pg_ready())
    [e] = h.events
    assert e.affected == 1
    assert e.error == '42P01: relation "nope" does not exist'


def test_extended_protocol_with_parameters(h):
    h.client(pg_parse("UPDATE orders SET note = $1 WHERE id = $2", oids=(25, 23))
             + pg_bind([b"paid", struct.pack("!i", 42)], formats=(0, 1)) + pg_execute() + pg_msg("S"))
    h.server(pg_msg("1") + pg_msg("2") + pg_complete("UPDATE 1") + pg_ready())
    [e] = h.events
    assert e.kind == "execute"
    assert e.sql == "UPDATE orders SET note = $1 WHERE id = $2"
    assert e.params == ["paid", 42]
    assert e.affected == 1


def test_extended_protocol_text_numeric_and_null(h):
    h.client(pg_parse("SELECT $1, $2", name="s1", oids=(20, 0)) + pg_bind([b"7", None], name="s1")
             + pg_execute() + pg_msg("S"))
    h.server(pg_msg("1") + pg_msg("2") + pg_datarow(b"7", b"") + pg_complete("SELECT 1") + pg_ready())
    [e] = h.events
    assert e.params == [7, None] and e.rows == 1


def test_pipelined_executes_and_error_skips_until_sync(h):
    batch = b""
    for i in range(3):
        batch += pg_parse(f"SELECT {i}") + pg_bind([]) + pg_execute()
    h.client(batch + pg_msg("S") + pg_parse("SELECT 9") + pg_bind([]) + pg_execute() + pg_msg("S"))
    h.server(pg_msg("1") + pg_msg("2") + pg_complete("SELECT 1")
             + pg_error("division by zero", "22012") + pg_ready()
             + pg_msg("1") + pg_msg("2") + pg_complete("SELECT 1") + pg_ready())
    assert [(e.sql, e.error) for e in h.events] == [
        ("SELECT 0", None), ("SELECT 1", "22012: division by zero"), ("SELECT 9", None)]


def test_cancel_request_is_forwarded():
    h = Harness()
    msg = struct.pack("!IIII", 16, 80877102, 1, 2)
    assert h.client(msg) == (msg, b"")
    assert h.d.passthrough


def test_parameter_types_learned_from_parameter_description(h):
    # asyncpg style: Parse without types, Describe, then binary Bind
    h.client(pg_parse("SELECT * FROM t WHERE id = ANY($1) AND n = $2", name="__a1") + pg_msg("D", b"S__a1\0") + pg_msg("S"))
    h.server(pg_msg("1") + pg_msg("t", struct.pack("!hII", 2, 1007, 23)) + pg_msg("n") + pg_ready())
    array = struct.pack("!iiIii", 1, 0, 23, 2, 1) + struct.pack("!ii", 4, 5) + struct.pack("!ii", 4, 6)
    h.client(pg_bind([array, struct.pack("!i", 9)], formats=(1,), name="__a1") + pg_execute() + pg_msg("S"))
    h.server(pg_msg("2") + pg_complete("SELECT 0") + pg_ready())
    [e] = h.events
    assert e.params == [[5, 6], 9]
