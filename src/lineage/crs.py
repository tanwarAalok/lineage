"""Change-request context: what a CR is, everything it builds on, and where it lives in code.

A CR rarely stands alone: CR-3002 "depends on" CR-3001 when it is built on top of it. When a
developer starts CR-3002, the agent needs CR-3001's agreed contracts, decisions and the code each
system wrote for it, with provenance, plus warnings when that foundation is unfinished or changed.
"""

from __future__ import annotations

from collections import defaultdict

from lineage.store.base import Store

MAX_DEPTH = 10  # ponytail: dependency chains deeper than this are cut off; raise if CR trees get deep
MAX_FILES_PER_SYSTEM = 20
MAX_COMMITS_PER_SYSTEM = 10

GUIDE = (
    "builds_on lists every CR this one is built on (nearest first) with its agreed contracts, approved "
    "decisions and the code each system wrote for it. Build on those, do not re-derive them. Use "
    "read_code on implemented_in paths to see that code. Implement agreed contracts exactly: optional "
    "fields stay optional, limits and names as written. Contracts marked proposed are not final. "
    "Warnings name code that may lag a changed contract: check it, fix it if it is part of the task, "
    "and tell the developer about the rest.")


def ancestry(store: Store, cr_id: str) -> list[tuple[str, int]]:
    """(cr_id, depth) for every CR `cr_id` depends on, transitively, nearest first; cycle-safe."""
    found: list[tuple[str, int]] = []
    seen, frontier, depth = {cr_id}, [cr_id], 1
    while frontier and depth <= MAX_DEPTH:
        nxt = []
        for cid in frontier:
            for link in store.cr_links(cid):
                other = link["other_id"]
                if link["cr_id"] == cid and link["kind"] == "depends_on" and other not in seen:
                    seen.add(other)
                    found.append((other, depth))
                    nxt.append(other)
        frontier, depth = nxt, depth + 1
    return found


