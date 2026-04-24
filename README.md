# Agent Platform

**Self-hosted AI agents with a conversational setup wizard, live quality dashboard, and hard cost caps — built in Rust as a single binary.**

One binary, embedded SQLite (WAL mode) as the canonical state store, no cloud. Drop it on your box, point it at an LLM backend (OpenRouter free tier, Ollama, OpenAI, Anthropic, llama.cpp), and configure agents by talking to them. Python is only needed if you want to run plugin modules (IMAP/SMTP/etc.); the Rust core alone is fully functional.

---

## What makes this different

Most agent frameworks (LangChain, CrewAI, AutoGen, LlamaIndex) are Python libraries you wire up in code. Agent Platform is a **self-hosted runtime with UI**: you don't write glue code, you configure agents through a web dashboard — or by describing them to the built-in AI Wizard.

| | LangChain / LlamaIndex | AutoGen / CrewAI | **Agent Platform** |
|---|---|---|---|
| Runtime | Python library, you build | Python framework, you build | **Rust binary, you run** |
| Setup | Python env + pip | Python env + pip | **One binary** |
| UI | None (build your own) | None (build your own) | **Web dashboard included** |
| Agent creation | Write Python | Write Python | **Talk to the Wizard** |
| Quality observability | None built-in | None built-in | **Live dashboard + benchmark runner** |
| Cost safety | None built-in | None built-in | **Daily USD cap (hard-stop)** |
| Multi-backend | Yes (swap LLM in code) | Yes (swap LLM in code) | **Yes, and A/B-comparable in dashboard** |
| Tool validation | None | None | **Deterministic guardrail before every dispatch** |

Nothing radical in the building blocks — what's new is packaging them into a **runtime that runs without you opening a code editor**.

---

## Quick start

```bash
# 1. Build (or use docker compose up; see Dockerfile)
cd agent
cargo build --release

# 2. Run (creates ./agent-data on first start)
./target/release/agent ./agent-data

# 3. Open the dashboard
xdg-open http://localhost:8090
```

### First-run setup

On first start with an empty config, the server redirects `/` to `/setup` — a page with five LLM-backend presets (OpenRouter free tier, Ollama, OpenAI, Anthropic, llama.cpp). Pick one, paste an API key (where needed), click **Test** to verify with a real round-trip, then **Save**. The server now has a working backend and the agent-creation wizard is enabled automatically.

### Configure your first agent

Click **"New Agent"** → **"AI Assistant"** and describe the agent you want in plain language (German or English — the wizard adapts). The wizard proposes fields (ID, type, LLM backend, system prompt, permissions), you confirm or edit, and it commits to `config.json`. No JSON hand-editing required.

If you prefer direct config editing, see [docs/templates/](docs/templates/) for seven starter agent configurations (simple chat, daily digest cron, python coding helper, email triage, web monitor, system healthcheck, rag knowledge base).

---

## Features

### Agent framework core
- **Per-module scheduler** — each agent runs its own tokio task; a crash or panic in one doesn't affect others (RAII guard ensures cleanup even on unwinding)
- **Three task types** — LLM-tasks (reasoning), direct tool calls (no LLM, deterministic), chain tasks (cron-triggered tool sequences)
- **Exactly-once side-effects** — idempotency table deduplicates retries of shell/notify/files.write/smtp/aufgaben.erstellen by `(task_id, tool, params)` hash
- **Tamper-proof audit log** — every side-effect tool call and config change recorded in SQLite with DB triggers blocking UPDATE/DELETE
- **Atomic task claim** — `BEGIN IMMEDIATE` + `UPDATE WHERE status='erstellt'` in SQLite; no hard-link tricks, no race windows
- **Explicit linking** — agents only call each other when the user links them (no surprise data flow)
- **Python plugin system** — write tools in `modules/<name>/module.py`, subprocess-isolated, discovered at startup with full metadata (settings schema + tools) exposed to the wizard
- **Vector RAG** — cosine-similarity retrieval with keyword fallback, per-agent pools
- **Multi-backend LLM** — Ollama, OpenAI-compatible (incl. OpenRouter, llama.cpp server), Anthropic, Grok (xAI)

### Conversational Wizard
- Describe an agent in natural language; the Wizard proposes every config field
- Split-view UI — chat on the left, live-updating agent preview on the right
- Modes: **new**, **copy from existing**, **edit existing**
- Disk-first session persistence — safe across server restarts
- Optionally: wizard can scaffold new Python-module code (`allow_code_gen` flag, disabled by default)

