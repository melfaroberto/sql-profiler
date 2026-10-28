"use strict";
// Dashboard: control panel, sessions, live query grid (WebSocket) and detail view.

const MAX_ROWS = 5000;
const $ = id => document.getElementById(id);
const esc = SqlHL.esc;

const state = {
  view: "all",                 // "all" | "none" | session id
  rows: [], byId: new Map(),
  selected: new Set(), lastClicked: null,
  current: null,               // id shown in the detail view
  sessions: [], recording: { recording: false },
  filters: { q: "", type: "", table: "", fingerprint: "" },
  status: null, sqlMode: "formatted", er: null,
  tablesPanel: false,          // the detail view shows the tables of the profile
  overview: null,              // ... or the overview of this session
  sessionTables: null, sessionTablesFor: null, erKey: null,
  collapsed: new Set(),        // sessions whose sub-actions are hidden in the sidebar
};

const sessionById = id => state.sessions.find(s => s.id === id);
// the session a query of session id counts for in the view of parentId (itself or its parent)
// does a query of session id belong to the view of session `view` (itself, or one of its sub-actions at any depth)?
function inSession(id, view) {
  for (let s = sessionById(id), n = 0; s && n < 100; s = sessionById(s.parent_id), n++) if (s.id === view) return true;
  return false;
}

// ====================================================================== helpers

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts, body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

const pad = n => String(n).padStart(2, "0");
function clock(ts, ms = true) {
  const d = new Date(ts * 1000);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}` + (ms ? "." + String(d.getMilliseconds()).padStart(3, "0") : "");
}
function dateTime(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${clock(ts, false)}`;
}
function duration(sec) {
  sec = Math.max(0, Math.floor(sec));
  return sec >= 3600 ? `${Math.floor(sec / 3600)}:${pad(Math.floor(sec / 60) % 60)}:${pad(sec % 60)}` : `${pad(Math.floor(sec / 60))}:${pad(sec % 60)}`;
}
const ms = v => v == null ? "" : v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
const oneLine = s => (s || "").replace(/\s+/g, " ").trim();
const dialectOf = row => row.protocol === "postgres" ? "postgres" : "mysql";
const hideNoise = () => $("hide-noise").checked;

// window.prompt() does not exist in the desktop app (Electron): ask in a dialog of the page instead.
// Resolves to the text entered, or null when cancelled.
function askText(title, text, value = "") {
  const dlg = $("ask-dlg"), input = $("ask-input");
  $("ask-title").textContent = title;
  $("ask-text").textContent = text;
  input.value = value;
  return new Promise(resolve => {
    const done = result => {
      $("ask-form").onsubmit = $("ask-cancel").onclick = dlg.oncancel = null;
      dlg.close();
      resolve(result);
    };
    $("ask-form").onsubmit = e => { e.preventDefault(); done(input.value); };
    $("ask-cancel").onclick = () => done(null);
    dlg.oncancel = e => { e.preventDefault(); done(null); };
    dlg.showModal();
    input.select();
  });
}

function download(url) {
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  document.body.append(a);
  a.click();
  a.remove();
}

// ====================================================================== status & proxies

async function loadStatus() {
  try {
    state.status = await api("/api/status");
  } catch { return; }
  const s = state.status;
  $("proxies").innerHTML = s.proxies.map(p => {
    const n = p.connections.length;
    const tip = `${p.protocol} proxy: the target software connects to ${p.listen}, traffic goes to ${p.target}\n` +
      (n ? p.connections.map(c => `#${c.id} ${c.client} ${c.user ? c.user + "@" : ""}${c.database} – ${c.queries} queries${c.note ? " – " + c.note : ""}`).join("\n") : "no client connected");
    const enc = p.connections.some(c => c.encrypted);
    return `<span class="chip ${enc ? "bad" : n ? "live" : ""}" title="${esc(tip)}"><b>${esc(p.protocol)}</b> ${esc(p.listen)} → ${esc(p.target)} · ${n} conn${enc ? " · TLS!" : ""}</span>`;
  }).join("");
  const sc = $("schema-chip");
  if (s.schema_loading) {
    sc.textContent = "loading schema…";
    sc.classList.add("muted");
  } else if (s.schema && s.schema.tables) {
    sc.textContent = `schema: ${s.schema.tables} tables, ${s.schema.links} links`;
    sc.title = s.schema.source;
    sc.classList.remove("muted");
  } else {
    sc.textContent = "no schema";
    sc.title = "Load a schema in Settings to see keys and relationships";
    sc.classList.add("muted");
  }
  renderBanner(s);
  setRecording(s.recording, false);
}

function renderBanner(s) {
  const b = $("banner");
  if (s.problems && s.problems.length) {
    b.className = "banner bad";
    b.innerHTML = `<b>Settings problem:</b><ul>${s.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul>
      <button class="small" type="button" data-open-settings>Open settings</button>`;
  } else if (!s.proxies.length) {
    b.className = "banner";
    b.innerHTML = `<span>No proxy is running yet: tell the profiler where the target software's database is.</span>
      <button class="small primary" type="button" data-open-settings>Configure…</button>`;
  } else {
    b.hidden = true;
    return;
  }
  b.hidden = false;
}

// ====================================================================== profiles

async function loadProfiles() {
  const data = await api("/api/profiles").catch(() => ({ enabled: false }));
  const sel = $("profile-select");
  sel.hidden = !data.enabled;
  if (!data.enabled) return;
  state.profiles = data;
  sel.innerHTML = data.profiles.map(p =>
    `<option value="${esc(p.name)}" title="${esc(p.folder)}">${esc(p.name)}</option>`).join("") +
    `<option value="" data-new>＋ New profile…</option>`;
  sel.value = data.current;
  document.title = `${data.current} — SQL Profiler`;
}

async function changeProfile() {
  const sel = $("profile-select");
  const data = state.profiles;
  const isNew = sel.selectedOptions[0].hasAttribute("data-new");
  const restore = () => { sel.value = data.current; };
  if (state.recording.recording && !confirm("A recording is in progress: it will be stopped. Continue?")) return restore();
  try {
    if (isNew) {
      const name = await askText("New profile", "One profile per application you profile: it gets its own sessions, queries and settings.", data.next_name);
      if (name === null) return restore();
      await api("/api/profiles", { method: "POST", body: { name } });
    } else {
      await api("/api/profiles/current", { method: "POST", body: { name: sel.value } });
    }
  } catch (e) {
    alert(e.message);
    return restore();
  }
  location.reload();            // everything on the page belongs to the previous profile
}

// ====================================================================== settings dialog

const DEFAULT_PORTS = { mysql: [3307, 3306], postgres: [5433, 5432] };

