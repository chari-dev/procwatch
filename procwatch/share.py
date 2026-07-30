"""Let another machine read this one's history, over the network.

A separate listener from the dashboard, on its own port, and deliberately not
the same server with a flag flipped. The dashboard can end processes and hand
out a copy of the database; this can do neither, because those code paths are
not reachable from here at all. The worst anyone who guesses the key can do is
see which applications you run.

The key is three words. Three words are long enough to be unguessable and
short enough to read down a phone -- which is what actually happens when
someone is setting up a second machine. They come from the system dictionary,
which macOS ships: 33,000 words of four to six letters gives about 45 bits,
comfortably beyond guessing, and no wordlist has to travel in the program.
"""
import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

COOKIE = "procwatch_key"

# Pages allowed to read this server's replies from another origin.
#
# Only a local dev server, for anyone working on the dashboard itself. The
# project's site used to be on this list, because a page hosted there could read
# 127.0.0.1 and act as a viewer -- that page is gone, and an origin left on an
# allowlist after the reason for it has gone is a door nobody is watching.
#
# Deliberately a list rather than "*": a page on any site can already send a
# request to a loopback address and the browser will deliver it. What an
# allowlist controls is whether that page may READ the answer. With "*" any site
# you visited could quietly inventory your processes once it guessed the key.
ALLOWED_ORIGINS = (
    "http://localhost:8000",
    "http://127.0.0.1:8000",
)

from . import config, db

DEFAULT_PORT = 8791
HEADER = "X-Procwatch-Key"

DDL = """
CREATE TABLE IF NOT EXISTS share_key (
  id       INTEGER PRIMARY KEY CHECK (id = 1),
  key      TEXT NOT NULL,
  made_ts  INTEGER NOT NULL
);
"""

# Enough that a wrong guess is never worth repeating, short enough that a
# person mistyping their own key is not punished for a minute.
LOCKOUT_AFTER = 5
LOCKOUT_SECONDS = 60

_failures = {}
_failures_lock = threading.Lock()


def _wordlist():
    """Words a person can read down a phone.

    The system dictionary has 33,000 usable words and would give more bits,
    but they are words like "exaudi", "solyma" and "peepy" -- and a key that
    cannot be dictated is a key that gets written on a sticky note. This list
    is short, common and unambiguous. Three of them is around 340 million
    combinations, which matters only alongside the lockout below: five wrong
    answers buys a minute of silence, so guessing runs at five attempts a
    minute rather than as fast as a machine can ask.
    """
    return ("able acid aged also area army away baby back ball band bank base "
            "bath bear beat been beer bell belt bend bike bird bite blue boat "
            "body bone book boot born both bowl cake call calm camp card care "
            "cart case cash cast cave cell chat chip city clay clip club coal "
            "coat code coin cold cook cool copy cord corn cost crew crop dark "
            "data date dawn deal deep deer desk dial dirt dish dock dome door "
            "dose down draw drum dust duty each earn east easy edge exit face "
            "fact fade fair fall farm fast fear feed feel fell file fill film "
            "find fine fire fish flag flat flow foam fold folk food foot fork "
            "form fort four free frog fuel full gain game gate gear gift girl "
            "give glad glow goal gold golf gone good gray grew grid grip grow "
            "hail hair half hall hand hang hard harm heat held help herb hero "
            "hide high hill hint hold hole home hood hope horn host hour huge "
            "hunt idea inch iron item jazz join jump just keep kind king knee "
            "knot know lace lake lamp land lane last late lawn lead leaf lean "
            "left lend lens life lift line link lion list live load loan lock "
            "long look loop lord lost loud love luck lung made mail main make "
            "many maps mark mask mast meal meat meet melt mend menu mesh mile "
            "milk mill mind mine mint miss mist moon more moss most move much "
            "name near neat neck need nest news next nice node none noon nose "
            "note oath odds okay omit once open oven over pace pack page pain "
            "pair palm park part pass past path peak pear peel pick pier pile "
            "pine pink pipe plan play plot plug plus poem poet pole polo pond "
            "pool poor port post pull pump pure push quiz race rack rail rain "
            "rake ramp rank rare rate read real reed reef rely rest rice rich "
            "ride ring rise risk road roar rock role roll roof room root rope "
            "rose rule rush safe sail salt sand save scan seal seat seed seek "
            "self sell send shed ship shoe shop shot show sick side sign silk "
            "sing sink site size skin sky slab sled slip slow snap snow soap "
            "sock soft soil sold sole solo song soon sort soul soup sour span "
            "spin spot star stay stem step stir stop sung sure surf swim tale "
            "talk tall tank tape task team tell tent term test text than that "
            "thin this tide tidy tile till time tiny tone took tool torn tour "
            "town trap tray tree trim trip true tube tune turn twin type unit "
            "upon used vase vast verb very vest view vine visa void volt vote "
            "wage wait wake walk wall want ward warm wash wave weak wear week "
            "well went were west what when whom wide wife wild will wind wine "
            "wing wire wise wish wolf wood wool word wore work worm wrap yard "
            "yarn year yoga zone zoom").split()


