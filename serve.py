#!/usr/bin/env python3
"""
Hermes UI Proxy Server — DEPRECATED SHIM
========================================

`serve.py` has been superseded by `serve_lite.py`, which ships with the
current `/api/chat/*` two-step SSE API surface. The old `serve.py`
targeted the pre-v0.7.0 `/api/sessions/{id}/chat/*` routes and is not
compatible with the client in `hermes-ui.html`.

This file is kept only so that existing launchers, systemd units, and
documentation referencing `python3 serve.py` keep working. It prints a
one-time deprecation notice and then execs `serve_lite.py` with the same
arguments.

If you are writing something new, invoke `serve_lite.py` directly.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LITE = os.path.join(HERE, "serve_lite.py")

# Backwards-compat: old serve.py took the port as a positional arg
# (e.g. `python3 serve.py 8080`); serve_lite.py uses `--port 8080`.
# Translate so old invocations keep working.
argv = list(sys.argv[1:])
translated = []
i = 0
while i < len(argv):
    arg = argv[i]
    if arg.isdigit() and "--port" not in translated:
        translated.extend(["--port", arg])
    else:
        translated.append(arg)
    i += 1

sys.stderr.write(
    "[serve.py] DEPRECATED: serve.py is now a shim for serve_lite.py.\n"
    "[serve.py] Update your launcher / systemd unit to run `serve_lite.py` directly.\n"
)
sys.stderr.flush()

os.execv(sys.executable, [sys.executable, LITE] + translated)
