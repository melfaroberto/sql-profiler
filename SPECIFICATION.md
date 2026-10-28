# SQL Profiler & Interceptor — Specification

| | |
|---|---|
| Version | 1.0.0 |
| Date | 2026-10-27 |
| Source design | `sql_profiler_architecture.txt` (High-Level Design & Implementation Guide) |
| Status | Implemented; this document describes the delivered behaviour |

---

## 1. Purpose and scope

The SQL Profiler intercepts, records and analyses, in real time, the SQL statements that a target
(legacy) application sends to its database. Its goal is to support the reverse engineering of the
application's backend logic by mapping **UI interactions** (e.g. clicking *Save Order*) to the
**database operations** they cause.

In scope:

* A transparent TCP proxy for **MySQL/MariaDB** and **PostgreSQL** that decodes the wire protocol just enough
  to extract statements, bound parameters, timings and outcomes.
* Statement analysis with `sqlglot`: formatting, statement type, tables (and how they are accessed),
  columns (and how they are used), joins, fingerprint.
* Cross-reference with the static schema produced by **sql-fk-finder** (or read from SQL dumps).
* Grouping of statements into labelled **sessions** (one session = one UI action).
* Local persistence in SQLite.
* A real-time web dashboard (WebSocket push).
* Export of sessions to Markdown and JSON.

Also delivered: a desktop application (Electron) around the dashboard (§11.2).

Out of scope (see §15): other DBMS protocols (SQL Server TDS, Oracle TNS), decrypting TLS traffic,
log-tailing capture, multi-user/remote deployment.

## 2. Glossary

| Term | Meaning |
|---|---|
| Target application | The software being reverse-engineered. It is not modified. |
| Proxy | A listener of the profiler that the target application connects to instead of the real database. |
| Capture / query | One statement observed on the wire, with its outcome. |
| Session | A labelled, time-bounded group of captures, normally one UI action. |
| Recording | The state in which new captures are assigned to the current session. |
| Noise | Statements issued by drivers/connection pools rather than application logic (`SET`, `SHOW`, `SELECT @@…`, catalog introspection). |
| Fingerprint | Hash of a statement with all literal values replaced by placeholders; identical for executions of the same statement with different values. |
| Observed join | An equality predicate `a.x = b.y` between columns of two different tables, seen in a captured statement. |
| Candidate relationship | An observed join that is not a foreign key in the loaded schema. |

## 3. Architecture

### 3.1 Approach

The design follows the *Local Database Proxy* approach recommended by the architecture document (§2):
the target application is reconfigured (connection string or DNS/hosts entry) to connect to the profiler,
which forwards the raw TCP traffic to the real database and extracts the statements in transit.

### 3.2 Components

The four modules of the architecture document map onto the code as follows:

| Architecture module | Package / file | Main classes |
|---|---|---|
| A. Interceptor (proxy/sniffer engine) | `sqlprofiler/proxy.py`, `sqlprofiler/protocols/` | `DatabaseProxy`, `MySQLDecoder`, `PostgresDecoder` |
| B. Parser & Analyzer | `sqlprofiler/analyzer.py`, `sqlprofiler/schema.py` | `QueryAnalyzer`, `SchemaCatalog` |
| C. Session & Tagging Manager | `sqlprofiler/sessions.py` | `SessionManager` |
| D. Real-time dashboard | `sqlprofiler/server.py`, `sqlprofiler/web/` | `WebSocketHub`, `build_app` |
| Storage (pipeline step 4) | `sqlprofiler/storage.py` | `Storage` |
| Export (UI spec, §6) | `sqlprofiler/exporter.py` | `export_markdown`, `export_json` |
| Wiring | `sqlprofiler/app.py`, `sqlprofiler/__main__.py` | `Profiler`, CLI |
| Desktop app | `electron/` | `main.js` (Python discovery, backend process, window), `preload.js` |

### 3.3 Data pipeline