def latest_contracts(rows: list[dict], include_proposed: bool = True) -> list[dict]:
    """Per (cr, provider, name): the newest agreed version, plus any newer pending proposal."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["cr_id"], r["provider"], r["name"])].append(r)
    out = []
    for (cr, provider, name), versions in groups.items():
        versions.sort(key=lambda r: r["version"])
        agreed = [v for v in versions if v["status"] == "agreed"]
        current = agreed[-1] if agreed else (versions[-1] if include_proposed else None)
        if current is None or current["status"] == "rejected":
            continue
        entry = _contract(current) | {"versions": len(versions)}
        pending = [v for v in versions if v["status"] == "proposed" and v["version"] > current["version"]]
        if pending and include_proposed:
            entry["pending_version"] = _contract(pending[-1])
        out.append(entry)
    return out


def _contract(r: dict) -> dict:
    return {"id": r["id"], "cr": r["cr_id"], "provider": r["provider"], "name": r["name"], "version": r["version"],
            "status": r["status"], "spec": r["spec"], "consumers": _split(r["consumers"]), "source": r["source"],
            "author": r["author"], "agreed_by": r["reviewer"] if r["status"] == "agreed" else None,
            "created_at": r["created_at"]}


def _split(text: str | None) -> list[str]:
    return [s.strip() for s in (text or "").split(",") if s.strip()]


def implemented_in(mentions: list[dict]) -> dict:
    """{system: {"files": [{path, lines, symbols}], "commits": [...]}} from code and commit mentions."""
    systems: dict[str, dict] = {}
    for m in mentions:
        s = systems.setdefault(m["system_id"], {"files": {}, "commits": []})
        if m["source"] == "commit":
            if m["detail"] not in s["commits"] and len(s["commits"]) < MAX_COMMITS_PER_SYSTEM:
                s["commits"].append(m["detail"])
            s["files"].setdefault(m["path"], {"path": m["path"], "lines": [], "symbols": []})
        else:
            f = s["files"].setdefault(m["path"], {"path": m["path"], "lines": [], "symbols": []})
            f["lines"].append(m["line"])
            if m["symbol_name"] and m["symbol_name"] not in f["symbols"]:
                f["symbols"].append(m["symbol_name"])
    return {sys_id: {"files": list(s["files"].values())[:MAX_FILES_PER_SYSTEM], "commits": s["commits"]}
            for sys_id, s in systems.items()}


def build_context(store: Store, cr_id: str, project: str | None = None) -> dict:
    """`project` is the asking repository: drift that concerns it is listed first, with its files."""
    cr = store.get_cr(cr_id)
    links = store.cr_links(cr_id)
    chain = ancestry(store, cr_id)
    refs = [l["other_id"] for l in links if l["cr_id"] == cr_id and l["kind"] == "references"]
    ids = [cr_id] + [a for a, _ in chain] + refs
    contracts, decisions, mentions = store.contracts(ids), store.decisions(ids), store.cr_mentions(ids)
    by_cr = lambda rows, cid: [r for r in rows if r["cr_id"] == cid]  # noqa: E731

    def summary(cid: str, full: bool) -> dict:
        c = store.get_cr(cid)
        return {
            "cr": cid, "registered": c is not None,
            "title": c["title"] if c else None, "status": c["status"] if c else None,
            "summary": c["summary"] if c else None,
            "contracts": latest_contracts(by_cr(contracts, cid), include_proposed=full),
            "decisions": [_decision(d) for d in by_cr(decisions, cid) if full or d["status"] == "approved"],
            "implemented_in": implemented_in(by_cr(mentions, cid)),
        }

    this = summary(cr_id, full=True) | ({"created_at": cr["created_at"], "created_by": cr["created_by"]} if cr else {})
    builds_on = [summary(a, full=False) | {"depth": d} for a, d in chain]
    drift = _drift(this, builds_on)
    # Agents attend most to what comes first: actions for the asking repo, warnings and guidance lead.
    context = {
        "for_this_repository": [
            {"repository": d["system"], "cr": d["cr"], "contract": f"{d['provider']} {d['contract']}",
             "changed_to": f"v{d['version']} on {d['changed']}", "your_code_last_changed": d["last_commit"],
             "contract_now_says": d["spec"], "your_files": d["files"],
             "action": "This repository's code may still follow the older contract. Read these files and bring "
                       "them in line with contract_now_says, then tell the developer what you changed."}
            for d in drift if project and d["system"] == project],
        "warnings": _warnings(cr_id, cr, this, builds_on, contracts, drift),
        "how_to_use": GUIDE,
        **this,
        "builds_on": builds_on,
        "references": [{k: v for k, v in summary(r, full=False).items() if k in ("cr", "title", "status", "summary")}
                       for r in refs],
        "built_on_by": [{"cr": l["cr_id"], "kind": l["kind"]} for l in links if l["other_id"] == cr_id],
        "contract_history": [_contract(r) for r in by_cr(contracts, cr_id)],
    }
    return context


def _decision(d: dict) -> dict:
    return {"id": d["id"], "system": d["system_id"], "question": d["question"], "decision": d["decision"],
            "status": d["status"], "source": d["source"], "author": d["author"], "created_at": d["created_at"]}


def _drift(this: dict, builds_on: list[dict]) -> list[dict]:
    """Systems whose code for a CR was last committed before a contract they provide or consume
    changed: they may still follow an older version (consumers often copy values, e.g. a limit).
    ponytail: compares commit dates, not schemas; a fix committed without the CR id is missed."""
    found = []
    for s in [this] + builds_on:
        for c in s["contracts"]:
            if c["status"] != "agreed" or c["version"] < 2:
                continue
            for system in [c["provider"], *c["consumers"]]:
                code = s["implemented_in"].get(system, {})
                dates = [m.split()[1] for m in code.get("commits", []) if len(m.split()) > 1]  # "<sha> <date> ..."
                if dates and max(dates) < c["created_at"][:10]:
                    found.append({"system": system, "role": "provides" if system == c["provider"] else "consumes",
                                  "cr": s["cr"], "provider": c["provider"], "contract": c["name"],
                                  "version": c["version"], "changed": c["created_at"][:10], "last_commit": max(dates),
                                  "spec": c["spec"], "files": [f["path"] for f in code.get("files", [])]})
    return found


def _warnings(cr_id: str, cr: dict | None, this: dict, builds_on: list[dict], contracts: list[dict],
              drift: list[dict]) -> list[str]:
    out = []
    if cr is None:
        out.append(f"{cr_id} is not registered in Lineage; only its code and commit mentions are known.")
    for c in this["contracts"]:
        if c["status"] == "proposed":
            out.append(f"{cr_id}'s contract '{c['provider']} {c['name']}' is still proposed, not agreed: not final.")
        if "pending_version" in c:
            out.append(f"{cr_id}'s contract '{c['provider']} {c['name']}' has a proposed v{c['pending_version']['version']}"
                       f" awaiting review (agreed: v{c['version']}).")
    for a in builds_on:
        if not a["registered"]:
            out.append(f"{cr_id} depends on {a['cr']}, which is not registered in Lineage.")
        elif a["status"] != "implemented":
            out.append(f"{cr_id} builds on {a['cr']} ({a['status']}): that foundation may still change.")
    for d in drift:
        out.append(f"{d['system']} {d['role']} '{d['provider']} {d['contract']}', which changed to v{d['version']} on "
                   f"{d['changed']}, but its code for {d['cr']} was last committed {d['last_commit']}: it may still "
                   f"follow an older version. Read that code; update it if it does.")
    if cr is not None:
        foundation = {a["cr"] for a in builds_on}
        for r in contracts:  # a foundation that moved after this CR started
            if r["cr_id"] in foundation and r["status"] == "agreed" and r["version"] > 1 and r["created_at"] > cr["created_at"]:
                out.append(f"{r['cr_id']}'s contract '{r['provider']} {r['name']}' changed to v{r['version']} on "
                           f"{r['created_at'][:10]}, after {cr_id} was created: check {cr_id} still matches it.")
    return out


def contracts_view(store: Store, cr_id: str | None, provider: str | None, name: str | None,
                   history: bool) -> list[dict]:
    """Contracts filtered by CR/provider/name. With a CR, also the ones it inherits from the CRs it
    builds on (marked inherited, nearest first)."""
    ids = None
    if cr_id:
        ids = [cr_id] + [a for a, _ in ancestry(store, cr_id)]
    rows = store.contracts(ids, provider, name)
    order = {cid: i for i, cid in enumerate(ids or [])}
    items = [_contract(r) for r in rows] if history else latest_contracts(rows)
    for item in items:
        item["inherited"] = bool(cr_id) and item["cr"] != cr_id
    # Nearest CR first, then per contract in the order versions were written: that is the history.
    return sorted(items, key=lambda c: (order.get(c["cr"], 0), c["provider"], c["name"], c["created_at"], c["version"]))
