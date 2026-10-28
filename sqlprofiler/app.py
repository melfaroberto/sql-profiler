"""Wires the pipeline together: proxies -> analyzer -> session tagging -> SQLite -> dashboard."""
import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import __version__
from .analyzer import QueryAnalyzer, json_safe
from .protocols import DECODERS
from .proxy import DatabaseProxy
from .schema import SchemaCatalog, load_schema
from .sessions import SessionManager
from .storage import Storage

log = logging.getLogger("sqlprofiler")


@dataclass
class ProxySpec:
    protocol: str
    listen_port: int
    target_host: str
    target_port: int


@dataclass
class Config:
    proxies: list = field(default_factory=list)
    listen_host: str = "127.0.0.1"
    ui_host: str = "127.0.0.1"
    ui_port: int = 8780
    db_path: str = "sqlprofiler.sqlite"
    max_sql_bytes: int = 1 << 20
    schema_source: str = ""         # what the schema was loaded from (file or folder)
    sqlfk_path: str = ""
    settings_path: str = ""         # JSON file where the settings changed from the dashboard are saved


def read_settings(path):
    """Settings saved by the dashboard: {"proxies": [...], "listen_host", "schema", "sqlfk_path"}"""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as e:
        log.error("ignoring unreadable settings file %s: %s", path, e)
        return {}


def parse_settings(data):
    """Validates settings sent by the dashboard -> (proxy specs, listen host, schema, sqlfk path, errors)."""
    errors, specs = [], []
    for i, p in enumerate(data.get("proxies") or [], 1):
        try:
            proto = str(p["protocol"]).lower()
            if proto not in DECODERS:
                raise ValueError(f"unknown protocol {proto!r}")
            spec = ProxySpec(proto, int(p["listen_port"]), str(p["target_host"]).strip(), int(p["target_port"]))
            if not spec.target_host or not (0 < spec.listen_port < 65536) or not (0 < spec.target_port < 65536):
                raise ValueError("host and ports are required")
            specs.append(spec)
        except (KeyError, TypeError, ValueError) as e:
            errors.append(f"proxy {i}: {e}")
    ports = [s.listen_port for s in specs]
    if len(ports) != len(set(ports)):
        errors.append("two proxies cannot listen on the same port")
    listen_host = str(data.get("listen_host") or "127.0.0.1")
    return specs, listen_host, str(data.get("schema") or "").strip(), str(data.get("sqlfk_path") or "").strip(), errors


