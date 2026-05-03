# Hermes UI Manual Test Checklist

Use this checklist before tagging a release. Start Hermes Agent first, then run `serve_lite.py` and open `http://localhost:3333/hermes-ui.html`.

## Spaces / Workspaces

- Open the Spaces view from the sidebar.
- Add a new space with Browse, choose a folder, and save it.
- Confirm the new space appears in the sidebar Space selector.
- Switch spaces from the sidebar selector and from the Spaces list.
- Open Files and confirm it shows the selected workspace root.
- Read and edit a small test file inside the selected workspace.
- Start a new chat and ask what workspace is active.
- Use `/workspace <space name>` and confirm the active space changes.
- Remove a test space and confirm files on disk are not deleted.

## Chat And Composer

- Send a normal chat message and confirm streaming completes.
- Open the slash command menu with `/` and run one command.
- Switch chat profile and confirm the outgoing response style changes.
- Paste or type a Mermaid diagram and confirm it renders.
- Trigger a side question and confirm it keeps the active workspace.

## Operations

- Open Settings and confirm the Restart tab is visible.
- Open the live log panel and confirm log streaming still works.
- Open terminal tabs and run a harmless command such as `pwd`.
- Refresh the browser and confirm sessions, selected space, and layout recover.

## Release Sanity

- Run `python3 -m py_compile serve_lite.py`.
- Run `git diff --check`.
- Confirm `serve_lite.py` `__version__`, README badge, and UI footer match the release tag.
- Confirm no local-only files appear in `git status --short`.
