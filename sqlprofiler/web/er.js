"use strict";
// Mini ER diagram of the tables involved in one query: each table is a box with its key columns and the
// columns the query uses; lines go from the foreign-key column to the referenced column.
// Referenced ("parent") tables are placed left of the tables that point to them. Wheel = zoom, drag = pan.

const MiniER = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const ROW = 18, HEAD = 24, PAD = 8, CHAR = 6.9, GAP_X = 90, GAP_Y = 26, MIN_W = 150;
  const norm = s => String(s || "").toLowerCase();

  const el = (tag, attrs = {}, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function measure(t) {
    const longest = Math.max(4, ...t.columns.map(c => c.name.length + 4 + Math.min((c.type || "").length, 18)));
    t.w = Math.max(MIN_W, Math.ceil(longest * CHAR) + PAD * 2 + 18, t.name.length * 7.6 + 46);
    t.h = HEAD + t.columns.length * ROW + (t.hidden ? ROW : 0) + 6;
  }

  /** Every edge as {parent, pcol, child, ccol, kind}: parents are the referenced tables. */
  function edges(er) {
    const out = [];
    for (const l of er.links || []) {
      l.from_cols.forEach((c, i) => out.push({ parent: norm(l.to), pcol: l.to_cols[i] || l.to_cols[0], child: norm(l.from),
                                               ccol: c, kind: l.kind === "declared" ? "declared" : "inferred" }));
    }
    const byName = Object.fromEntries(er.tables.map(t => [norm(t.name), t]));
    for (const j of er.observed || []) {
      // the parent is the side whose column is the whole primary key (then: part of it)
      const score = (t, col) => {
        if (!t) return 0;
        const pks = t.columns.filter(c => c.pk);
        const isPk = pks.some(c => norm(c.name) === norm(col));
        return isPk ? (pks.length === 1 ? 2 : 1) : 0;
      };
      const leftParent = score(byName[norm(j.left_table)], j.left_column) > score(byName[norm(j.right_table)], j.right_column);
      const [p, pc, ch, cc] = leftParent ? [j.left_table, j.left_column, j.right_table, j.right_column]
                                         : [j.right_table, j.right_column, j.left_table, j.left_column];
      out.push({ parent: norm(p), pcol: pc, child: norm(ch), ccol: cc, kind: "observed" });
    }
    return out.filter(e => byName[e.parent] && byName[e.child]);
  }

  function layout(tables, es) {
    const rank = Object.fromEntries(tables.map(t => [norm(t.name), 0]));
    for (let i = 0; i < tables.length; i++) {          // longest path, bounded so cycles terminate
      let changed = false;
      for (const e of es) {
        if (e.parent !== e.child && rank[e.child] < rank[e.parent] + 1 && rank[e.parent] + 1 < tables.length) {
          rank[e.child] = rank[e.parent] + 1;
          changed = true;
        }
      }
      if (!changed) break;
    }
    const cols = [];
    tables.forEach(t => (cols[rank[norm(t.name)]] ||= []).push(t));
    const heights = cols.map(c => (c || []).reduce((s, t) => s + t.h + GAP_Y, -GAP_Y));
    const maxH = Math.max(...heights, 0);
    let x = 0;
    cols.forEach((col, i) => {
      if (!col) return;
      let y = (maxH - heights[i]) / 2;
      const w = Math.max(...col.map(t => t.w));
      col.forEach(t => { t.x = x; t.y = y; y += t.h + GAP_Y; });
      x += w + GAP_X;
    });
    return { width: Math.max(x - GAP_X, 1), height: Math.max(maxH, 1) };
  }

  function rowY(t, column) {
    const i = t.columns.findIndex(c => norm(c.name) === norm(column));
    return t.y + (i < 0 ? HEAD / 2 : HEAD + i * ROW + ROW / 2 + 2);
  }

  function drawTable(t) {
    const g = el("g", { class: "er-table" + (t.known ? "" : " unknown"), transform: `translate(${t.x},${t.y})` });
    g.append(el("rect", { class: "body", width: t.w, height: t.h, rx: 5 }));
    g.append(el("rect", { class: "head", width: t.w, height: HEAD, rx: 5 }));
    g.append(el("rect", { class: "head", y: HEAD - 6, width: t.w, height: 6 }));
    const title = el("text", { class: "title", x: PAD, y: 16 }, t.name);
    g.append(title);
    const acc = (t.access || "").split("/").map(a => ({ read: "R", write: "W", delete: "D" }[a] || "")).join("");
    if (acc) g.append(el("text", { class: "acc", x: t.w - PAD, y: 16, "text-anchor": "end" }, acc));
    const tip = el("title", {}, `${t.name}${t.known ? "" : " (not in the loaded schema)"}\naccess: ${t.access}`);
    g.append(tip);
    t.columns.forEach((c, i) => {
      const y = HEAD + i * ROW + 2;
      if (c.used && c.used.length) {
        g.append(el("rect", { class: "used" + (c.used.includes("write") ? " write" : ""), x: 1, y, width: t.w - 2, height: ROW }));
      }
      const key = [c.pk ? "PK" : "", c.fk ? "FK" : ""].filter(Boolean).join(" ");
      if (key) g.append(el("text", { class: "key", x: PAD, y: y + 13 }, key));
      const name = el("text", { x: PAD + 30, y: y + 13, class: c.used && c.used.length ? "used" : "" }, c.name);
      const ct = el("title", {}, `${c.name} ${c.type || ""}${c.used && c.used.length ? "\nused for: " + c.used.join(", ") : ""}` +
                                (c.refs && c.refs.length ? "\n→ " + c.refs.join(", ") : ""));
      name.append(ct);
      g.append(name);
      if (c.type) g.append(el("text", { class: "type", x: t.w - PAD, y: y + 13, "text-anchor": "end" },
                                 c.type.length > 18 ? c.type.slice(0, 17) + "…" : c.type));
    });
    if (t.hidden) {
      g.append(el("text", { class: "more", x: PAD + 30, y: HEAD + t.columns.length * ROW + 15 },
                  `+ ${t.hidden} more column${t.hidden > 1 ? "s" : ""}`));
    }
    return g;
  }

  function drawEdge(e, byName) {
    const p = byName[e.parent], c = byName[e.child];
    const y1 = rowY(p, e.pcol), y2 = rowY(c, e.ccol);
    let d;
    if (p === c) {                                       // self reference: loop on the right side
      const x = p.x + p.w;
      d = `M${x},${y1} C${x + 40},${y1} ${x + 40},${y2} ${x},${y2}`;
    } else if (p.x + p.w <= c.x) {                        // parent left of child
      const x1 = p.x + p.w, x2 = c.x, mid = (x2 - x1) / 2;
      d = `M${x1},${y1} C${x1 + mid},${y1} ${x2 - mid},${y2} ${x2},${y2}`;
    } else {                                             // same column: route around the right side
      const x1 = p.x + p.w, x2 = c.x + c.w, x = Math.max(x1, x2) + 36;
      d = `M${x1},${y1} C${x},${y1} ${x},${y2} ${x2},${y2}`;
    }
    const path = el("path", { class: `er-link ${e.kind}`, d, "marker-start": `url(#er-one-${e.kind})` });
    path.append(el("title", {}, `${c.name}.${e.ccol} → ${p.name}.${e.pcol}\n${
      { declared: "declared foreign key", inferred: "foreign key inferred by sql-fk-finder", observed: "join observed in this query (not in the schema)" }[e.kind]}`));
    return path;
  }

  function render(container, er) {
    container.innerHTML = "";
    const tables = (er.tables || []).map(t => ({ ...t, columns: t.columns || [] }));
    if (!tables.length) {
      container.innerHTML = '<div class="placeholder">No tables in this statement.</div>';
      return;
    }
    tables.forEach(measure);
    const es = edges({ ...er, tables });
    const size = layout(tables, es);
    const byName = Object.fromEntries(tables.map(t => [norm(t.name), t]));

    const margin = 30;
    const svg = el("svg", { xmlns: NS });
    const defs = el("defs");
    for (const kind of ["declared", "inferred", "observed"]) {
      const m = el("marker", { id: `er-one-${kind}`, viewBox: "0 0 10 10", refX: 1, refY: 5, markerWidth: 8, markerHeight: 8, orient: "auto" });
      m.append(el("path", { d: "M2,0 L2,10", class: `er-link ${kind}` }));
      defs.append(m);
    }
    svg.append(defs);
    const world = el("g");
    es.forEach(e => world.append(drawEdge(e, byName)));
    tables.forEach(t => world.append(drawTable(t)));
    svg.append(world);
    container.append(svg);

    // view box: fit, then wheel zoom and drag pan
    const fit = { x: -margin, y: -margin, w: size.width + 2 * margin + 40, h: size.height + 2 * margin };
    // never enlarge small diagrams (one or two tables) beyond their natural size
    const box = container.getBoundingClientRect();
    for (const [len, pos, min] of [["w", "x", box.width || 600], ["h", "y", box.height || 300]]) {
      if (fit[len] < min) {
        fit[pos] -= (min - fit[len]) / 2;
        fit[len] = min;
      }
    }
    let vb = { ...fit };
    const apply = () => svg.setAttribute("viewBox", `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
    apply();
    svg.addEventListener("wheel", ev => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect();
      const k = ev.deltaY > 0 ? 1.15 : 1 / 1.15;
      const scale = Math.max(vb.w / r.width, vb.h / r.height);
      // keep the point under the mouse fixed (approximately, with the preserveAspectRatio centring)
      const mx = vb.x + (ev.clientX - r.left - (r.width - vb.w / scale) / 2) * scale;
      const my = vb.y + (ev.clientY - r.top - (r.height - vb.h / scale) / 2) * scale;
      vb = { x: mx - (mx - vb.x) * k, y: my - (my - vb.y) * k, w: vb.w * k, h: vb.h * k };
      apply();
    }, { passive: false });
    let drag = null;
    svg.addEventListener("pointerdown", ev => { drag = { x: ev.clientX, y: ev.clientY, vb: { ...vb } }; svg.classList.add("drag"); svg.setPointerCapture(ev.pointerId); });
    svg.addEventListener("pointermove", ev => {
      if (!drag) return;
      const r = svg.getBoundingClientRect();
      const scale = Math.max(vb.w / r.width, vb.h / r.height);
      vb = { ...vb, x: drag.vb.x - (ev.clientX - drag.x) * scale, y: drag.vb.y - (ev.clientY - drag.y) * scale };
      apply();
    });
    const end = () => { drag = null; svg.classList.remove("drag"); };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    return { reset: () => { vb = { ...fit }; apply(); } };
  }

  return { render };
})();
