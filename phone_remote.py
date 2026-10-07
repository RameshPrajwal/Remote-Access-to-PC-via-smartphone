#!/usr/bin/env python3
"""
Phone Remote: use your phone as a trackpad, mouse and keyboard for this PC.

Run this on the PC, then open the address it prints in the phone's browser
(both must be on the same Wi-Fi). The page works as the remote, so nothing
has to be installed on the phone.

    pip install aiohttp pynput
    python phone_remote.py
"""
import argparse
import json
import math
import os
import platform
import socket
import sys

try:
    from aiohttp import WSMsgType, web
except ImportError:
    sys.exit("Missing packages. Run this first:\n\n    pip install aiohttp pynput\n")

SYSTEM = platform.system()  # "Windows", "Darwin" or "Linux"
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


async def websocket(request):
    app = request.app
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
    await ws.prepare(request)
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
    args = parser.parse_args()

    try:
        inp = RealInput()
    except ImportError as err:
        if "pynput" in str(err) and "No module" in str(err):
            sys.exit("Missing packages. Run this first:\n\n    pip install aiohttp pynput\n")
        sys.exit(f"This PC would not let the program control the mouse:\n{err}")

    sock, port = open_socket(args.port)
    app = web.Application()
    app["input"] = inp
    app["name"] = platform.node() or "this PC"
    url = f"http://{lan_address()}:{port}/"
    app.add_routes([web.get("/", page), web.get("/ws", websocket)])

    print("\nPhone Remote is running.\n")
    print("Open this on your phone (same Wi-Fi as the PC):\n")
    print(f"    {url}\n")
    print("Keep this window open while you use the remote. Press Ctrl+C to stop.\n", flush=True)

    web.run_app(app, sock=sock, print=None, access_log=None)


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

  /* Top line: connection */
  .top { display:flex; align-items:center; gap:8px; min-height:34px; flex:none; }
  .state { display:flex; align-items:center; gap:8px; flex:1; min-width:0; color:var(--muted); font-size:14px; }
  .state b { font-weight:600; color:var(--text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .dot { width:9px; height:9px; border-radius:50%; background:var(--bad); flex:none; }
  .on .dot { background:var(--ok); }

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
  .r4 { grid-template-columns:repeat(4, 1fr); }
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
</style>
</head>
<body>
<div id="app">
  <div class="top">
    <div class="state" id="state"><i class="dot"></i><span id="stateText">Connecting</span></div>
  </div>

  <div id="pad" role="application" aria-label="Trackpad">
    <div class="hint">Slide to move, tap to click.<br>Two fingers scroll, a two-finger tap right-clicks.<br>Hold still, then slide to drag.</div>
  </div>

  <div class="row clicks">
    <button class="k big" data-b="left">Left click</button>
    <button class="k big" data-b="right">Right click</button>
  </div>

  <div class="row r4 extra">
    <button class="k" data-k="esc">Esc</button>
    <button class="k" data-k="tab">Tab</button>
    <button class="k" data-k="backspace" data-repeat aria-label="Backspace">
      <svg viewBox="0 0 24 24"><path d="M9 5h10a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9a2 2 0 0 1-1.5-.7L2 12l5.5-6.3A2 2 0 0 1 9 5zm2.7 3.9-1.4 1.4 1.7 1.7-1.7 1.7 1.4 1.4 1.7-1.7 1.7 1.7 1.4-1.4-1.7-1.7 1.7-1.7-1.4-1.4-1.7 1.7z"/></svg>
    </button>
    <button class="k" data-k="enter">Enter</button>
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
</div>

<script>
(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var app = $('app'), pad = $('pad'), stateEl = $('state'), stateText = $('stateText');

  // ---- Connection --------------------------------------------------------
  var ws = null, retry = 0, pcName = '';

  function connect() {
    clearTimeout(retry);
    if (ws && ws.readyState < 2) return;
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.onmessage = function (e) {
      var m = {}; try { m = JSON.parse(e.data); } catch (err) {}
      if (m.t === 'hello') {
        pcName = m.name; stateEl.classList.add('on');
        stateText.innerHTML = 'Controlling <b></b>'; stateText.querySelector('b').textContent = pcName;
      }
    };
    ws.onclose = function (e) {
      stateEl.classList.remove('on');
      stateText.textContent = 'Not connected. Is the PC on and the remote running?';
      retry = setTimeout(connect, 1200);
    };
  }
  function send(msg) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg)); }
  document.addEventListener('visibilitychange', function () { if (!document.hidden) connect(); });
  window.addEventListener('pageshow', connect);
  connect();

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
      var gain = 1 + Math.min(dist / dt * 2.4, 4.5);
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
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