```
[Target app] ──TCP──► [DatabaseProxy + protocol Decoder] ──TCP──► [Real database]
                                 │ CapturedQuery (sql, params, timing, rows, error)
                                 ▼
                     asyncio queue (never blocks the traffic)
                                 ▼  worker thread
        [QueryAnalyzer] ──► [SessionManager: session id] ──► [Storage: SQLite]
                                 ▼
                     [WebSocketHub] ──► browsers (live grid)
```

1. The target application executes a statement.
2. The decoder recognises the command in the client→server stream, timestamps it, and follows the
   server→client stream until the response is complete (result set end, OK or error).
3. A `CapturedQuery` is put on an asyncio queue. The session is decided **at capture time**, so a recording
   stopped while the query waits in the queue still gets it.
4. A single worker analyses (sqlglot) and stores (SQLite) the query in a thread pool, preserving order.
5. The stored row is broadcast to every connected browser.

### 3.4 Concurrency model

* One asyncio event loop runs the proxies, the web server and the WebSocket hub.
* Per proxied connection, two coroutines pump bytes (client→server, server→client) and feed the decoder.
* Analysis and SQLite writes run in a worker thread so a long statement never stalls the network.
* `Storage` serialises access with a lock; SQLite runs in WAL mode.

## 4. Interceptor (module A)

### 4.1 General requirements

| ID | Requirement |
|---|---|
| A-1 | Each proxy listens on `listen_host:listen_port` and opens one connection to `target_host:target_port` per accepted client. |
| A-2 | All bytes are forwarded unchanged, except the capability/encryption negotiation described in §4.2–4.3. |
| A-3 | Decoding failures must never break a connection: the decoder logs the error and becomes a transparent pipe for that connection (`passthrough`). |
| A-4 | Large payloads are not buffered: frames are scanned keeping only the first bytes (64 KB for server messages, `max_sql_bytes` + 64 for client messages). |
| A-5 | Duration = time between the command leaving the client and the last packet of its response arriving from the server, measured at the proxy (`perf_counter`). |
| A-6 | Each connection records: client address, user, database, query count, and a note (server version, TLS problem, decoding stopped). |
| A-7 | When either side closes, the other side is closed. |
| A-8 | Several proxies (also of different protocols) can run at the same time. |

### 4.2 MySQL / MariaDB

**Handshake.** The server greeting (HandshakeV10) is held until complete and these capability bits are
cleared before forwarding it: `CLIENT_SSL`, `CLIENT_COMPRESS`, `CLIENT_ZSTD_COMPRESSION`,
`CLIENT_DEPRECATE_EOF`, `CLIENT_QUERY_ATTRIBUTES`, `CLIENT_OPTIONAL_RESULTSET_METADATA`.
The same bits (except `CLIENT_SSL`) are also cleared in the client's HandshakeResponse, because some
clients (libmysqlclient 8/9) announce them even when not offered, and the real server would then use a
framing the client does not expect. The packet lengths do not change; the authentication scramble is not
affected. The user name and initial database are read from the HandshakeResponse.

If the client still sends an SSLRequest (TLS required by its configuration), the connection is marked
`encrypted` and forwarded without decoding.

**Commands.**

| Command | Captured as | Notes |
|---|---|---|
| `COM_QUERY` | `kind = query` | Statement text (UTF-8, Latin-1 fallback). |
| `COM_STMT_PREPARE` | not logged if it succeeds; `kind = prepare` if it fails | Statement id → SQL and parameter count are remembered. |
| `COM_STMT_EXECUTE` | `kind = execute` | SQL of the statement + decoded binary parameters (§4.2.1). |
| `COM_INIT_DB` | `kind = use`, SQL ``USE `db` `` | Updates the connection's database. |
| `COM_STMT_SEND_LONG_DATA` | – | The parameter is shown as `<long data>`. |
| `COM_STMT_CLOSE`, `COM_QUIT`, `COM_PING`, `COM_STMT_RESET`, others | not logged | Responses are consumed to keep the stream in sync. |
| `COM_CHANGE_USER` | – | Returns to the authentication phase. |
| `COM_BINLOG_DUMP(_GTID)` | – | Connection switches to passthrough. |