function proxyRow(p) {
  const tr = document.createElement("tr");
  tr.className = "proxy";
  tr.innerHTML = `
    <td><select data-f="protocol"><option value="mysql">MySQL / MariaDB</option><option value="postgres">PostgreSQL</option></select></td>
    <td><input data-f="listen_port" type="number" min="1" max="65535" required></td>
    <td><input data-f="target_host" type="text" required placeholder="host or IP"></td>
    <td><input data-f="target_port" type="number" min="1" max="65535" required></td>
    <td class="c-del"><button class="small danger" type="button" title="Remove">✕</button></td>`;
  const hint = document.createElement("tr");
  hint.innerHTML = `<td class="hint" colspan="5"></td>`;
  const f = name => tr.querySelector(`[data-f="${name}"]`);
  f("protocol").value = p.protocol === "postgresql" ? "postgres" : p.protocol === "mariadb" ? "mysql" : p.protocol;
  f("listen_port").value = p.listen_port;
  f("target_host").value = p.target_host;
  f("target_port").value = p.target_port;
  const update = () => {
    const host = $("listen-all").checked ? "<this computer's IP>" : "127.0.0.1";
    hint.firstElementChild.textContent = `→ point the target software to ${host}:${f("listen_port").value || "?"} ` +
      `instead of ${f("target_host").value || "?"}:${f("target_port").value || "?"}`;
  };
  f("protocol").addEventListener("change", e => {
    const [listen, target] = DEFAULT_PORTS[e.target.value];
    f("listen_port").value = listen;
    f("target_port").value = target;
    update();
  });
  tr.addEventListener("input", update);
  $("listen-all").addEventListener("change", update);
  tr.querySelector(".c-del button").onclick = () => { tr.remove(); hint.remove(); };
  update();
  return [tr, hint];
}

function readProxies() {
  return [...document.querySelectorAll("#proxy-rows tr.proxy")].map(tr => {
    const f = name => tr.querySelector(`[data-f="${name}"]`).value.trim();
    return { protocol: f("protocol"), listen_port: Number(f("listen_port")), target_host: f("target_host"),
             target_port: Number(f("target_port")) };
  });
}

function showProblems(problems) {
  const box = $("settings-problems");
  box.hidden = !problems.length;
  box.textContent = problems.join("\n");
}

async function openSettings() {
  const data = await api("/api/settings");
  const s = data.settings;
  $("proxy-rows").innerHTML = "";
  const proxies = s.proxies.length ? s.proxies : [{ protocol: "mysql", listen_port: 3307, target_host: "127.0.0.1", target_port: 3306 }];
  $("listen-all").checked = s.listen_host === "0.0.0.0";
  proxies.forEach(p => $("proxy-rows").append(...proxyRow(p)));
  $("schema-path").value = s.schema || "";
  $("sqlfk-path").value = s.sqlfk_path || "";
  showProblems(data.problems || []);
  $("settings-saved").textContent = data.saved_to ? `Saved in ${data.saved_to}` :
    "These settings apply until the profiler is restarted (it was started without --config).";
  $("settings-save").disabled = false;
  $("settings-save").textContent = "Save and apply";
  $("settings-dlg").showModal();
}

async function saveSettings(e) {
  e.preventDefault();
  if (!$("settings-form").reportValidity()) return;
  const btn = $("settings-save");
  btn.disabled = true;
  btn.textContent = $("schema-path").value.trim() ? "Applying (loading the schema)…" : "Applying…";
  try {
    const r = await api("/api/settings", { method: "PUT", body: {
      proxies: readProxies(), listen_host: $("listen-all").checked ? "0.0.0.0" : "127.0.0.1",
      schema: $("schema-path").value.trim(), sqlfk_path: $("sqlfk-path").value.trim() } });
    showProblems(r.problems);
    if (r.ok) $("settings-dlg").close();
    loadStatus();
    loadTableNames();
  } catch (err) {
    showProblems([err.message]);
  } finally {
    btn.disabled = false;
    btn.textContent = "Save and apply";
  }
}

async function loadTableNames() {
  try {
    const s = await api("/api/schema");
    $("table-names").innerHTML = s.tables.map(t => `<option value="${esc(t)}">`).join("");
  } catch { /* no schema */ }
}

// ====================================================================== recording

// the innermost session being recorded (the queries go there), and the path that leads to it
const recPath = r => r.recording ? (r.path || [r.session]) : [];
const recordingId = r => r.recording ? recPath(r)[recPath(r).length - 1].id : null;
const isRecording = id => (state.recording.recording_ids || []).includes(id);

function setRecording(rec, switchView = true) {
  const was = recordingId(state.recording);
  state.recording = rec || { recording: false };
  const r = state.recording, on = r.recording;
  if (on) {                    // the same objects as the sidebar, so that the live counters are shared
    r.path = recPath(r).map(s => sessionById(s.id) || s);
    r.session = r.path[0];
  }
  $("rec").classList.toggle("on", on);
  $("rec-text").textContent = on ? "Stop recording" : "Start recording";
  $("rec").title = on ? "Stop everything that is being recorded" : "Start recording a new action";
  $("action").hidden = on;
  $("rec-live").hidden = !on;
  if (!on && was) $("action").value = "";
  if (on && switchView && was !== recordingId(r)) selectView(recordingId(r));
  renderRecording();
  renderSubControl();
  tick();
}

function renderRecording() {
  const r = state.recording;
  if (!r.recording) return;
  $("rec-path").innerHTML = recPath(r).map(s =>
    `<a data-view="${s.id}" class="${isRecording(s.id) ? "" : "closed"}" title="${isRecording(s.id) ? "being recorded" : "closed session: the sub-action is added to it"}">${esc(s.label)}</a>`).join("<span>›</span>");
}

// the session a new sub-action goes into: the innermost one being recorded, else the selected one
function subParent() {
  const r = state.recording;
  if (r.recording) return recPath(r)[recPath(r).length - 1];
  return typeof state.view === "number" ? sessionById(state.view) : null;
}

function renderSubControl() {
  const parent = subParent(), r = state.recording;
  $("sub-wrap").hidden = !parent;
  if (!parent) return;
  const live = r.recording;
  $("sub-action").placeholder = live
    ? `Sub-action inside “${parent.label}” — e.g. “Search”, “Add customer” — press Enter to start it`
    : `Add a sub-action to “${parent.label}” (it is recorded inside it) — e.g. “Search”`;
  // the innermost session can be stopped on its own when it is a sub-action
  const innermost = live ? recPath(r)[recPath(r).length - 1] : null;
  $("sub-stop").hidden = !(innermost && innermost.parent_id);
  if (innermost && innermost.parent_id) {
    const outer = sessionById(innermost.parent_id);
    $("sub-stop").textContent = `■ Stop “${innermost.label}”`;
    $("sub-stop").title = `Back to recording “${outer ? outer.label : "the session that contains it"}”`;
  }
}

function tick() {
  const r = state.recording;
  if (!r.recording) { $("rec-info").textContent = ""; return; }
  const s = recPath(r)[recPath(r).length - 1];
  $("rec-info").textContent = `${duration(Date.now() / 1000 - s.started_at)} · ${s.queries || 0} queries`;
}

