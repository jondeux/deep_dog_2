"""Optional OKF v0.2 (Open Knowledge Format) export. Internal schema stays richer; OKF natives are used where the
mapping is lossless (type, title, generated, verified, status, stale_after, sources) and everything else travels in
namespaced x_ extension keys, which the spec requires consumers to preserve."""
import datetime as dt, json, os, re
from .core import HALF_LIFE, parse_ts


def _yaml(v, ind=0):
    pad = "  " * ind
    if isinstance(v, dict):
        return "\n".join(f"{pad}{k}:" + (("\n" + _yaml(x, ind + 1)) if isinstance(x, (dict, list)) and x else f" {_scalar(x)}") for k, x in v.items())
    if isinstance(v, list):
        out = []
        for x in v:
            if isinstance(x, dict):
                items = list(x.items())
                first = f"{pad}- {items[0][0]}: {_scalar(items[0][1])}"
                out.append("\n".join([first] + [f"{pad}  {k}: {_scalar(y)}" for k, y in items[1:]]))
            else:
                out.append(f"{pad}- {_scalar(x)}")
        return "\n".join(out)
    return pad + _scalar(v)


def _scalar(x):
    if x is None:
        return "null"
    if isinstance(x, (bool, int, float)):
        return json.dumps(x)
    if isinstance(x, (list, dict)):
        return json.dumps(x, ensure_ascii=False)
    return json.dumps(str(x), ensure_ascii=False)


def export(store, outdir, include_history=False):
    os.makedirs(outdir, exist_ok=True)
    rows = store.db.execute("SELECT * FROM memories WHERE forgotten=0" + ("" if include_history else " AND valid_to IS NULL"))
    n = 0
    for r in rows:
        m = store.row(r)
        meta = m.get("meta") or {}
        hl = HALF_LIFE.get(m["kind"])
        stale = None
        if m.get("ttl_days"):
            stale = (parse_ts(m["valid_from"]) + dt.timedelta(days=float(m["ttl_days"]))).isoformat()
        elif hl:
            stale = (parse_ts(m["valid_from"]) + dt.timedelta(days=hl)).isoformat()
        fm = {"type": f"memory.{m['kind']}", "title": (m.get("key") or m["text"][:60]),
              "generated": {"by": meta.get("generated_by", "agent"), "at": m["created"]},
              "status": "deprecated" if m.get("valid_to") else ("draft" if m["tier"] in ("agent", "external") else "stable")}
        if meta.get("verified"):
            fm["verified"] = meta["verified"]
        if stale:
            fm["stale_after"] = stale
        if m.get("source_ids"):
            fm["sources"] = [{"resource": "agentmem:raw", "id": s} for s in m["source_ids"]]
        fm.update({"x_id": m["id"], "x_tier": m["tier"], "x_key": m.get("key", ""), "x_valid_from": m["valid_from"],
                   "x_valid_to": m.get("valid_to"), "x_supersedes": m.get("supersedes"), "x_derived_from": m.get("derived_from") or [],
                   "x_importance": m.get("importance"), "x_entities": m.get("entities") or [],
                   "x_scope": {k: m.get(k, "") for k in ("user", "agent", "project")}})
        name = re.sub(r"[^a-z0-9-]", "-", (m.get("key") or m["id"]).lower()).strip("-")[:60] or m["id"]
        with open(os.path.join(outdir, f"{name}--{m['id']}.md"), "w", encoding="utf-8") as f:
            f.write("---\n" + _yaml(fm) + "\n---\n" + m["text"] + "\n")
        n += 1
    with open(os.path.join(outdir, "index.md"), "w", encoding="utf-8") as f:
        f.write(f"---\nokf_version: \"0.2\"\ntype: index\ntitle: \"agent-memory export\"\n---\n{n} memory records exported by agentmem.\n")
    return {"exported": n, "dir": outdir}
