#!/usr/bin/env python3
"""Verify that release metadata has one authoritative version source."""

import argparse
import pathlib
import re
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]


def fail(message):
    print(f"release consistency: {message}", file=sys.stderr)
    return 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Expected release tag, such as v3.3.22")
    args = parser.parse_args()

    server_text = (ROOT / "serve_lite.py").read_text(encoding="utf-8")
    html_text = (ROOT / "hermes-ui.html").read_text(encoding="utf-8")
    readme_text = (ROOT / "README.md").read_text(encoding="utf-8")

    match = re.search(r'^__version__\s*=\s*"([^"]+)"', server_text, re.MULTILINE)
    if not match:
        return fail("serve_lite.py does not define __version__")
    version = match.group(1)

    if args.tag and args.tag != f"v{version}":
        return fail(f"tag {args.tag!r} does not match serve_lite.py version v{version}")

    hardcoded_ui_versions = re.findall(r"Hermes UI v\d+(?:\.\d+)+", html_text)
    if hardcoded_ui_versions:
        return fail(f"hardcoded UI version found: {hardcoded_ui_versions[0]}")

    if "img.shields.io/github/v/release/pyrate-llama/hermes-ui" not in readme_text:
        return fail("README badge is not driven by the latest GitHub Release")

    if f"## What's new in v{version}" not in readme_text:
        return fail(f"README is missing a What's new in v{version} section")

    print(f"release consistency: v{version} is coherent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