### Quality dashboard (Guardrail)
- Every LLM tool-call validated before execution — catches hallucinated tool names, malformed JSON, missing parameters
- Structured retry with feedback message on failure (up to N retries, then hard-fail)
- Event log in NDJSON (`agent-data/guardrail-events/YYYY-MM-DD.jsonl`) — filterable, persistent
- **Quality tab** shows: aggregate valid-rate, per-backend/per-model breakdown, top error codes, raw event list
- **Benchmark runner** — 20 curated prompts; click to evaluate how well a given LLM backend works with this framework
- **A/B benchmark** — run two backends in parallel, compare pass-rate per case

### Cost safety
- **Hard daily USD cap** (`daily_budget_usd` in config) — pre-call atomic reservation in SQLite; parallel calls each see the accumulating reservation and fail-fast if the budget would be exceeded
- Model-aware reservation estimate (input tokens × input price + max output × output price) so expensive and cheap models are tracked correctly
- **Anthropic prompt caching** — system prompts ≥4kB get `cache_control: ephemeral` → 90% discount on repeated input tokens within 5 min
- Persistent across restarts (daily cap applies even after `systemctl restart`)
- Per-model price table built in for Claude, GPT, Grok, OpenAI, Ollama families (unknown models cost $0)
- Auto-resets at UTC midnight
- Live cost mini-card in Config tab + per-module and per-backend breakdown APIs

### Quality alerts
- Background task polls valid-rate every 5 min
- If a backend/model drops below threshold → fires `notify.send` through a configured module (ntfy, gotify, telegram, etc.)
- Cooldown to prevent alert spam

### Backup-LLM fallback
- When primary backend hard-fails guardrail validation 2× in a row, automatically retries once with the module's configured `backup_llm`
- Logged as `fallback_triggered` event in the dashboard

### Config safety
- Rotating 3-slot config backup before every save (`config.json.bak-1/2/3`)
- Load-time fallback chain: corrupt current → bak-1 → bak-2 → bak-3 → only then defaults (previous versions silently wiped all modules on parse error)
- Atomic writes with unique per-call temp files (no collision between concurrent writers)
- Global config-write mutex — web API, orchestrator cleanup, wizard commit, and temp-module load all serialize through the same lock in the same order (mutex first, then memory RwLock) to prevent both last-write-wins and lock inversion
- One-click restore from UI

---

## Architecture

```
Orchestrator (monitors all schedulers, fires cron, cleans tasks)
  |
  +-- ModulScheduler: chat.roland     (own heartbeat, own loop, RAII cleanup on panic)
  +-- ModulScheduler: shell.ops       (own heartbeat, own loop)
  +-- ModulScheduler: cron.backup     (fires on schedule, no LLM)
  +-- ModulScheduler: websearch.neu2  (own heartbeat, own loop)
  |
  +-- Store (SQLite+WAL): tasks, audit_log, cron_state, token_stats,
  |                       idempotency, conversations — single source of truth
  +-- Guardrail validator (pre-execute hook for every LLM tool call,
  |                        runs on both OpenAI tool_calls and <tool> fallback)
  +-- Quality alert loop  (5-min poll, fires notify on threshold breach)
  +-- Cost tracker        (atomic SQL reservation, model-aware, persistent)
  +-- Wizard sessions     (disk, archived on commit, knows full py-module metadata)
  +-- Watchdog            (per-scheduler heartbeat; abort + requeue w/ retry count)
```

See [AGENT.md](AGENT.md) for the full architecture: component responsibilities,
SQLite schema, permission model, and lock-order invariants.

### Task pipeline