**Responses.** OK (affected rows, more-results flag), ERR (`code (sqlstate): message`), result sets
(column count, column definitions, EOF, rows, EOF/OK; rows are counted), multiple result sets
(`SERVER_MORE_RESULTS_EXISTS`, e.g. stored procedures) merged into one capture, `LOCAL INFILE`
requests, cursors (`SERVER_STATUS_CURSOR_EXISTS`). Packets split at 16 MB are reassembled logically.

#### 4.2.1 Binary parameter decoding

Integers (signed/unsigned, 1/2/3/4/8 bytes), `FLOAT`, `DOUBLE`, `DATE`, `DATETIME`, `TIMESTAMP`, `TIME`,
`YEAR`, `NULL` (null bitmap), and length-encoded values (strings, `DECIMAL`, JSON, blobs). Parameter types
sent with the first execution are reused by later executions (new-params-bound flag = 0). Values that
cannot be decoded are shown as `<truncated>`.

### 4.3 PostgreSQL (protocol v3)

**Startup.** `SSLRequest` and `GSSENCRequest` are answered `N` by the proxy and not forwarded, so the client
continues in clear text (works with `sslmode=disable|allow|prefer`). `CancelRequest` is forwarded and the
connection is passed through. The startup message gives user and database.

**Simple protocol.** One capture per `Query` message (which may contain several statements); rows and
affected rows are summed from the `CommandComplete` tags; the capture ends at `ReadyForQuery`.

**Extended protocol.** `Parse` (statement name → SQL and parameter type OIDs), `Bind` (portal → statement and
parameters), `Execute` (one capture, `kind = execute`), `Sync`. Parameter types left unspecified by the
client (asyncpg, JDBC) are taken from the server's `ParameterDescription`. After an `ErrorResponse` the
failing execute gets the error and the executes queued before the next `Sync` are discarded, as the server
does. Pipelined batches are supported.

**Parameter decoding.** Text format: string (number if the type is numeric). Binary format: `bool`, `int2`,
`int4`, `int8`, `float4`, `float8`, `uuid`, text types, and one-dimensional arrays of those; anything else is
shown as hex.

## 5. Parser & Analyzer (module B)

The analysis depends only on the statement text and the dialect (`mysql` or `postgres`, from the proxy) and is
cached (LRU, 4096 entries). It runs on the statement as sent (placeholders kept), so all executions of a
prepared statement share it.

| ID | Output | Rule |
|---|---|---|
| B-1 | `formatted` | `sqlglot` pretty print in the source dialect; the raw text if parsing fails. |
| B-2 | `query_type` | From the AST: `SELECT` (incl. `UNION`/`WITH`), `INSERT`, `REPLACE`, `UPDATE`, `DELETE`, `MERGE`, `CREATE`, `DROP`, `ALTER`, `TRUNCATE`, `SET`, `SHOW`, `USE`, `DESCRIBE`, `BEGIN`, `COMMIT`, `ROLLBACK`; otherwise the first keyword (`CALL`, …); `MULTI` when a capture contains statements of different types. |
| B-3 | `tables` | Every table reference except CTE names: canonical name (as in the schema when known), schema qualifier, aliases, `known` (present in the loaded schema), `access` ∈ `read`, `write`, `delete` (combined with `/`). Targets of INSERT/REPLACE/UPDATE/MERGE are `write`, of DELETE/TRUNCATE `delete`. |
| B-4 | `columns` | Every column reference resolved to its table (alias/qualifier; else the only table in the same `SELECT`; else the only table of the query having that column in the schema). `usage` ∈ `read`, `filter` (inside WHERE/JOIN/HAVING), `write` (INSERT column list, UPDATE SET, ON DUPLICATE KEY UPDATE). Computed for SELECT/INSERT/REPLACE/UPDATE/DELETE/MERGE only. |
| B-5 | `joins` | Equality predicates between columns of two different resolved tables, with `in_schema` = the pair is a foreign key (declared or inferred) in the schema. |
| B-6 | `fingerprint` | SHA-1 (12 hex) of the statement with literals → placeholders, IN-lists and multi-row VALUES collapsed, lower-cased. Regex-based fallback when parsing fails. |
| B-7 | `noise` | True for `SET`, `SHOW`, `USE`, `SELECT @@…`, `SELECT DATABASE()/VERSION()/1`, ping comments, `DEALLOCATE`, `DISCARD`, `RESET`, and statements referencing `pg_catalog`, `pg_type`, `information_schema`, `current_setting()`, `set_config()`. Transaction statements are never noise. |
| B-8 | `parse_error` | First line of the sqlglot error when the statement cannot be parsed. The capture is kept. |
| B-9 | ER subgraph | For the detail view: the tables of the query with their PK/FK columns plus the columns used (others counted as "+N more"), the schema links among them, and the observed joins not in the schema. |

