# SQL Profiler & Interceptor

A local database proxy that captures every SQL statement a target application runs, analyses it with
`sqlglot`, cross-references it with the schema analysed by **sql-fk-finder**, and groups it under the UI
action you were performing ("User Login", "Save Invoice"). Everything streams live to a web dashboard
and can be exported to Markdown or JSON to document how the legacy software works internally.

The target software is not modified: you only change **where it connects**.

```
 target app ──► profiler proxy :3307 ──► real database :3306
                     │
                     ├─ analyzer (sqlglot + schema)
                     ├─ sessions (Start/Stop recording)
                     ├─ SQLite history
                     └─ dashboard  http://127.0.0.1:8780  (WebSocket)
```

Supported databases: **MySQL / MariaDB** and **PostgreSQL**.
The full specification is in [SPECIFICATION.md](SPECIFICATION.md).

## Desktop app

Like sql-fk-finder, the profiler also comes as a desktop application (Electron) — see
[electron/README.md](electron/README.md):

```bash
cd electron && npm install && npm run dist      # -> dist/SQL-Profiler-1.0.0-linux-x86_64.AppImage
```

Double-click the AppImage (or the Windows portable exe), open **⚙ Settings**, add a proxy and the schema:
everything else works as in the browser. It needs Python 3.10+ on the computer and installs its two libraries
by itself on first launch.

## Install

Python 3.10 or newer.

```bash
pip install -r requirements.txt        # sqlglot, aiohttp
# or, as a command:  pip install .   ->  sql-profiler run ...
```

## Quick start

```bash
# proxy MySQL: the app connects to 3307, the real server stays on 3306
python -m sqlprofiler run --proxy mysql:3307:127.0.0.1:3306 \
                          --schema ../Dump20261015-1 --open
```

1. Reconfigure the target software to connect to `127.0.0.1:3307` instead of the real server
   (connection string, ini file, ODBC DSN, or a hosts/DNS entry). User, password and database stay the same.
2. Open the dashboard (`--open` does it for you): every statement appears in the live grid.
   Proxies and schema can also be changed while it runs, from **⚙ Settings** in the dashboard.
3. Type the action you are about to perform in the control panel ("Save Order"), press **Start recording**,
   do it in the target software, press **Stop recording**.
4. Select the session on the left: review the statements, the tables they touch, the mini ER diagram;
   export it with **Export .md** / **Export .json**.

Forgot to press Start? Tick the rows in the grid and use **Group selected…** to make a session afterwards.

### Options

| Option | Default | Meaning |
|---|---|---|
| `--proxy PROTO:PORT:HOST[:PORT]` | `mysql:3307:127.0.0.1:3306` | Proxy to start; repeatable. `PROTO` is `mysql`, `mariadb`, `postgres` |
| `--listen-host` | `127.0.0.1` | Use `0.0.0.0` when the target software runs on another machine |
| `--ui-host`, `--ui-port` | `127.0.0.1`, `8780` | Dashboard address |
| `--db` | `sqlprofiler.sqlite` | SQLite file with the captured history (kept between runs) |
| `--profiles FOLDER` | – | One profile (sub-folder with its own history and settings) per application being profiled; created and switched from the dashboard. Replaces `--db` and `--config` |
| `--schema` | – | sql-fk-finder model `.json`, a folder of `.sql` dumps, or one `.sql` file |
| `--sqlfk-path` | – | Folder containing the `sqlfk` package: analyse the dumps with sql-fk-finder (inferred keys too) |
| `--max-sql-kb` | `1024` | Longer statements are stored truncated |
| `--config FILE` | – | JSON settings file (proxies, schema): read at start and rewritten when you change them in **⚙ Settings**. In this mode problems (busy port, missing schema) are shown in the dashboard instead of stopping the program |

### Schema sources

* `--schema model.json` — the JSON model exported by sql-fk-finder (*Modello relazionale → json*, or
  `python -m sqlfk.modello <dumps> --formato json > model.json`): declared **and inferred** foreign keys.
* `--schema <dumps folder> --sqlfk-path <.../resources>` — runs the sql-fk-finder analysis directly
  (needs its dependencies: pandas, networkx, …).
* `--schema <dumps folder>` — built-in reader: tables, columns, primary keys, declared foreign keys.
  Reads the 444 MB `Dump20261015-1` folder in about 2 seconds.

Joins seen at runtime that are **not** in the schema are flagged as *candidate relationships*.