WORDS_FILE = None      # kept out of the key deliberately; see _wordlist


def local_address():
    """This machine's address on the local network.

    Wanted so the address can be copied off the screen rather than hunted for
    in System Settings. Found by opening a UDP socket towards an address that
    is never contacted -- no packet is sent, but the kernel has to choose a
    route, and the interface it picks is the one another machine on this
    network would reach us on. Reading en0 directly would be wrong on a Mac
    using wi-fi and ethernet, or a VPN.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))      # reserved for documentation
        return probe.getsockname()[0]
    except OSError:
        return ""
    finally:
        probe.close()


# Where a Mac keeps the Tailscale command, depending on how it was installed.
TAILSCALE_PATHS = (
    "/usr/local/bin/tailscale",
    "/opt/homebrew/bin/tailscale",
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
)


def tailscale_address():
    """This machine's address on its private network, if it has one.

    Detected two ways because either can be true on its own: the command line
    tool is missing from a Mac App Store install, and the interface can be up
    while the tool is not on PATH.

    100.64.0.0/10 is the range Tailscale allocates from. Nothing else on a
    normal Mac uses it, so an interface address inside it is the answer even
    when the command cannot be found.
    """
    import subprocess
    for path in TAILSCALE_PATHS:
        if not os.path.exists(path):
            continue
        try:
            out = subprocess.run([path, "ip", "-4"], capture_output=True,
                                 text=True, timeout=8)
        except (OSError, subprocess.TimeoutExpired):
            continue
        first = (out.stdout or "").strip().split("\n")[0].strip()
        if first.startswith("100."):
            return first
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True,
                             timeout=8).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    import re
    for found in re.findall(r"inet (100\.\d+\.\d+\.\d+)", out):
        octet = int(found.split(".")[1])
        if 64 <= octet <= 127:               # 100.64.0.0/10
            return found
    return ""


def make_key(words=3):
    """Three words, chosen with the system's cryptographic randomness."""
    pool = _wordlist()
    return "-".join(secrets.choice(pool) for _ in range(words))


def key(conn, reset=False):
    """This machine's key, made on first use and kept."""
    with conn:
        conn.executescript(DDL)
    row = None if reset else conn.execute(
        "SELECT key FROM share_key WHERE id = 1").fetchone()
    if row:
        return row[0]
    fresh = make_key()
    with conn:
        conn.execute("INSERT OR REPLACE INTO share_key (id, key, made_ts) "
                     "VALUES (1, ?, ?)", (fresh, int(time.time())))
    return fresh


def _locked_out(who):
    with _failures_lock:
        count, until = _failures.get(who, (0, 0))
        return time.time() < until


def _note_failure(who):
    with _failures_lock:
        count, until = _failures.get(who, (0, 0))
        count += 1
        if count >= LOCKOUT_AFTER:
            _failures[who] = (0, time.time() + LOCKOUT_SECONDS)
        else:
            _failures[who] = (count, until)


def _note_success(who):
    with _failures_lock:
        _failures.pop(who, None)


