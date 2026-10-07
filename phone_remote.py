#!/usr/bin/env python3
"""
Phone Remote: use your phone as a trackpad, mouse and keyboard for this PC.

Run this on the PC. It shows a QR code; scan it with the phone (both must be
on the same Wi-Fi). The phone opens a web page that works as the remote, so
nothing has to be installed on the phone.

    pip install aiohttp pynput qrcode
    python phone_remote.py
"""
import argparse
import json
import math
import os
import platform
import secrets
import socket
import sys
import webbrowser
from pathlib import Path

try:
    from aiohttp import WSMsgType, web
except ImportError:
    sys.exit("Missing packages. Run this first:\n\n    pip install aiohttp pynput qrcode\n")

SYSTEM = platform.system()  # "Windows", "Darwin" or "Linux"
HERE = Path(__file__).resolve().parent
KEY_FILE = HERE / "remote.key"
DEFAULT_PORT = 8765


# ----------------------------------------------------------------------------
# Moving the real mouse and keyboard
# ----------------------------------------------------------------------------
class RealInput:
    """Sends mouse and keyboard input to this computer through pynput."""

    def __init__(self):
        if SYSTEM == "Windows":
            # Makes cursor movement use real pixels on scaled (125%, 150%) displays.
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass
        from pynput.keyboard import Controller as Keyboard, Key
        from pynput.mouse import Button, Controller as Mouse

        self.mouse = Mouse()
        self.keyboard = Keyboard()
        self.buttons = {"left": Button.left, "right": Button.right, "middle": Button.middle}
        self.keys = {
            "esc": Key.esc, "enter": Key.enter, "tab": Key.tab, "space": Key.space,
            "backspace": Key.backspace, "delete": Key.delete,
            "up": Key.up, "down": Key.down, "left": Key.left, "right": Key.right,
            "home": Key.home, "end": Key.end, "pageup": Key.page_up, "pagedown": Key.page_down,
            "play": Key.media_play_pause, "next": Key.media_next, "prev": Key.media_previous,
            "volup": Key.media_volume_up, "voldown": Key.media_volume_down,
            "mute": Key.media_volume_mute,
        }
        self._mx = self._my = 0.0   # leftover fractions of a pixel
        self._sx = self._sy = 0.0   # leftover fractions of a scroll notch
        self.held = set()

    def move(self, dx, dy):
        self._mx += dx
        self._my += dy
        ix, iy = int(self._mx), int(self._my)
        if ix or iy:
            self._mx -= ix
            self._my -= iy
            self.mouse.move(ix, iy)

    def scroll(self, dx, dy):
        """dx, dy are in wheel notches and may be fractions."""
        self._sx += dx
        self._sy += dy
        if SYSTEM == "Windows":
            # Windows accepts 1/120ths of a notch, which gives smooth scrolling.
            qx, qy = int(self._sx * 120), int(self._sy * 120)
            if qx or qy:
                self._sx -= qx / 120
                self._sy -= qy / 120
                nudge = lambda q: (q + math.copysign(0.5, q)) / 120 if q else 0
                self.mouse.scroll(nudge(qx), nudge(qy))
        else:
            scale = 4 if SYSTEM == "Darwin" else 2
            qx, qy = int(self._sx * scale), int(self._sy * scale)
            if qx or qy:
                self._sx -= qx / scale
                self._sy -= qy / scale
                self.mouse.scroll(qx, qy)

    def click(self, button, count=1):
        self.mouse.click(self.buttons[button], count)

    def hold(self, button, down):
        b = self.buttons[button]
        if down and button not in self.held:
            self.mouse.press(b)
            self.held.add(button)
        elif not down and button in self.held:
            self.mouse.release(b)
            self.held.discard(button)

    def release_all(self):
        for button in list(self.held):
            self.hold(button, False)

    def key(self, name):
        self.keyboard.tap(self.keys[name])

    def type(self, text, backspaces=0):
        from pynput.keyboard import Key
        for _ in range(backspaces):
            self.keyboard.tap(Key.backspace)
        for ch in text:
            if ch == "\n":
                self.keyboard.tap(Key.enter)
            else:
                self.keyboard.type(ch)


