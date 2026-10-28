# SQL Profiler — desktop app

An Electron window around the profiler, built the same way as the sql-fk-finder desktop app: it finds a Python
3.10+, installs the two libraries (`sqlglot`, `aiohttp`) in a private environment on first launch if they are
missing, starts `python -m sqlprofiler run` on a free port, and shows the dashboard.

Everything is configured from the dashboard: **⚙ Settings** → proxies (listen port → database host:port) and the
schema (sql-fk-finder `.json` model, a folder of dumps or a `.sql` file, with *File…* / *Folder…* pickers).
Settings and history are kept in the app's data folder:

| | Linux | Windows |
|---|---|---|
| data folder | `~/.config/SQL Profiler/` | `%APPDATA%\SQL Profiler\` |
| files | `settings.json`, `history.sqlite`, `profiler.log`, `python/` (private environment, if created) | same |

Set `SQLPROFILER_DATA_DIR` to use another folder (e.g. next to the portable exe), and `SQLPROFILER_PYTHON` to
force a specific Python.

## Run from the sources

```bash
cd electron
npm install          # once
npm start
```

## Build the executables

```bash
npm run dist         # Linux: dist/SQL-Profiler-1.0.0-linux-x86_64.AppImage and -linux-x64.tar.gz
npm run dist:win     # Windows: dist/SQL-Profiler-1.0.0-portable.exe
npm run dist:all     # both
```

The Python package (`../sqlprofiler`) and `../requirements.txt` are copied into the app's resources.

Notes:

* **Linux sandbox.** Ubuntu 24.04+ forbids the user namespaces the Chromium sandbox needs, and Electron aborts
  at start. `npm start` passes `--no-sandbox`, and the build hook `build/linux-no-sandbox.js` wraps the packaged
  executable in a script that does the same, so the AppImage starts with a double click. The window only shows
  the profiler's own local page.
* **Windows from Linux.** Stamping the icon and version details into the `.exe` needs Wine; `dist:win` skips that
  step (`signAndEditExecutable=false`), so the portable exe works but shows Electron's default file details.
  Building on Windows with `npx electron-builder --win portable` gives the full icon.
* The packaged app still needs **Python 3.10+** on the computer (like sql-fk-finder). On first launch without
  the libraries it offers to install them (internet needed once).
