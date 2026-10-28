"""Command line.

    python -m sqlprofiler run --proxy mysql:3307:127.0.0.1:3306 --schema Dump20261015-1
    python -m sqlprofiler run --profiles profiles      # one folder per application, chosen in the dashboard
    python -m sqlprofiler export --session 3 --format md -o login.md
    python -m sqlprofiler sessions
"""
import argparse
import asyncio
import logging
import signal
import sys
import webbrowser

from .app import Config, Profiler, ProxySpec, read_settings
from .protocols import DECODERS, DEFAULT_PORTS

log = logging.getLogger("sqlprofiler")


def parse_proxy(text):
    """PROTOCOL:LISTEN_PORT:TARGET_HOST[:TARGET_PORT], e.g. mysql:3307:127.0.0.1:3306"""
    parts = text.split(":")
    if len(parts) not in (3, 4) or parts[0].lower() not in DECODERS:
        raise argparse.ArgumentTypeError(
            f"{text!r}: expected PROTOCOL:LISTEN_PORT:TARGET_HOST[:TARGET_PORT] with PROTOCOL one of {', '.join(DECODERS)}")
    proto = parts[0].lower()
    try:
        return ProxySpec(proto, int(parts[1]), parts[2], int(parts[3]) if len(parts) == 4 else DEFAULT_PORTS[proto])
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: ports must be numbers")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m sqlprofiler", description="SQL Profiler & Interceptor")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start the proxies and the dashboard")
    run.add_argument("--proxy", action="append", type=parse_proxy, metavar="PROTO:PORT:HOST[:PORT]",
                     help="proxy to start (repeatable). Default: mysql:3307:127.0.0.1:3306, or the ones in --config")
    run.add_argument("--listen-host", default=None,
                     help="address the proxies listen on; 0.0.0.0 if the target software runs on another machine")
    run.add_argument("--config", help="JSON settings file: read at start, written when the settings are changed "
                                      "from the dashboard (proxies, schema). Problems are shown in the dashboard "
                                      "instead of stopping the program")
    run.add_argument("--profiles", metavar="FOLDER",
                     help="folder of profiles, one per application being profiled, each with its own history and "
                          "settings; they are created and switched from the dashboard. Replaces --db and --config")
    run.add_argument("--ui-host", default="127.0.0.1")
    run.add_argument("--ui-port", type=int, default=8780, help="dashboard port (0 = any free port)")
    run.add_argument("--db", default="sqlprofiler.sqlite", help="SQLite file with the captured history")
    run.add_argument("--schema", help="sql-fk-finder model (.json), a folder of .sql dumps or a .sql file")
    run.add_argument("--sqlfk-path", help="folder that contains the sqlfk package: analyse the dumps with "
                                          "sql-fk-finder (inferred keys too) instead of the built-in reader")
    run.add_argument("--dialect", default=None, help="SQL dialect of the schema files (default: from the first proxy)")
    run.add_argument("--max-sql-kb", type=int, default=1024, help="longer statements are stored truncated")
    run.add_argument("--open", action="store_true", help="open the dashboard in the browser")
    run.add_argument("--verbose", "-v", action="store_true")

    exp = sub.add_parser("export", help="export recorded sessions")
    exp.add_argument("--db", default="sqlprofiler.sqlite")
    exp.add_argument("--session", action="append", type=int, help="session id (repeatable; default: all)")
    exp.add_argument("--format", choices=["md", "json"], default="md")
    exp.add_argument("--noise", action="store_true", help="include driver statements (SET, SHOW, SELECT @@...)")
    exp.add_argument("-o", "--output", help="file to write (default: standard output)")

    ls = sub.add_parser("sessions", help="list recorded sessions")
    ls.add_argument("--db", default="sqlprofiler.sqlite")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
    if args.command == "run":
        return run_command(args)
    if args.command == "export":
        return export_command(args)
    return sessions_command(args)