async function startSub() {
  try {
    const parent = subParent();
    await api("/api/recording/sub/start", { method: "POST", body: { label: $("sub-action").value, parent_id: parent && parent.id } });
    $("sub-action").value = "";
    await Promise.all([loadSessions(), loadStatus()]);
  } catch (e) { alert(e.message); }
}

async function stopSub() {
  try {
    await api("/api/recording/sub/stop", { method: "POST" });
    await Promise.all([loadSessions(), loadStatus()]);
  } catch (e) { alert(e.message); }
}

async function toggleRecording() {
  try {
    if (state.recording.recording) {
      await api("/api/recording/stop", { method: "POST" });
    } else {
      await api("/api/recording/start", { method: "POST", body: { label: $("action").value } });
    }
    // the WebSocket "session" event updates the UI; refresh anyway in case it is disconnected
    await Promise.all([loadSessions(), loadStatus()]);
  } catch (e) { alert(e.message); }
}

// ====================================================================== sessions (a tree: sub-actions at any depth)

const childrenOf = id => state.sessions.filter(s => s.parent_id === id).sort((a, b) => a.id - b.id);
function ancestors(id) {
  const out = [];
  for (let s = sessionById(id); s && s.parent_id; s = sessionById(s.parent_id)) {
    const p = sessionById(s.parent_id);
    if (!p) break;
    out.unshift(p);
  }
  return out;
}
const descendants = id => childrenOf(id).flatMap(c => [c, ...descendants(c.id)]);
const pathLabel = id => [...ancestors(id), sessionById(id)].filter(Boolean).map(s => s.label).join(" › ");

async function loadSessions() {
  const data = await api("/api/sessions");
  state.sessions = data.sessions;
  setRecording(data.recording);
  renderSessions();
  renderSessionHead();
}

function renderSessions() {
  const counts = state.status ? state.status.counts : null;
  const items = [
    `<li class="special ${state.view === "all" ? "active" : ""}" data-view="all"><span class="name">⚡ Live — all queries</span><span class="n">${counts ? counts.total : ""}</span></li>`,
    `<li class="special ${state.view === "none" ? "active" : ""}" data-view="none"><span class="name">○ Outside sessions</span><span class="n">${counts ? counts.unassigned : ""}</span></li>`,
    `<li class="sep"></li>`,
  ];
  const item = (s, depth) => {
    const kids = childrenOf(s.id);
    const open = !state.collapsed.has(s.id);
    const caret = kids.length ? `<span class="caret" data-toggle="${s.id}" title="${open ? "Hide" : "Show"} the ${kids.length} sub-actions">${open ? "▾" : "▸"}</span>` : `<span class="caret none"></span>`;
    items.push(`<li class="node ${state.view === s.id ? "active" : ""} ${isRecording(s.id) ? "recording" : ""}" data-view="${s.id}" style="--depth:${depth}" title="${esc(pathLabel(s.id))}">
      ${caret}<span class="name">${esc(s.label)}</span><span class="n" title="queries (without driver noise)${s.errors ? ", errors" : ""}">${s.relevant}${s.errors ? ` · <span class="err-n">${s.errors}⚠</span>` : ""}</span>
      <span class="when">${dateTime(s.started_at)}${s.ended_at ? " · " + duration(s.ended_at - s.started_at) : ""}${kids.length ? ` · ${kids.length} sub-action${kids.length > 1 ? "s" : ""}` : ""}</span></li>`);
    if (open) kids.forEach(c => item(c, depth + 1));
  };
  state.sessions.filter(s => !s.parent_id || !sessionById(s.parent_id)).forEach(s => item(s, 0));
  if (!state.sessions.length) items.push(`<li class="hint">No sessions yet.<br>Write what you are about to do in the box at the top and press <b>Start recording</b>.</li>`);
  $("sessions").innerHTML = items.join("");
}

// the live counters are corrected from the server shortly after the queries (a session list loaded while
// they were arriving may be behind)
let sessionsRefreshTimer = null;
function scheduleSessionsRefresh() {
  if (sessionsRefreshTimer) return;
  sessionsRefreshTimer = setTimeout(() => { sessionsRefreshTimer = null; loadSessions(); }, 2000);
}

function selectView(view) {
  state.view = view === "all" || view === "none" ? view : Number(view);
  state.selected.clear();
  // a session inside a collapsed one becomes visible
  if (typeof state.view === "number") ancestors(state.view).forEach(a => state.collapsed.delete(a.id));
  renderSessions();
  renderSessionHead();
  renderSubControl();
  loadQueries();
  if (typeof state.view === "number") showOverview();
  else if (!state.current && !state.tablesPanel) $("detail").innerHTML = viewPlaceholder();
}

function viewPlaceholder() {
  return `<div class="placeholder"><p><b>${state.view === "none" ? "Queries captured outside any session" : "Every query, live"}</b></p>
    <p>Select a query to see its SQL, values and tables, a session on the left to see its tables and diagram,
       or <a class="link" data-show-tables>the tables of the profile</a>.</p></div>`;
}

function renderSessionHead() {
  const s = typeof state.view === "number" ? sessionById(state.view) : null;
  $("session-head").hidden = !s;
  $("clear-btn").hidden = typeof state.view === "number";
  if (!s) { state.sessionTablesFor = null; return; }
  if (document.activeElement !== $("session-label")) $("session-label").value = s.label;
  if (document.activeElement !== $("session-notes")) $("session-notes").value = s.notes || "";
  $("session-crumbs").innerHTML = ancestors(s.id).map(a => `<a data-view="${a.id}" title="Open “${esc(a.label)}”">${esc(a.label)}</a><span>›</span>`).join("");
  if (state.sessionTablesFor !== s.id) {
    state.sessionTablesFor = s.id;
    $("session-tables").innerHTML = "";
    loadSessionTables();
  }
}

// ---------------------------------------------------------------------- tables of the selected session

const writes = t => t.access.some(a => a !== "read");

function attrChips(t) {
  return t.columns.map(c => `<span class="attr ${c.usage.includes("write") ? "w" : c.usage.includes("filter") ? "f" : ""} ${c.in_schema || !t.in_schema ? "" : "unknown"}"
      title="${esc(c.column)}: ${esc(c.usage.join(", "))} · ${c.statements} statement${c.statements > 1 ? "s" : ""}${c.in_schema || !t.in_schema ? "" : " · not in the schema"}">${esc(c.column)}</span>`).join("")
    || `<span class="muted">—</span>`;
}