### Export from the command line

```bash
python -m sqlprofiler sessions                          # list
python -m sqlprofiler export --session 3 -o save-order.md
python -m sqlprofiler export --format json -o all.json  # every session
```

### Profiles

Start with `--profiles profiles` (the desktop app always does) to keep each application you analyse in its own
folder: `profiles/Profile 1/history.sqlite` + `settings.json`. The menu next to the title in the dashboard
switches profile or creates a new one (named "Profile N" by default); a new profile starts with the current
proxies and no schema. The profiler reopens the last profile used.

### Sub-actions, tables and diagrams

While a session is recorded ("Customers"), *Start sub-action* opens a sub-session inside the innermost one
being recorded ("Search", then "Open customer card" inside it…, at any depth); *Stop “…”* closes the innermost
one. A session shows its own queries plus those of all its sub-sessions; the sidebar shows them as a tree. A
sub-action can also be added later to a closed session, and *Group selected…* inside a session moves the
selected queries to a new sub-action.

Selecting a session shows its overview: every table used by it and its sub-sessions, with the access, the
attributes (columns) used — read, filter, write — and whether they are in the schema, plus the ER diagram of
those tables and their links. *Tables of the profile*, at the bottom of the sessions list, shows the same live
for the whole profile, with the schema tables never used. *⬇ Download* (session or profile) saves the queries
(.md / .json), the tables and attributes (.md / .csv) and the diagram as a standalone page (.html, works
offline, printable to PDF).

## Try it with the demo application

`demo/` contains **Mini Shop**, a small order-management web app that uses MySQL like a typical legacy
application: a permission check on every page, an audit log, prepared statements mixed with plain queries,
transactions with row locks. Its schema leaves two relationships undeclared on purpose
(`orders.created_by`, `audit_log.user_id`) for the profiler to discover.

```bash
pip install -r demo/requirements.txt     # mysql-connector-python
sudo mysql < demo/mysql_user.sql         # once: database shop_demo and user shop / Shop-Demo-2026 on the installed MySQL
demo/run_demo.sh --fresh                 # profiler :3317 -> MySQL :3306, dashboard :8780, app :8090
```

The demo uses the MySQL server installed on the computer (`MYSQL_PORT=3307 demo/run_demo.sh` if it is not on
3306). It only touches the `shop_demo` database; `--fresh` drops and recreates it with the sample data.
On Windows run `mysql_user.sql` from MySQL Workbench (or `mysql -u root -p < demo\mysql_user.sql`).

Then either:

* **by hand** — open the app at http://127.0.0.1:8090 (login `admin`/`admin` or `enrico`/`enrico`) and the
  dashboard at http://127.0.0.1:8780; name an action ("Save order"), press *Start recording*, do it in the app,
  press *Stop recording*, and look at what the app did in the database;
* **automatically** — `python demo/scenario.py` (in another terminal) plays ten actions (failed login, login,
  search, customer card, new customer, order, order without stock, cancel, reports, logout), records each one
  as a session and writes `demo/scenario-report.md`.

## Things to know

* **Encryption.** The proxy has to read the traffic. For MySQL it removes TLS (and compression) from the
  capabilities the server announces; for PostgreSQL it answers "no" to the SSL request. Clients configured
  with `ssl-mode=PREFERRED` / `sslmode=prefer` (the defaults) continue in clear text. Clients that *require*
  TLS (`ssl-mode=REQUIRED`, `sslmode=require`) still work, but their statements cannot be captured: the
  connection is shown in red in the header.
* **Authentication.** Passwords are forwarded unchanged and never stored. With MySQL 8 `caching_sha2_password`
  over a non-TLS link, some clients need `allowPublicKeyRetrieval=true` (JDBC) or `get-server-public-key`.
* **Prepared statements** started before the profiler was running show as
  `/* prepared statement #N … */`: restart the target software after starting the profiler.
* **Noise.** Driver chatter (`SET`, `SHOW`, `SELECT @@…`, catalog introspection) is hidden by default
  (*Hide noise*) and excluded from exports unless `--noise` is passed.
* The captured SQL contains real data values: treat the SQLite file and the exports accordingly.

## Tests

```bash
pip install pytest
python -m pytest
```

The suite covers both wire protocols with hand-built packets (also fed one byte at a time), the analyzer,
storage, sessions, export, and an end-to-end run (fake MySQL server → proxy → REST API + WebSocket).
