#!/usr/bin/env python3
"""OAuth (auth-code + PKCE) login for the Maltego Data MCP, then a smoke test.

Probe for "can our own app act as an MCP client?". Tokens are written to
~/.config/csa_check/maltego_token.json (mode 600), never into the project.

  python3 mcp_auth.py login     # prints a URL; open it, sign in, approve
  python3 mcp_auth.py test      # initialize + tools/list using saved token
"""
import base64, hashlib, http.server, json, os, secrets, sys, threading, time
import urllib.parse, urllib.request, urllib.error

REALM = "https://identity.maltego.com/realms/maltego/protocol/openid-connect"
CLIENT_ID = "maltego-datamcp-mcp"
PORT = 33418
REDIRECT = f"http://localhost:{PORT}/callback"
MCP_URL = "https://datamcp.maltego.com/mcp"
TOKEN_FILE = os.path.expanduser("~/.config/csa_check/maltego_token.json")


def b64(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def post_form(url, data):
    req = urllib.request.Request(url, urllib.parse.urlencode(data).encode(),
                                 {"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def save(tok):
    tok["obtained_at"] = int(time.time())
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(tok, f)


def login():
    verifier = b64(secrets.token_bytes(48))
    challenge = b64(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_urlsafe(16)
    q = urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT,
        "scope": "openid profile offline_access", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": MCP_URL})
    got = {}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            p = urllib.parse.urlparse(self.path)
            if p.path != "/callback":
                self.send_response(404); self.end_headers(); return
            params = {k: v[0] for k, v in urllib.parse.parse_qs(p.query).items()}
            if params.get("state") != state or "error" in params:
                print(f"callback state_matches={params.get('state') == state} "
                      f"error={params.get('error')!r} "
                      f"description={params.get('error_description')!r}", flush=True)
                self.send_response(400); self.end_headers()
                self.wfile.write(b"state mismatch")
                return
            got.update(params)
            self.send_response(200); self.send_header("Content-Type", "text/plain"); self.end_headers()
            self.wfile.write(b"csa_check: login received. You can close this tab.")
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("Open this URL in your browser and sign in:\n")
    print(f"{REALM}/auth?{q}\n", flush=True)
    for _ in range(300):
        if got: break
        time.sleep(1)
    srv.shutdown()
    if not got:
        sys.exit("timed out waiting for the browser callback")
    if got.get("state") != state:
        sys.exit("state mismatch; aborting")
    if "error" in got:
        sys.exit(f"authorization error: {got.get('error')}: {got.get('error_description')}")
    tok = post_form(f"{REALM}/token", {
        "grant_type": "authorization_code", "code": got["code"], "redirect_uri": REDIRECT,
        "client_id": CLIENT_ID, "code_verifier": verifier})
    save(tok)
    print(f"login ok; token saved (expires_in={tok.get('expires_in')}s, "
          f"refresh={'yes' if tok.get('refresh_token') else 'no'}, scope={tok.get('scope')})")


def access_token():
    tok = json.load(open(TOKEN_FILE))
    if time.time() > tok["obtained_at"] + tok.get("expires_in", 0) - 30:
        if not tok.get("refresh_token"):
            sys.exit("token expired and no refresh token; run login again")
        new = post_form(f"{REALM}/token", {"grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"], "client_id": CLIENT_ID})
        save(new); tok = new
    return tok["access_token"]


def rpc(method, params=None, rid=1, session=None):
    headers = {"Authorization": f"Bearer {access_token()}", "Content-Type": "application/json",
               "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
    if session: headers["Mcp-Session-Id"] = session
    body = {"jsonrpc": "2.0", "method": method}
    if rid is not None: body["id"] = rid
    if params is not None: body["params"] = params
    req = urllib.request.Request(MCP_URL, json.dumps(body).encode(), headers)
    with urllib.request.urlopen(req, timeout=120) as r:
        sid = r.headers.get("Mcp-Session-Id")
        raw = r.read().decode()
    if raw.lstrip().startswith("event:") or raw.lstrip().startswith("data:"):
        raw = "".join(l[5:].strip() for l in raw.splitlines() if l.startswith("data:"))
    return (json.loads(raw) if raw.strip() else {}), sid


def test():
    res, sid = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "csa_check", "version": "0.1"}})
    print("initialize ->", json.dumps(res.get("result", res))[:300])
    rpc("notifications/initialized", rid=None, session=sid)
    res, _ = rpc("tools/list", rid=2, session=sid)
    tools = res.get("result", {}).get("tools", [])
    print(f"tools/list -> {len(tools)} tools:", ", ".join(t["name"] for t in tools))


if __name__ == "__main__":
    {"login": login, "test": test}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit(__doc__))()