function tablesTable(tables, withSessions) {
  if (!tables.length) return `<p class="muted">No tables used yet.</p>`;
  return `<table class="kv tables-list"><tr><th>Table</th><th>Access</th><th class="num">Queries</th>${withSessions ? `<th class="num">Sessions</th>` : ""}<th>In schema?</th><th>Attributes used</th></tr>${
    tables.map(t => `<tr class="click" data-table="${esc(t.table)}" title="Show only the queries that use ${esc(t.table)}">
      <td><span class="tag ${writes(t) ? "w" : ""} ${t.in_schema ? "" : "unknown"}">${esc(t.table)}</span></td>
      <td>${esc(t.access.join(", "))}</td><td class="num">${t.statements}</td>${withSessions ? `<td class="num">${t.sessions}</td>` : ""}
      <td>${t.in_schema ? `<span class="yes">✓ yes</span>` : `<span class="no">✗ no</span>`}</td>
      <td class="attrs">${attrChips(t)}</td></tr>`).join("")}</table>
    <p class="legend-line"><span class="attr">read</span> selected <span class="attr f">filter</span> WHERE / JOIN <span class="attr w">write</span> INSERT / UPDATE · <i>italic</i> = not in the schema · click a row to see its queries</p>`;
}

async function loadSessionTables() {
  const id = state.view;
  if (typeof id !== "number") return;
  let data;
  try { data = await api(`/api/tables?session=${id}`); } catch { return; }
  if (state.view !== id) return;
  state.sessionTables = data;
  const box = $("session-tables");
  const filter = state.filters.table.toLowerCase();
  const subs = descendants(id).length;
  box.innerHTML = !data.tables.length ? `<span class="lbl">No tables used yet.</span>`
    : `<span class="lbl" title="${subs ? "including its sub-actions" : ""}">Tables${subs ? " (with sub-actions)" : ""}:</span>` + data.tables.map(t => {
      const tip = `${t.access.join(", ")} · ${t.statements} queries${t.in_schema ? "" : " · not in the schema"}\nattributes: ${t.columns.map(c => c.column).join(", ") || "—"}\nClick to show only its queries`;
      return `<span class="tag ${writes(t) ? "w" : ""} ${t.in_schema ? "" : "unknown"} ${t.table.toLowerCase() === filter ? "on" : ""}" data-table="${esc(t.table)}" title="${esc(tip)}">${esc(t.table)}<small>${t.statements}</small></span>`;
    }).join("") + (filter ? `<a class="link clear-filter" data-table="">✕ show all</a>` : "");
  if (state.overview === id) renderOverview();
}

let sessionTablesTimer = null;
function scheduleSessionTables() {
  if (sessionTablesTimer) return;
  sessionTablesTimer = setTimeout(() => { sessionTablesTimer = null; loadSessionTables(); }, 1000);
}

function setTableFilter(name) {
  state.filters.table = name;
  $("table-filter").value = name;
  loadQueries();
  loadSessionTables();
}

// ---------------------------------------------------------------------- overview of a session (detail pane)

function leaveDetailModes() {
  state.tablesPanel = false;
  state.overview = null;
  $("tables-btn").classList.remove("active");
  const prev = document.querySelector("#rows tr.sel");
  if (prev) prev.classList.remove("sel");
  state.current = null;
}

function showOverview() {
  if (typeof state.view !== "number") return;
  leaveDetailModes();
  state.overview = state.view;
  state.erKey = null;
  $("detail").innerHTML = `<div class="placeholder">Loading…</div>`;
  if (state.sessionTables && state.sessionTablesFor === state.view) renderOverview();
  loadSessionTables();
}

function renderOverview() {
  const s = sessionById(state.overview), data = state.sessionTables;
  if (!s || !data) return;
  const kids = childrenOf(s.id), all = descendants(s.id);
  const subList = kids.length ? `<h3>Sub-actions</h3><ul class="sub-list">${kids.map(c => `<li><a class="link" data-view="${c.id}">${esc(c.label)}</a>
      <span class="muted">${c.relevant} queries${childrenOf(c.id).length ? ` · ${childrenOf(c.id).length} sub-actions` : ""}</span></li>`).join("")}</ul>` : "";
  const inSchema = data.tables.filter(t => t.in_schema).length;
  const keep = $("detail").querySelector(".ov-er");          // keep the diagram (and its zoom) when nothing changed
  $("detail").innerHTML = `<div class="overview">
    <div class="ov-head"><span class="ov-kicker">${s.parent_id ? "Sub-action" : "Session"} #${s.id}</span>
      <div class="ov-title">${esc(pathLabel(s.id))}</div></div>
    <div class="stats">
      <div><b>${s.relevant}</b><span>queries</span></div>
      <div><b>${data.tables.length}</b><span>tables</span></div>
      <div><b>${data.tables.filter(writes).length}</b><span>written</span></div>
      <div><b>${all.length}</b><span>sub-actions</span></div>
      <div><b>${s.ended_at ? duration(s.ended_at - s.started_at) : isRecording(s.id) ? "REC" : "—"}</b><span>duration</span></div>
    </div>
    ${s.errors ? `<div class="error-box">${s.errors} queries ended with an error.</div>` : ""}
    ${all.length ? `<p class="note-box">The tables and the diagram include all its sub-actions.</p>` : ""}
    ${subList}
    <h3>Tables and attributes used <span class="muted">${data.unused ? `${inSchema} of ${data.schema_tables} schema tables` : ""}</span></h3>
    ${tablesTable(data.tables, false)}
    <h3>Diagram of the tables and their links</h3>
    <div class="ov-er-slot"></div>
    <div class="dl-row"><span class="muted">Download:</span>
      <button class="small" type="button" data-dl="tables-csv">Tables &amp; attributes .csv</button>
      <button class="small" type="button" data-dl="tables-md">.md</button>
      <button class="small" type="button" data-dl="er-html">Diagram .html</button>
      <button class="small" type="button" data-dl="queries-md">Queries .md</button></div>
  </div>`;
  const slot = $("detail").querySelector(".ov-er-slot");
  const key = data.tables.map(t => t.table + ":" + t.columns.length).join(",");
  if (keep && state.erKey === key) slot.replaceWith(keep);
  else {
    slot.outerHTML = erBox("ov-er");
    state.erKey = key;
    loadDiagram(`/api/er?session=${s.id}`, "ov-er", s.id);
  }
}

function erBox(cls) {
  return `<div class="${cls}"><div class="er-box big"><div class="er-canvas" style="height:100%"></div>
      <div class="er-tools"><button class="small" type="button" data-er-fit>Fit</button></div></div>
    <div class="er-legend"><span><i style="border-color:var(--primary)"></i>declared FK</span>
      <span><i style="border-color:var(--ok);border-top-style:dashed"></i>inferred FK</span>
      <span><i style="border-color:var(--warn);border-top-style:dotted"></i>join observed, not in the schema</span>
      <span>highlighted = attributes used · wheel = zoom · drag = move</span></div></div>`;
}

async function loadDiagram(url, cls, owner) {
  let er;
  try { er = await api(url); } catch { return; }
  const box = $("detail").querySelector(`.${cls} .er-canvas`);
  if (!box || (owner !== undefined && state.overview !== owner && owner !== "profile")) return;
  state.er = MiniER.render(box, er);
}