def clamp(value, limit):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(-limit, min(limit, value))


def handle_message(inp, msg):
    kind = msg.get("t")
    if kind == "m":
        inp.move(clamp(msg.get("x"), 3000), clamp(msg.get("y"), 3000))
    elif kind == "s":
        inp.scroll(clamp(msg.get("x"), 40), clamp(msg.get("y"), 40))
    elif kind == "c" and msg.get("b") in inp.buttons:
        inp.click(msg["b"], 2 if msg.get("n") == 2 else 1)
    elif kind == "d" and msg.get("b") in inp.buttons:
        inp.hold(msg["b"], bool(msg.get("down")))
    elif kind == "k" and msg.get("k") in inp.keys:
        inp.key(msg["k"])
    elif kind == "x":
        text = str(msg.get("s", ""))[:500]
        inp.type(text, int(clamp(msg.get("bs"), 500)))


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
async def page(request):
    return web.Response(text=REMOTE_PAGE, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def pair(request):
    if request.remote not in ("127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="Open this page on the PC itself.")
    app = request.app
    html = (PAIR_PAGE
            .replace("__QR__", qr_svg(app["url"]))
            .replace("__URL__", app["url"])
            .replace("__NAME__", app["name"]))
    return web.Response(text=html, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


async def websocket(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
    await ws.prepare(request)
    if not secrets.compare_digest(request.query.get("k", ""), app["key"]):
        await ws.close(code=4401, message=b"wrong key")
        return ws
    inp = app["input"]
    print(f"Phone connected ({request.remote})", flush=True)
    await ws.send_json({"t": "hello", "name": app["name"]})
    try:
        async for raw in ws:
            if raw.type != WSMsgType.TEXT:
                continue
            try:
                msg = json.loads(raw.data)
                if isinstance(msg, dict):
                    handle_message(inp, msg)
            except Exception as err:  # one bad event must not drop the phone
                print(f"Could not apply {raw.data!r}: {err}", flush=True)
    finally:
        inp.release_all()  # never leave a mouse button stuck down
        print(f"Phone disconnected ({request.remote})", flush=True)
    return ws


def qr_svg(text):
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=10, border=2)
        svg = img.to_string(encoding="unicode")
        return svg[svg.index("<svg"):]
    except Exception:
        return "<p>Type the address below into Safari on your phone.</p>"


def lan_address():
    """The address other devices on the Wi-Fi can reach this PC on."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # nothing is sent; this only picks the route
        return s.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        s.close()


def load_key():
    """A private key in the link, so only phones that scanned the code get in."""
    try:
        key = KEY_FILE.read_text().strip()
        if len(key) >= 8:
            return key
    except OSError:
        pass
    key = secrets.token_urlsafe(9)
    try:
        KEY_FILE.write_text(key)
    except OSError:
        pass
    return key


def open_socket(port):
    for candidate in range(port, port + 20):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if SYSTEM != "Windows":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("0.0.0.0", candidate))
            return s, candidate
        except OSError:
            s.close()
    sys.exit(f"Ports {port}-{port + 19} are all in use. Try --port with another number.")


def main():
    parser = argparse.ArgumentParser(description="Use your phone as a remote for this PC.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the pairing page on this PC")
    parser.add_argument("--new-key", action="store_true",
                        help="make a new link; phones paired before must scan again")
    args = parser.parse_args()

    if args.new_key:
        try:
            KEY_FILE.unlink()
        except OSError:
            pass

    try:
        inp = RealInput()
    except ImportError as err:
        if "pynput" in str(err) and "No module" in str(err):
            sys.exit("Missing packages. Run this first:\n\n    pip install aiohttp pynput qrcode\n")
        sys.exit(f"This PC would not let the program control the mouse:\n{err}")

    sock, port = open_socket(args.port)
    key = load_key()
    app = web.Application()
    app["input"] = inp
    app["key"] = key
    app["name"] = platform.node() or "this PC"
    app["url"] = f"http://{lan_address()}:{port}/?k={key}"
    app.add_routes([web.get("/", page), web.get("/pair", pair), web.get("/ws", websocket)])

    print("\nPhone Remote is running.\n")
    print("Open this on your phone (same Wi-Fi as the PC):\n")
    print(f"    {app['url']}\n")
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(app["url"])
        qr.print_ascii(invert=True)
    except Exception:
        pass
    print("Keep this window open while you use the remote. Press Ctrl+C to stop.\n", flush=True)

    if not args.no_browser:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/pair")
        except Exception:
            pass

    web.run_app(app, sock=sock, print=None, access_log=None)


# ----------------------------------------------------------------------------
# The page shown on the PC: QR code to scan
# ----------------------------------------------------------------------------
PAIR_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pair your phone</title>
<style>
  :root { --bg:#12161F; --panel:#1C2330; --text:#E9EDF3; --muted:#8E99AC; --mark:#F2CB1D; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:32px;
    background:var(--bg); color:var(--text);
    font:17px/1.5 ui-rounded, "SF Pro Rounded", system-ui, "Segoe UI", sans-serif; }
  main { display:flex; gap:48px; align-items:center; flex-wrap:wrap; justify-content:center; max-width:860px; }
  .code { background:#fff; border-radius:20px; padding:10px; width:300px; height:300px; flex:none; }
  .code svg { width:100%; height:100%; display:block; }
  .text { max-width:400px; }
  h1 { font-size:34px; line-height:1.15; margin:0 0 16px; font-weight:700; letter-spacing:-0.01em; }
  p { margin:0 0 14px; color:var(--muted); }
  p strong { color:var(--text); font-weight:600; }
  code { display:block; margin:20px 0; padding:12px 14px; border-radius:10px; background:var(--panel);
    color:var(--mark); font:15px/1.4 ui-monospace, Consolas, monospace; word-break:break-all; }
</style></head>
<body><main>
  <div class="code">__QR__</div>
  <div class="text">
    <h1>Point your phone's camera at the code</h1>
    <p>Your phone needs to be on <strong>the same Wi-Fi</strong> as __NAME__. Tap the link the camera shows and the remote opens in the browser.</p>
    <code>__URL__</code>
    <p>To keep it handy, tap Share and then <strong>Add to Home Screen</strong>. The link only works while the Phone Remote window is open on this PC.</p>
  </div>
</main></body></html>
"""


# ----------------------------------------------------------------------------
# The page shown on the phone: the remote itself
# ----------------------------------------------------------------------------
REMOTE_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Remote">
<meta name="theme-color" content="#12161F">
<title>Remote</title>
<style>
  :root {
    --bg:#12161F; --pad:#1B2230; --key:#273042; --key-hi:#313C52;
    --text:#E9EDF3; --muted:#8E99AC; --mark:#F2CB1D; --ok:#56B6F5; --bad:#F0705F;
    --gap:8px; --r:14px;
  }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body { margin:0; height:100%; overflow:hidden; overscroll-behavior:none; background:var(--bg); }
  body {
    color:var(--text);
    font:16px/1.2 ui-rounded, "SF Pro Rounded", system-ui, -apple-system, "Segoe UI", sans-serif;
    -webkit-user-select:none; user-select:none; -webkit-touch-callout:none;
  }
  #app {
    position:fixed; left:0; right:0; top:0; height:var(--h, 100dvh);
    display:flex; flex-direction:column; gap:var(--gap);
    padding:calc(env(safe-area-inset-top) + 8px) calc(env(safe-area-inset-right) + 10px)
            calc(env(safe-area-inset-bottom) + 10px) calc(env(safe-area-inset-left) + 10px);
  }

  /* Top line: connection and cursor speed */
  .top { display:flex; align-items:center; gap:8px; min-height:34px; flex:none; }
  .state { display:flex; align-items:center; gap:8px; flex:1; min-width:0; color:var(--muted); font-size:14px; }
  .state b { font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--bad); flex:none; }
  .on .dot { background:var(--ok); }
  .speed { border:0; background:none; color:var(--muted); font:inherit; font-size:14px; padding:8px 4px; }
  .speed span { color:var(--text); font-weight:600; }

  /* Typing bar, shown while the phone keyboard is open */
  .typebar { display:none; gap:var(--gap); flex:none; }
  .typing .typebar { display:flex; }
  .typing .top { display:none; }
  .typebar input {
    flex:1; min-width:0; height:44px; border-radius:12px; border:1.5px solid var(--mark);
    background:var(--pad); color:var(--text); font:inherit; font-size:17px; padding:0 12px; outline:0;
    -webkit-user-select:text; user-select:text;
  }
  .typebar input::placeholder { color:var(--muted); }

  /* Trackpad */
  #pad {
    flex:1; min-height:120px; position:relative; overflow:hidden; touch-action:none;
    border-radius:22px; background:var(--pad);
    background-image:radial-gradient(circle, rgba(233,237,243,.07) 1px, transparent 1.5px);
    background-size:22px 22px; background-position:11px 11px;
    box-shadow:inset 0 0 0 1px rgba(233,237,243,.06);
    transition:box-shadow .12s;
  }
  #pad.drag { box-shadow:inset 0 0 0 2px var(--mark); }
  .hint {
    position:absolute; left:0; right:0; bottom:16px; text-align:center; pointer-events:none;
    color:var(--muted); font-size:13px; line-height:1.5; padding:0 20px; transition:opacity .3s;
  }
  .used .hint { opacity:0; }
  .blip {
    position:absolute; width:56px; height:56px; margin:-28px 0 0 -28px; border-radius:50%;
    border:2px solid var(--mark); pointer-events:none; animation:blip .32s ease-out forwards;
  }
  .blip.alt { border-color:var(--ok); }
  @keyframes blip { from { transform:scale(.3); opacity:.9; } to { transform:scale(1); opacity:0; } }
  @media (prefers-reduced-motion: reduce) { .blip { animation-duration:.01s; } }

  /* Buttons */
  .row { display:grid; gap:var(--gap); flex:none; }
  .clicks { grid-template-columns:1fr 1fr; }
  .r5 { grid-template-columns:repeat(5, 1fr); }
  .r6 { grid-template-columns:repeat(4, 1fr) 2fr; }
  .media { grid-template-columns:repeat(6, 1fr); }
  button.k {
    appearance:none; border:0; margin:0; padding:0; height:46px; border-radius:var(--r);
    background:var(--key); color:var(--text); font:inherit; font-weight:600; font-size:15px;
    display:flex; align-items:center; justify-content:center; touch-action:none;
  }
  button.k.big { height:58px; background:var(--key-hi); font-size:16px; }
  button.k.glyph { font-size:20px; font-weight:500; }
  button.k.down { background:var(--mark); color:#12161F; }
  button.k svg { width:24px; height:24px; fill:currentColor; pointer-events:none; }
  button:focus-visible { outline:2px solid var(--mark); outline-offset:2px; }
  .typing .row.extra { display:none; }

  /* Shown when the link no longer matches this PC */
  .stale { display:none; position:absolute; inset:0; padding:32px; background:var(--bg);
    flex-direction:column; justify-content:center; gap:12px; z-index:5; }
  .stale h1 { font-size:26px; margin:0; line-height:1.2; }
  .stale p { margin:0; color:var(--muted); line-height:1.5; }
  .nokey .stale { display:flex; }
</style>
</head>
<body>
<div id="app">
  <div class="top">
    <div class="state" id="state"><i class="dot"></i><span id="stateText">Connecting</span></div>
    <button class="speed" id="speed" aria-label="Change cursor speed">Cursor <span id="speedText">normal</span></button>
  </div>

  <div class="typebar">
    <input id="text" type="text" inputmode="text" enterkeyhint="send" placeholder="Type here, it appears on the PC"
           autocomplete="off" autocorrect="off" autocapitalize="off" spellcheck="false" aria-label="Text to type on the PC">
    <button class="k" id="typeDone" style="padding:0 16px;height:44px">Done</button>
  </div>

  <div id="pad" role="application" aria-label="Trackpad">
    <div class="hint">Slide to move, tap to click.<br>Two fingers scroll, a two-finger tap right-clicks.<br>Hold still, then slide to drag.</div>
  </div>

  <div class="row clicks">
    <button class="k big" data-b="left">Left click</button>
    <button class="k big" data-b="right">Right click</button>
  </div>

  <div class="row r5 extra">
    <button class="k" data-k="esc">Esc</button>
    <button class="k" data-k="tab">Tab</button>
    <button class="k" data-k="backspace" data-repeat aria-label="Backspace">
      <svg viewBox="0 0 24 24"><path d="M9 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-1.5-.7L2 12l5.5-6.3A2 2 0 0 1 9 5zm2.7 3.9-1.4 1.4 1.7 1.7-1.7 1.7 1.4 1.4 1.7-1.7 1.7 1.7 1.4-1.4-1.7-1.7 1.7-1.7-1.4-1.4-1.7 1.7z"/></svg>
    </button>
    <button class="k" data-k="enter">Enter</button>
    <button class="k" id="typeOpen" aria-label="Open keyboard to type">
      <svg viewBox="0 0 24 24"><path d="M4 5h16a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2zm1 3v2h2V8zm3.5 0v2h2V8zm3.5 0v2h2V8zm3.500 0v2h2V8zM5 11.5v2h2v-2zm3.5 0v2h2v-2zm3.5 0v2h2v-2zm3.5 0v2h2v-2zM8 15v1.500h8V15z"/></svg>
    </button>
  </div>

  <div class="row r6 extra">
    <button class="k glyph" data-k="left" data-repeat aria-label="Left arrow">&#8592;</button>
    <button class="k glyph" data-k="up" data-repeat aria-label="Up arrow">&#8593;</button>
    <button class="k glyph" data-k="down" data-repeat aria-label="Down arrow">&#8595;</button>
    <button class="k glyph" data-k="right" data-repeat aria-label="Right arrow">&#8594;</button>
    <button class="k" data-k="space">Space</button>
  </div>

  <div class="row media extra">
    <button class="k" data-k="prev" aria-label="Previous track"><svg viewBox="0 0 24 24"><path d="M6 6h2v12H6zm3.500 6 8.500 6V6z"/></svg></button>
    <button class="k" data-k="play" aria-label="Play or pause"><svg viewBox="0 0 24 24"><path d="M3 6v12l8-6zm10 0h3v12h-3zm5 0h3v12h-3z"/></svg></button>
    <button class="k" data-k="next" aria-label="Next track"><svg viewBox="0 0 24 24"><path d="M16 6h2v12h-2zM6 18l8.500-6L6 6z"/></svg></button>
    <button class="k" data-k="voldown" data-repeat aria-label="Volume down"><svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 5V4L8 9zm12 2h5v2h-5z"/></svg></button>
    <button class="k" data-k="mute" aria-label="Mute"><svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 5V4L8 9zm11.400.200L14 10.600l1.900 1.900-1.900 1.900 1.400 1.400 1.900-1.900 1.900 1.900 1.400-1.400-1.900-1.900 1.900-1.900-1.400-1.400-1.900 1.900z"/></svg></button>
    <button class="k" data-k="volup" data-repeat aria-label="Volume up"><svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 5V4L8 9zm13-1h2v3h3v2h-3v3h-2v-3h-3v-2h3z"/></svg></button>
  </div>

  <div class="stale">
    <h1>This link is out of date</h1>
    <p>On the PC, open the Phone Remote window and scan the code again with your camera.</p>
  </div>
</div>

<script>
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var app = $('app'), pad = $('pad'), stateEl = $('state'), stateText = $('stateText');

  // ---- Remembering small settings (never required for the page to work) ----
  function load(name) { try { return localStorage.getItem(name); } catch (e) { return null; } }
  function save(name, value) { try { localStorage.setItem(name, value); } catch (e) {} }

  // ---- Connection --------------------------------------------------------
  var key = new URLSearchParams(location.search).get('k') || load('remoteKey') || '';
  if (key) save('remoteKey', key);
  var ws = null, retry = 0, pcName = '';

  function connect() {
    clearTimeout(retry);
    if (ws && ws.readyState < 2) return;
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws?k=' + encodeURIComponent(key));
    ws.onmessage = function (e) {
      var m = {}; try { m = JSON.parse(e.data); } catch (err) {}
      if (m.t === 'hello') {
        pcName = m.name; stateEl.classList.add('on');
        stateText.innerHTML = 'Controlling <b></b>'; stateText.querySelector('b').textContent = pcName;
      }
    };
    ws.onclose = function (e) {
      stateEl.classList.remove('on');
      if (e.code === 4401) { app.classList.add('nokey'); return; }
      stateText.textContent = 'Not connected. Is the PC on and the remote running?';
      retry = setTimeout(connect, 1200);
    };
  }
  function send(msg) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) connect(); });
  window.addEventListener('pageshow', connect);
  connect();

  // ---- Cursor speed ------------------------------------------------------
  var speeds = [['slow', 0.65], ['normal', 1.0], ['fast', 1.6]];
  var speedIndex = Math.min(2, Math.max(0, parseInt(load('remoteSpeed') || '1', 10) || 0));
  function showSpeed() { $('speedText').textContent = speeds[speedIndex][0]; }
  $('speed').addEventListener('click', function () {
    speedIndex = (speedIndex + 1) % speeds.length; save('remoteSpeed', String(speedIndex)); showSpeed();
  });
  showSpeed();

  // ---- Trackpad ----------------------------------------------------------
  var SLOP = 7;            // px a finger may wobble and still count as a tap
  var TAP_MS = 320;        // longest touch that counts as a tap
  var HOLD_MS = 380;       // holding still this long picks things up for dragging
  var NOTCH = 42;          // finger px per scroll-wheel notch
  var fingers = new Map(); // touch id -> last position
  var g = null;            // the gesture in progress
  var dragging = false, holdTimer = 0;
  var mx = 0, my = 0, sx = 0, sy = 0, frame = 0;

  function flush() {
    frame = 0;
    if (mx || my) { send({ t: 'm', x: +mx.toFixed(2), y: +my.toFixed(2) }); mx = my = 0; }
    if (sx || sy) { send({ t: 's', x: +sx.toFixed(3), y: +sy.toFixed(3) }); sx = sy = 0; }
  }
  function queue() { if (!frame) frame = requestAnimationFrame(flush); }

  function blip(x, y, alt) {
    var r = pad.getBoundingClientRect(), d = document.createElement('i');
    d.className = 'blip' + (alt ? ' alt' : '');
    d.style.left = (x - r.left) + 'px'; d.style.top = (y - r.top) + 'px';
    pad.appendChild(d);
    setTimeout(function () { d.remove(); }, 400);
  }

  pad.addEventListener('touchstart', function (e) {
    e.preventDefault();
    var now = performance.now();
    for (var i = 0; i < e.changedTouches.length; i++) {
      var t = e.changedTouches[i];
      fingers.set(t.identifier, { x: t.clientX, y: t.clientY, t: now });
    }
    if (!g) {
      var first = e.changedTouches[0];
      g = { start: now, travel: 0, most: 0, moving: false, x: first.clientX, y: first.clientY };
      // One finger resting in place: press the left button so the next slide drags.
      holdTimer = setTimeout(function () {
        if (!g || g.moving || g.most !== 1) return;
        dragging = true; pad.classList.add('drag'); app.classList.add('used');
        send({ t: 'd', b: 'left', down: true });
      }, HOLD_MS);
    }
    g.most = Math.max(g.most, fingers.size);
    if (g.most > 1) clearTimeout(holdTimer);
  }, { passive: false });

  pad.addEventListener('touchmove', function (e) {
    e.preventDefault();
    if (!g) return;
    var now = performance.now(), dx = 0, dy = 0, dt = 16, hit = 0;
    for (var i = 0; i < e.changedTouches.length; i++) {
      var t = e.changedTouches[i], f = fingers.get(t.identifier);
      if (!f) continue;
      dx += t.clientX - f.x; dy += t.clientY - f.y;
      dt = Math.max(4, now - f.t);
      f.x = t.clientX; f.y = t.clientY; f.t = now; hit++;
    }
    if (!hit) return;
    var dist = Math.hypot(dx, dy);
    g.travel += dist;
    if (!g.moving) {
      if (g.travel < SLOP) return;
      g.moving = true; app.classList.add('used'); clearTimeout(holdTimer);
    }
    if (g.most === 1) {
      // Slow fingers move precisely, fast swipes cross the screen.
      var gain = speeds[speedIndex][1] * (1 + Math.min(dist / dt * 2.4, 4.5));
      mx += dx * gain; my += dy * gain;
    } else if (fingers.size >= 2) {
      // Content follows the fingers, like scrolling on the phone itself.
      sx -= dx / 2 / NOTCH; sy += dy / 2 / NOTCH;
    }
    queue();
  }, { passive: false });

  function lift(e) {
    e.preventDefault();
    for (var i = 0; i < e.changedTouches.length; i++) fingers.delete(e.changedTouches[i].identifier);
    if (fingers.size || !g) return;
    var now = performance.now();
    clearTimeout(holdTimer);
    flush();
    if (dragging) {
      dragging = false; pad.classList.remove('drag'); send({ t: 'd', b: 'left', down: false });
    } else if (e.type === 'touchend' && !g.moving && now - g.start < TAP_MS) {
      if (g.most === 1) { send({ t: 'c', b: 'left' }); blip(g.x, g.y); }
      else if (g.most === 2) { send({ t: 'c', b: 'right' }); blip(g.x, g.y, true); }
    }
    g = null;
  }
  pad.addEventListener('touchend', lift, { passive: false });
  pad.addEventListener('touchcancel', lift, { passive: false });

  // ---- Buttons -----------------------------------------------------------
  Array.prototype.forEach.call(document.querySelectorAll('button.k[data-k], button.k[data-b]'), function (btn) {
    var timer = 0, isDown = false;
    function down(e) {
      e.preventDefault();
      if (isDown) return;
      isDown = true; btn.classList.add('down');
      try { btn.setPointerCapture(e.pointerId); } catch (err) {}
      if (btn.dataset.b) { send({ t: 'd', b: btn.dataset.b, down: true }); return; }
      var k = btn.dataset.k;
      send({ t: 'k', k: k });
      if (btn.hasAttribute('data-repeat')) {
        timer = setTimeout(function again() { send({ t: 'k', k: k }); timer = setTimeout(again, 75); }, 420);
      }
    }
    function up() {
      if (!isDown) return;
      isDown = false; btn.classList.remove('down'); clearTimeout(timer);
      if (btn.dataset.b) send({ t: 'd', b: btn.dataset.b, down: false });
    }
    btn.addEventListener('pointerdown', down);
    btn.addEventListener('pointerup', up);
    btn.addEventListener('pointercancel', up);
    btn.addEventListener('lostpointercapture', up);
    btn.addEventListener('contextmenu', function (e) { e.preventDefault(); });
  });

  // ---- Typing with the phone keyboard -----------------------------------
  var text = $('text'), prev = [];
  function resetText() { text.value = ''; prev = []; }
  $('typeOpen').addEventListener('click', function () {
    app.classList.add('typing'); resetText(); text.focus();
  });
  function closeTyping() { app.classList.remove('typing'); text.blur(); resetText(); }
  $('typeDone').addEventListener('click', closeTyping);
  text.addEventListener('blur', function () { setTimeout(function () {
    if (document.activeElement !== text) app.classList.remove('typing');
  }, 150); });

  // Whatever changed in the box is replayed on the PC: backspaces for what
  // was removed, then the new characters. This also copes with autocorrect.
  text.addEventListener('input', function () {
    var cur = Array.from(text.value), same = 0;
    while (same < prev.length && same < cur.length && prev[same] === cur[same]) same++;
    var bs = prev.length - same, add = cur.slice(same).join('');
    if (bs || add) send({ t: 'x', bs: bs, s: add });
    prev = cur;
  });
  text.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); send({ t: 'k', k: 'enter' }); resetText(); }
    else if (e.key === 'Backspace' && text.value === '') { send({ t: 'k', k: 'backspace' }); }
  });

  // ---- Keep the layout above the phone keyboard --------------------------
  var vv = window.visualViewport;
  function fit() {
    if (vv) document.documentElement.style.setProperty('--h', vv.height + 'px');
    window.scrollTo(0, 0);
  }
  if (vv) { vv.addEventListener('resize', fit); vv.addEventListener('scroll', fit); }
  window.addEventListener('orientationchange', fit);
  fit();
  document.addEventListener('gesturestart', function (e) { e.preventDefault(); });
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
