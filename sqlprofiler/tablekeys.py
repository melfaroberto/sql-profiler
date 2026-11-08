"""Keys, relations and areas of the tables a log uses: the analysis of sql-fk-finder, on those tables only.

The rules come from sql-fk-finder (sqlfk/analisi.py: tipo_relazione, controlli_progettazione, raggruppa_tabelle,
aree_e_categorie), rewritten without pandas, scipy and networkx:
  * primary keys (declared, or guessed when the dump has none) and tables without one;
  * foreign keys: declared in the dump, inferred from the names (high / medium confidence, see fkinfer), each with its
    relation (1:1 or 1:N, mandatory or optional) and whether the types match;
  * bridge tables (N:M): a composite primary key made of foreign keys to different tables;
  * redundant links: X -> Z when X -> Y -> Z already exists;
  * subgroups (tables with similar names, hierarchical clustering) and, above them, areas (subgroups closely linked by
    foreign keys, greedy modularity); the tables pointed to by very many others weigh little in the division;
  * the category of every table (lookup, master, transaction, detail, bridge, history, dependent, isolated).
"""
import heapq
import re
import unicodedata
from collections import Counter
from itertools import combinations

from .fkinfer import base_type, compatible, singulars
from .schema import norm

NOT_ENTITY = {"tbl", "tab", "tb", "t", "tmp", "temp", "bak", "backup", "old", "new", "x", "ext", "cfg", "web", "log"}
GROUP_THRESHOLD = 0.75                                   # how similar two names must be to share a subgroup
GENERIC = {"id", "nome", "descrizione", "descr", "note", "data", "stato", "tipo", "codice", "valore", "importo",
           "createdat", "updatedat", "deletedat", "datains", "datamod", "utenteins", "utentemod", "attivo", "flag"}
HISTORY_WORDS = {"log", "logs", "storico", "storici", "history", "hist", "audit", "archivio", "archive", "tracking",
                 "eventi", "events", "versioni", "versions", "revisioni", "revisions"}
DETAIL_WORDS = {"righe", "riga", "dettaglio", "dettagli", "det", "details", "detail", "items", "item", "lines", "line",
                "voci", "voce", "rows", "row", "positions"}
DATE_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIMESTAMPTZ", "TIME", "SMALLDATETIME", "DATETIME2"}
TEXT_TYPES = {"VARCHAR", "CHAR", "TEXT", "NVARCHAR", "NCHAR", "STRING", "TINYTEXT", "MEDIUMTEXT", "LONGTEXT", ""}
CATEGORIES = {
    "lookup": "lookup (coding) table: few fields, only pointed to by the others",
    "master": "main entity: other tables point to it",
    "transaction": "fact or event with a date, points to the main entities",
    "detail": "rows that depend on another table (its key contains the parent's key)",
    "bridge": "bridge table of an N:M relation",
    "history": "history, log or archive",
    "dependent": "points to other tables, but none points to it",
    "isolated": "no link with other tables",
}
NO_LINKS = "(no links)"


# ====================================================================== names

def _plain(text):
    return "".join(c for c in unicodedata.normalize("NFKD", str(text)) if not unicodedata.combining(c))


def name_parts(name):
    """ordini_righe -> [ordini, righe]; OrdiniRighe -> [ordini, righe]"""
    name = re.sub(r"([a-z])([A-Z])", r"\1_\2", _plain(name).split(".")[-1])
    return [p for p in re.split(r"[^A-Za-z0-9]+", name.lower()) if p]


def _entity_words(table):
    """tbl_ordini_righe -> [(ordini, {ordini, ordine...}), (righe, {righe, riga})]"""
    parts = [p for p in name_parts(table) if p not in NOT_ENTITY and not p.isdigit() and len(p) >= 2]
    parts = parts or [re.sub(r"[^a-z0-9]", "", str(table).lower())]
    return [(p, frozenset(singulars(p))) for p in parts]


