"""Building blocks shared by the wire-protocol decoders."""
import logging
import time

log = logging.getLogger("sqlprofiler.protocol")


class Frame:
    """A complete protocol message. Only the first `head_limit` bytes of the payload are kept,
    so that large result rows or blobs never accumulate in memory."""
    __slots__ = ("kind", "seq", "head", "length")

    def __init__(self, kind, seq, head, length):
        self.kind, self.seq, self.head, self.length = kind, seq, head, length

    @property
    def truncated(self):
        return len(self.head) < self.length


class Framer:
    """Splits a byte stream into frames. Subclasses define the header layout."""
    header_size = 4

    def __init__(self, head_limit):
        self.head_limit = head_limit
        self._buf = bytearray()
        self._cur = None            # [kind, seq, remaining, head, length]

    def parse_header(self, header):
        """-> (kind, seq, payload_length)"""
        raise NotImplementedError

    def feed(self, data):
        self._buf += data
        frames, pos, buf = [], 0, self._buf
        while True:
            if self._cur is None:
                if len(buf) - pos < self.header_size:
                    break
                kind, seq, length = self.parse_header(bytes(buf[pos:pos + self.header_size]))
                pos += self.header_size
                self._cur = [kind, seq, length, bytearray(), length]
            cur = self._cur
            take = min(cur[2], len(buf) - pos)
            room = self.head_limit - len(cur[3])
            if room > 0:
                cur[3] += buf[pos:pos + min(take, room)]
            pos += take
            cur[2] -= take
            if cur[2]:
                break
            frames.append(Frame(cur[0], cur[1], bytes(cur[3]), cur[4]))
            self._cur = None
        del buf[:pos]
        return frames


class Decoder:
    """Watches both directions of one proxied connection.

    feed_client(data) -> (bytes for the server, bytes to answer the client directly)
    feed_server(data) -> bytes for the client

    Decoding must never break the connection: if anything goes wrong the decoder logs it and
    becomes a transparent pipe for the rest of the connection."""

    def __init__(self, conn, emit, max_sql_bytes=1 << 20):
        self.conn, self.emit, self.max_sql_bytes = conn, emit, max_sql_bytes
        self.passthrough = False

    def feed_client(self, data):
        if self.passthrough:
            return data, b""
        try:
            return self._client(data)
        except Exception:
            self._give_up("client")
            return data, b""

    def feed_server(self, data):
        if self.passthrough:
            return data
        try:
            return self._server(data)
        except Exception:
            self._give_up("server")
            return data

    def _give_up(self, side):
        log.exception("connection %s: cannot decode %s traffic, forwarding it unchanged", self.conn.id, side)
        self.passthrough = True
        self.conn.note = "protocol decoding stopped (see log)"

    def _client(self, data):
        raise NotImplementedError

    def _server(self, data):
        raise NotImplementedError

    @staticmethod
    def now():
        return time.time(), time.perf_counter()

    def text(self, raw):
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1")