Tasks live in a SQLite table (`tasks.status` enum: `erstellt` → `gestartet` → `success`/`failed`/`cancelled`). State transitions are atomic via `BEGIN IMMEDIATE` + `UPDATE WHERE status='erstellt' AND faellig_ab_ts <= now`, so multiple schedulers competing for the same task deterministically resolve to one winner (there's a regression test with 50 threads × 50 tasks verifying exactly 50 winners).

Side-effect tools (`shell.exec`, `notify.send`, `files.write`, `smtp.send`, `aufgaben.erstellen`, `agent.spawn`) go through an idempotency gate: a `task_id + tool + params` hash gets a pre-execute `IN_PROGRESS` marker, overwritten with the actual result on success. A retry with the same inputs returns the cached result (exactly-once). Stuck markers from crashes auto-expire after 10 min.

The audit log (`audit_log` table) records every side-effect tool call and config change with DB triggers that block UPDATE/DELETE — tamper-proof at the storage level.

The `tasks.status` column uses five values: `erstellt` (created), `gestartet` (running), and the terminal states `success`, `failed`, `cancelled`. The first two are still German because that was the original source language and renaming them requires a breaking migration on existing installs; terminal states were added in English during the SQLite migration. These are SQLite enum values, **not** filesystem directories. The word `erledigt` survives only as part of column names like `erledigt_ts` (the timestamp at which a task reached a terminal state).

Previous versions did use file-based storage under `agent-data/erstellt/`, `gestartet/`, `erledigt/` JSON folders. Upgrading instances migrate those files into SQLite on first startup and rename the old directories to `*.migrated.<timestamp>` — if you see those folders on an existing install, they are frozen legacy state, not the live task queue.

### Module types

| Type | Purpose | LLM required |
|------|---------|--------------|
| chat | Interactive AI chat with tools | Yes |
| filesystem | Read/write with path whitelist | No |
| websearch | Web search (DuckDuckGo, Brave, Google, Grok, Tavily) | No |
| shell | Command execution with whitelist | No |
| notify | Push notifications (ntfy, gotify, telegram) | No |
| cron | Scheduled tasks (direct, chain, LLM) | Optional |

### Python modules

Place modules in `modules/<name>/module.py` with:
- `MODULE` dict (name, description, tools)
- `handle_tool(tool_name, params, config)` function

Communication is JSON over stdin/stdout. Processes are pooled for performance, killed on drop.

---

## Configuration

All state in `agent-data/config.json`. Sensitive fields (`api_key`, `password`, tokens) auto-redacted in API responses; original values restored on save.

Key sections:

```json
{
  "name": "Agent Platform",
  "web_port": 8090,
  "bind_address": "127.0.0.1",
  "api_auth_token": "optional-bearer-token-for-remote-access",
  "daily_budget_usd": 5.00,
  "llm_backends": [...],
  "module": [...],
  "rag_pools": [...],
  "wizard": {
    "enabled": true,
    "llm": {...},
    "max_rounds_per_session": 30,
    "allow_code_gen": false
  },
  "guardrail": {
    "enabled": true,
    "max_retries": 2,
    "strict_mode": false,
    "alert": {
      "enabled": true,
      "threshold_valid_pct": 70,
      "notify_backend_id": "notify.ntfy"
    },
    "fallback_on_hard_fail": true
  }
}
```

See [docs/templates/](docs/templates/) for three ready-to-merge agent templates.

---

## API endpoints

### Core
| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Admin dashboard (redirects to `/setup` when no backend reachable) |
| GET | `/setup` | First-run setup: pick + test + save an LLM backend |
| GET | `/wizard` | Conversational agent-creation wizard |
| GET | `/chat/{modul_id}` | Per-agent chat page |
| GET/POST | `/api/config` | Get/save full configuration |
| GET | `/api/config/backups` | List 3 backup slots |
| POST | `/api/config/restore/{slot}` | Restore from a backup slot |
| POST | `/api/setup/test-backend` | Real "say hi" round-trip to a candidate backend |
| POST | `/api/setup/save-backend` | Persist a backend and enable the wizard |
| POST | `/api/chat-stream` | Chat NDJSON stream |
| GET | `/api/aufgaben` | List all tasks |
| GET | `/api/status` | Scheduler health + task counts |
| GET | `/api/metrics` | Prometheus metrics |
| GET | `/api/tokens` | Token usage + cost (today + total) |
| GET | `/api/tokens/by-modul?days=N` | Per-module aggregate (top burners) |
| GET | `/api/tokens/by-backend?days=N` | Per-backend/model aggregate (GPT vs DeepSeek etc.) |
| GET | `/api/audit?action=&actor=&since=&limit=` | Filter tamper-proof audit trail |
| GET | `/api/module-capabilities/{id}` | What a module DARES + CAN (permissions + tools in plain text) |

### Wizard
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/wizard/start` | Create session (new/copy/edit mode) |
| POST | `/api/wizard/turn` | NDJSON stream per user message |
| POST | `/api/wizard/patch` | User edits a preview field |
| POST | `/api/wizard/abort` | Discard session |
| GET | `/api/wizard/sessions` | List active sessions |
| GET | `/api/wizard/models?provider=X` | Pull model list from provider |
| POST | `/api/wizard/test-connection` | Ping a backend with a trivial call |
| POST | `/api/wizard/confirm-code-gen` | Approve/reject proposed py-module |

### Quality
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/quality/stats?hours=24` | Aggregate per backend/model |
| GET | `/api/quality/events?since=&limit=&backend=&only_failed=` | Filterable event list |
| GET | `/api/quality/benchmark/cases` | Built-in benchmark suite |
| POST | `/api/quality/benchmark/run` | Run benchmark against a backend (NDJSON) |
| POST | `/api/quality/benchmark/compare` | Run two backends in parallel (NDJSON) |

---

## Security model

- **Transport**: optional Bearer token (`api_auth_token`); without token only 127.0.0.1 is allowed
- **File access**: per-agent path whitelist using canonical `Path::starts_with` (no string-prefix collisions like `/safe` matching `/safe_evil`); `./home/...` paths are NOT expanded to absolute
- **Shell commands**: command whitelist + metacharacter block + **argument path blacklist** (`/etc/`, `/root/`, `~/.ssh`, `id_rsa`, `authorized_keys` etc. refused even if the command itself is whitelisted)
- **Permissions**: each agent declares `berechtigungen`, validated on every tool call through one unified dispatcher. Typ-implicit grants (shell/files/notify/websearch via `typ` field) apply **only to persistent modules** — temp-agents spawned via `agent.spawn` must have every permission explicit
- **`agent.spawn` privilege containment**: spawned temp-agents inherit only safe permissions (rag.*, websearch). `files`, `shell`, `notify`, `agent.spawn`, and `py.*` are stripped regardless of the parent's grants. Temp-agent results route to the creator as `ChatReply` (presented as text), never as `LlmCall` (which would execute them as new instructions — a prompt-injection vector)
- **Module linking**: agents can only create tasks for explicitly linked modules
- **Python permissions**: exact match or `<name>.<instance>` prefix on `linked_modules` — substring matching (which allowed `chat.mail` to grant access to `py.mail`) was removed
- **SSRF**: external URLs validated against private/metadata IP ranges before request **and on every redirect hop** (previous versions only checked the initial URL)
- **Secret redaction**: API keys/passwords redacted in all API responses, restored on save
- **Cost cap**: daily USD budget is a hard-stop (HTTP 402 on exceed), atomic SQL reservation prevents parallel-call overrun
- **Audit log**: tamper-proof by DB trigger (UPDATE/DELETE raise SQLITE error on `audit_log` table)

---

## Development

- **Tests**: `cargo test` — 152 tests covering concurrent claim, audit immutability, cron dedup persistence, idempotency stability, atomic token reservation, path traversal blocking, Python permission exact-match, typ-permission no-leak to temp-agents, schema-order parameter parsing
- **Format**: `cargo fmt`
- **Lint**: `cargo clippy`
- **Release build**: `cargo build --release`
- **Docker**: `docker compose up -d` (runtime-only image, multi-stage build)
- **Multi-LLM code review**: `tools/konklave.sh` dispatches the codebase (or a diff, or a `--focus` angle) to several flagship LLMs via OpenRouter in parallel and collects adversarial reviews. Requires `OPENROUTER_API_KEY` in `~/.konklave/env`. Used during hardening to surface race conditions, privilege escalation paths, and logic holes that weren't obvious from unit tests

No CI setup required to run locally. Contributions welcome.

### A note on honesty

This project went through extensive iterative hardening against multi-LLM
adversarial review (see `tools/konklave.sh`). The git history is a single
squashed commit — the old history was scrubbed because it contained a
Tavily API key in `modules/tavily/config.json` that had to be revoked
before a public push. The squashed commit is the canonical starting point.

External-facing surface (README, `AGENT.md`, `docs/templates/`, `modules/README.md`,
the setup wizard, CLI errors, `tools/konklave.sh`) is English. The task-status
column names (`erstellt`/`gestartet`/`erledigt`) and a subset of internal Rust
comments are still in German — that was the original working language and
renaming them requires a DB migration. German is not a runtime requirement
anywhere; the UI has a language toggle (EN/DE) in the setup wizard.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 678asgfg78asa78sfh. Free to use, modify, and distribute for any purpose including commercial; attribution required per the license terms.
