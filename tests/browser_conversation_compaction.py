#!/usr/bin/env python3
"""Verify large conversation lists hydrate compactly in the browser."""

import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for_server(url, process, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("serve_lite.py exited before test connected")
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise TimeoutError(f"server did not become ready at {url}")


def make_conversation(index, turns=120, chars=1400):
    messages = []
    for turn in range(turns):
        messages.append({
            "id": f"u-{index}-{turn}",
            "role": "user",
            "content": "prompt " + ("x" * chars),
        })
        messages.append({
            "id": f"a-{index}-{turn}",
            "role": "assistant",
            "content": "reply " + ("y" * chars),
        })
    return {
        "id": f"compact-{index}",
        "title": f"Compact stress {index}",
        "created": "6/24/2026, 12:00:00 AM",
        "last_active_at": f"2026-06-24T07:{index % 60:02d}:00Z",
        "messages": messages,
        "message_count": len(messages),
        "user_prompt_count": turns,
        "tool_call_count": 0,
    }


def main():
    port = free_port()
    base_url = f"http://127.0.0.1:{port}"

    with tempfile.TemporaryDirectory(prefix="hermes-ui-compact-test-") as home:
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
            payload = [make_conversation(i) for i in range(50)]
            raw = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/api/ui-conversations",
                data=raw,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(req, timeout=60).read()

            compact = json.loads(
                urllib.request.urlopen(
                    f"{base_url}/api/ui-conversations?compact=1",
                    timeout=60,
                ).read().decode("utf-8")
            )
            if len(json.dumps(compact).encode("utf-8")) >= 100_000:
                raise AssertionError("compact conversation list is unexpectedly large")
            if any(item.get("messages") for item in compact):
                raise AssertionError("compact conversation list leaked message bodies")

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.add_init_script(
                    """
                    localStorage.setItem('hermes-ui-welcomed', '1');
                    localStorage.setItem('hermes-active-session', 'compact-0');
                    """
                )
                page.goto(f"{base_url}/hermes-ui.html", wait_until="domcontentloaded", timeout=120_000)
                page.wait_for_selector(".app", timeout=120_000)
                page.wait_for_timeout(2500)
                metrics = page.evaluate(
                    """() => {
                      const saved = localStorage.getItem('hermes-conversations') || '';
                      const convs = JSON.parse(saved || '[]');
                      return {
                        localStorageBytes: saved.length,
                        sidebarItems: document.querySelectorAll('.conv-item').length,
                        messageDom: document.querySelectorAll('.message').length,
                        storedMessages: convs.reduce((sum, conv) => sum + (conv.messages || []).length, 0),
                        firstCount: convs[0] && convs[0].message_count,
                      };
                    }"""
                )
                page.get_by_placeholder("Filter chats...").fill("Compact stress 7")
                page.locator(".conv-item", has_text="Compact stress 7").click()
                page.wait_for_timeout(5000)
                switch_metrics = page.evaluate(
                    """() => ({
                      title: document.querySelector('.header-title')?.textContent || '',
                      activeId: localStorage.getItem('hermes-active-session'),
                      messageDom: document.querySelectorAll('.message').length,
                    })"""
                )
                browser.close()

            if metrics["localStorageBytes"] >= 100_000:
                raise AssertionError(f"browser cache too large: {metrics}")
            if metrics["storedMessages"] != 0:
                raise AssertionError(f"browser cache retained messages: {metrics}")
            if metrics["sidebarItems"] != 50:
                raise AssertionError(f"sidebar did not hydrate compact rows: {metrics}")
            if metrics["messageDom"] == 0:
                raise AssertionError(f"active conversation did not load full messages: {metrics}")
            if metrics["firstCount"] != 240:
                raise AssertionError(f"compact row lost message count: {metrics}")
            if switch_metrics["activeId"] != "compact-7" or switch_metrics["messageDom"] == 0:
                raise AssertionError(f"inactive compact row did not hydrate on switch: {switch_metrics}")

            # Simulate a returning user opening an inactive compact row after
            # the browser has POSTed compact metadata back to the server.
            full_after_compact_save = json.loads(
                urllib.request.urlopen(
                    f"{base_url}/api/ui-conversation?id=compact-7",
                    timeout=60,
                ).read().decode("utf-8")
            )
            if len(full_after_compact_save.get("messages") or []) != 240:
                raise AssertionError("compact browser save overwrote inactive full messages")
        finally:
            process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                process.kill()
            output = process.stdout.read() if process.stdout else ""
            if process.returncode not in (0, -15):
                print(output, file=sys.stderr)

    print("Browser compact conversation test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
