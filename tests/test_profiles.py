import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from sqlprofiler.app import Config, Profiler, read_settings
from sqlprofiler.profiles import Profiles
from sqlprofiler.server import build_app
from test_settings import free_port


def test_profiles_folder(tmp_path):
    p = Profiles(tmp_path / "profiles")
    assert p.names() == []
    assert p.current() == "Profile 1" and (tmp_path / "profiles" / "Profile 1").is_dir()
    assert p.create() == "Profile 2"
    assert p.create('  Invoicing: "v2"/old  ', {"proxies": []}) == "Invoicing v2 old"
    assert p.next_name() == "Profile 3"
    assert p.names() == ["Profile 1", "Profile 2", "Invoicing v2 old"]
    assert read_settings(p.paths("Invoicing v2 old")[1]) == {"proxies": []}
    try:
        p.create("profile 2")
        raise AssertionError("duplicate accepted")
    except ValueError:
        pass
    p.set_current("Profile 2")
    assert Profiles(tmp_path / "profiles").current() == "Profile 2"
    assert [x["queries"] for x in p.list()] == [0, 0, 0]


def test_switch_profile(tmp_path):
    ddl = tmp_path / "schema.sql"
    ddl.write_text("CREATE TABLE customers (id INT PRIMARY KEY);\n")
    profiles = Profiles(tmp_path / "profiles")
    first = profiles.current()
    db, settings_path = profiles.paths(first)
    port = free_port()
    settings_path.write_text(json.dumps({"proxies": [{"protocol": "mysql", "listen_port": port,
                                                      "target_host": "127.0.0.1", "target_port": 3306}],
                                         "schema": str(ddl)}))

    async def scenario():
        profiler = Profiler(Config(db_path=str(db), settings_path=str(settings_path)))
        profiler.profiles, profiler.profile = profiles, first
        await profiler.start()
        await profiler.apply_settings(read_settings(settings_path))
        client = TestClient(TestServer(build_app(profiler)))
        await client.start_server()
        try:
            await client.post("/api/recording/start", json={"label": "in profile 1"})
            await client.post("/api/recording/stop")

            # a new profile keeps the proxies, not the schema, and starts with an empty history
            r = await (await client.post("/api/profiles", json={"name": "Second app"})).json()
            assert r["ok"] and r["profile"] == "Second app"
            status = await (await client.get("/api/status")).json()
            assert status["profile"] == "Second app" and status["schema"] is None
            assert [p["listen"] for p in status["proxies"]] == [f"127.0.0.1:{port}"]
            assert (await (await client.get("/api/sessions")).json())["sessions"] == []
            assert read_settings(profiles.paths("Second app")[1])["schema"] == ""

            # back to the first one: its sessions and its schema
            r = await (await client.post("/api/profiles/current", json={"name": first})).json()
            assert r["ok"] and r["problems"] == []
            status = await (await client.get("/api/status")).json()
            assert status["schema"]["tables"] == 1
            [s] = (await (await client.get("/api/sessions")).json())["sessions"]
            assert s["label"] == "in profile 1"

            listing = await (await client.get("/api/profiles")).json()
            assert listing["current"] == first and listing["next_name"] == "Profile 2"
            assert [p["sessions"] for p in listing["profiles"]] == [1, 0]
            assert (await client.post("/api/profiles/current", json={"name": "nope"})).status == 404
            assert (await client.post("/api/profiles", json={"name": "second APP"})).status == 400
        finally:
            await client.close()
            await profiler.stop()

    asyncio.run(scenario())


def test_relative_profiles_folder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from sqlprofiler.storage import Storage
    p = Profiles("profiles")
    Storage(p.paths(p.current())[0]).close()         # a history exists: list() opens it
    assert [(x["name"], x["sessions"]) for x in p.list()] == [("Profile 1", 0)]
