#!/usr/bin/env python3
"""Boot the real proxy and verify that Hermes UI renders in Chromium."""

import contextlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parents[1]
VERSION_MATCH = re.search(
    r'^__version__\s*=\s*"([^"]+)"',
    (ROOT / "serve_lite.py").read_text(encoding="utf-8"),
    re.MULTILINE,
)
EXPECTED_VERSION = VERSION_MATCH.group(1) if VERSION_MATCH else ""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(url, process, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("serve_lite.py exited before the smoke test connected")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"server did not become ready at {url}")


def main():
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="hermes-ui-smoke-") as home:
        env = os.environ.copy()
        env["HOME"] = home
        for key in tuple(env):
            if key.endswith("_API_KEY") or key.endswith("_TOKEN"):
                env.pop(key, None)

        process = subprocess.Popen(
            [sys.executable, "serve_lite.py", "--port", str(port)],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            wait_for_server(f"{base_url}/health", process)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page_errors = []
                console_errors = []
                bad_responses = []
                page.on("pageerror", lambda error: page_errors.append(str(error)))
                page.on(
                    "console",
                    lambda message: console_errors.append(
                        f"{message.text} ({message.location.get('url', '')})"
                    )
                    if message.type == "error"
                    and "deoptimised the styling of /Inline Babel script" not in message.text
                    else None,
                )
                page.on(
                    "response",
                    lambda response: bad_responses.append(
                        f"{response.status} {response.url}"
                    )
                    if response.status >= 400
                    else None,
                )

                response = page.goto(
                    f"{base_url}/hermes-ui.html",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if not response or not response.ok:
                    raise AssertionError("Hermes UI HTML did not load successfully")

                page.wait_for_selector(".app", timeout=30_000)
                page.wait_for_selector('textarea[placeholder*="Message Hermes"]', timeout=30_000)
                version = json.loads(
                    urllib.request.urlopen(f"{base_url}/api/version", timeout=10)
                    .read()
                    .decode("utf-8")
                )
                if version.get("current") != EXPECTED_VERSION:
                    raise AssertionError(f"unexpected current version: {version!r}")
                page.wait_for_function(
                    "(expected) => document.body.innerText.includes('Hermes UI v' + expected)",
                    arg=EXPECTED_VERSION,
                    timeout=15_000,
                )
                if page.locator(".auth-error").count():
                    raise AssertionError("authentication error rendered during startup")
                if page.locator(".error-boundary").count():
                    raise AssertionError("React error boundary rendered during startup")
                if page_errors:
                    raise AssertionError(f"uncaught page errors: {page_errors}")
                if console_errors:
                    raise AssertionError(
                        f"console errors: {console_errors}; bad responses: {bad_responses}"
                    )

                browser.close()
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
            output = process.stdout.read() if process.stdout else ""
            if process.returncode not in (0, -15):
                print(output, file=sys.stderr)

    print("Browser smoke: Hermes UI rendered without page or console errors")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
