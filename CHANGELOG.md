# Changelog

## Unreleased

## 0.1.0 — 2026-10-02

- Added task-oriented CLI and seven official-SDK MCP tools using OpenSSH multiplexing and remote tmux, with durable task IDs, same-request deduplication, explicit states, incremental bounded logs, cancellation and timeout.
- Added streaming uploads and completion-gated artifact retrieval with SHA256 verification and atomic per-file replacement; preserve unknown submission state without replaying side effects.
- Validated on solvinglab with unit, real-server and stdio MCP tests; recorded scoped latency and returned-output token measurements.
- Added a standalone Linux x86_64 / WSL release containing Python, the MCP SDK and OpenSSH, with SHA256-checked installation, versioned directories and tag-triggered GitHub Releases.
- Added the public server catalog, existing-key discovery, passwordless-deployment records and detailed agent instructions; exclude credentials and private runtime data from source and release artifacts.
- Isolated MCP stdio handles so SDK shutdown cannot close the frozen runtime’s process output.