MySQL `REPLACE INTO`, which sqlglot does not parse, is analysed as `INSERT` and reported as `REPLACE`.

### 5.1 Schema catalog

`SchemaCatalog` holds tables (columns, types, PK, FK, referenced tables, group/area from sql-fk-finder) and
links (`from`, `from_cols`, `to`, `to_cols`, `kind`). Lookups are case-insensitive and accept names with or
without the database/schema qualifier.

| Source (`--schema`) | Loader | Links |
|---|---|---|
| `*.json` exported by sql-fk-finder (`sqlfk.modello`, format `json`: `{"tabelle", "collegamenti"}`) | `from_sqlfk_model` | declared + inferred (`inferred-alta`, `inferred-media`) |
| folder + `--sqlfk-path` | `from_sqlfk_package`: runs `sqlfk.analisi.leggi_cartella` → `analizza` → `modello.struttura` | declared + inferred |
| folder of `.sql` files or one `.sql` file | `from_ddl`: streams the files, parses only the `CREATE TABLE` statements with sqlglot | declared |

The built-in reader skips data lines, so multi-hundred-MB dumps load in seconds.

## 6. Session & Tagging Manager (module C)

| ID | Requirement |
|---|---|
| C-1 | `start_recording(label)` creates a session (`started_at` = now). An empty label becomes `Action <date time>`. Starting while recording closes the current session first. |
| C-2 | While recording, every capture whose start time is ≥ the session start is assigned to it. Captures outside a recording have no session ("Outside sessions"). |
| C-3 | `stop_recording()` sets `ended_at`. |
| C-4 | `group(label, query_ids)` creates a closed session from already-captured queries (start/end = first/last query) and moves them into it. |
| C-5 | Sessions can be renamed, annotated (free-text notes, included in exports) and deleted (their queries go back to "Outside sessions" unless deletion of the queries is requested). |
| C-6 | A session left open by a crash is closed at its last query on the next start. |
| C-7 | Session events (`started`, `stopped`, `created`, `updated`, `deleted`) are pushed to the dashboard. |

## 7. Storage

SQLite file (`--db`, default `sqlprofiler.sqlite`), WAL mode, history kept between runs.

**`sessions`**: `id`, `label`, `notes`, `started_at`, `ended_at` (epoch seconds).

**`queries`**:

| Column | Content |
|---|---|
| `id`, `ts` | capture id, start time (epoch seconds) |
| `session_id` | session or NULL |
| `conn_id`, `proxy`, `protocol`, `client`, `db_user`, `database` | connection data |
| `kind` | `query`, `execute`, `prepare`, `use` |
| `sql`, `truncated` | statement as sent (truncated to `--max-sql-kb`) |
| `params` | JSON list of bound values |
| `duration_ms`, `rows`, `affected`, `error` | outcome |
| `query_type`, `tables`, `columns`, `joins`, `fingerprint`, `formatted`, `parse_error`, `noise` | analysis (lists as JSON) |

Indexes: `(session_id, id)`, `fingerprint`.

## 8. Dashboard backend (module D)

Local web server (aiohttp), default `http://127.0.0.1:8780`.

### 8.1 REST API

