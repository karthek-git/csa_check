#!/usr/bin/env python3
"""csa_check web server: static UI + live investigation over server-sent events.

  OPENROUTER_API_KEY=... python3 server.py [port]      (default 8000, localhost only)

GET /api/run?q=<question>&source=synthetic|maltego   -> text/event-stream
"""
import http.server, json, mimetypes, os, queue, sys, threading, traceback, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)          # works under `python3 -I` too
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
STATIC_OK = {".html", ".json", ".js", ".css"}
WEB_CREDIT_BUDGET = float(os.environ.get("CSA_CHECK_WEB_CREDIT_BUDGET", "100"))


def make_toolbox(source):
    if source == "maltego":
        from tools import MaltegoToolbox
        return MaltegoToolbox(credit_budget=WEB_CREDIT_BUDGET)
    from synthetic_tools import SyntheticToolbox
    return SyntheticToolbox()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "csa_check"

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/run":
            return self.run_stream(urllib.parse.parse_qs(u.query))
        name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        path = os.path.realpath(os.path.join(HERE, name))
        ext = os.path.splitext(path)[1]
        if (os.path.dirname(path) != HERE or ext not in STATIC_OK
                or os.path.basename(path).startswith(".") or not os.path.isfile(path)):
            self.send_response(404); self.end_headers(); return
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(path)[0] or "text/plain") + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(data)

    def run_stream(self, qs):
        question = (qs.get("q", [""])[0] or "").strip()[:500]
        source = qs.get("source", ["synthetic"])[0]
        if not question or source not in ("synthetic", "maltego"):
            self.send_response(400); self.end_headers(); self.wfile.write(b"bad request"); return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        q, stop = queue.Queue(), threading.Event()
        emit = lambda typ, payload: q.put({"type": typ, **payload})

        def work():
            try:
                from agent_loop import Investigation
                Investigation(question, make_toolbox(source), emit, cancelled=stop.is_set).run()
            except Exception as e:
                traceback.print_exc()
                emit("error", {"message": f"{type(e).__name__}: {e}"[:400]})
            finally:
                q.put(None)

        threading.Thread(target=work, daemon=True).start()
        try:
            self.wfile.write(b": start\n\n"); self.wfile.flush()
            while True:
                try:
                    ev = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n"); self.wfile.flush(); continue
                if ev is None: break
                self.wfile.write(b"data: " + json.dumps(ev, default=str).encode() + b"\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            stop.set()        # browser went away; agent stops at its next iteration


if __name__ == "__main__":
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"csa_check on http://127.0.0.1:{PORT}  (key set: {bool(os.environ.get('OPENROUTER_API_KEY'))})")
    srv.serve_forever()
