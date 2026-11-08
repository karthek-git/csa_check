"""Demo data source: serves the synthetic case's records through search tools.

Same interface as MaltegoToolbox (schemas() / call() / records / calls), so the
agent loop treats them interchangeably. Only records the agent actually
retrieves become part of its trail.
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
STOP = {"the", "and", "for", "are", "with", "that", "this", "from", "who", "what", "which",
        "is", "of", "in", "to", "a", "an", "by", "on", "it", "its", "part", "example"}


class SyntheticToolbox:
    def __init__(self, case_path=None):
        path = case_path or os.path.join(HERE, "case_baltic_echo.json")
        self.all = json.load(open(path))["records"]
        self.records, self.calls = {}, []

    def schemas(self):
        s = lambda d: {"type": "string", "description": d}
        def fn(name, desc, props, req):
            return {"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": req}}}
        return [
            fn("search_records", "Keyword-search the case data holdings: WHOIS, DNS, urlscan, web archive, "
               "Telegram exports and profiles, company registries, ad library, flight tracking, sanctions "
               "screening, text analysis. Returns up to 8 citable records. Use specific terms "
               "(domain names, account ids like u1001, channel names, company names).",
               {"query": s("keywords")}, ["query"]),
            fn("get_record", "Fetch one record by id.", {"record_id": s("e.g. r7")}, ["record_id"]),
        ]

    def _reveal(self, rid):
        self.records[rid] = dict(self.all[rid])
        return {"record_id": rid, "source": self.all[rid]["source"], "text": self.all[rid]["text"]}

    def call(self, name, args):
        args = args or {}
        if name == "search_records":
            toks = [t for t in re.findall(r"[a-z0-9_@.-]+", str(args.get("query", "")).lower())
                    if len(t) >= 2 and t not in STOP]
            subs = {p for t in toks for p in re.split(r"[.\-]", t) if len(p) >= 3 and p not in STOP}
            scored = []
            for rid, r in self.all.items():
                low = (r["text"] + " " + r["source"]).lower()
                sc = 2 * sum(t in low for t in toks) + sum(p in low for p in subs)
                if sc: scored.append((sc, int(rid[1:]), rid))
            hits = [self._reveal(rid) for _, _, rid in sorted(scored, key=lambda x: (-x[0], x[1]))[:8]]
            self.calls.append({"tool": name, "args": args, "records": [h["record_id"] for h in hits]})
            return json.dumps({"count": len(hits), "records": hits})
        if name == "get_record":
            rid = str(args.get("record_id"))
            if rid not in self.all: return json.dumps({"error": f"no record {rid}"})
            self.calls.append({"tool": name, "args": args, "records": [rid]})
            return json.dumps(self._reveal(rid))
        return json.dumps({"error": f"unknown tool {name}"})