| Method & path | Purpose |
|---|---|
| `GET /api/status` | version, proxies with live connections, recording state, schema summary, counts |
| `GET /api/queries` | list (most recent `limit`, chronological). Filters: `session` (`none` or id), `after_id`, `before_id`, `q` (text in SQL or error), `type`, `table`, `fingerprint`, `noise=0` (hide noise), `limit` (≤ 20000) |
| `GET /api/queries/{id}` | full capture + `er` subgraph |
| `DELETE /api/queries?session=none\|all\|{id}` | delete captures |
| `GET /api/sessions` | sessions with counts (`queries`, `relevant` = non-noise, `errors`) + recording state |
| `POST /api/recording/start` `{label}` / `POST /api/recording/stop` | control panel |
| `POST /api/sessions` `{label, query_ids, parent_id?}` | group existing captures into a new session, or into a sub-session of `parent_id` |
| `PATCH /api/sessions/{id}` `{label?, notes?}` / `DELETE /api/sessions/{id}[?queries=delete]` | edit / delete |
| `GET /api/profiles` | `{enabled, current, root, next_name, profiles: [{name, folder, sessions, queries}]}` (`enabled` false without `--profiles`) |
| `POST /api/profiles` `{name?}` | create a profile (current proxies, no schema) and switch to it |
| `POST /api/profiles/current` `{name}` | switch profile: stops the recording, moves to its history and applies its settings; clients reload on the `profile` WebSocket message |
| `POST /api/recording/sub/start` `{label, parent_id?}` / `POST /api/recording/sub/stop` | sub-session inside the innermost session being recorded (or inside `parent_id`, a closed session); any depth. Stop closes the innermost one. The recording status has `path` (outermost first) and `recording_ids` |
| `GET /api/tables[?session=ID\|none]` | `{tables: [{table, access, statements, sessions, last_ts, in_schema, columns: [{column, usage, statements, in_schema}]}], unused: [names] \| null, schema_tables}` — for a session, all its sub-sessions included |
| `GET /api/er[?session=ID]` | ER subgraph (as for a query) of the tables used by the session and its sub-sessions, or by the profile |
| `GET /api/er/export[?session=ID]` | the same as a standalone HTML page (diagram + tables and attributes), works offline |
| `GET /api/tables/export?format=md\|csv[&session=ID]` | tables and attributes used by the profile (summary, unused, per session and sub-session) or by one session; the CSV has one row per (scope, table, attribute) |
| `DELETE /api/sessions[?queries=delete]` | delete every session (stops a recording in progress); queries are kept as unassigned unless `queries=delete` |
| `GET /api/export?sessions=1,2&format=md\|json[&noise=1]` | download (all sessions when `sessions` is omitted) |
| `GET /api/schema` | schema summary and table names |
| `GET /api/settings` / `PUT /api/settings` `{proxies, listen_host, schema, sqlfk_path}` | read / apply the runtime settings (§11.1); the answer lists the problems and the new status |

### 8.2 WebSocket `/ws`

Server → browser JSON messages, in pipeline order:

| `type` | Payload |
|---|---|
| `query` | `query`: the stored row (without `columns`, `joins`, `formatted`) |
| `settings` | settings applied or schema loading started/finished (the page reloads the status) |
| `session` | `event` (`started`, `stopped`, `created`, `updated`, `deleted`), `session` |
| `connection` | `event` (`open`, `close`), `connection` |
| `cleared` | `session` whose captures were deleted |

Each browser has its own bounded queue (10 000 messages) so a stalled browser cannot block the pipeline.
On reconnection the page reloads status, sessions and the grid to catch up.

## 9. User interface

Single page, no external resources (works offline), light and dark themes following the OS setting.

