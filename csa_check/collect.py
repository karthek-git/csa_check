#!/usr/bin/env python3
"""Ingest a Maltego Data MCP run result (JSON on stdin) into records.json.

Each result entity becomes one record: source, locator (run_id#ref), a text
rendering that includes provenance, and a content hash.

  python3 collect.py --capability "To Website [Quick lookup]" \
      --input "maltego.Domain maltego.com" < run.json
"""
import argparse, hashlib, json, os, sys

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "records.json")


def sha(s):
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def render(ent, capability, inp):
    props = {k: v for k, v in ent.items() if k not in ("ref", "type", "value")}
    ptxt = ", ".join(f"{k}={json.dumps(v)}" for k, v in props.items())
    t = f"Maltego result of [{capability}] on {inp}: {ent['type']} {ent['value']}"
    return t + (f" ({ptxt})" if ptxt else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--capability", required=True)
    ap.add_argument("--input", required=True, help='e.g. "maltego.Domain maltego.com"')
    a = ap.parse_args()
    run = json.load(sys.stdin)
    store = json.load(open(STORE)) if os.path.exists(STORE) else {}
    existing = {r["locator"] for r in store.values()}
    added = 0
    for ent in run.get("entities", []):
        loc = ent.get("ref") or f"{run['run_id']}#{ent['value']}"
        if loc in existing:
            continue
        text = render(ent, a.capability, a.input)
        rid = f"r{len(store) + 1}"
        store[rid] = {"source": f"maltego:{a.capability}", "locator": loc,
                      "run_id": run.get("run_id"), "text": text, "hash": sha(text)}
        added += 1
    json.dump(store, open(STORE, "w"), indent=2)
    print(f"added {added} record(s); store now has {len(store)}")


if __name__ == "__main__":
    main()
