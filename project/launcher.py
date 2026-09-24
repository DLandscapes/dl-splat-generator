"""Start DL-SplatGenerator and open it in the browser.

Phase 1 is a pure client-side app, so this only needs to serve the folder over
HTTP -- ES modules and the import map will not load from a file:// URL. It uses
nothing but the standard library, so there is no virtualenv to build.

    python launcher.py                 # pick a free port, open the browser
    python launcher.py --port 9000     # insist on one port
    python launcher.py --no-browser
    python launcher.py --page index.html   # open the v1 single-file viewer
    python launcher.py --selftest      # start, check the page serves, exit

The port is negotiated rather than hard-coded: a second copy of the app must
never fight the one already running on 8992.
"""
from __future__ import annotations

import argparse
import functools
import http.server
import posixpath
import socket
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

DEFAULT_PORT = 8992
PORT_ATTEMPTS = 20
ROOT = Path(__file__).resolve().parent
# Captures and generated scenes live outside the app folder so they never end up
# in a backup or an export. Serving them read-only means they can be opened by
# URL instead of being copied in.
SIBLINGS = {
    "input": ROOT.parent / "input",     # source video and third-party captures
    "output": ROOT.parent / "output",   # scenes produced by tools/capture.py
}


def port_is_free(port: int) -> bool:
    """True if nothing is listening on the port and we could bind it.

    Deliberately NO SO_REUSEADDR: on Windows that option lets a bind succeed
    on a port that already has a listener, which would report a busy port as
    free and hand the user a server that dies on startup. The connect probe
    catches listeners the bind test might still let through.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.2)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def free_port(preferred: int, attempts: int = PORT_ATTEMPTS) -> int:
    """First free port at or above `preferred`."""
    for port in range(preferred, preferred + attempts):
        if port_is_free(port):
            return port
    raise SystemExit(
        f"No free port between {preferred} and {preferred + attempts - 1}. "
        f"Close the other copy of the app and try again.")


class Handler(http.server.SimpleHTTPRequestHandler):
    """Static handler that keeps the shell uncached and stays quiet."""

    def translate_path(self, path: str) -> str:
        """Map /input/... and /output/... onto sibling folders, the rest to ROOT."""
        clean = urllib.parse.unquote(path.split("?", 1)[0].split("#", 1)[0])
        for prefix, base in SIBLINGS.items():
            if not clean.startswith(f"/{prefix}/"):
                continue
            rel = posixpath.normpath(clean[len(prefix) + 2:])
            # Resolve and confirm the result really sits inside the folder,
            # rather than trying to spot escape patterns by eye. Anything else
            # is pointed at a name that cannot exist, so it 404s instead of
            # quietly serving something from the app root.
            candidate = (base / rel).resolve()
            try:
                candidate.relative_to(base.resolve())
            except ValueError:
                return str(base / "__forbidden__")
            return str(candidate)
        return super().translate_path(path)

    def end_headers(self) -> None:
        # never cache the app shell or its modules, so edits always take effect
        if self.path.endswith((".html", ".js", ".css", ".json")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def log_message(self, *args) -> None:  # noqa: D102 - silence per-request noise
        pass


def make_server(port: int) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(Handler, directory=str(ROOT))
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)


def backend_available() -> bool:
    """True when this interpreter can run the FastAPI backend."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        return False
    return (ROOT / "app" / "main.py").is_file()


def serve_backend(port: int) -> None:
    """Run the FastAPI app: same viewer, plus the capture API.

    The viewer works perfectly well without this -- an exported scene has to run
    on a plain static host -- so the backend is strictly additive. Without it,
    the sidebar just does not offer to make scenes from video.
    """
    import uvicorn
    sys.path.insert(0, str(ROOT / "app"))
    from app.main import app as fastapi_app          # noqa: WPS433
    uvicorn.run(fastapi_app, host="127.0.0.1", port=port, log_level="warning")


def selftest(port: int, page: str) -> int:
    """Prove the app actually serves: start, fetch the page and its entry module."""
    server = make_server(port)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/{page}", timeout=5) as r:
            shell = r.read()
    except Exception as exc:  # noqa: BLE001 - the message is the point
        print(f"SELFTEST FAILED: could not serve {page}: {exc}")
        return 1
    if b"DL-SplatGenerator" not in shell:
        print(f"SELFTEST FAILED: {page} was served but looks wrong")
        return 1
    # the frontend is a separate bundling problem from the server side
    for asset in ("static/app.js", "static/vendor/spark/spark.module.min.js"):
        try:
            with urllib.request.urlopen(f"{base}/{asset}", timeout=10) as r:
                if not r.read(64):
                    raise ValueError("empty response")
        except Exception as exc:  # noqa: BLE001
            print(f"SELFTEST FAILED: {asset} did not serve: {exc}")
            return 1
    server.shutdown()
    print(f"SELFTEST OK: serving {page} on port {port}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Start DL-SplatGenerator.")
    ap.add_argument("--port", type=int, default=None,
                    help=f"use this exact port instead of the first free one "
                         f"from {DEFAULT_PORT}")
    ap.add_argument("--no-browser", action="store_true",
                    help="do not open a browser window")
    ap.add_argument("--page", default="viewer.html",
                    help="page to open (default viewer.html)")
    ap.add_argument("--viewer-only", action="store_true",
                    help="serve the viewer without the capture backend")
    ap.add_argument("--selftest", action="store_true",
                    help="start, verify the app serves, exit (for CI)")
    args = ap.parse_args()

    port = args.port if args.port is not None else free_port(DEFAULT_PORT)

    if args.selftest:
        return selftest(port, args.page)

    url = f"http://localhost:{port}/{args.page}"
    backend = backend_available() and not args.viewer_only
    print("DL-SplatGenerator")
    print(f"  {url}")
    if port != DEFAULT_PORT:
        print(f"  (port {DEFAULT_PORT} was busy - another copy may be running)")
    if backend:
        print("  capture backend: on - you can make scenes from video in the app")
    else:
        print("  capture backend: off - viewer only")
        print("  (run this with the .venv interpreter to enable it:")
        print("   ..\\.venv\\Scripts\\python launcher.py)")
    print("  close this window to stop the app")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        if backend:
            serve_backend(port)
        else:
            make_server(port).serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