// ---------------------------------------------------------------------- tables of the profile

async function loadProfileTables() {
  let data;
  try { data = await api("/api/tables"); } catch { return; }
  state.tables = data;
  const used = data.tables.length;
  $("tables-count").textContent = data.unused ? `${used} used · ${data.unused.length} unused` : `${used} used`;
  if (state.tablesPanel) renderTablesPanel();
}

let profileTablesTimer = null;
function scheduleProfileTables() {
  if (profileTablesTimer) return;
  profileTablesTimer = setTimeout(() => { profileTablesTimer = null; loadProfileTables(); }, 1500);
}

function showTablesPanel() {
  leaveDetailModes();
  state.tablesPanel = true;
  state.erKey = null;
  $("tables-btn").classList.add("active");
  renderTablesPanel();
  loadProfileTables();
}

function renderTablesPanel() {
  const data = state.tables;
  if (!data) { $("detail").innerHTML = `<div class="placeholder">Loading…</div>`; return; }
  const profile = state.profiles && state.profiles.current;
  const inSchema = data.tables.filter(t => t.in_schema).length;
  const unused = data.unused == null
    ? `<p class="muted">No schema loaded: add one in ⚙ Settings to see which tables are never used.</p>`
    : data.unused.length ? `<ul class="unused-list">${data.unused.map(n => `<li>${esc(n)}</li>`).join("")}</ul>`
    : `<p class="muted">Every table of the schema has been used. 🎉</p>`;
  const keep = $("detail").querySelector(".pt-er");
  $("detail").innerHTML = `<div class="overview">
    <div class="ov-head"><span class="ov-kicker">Profile · updated live</span>
      <div class="ov-title">Tables${profile ? " of “" + esc(profile) + "”" : ""}</div></div>
    <div class="stats">
      <div><b>${data.tables.length}</b><span>used</span></div>
      ${data.unused ? `<div class="warn"><b>${data.unused.length}</b><span>never used</span></div>
      <div><b>${data.schema_tables}</b><span>in the schema</span></div>
      <div><b>${data.schema_tables ? Math.round(100 * inSchema / data.schema_tables) : 0}%</b><span>coverage</span></div>` : ""}
    </div>
    <h3>Never used${data.unused ? ` (${data.unused.length})` : ""}</h3>
    ${unused}
    <h3>Used (${data.tables.length})</h3>
    ${tablesTable(data.tables, true)}
    <h3>Diagram of the tables used and their links</h3>
    <div class="pt-er-slot"></div>
    <div class="dl-row"><span class="muted">Download:</span>
      <button class="small" type="button" data-pdl="tables-csv">Tables &amp; attributes .csv</button>
      <button class="small" type="button" data-pdl="tables-md">.md</button>
      <button class="small" type="button" data-pdl="er-html">Diagram .html</button></div>
  </div>`;
  const slot = $("detail").querySelector(".pt-er-slot");
  const key = data.tables.map(t => t.table + ":" + t.columns.length).join(",");
  if (keep && state.erKey === key) slot.replaceWith(keep);
  else {
    slot.outerHTML = erBox("pt-er");
    state.erKey = key;
    if (data.tables.length) loadDiagram("/api/er", "pt-er", "profile");
  }
}

// downloads: kind = queries-md | queries-json | tables-md | tables-csv | er-html; session = id or null (profile)
function downloadKind(kind, session) {
  const s = session != null ? `session=${session}&` : "";
  download({
    "queries-md": `/api/export?${session != null ? `sessions=${session}&` : ""}format=md`,
    "queries-json": `/api/export?${session != null ? `sessions=${session}&` : ""}format=json`,
    "tables-md": `/api/tables/export?${s}format=md`,
    "tables-csv": `/api/tables/export?${s}format=csv`,
    "er-html": `/api/er/export?${s}`.replace(/[?&]$/, ""),
  }[kind]);
}

async function patchSession(fields) {
  if (typeof state.view !== "number") return;
  try {
    const s = await api(`/api/sessions/${state.view}`, { method: "PATCH", body: fields });
    replaceSession(s);
  } catch (e) { alert(e.message); }
}

function replaceSession(s) {
  const i = state.sessions.findIndex(x => x.id === s.id);
  if (i >= 0) state.sessions[i] = s; else state.sessions.unshift(s);
  if (state.recording.recording) {
    state.recording.path = recPath(state.recording).map(x => x.id === s.id ? s : x);
    state.recording.session = state.recording.path[0];
    renderRecording();
  }
  renderSessions();
  renderSessionHead();
}

// ====================================================================== query grid

function queryParams() {
  const p = new URLSearchParams({ limit: MAX_ROWS });
  if (state.view !== "all") p.set("session", state.view);
  if (state.filters.q) p.set("q", state.filters.q);
  if (state.filters.type) p.set("type", state.filters.type);
  if (state.filters.table) p.set("table", state.filters.table);
  if (state.filters.fingerprint) p.set("fingerprint", state.filters.fingerprint);
  if (hideNoise()) p.set("noise", "0");
  return p;
}

let loadSeq = 0;
async function loadQueries() {
  const seq = ++loadSeq;
  const data = await api("/api/queries?" + queryParams());
  if (seq !== loadSeq) return;              // a newer reload is in flight
  state.rows = data.queries;
  state.byId = new Map(state.rows.map(r => [r.id, r]));
  const body = $("rows");
  body.innerHTML = "";
  const frag = document.createDocumentFragment();
  state.rows.forEach(r => frag.append(rowElement(r)));
  body.append(frag);
  updateCount();
  if ($("follow").checked) scrollToEnd();
}

function matches(r) {
  const f = state.filters;
  if (state.view === "none" && r.session_id != null) return false;
  if (typeof state.view === "number" && !inSession(r.session_id, state.view)) return false;
  if (hideNoise() && r.noise) return false;
  if (f.type && r.query_type !== f.type) return false;
  if (f.fingerprint && r.fingerprint !== f.fingerprint) return false;
  if (f.q && !(r.sql || "").toLowerCase().includes(f.q.toLowerCase()) && !(r.error || "").toLowerCase().includes(f.q.toLowerCase())) return false;
  if (f.table && !(r.tables || []).some(t => t.name.toLowerCase() === f.table.toLowerCase())) return false;
  return true;
}

