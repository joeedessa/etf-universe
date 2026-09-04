#!/usr/bin/env python3
"""Static file server for local preview.

`python -m http.server` cannot be used here: its argparse setup evaluates
os.getcwd() as a default before parsing any arguments, and in a sandboxed
spawn that raises PermissionError before --directory is ever read. Importing
the module instead of running it as __main__ avoids that, and chdir'ing to a
path derived from this file's location keeps it independent of the cwd we were
launched with.

  python3 scripts/serve.py [port]
"""

import functools
import http.server
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
os.chdir(ROOT)

class NoCache(http.server.SimpleHTTPRequestHandler):
    """Serve with caching off.

    The default handler sends Last-Modified and the browser then reuses a
    cached data/*.json across a rebuild, so the page renders yesterday's shape
    against today's code — which looks like a bug in the code.
    """

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()


port = int(sys.argv[1]) if len(sys.argv) > 1 else 8791
handler = functools.partial(NoCache, directory=str(ROOT))
print(f"serving {ROOT} on http://127.0.0.1:{port}", flush=True)
http.server.ThreadingHTTPServer(("127.0.0.1", port), handler).serve_forever()
