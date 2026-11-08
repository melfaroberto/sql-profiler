"use strict";
// Mini ER diagram of the tables involved in one query: each table is a box with its key columns and the
// columns the query uses; lines go from the foreign-key column to the referenced column.
// Referenced ("parent") tables are placed left of the tables that point to them. Wheel = zoom, drag = pan.
// The ends of the foreign keys use the crow's foot notation: on the referenced side || (mandatory) or o| (optional),
// on the side of the foreign key a crow's foot (many) or a bar (one: the key is unique or the whole primary key).
// With opts.interactive the tables can be dragged and clicked (the one clicked and its neighbours are highlighted).

const MiniER = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const ROW = 18, HEAD = 24, PAD = 8, CHAR = 6.9, GAP_X = 90, GAP_Y = 26, MIN_W = 150;
  const norm = s => String(s || "").toLowerCase();
  let drawings = 0;

  const el = (tag, attrs = {}, text) => {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined) n.setAttribute(k, v);
    if (text !== undefined) n.textContent = text;
    return n;
  };

  function measure(t) {
    const longest = Math.max(4, ...t.columns.map(c => c.name.length + 4 + Math.min((c.type || "").length, 18)));
    t.w = Math.max(MIN_W, Math.ceil(longest * CHAR) + PAD * 2 + 18, t.name.length * 7.6 + 46 + (t.badge || "").length * 6,
                   (t.note || "").length * 6.4 + PAD * 2);
    t.h = HEAD + t.columns.length * ROW + (t.hidden ? ROW : 0) + (t.note ? ROW : 0) + 6;
    if (t.columns.some(c => c.valueText)) {            // name, value, type: the value gets a column of its own
      t.valueX = PAD + 30 + Math.max(...t.columns.map(c => c.name.length)) * CHAR + 14;
      const value = Math.max(...t.columns.map(c => (c.valueText || "").length)) * CHAR;
      const type = Math.max(...t.columns.map(c => Math.min((c.type || "").length, 18))) * 6.4;
      t.w = Math.max(t.w, Math.ceil(t.valueX + value + 14 + type + PAD));
    }
  }

  /** Cardinality of a foreign key made of the columns cols of table t: {many, mandatory}. */
  function cardinality(t, cols) {
    if (!t) return { many: true, mandatory: false };
    const found = cols.map(n => t.columns.find(c => norm(c.name) === norm(n)));
    const pk = t.columns.filter(c => c.pk).map(c => norm(c.name)).sort().join(",");
    const one = (found.length === 1 && found[0] && found[0].unique) || (pk && cols.map(norm).sort().join(",") === pk);
    return { many: !one, mandatory: found.every(c => c && c.not_null) };
  }

  /** Every edge as {parent, pcol, child, ccol, kind, many, mandatory}: parents are the referenced tables. */
  function edges(er) {
    const out = [];
    const byName = Object.fromEntries(er.tables.map(t => [norm(t.name), t]));
    for (const l of er.links || []) {
      const card = cardinality(byName[norm(l.from)], l.from_cols);
      l.from_cols.forEach((c, i) => out.push({ parent: norm(l.to), pcol: l.to_cols[i] || l.to_cols[0], child: norm(l.from),
                                               ccol: c, kind: l.kind === "declared" ? "declared" : l.kind === "inferred-media" ? "possible" : "inferred",
                                               ...card }));
    }
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

  // For many tables: each group of connected tables is laid out by dagre, then the groups are placed in rows that
  // give the picture about the shape of a screen. Needs dagre to be loaded (the log diagram does).
  function graphLayout(tables, es) {
    const parent = Object.fromEntries(tables.map(t => [norm(t.name), norm(t.name)]));
    const find = k => parent[k] === k ? k : (parent[k] = find(parent[k]));
    es.forEach(e => { parent[find(e.child)] = find(e.parent); });
    const groups = new Map();
    tables.forEach(t => {
      const k = find(norm(t.name));
      groups.set(k, [...(groups.get(k) || []), t]);
    });
    const boxes = [...groups.entries()].map(([k, members]) => {
      const g = new dagre.graphlib.Graph({ multigraph: true });
      g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 110, edgesep: 12, marginx: 0, marginy: 0 });
      g.setDefaultEdgeLabel(() => ({}));
      members.forEach(t => g.setNode(norm(t.name), { width: t.w, height: t.h }));
      es.forEach((e, i) => { if (e.parent !== e.child && find(e.parent) === k) g.setEdge(e.parent, e.child, {}, `e${i}`); });
      dagre.layout(g);
      members.forEach(t => { const n = g.node(norm(t.name)); t.x = n.x - t.w / 2; t.y = n.y - t.h / 2; });
      const x0 = Math.min(...members.map(t => t.x)), y0 = Math.min(...members.map(t => t.y));
      members.forEach(t => { t.x -= x0; t.y -= y0; });
      return { members, w: Math.max(...members.map(t => t.x + t.w)), h: Math.max(...members.map(t => t.y + t.h)) };
    }).sort((a, b) => b.h - a.h || b.w - a.w);
    const area = boxes.reduce((s, b) => s + (b.w + GAP_X) * (b.h + GAP_Y), 0);
    const limit = Math.max(Math.sqrt(area * 1.6), ...boxes.map(b => b.w));
    let x = 0, y = 0, rowH = 0, width = 0;
    boxes.forEach(b => {
      if (x > 0 && x + b.w > limit) { x = 0; y += rowH + GAP_X; rowH = 0; }
      b.members.forEach(t => { t.x += x; t.y += y; });
      width = Math.max(width, x + b.w);
      rowH = Math.max(rowH, b.h);
      x += b.w + GAP_X;
    });
    return { width: Math.max(width, 1), height: Math.max(y + rowH, 1) };
  }

  function rowY(t, column) {
    const i = t.columns.findIndex(c => norm(c.name) === norm(column));
    return t.y + (i < 0 ? HEAD / 2 : HEAD + i * ROW + ROW / 2 + 2);
  }

  function drawTable(t) {
    const g = el("g", { class: "er-table" + (t.known ? "" : " unknown"), transform: `translate(${t.x},${t.y})`, "data-name": norm(t.name) });
    g.append(el("rect", { class: "body", width: t.w, height: t.h, rx: 5 }));
    g.append(el("rect", { class: "head", width: t.w, height: HEAD, rx: 5 }));
    g.append(el("rect", { class: "head", y: HEAD - 6, width: t.w, height: 6 }));
    const title = el("text", { class: "title", x: PAD, y: 16 }, t.name);
    g.append(title);
    const acc = (t.access || "").split("/").map(a => ({ read: "R", write: "W", delete: "D" }[a] || "")).join("");
    if (acc || t.badge) g.append(el("text", { class: "acc", x: t.w - PAD, y: 16, "text-anchor": "end" }, t.badge || acc));
    const tip = el("title", {}, `${t.name}${t.known ? "" : " (not in the loaded schema)"}${t.access ? "\naccess: " + t.access : ""}`);
    g.append(tip);
    t.columns.forEach((c, i) => {
      const y = HEAD + i * ROW + 2;
      if (c.used && c.used.length) {
        g.append(el("rect", { class: "used" + (c.used.includes("write") ? " write" : c.used.includes("filter") && !c.used.includes("read") ? " filter" : ""), x: 1, y, width: t.w - 2, height: ROW }));
      }
      const key = [c.pk ? "PK" : "", c.fk ? "FK" : ""].filter(Boolean).join(" ");
      if (key) g.append(el("text", { class: "key", x: PAD, y: y + 13 }, key));
      const name = el("text", { x: PAD + 30, y: y + 13, class: c.used && c.used.length ? "used" : t.mute ? "unused" : "" }, c.name);
      const ct = el("title", {}, `${c.name} ${c.type || ""}${c.used && c.used.length ? "\nused for: " + c.used.join(", ") : ""}` +
                                (c.refs && c.refs.length ? "\n→ " + c.refs.join(", ") : ""));
      name.append(ct);
      g.append(name);
      if (c.valueText) {
        const value = el("text", { class: "value" + (c.valueText === "NULL" ? " null" : "") + (c.valueClass ? " " + c.valueClass : ""), x: t.valueX, y: y + 13 }, c.valueText);
        if (c.valueTip) value.append(el("title", {}, c.valueTip));
        g.append(value);
      }
      if (c.type) g.append(el("text", { class: "type", x: t.w - PAD, y: y + 13, "text-anchor": "end" },
                                 c.type.length > 18 ? c.type.slice(0, 17) + "…" : c.type));
    });
    if (t.hidden) {
      g.append(el("text", { class: "more", x: PAD + 30, y: HEAD + t.columns.length * ROW + 15 },
                  `+ ${t.hidden} more column${t.hidden > 1 ? "s" : ""}`));
    }
    if (t.note) {
      g.append(el("text", { class: "note", x: PAD, y: HEAD + t.columns.length * ROW + 15 }, t.note));
    }
    return g;
  }

  /** One table on its own, as an <svg> element for a page. */
  function tableBox(table) {
    const t = { ...table, columns: table.columns || [], x: 0, y: 0 };
    measure(t);
    const svg = el("svg", { xmlns: NS, width: t.w + 2, height: t.h + 2, viewBox: `-1 -1 ${t.w + 2} ${t.h + 2}`, class: "er-single" });
    svg.append(drawTable(t));
    return svg;
  }

  function edgePath(e, byName) {
    const p = byName[e.parent], c = byName[e.child];
    const y1 = rowY(p, e.pcol), y2 = rowY(c, e.ccol);
    let d;
    if (p === c) {                                       // self reference: loop on the right side
      const x = p.x + p.w;
      d = `M${x},${y1} C${x + 40},${y1} ${x + 40},${y2} ${x},${y2}`;
    } else if (p.x + p.w <= c.x) {                        // parent left of child
      const x1 = p.x + p.w, x2 = c.x, mid = (x2 - x1) / 2;
      d = `M${x1},${y1} C${x1 + mid},${y1} ${x2 - mid},${y2} ${x2},${y2}`;
    } else if (c.x + c.w <= p.x) {                        // parent right of child (a cycle)
      const x1 = p.x, x2 = c.x + c.w, mid = (x1 - x2) / 2;
      d = `M${x1},${y1} C${x1 - mid},${y1} ${x2 + mid},${y2} ${x2},${y2}`;
    } else {                                             // same column: route around the right side
      const x1 = p.x + p.w, x2 = c.x + c.w, x = Math.max(x1, x2) + 36;
      d = `M${x1},${y1} C${x},${y1} ${x},${y2} ${x2},${y2}`;
    }
    return d;
  }

  // ids: every drawing on the page has its own markers (url(#id) would pick those of a hidden drawing)
  function drawEdge(e, byName, ids = "") {
    const p = byName[e.parent], c = byName[e.child];
    const ends = e.kind === "observed" ? { "marker-start": `url(#${ids}er-one-${e.kind})` }
      : { "marker-start": `url(#${ids}er-${e.mandatory ? "p1" : "p0"}-${e.kind})`, "marker-end": `url(#${ids}er-${e.many ? "cn" : "c1"}-${e.kind})` };
    const path = el("path", { class: `er-link ${e.kind}`, d: edgePath(e, byName), ...ends });
    const relation = e.kind === "observed" ? "" : `\n${e.many ? "1:N" : "1:1"} · ${e.mandatory ? "mandatory" : "optional"}`;
    path.append(el("title", {}, `${c.name}.${e.ccol} → ${p.name}.${e.pcol}\n${
      { declared: "declared foreign key", inferred: "foreign key inferred from the names", possible: "possible foreign key: the column name points to this table", observed: "join observed in this query (not in the schema)" }[e.kind]}${relation}`));
    return path;
  }

  // the ends of the lines, in the direction of the line: the referenced side starts at x = 0, the other ends at x = 16
  const ENDS = {
    one: { ref: 1, shapes: [["path", { d: "M2,1 L2,11" }]] },
    p1: { ref: 0, shapes: [["path", { d: "M4,1 L4,11 M8,1 L8,11" }]] },                               // exactly one
    p0: { ref: 0, shapes: [["path", { d: "M4,1 L4,11" }], ["circle", { cx: 10.5, cy: 6, r: 3, class: "hole" }]] },  // zero or one
    cn: { ref: 16, shapes: [["path", { d: "M3,6 L16,1 M3,6 L16,6 M3,6 L16,11" }]] },                // many
    c1: { ref: 16, shapes: [["path", { d: "M11,1 L11,11" }]] },                                     // one
  };

  function markers(ids = "") {
    const defs = el("defs");
    for (const kind of ["declared", "inferred", "possible", "observed"]) {
      for (const [name, end] of Object.entries(ENDS)) {
        const m = el("marker", { id: `${ids}er-${name}-${kind}`, viewBox: "0 0 16 12", refX: end.ref, refY: 6, markerWidth: 16, markerHeight: 12,
                                 markerUnits: "userSpaceOnUse", orient: "auto" });
        end.shapes.forEach(([tag, attrs]) => m.append(el(tag, { ...attrs, class: `er-mark ${kind} ${attrs.class || ""}` })));
        defs.append(m);
      }
    }
    return defs;
  }

  /** The tables with only their key columns and the ones used (the others are counted in "more columns"). */
  function compact(er) {
    return { ...er, tables: (er.tables || []).map(t => {
      const shown = (t.columns || []).filter(c => c.pk || c.fk || (c.used && c.used.length));
      return { ...t, columns: shown, hidden: (t.hidden || 0) + (t.columns || []).length - shown.length };
    }) };
  }

  /** Places the tables at the saved positions ({name: {x, y}}); the ones without a position go below. */
  function place(tables, es, positions) {
    const missing = tables.filter(t => !positions[norm(t.name)]);
    let size = { width: 0, height: 0 };
    if (missing.length) size = graphLayout(missing, es.filter(e => missing.some(t => norm(t.name) === e.parent) && missing.some(t => norm(t.name) === e.child)));
    const placed = tables.filter(t => positions[norm(t.name)]);
    placed.forEach(t => { t.x = positions[norm(t.name)].x; t.y = positions[norm(t.name)].y; });
    if (placed.length && missing.length) {
      const bottom = Math.max(...placed.map(t => t.y + t.h)) + GAP_X, left = Math.min(...placed.map(t => t.x));
      missing.forEach(t => { t.x += left; t.y += bottom; });
    }
    return bounds(tables, size);
  }

  function bounds(tables) {
    if (!tables.length) return { x: 0, y: 0, width: 1, height: 1 };
    const x = Math.min(...tables.map(t => t.x)), y = Math.min(...tables.map(t => t.y));
    return { x, y, width: Math.max(...tables.map(t => t.x + t.w)) - x, height: Math.max(...tables.map(t => t.y + t.h)) - y };
  }

  // opts.graph: lay out with dagre (see graphLayout); opts.positions: {name: {x, y}} kept from a previous drawing;
  // opts.interactive: tables can be dragged and selected, opts.onSelect(name or null) is told
  function render(container, er, opts = {}) {
    container.innerHTML = "";
    const tables = (er.tables || []).map(t => ({ ...t, columns: t.columns || [] }));
    if (!tables.length) {
      container.innerHTML = `<div class="placeholder">${opts.empty || "No tables in this statement."}</div>`;
      return { reset() {}, positions: () => ({}), select() {} };
    }
    tables.forEach(measure);
    const es = edges({ ...er, tables });
    if (opts.positions) place(tables, es, opts.positions);
    else if (opts.graph) graphLayout(tables, es);
    else layout(tables, es);
    const byName = Object.fromEntries(tables.map(t => [norm(t.name), t]));

    const margin = 30;
    const ids = `d${++drawings}-`;
    const svg = el("svg", { xmlns: NS });
    svg.append(markers(ids));
    const world = el("g");
    const paths = es.map(e => { const p = drawEdge(e, byName, ids); world.append(p); return p; });
    const groups = {};
    tables.forEach(t => { groups[norm(t.name)] = drawTable(t); world.append(groups[norm(t.name)]); });
    svg.append(world);
    container.append(svg);

    // view box: fit, then wheel zoom and drag pan
    const fitBox = () => {
      const b = bounds(tables);
      const fit = { x: b.x - margin, y: b.y - margin, w: b.width + 2 * margin + 40, h: b.height + 2 * margin };
      // never enlarge small diagrams (one or two tables) beyond their natural size
      const box = container.getBoundingClientRect();
      for (const [len, pos, min] of [["w", "x", box.width || 600], ["h", "y", box.height || 300]]) {
        if (fit[len] < min) {
          fit[pos] -= (min - fit[len]) / 2;
          fit[len] = min;
        }
      }
      return fit;
    };
    let vb = fitBox();
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
    let drag = null, selected = null;
    const select = name => {
      selected = name && byName[norm(name)] ? norm(name) : null;
      const near = new Set(selected ? [selected] : []);
      es.forEach((e, i) => {
        const on = selected && (e.parent === selected || e.child === selected);
        if (on) { near.add(e.parent); near.add(e.child); }
        paths[i].classList.toggle("near", !!on);
      });
      svg.classList.toggle("has-sel", !!selected);
      for (const [k, g] of Object.entries(groups)) {
        g.classList.toggle("sel", k === selected);
        g.classList.toggle("near", near.has(k) && k !== selected);
      }
      if (opts.onSelect) opts.onSelect(selected ? byName[selected].name : null);
    };
    svg.addEventListener("pointerdown", ev => {
      if (ev.button !== 0) return;
      const g = opts.interactive && ev.target.closest(".er-table");
      const t = g && byName[g.dataset.name];
      drag = { x: ev.clientX, y: ev.clientY, vb: { ...vb }, table: t, tx: t && t.x, ty: t && t.y, moved: false };
      if (t) world.append(g);                            // on top of the others while it moves
      svg.classList.add("drag");
      svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener("pointermove", ev => {
      if (!drag) return;
      if (Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 3) drag.moved = true;
      if (!drag.moved) return;
      const r = svg.getBoundingClientRect();
      const scale = Math.max(vb.w / r.width, vb.h / r.height);
      if (drag.table) {
        const t = drag.table, k = norm(t.name);
        t.x = drag.tx + (ev.clientX - drag.x) * scale;
        t.y = drag.ty + (ev.clientY - drag.y) * scale;
        groups[k].setAttribute("transform", `translate(${t.x},${t.y})`);
        es.forEach((e, i) => { if (e.parent === k || e.child === k) paths[i].setAttribute("d", edgePath(e, byName)); });
        return;
      }
      vb = { ...vb, x: drag.vb.x - (ev.clientX - drag.x) * scale, y: drag.vb.y - (ev.clientY - drag.y) * scale };
      apply();
    });
    const end = ev => {
      if (drag && !drag.moved && opts.interactive && ev.type === "pointerup") {
        const k = drag.table && norm(drag.table.name);
        select(k && k !== selected ? k : null);
      }
      if (drag && drag.moved && drag.table && opts.onMove) opts.onMove(drag.table.name);
      drag = null;
      svg.classList.remove("drag");
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    return {
      reset: () => { vb = fitBox(); apply(); },
      positions: () => Object.fromEntries(tables.map(t => [norm(t.name), { x: t.x, y: t.y }])),
      select,
      // centres the view on a table, at a readable zoom
      focus(name) {
        const t = byName[norm(name)];
        if (!t) return;
        const r = svg.getBoundingClientRect();
        const w = Math.max(vb.w, 1), h = Math.max(vb.h, 1);
        const k = Math.min(1, Math.max((r.width || 600) * 1.1 / w, 0.25));   // zoom in up to about 1:1
        const nw = w * k, nh = h * k;
        vb = { x: t.x + t.w / 2 - nw / 2, y: t.y + t.h / 2 - nh / 2, w: nw, h: nh };
        apply();
        select(name);
      },
    };
  }

  // ---------------------------------------------------------------------- the diagram as an image file

  // fixed light colours: the file must look the same whatever the theme of the screen
  const IMAGE_CSS = `
    text { font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
    .er-table rect.body { fill: #fff; stroke: #c9ced6; } .er-table rect.head { fill: #1f4e79; }
    .er-table.unknown rect.head { fill: #6b7480; }
    .er-table text { font-family: Consolas, Menlo, "DejaVu Sans Mono", monospace; font-size: 11.5px; fill: #1d2330; }
    .er-table text.title { fill: #fff; font-weight: 700; font-family: "Segoe UI", Roboto, Helvetica, Arial, sans-serif; font-size: 12px; }
    .er-table text.acc { fill: #fff; font-size: 10px; font-weight: 700; } .er-table text.type { fill: #5f6b7a; font-size: 10.5px; }
    .er-table text.key { font-size: 9.5px; font-weight: 700; fill: #c25e00; }
    .er-table text.more { fill: #5f6b7a; font-style: italic; }
    .er-table rect.used { fill: #e7eef6; } .er-table rect.used.write { fill: #fdecea; } .er-table rect.used.filter { fill: #fbf0e2; } .er-table text.used { font-weight: 700; }
    .er-table text.value { fill: #1d2330; } .er-table text.value.null { fill: #8a94a3; font-style: italic; }
    .er-table text.value.cond { fill: #b85a00; } .er-table text.value.changed { font-weight: 700; text-decoration: underline; }
    .er-table text.unused { fill: #8a94a3; } .er-table text.note { fill: #b85a00; font-size: 10.5px; }
    .img-section { font-size: 15px; font-weight: 700; }
    .er-link { fill: none; stroke-width: 1.6; } .er-link.declared { stroke: #1f4e79; }
    .er-link.inferred { stroke: #2e7d32; stroke-dasharray: 5 3; } .er-link.possible { stroke: #8e24aa; stroke-dasharray: 8 4 2 4; }
    .er-link.observed { stroke: #c25e00; stroke-dasharray: 2 3; stroke-width: 2; }
    .er-mark { fill: none; stroke-width: 1.6; } .er-mark.hole { fill: #fff; } .er-mark.declared { stroke: #1f4e79; }
    .er-mark.inferred { stroke: #2e7d32; } .er-mark.possible { stroke: #8e24aa; } .er-mark.observed { stroke: #c25e00; }
    .img-title { font-size: 18px; font-weight: 700; fill: #1d2330; } .img-sub, .img-legend { font-size: 12px; fill: #5f6b7a; }`;
  const LEGEND = [["declared", "declared foreign key"], ["inferred", "inferred from the names"],
                  ["possible", "possible (column name points to the table)"], ["observed", "join in the log, not in the schema"]];

  /** {svg: text of a standalone .svg file, width, height}: title, diagram and legend. */
  function toSvg(er, { title = "", subtitle = "", graph = true, positions = null } = {}) {
    const tables = (er.tables || []).map(t => ({ ...t, columns: t.columns || [] }));
    tables.forEach(measure);
    const es = edges({ ...er, tables });
    if (positions) place(tables, es, positions);
    else if (graph) graphLayout(tables, es);
    else layout(tables, es);
    const b = bounds(tables);
    tables.forEach(t => { t.x -= b.x; t.y -= b.y; });
    const size = { width: b.width, height: b.height };
    const byName = Object.fromEntries(tables.map(t => [norm(t.name), t]));
    const margin = 30, top = margin + 52, legendH = 56;
    // a loop or a route around a table can leave the boxes on the right
    const width = Math.ceil(Math.max(size.width + 60, 1060) + 2 * margin), height = Math.ceil(top + size.height + legendH + margin);
    const svg = el("svg", { xmlns: NS, width, height, viewBox: `0 0 ${width} ${height}` });
    svg.append(el("style", {}, IMAGE_CSS), markers());
    svg.append(el("rect", { width, height, fill: "#ffffff" }));
    svg.append(el("text", { class: "img-title", x: margin, y: margin + 10 }, title));
    svg.append(el("text", { class: "img-sub", x: margin, y: margin + 30 }, subtitle));
    const world = el("g", { transform: `translate(${margin},${top})` });
    es.forEach(e => world.append(drawEdge(e, byName)));
    tables.forEach(t => world.append(drawTable(t)));
    svg.append(world);
    const legend = el("g", { transform: `translate(${margin},${top + size.height + 26})` });
    let x = 0;
    for (const [kind, label] of LEGEND) {
      legend.append(el("path", { class: `er-link ${kind}`, d: `M${x},-4 L${x + 28},-4` }));
      legend.append(el("text", { class: "img-legend", x: x + 34, y: 0 }, label));
      x += 34 + label.length * 6.2 + 24;
    }
    legend.append(el("text", { class: "img-legend", x: 0, y: 22 },
      "line ends: || exactly one · o| zero or one · crow's foot = many · R / W / D = read / write / delete · highlighted = columns used by the log (red = written) · grey header = table not in the dumps"));
    svg.append(legend);
    return { svg: new XMLSerializer().serializeToString(svg), width, height };
  }

  /** {svg, width, height}: every table, section by section (sections: [{label, color, tables}]), and the legend. */
  function tablesToSvg(sections, { title = "", subtitle = "" } = {}) {
    const margin = 30, gap = 24, maxWidth = 1500;
    const parts = [];
    let y = margin + 52, width = 900;
    for (const section of sections) {
      const tables = section.tables.map(t => ({ ...t, columns: t.columns || [] }));
      tables.forEach(measure);
      parts.push(el("text", { class: "img-section", x: margin, y: y + 14, fill: section.color }, `${section.label} (${tables.length})`));
      parts.push(el("rect", { x: margin, y: y + 22, width: "100%", height: 1.5, fill: section.color }));
      y += 36;
      let x = margin, rowH = 0;
      for (const t of tables) {
        if (x > margin && x + t.w > maxWidth) { x = margin; y += rowH + gap; rowH = 0; }
        t.x = x; t.y = y;
        parts.push(drawTable(t));
        x += t.w + gap;
        rowH = Math.max(rowH, t.h);
        width = Math.max(width, x - gap + margin);
      }
      y += rowH + 30;
    }
    const height = Math.ceil(y + 34);
    width = Math.ceil(width);
    const svg = el("svg", { xmlns: NS, width, height, viewBox: `0 0 ${width} ${height}` });
    svg.append(el("style", {}, IMAGE_CSS), el("rect", { width, height, fill: "#ffffff" }));
    svg.append(el("text", { class: "img-title", x: margin, y: margin + 10 }, title));
    svg.append(el("text", { class: "img-sub", x: margin, y: margin + 30 }, subtitle));
    parts.forEach(p => svg.append(p));
    svg.append(el("text", { class: "img-legend", x: margin, y: y + 8 },
      "Highlighted attributes: blue = read · orange = filter (WHERE / JOIN) · red = written · grey = not used by the log with this operation · grey header = table not in the dump"));
    return { svg: new XMLSerializer().serializeToString(svg), width, height };
  }

  /** The .svg text drawn on a canvas -> PNG blob: twice the size when it fits the limits of a canvas. */
  function toPng({ svg, width, height }) {
    const MAX_SIDE = 16000, MAX_AREA = 120e6;
    const scale = Math.min(2, MAX_SIDE / width, MAX_SIDE / height, Math.sqrt(MAX_AREA / (width * height)));
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement("canvas");
        canvas.width = Math.floor(width * scale);
        canvas.height = Math.floor(height * scale);
        const ctx = canvas.getContext("2d");
        if (!ctx) return reject(new Error("The picture is too big for a PNG: save it as SVG."));
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
        canvas.toBlob(b => b ? resolve(b) : reject(new Error("The picture is too big for a PNG: save it as SVG.")), "image/png");
      };
      img.onerror = () => reject(new Error("The picture could not be converted to PNG: save it as SVG."));
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    });
  }

  return { render, toSvg, toPng, tableBox, tablesToSvg, compact, edges };
})();