function rowElement(r, fresh = false) {
  const tr = document.createElement("tr");
  tr.dataset.id = r.id;
  if (r.error) tr.classList.add("err");
  if (fresh) tr.classList.add("fresh");
  if (r.id === state.current) tr.classList.add("sel");
  const tables = (r.tables || []).map(t => `<span class="tag ${t.access !== "read" ? "w" : ""}" title="${esc(t.access)}">${esc(t.name)}</span>`).join("");
  const writes = ["INSERT", "REPLACE", "UPDATE", "DELETE", "MERGE"].includes(r.query_type);
  const rows = r.rows != null ? r.rows : r.affected != null && writes ? `<span title="rows affected">${r.affected}✎</span>` : "";
  const slow = r.duration_ms >= 100 ? "slow" : "";
  tr.innerHTML = `<td class="c-sel"><input type="checkbox" ${state.selected.has(r.id) ? "checked" : ""}></td>
    <td class="c-id">${r.id}</td><td class="c-time">${clock(r.ts)}</td><td class="c-conn" title="${esc(r.client || "")} ${esc(r.db_user || "")}@${esc(r.database || "")}">${r.conn_id}</td>
    <td class="c-type"><span class="badge ${esc(r.query_type || "")}">${esc(r.query_type || "?")}</span></td>
    <td class="c-tables">${tables}</td>
    <td class="c-stmt" title="${esc(r.error || "")}">${r.error ? "⚠ " : ""}${esc(oneLine(r.sql).slice(0, 300))}</td>
    <td class="c-num ${slow}">${ms(r.duration_ms)}</td><td class="c-num">${rows}</td>`;
  return tr;
}

function onQuery(r) {
  // counters
  if (state.status) {
    state.status.counts.total++;
    if (r.session_id == null) state.status.counts.unassigned++;
  }
  const s = sessionById(r.session_id);
  for (const x of s ? [s, ...ancestors(s.id)] : []) { x.queries++; if (!r.noise) x.relevant++; if (r.error) x.errors++; }
  if (r.session_id != null) scheduleSessionsRefresh();
  scheduleSidebar();
  if (r.tables && r.tables.length) scheduleProfileTables();
  if (typeof state.view === "number" && inSession(r.session_id, state.view) && r.tables && r.tables.length) scheduleSessionTables();
  if (!matches(r)) return;

  const wrap = $("grid-wrap");
  const atEnd = wrap.scrollHeight - wrap.scrollTop - wrap.clientHeight < 40;
  state.rows.push(r);
  state.byId.set(r.id, r);
  $("rows").append(rowElement(r, true));
  while (state.rows.length > MAX_ROWS) {
    const old = state.rows.shift();
    state.byId.delete(old.id);
    $("rows").firstElementChild.remove();
  }
  updateCount();
  if ($("follow").checked && atEnd) scrollToEnd();
}

let sidebarTimer = null;
function scheduleSidebar() {
  if (sidebarTimer) return;
  sidebarTimer = setTimeout(() => { sidebarTimer = null; renderSessions(); tick(); }, 300);
}

function scrollToEnd() {
  const wrap = $("grid-wrap");
  wrap.scrollTop = wrap.scrollHeight;
}

function updateCount() {
  $("count").textContent = `${state.rows.length}${state.rows.length >= MAX_ROWS ? "+" : ""} shown`;
  $("empty").hidden = state.rows.length > 0;
  const n = state.selected.size;
  $("group-btn").hidden = n === 0;
  $("group-btn").textContent = `Group ${n} selected into a session…`;
  const fp = $("fp-chip");
  fp.hidden = !state.filters.fingerprint;
  fp.innerHTML = state.filters.fingerprint ? `same statement as #${state.filters.fpFrom} <a class="link" id="fp-clear">✕</a>` : "";
}

function selectRow(id) {
  const prev = document.querySelector("#rows tr.sel");
  if (prev) prev.classList.remove("sel");
  const tr = document.querySelector(`#rows tr[data-id="${id}"]`);
  if (tr) {
    tr.classList.add("sel");
    tr.scrollIntoView({ block: "nearest" });
  }
  showDetail(id);
}

function toggleSelected(id, checked, range) {
  const ids = range && state.lastClicked != null ? rangeIds(state.lastClicked, id) : [id];
  ids.forEach(i => checked ? state.selected.add(i) : state.selected.delete(i));
  ids.forEach(i => {
    const box = document.querySelector(`#rows tr[data-id="${i}"] input`);
    if (box) box.checked = checked;
  });
  state.lastClicked = id;
  updateCount();
}

function rangeIds(a, b) {
  const ia = state.rows.findIndex(r => r.id === a), ib = state.rows.findIndex(r => r.id === b);
  if (ia < 0 || ib < 0) return [b];
  return state.rows.slice(Math.min(ia, ib), Math.max(ia, ib) + 1).map(r => r.id);
}

// ====================================================================== detail view

async function showDetail(id) {
  leaveDetailModes();
  state.current = id;
  let q;
  try {
    q = await api(`/api/queries/${id}`);
  } catch (e) {
    $("detail").innerHTML = `<div class="placeholder">${esc(e.message)}</div>`;
    return;
  }
  if (state.current !== id) return;
  const session = state.sessions.find(s => s.id === q.session_id);
  const facts = [
    `<span><b>${ms(q.duration_ms)}</b> ms</span>`,
    q.rows != null ? `<span><b>${q.rows}</b> rows returned</span>` : "",
    q.affected != null ? `<span><b>${q.affected}</b> rows affected</span>` : "",
    `<span>conn <b>#${q.conn_id}</b> ${esc(q.db_user || "")}${q.database ? "@" + esc(q.database) : ""}</span>`,
    `<span>${esc(q.client || "")}</span>`,
    `<span>${esc(q.kind === "execute" ? "prepared statement" : q.kind)}</span>`,
    `<span>${session ? "session <b>" + esc(session.label) + "</b>" : "outside sessions"}</span>`,
  ].filter(Boolean).join("");

  const params = q.params && q.params.length ? `<h3>Parameters</h3><table class="kv"><tr><th>#</th><th>Value</th></tr>${
    q.params.map((p, i) => `<tr><td>${dialectOf(q) === "postgres" ? "$" + (i + 1) : i + 1}</td><td class="mono">${p === null ? '<span class="null">NULL</span>' : esc(typeof p === "string" ? p : JSON.stringify(p))}</td></tr>`).join("")}</table>` : "";

  const columns = q.columns && q.columns.length ? `<h3>Columns used</h3><table class="kv"><tr><th>Table</th><th>Column</th><th>Used for</th></tr>${
    q.columns.map(c => `<tr><td>${esc(c.table || "?")}</td><td class="mono">${esc(c.column)}</td><td>${esc(c.usage.join(", "))}</td></tr>`).join("")}</table>` : "";

  const joins = q.joins && q.joins.length ? `<h3>Joins</h3><ul class="joins">${
    q.joins.map(j => `<li><code>${esc(j.left_table)}.${esc(j.left_column)} = ${esc(j.right_table)}.${esc(j.right_column)}</code> ${
      j.in_schema ? '<span class="muted">— in the schema</span>' : '<span class="cand">— not in the schema: candidate relationship</span>'}</li>`).join("")}</ul>` : "";

  const notes = [
    q.truncated ? "The statement was longer than the capture limit and has been truncated." : "",
    q.parse_error ? `sqlglot could not parse this statement (${esc(q.parse_error)}); tables and columns may be missing.` : "",
  ].filter(Boolean).map(n => `<div class="note-box">${n}</div>`).join("");

  const back = typeof state.view === "number" && sessionById(state.view)
    ? `<a class="link back" data-back-overview>← Overview of “${esc(sessionById(state.view).label)}”</a>` : "";
  $("detail").innerHTML = `${back}
    <div class="d-head"><span class="badge ${esc(q.query_type)}">${esc(q.query_type)}</span>
      <span class="big">#${q.id}</span><span class="muted">${dateTime(q.ts)}.${String(Math.round((q.ts % 1) * 1000)).padStart(3, "0")}</span></div>
    <div class="facts">${facts}</div>
    ${q.error ? `<div class="error-box">${esc(q.error)}</div>` : ""}
    ${notes}
    <div class="sql-bar">
      <div class="seg" id="sql-mode">
        <button data-mode="formatted">Formatted</button>
        <button data-mode="values" ${q.params && q.params.length ? "" : "disabled"}>With values</button>
        <button data-mode="raw">As sent</button>
      </div>
      <div><button class="small" id="copy-sql">Copy</button></div>
    </div>
    <pre class="sql" id="sql-text"></pre>
    ${params}
    <h3>Tables involved</h3>
    <div class="er-box"><div id="er" style="height:100%"></div>
      <div class="er-tools"><button class="small" data-er-fit title="Fit to view">Fit</button></div></div>
    <div class="er-legend"><span><i style="border-color:var(--primary)"></i>declared FK</span>
      <span><i style="border-color:var(--ok);border-top-style:dashed"></i>inferred FK</span>
      <span><i style="border-color:var(--warn);border-top-style:dotted"></i>join observed, not in schema</span>
      <span>R/W/D = read / write / delete · highlighted = used by the query</span></div>
    ${columns}
    ${joins}
    <h3>Statement</h3>
    <div class="facts"><span>fingerprint <code>${esc(q.fingerprint || "")}</code></span>
      <a class="link" id="same-fp">show every execution of this statement</a></div>`;

  state.detail = q;
  renderSql();
  state.er = MiniER.render($("er"), q.er || { tables: [] });
}

