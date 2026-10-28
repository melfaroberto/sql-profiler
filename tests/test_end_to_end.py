"""Target app -> proxy -> fake MySQL server, with the pipeline, the REST API and the WebSocket."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from sqlprofiler.app import Config, Profiler, ProxySpec
from sqlprofiler.schema import from_ddl
from sqlprofiler.server import build_app
from wire import com_query, greeting, handshake_response, ok, pkt, resultset


async def read_packet(reader):
    head = await reader.readexactly(4)
    return head[3], await reader.readexactly(int.from_bytes(head[:3], "little"))


async def fake_mysql(reader, writer):
    """Minimal server: SELECT -> one row, anything else -> OK with 1 affected row, unknown table -> error."""
    writer.write(greeting())
    await read_packet(reader)
    writer.write(ok(seq=2))
    while True:
        try:
            _, payload = await read_packet(reader)
        except asyncio.IncompleteReadError:
            break
        if payload[:1] == b"\x01":
            break
        sql = payload[1:].decode()
        if "nope" in sql:
            writer.write(pkt(1, b"\xff\x7a\x04#42S02Table 'nope' doesn't exist"))
        elif sql.upper().startswith("SELECT"):
            writer.write(resultset([(1, "Mario")], ncols=2))
        else:
            writer.write(ok(seq=1, affected=1))
        await writer.drain()
    writer.close()


class App:
    """What the target software does: connect through the proxy and run statements."""

    def __init__(self, port):
        self.port = port

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", self.port)
        await read_packet(self.reader)
        self.writer.write(handshake_response(user="leo", db="shop"))
        await read_packet(self.reader)

    async def query(self, sql):
        self.writer.write(com_query(sql))
        await self.writer.drain()
        _, first = await read_packet(self.reader)
        if first[:1] in (b"\x00", b"\xff"):
            return first
        while True:                                     # column defs, EOF, rows, EOF
            _, p = await read_packet(self.reader)
            if p[:1] == b"\xfe":
                break
        while True:
            _, p = await read_packet(self.reader)
            if p[:1] == b"\xfe" and len(p) < 9:
                return p

    async def close(self):
        self.writer.write(pkt(0, b"\x01"))
        self.writer.close()


def test_end_to_end(tmp_path):
    ddl = tmp_path / "schema.sql"
    ddl.write_text("CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR(50));\n"
                   "CREATE TABLE orders (id INT PRIMARY KEY, customer_id INT,\n"
                   "  FOREIGN KEY (customer_id) REFERENCES customers (id));\n")

    async def scenario():
        db = await asyncio.start_server(fake_mysql, "127.0.0.1", 0)
        db_port = db.sockets[0].getsockname()[1]
        profiler = Profiler(Config(proxies=[ProxySpec("mysql", 0, "127.0.0.1", db_port)],
                                   db_path=str(tmp_path / "p.sqlite")), from_ddl(ddl))
        await profiler.start()
        client = TestClient(TestServer(build_app(profiler)))
        await client.start_server()
        try:
            ws = await client.ws_connect("/ws")
            app = App(profiler.proxies[0].listen_port)
            await app.connect()

            await app.query("SELECT 1 FROM dual")
            r = await client.post("/api/recording/start", json={"label": "Open customer"})
            session = await r.json()
            await app.query("SELECT c.name FROM customers c JOIN orders o ON o.customer_id = c.id WHERE c.id = 1")
            await app.query("UPDATE customers SET name = 'Luigi' WHERE id = 1")
            await app.query("SELECT * FROM nope")
            await client.post("/api/recording/stop")
            await app.query("SELECT 2")
            await profiler.drain()

            status = await (await client.get("/api/status")).json()
            assert status["counts"] == {"total": 5, "unassigned": 2}
            assert status["proxies"][0]["connections"][0]["user"] == "leo"

            rows = (await (await client.get(f"/api/queries?session={session['id']}")).json())["queries"]
            assert [r["query_type"] for r in rows] == ["SELECT", "UPDATE", "SELECT"]
            assert rows[0]["rows"] == 1 and rows[1]["affected"] == 1
            assert rows[2]["error"].startswith("1146")

            detail = await (await client.get(f"/api/queries/{rows[0]['id']}")).json()
            assert {t["name"] for t in detail["er"]["tables"]} == {"customers", "orders"}
            assert len(detail["er"]["links"]) == 1 and detail["er"]["observed"] == []
            assert "JOIN orders" in detail["formatted"]

            md = await (await client.get(f"/api/export?sessions={session['id']}&format=md")).text()
            assert "## Open customer" in md and "UPDATE customers" in md and "SELECT 2" not in md

            grouped = await (await client.post("/api/sessions", json={"label": "later", "query_ids": [rows[0]["id"] - 1]})).json()
            assert grouped["queries"] == 1

            # the browser got every query, plus the session events
            kinds = []
            while True:
                try:
                    msg = await ws.receive(timeout=1)
                except asyncio.TimeoutError:
                    break
                if msg.type.name != "TEXT":
                    break
                kinds.append(json.loads(msg.data)["type"])
            assert kinds.count("query") == 5
            assert kinds.count("session") >= 3

            await client.delete("/api/sessions?queries=delete")
            status = await (await client.get("/api/status")).json()
            assert (await (await client.get("/api/sessions")).json())["sessions"] == []
            assert status["counts"] == {"total": 1, "unassigned": 1}
            await ws.close()
            await app.close()
        finally:
            await client.close()
            await profiler.stop()
            db.close()

    asyncio.run(scenario())
