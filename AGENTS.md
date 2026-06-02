# AGENTS.md

Guide for AI agents working in `drukbox-python-sdk`.

## What This Project Is

`drukbox-python-sdk` is the async Python SDK for Drukbox's HTTP host API:
a small client library that speaks HTTP, parses typed records, and raises
typed errors.

The package name is `drukbox-python-sdk`; the import package is
`drukbox_sdk`. Public API names use `Sandbox*` because the Drukbox domain
object is a sandbox host.

## Boundaries

- Keep this repo focused on HTTP client behavior, typed records, and typed
  exceptions.
- Do not add SSH session management, file transfer, command execution,
  Tailscale management, VM provider logic, or service-side lifecycle behavior.
- Prefer explicit Drukbox wire contracts over broad defensive parsing.
- Keep dependencies light; justify any new runtime dependency before adding it.

## Layout

```text
src/drukbox_sdk/
  api.py          # SandboxAPI, records, parsers, HTTP request handling
  exceptions.py   # SDK exception hierarchy
  __init__.py     # Public exports
tests/
  test_api.py     # respx-backed SDK contract tests
```

## Development Commands

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

Run the full set when changing Python behavior. For documentation-only edits,
grep for any names you changed and state in the summary that tests were not
run.

## Working Rules

- Read this file, `pyproject.toml`, and the relevant source/tests before
  editing.
- Follow the repo's Python 3.11+ typing style and keep public APIs typed.
- Match the existing async `httpx` style and strict typing.
- Add focused tests for behavior changes.
- Keep docs concise and repo-specific. Avoid references to downstream
  consumers unless the user asks for them.
- Preserve user changes in the worktree; do not clean or rewrite unrelated
  files.
