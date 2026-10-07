#!/usr/bin/env python3
"""
Phone Remote: use your phone as a trackpad, mouse and keyboard for this PC.

Run this on the PC, then open the address it prints in the phone's browser
(both must be on the same Wi-Fi). The page works as the remote, so nothing
has to be installed on the phone.

    pip install aiohttp
    python phone_remote.py
"""
import argparse
import os
import platform
import socket
import sys

try:
    from aiohttp import web
except ImportError:
    sys.exit("Missing packages. Run this first:\n\n    pip install aiohttp\n")

SYSTEM = platform.system()  # "Windows", "Darwin" or "Linux"
DEFAULT_PORT = 8765


# ----------------------------------------------------------------------------
# Web server
# ----------------------------------------------------------------------------
async def page(request):
    return web.Response(text=REMOTE_PAGE, content_type="text/html",
                        headers={"Cache-Control": "no-store"})


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

    sock, port = open_socket(args.port)
    app = web.Application()
    url = f"http://{lan_address()}:{port}/"
    app.add_routes([web.get("/", page)])

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
    --bg:#12161F; --pad:#1B2230;
    --text:#E9EDF3; --muted:#8E99AC; --mark:#F2CB1D; --ok:#56B6F5; --bad:#F0705F;
    --gap:8px;
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
</style>
</head>
<body>
<div id="app">
  <div class="top">
    <div class="state" id="state"><i class="dot"></i><span id="stateText">Page served by the PC</span></div>
  </div>
</div>
</body>
</html>
"""


if __name__ == "__main__":
    main()