def _word_similarity(a, b):
    """1 for the same word (also singular / plural); 0 when only the ending differs; else the share of common start."""
    (pa, fa), (pb, fb) = a, b
    if not fa.isdisjoint(fb):
        return 1.0
    n = 0
    while n < min(len(pa), len(pb)) and pa[n] == pb[n]:
        n += 1
    if n >= max(len(pa), len(pb)) - 2:
        return 0.0
    return n / max(len(pa), len(pb))


def _distance(wa, wb):
    same_a = sum(any(not fa.isdisjoint(fb) for _, fb in wb) for _, fa in wa)
    same_b = sum(any(not fb.isdisjoint(fa) for _, fa in wa) for _, fb in wb)
    return 1 - max(_word_similarity(wa[0], wb[0]), (same_a + same_b) / (len(wa) + len(wb)))


def name_groups(names):
    """{table: subgroup name}: average-linkage clustering of the names, cut where they stop being similar.

    The heap holds only the pairs close enough to be merged; the distances of a new cluster are updated with the
    Lance-Williams formula, so a few hundred tables take a moment."""
    names = sorted(set(names), key=str.lower)
    limit = 1 - GROUP_THRESHOLD
    words = [_entity_words(n) for n in names]
    dist = {i: {} for i in range(len(names))}
    heap = []
    for i, j in combinations(range(len(names)), 2):
        d = _distance(words[i], words[j])
        dist[i][j] = dist[j][i] = d
        if d <= limit:
            heap.append((d, i, j))
    heapq.heapify(heap)
    members = {i: [i] for i in range(len(names))}
    next_id = len(names)
    while heap:
        d, a, b = heapq.heappop(heap)
        if a not in members or b not in members:
            continue
        na, nb = len(members[a]), len(members[b])
        new = next_id
        next_id += 1
        members[new] = members.pop(a) + members.pop(b)
        dist[new] = {}
        for k in members:
            if k == new:
                continue
            dk = (na * dist[a][k] + nb * dist[b][k]) / (na + nb)
            dist[new][k] = dist[k][new] = dk
            if dk <= limit:
                heapq.heappush(heap, (dk, min(new, k), max(new, k)))
        for k in (a, b):
            for other in dist.pop(k):
                dist.get(other, {}).pop(k, None)
    group_of, used = {}, Counter()
    for cluster in sorted(members.values(), key=lambda m: (-len(m), names[m[0]].lower())):
        tables = [names[i] for i in cluster]
        first = Counter(next((p for p in name_parts(t) if p not in NOT_ENTITY), t.lower()) for t in tables)
        label = min(first, key=lambda p: (-first[p], len(p), p))
        used[label] += 1
        if used[label] > 1:
            label = f"{label} ({used[label]})"
        for t in tables:
            group_of[t] = label
    return group_of


# ====================================================================== communities

def _modularity_communities(nodes, weights):
    """Greedy modularity (Clauset-Newman-Moore): merges the two communities that raise the modularity most, while
    one does. weights: {(a, b): weight} of an undirected graph. Returns a list of sets."""
    total = sum(weights.values())
    if not total:
        return [{n} for n in nodes]
    degree = Counter()
    for (a, b), w in weights.items():
        degree[a] += w
        degree[b] += w
    community = {n: n for n in nodes}
    members = {n: {n} for n in nodes}
    share = {n: degree[n] / (2 * total) for n in nodes}                 # a_i
    between = {}                                                        # e_ij, both directions summed once
    for (a, b), w in weights.items():
        key = (min(a, b), max(a, b))
        between[key] = between.get(key, 0) + w / (2 * total)
    while True:
        best, pair = 0.0, None
        for (i, j), e in between.items():
            gain = 2 * (e - share[i] * share[j])
            if gain > best + 1e-12 or (pair and abs(gain - best) <= 1e-12 and (i, j) < pair):
                best, pair = gain, (i, j)
        if not pair:
            break
        keep, gone = pair
        members[keep] |= members.pop(gone)
        share[keep] += share.pop(gone)
        for n in members[keep]:
            community[n] = keep
        merged = {}
        for (i, j), e in between.items():
            i, j = (keep if i == gone else i), (keep if j == gone else j)
            if i == j:
                continue
            key = (min(i, j), max(i, j))
            merged[key] = merged.get(key, 0) + e
        between = merged
    return list(members.values())


