#!/usr/bin/env python3
"""Maltego tool layer for csa_check agents.

An agent calls three tools (OpenAI-style function schemas via .schemas()):
  find_capabilities(goal, ...)  -> ranked transforms for a question (free)
  describe(ids)                 -> settings/inputs for transforms (free)
  run(capability_id, ...)       -> executes a transform, may cost credits

Every result entity becomes a hashed, re-fetchable record (locator run_id#ref)
that the agent can cite. Results are cached on disk; spending is capped.

Auth: run `python3 mcp_auth.py login` once (see mcp_auth.py).
Env: CSA_CHECK_CREDIT_BUDGET (default 300 credits per toolbox instance).
"""
import hashlib, json, os, time

import mcp_auth
from collect import render, sha

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, ".maltego_cache")
MAX_TEXT = 1500           # per-record text cap (hashed after truncation)
MAX_REPLY = 6000          # per-tool-reply cap sent back to the model
RUN_TIMEOUT = 300         # seconds to wait for a slow transform


class ToolError(Exception):
    pass


class MaltegoToolbox:
    def __init__(self, records=None, credit_budget=None, use_cache=True):
        self.records = records if records is not None else {}   # rid -> record
        self.calls = []                                          # what the agent looked at
        self.budget = credit_budget if credit_budget is not None else \
            float(os.environ.get("CSA_CHECK_CREDIT_BUDGET", "300"))
        self.spent = 0.0
        self.use_cache = use_cache
        self._session, self._rid, self._ready = None, 0, False
        self.names = {}          # capability_id -> readable name, learned from search
        self.remaining = None    # account credits left this period, from last paid run

    # ---------- MCP plumbing ----------
    def _rpc(self, method, params=None, notify=False):
        self._rid += 1
        try:
            res, sid = mcp_auth.rpc(method, params, rid=None if notify else self._rid,
                                    session=self._session)
        except SystemExit as e:                  # mcp_auth exits on missing/expired token
            raise ToolError(f"Maltego auth problem: {e}")
        except Exception as e:
            raise ToolError(f"MCP call failed: {e}")
        if sid: self._session = sid
        return res

    def _init(self):
        if self._ready: return
        self._rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                 "clientInfo": {"name": "csa_check", "version": "0.1"}})
        self._rpc("notifications/initialized", notify=True)
        self._ready = True

    def _tool(self, name, arguments):
        self._init()
        res = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if "error" in res:
            raise ToolError(f"{name}: {res['error']}")
        r = res.get("result", {})
        if r.get("structuredContent"):
            out = r["structuredContent"]
        else:
            text = "".join(c.get("text", "") for c in r.get("content", []))
            try: out = json.loads(text)
            except Exception: out = {"text": text}
        if r.get("isError"):
            raise ToolError(f"{name}: {json.dumps(out)[:500]}")
        return out

    # ---------- cache ----------
    def _ckey(self, *parts):
        return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]

    def _cache_get(self, key):
        p = os.path.join(CACHE_DIR, key + ".json")
        if self.use_cache and os.path.exists(p):
            return json.load(open(p))

    def _cache_put(self, key, val):
        os.makedirs(CACHE_DIR, exist_ok=True)
        json.dump(val, open(os.path.join(CACHE_DIR, key + ".json"), "w"))

    # ---------- records ----------
    def _add_records(self, entities, capability, inp, run_id):
        out, existing = [], {r["locator"]: rid for rid, r in self.records.items()}
        for ent in entities:
            loc = ent.get("ref") or f"{run_id}#{ent.get('value')}"
            if loc in existing:
                out.append(existing[loc]); continue
            text = render(ent, capability, inp)
            if len(text) > MAX_TEXT: text = text[:MAX_TEXT] + " [truncated]"
            rid = f"r{len(self.records) + 1}"
            self.records[rid] = {"source": f"maltego:{capability}", "locator": loc,
                                 "run_id": run_id, "text": text, "hash": sha(text)}
            existing[loc] = rid
            out.append(rid)
        return out

    # ---------- agent-facing tools ----------
    def find_capabilities(self, goal, entity_type=None, entity_value=None, query=None,
                          from_record=None, max_results=8):
        args = {"goal": goal, "max_results": min(int(max_results), 15)}
        if from_record:
            args["entity_ref"] = self._ref(from_record)
        else:
            if entity_type: args["entity_type"] = entity_type
            if entity_value: args["entity_value"] = entity_value
        if query: args["query"] = query
        res = self._tool("maltego_search", args)
        hits = []
        for r in res.get("objective_matches", {}).get("results", []):
            for v in r.get("target_variants", []) or [{}]:
                hits.append({"capability_id": v.get("catalog_id"), "name": v.get("display_name") or r.get("capability"),
                             "input_type": v.get("input_type"), "question": r.get("question"),
                             "score": r.get("score")})
        for h in hits:
            if h["capability_id"] and h["name"]: self.names[h["capability_id"]] = h["name"]
        self.calls.append({"tool": "find_capabilities", "args": args, "n": len(hits)})
        return {"matches": [h for h in hits if h["capability_id"]],
                "note": "pass capability_id to run(); check input_type matches your entity"}

    def describe(self, ids):
        res = self._tool("maltego_describe", {"ids": list(ids)[:5]})
        self.calls.append({"tool": "describe", "ids": ids})
        return res

    def _ref(self, record_id):
        r = self.records.get(record_id)
        if not r: raise ToolError(f"unknown record {record_id!r}")
        return r["locator"]

    def run(self, capability_id, entity_type=None, entity_value=None, from_record=None,
            entity_properties=None, settings=None, max_results=25, capability_name=None):
        args = {"capability_id": capability_id, "max_results": min(int(max_results), 50)}
        if from_record:
            args["entity_ref"] = self._ref(from_record)
            inp = f"record {from_record}"
        elif entity_type and entity_value:
            args["entity_type"], args["entity_value"] = entity_type, entity_value
            if entity_properties: args["entity_properties"] = entity_properties
            inp = f"{entity_type} {entity_value}"
        else:
            raise ToolError("give from_record, or entity_type and entity_value")
        if settings: args["settings"] = settings

        key = self._ckey("run", args)
        cached = self._cache_get(key)
        if cached:
            run = cached; from_cache = True
        else:
            if self.spent >= self.budget:
                raise ToolError(f"credit budget {self.budget:g} reached ({self.spent:g} spent); stop running transforms")
            first = self._tool("maltego_run", args)
            run = self._finish(first)
            from_cache = False
            cr = run.get("credits") or {}
            self.spent += float(cr.get("period_consumed_total") or 0)   # this run's estimated cost
            self.remaining = cr.get("period_remaining", getattr(self, "remaining", None))
            if run.get("status") == "ok" and run.get("run_status") == "SUCCEEDED":
                self._cache_put(key, run)

        if run.get("status") not in ("ok",):
            raise ToolError(f"run failed: {json.dumps(run)[:500]}")
        name = capability_name or self.names.get(capability_id) or capability_id
        rids = self._add_records(run.get("entities", []), name, inp, run.get("run_id"))
        self.calls.append({"tool": "run", "capability": capability_id, "input": inp,
                           "run_id": run.get("run_id"), "records": rids, "cached": from_cache})
        return {"run_id": run.get("run_id"), "cached": from_cache, "count": len(rids),
                "records": [{"record_id": rid, "text": self.records[rid]["text"]} for rid in rids],
                "note": "cite records by record_id with an exact quote from text; "
                        "use from_record to chain transforms from a record's entity"}

    def _finish(self, first):
        """maltego_run may return running; poll maltego_run_status until done."""
        run, deadline = first, time.time() + RUN_TIMEOUT
        while run.get("status") == "running" or run.get("run_status") in ("RUNNING", "PENDING", "QUEUED"):
            if time.time() > deadline:
                raise ToolError(f"run {run.get('run_id')} still running after {RUN_TIMEOUT}s")
            time.sleep(min(float(run.get("poll_after_seconds") or 2), 10))
            run = self._tool("maltego_run_status", {"run_id": first["run_id"], "wait_seconds": 20,
                                                    "entities_limit": 50, "verbosity": "detailed"})
        if "entities" not in run and first.get("entities"):
            run["entities"] = first["entities"]
        return run

    # ---------- LLM glue ----------
    def schemas(self):
        def fn(name, desc, props, req):
            return {"type": "function", "function": {"name": name, "description": desc,
                    "parameters": {"type": "object", "properties": props, "required": req}}}
        s = lambda d: {"type": "string", "description": d}
        return [
            fn("find_capabilities", "Find Maltego transforms that help answer a question (free). Optionally narrow by the entity you hold.",
               {"goal": s("one-sentence investigative question"), "entity_type": s("e.g. maltego.Domain"),
                "entity_value": s("e.g. example.com"), "from_record": s("record id whose entity you hold, e.g. r3"),
                "query": s("keywords like whois, passive dns")}, ["goal"]),
            fn("describe", "Get input requirements and settings for transform ids (free).",
               {"ids": {"type": "array", "items": {"type": "string"}}}, ["ids"]),
            fn("run", "Run one Maltego transform on one entity. May cost credits. Returns citable records.",
               {"capability_id": s("ci_... or mx_... from find_capabilities"), "entity_type": s("e.g. maltego.Domain"),
                "entity_value": s("entity value"), "from_record": s("record id to chain from instead of entity_type/value"),
                "max_results": {"type": "integer"}}, ["capability_id"]),
        ]

    def call(self, name, arguments):
        """Execute a tool call; always returns a JSON string (errors included)."""
        fns = {"find_capabilities": self.find_capabilities, "describe": self.describe, "run": self.run}
        try:
            if name not in fns: raise ToolError(f"unknown tool {name}")
            out = fns[name](**(arguments or {}))
        except ToolError as e:
            out = {"error": str(e)}
        except TypeError as e:
            out = {"error": f"bad arguments: {e}"}
        text = json.dumps(out, default=str)
        return text if len(text) <= MAX_REPLY else text[:MAX_REPLY] + '..."[truncated]"'


if __name__ == "__main__":                      # quick live smoke test
    tb = MaltegoToolbox(credit_budget=20)
    print(json.dumps(json.loads(tb.call("find_capabilities", {
        "goal": "does this domain host a website", "entity_type": "maltego.Domain",
        "entity_value": "maltego.com", "max_results": 3})), indent=1)[:1200])
    for i in (1, 2):
        out = json.loads(tb.call("run", {"capability_id": "ci_MbW_V7svQmQAn41iPZTDCtLAZb",
                                          "entity_type": "maltego.Domain", "entity_value": "maltego.com",
                                          "max_results": 5}))
        print(f"run #{i}: cached={out.get('cached')} count={out.get('count')} err={out.get('error')}")
        for r in out.get("records", []): print("  ", r["record_id"], r["text"])
    print("spent:", tb.spent, "calls:", len(tb.calls))
