# Changelog

## Unreleased

## 0.4.0 — 2026-10-02

- Removed the bundled server catalog and deployment templates. Connections now require an external profile with an explicit SSH target and tmux session.
- Kept installation and releases independent of private configuration, authentication records and raw test reports; live tests take explicit server parameters and save reports outside the repository.

## 0.3.0 — 2026-10-02

- Added interactive `connect SERVER [--session NAME]` to open SSH directly into tmux, remembering the most recently selected target across client restarts and connection loss. First use defaults to the configured session; missing sessions are created without detaching existing clients.
- Kept interactive session selection separate from automated CLI/MCP task execution, with real PTY checks for session resumption, retained shell state and simultaneous clients.

## 0.2.1 — 2026-10-02

- Isolated download subprocess stdin from the MCP JSON-RPC stream. Concurrent downloads could otherwise consume subsequent tool requests, leaving fetched files on disk but closing or stalling the MCP session.
- Added real Codex subagent workflow tests covering synthetic R analysis, dependent plotting, mixed parallel MCP jobs, independent-process handoff and lost-acknowledgement recovery.

## 0.2.0 — 2026-10-02

- Added batched CLI/MCP status queries and one-stream multi-artifact downloads with streaming hashes and completion-gated atomic delivery.
- Added bounded network retries for observations/downloads using fresh connections, configurable deadlines and default SSH compression; retain recovery IDs for incomplete submissions and uploads without automatic replay.
- Detected truncated artifact/JSON responses even when a disconnected OpenSSH mux client reports exit 0, permitting one safe read retry without publishing partial files.
- Protected concurrent local task registration with locks and atomic records; verified isolated network faults, 32-way real-server concurrency, transfer integrity and standalone releases.

## 0.1.0 — 2026-10-02

- Added task-oriented CLI and seven official-SDK MCP tools using OpenSSH multiplexing and remote tmux, with durable task IDs, same-request deduplication, explicit states, incremental bounded logs, cancellation and timeout.
- Added streaming uploads and completion-gated artifact retrieval with SHA256 verification and atomic per-file replacement; preserve unknown submission state without replaying side effects.
- Validated on a configured remote host with unit, real-server and stdio MCP tests; recorded scoped latency and returned-output token measurements.
- Added a standalone Linux x86_64 / WSL release containing Python, the MCP SDK and OpenSSH, with SHA256-checked installation, versioned directories and tag-triggered GitHub Releases.
- Added the connection setup and local authentication support and detailed agent instructions; exclude credentials and private runtime data from source and release artifacts.
- Isolated MCP stdio handles so SDK shutdown cannot close the frozen runtime’s process output.