| Area | Content and behaviour |
|---|---|
| Banner | Shown when no proxy is configured (with a *Configure…* button) or when applying the settings failed (list of problems). |
| Settings dialog (⚙) | Proxies table (protocol, listen port, database host, database port; add/remove; for each a hint "point the target software to … instead of …"), *accept connections from other computers* (0.0.0.0), schema path, sql-fk-finder package path (advanced). In the desktop app, native *File…* / *Folder…* pickers. *Save and apply* applies without restarting and reports problems inline. |
| Header | Proxy chips (`protocol listen → target · N conn`; green with active clients, red if a client forced TLS; tooltip lists connections), schema chip, live/offline indicator. |
| Control panel | Action name field and a prominent **Start/Stop recording** button (Enter starts). While recording: elapsed time and statement count. Starting switches the view to the new session. |
| Sessions sidebar | *Live — all queries*, *Outside sessions*, then sessions (newest first) with relevant/error counts, start time and duration; the recording one pulses. *Export all* (Markdown/JSON), *Delete all* (asks whether to delete the sessions' queries too). |
| Session header | Editable label, notes, Export .md / .json, Delete. |
| Toolbar | Text filter, type filter, table filter (autocomplete from the schema), *Hide noise* (default on), *Follow* (auto-scroll), *Group selected…*, *Clear unassigned*, *Delete all queries* (every query of the profile; sessions are kept), row count; a chip when filtering by fingerprint. |
| Live log grid | Columns: selection, #, time (ms), connection, type badge (colour per type), tables (writes in red), statement (one line), duration (≥ 100 ms highlighted), rows (or affected rows ✎ for DML). Errors in red. New rows flash and are appended live; at most 5000 rows are kept. Arrow keys move the selection; Shift-click selects a range. |
| Detail view | Type, id, timestamp, duration, rows/affected, connection, user@database, client, kind, session. Error box. Notes for truncated or unparsable SQL. SQL with syntax highlighting in three modes: *Formatted*, *With values* (placeholders replaced by the bound parameters), *As sent*; Copy button. Parameter table. **Mini ER diagram** of only the tables involved: PK/FK and used columns (highlighted; writes in red), R/W/D access, declared FK (solid), inferred FK (dashed), observed join not in schema (dotted orange); referenced tables on the left; zoom (wheel), pan (drag), Fit. Columns-used table, joins list with candidate relationships, fingerprint with *show every execution of this statement*. |

Layout: three columns on wide screens, detail below the grid under 1100 px, single column under 700 px.

## 10. Export

Both formats exclude noise unless requested (`noise=1` / `--noise`).

**Markdown**: one section per session with label, time span, statement and error counts, notes; *Tables
involved* (access, number of statements, in schema); *Columns written*; *Joins observed* (count, declared or
**candidate relationship**); *Distinct statements* (by fingerprint: type, executions, total ms); *Sequence*: every
statement in order with relative time, type, duration, rows/affected, connection, formatted SQL **with the
parameter values in place**, the parameters, and the error. With several sessions, an index table comes first.

**JSON**: `{"generator", "exported_at", "sessions": [{"session", "summary", "queries"}]}` where `summary` has
`tables`, `written_columns`, `joins`, `distinct_statements`, and every query includes `sql_with_values`.

## 11. Command line

```
python -m sqlprofiler run [--proxy PROTO:PORT:HOST[:PORT]]... [--listen-host H] [--config FILE] [--ui-host H] [--ui-port N]
                          [--db FILE] [--schema PATH] [--sqlfk-path DIR] [--dialect D] [--max-sql-kb N]
                          [--open] [-v]
python -m sqlprofiler export [--db FILE] [--session ID]... [--format md|json] [--noise] [-o FILE]
python -m sqlprofiler sessions [--db FILE]
```

`PROTO` ∈ `mysql`, `mariadb`, `postgres`, `postgresql`; the target port defaults to 3306/5432. Default proxy:
`mysql:3307:127.0.0.1:3306`. `run` logs the address the target software must use and stops cleanly on
Ctrl+C/SIGTERM (closing an active recording). Exit code 2 when the schema cannot be loaded or a port is busy.

### 11.1 Settings file mode

`run --config FILE` (used by the desktop app): proxies, listen host, schema and sql-fk-finder path are read
from the JSON file (command-line options override it), applied **after** the dashboard is up, and written
back atomically whenever they are changed from the dashboard. Applying settings never stops the program:
invalid values are refused as a whole; a busy port or an unloadable schema is reported (`problems` in the
status, banner in the UI) while the rest is applied and the previous schema is kept. Proxies are started and
stopped individually (connections already open through a removed proxy continue until they close); changing
the listen host restarts all of them; the schema loads in a worker thread (`schema_loading`) and the analysis
cache is reset. Once listening, `run` prints `SQLPROFILER_PORT=<port>` on stdout; `--ui-port 0` picks a
free port.

### 11.2 Desktop application (`electron/`)

| ID | Requirement |
|---|---|
| E-1 | Same architecture as the sql-fk-finder desktop app: Electron window showing the dashboard served by the Python backend. |
| E-2 | Python discovery: `SQLPROFILER_PYTHON`, the app's private environment, then the usual installations (python.org, Anaconda/Miniconda, Homebrew, system). Python ≥ 3.10 required. |
| E-3 | If no Python has `sqlglot` and `aiohttp`, the app offers to create a private virtual environment in its data folder and install `requirements.txt` there (first launch, internet needed), with a progress window. |
| E-4 | The backend runs as `python -m sqlprofiler run --config <data>/settings.json --db <data>/history.sqlite --ui-port 0`; the window opens on the port announced by `SQLPROFILER_PORT`. Backend output goes to `<data>/profiler.log`. If the backend exits unexpectedly the app shows the error and closes. |
| E-5 | Data folder: the Electron user-data folder (`~/.config/SQL Profiler`, `%APPDATA%\SQL Profiler`), or `SQLPROFILER_DATA_DIR`. |
| E-6 | Single instance (a second launch focuses the first, which owns the proxy ports). |
| E-7 | On quit, an active recording is stopped through the API, then the backend is terminated. |
| E-8 | Native file/folder pickers for the Settings dialog through a context-isolated preload bridge (`window.sqlprofilerApp.choosePath`); exports open a save dialog starting in Documents; external links open in the browser. |
| E-9 | Packages (electron-builder): Linux AppImage and tar.gz, Windows portable exe. The `sqlprofiler` package and `requirements.txt` are bundled as resources. On Linux the executable is wrapped to pass `--no-sandbox`, because Ubuntu 24.04+ restricts the user namespaces the Chromium sandbox needs. |

## 12. Non-functional requirements

| Area | Requirement |
|---|---|
| Transparency | The proxy adds no protocol round-trips; latency is one local hop. Captures are processed off the network path. |
| Robustness | Decoder errors switch the connection to passthrough (A-3). Unparsable SQL is still stored. Packet boundaries at any byte position are handled (tested byte-by-byte). |
| Memory | Bounded per connection by the frame head limits (A-4); grid limited to 5000 rows; WebSocket queues bounded. |
| Security | Everything binds to 127.0.0.1 by default. Passwords are forwarded, never parsed or stored. Captured SQL contains business data: the SQLite file and the exports must be handled as sensitive. Removing TLS is intended for a controlled test environment. |
| Portability | Pure Python ≥ 3.10, dependencies `sqlglot` and `aiohttp`; Linux and Windows (signal handlers are optional). |
| Compatibility | Verified with MySQL 5.7 (mysql CLI, mysql-connector-python 9.1 C extension and pure Python, PyMySQL) and PostgreSQL 16 (psycopg 3, asyncpg). |

## 13. Integration with sql-fk-finder

* The JSON produced by `sqlfk.modello.struttura` is the interchange format (table, columns with `pk`/`fk`/`verso`,
  links with `tipo` = `dichiarata`/`alta`/`media`).
* With `--sqlfk-path` the profiler imports the `sqlfk` package and runs its analysis on the dumps.
* The dashboard's visual language (palette, DBeaver-style table boxes, FK line styles) matches the
  sql-fk-finder interface, and the mini ER diagram follows the same conventions as `sqlfk/web/diagramma.js`
  (parents on the left, lines from the FK column to the referenced column).
* Candidate relationships found at runtime complement the static inference of sql-fk-finder.

## 14. Verification

`python -m pytest` (80 tests):

* MySQL decoder: capability stripping in greeting and response, user/database, text queries, OK/ERR,
  `COM_INIT_DB`, multiple result sets, prepared statements with all parameter families, type reuse, failed
  PREPARE, unknown statement ids, TLS passthrough, 16 MB packet reassembly and truncation, fault tolerance —
  each conversation fed whole, byte by byte and in 7-byte chunks.
* PostgreSQL decoder: SSL refusal, startup, simple and multi-statement queries, extended protocol with text and
  binary parameters, types from ParameterDescription, arrays, pipelining with error-until-Sync, cancel requests.
* Runtime settings: validation, proxies started/stopped, busy port and missing schema reported, file saved.
* Analyzer, schema loaders, storage, sessions, export, and an end-to-end test (fake MySQL server → proxy →
  pipeline → REST API and WebSocket).

Manual verification was performed against real MySQL 5.7 and PostgreSQL 16 servers with the drivers listed
in §12, and the dashboard was checked in Chrome in both themes.

**Desktop app**: the Linux AppImage was run as built (Ubuntu with user-namespace restrictions), configured
through its settings file, and captured the whole demo scenario below through its proxy; quitting stopped the
Python backend cleanly.

**Demo application** (`demo/`): *Mini Shop*, an order-management web app on MySQL (mysql-connector with a
connection pool, prepared and plain statements, transactions with `SELECT … FOR UPDATE`, a multi-table
`UPDATE … JOIN`, an audit log, reports with aggregates). `demo/run_demo.sh` starts a throwaway MySQL
server, the profiler and the app; `demo/scenario.py` plays ten user actions, records each as a session
through the REST API and exports the Markdown report. Expected results: every action recorded with zero
errors, the "not enough stock" order shows the rolled-back transaction, and the undeclared relationships
`orders.created_by → users.id` and `audit_log.user_id → users.id` are reported as candidate relationships.

## 15. Limitations and possible extensions

* **TLS-only clients** cannot be decoded (forwarded and flagged). A TLS-terminating mode with a local
  certificate could be added.
* **SQL Server (TDS)** and **Oracle** are not supported; the `Decoder` interface (`feed_client`/`feed_server`)
  is designed so new protocols can be added in `sqlprofiler/protocols/`.
* **Native log tailing** (MySQL general log, PostgreSQL `log_statement`), mentioned as an alternative in the
  architecture document, is not implemented; the proxy gives timings and outcomes that logs do not.
* Statements prepared before the profiler started cannot be resolved to their SQL.
* Column resolution for unqualified columns in multi-table statements needs the schema; without it such
  columns may have no table.
* The desktop app needs a Python 3.10+ installation on the computer (it is not bundled), like sql-fk-finder.
* The Windows portable exe is built on Linux without Wine, so its file details/icon are Electron's defaults;
  it has not been tested on Windows yet.
* Single local user; no authentication on the dashboard.

## 16. Traceability to the architecture document

| Architecture section | Where it is satisfied |
|---|---|
| §1 Objective | §1, whole system |
| §2 Local database proxy, zero-touch | §3.1, §4 |
| §3A Interceptor: listen, forward, decode, SQL + timestamps + duration | §4 (A-1…A-8) |
| §3B Parser: format, type, tables/columns, sql-fk-finder cross-reference | §5 (B-1…B-9), §5.1, §13 |
| §3C Session & tagging, Start/Stop recording, Session ID + label | §6 (C-1…C-7) |
| §3D Real-time dashboard, local server, WebSockets | §8, §9 |
| §4 Pipeline: proxy → parser → SQLite → WebSocket | §3.3, §7 |
| §5 `DatabaseProxy`, `QueryAnalyzer`, `SessionManager` interfaces | `proxy.py`, `analyzer.py` + `app.Profiler.process`, `sessions.py` |
| §6 Control panel, live log grid, detail view with highlighted SQL and mini ER diagram, export JSON/Markdown | §9, §10 |
| §7 Python + SQLite, import of the static schema | §5.1, §7, §12 |
