#!/usr/bin/env python3
"""Fail when tracked text files contain likely credentials or personal paths."""

import pathlib
import re
import subprocess
import sys


ROOT = pathlib.Path(__file__).resolve().parents[1]
PATTERNS = (
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})\b")),
    ("provider API key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("personal macOS path", re.compile(r"/Users/(?!\.\.\.|<|your|username|USER)[A-Za-z0-9._-]+/")),
    ("personal Linux path", re.compile(r"/home/(?!\.\.\.|<|your|username|USER)[A-Za-z0-9._-]+/")),
)


def tracked_files():
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / item.decode("utf-8") for item in output.split(b"\0") if item]


def main():
    findings = []
    for path in tracked_files():
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\0" in raw:
            continue
        text = raw.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{path.relative_to(ROOT)}:{line_number}: {label}")

    if findings:
        print("Sensitive-data check failed:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1

    print("Sensitive-data check: no likely credentials or personal paths in tracked files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