function sqlText(q, mode) {
  if (mode === "raw") return q.sql;
  const base = q.formatted || q.sql;
  return mode === "values" ? SqlHL.interpolate(base, q.params, dialectOf(q)) : base;
}

function renderSql() {
  const q = state.detail;
  if (!q) return;
  let mode = state.sqlMode;
  if (mode === "values" && !(q.params && q.params.length)) mode = "formatted";
  document.querySelectorAll("#sql-mode button").forEach(b => b.classList.toggle("on", b.dataset.mode === mode));
  $("sql-text").innerHTML = SqlHL.highlight(sqlText(q, mode));
}

// ====================================================================== WebSocket

let ws = null, wsDelay = 500;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => {
    wsDelay = 500;
    const c = $("ws-state");
    c.textContent = "live";
    c.className = "chip live";
    // catch up with whatever happened while disconnected
    loadStatus().then(loadSessions).then(loadQueries);
  };
  ws.onclose = () => {
    const c = $("ws-state");
    c.textContent = "offline — reconnecting";
    c.className = "chip bad";
    setTimeout(connect, wsDelay);
    wsDelay = Math.min(wsDelay * 2, 8000);
  };
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === "query") onQuery(m.query);
    else if (m.type === "session") onSessionEvent(m);
    else if (m.type === "connection") scheduleStatus();
    else if (m.type === "settings") { scheduleStatus(); scheduleProfileTables(); }
    else if (m.type === "profile") location.reload();
    else if (m.type === "cleared") { loadStatus().then(loadSessions); loadQueries(); loadProfileTables(); state.sessionTablesFor = null; }
  };
}

function onSessionEvent(m) {
  if (m.event === "cleared") {
    state.sessions = [];
    setRecording({ recording: false });
    selectView("all");
    loadStatus();
    return;
  }
  if (m.event === "deleted") {
    const gone = id => inSession(id, m.session.id);
    const viewGone = typeof state.view === "number" && gone(state.view);
    state.sessions = state.sessions.filter(s => !gone(s.id));
    if (viewGone) selectView("all");
    else renderSessions();
    scheduleProfileTables();
    return;
  }
  replaceSession(m.session);
  if (m.event === "started" || m.event === "stopped") loadSessions();      // recording state, with sub-actions
  if (m.event === "created") { selectView(m.session.id); scheduleProfileTables(); }
}

let statusTimer = null;
function scheduleStatus() {
  if (statusTimer) return;
  statusTimer = setTimeout(() => { statusTimer = null; loadStatus(); }, 250);
}

// ====================================================================== events