# ====================================================================== the analysis

def _relation(table, columns):
    """("1:1" or "1:N", mandatory?) of a foreign key made of columns of table."""
    by_name = {norm(c["name"]): c for c in table["columns"]}
    cols = [by_name.get(norm(c)) for c in columns]
    pk = sorted(norm(c["name"]) for c in table["columns"] if c.get("pk"))
    one = (len(cols) == 1 and cols[0] is not None and cols[0].get("unique")) or sorted(norm(c) for c in columns) == pk
    return ("1:1" if one else "1:N"), all(c is not None and c.get("not_null") for c in cols)


def _type_of(table, column):
    return next((c.get("type", "") for c in table["columns"] if norm(c["name"]) == norm(column)), None)


def analyse_keys(catalog, names):
    """Everything about the keys of the tables called names (the ones the log uses). Call it after
    fkinfer.add_inferred_links(catalog, names): the inferred links and the guessed primary keys are read from the catalog."""
    tables = {}
    for n in names:
        t = catalog.table(n)
        if t:
            tables[norm(t["name"])] = t
    key_of = lambda name: norm(catalog.canonical(name))

    # ---- foreign keys: from used tables; the target may be a table the log does not use
    fks, accepted = [], []
    for l in catalog.links:
        src = key_of(l["from"])
        if src not in tables:
            continue
        source, target = tables[src], catalog.table(l["to"])
        to_cols = list(l["to_cols"])
        if target and not to_cols:
            to_cols = [c["name"] for c in target["columns"] if c.get("pk")]
        relation, mandatory = _relation(source, l["from_cols"])
        types = [(_type_of(source, a) or "", (_type_of(target, b) or "") if target else "")
                 for a, b in zip(l["from_cols"], to_cols)]
        kind = l["kind"]
        fk = {"from": source["name"], "from_cols": list(l["from_cols"]),
              "to": target["name"] if target else l["to"], "to_cols": to_cols,
              "kind": "declared" if kind == "declared" else kind.replace("inferred-alta", "inferred-high")
                                                              .replace("inferred-media", "inferred-medium"),
              "relation": relation, "mandatory": mandatory,
              "types_ok": all(compatible(a, b) for a, b in types) and bool(target),
              "types": " / ".join(f"{a or '?'} → {b or '?'}" for a, b in types),
              "target_used": key_of(l["to"]) in tables, "target_in_dump": bool(target), "redundant": ""}
        fks.append(fk)
        if fk["target_used"] and (kind == "declared" or (kind == "inferred-alta" and fk["types_ok"])):
            accepted.append(fk)
    edges = [(key_of(f["from"]), f["from_cols"], key_of(f["to"]), f) for f in accepted if key_of(f["from"]) != key_of(f["to"])]

    # ---- primary keys
    pk_of = {k: [c["name"] for c in t["columns"] if c.get("pk")] for k, t in tables.items()}
    no_pk = [{"table": t["name"], "guessed": ", ".join(pk_of[k]) if t.get("pk_guessed") else "",
              "suggestion": (f"declare PRIMARY KEY ({', '.join(pk_of[k])})" if t.get("pk_guessed") and pk_of[k]
                             else f"add an artificial key, e.g. id_{t['name']} INT AUTO_INCREMENT PRIMARY KEY")}
             for k, t in sorted(tables.items()) if t.get("pk_guessed") or not pk_of[k]]

    # ---- bridges (N:M)
    points = {}
    for src, cols, dst, _ in edges:
        if len(cols) == 1:
            points.setdefault((src, norm(cols[0])), dst)
    bridges = {}
    for k, pk in pk_of.items():
        if len(pk) < 2:
            continue
        linked = [(c, points[(k, norm(c))]) for c in pk if (k, norm(c)) in points]
        targets = sorted({d for _, d in linked})
        if len(targets) >= 2:
            bridges[k] = {"table": tables[k]["name"], "links": [tables[d]["name"] for d in targets],
                          "through": [f"{c} → {tables[d]['name']}" for c, d in linked],
                          "attributes": [c["name"] for c in tables[k]["columns"] if c["name"] not in pk]}

    # ---- redundant links: a direct link that repeats a path of 2 or 3 steps
    out_of = {}
    for src, _, dst, f in edges:
        out_of.setdefault(src, []).append(dst)
    for src, _, dst, f in edges:
        paths, found = [[src]], None
        for _ in range(3):
            longer = []
            for p in paths:
                for d in out_of.get(p[-1], []):
                    if d in p or (p[-1] == src and d == dst):
                        continue
                    if d == dst and len(p) >= 2:
                        found = p + [d]
                        break
                    longer.append(p + [d])
                if found:
                    break
            if found:
                break
            paths = longer
        if found:
            f["redundant"] = " → ".join(tables[x]["name"] for x in found)

    # ---- subgroups and areas
    group_of = {norm(n): g for n, g in name_groups([t["name"] for t in tables.values()]).items()}
    outgoing, incoming, pointed_by = Counter(), Counter(), {}
    for src, _, dst, _ in edges:
        outgoing[src] += 1
        incoming[dst] += 1
        pointed_by.setdefault(dst, set()).add(src)
    linked_tables = {s for s, _, _, _ in edges} | {d for _, _, d, _ in edges}
    minimum = max(4, round(0.1 * len(linked_tables)))
    central = {d for d, by in pointed_by.items()
               if len(by) >= minimum and len({group_of[t] for t in by} - {group_of[d]}) >= 3}
    groups = sorted(set(group_of.values()), key=str.lower)
    weights = {}
    for src, _, dst, _ in edges:
        a, b = group_of[src], group_of[dst]
        if a != b:
            key = (min(a, b), max(a, b))
            weights[key] = weights.get(key, 0) + (0.1 if src in central or dst in central else 1.0)
    with_links = sorted({g for k in weights for g in k})
    fk_count = Counter()
    for src, _, dst, _ in edges:
        fk_count[group_of[src]] += 1
        fk_count[group_of[dst]] += 1
    central_groups = {group_of[t] for t in central}
    area_of = {}
    for members in _modularity_communities(with_links, weights):
        head = max(members, key=lambda g: (g not in central_groups, fk_count[g],
                                           sum(group_of[t] == g for t in tables), g))
        for g in members:
            area_of[g] = head
    for g in with_links:                                 # a central subgroup alone goes with its strongest neighbour
        if area_of[g] == g and sum(1 for v in area_of.values() if v == g) == 1:
            near = Counter()
            for (a, b), w in weights.items():
                if g in (a, b):
                    near[area_of[b if a == g else a]] += w
            if near:
                area_of[g] = near.most_common(1)[0][0]
    inner = {group_of[s] for s, _, d, _ in edges if group_of[s] == group_of[d]}
    for g in groups:
        if g not in area_of:
            n = sum(group_of[t] == g for t in tables)
            area_of[g] = g if (n > 1 or g in inner) else NO_LINKS

    # ---- categories
    lower_pk = {k: {norm(c) for c in pk} for k, pk in pk_of.items()}

    def has_date(t):
        return any(base_type(c.get("type")) in DATE_TYPES and re.sub(r"[^a-z0-9]", "", c["name"].lower()) not in GENERIC
                   for c in t["columns"])

    def coding(k):
        others = [c for c in tables[k]["columns"] if not c.get("pk")]
        return len(tables[k]["columns"]) <= 3 and all(base_type(c.get("type")) in TEXT_TYPES for c in others)

    parent_in_key = {s for s, cols, d, _ in edges
                     if d not in central and len(lower_pk[s]) > 1 and {norm(c) for c in cols} < lower_pk[s]}
    codings = {k for k in tables if incoming[k] and not outgoing[k] and coding(k)}
    points_entity = {s for s, _, d, _ in edges if d not in codings}
    category = {}
    for k, t in tables.items():
        words = set(name_parts(t["name"]))
        if words & HISTORY_WORDS and k not in bridges:
            cat = "history"
        elif not outgoing[k] and not incoming[k]:
            cat = "isolated"
        elif k in bridges:
            cat = "bridge"
        elif outgoing[k] and (k in parent_in_key or words & DETAIL_WORDS):
            cat = "detail"
        elif k in points_entity and has_date(t) and k not in central:
            cat = "transaction"
        elif k in codings:
            cat = "lookup"
        elif incoming[k]:
            cat = "master"
        else:
            cat = "dependent"
        category[k] = cat

    # ---- links between areas
    between = {}
    for src, cols, dst, f in edges:
        a, b = area_of[group_of[src]], area_of[group_of[dst]]
        if a != b:
            v = between.setdefault(tuple(sorted((a, b))), {"areas": sorted((a, b)), "links": 0, "through": []})
            v["links"] += 1
            v["through"].append(f"{f['from']}.{', '.join(cols)} → {f['to']}")

    # ---- per table, and the areas
    by_table = {}
    for f in fks:
        by_table.setdefault(key_of(f["from"]), {"out": [], "in": []})["out"].append(f)
        if f["target_used"]:
            by_table.setdefault(key_of(f["to"]), {"out": [], "in": []})["in"].append(f)
    table_rows = []
    for k, t in sorted(tables.items()):
        g = group_of[k]
        table_rows.append({
            "table": t["name"], "subgroup": g, "area": area_of[g], "category": category[k], "central": k in central,
            "pk": pk_of[k], "pk_guessed": bool(t.get("pk_guessed")), "columns": len(t["columns"]),
            "fk_out": [f for f in by_table.get(k, {}).get("out", [])],
            "fk_in": [{"from": f["from"], "from_cols": f["from_cols"], "kind": f["kind"], "relation": f["relation"]}
                      for f in by_table.get(k, {}).get("in", [])],
            "bridge": bridges.get(k)})
    areas = {}
    for row in table_rows:
        a = areas.setdefault(row["area"], {"area": row["area"], "subgroups": {}, "tables": [], "central": []})
        a["subgroups"].setdefault(row["subgroup"], []).append(row["table"])
        a["tables"].append(row["table"])
        if row["central"]:
            a["central"].append(row["table"])
    area_list = sorted(areas.values(), key=lambda a: (a["area"] == NO_LINKS, -len(a["tables"]), a["area"].lower()))
    for a in area_list:
        a["linked_to"] = sorted({x for k in between for x in k if a["area"] in k} - {a["area"]})
        a["subgroups"] = [{"subgroup": g, "tables": ts} for g, ts in
                          sorted(a["subgroups"].items(), key=lambda x: (-len(x[1]), x[0].lower()))]
        a["categories"] = dict(Counter(category[norm(t)] for t in a["tables"]).most_common())

    count = Counter(f["kind"] for f in fks)
    return {
        "tables": table_rows, "areas": area_list, "fks": fks, "no_pk": no_pk, "bridges": list(bridges.values()),
        "area_links": sorted(between.values(), key=lambda v: -v["links"]), "categories": CATEGORIES,
        "summary": {"tables": len(tables), "with_pk": len(tables) - len(no_pk), "no_pk": len(no_pk),
                    "declared": count["declared"], "inferred_high": count["inferred-high"],
                    "inferred_medium": count["inferred-medium"],
                    "one_to_one": sum(f["relation"] == "1:1" for f in accepted),
                    "one_to_many": sum(f["relation"] == "1:N" for f in accepted), "bridges": len(bridges),
                    "redundant": sum(bool(f["redundant"]) for f in fks),
                    "type_mismatch": sum(not f["types_ok"] and f["target_in_dump"] for f in fks),
                    "to_unused": sum(f["target_in_dump"] and not f["target_used"] for f in fks),
                    "areas": len(area_list)},
    }