def run_command(args):
    from .schema import load_schema
    from .server import start_server

    profiles = None
    if args.profiles:
        from .profiles import Profiles
        profiles = Profiles(args.profiles)
        name = profiles.current()
        profiles.set_current(name)
        db, settings_path = profiles.paths(name)
        args.db, args.config = str(db), str(settings_path)
        log.info("profile: %s (%s)", name, db.parent)

    if args.config:
        # settings file mode (desktop app): command-line options override the file, problems go to the dashboard
        saved = read_settings(args.config)
        settings = {
            "proxies": ([{"protocol": p.protocol, "listen_port": p.listen_port, "target_host": p.target_host,
                          "target_port": p.target_port} for p in args.proxy] if args.proxy else saved.get("proxies", [])),
            "listen_host": args.listen_host or saved.get("listen_host", "127.0.0.1"),
            "schema": args.schema or saved.get("schema", ""),
            "sqlfk_path": args.sqlfk_path or saved.get("sqlfk_path", ""),
        }
        config = Config(ui_host=args.ui_host, ui_port=args.ui_port, db_path=args.db,
                        max_sql_bytes=args.max_sql_kb * 1024, settings_path=args.config)
        profiler = Profiler(config)
    else:
        settings = None
        proxies = args.proxy or [ProxySpec("mysql", 3307, "127.0.0.1", 3306)]
        dialect = args.dialect or DECODERS[proxies[0].protocol].dialect
        try:
            schema = load_schema(args.schema, dialect, args.sqlfk_path)
        except Exception as e:
            log.error("cannot load the schema: %s", e)
            return 2
        config = Config(proxies=proxies, listen_host=args.listen_host or "127.0.0.1", ui_host=args.ui_host,
                        ui_port=args.ui_port, db_path=args.db, max_sql_bytes=args.max_sql_kb * 1024,
                        schema_source=args.schema or "", sqlfk_path=args.sqlfk_path or "")
        profiler = Profiler(config, schema)
    if profiles:
        profiler.profiles, profiler.profile = profiles, name

    async def serve():
        try:
            await profiler.start()
            runner = await start_server(profiler, args.ui_host, args.ui_port)
        except OSError as e:
            log.error("cannot listen: %s", e)
            await profiler.stop()
            return 2
        port = runner.addresses[0][1]
        url = f"http://{args.ui_host}:{port}/"
        log.info("dashboard: %s", url)
        print(f"SQLPROFILER_PORT={port}", flush=True)       # read by the desktop app
        if settings is not None:
            for problem in await profiler.apply_settings(settings):
                log.warning("%s", problem)
        for p in profiler.proxies:
            log.info("point the target software to %s:%s (instead of %s:%s)",
                     profiler.config.listen_host, p.listen_port, p.target_host, p.target_port)
        if not profiler.proxies:
            log.info("no proxy configured: add one in the dashboard (Settings)")
        if args.open:
            webbrowser.open(url)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):     # Windows
                pass
        try:
            await stop.wait()
        finally:
            log.info("stopping")
            await runner.cleanup()
            await profiler.stop()
        return 0

    try:
        return asyncio.run(serve())
    except KeyboardInterrupt:
        return 0


def export_command(args):
    from . import exporter
    from .storage import Storage

    storage = Storage(args.db)
    ids = exporter.ordered_sessions(storage, args.session or None)
    if not ids:
        print("no sessions recorded", file=sys.stderr)
        return 1
    try:
        fn = exporter.export_json if args.format == "json" else exporter.export_markdown
        text = fn(storage, ids, args.noise)
    except KeyError as e:
        print(f"session {e} not found", file=sys.stderr)
        return 1
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    return 0


def sessions_command(args):
    import time
    from .storage import Storage

    for s in Storage(args.db).list_sessions():
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s["started_at"]))
        print(f"{s['id']:>5}  {when}  {s['queries']:>6} queries  {s['label']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
