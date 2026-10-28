"""The Real-Time Dashboard backend: REST API, WebSocket push and the static web UI (web/)."""
import asyncio
import json
import logging
import re
from pathlib import Path

from aiohttp import WSMsgType, web

from . import exporter

log = logging.getLogger("sqlprofiler.server")
WEB = Path(__file__).parent / "web"


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _json(obj, status=200):
    return web.json_response(obj, status=status, dumps=_dumps)


class WebSocketHub:
    """Pushes pipeline events to every connected browser, in order, without blocking the pipeline."""

    def __init__(self):
        self.clients = {}               # ws -> asyncio.Queue

    def __call__(self, message):
        for q in self.clients.values():
            if q.qsize() < 10_000:      # a stuck browser must not eat all the memory
                q.put_nowait(message)

    async def handle(self, request):
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        queue = asyncio.Queue()
        self.clients[ws] = queue

        async def sender():
            while True:
                message = await queue.get()
                await ws.send_str(_dumps(message))

        task = asyncio.ensure_future(sender())
        try:
            async for msg in ws:        # the browser does not send anything; this just waits for the close
                if msg.type == WSMsgType.ERROR:
                    break
        finally:
            task.cancel()
            self.clients.pop(ws, None)
        return ws


def build_app(profiler):
    app = web.Application(client_max_size=4 * 1024 * 1024)

    async def no_cache(request, response):
        if request.path.startswith("/static/"):      # always serve the current UI after an update
            response.headers["Cache-Control"] = "no-cache"
    app.on_response_prepare.append(no_cache)
    hub = WebSocketHub()
    profiler.listeners.append(hub)
    routes = web.RouteTableDef()

    @routes.get("/")
    async def index(request):
        return web.FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})

    @routes.get("/ws")
    async def ws(request):
        return await hub.handle(request)

    @routes.get("/api/status")
    async def status(request):
        return _json(profiler.status())

    # ------------------------------------------------------------------ queries

    @routes.get("/api/queries")
    async def queries(request):
        q = request.query
        rows = profiler.storage.list_queries(
            session=q.get("session") or None, after_id=q.get("after_id"), before_id=q.get("before_id"),
            limit=min(int(q.get("limit", 1000)), 20000), search=q.get("q") or None,
            query_type=q.get("type") or None, table=q.get("table") or None, hide_noise=q.get("noise") == "0",
            fingerprint=q.get("fingerprint") or None)
        return _json({"queries": rows})

    @routes.get(r"/api/queries/{id:\d+}")
    async def query(request):
        row = profiler.storage.get_query(int(request.match_info["id"]))
        if row is None:
            return _json({"error": "not found"}, 404)
        row["er"] = profiler.er_for(row)
        return _json(row)

    @routes.delete("/api/queries")
    async def clear(request):
        profiler.storage.delete_queries(request.query.get("session", "none"))
        profiler.broadcast({"type": "cleared", "session": request.query.get("session", "none")})
        return _json({"ok": True})

    # ------------------------------------------------------------------ sessions

    @routes.get("/api/sessions")
    async def sessions(request):
        return _json({"sessions": profiler.storage.list_sessions(), "recording": profiler.sessions.status()})

    @routes.post("/api/recording/start")
    async def start(request):
        body = await _body(request)
        return _json(profiler.sessions.start_recording(body.get("label", "")))

    @routes.post("/api/recording/stop")
    async def stop(request):
        return _json(profiler.sessions.stop_recording() or {})

    @routes.post("/api/recording/sub/start")
    async def sub_start(request):
        body = await _body(request)
        try:
            return _json(profiler.sessions.start_sub(body.get("label", ""), body.get("parent_id")))
        except ValueError as e:
            return _json({"error": str(e)}, 400)

    @routes.post("/api/recording/sub/stop")
    async def sub_stop(request):
        return _json(profiler.sessions.stop_sub() or {})

    @routes.post("/api/sessions")
    async def group(request):
        body = await _body(request)
        try:
            session = profiler.sessions.group(body.get("label", ""), body.get("query_ids") or [], body.get("parent_id"))
        except ValueError as e:
            return _json({"error": str(e)}, 400)
        if session is None:
            return _json({"error": "no queries selected"}, 400)
        return _json(session)

    @routes.patch(r"/api/sessions/{id:\d+}")
    async def update(request):
        body = await _body(request)
        session = profiler.storage.update_session(int(request.match_info["id"]), body.get("label"), body.get("notes"))
        profiler.broadcast({"type": "session", "event": "updated", "session": session})
        return _json(session)

    @routes.delete(r"/api/sessions/{id:\d+}")
    async def delete(request):
        sid = int(request.match_info["id"])
        cur = profiler.sessions.status()
        if cur.get("recording") and sid in [s["id"] for s in cur["path"]]:
            profiler.sessions.stop_recording()          # it is, or contains, what is being recorded
        profiler.storage.delete_session(sid, keep_queries=request.query.get("queries") != "delete")
        profiler.broadcast({"type": "session", "event": "deleted", "session": {"id": sid}})
        return _json({"ok": True})

    @routes.delete("/api/sessions")
    async def delete_all(request):
        profiler.sessions.stop_recording()
        profiler.storage.delete_all_sessions(keep_queries=request.query.get("queries") != "delete")
        profiler.broadcast({"type": "session", "event": "cleared", "session": None})
        return _json({"ok": True})

    # ------------------------------------------------------------------ profiles

    @routes.get("/api/profiles")
    async def profiles(request):
        if not profiler.profiles:
            return _json({"enabled": False, "profiles": []})
        p = profiler.profiles
        return _json({"enabled": True, "current": profiler.profile, "root": str(p.root),
                      "next_name": p.next_name(), "profiles": p.list()})

    @routes.post("/api/profiles")
    async def profile_create(request):
        """{name?}: a new profile, which starts with the current proxies (not the schema), and switches to it."""
        if not profiler.profiles:
            return _json({"error": "the profiler was started without --profiles"}, 400)
        body = await _body(request)
        settings = {**profiler.settings(), "schema": "", "sqlfk_path": ""}
        try:
            name = profiler.profiles.create(body.get("name"), settings)
        except (ValueError, OSError) as e:
            return _json({"error": str(e)}, 400)
        problems = await profiler.switch_profile(name)
        return _json({"ok": True, "profile": name, "problems": problems})

    @routes.post("/api/profiles/current")
    async def profile_switch(request):
        if not profiler.profiles:
            return _json({"error": "the profiler was started without --profiles"}, 400)
        name = (await _body(request)).get("name")
        if name not in profiler.profiles.names():
            return _json({"error": f"profile {name!r} not found"}, 404)
        problems = await profiler.switch_profile(name) if name != profiler.profile else []
        return _json({"ok": True, "profile": name, "problems": problems})

    # ------------------------------------------------------------------ tables

    @routes.get("/api/tables")
    async def tables(request):
        """?session=ID|none (default: the whole profile). Used tables, and the schema tables nobody used."""
        session = request.query.get("session") or None
        return _json(await asyncio.get_running_loop().run_in_executor(
            None, exporter.table_report, profiler.storage, profiler.schema, session))

    @routes.get("/api/tables/export")
    async def tables_export(request):
        """?session=ID (default: the whole profile, session by session) &format=md|csv"""
        fmt = request.query.get("format", "md")
        session = request.query.get("session") or None
        if session and not profiler.storage.get_session(int(session)):
            return _json({"error": f"session {session} not found"}, 404)
        fn = exporter.tables_csv if fmt == "csv" else exporter.tables_markdown
        text = await asyncio.get_running_loop().run_in_executor(
            None, fn, profiler.storage, profiler.schema, session, profiler.profile)
        name = f"tables-session-{session}" if session else "tables-" + _safe_name(profiler.profile or "profile")
        ctype = "text/csv" if fmt == "csv" else "text/markdown"
        return web.Response(text=text, content_type=ctype, charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.{"csv" if fmt == "csv" else "md"}"'})

    @routes.get("/api/er")
    async def er(request):
        """?session=ID (default: the whole profile): ER diagram of the tables used and their links."""
        session = request.query.get("session") or None
        return _json(await asyncio.get_running_loop().run_in_executor(
            None, exporter.er_diagram, profiler.storage, profiler.analyzer(profiler.dialect()), session))

    @routes.get("/api/er/export")
    async def er_export(request):
        """?session=ID (default: the whole profile): standalone HTML page with the diagram and the tables."""
        session = request.query.get("session") or None
        if session and not profiler.storage.get_session(int(session)):
            return _json({"error": f"session {session} not found"}, 404)
        text = await asyncio.get_running_loop().run_in_executor(
            None, exporter.er_html, profiler.storage, profiler.analyzer(profiler.dialect()), profiler.schema,
            session, profiler.profile)
        name = f"diagram-session-{session}" if session else "diagram-" + _safe_name(profiler.profile or "profile")
        return web.Response(text=text, content_type="text/html", charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.html"'})

    # ------------------------------------------------------------------ export

    @routes.get("/api/export")
    async def export(request):
        """?sessions=1,2,3 (default: all) &format=json|md &noise=1 to include driver noise."""
        fmt = request.query.get("format", "md")
        requested = [int(x) for x in re.findall(r"\d+", request.query.get("sessions", ""))]
        ids = exporter.ordered_sessions(profiler.storage, requested or None)
        if requested and not ids:
            return _json({"error": f"session {requested[0]} not found"}, 404)
        name = f"session-{requested[0]}" if len(requested) == 1 else "sessions"
        noise = request.query.get("noise") == "1"
        try:
            if fmt == "json":
                text, ctype, ext = exporter.export_json(profiler.storage, ids, noise), "application/json", "json"
            else:
                text, ctype, ext = exporter.export_markdown(profiler.storage, ids, noise), "text/markdown", "md"
        except KeyError as e:
            return _json({"error": f"session {e} not found"}, 404)
        return web.Response(text=text, content_type=ctype, charset="utf-8",
                            headers={"Content-Disposition": f'attachment; filename="{name}.{ext}"'})

    # ------------------------------------------------------------------ settings

    @routes.get("/api/settings")
    async def get_settings(request):
        return _json({"settings": profiler.settings(), "problems": profiler.problems,
                      "saved_to": profiler.config.settings_path or None})

    @routes.put("/api/settings")
    async def put_settings(request):
        errors = await profiler.apply_settings(await _body(request))
        return _json({"ok": not errors, "problems": errors, "status": profiler.status()})

    # ------------------------------------------------------------------ schema

    @routes.get("/api/schema")
    async def schema(request):
        s = profiler.schema
        return _json({"summary": s.summary(), "tables": sorted(t["name"] for t in s.tables.values())})

    routes.static("/static", WEB)
    app.add_routes(routes)
    return app


def _safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-") or "profile"


async def _body(request):
    try:
        return await request.json()
    except (ValueError, json.JSONDecodeError):
        return {}


async def start_server(profiler, host, port):
    runner = web.AppRunner(build_app(profiler), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return runner
