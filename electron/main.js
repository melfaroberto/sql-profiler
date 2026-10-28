// Desktop app: starts the Python profiler (python -m sqlprofiler run) and shows its dashboard in a window.
// Works from the project folder (npm start) and as an installed app (AppImage / portable exe): in that case the
// sqlprofiler Python package and requirements.txt are among the app's resources.
// Same structure as the sql-fk-finder desktop app.
const { app, BrowserWindow, dialog, ipcMain, session, shell } = require("electron");
const { spawn, spawnSync } = require("child_process");
const path = require("path");
const os = require("os");
const fs = require("fs");

// folder that contains sqlprofiler/ and requirements.txt
const ROOT = app.isPackaged ? process.resourcesPath : path.join(__dirname, "..");
const REQUIREMENTS = path.join(ROOT, "requirements.txt");
const ICON = path.join(__dirname, "icon.png");
const LIBRARIES = "import sqlglot, aiohttp";
const WIN = process.platform === "win32";
const TITLE = "SQL Profiler";

let server = null;
let address = null;
let mainWindow = null;
let quitting = false;

// settings, history and the private Python environment live in the app's user data folder
// (SQLPROFILER_DATA_DIR moves it, e.g. next to a portable exe or on a USB stick)
if (process.env.SQLPROFILER_DATA_DIR) app.setPath("userData", path.resolve(process.env.SQLPROFILER_DATA_DIR));
const dataPath = (...p) => path.join(app.getPath("userData"), ...p);
const envDir = () => dataPath("python");
const envPython = () => path.join(envDir(), WIN ? "Scripts" : "bin", WIN ? "python.exe" : "python");

// [command, initial arguments] of the Pythons to try, in order
function candidates() {
  const home = os.homedir();
  const list = [];
  if (process.env.SQLPROFILER_PYTHON) list.push([process.env.SQLPROFILER_PYTHON, []]);
  list.push([envPython(), []]);
  if (WIN) {
    const programs = path.join(process.env.LOCALAPPDATA || "", "Programs", "Python");
    list.push(["python", []], ["py", ["-3"]],
      [path.join(home, "anaconda3", "python.exe"), []], [path.join(home, "miniconda3", "python.exe"), []],
      ...["Python314", "Python313", "Python312", "Python311", "Python310"].map(v => [path.join(programs, v, "python.exe"), []]));
  } else {
    // an app started from the desktop menu does not have the terminal's PATH: try the usual places too
    list.push([path.join(home, "anaconda3", "bin", "python"), []], [path.join(home, "miniconda3", "bin", "python"), []],
      ["/opt/anaconda3/bin/python", []], ["/opt/homebrew/bin/python3", []], ["/usr/local/bin/python3", []],
      ["python3", []], ["python", []], ["/usr/bin/python3", []]);
  }
  return list;
}

function tryPython(python, args, code) {
  try {
    const r = spawnSync(python, [...args, "-c", code], { cwd: ROOT, timeout: 60000, windowsHide: true });
    return r.status === 0;
  } catch { return false; }
}

// {ready: [cmd, args]} if a Python already has the libraries, otherwise {base: [cmd, args]} (Python >= 3.10)
function findPython() {
  let base = null;
  for (const [python, args] of candidates()) {
    if (path.isAbsolute(python) && !fs.existsSync(python)) continue;
    if (!tryPython(python, args, "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)")) continue;
    if (tryPython(python, args, LIBRARIES)) return { ready: [python, args] };
    if (!base && python !== envPython()) base = [python, args];
  }
  return { base };
}

function run(command, args, win) {
  return new Promise((resolve, reject) => {
    const p = spawn(command, args, { cwd: ROOT, windowsHide: true });
    let output = "";
    const read = data => {
      output += data;
      const line = String(data).trim().split("\n").pop();
      if (line && win && !win.isDestroyed()) {
        win.webContents.executeJavaScript(
          `document.getElementById("detail").textContent = ${JSON.stringify(line.slice(0, 160))}`).catch(() => {});
      }
    };
    p.stdout.on("data", read);
    p.stderr.on("data", read);
    p.on("error", reject);
    p.on("exit", code => code === 0 ? resolve() : reject(new Error(output.slice(-2000))));
  });
}

// creates a private Python environment for the app and installs the libraries (needs internet, first launch only)
async function installLibraries([python, args]) {
  const win = new BrowserWindow({ width: 560, height: 220, resizable: false, title: TITLE, icon: ICON,
                                  autoHideMenuBar: true, webPreferences: { sandbox: true } });
  win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(
    "<body style='font-family:-apple-system,Segoe UI,sans-serif;padding:24px'><h3>Installing the Python libraries</h3>" +
    "<p>First launch only: needs internet and a minute.</p><p id='detail' style='color:#666;font-size:12px'>starting…</p></body>"));
  try {
    await run(python, [...args, "-m", "venv", envDir()], win);
    await run(envPython(), ["-m", "pip", "install", "--upgrade", "pip"], win);
    await run(envPython(), ["-m", "pip", "install", "-r", REQUIREMENTS], win);
  } finally {
    if (!win.isDestroyed()) win.destroy();
  }
  return [envPython(), []];
}