class Profiler:
    def __init__(self, config, schema=None):
        self.config = config
        self.schema = schema or SchemaCatalog()
        self.storage = Storage(config.db_path)
        self.sessions = SessionManager(self.storage, on_change=self._session_changed)
        self.analyzers = {}
        self.proxies = []
        self.listeners = []             # callables(message dict), e.g. the WebSocket hub
        self.started_at = time.time()
        self._queue = None
        self._worker = None
        self._loop = None
        self.stats = {"captured": 0, "errors": 0}
        self.problems = []              # what went wrong applying the settings (busy port, bad schema path…)
        self.schema_loading = False
        self._settings_lock = asyncio.Lock()
        self.profiles = None            # Profiles, when started with --profiles
        self.profile = None             # name of the profile in use

    def analyzer(self, dialect):
        analyzers = self.analyzers
        if dialect not in analyzers:
            analyzers[dialect] = QueryAnalyzer(dialect, self.schema)
        return analyzers[dialect]

    def dialect(self):
        """SQL dialect of the schema: the one of the first proxy."""
        return DECODERS[self.config.proxies[0].protocol].dialect if self.config.proxies else "mysql"

    def set_schema(self, schema):
        self.schema = schema
        self.analyzers = {}             # analyses are cached per schema

    # ------------------------------------------------------------------ lifecycle

    async def start(self):
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._worker = asyncio.ensure_future(self._work())
        for spec in self.config.proxies:
            await self._start_proxy(spec)

    async def _start_proxy(self, spec):
        proxy = DatabaseProxy(spec.protocol, spec.listen_port, spec.target_host, spec.target_port,
                              on_query=self._on_query, listen_host=self.config.listen_host,
                              on_connection=self._on_connection, max_sql_bytes=self.config.max_sql_bytes)
        await proxy.start()
        self.proxies.append(proxy)

    # ------------------------------------------------------------------ settings (dashboard / desktop app)

    def settings(self):
        return {"proxies": [asdict(s) for s in self.config.proxies], "listen_host": self.config.listen_host,
                "schema": self.config.schema_source, "sqlfk_path": self.config.sqlfk_path}

    async def apply_settings(self, data):
        """Starts/stops proxies and reloads the schema to match data, saves the settings, returns the problems.
        Connections already open through a removed proxy keep working until they close."""
        async with self._settings_lock:
            return await self._apply_settings(data)

    async def _apply_settings(self, data, save=True):
        specs, listen_host, schema, sqlfk_path, errors = parse_settings(data)
        if errors:
            return errors

        key = lambda s: (s.protocol, s.listen_port, s.target_host, s.target_port)
        restart_all = listen_host != self.config.listen_host
        self.config.listen_host = listen_host
        wanted = {key(s) for s in specs}
        for p in list(self.proxies):
            if restart_all or (p.protocol, p.listen_port, p.target_host, p.target_port) not in wanted:
                await p.stop()
                self.proxies.remove(p)
        running = {(p.protocol, p.listen_port, p.target_host, p.target_port) for p in self.proxies}
        for spec in specs:
            if key(spec) not in running:
                try:
                    await self._start_proxy(spec)
                except OSError as e:
                    errors.append(f"cannot listen on {listen_host}:{spec.listen_port}: {e.strerror or e}")
        self.config.proxies = specs

        if (schema, sqlfk_path) != (self.config.schema_source, self.config.sqlfk_path):
            if not schema:
                self.set_schema(SchemaCatalog())
                self.config.schema_source, self.config.sqlfk_path = "", sqlfk_path
            else:
                dialect = DECODERS[specs[0].protocol].dialect if specs else "mysql"
                self.schema_loading = True
                self.broadcast({"type": "settings"})
                try:
                    catalog = await asyncio.get_running_loop().run_in_executor(
                        None, load_schema, schema, dialect, sqlfk_path or None)
                    self.set_schema(catalog)
                    self.config.schema_source, self.config.sqlfk_path = schema, sqlfk_path
                except Exception as e:
                    log.error("cannot load the schema from %s: %s", schema, e)
                    errors.append(f"schema not loaded: {e}")
                finally:
                    self.schema_loading = False

        self.problems = errors
        if save:
            self.save_settings()
        self.broadcast({"type": "settings"})
        return errors

    async def switch_profile(self, name):
        """Moves to another profile: its history, and its settings (proxies, schema). Returns the problems."""
        async with self._settings_lock:
            db, settings_path = self.profiles.paths(name)
            self.sessions.stop_recording()
            if self._queue is not None:
                await self._queue.join()        # what was captured so far goes to the old history
            self.storage.close()
            self.config.db_path, self.config.settings_path = str(db), str(settings_path)
            self.storage = Storage(db)
            self.sessions = SessionManager(self.storage, on_change=self._session_changed)
            self.profile = name
            self.profiles.set_current(name)
            data = read_settings(settings_path)
            if (data.get("schema") or "", data.get("sqlfk_path") or "") != (self.config.schema_source, self.config.sqlfk_path):
                # never keep the previous profile's schema if this one's cannot be loaded
                self.set_schema(SchemaCatalog())
                self.config.schema_source = self.config.sqlfk_path = ""
            # the profile's file is left as it is, even if something in it cannot be applied now
            errors = await self._apply_settings(data, save=False)
        self.broadcast({"type": "profile", "profile": name})
        return errors

    def save_settings(self):
        if not self.config.settings_path:
            return
        path = Path(self.config.settings_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.settings(), indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def stop(self):
        for p in self.proxies:
            await p.stop()
        if self._queue is not None:
            await self._queue.join()
        if self._worker:
            self._worker.cancel()
        self.sessions.stop_recording()
        self.storage.close()

    async def drain(self):
        """Waits until every captured query has been analysed and stored (used by tests)."""
        await self._queue.join()

    # ------------------------------------------------------------------ pipeline

    def _on_query(self, captured):
        """Called by the decoders, in the event loop: queue the query, never block the proxy."""
        captured.conn.queries += 1
        # the session is decided now: the recording may be stopped before the worker gets to this query
        self._queue.put_nowait((captured, self.sessions.session_for(captured.started_at)))

    async def _work(self):
        while True:
            captured, session_id = await self._queue.get()
            try:
                # sqlglot and SQLite run in a thread so that a long statement never stalls the traffic
                row = await self._loop.run_in_executor(None, self.process, captured, session_id)
                self.broadcast({"type": "query", "query": row})
            except Exception:
                log.exception("cannot process a captured query")
            finally:
                self._queue.task_done()

    def process(self, captured, session_id=None):
        """QueryAnalyzer.process of the architecture: analyse, tag with the session, store. Returns the row."""
        conn = captured.conn
        dialect = DECODERS[conn.protocol].dialect
        sql = captured.sql
        stored_sql = sql if len(sql) <= self.config.max_sql_bytes else sql[:self.config.max_sql_bytes]
        a = self.analyzer(dialect).analyze(stored_sql)
        row = {
            "ts": captured.started_at, "session_id": session_id,
            "conn_id": conn.id, "proxy": conn.proxy, "protocol": conn.protocol, "client": conn.client,
            "db_user": conn.user, "database": conn.database, "kind": captured.kind, "sql": stored_sql,
            "params": [json_safe(v) for v in captured.params] if captured.params is not None else None,
            "truncated": int(captured.truncated or len(sql) > self.config.max_sql_bytes),
            "duration_ms": round(captured.duration_ms, 3), "rows": captured.rows, "affected": captured.affected,
            "error": captured.error, "query_type": a["query_type"], "tables": a["tables"], "columns": a["columns"],
            "joins": a["joins"], "fingerprint": a["fingerprint"], "formatted": a["formatted"],
            "parse_error": a["parse_error"], "noise": int(a["noise"]),
        }
        row["id"] = self.storage.insert_query(row)
        self.stats["captured"] += 1
        self.stats["errors"] += 1 if captured.error else 0
        return {k: v for k, v in row.items() if k not in ("columns", "joins", "formatted")}

    def er_for(self, query_row):
        dialect = {"postgres": "postgres"}.get(query_row.get("protocol"), "mysql")
        analysis = {"tables": query_row.get("tables") or [], "columns": query_row.get("columns") or [],
                    "joins": query_row.get("joins") or []}
        return self.analyzer(dialect).er_subgraph(analysis)

    # ------------------------------------------------------------------ events for the dashboard

    def broadcast(self, message):
        for listener in list(self.listeners):
            try:
                listener(message)
            except Exception:
                log.exception("listener failed")

    def _on_connection(self, conn, event):
        self.broadcast({"type": "connection", "event": event, "connection": conn.as_dict()})

    def _session_changed(self, session, event):
        message = {"type": "session", "event": event, "session": session}
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.broadcast, message)
        else:
            self.broadcast(message)

    def status(self):
        return {
            "version": __version__,
            "started_at": self.started_at,
            "proxies": [p.describe() for p in self.proxies],
            "recording": self.sessions.status(),
            "schema": self.schema.summary() if self.schema else None,
            "schema_loading": self.schema_loading,
            "settings": self.settings(),
            "settings_saved_to": self.config.settings_path or None,
            "problems": self.problems,
            "counts": self.storage.counts(),
            "database": self.storage.path,
            "profile": self.profile,
        }
