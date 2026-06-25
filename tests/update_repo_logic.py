#!/usr/bin/env python3
"""Regression tests for the in-UI update helpers."""

import os
import pathlib
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import serve_lite  # noqa: E402


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-C", str(cwd)] + list(args),
        capture_output=True,
        text=True,
        check=True,
    )
    return (result.stdout or "").strip()


def commit_file(repo, path, text):
    target = pathlib.Path(repo) / path
    target.write_text(text, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-m", f"update {path}")


def new_handler():
    return object.__new__(serve_lite.HermesDirectServer)


def main():
    old_home = os.environ.get("HOME")
    with tempfile.TemporaryDirectory(prefix="hermes-update-logic-") as tmp:
        tmp_path = pathlib.Path(tmp)
        os.environ["HOME"] = str(tmp_path / "home")

        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        install = tmp_path / "install"
        git(tmp_path, "init", "--bare", str(origin))
        git(tmp_path, "clone", str(origin), str(work))
        git(work, "config", "user.email", "test@example.invalid")
        git(work, "config", "user.name", "Hermes Test")

        commit_file(work, "serve_lite.py", "__version__ = '3.3.22'\n")
        git(work, "tag", "v3.3.22")
        commit_file(work, "serve_lite.py", "__version__ = '3.3.24'\n")
        git(work, "tag", "v3.3.24")
        git(work, "push", "--tags", "origin", "HEAD:main")

        git(tmp_path, "clone", str(origin), str(install))
        git(install, "checkout", "v3.3.22")
        if git(install, "describe", "--tags", "--exact-match") != "v3.3.22":
            raise AssertionError("test install did not start on v3.3.22")

        ok, output = new_handler()._update_ui_repo(str(install))
        if not ok:
            raise AssertionError(output)
        if git(install, "describe", "--tags", "--exact-match") != "v3.3.24":
            raise AssertionError(f"detached release checkout did not update:\n{output}")

        branch_install = tmp_path / "branch-install"
        git(tmp_path, "clone", str(origin), str(branch_install))
        commit_file(work, "serve_lite.py", "__version__ = '3.3.25'\n")
        git(work, "tag", "v3.3.25")
        git(work, "push", "--tags", "origin", "HEAD:main")

        ok, output = new_handler()._update_ui_repo(str(branch_install))
        if not ok:
            raise AssertionError(output)
        if git(branch_install, "rev-parse", "HEAD") != git(work, "rev-parse", "HEAD"):
            raise AssertionError(f"branch checkout did not fast-forward:\n{output}")

    if old_home is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = old_home

    print("Update repo logic tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