async function preparePython() {
  const found = findPython();
  if (found.ready) return found.ready;
  if (!found.base) {
    const choice = dialog.showMessageBoxSync({
      type: "error", title: "Python not found", buttons: ["Download Python", "Quit"], defaultId: 0,
      message: "SQL Profiler needs Python 3.10 or newer.",
      detail: "Install it from python.org (on Windows tick \"Add python.exe to PATH\") and open the app again: " +
              "it installs its libraries by itself.\n\nIf Python is in an unusual place, start the app with the " +
              "SQLPROFILER_PYTHON environment variable set to the python executable.",
    });
    if (choice === 0) await shell.openExternal("https://www.python.org/downloads/");
    return null;
  }
  const choice = dialog.showMessageBoxSync({
    type: "question", title: "First launch", buttons: ["Install", "Quit"], defaultId: 0,
    message: "The app's Python libraries (sqlglot, aiohttp) are missing.",
    detail: `I can install them now in a folder of the app (${envDir()}), without touching your Python. ` +
            "It needs internet and a minute, only this time.",
  });
  if (choice !== 0) return null;
  try {
    return await installLibraries(found.base);
  } catch (e) {
    fs.rmSync(envDir(), { recursive: true, force: true });
    dialog.showErrorBox("Installation failed",
      "The libraries could not be installed. You can install them by hand with:\n  pip install -r requirements.txt\n\n" + e.message);
    return null;
  }
}

// before profiles existed, the settings and the history were in the data folder itself: they become "Profile 1"
function migrateToProfiles() {
  const old = ["settings.json", "history.sqlite", "history.sqlite-wal", "history.sqlite-shm"].filter(f => fs.existsSync(dataPath(f)));
  if (fs.existsSync(dataPath("profiles")) || !old.length) return;
  fs.mkdirSync(dataPath("profiles", "Profile 1"), { recursive: true });
  for (const f of old) fs.renameSync(dataPath(f), dataPath("profiles", "Profile 1", f));
}

function startServer([python, args]) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(dataPath(), { recursive: true });
    migrateToProfiles();
    const log = fs.createWriteStream(dataPath("profiler.log"));
    server = spawn(python, [...args, "-m", "sqlprofiler", "run", "--profiles", dataPath("profiles"), "--ui-port", "0"], {
      cwd: ROOT, stdio: ["ignore", "pipe", "pipe"], windowsHide: true,
      env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1", PYTHONUTF8: "1", PYTHONUNBUFFERED: "1" },
    });
    let errors = "";
    server.stdout.on("data", data => {
      log.write(data);
      const m = String(data).match(/SQLPROFILER_PORT=(\d+)/);
      if (m) resolve(`http://127.0.0.1:${m[1]}/`);
    });
    server.stderr.on("data", data => { errors += data; log.write(data); process.stderr.write(data); });
    server.on("exit", code => {
      server = null;
      const message = `The profiler stopped (code ${code}).\n\n${errors.slice(-1500)}\n\nLog: ${dataPath("profiler.log")}`;
      reject(new Error(message));
      if (!quitting && mainWindow) {
        dialog.showErrorBox(TITLE, message);
        app.quit();
      }
    });
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1500, height: 950, minWidth: 900, minHeight: 600, title: TITLE, icon: ICON, autoHideMenuBar: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true,
                      preload: path.join(__dirname, "preload.js") },
  });
  // links to other sites open in the browser
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (!url.startsWith(address)) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("page-title-updated", e => e.preventDefault());
  mainWindow.on("closed", () => { mainWindow = null; });
  mainWindow.loadURL(address);
}

// file / folder picker for the Settings dialog
ipcMain.handle("choose-path", async (event, kind) => {
  const folder = kind === "folder";
  const r = await dialog.showOpenDialog(BrowserWindow.fromWebContents(event.sender), {
    title: folder ? "Choose a folder" : "Choose a file",
    properties: [folder ? "openDirectory" : "openFile"],
    filters: folder ? [] : [{ name: "Schema (sql-fk-finder model, SQL dump)", extensions: ["json", "sql"] },
                            { name: "All files", extensions: ["*"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

if (!app.requestSingleInstanceLock()) {
  app.quit();          // a second copy would find the proxy ports already taken: show the first one instead
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    if (process.platform === "darwin" && app.dock) app.dock.setIcon(ICON);
    // exports: ask where to save, starting from Documents
    session.defaultSession.on("will-download", (event, item) => {
      item.setSaveDialogOptions({ defaultPath: path.join(app.getPath("documents"), item.getFilename()) });
    });
    const python = await preparePython();
    if (!python) {
      app.quit();
      return;
    }
    try {
      address = await startServer(python);
    } catch (e) {
      dialog.showErrorBox("The profiler did not start", e.message);
      app.quit();
      return;
    }
    createWindow();
    app.on("activate", () => { if (!mainWindow) createWindow(); });
  });
}

app.on("window-all-closed", () => app.quit());

// close a recording in progress cleanly, then stop the Python process
app.on("before-quit", event => {
  if (quitting || !server) return;
  event.preventDefault();
  quitting = true;
  fetch(address + "api/recording/stop", { method: "POST", signal: AbortSignal.timeout(1500) })
    .catch(() => {})
    .finally(() => {
      if (server) server.kill();
      app.quit();
    });
});
