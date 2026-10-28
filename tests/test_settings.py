import asyncio
import json
import socket

from aiohttp.test_utils import TestClient, TestServer

from sqlprofiler.app import Config, Profiler, read_settings
from sqlprofiler.server import build_app


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_apply_settings(tmp_path):
    ddl = tmp_path / "schema.sql"
    ddl.write_text("CREATE TABLE customers (id INT PRIMARY KEY);\n")
    settings_file = tmp_path / "settings.json"

    async def scenario():
        profiler = Profiler(Config(db_path=str(tmp_path / "p.sqlite"), settings_path=str(settings_file)))
        await profiler.start()
        client = TestClient(TestServer(build_app(profiler)))
        await client.start_server()
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        try:
            # invalid input is refused and nothing changes
            r = await (await client.put("/api/settings", json={"proxies": [{"protocol": "oracle", "listen_port": 1,
                                                                           "target_host": "x", "target_port": 1}]})).json()
            assert not r["ok"] and "unknown protocol" in r["problems"][0]
            assert not settings_file.exists()

            # one proxy + the schema
            port = free_port()
            wanted = {"proxies": [{"protocol": "mysql", "listen_port": port, "target_host": "127.0.0.1",
                                   "target_port": 3306}], "listen_host": "127.0.0.1", "schema": str(ddl), "sqlfk_path": ""}
            r = await (await client.put("/api/settings", json=wanted)).json()
            assert r["ok"], r["problems"]
            assert [p["listen"] for p in r["status"]["proxies"]] == [f"127.0.0.1:{port}"]
            assert r["status"]["schema"]["tables"] == 1
            assert read_settings(settings_file) == wanted

            # a busy port and a missing schema are reported, the rest is applied
            busy_port = busy.getsockname()[1]
            wanted["proxies"].append({"protocol": "postgres", "listen_port": busy_port, "target_host": "db", "target_port": 5432})
            wanted["schema"] = str(tmp_path / "missing.sql")
            r = await (await client.put("/api/settings", json=wanted)).json()
            assert not r["ok"]
            assert any(f":{busy_port}" in p for p in r["problems"]) and any("schema" in p for p in r["problems"])
            assert r["status"]["schema"]["tables"] == 1          # the previous schema is kept
            assert len(r["status"]["proxies"]) == 1

            # removing every proxy stops them
            wanted = {"proxies": [], "schema": ""}
            r = await (await client.put("/api/settings", json=wanted)).json()
            assert r["ok"] and r["status"]["proxies"] == [] and r["status"]["schema"] is None
            assert json.loads(settings_file.read_text())["proxies"] == []
        finally:
            busy.close()
            await client.close()
            await profiler.stop()

    asyncio.run(scenario())