function bind() {
  $("rec").onclick = toggleRecording;
  $("action").addEventListener("keydown", e => { if (e.key === "Enter") toggleRecording(); });

  $("sessions").addEventListener("click", e => {
    const li = e.target.closest("li[data-view]");
    if (li) selectView(li.dataset.view);
  });

  let filterTimer = null;
  const refilter = () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadQueries, 200); };
  $("search").addEventListener("input", e => { state.filters.q = e.target.value.trim(); refilter(); });
  $("type-filter").addEventListener("change", e => { state.filters.type = e.target.value; loadQueries(); });
  $("table-filter").addEventListener("input", e => { state.filters.table = e.target.value.trim(); refilter(); scheduleSessionTables(); });
  $("hide-noise").addEventListener("change", loadQueries);
  $("follow").addEventListener("change", e => { if (e.target.checked) scrollToEnd(); });

  $("rows").addEventListener("click", e => {
    const tr = e.target.closest("tr[data-id]");
    if (!tr) return;
    const id = Number(tr.dataset.id);
    if (e.target.matches("input[type=checkbox]")) toggleSelected(id, e.target.checked, e.shiftKey);
    else selectRow(id);
  });
  $("sel-all").addEventListener("change", e => {
    state.rows.forEach(r => e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id));
    document.querySelectorAll("#rows input[type=checkbox]").forEach(b => b.checked = e.target.checked);
    updateCount();
  });
  $("grid-wrap").addEventListener("keydown", e => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
    e.preventDefault();
    const i = state.rows.findIndex(r => r.id === state.current);
    const next = state.rows[Math.max(0, Math.min(state.rows.length - 1, i < 0 ? state.rows.length - 1 : i + (e.key === "ArrowDown" ? 1 : -1)))];
    if (next) selectRow(next.id);
  });

  $("group-btn").onclick = async () => {
    const parent = typeof state.view === "number" ? sessionById(state.view) : null;
    const label = parent
      ? await askText("New sub-action", `Name of the sub-action of “${parent.label}” these ${state.selected.size} queries belong to:`)
      : await askText("Group selected queries", `Name of the UI action these ${state.selected.size} queries belong to:`);
    if (label === null) return;
    try {
      const s = await api("/api/sessions", { method: "POST", body: { label, query_ids: [...state.selected], parent_id: parent ? parent.id : null } });
      state.selected.clear();
      await loadStatus();
      replaceSession(s);
      selectView(s.id);
    } catch (e) { alert(e.message); }
  };
  $("clear-btn").onclick = async () => {
    if (!confirm("Delete every captured query that does not belong to a session?")) return;
    await api("/api/queries?session=none", { method: "DELETE" });
  };

  $("clear-all-btn").onclick = async () => {
    const n = state.status ? state.status.counts.total : 0;
    if (!n || !confirm(`Delete all ${n} captured queries, inside and outside sessions? This cannot be undone.\n\nThe sessions are kept (empty): use “Delete all” next to Sessions to remove them too.`)) return;
    await api("/api/queries?session=all", { method: "DELETE" });
    leaveDetailModes();
    $("detail").innerHTML = viewPlaceholder();
  };
  $("profile-select").addEventListener("change", changeProfile);

  $("session-label").addEventListener("change", e => patchSession({ label: e.target.value.trim() || "Untitled action" }));
  $("session-label").addEventListener("keydown", e => { if (e.key === "Enter") e.target.blur(); });
  $("session-notes").addEventListener("change", e => patchSession({ notes: e.target.value }));
  $("session-delete").onclick = async () => {
    const s = state.sessions.find(x => x.id === state.view);
    const subs = s ? descendants(s.id).length : 0;
    if (!s || !confirm(`Delete the ${s.parent_id ? "sub-action" : "session"} “${s.label}”${subs ? ` and its ${subs} sub-actions` : ""}? Its queries are kept and become “outside sessions”.`)) return;
    await api(`/api/sessions/${s.id}`, { method: "DELETE" });
    await loadStatus();
    selectView("all");
    loadSessions();
  };

  $("delete-all").onclick = async () => {
    const n = state.sessions.length;
    if (!n || !confirm(`Delete all ${n} sessions? This cannot be undone.`)) return;
    const queries = confirm("Also delete the queries they contain?\n\nOK = delete them too\nCancel = keep them as “outside sessions”");
    await api(`/api/sessions${queries ? "?queries=delete" : ""}`, { method: "DELETE" });
    await loadStatus();
    selectView("all");
    loadSessions();
  };

  // sub-actions
  $("sub-rec").onclick = startSub;
  $("sub-stop").onclick = stopSub;
  $("sub-action").addEventListener("keydown", e => { if (e.key === "Enter") startSub(); });

  // links to a session: recording path, breadcrumbs, sub-action list of the overview
  for (const box of ["rec-path", "session-crumbs"]) {
    $(box).addEventListener("click", e => { const a = e.target.closest("[data-view]"); if (a) selectView(a.dataset.view); });
  }
  $("sessions").addEventListener("click", e => {
    const caret = e.target.closest("[data-toggle]");
    if (caret) {
      e.stopPropagation();
      const id = Number(caret.dataset.toggle);
      state.collapsed.has(id) ? state.collapsed.delete(id) : state.collapsed.add(id);
      renderSessions();
    }
  }, true);
  $("session-tables").addEventListener("click", e => {
    const tag = e.target.closest("[data-table]");
    if (tag) setTableFilter(!tag.dataset.table || state.filters.table.toLowerCase() === tag.dataset.table.toLowerCase() ? "" : tag.dataset.table);
  });
  $("tables-btn").onclick = showTablesPanel;
  $("session-overview").onclick = showOverview;

  // download menus
  const menu = (button, list, session) => {
    $(button).onclick = e => {
      e.stopPropagation();
      const open = $(list).hidden;
      document.querySelectorAll(".menu").forEach(m => m.hidden = true);
      $(list).hidden = !open;
    };
    $(list).addEventListener("click", e => {
      const a = e.target.closest("a[data-dl]");
      if (a) downloadKind(a.dataset.dl, session());
      $(list).hidden = true;
    });
  };
  menu("export-all", "export-menu", () => null);
  menu("session-export", "session-export-menu", () => state.view);
  document.addEventListener("click", () => document.querySelectorAll(".menu").forEach(m => m.hidden = true));

  $("detail").addEventListener("click", e => {
    const dl = e.target.closest("[data-dl]");
    if (dl) { downloadKind(dl.dataset.dl, state.overview); return; }
    const pdl = e.target.closest("[data-pdl]");
    if (pdl) { downloadKind(pdl.dataset.pdl, null); return; }
    if (e.target.closest("[data-er-fit]")) { if (state.er) state.er.reset(); return; }
    if (e.target.closest("[data-show-tables]")) { showTablesPanel(); return; }
    if (e.target.closest("[data-back-overview]")) { showOverview(); return; }
    const link = e.target.closest("a[data-view]");
    if (link) { selectView(link.dataset.view); return; }
    const tr = e.target.closest(".tables-list tr[data-table]");
    if (tr) {
      if (state.tablesPanel && state.view !== "all") selectView("all");
      setTableFilter(tr.dataset.table);
      return;
    }
    const b = e.target.closest("#sql-mode button");
    if (b && !b.disabled) { state.sqlMode = b.dataset.mode; renderSql(); return; }
    if (e.target.id === "copy-sql") {
      navigator.clipboard.writeText(sqlText(state.detail, state.sqlMode === "values" && !(state.detail.params || []).length ? "formatted" : state.sqlMode))
        .then(() => { e.target.textContent = "Copied"; setTimeout(() => e.target.textContent = "Copy", 1200); });
    } else if (e.target.id === "same-fp") {
      state.filters.fingerprint = state.detail.fingerprint;
      state.filters.fpFrom = state.detail.id;
      loadQueries();
    }
  });
  document.querySelector(".toolbar").addEventListener("click", e => {
    if (e.target.id === "fp-clear") { state.filters.fingerprint = ""; loadQueries(); }
  });

  $("settings-btn").onclick = openSettings;
  document.addEventListener("click", e => { if (e.target.closest("[data-open-settings]")) openSettings(); });
  $("add-proxy").onclick = () => {
    const used = readProxies().map(p => p.listen_port);
    let port = 3307;
    while (used.includes(port)) port++;
    $("proxy-rows").append(...proxyRow({ protocol: "mysql", listen_port: port, target_host: "127.0.0.1", target_port: 3306 }));
  };
  $("settings-form").addEventListener("submit", saveSettings);
  $("settings-cancel").onclick = () => $("settings-dlg").close();
  // desktop app: native file/folder pickers
  if (window.sqlprofilerApp) {
    document.body.classList.add("desktop");
    document.querySelectorAll("[data-pick]").forEach(b => b.onclick = async () => {
      const chosen = await window.sqlprofilerApp.choosePath(b.dataset.kind);
      if (chosen) $(b.dataset.pick).value = chosen;
    });
  }

  setInterval(tick, 1000);
  setInterval(loadStatus, 10000);
}

bind();
loadProfiles();
loadTableNames();
loadProfileTables();
connect();
