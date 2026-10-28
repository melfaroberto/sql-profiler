"""The Interceptor: a TCP proxy that sits between the target application and the real database."""
import asyncio
import logging
import time

from .events import ConnectionInfo
from .protocols import DECODERS

log = logging.getLogger("sqlprofiler.proxy")

CHUNK = 256 * 1024


class DatabaseProxy:
    """Listens on listen_host:listen_port, forwards every connection to target_host:target_port and
    hands the decoded statements to on_query(CapturedQuery)."""

    def __init__(self, protocol, listen_port, target_host, target_port, on_query,
                 listen_host="127.0.0.1", on_connection=None, max_sql_bytes=1 << 20):
        if protocol not in DECODERS:
            raise ValueError(f"unsupported protocol {protocol!r} (use one of {', '.join(DECODERS)})")
        self.protocol = protocol
        self.listen_host, self.listen_port = listen_host, listen_port
        self.target_host, self.target_port = target_host, target_port
        self.on_query = on_query
        self.on_connection = on_connection or (lambda conn, event: None)
        self.max_sql_bytes = max_sql_bytes
        self.connections = {}
        self.server = None
        self.name = f"{protocol}:{listen_port}"

    @property
    def dialect(self):
        return DECODERS[self.protocol].dialect

    def describe(self):
        return {"name": self.name, "protocol": self.protocol, "dialect": self.dialect,
                "listen": f"{self.listen_host}:{self.listen_port}",
                "target": f"{self.target_host}:{self.target_port}",
                "connections": [c.as_dict() for c in self.connections.values()]}

    async def start(self):
        self.server = await asyncio.start_server(self._handle, self.listen_host, self.listen_port)
        if self.listen_port == 0:
            self.listen_port = self.server.sockets[0].getsockname()[1]
            self.name = f"{self.protocol}:{self.listen_port}"
        log.info("%s: listening on %s:%s -> %s:%s", self.name, self.listen_host, self.listen_port,
                 self.target_host, self.target_port)

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, creader, cwriter):
        peer = cwriter.get_extra_info("peername") or ("?", 0)
        conn = ConnectionInfo(proxy=self.name, protocol=self.protocol, client=f"{peer[0]}:{peer[1]}")
        try:
            sreader, swriter = await asyncio.open_connection(self.target_host, self.target_port)
        except OSError as e:
            log.error("%s: cannot reach the database at %s:%s: %s", self.name, self.target_host, self.target_port, e)
            cwriter.close()
            return
        decoder = DECODERS[self.protocol](conn, self.on_query, self.max_sql_bytes)
        self.connections[conn.id] = conn
        self.on_connection(conn, "open")

        async def client_to_server():
            while data := await creader.read(CHUNK):
                to_server, to_client = decoder.feed_client(data)
                if to_client:
                    cwriter.write(to_client)
                    await cwriter.drain()
                if to_server:
                    swriter.write(to_server)
                    await swriter.drain()

        async def server_to_client():
            while data := await sreader.read(CHUNK):
                out = decoder.feed_server(data)
                if out:
                    cwriter.write(out)
                    await cwriter.drain()

        tasks = [asyncio.ensure_future(client_to_server()), asyncio.ensure_future(server_to_client())]
        try:
            # when either side closes, close the other one too
            done, rest = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in rest:
                t.cancel()
            for t in done:
                if t.exception() and not isinstance(t.exception(), ConnectionError):
                    log.warning("connection %s: %r", conn.id, t.exception())
        finally:
            for w in (cwriter, swriter):
                try:
                    w.close()
                except Exception:
                    pass
            self.connections.pop(conn.id, None)
            conn.closed_at = time.time()
            self.on_connection(conn, "close")
