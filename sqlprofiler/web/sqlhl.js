"use strict";
// SQL syntax highlighting and placeholder interpolation (no dependencies, works offline).

const SqlHL = (() => {
  const KEYWORDS = new Set(`ADD ALL ALTER AND ANY AS ASC AUTO_INCREMENT BEGIN BETWEEN BY CALL CASE CAST CHECK COLLATE
    COLUMN COMMIT CONSTRAINT CREATE CROSS CURRENT_DATE CURRENT_TIMESTAMP DATABASE DEFAULT DELETE DESC DESCRIBE DISTINCT
    DROP DUPLICATE ELSE END ESCAPE EXCEPT EXISTS EXPLAIN FALSE FETCH FIRST FOR FOREIGN FROM FULL FUNCTION GROUP HAVING IF
    IGNORE IN INDEX INNER INSERT INTERSECT INTERVAL INTO IS JOIN KEY LEFT LIKE LIMIT LOCK MERGE NATURAL NEXT NOT NULL
    OFFSET ON OR ORDER OUTER OVER PARTITION PRIMARY PROCEDURE REFERENCES REPLACE RETURNING RIGHT ROLLBACK ROW ROWS
    SAVEPOINT SCHEMA SELECT SET SHOW START STRAIGHT_JOIN TABLE THEN TO TOP TRANSACTION TRIGGER TRUE TRUNCATE UNION
    UNIQUE UPDATE USE USING VALUES VIEW WHEN WHERE WINDOW WITH`.split(/\s+/));
  const FUNCS = new Set(`COUNT SUM AVG MIN MAX COALESCE IFNULL ISNULL NULLIF NOW CONCAT SUBSTRING SUBSTR UPPER LOWER
    TRIM LENGTH ROUND FLOOR CEIL DATE_FORMAT DATE_ADD DATE_SUB STR_TO_DATE GROUP_CONCAT ROW_NUMBER RANK
    DENSE_RANK LAG LEAD EXTRACT TO_CHAR TO_DATE STRING_AGG ARRAY_AGG`.split(/\s+/));

  const TOKEN = /(--[^\n]*|#[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^'\\]|\\.|'')*'?)|("(?:[^"\\]|\\.)*"?|`[^`]*`?)|(\$\d+|\?|:[A-Za-z_]\w*)|(\b\d+(?:\.\d+)?(?:[eE][-+]?\d+)?\b)|([A-Za-z_][\w$]*)|(\s+|.)/g;

  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  function highlight(sql) {
    let out = "";
    for (const m of sql.matchAll(TOKEN)) {
      const [t, com, str, ident, ph, num, word] = m;
      if (com) out += `<span class="hl-com">${esc(t)}</span>`;
      else if (str) out += `<span class="hl-str">${esc(t)}</span>`;
      else if (ident) out += `<span class="hl-id">${esc(t)}</span>`;
      else if (ph) out += `<span class="hl-ph">${esc(t)}</span>`;
      else if (num) out += `<span class="hl-num">${esc(t)}</span>`;
      else if (word && KEYWORDS.has(t.toUpperCase())) out += `<span class="hl-kw">${esc(t)}</span>`;
      else if (word && FUNCS.has(t.toUpperCase())) out += `<span class="hl-kw">${esc(t)}</span>`;
      else out += esc(t);
    }
    return out;
  }

  function literal(v, dialect) {
    if (v === null || v === undefined) return "NULL";
    if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
    if (typeof v === "number") return String(v);
    if (Array.isArray(v)) {
      const items = v.map(x => literal(x, dialect)).join(", ");
      return dialect === "postgres" ? `ARRAY[${items}]` : `(${items})`;
    }
    let s = String(v).replace(/'/g, "''");
    if (dialect === "mysql") s = s.replace(/\\/g, "\\\\");
    return `'${s}'`;
  }

  const SKIP = String.raw`'(?:[^'\\]|\\.|'')*'|"(?:[^"\\]|\\.)*"|` + "`[^`]*`" + String.raw`|--[^\n]*|\/\*[\s\S]*?\*\/`;

  /** Puts bound values in place of ? (MySQL) or $n (PostgreSQL), skipping strings and comments. */
  function interpolate(sql, params, dialect) {
    if (!params || !params.length) return sql;
    const lits = params.map(p => literal(p, dialect));
    if (dialect === "mysql") {
      let i = 0;
      return sql.replace(new RegExp(SKIP + String.raw`|\?`, "g"), m => m === "?" ? (i < lits.length ? lits[i++] : m) : m);
    }
    return sql.replace(new RegExp(SKIP + String.raw`|\$(\d+)`, "g"), (m, n) => {
      if (!n) return m;
      const k = Number(n) - 1;
      return k >= 0 && k < lits.length ? lits[k] : m;
    });
  }

  return { highlight, interpolate, esc };
})();