KEY_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Procwatch</title>
<style>
  :root{color-scheme:dark light}
  body{margin:0;min-height:100vh;display:flex;align-items:center;
    justify-content:center;background:#0a0a0c;color:#fafafa;
    font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
    padding:24px}
  .box{width:100%;max-width:340px;text-align:center}
  h1{font-size:19px;margin:0 0 6px;font-weight:600}
  p{color:#8b8b95;font-size:13.5px;margin:0 0 22px}
  input{width:100%;box-sizing:border-box;padding:14px 15px;font-size:17px;
    text-align:center;border-radius:11px;border:1px solid #2c2c34;
    background:#16161b;color:#fafafa;letter-spacing:.02em}
  input:focus{outline:0;border-color:#3b82f6}
  button{width:100%;margin-top:11px;padding:14px;font-size:16px;
    font-weight:560;border:0;border-radius:11px;background:#3b82f6;
    color:#fff}
  .bad{color:#f87171;font-size:13px;margin:16px 0 0}
  @media (prefers-color-scheme:light){
    body{background:#f8f8f9;color:#0f0f12}
    input{background:#fff;border-color:#e6e6ea;color:#0f0f12}
    p{color:#8b8b95}
  }
</style></head><body>
  <div class="box">
    <h1>__HOST__</h1>
    <p>Enter the three words this Mac showed you.</p>
    <form method="get" action="/">
      <input name="key" autocomplete="off" autocapitalize="none"
             autocorrect="off" spellcheck="false" placeholder="word-word-word"
             aria-label="Three word key" autofocus>
      <button type="submit">View</button>
    </form>
    __ERROR__
  </div>
</body></html>
"""


class ShareHandler(BaseHTTPRequestHandler):
    """Reads only.

    There is no do_POST here and no route to the backup, the export or
    anything that can end a process. Adding one later would be the mistake
    this whole file exists to avoid: the point is not that dangerous things
    are switched off, it is that they are not present.

    It does serve the dashboard, so a phone on the same network can open it
    without installing anything. A hosted page cannot do that job: a browser
    refuses to let an HTTPS page read a plain-HTTP private address at all --
    verified, it is a Mixed Content block, not something a header can permit.
    """

    def log_message(self, *args):
        pass

    def _cors(self):
        """Let an allowed page read this reply, and nobody else.

        Vary matters: without it a cache that saw the answer for one origin
        could hand the same headers to another.
        """
        origin = self.headers.get("Origin")
        self.send_header("Vary", "Origin")
        if origin and origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)

    def _send(self, code, body):
        payload = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _offered_key(self, params):
        """The key, from wherever this client could supply it.

        A header for another Mac's relay. A cookie for a browser, once it has
        been through the entry page. The query string for the first visit and
        for a link that can be sent to a phone, where typing three words on a
        keyboard nobody likes is the main obstacle.
        """
        header = self.headers.get(HEADER)
        if header:
            return header, "header"
        if params.get("key"):
            return params["key"][0].strip(), "query"
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value.strip(), "cookie"
        return "", "none"

    def _wants_page(self, path):
        return path in ("/", "/index.html")

    def do_GET(self):
        who = self.client_address[0]
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        page = self._wants_page(parsed.path)

        if _locked_out(who):
            if page:
                return self._send_key_page("Too many wrong keys. "
                                           "Try again in a minute.", 429)
            return self._send(429, json.dumps(
                {"error": "too many wrong keys; try again in a minute"}))

        offered, source = self._offered_key(params)
        if not hmac.compare_digest(offered, self.server.share_key):
            if source != "none":
                _note_failure(who)
                # A pause on every wrong answer, so guessing costs real time
                # rather than being limited by how fast a phone can ask.
                time.sleep(0.5)
            if page:
                return self._send_key_page(
                    "That key was not right." if source != "none" else "")
            return self._send(401, json.dumps({"error": "wrong or missing key"}))
        _note_success(who)

        if page:
            return self._send_dashboard(offered, remember=source == "query")

        from . import server                       # imported late: server imports us
        conn = db.connect(config.DB_PATH)
        try:
            payload = server.api_get(conn, parsed.path, params)
            if payload is None:
                return self._send(404, json.dumps({"error": "unknown path"}))
            self._send(200, json.dumps(payload))
        except ValueError as error:
            self._send(400, json.dumps({"error": str(error)}))
        finally:
            conn.close()

    def _send_key_page(self, error="", code=200):
        host = os.uname().nodename.split(".")[0]
        body = KEY_PAGE.replace("__HOST__", _escape(host)).replace(
            "__ERROR__", '<p class="bad">%s</p>' % _escape(error) if error else "")
        self._send_html(code, body)

    def _send_dashboard(self, key, remember):
        """The dashboard itself, marked read-only.

        The same page the local server sends, so there is one dashboard rather
        than a cut-down copy to keep in step. The marker tells it to hide the
        controls that act on a machine -- there is nothing behind them here,
        and a button that cannot work is worse than one that is absent.
        """
        try:
            from . import server
            page = server.dashboard_html()
        except (OSError, RuntimeError):
            return self._send(500, json.dumps({"error": "no dashboard installed"}))
        page = page.replace("__PROCWATCH_TOKEN__", "")
        page = page.replace("<head>",
                            '<head>\n<meta name="procwatch-readonly" content="1">', 1)
        headers = []
        if remember:
            # So a link carrying the key only has to be opened once, and the
            # key stops appearing in the address bar. Session-scoped: nothing
            # is written to that phone's disk.
            headers.append(("Set-Cookie",
                            "%s=%s; Path=/; SameSite=Lax; HttpOnly" % (COOKIE, key)))
        self._send_html(200, page, extra=headers)

    def _send_html(self, code, body, extra=()):
        payload = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)


def _escape(text):
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def serve(port=DEFAULT_PORT, host="0.0.0.0", reset=False):
    conn = db.connect(config.DB_PATH)
    try:
        secret = key(conn, reset=reset)
    finally:
        conn.close()
    httpd = ThreadingHTTPServer((host, port), ShareHandler)
    httpd.share_key = secret
    where = local_address() or "this-mac-address"
    print("Sharing this Mac's history on port %d." % port)
    print("\nFrom a phone or tablet on the same network, open:")
    print("  http://%s:%d/?key=%s" % (where, port, secret))
    print("\nFrom another Mac's dashboard, add this one as a device:")
    print("  procwatch peer add %s %s:%d --key %s"
          % (os.uname().nodename.split(".")[0].lower(), where, port, secret))
    print("\nRead-only: no process can be ended and no database copied "
          "through this port.")
    httpd.serve_forever()
    return 0
