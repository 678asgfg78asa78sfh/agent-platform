# Agent Platform

**Self-hosted AI agents with a conversational setup wizard, live quality dashboard, and hard cost caps — built in Rust as a single binary.**

No Python stack, no database, no cloud. Drop one binary on your box, point it at an LLM backend, and configure agents by talking to them.

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
# 1. Build
cd agent
cargo build --release

# 2. Run (creates ./agent-data on first start)
./target/release/agent ./agent-data

# 3. Open the dashboard
xdg-open http://localhost:8090
```

### Configure your first agent

Click **"Neuer Agent"** → **"KI-Assistent"** and describe the agent you want in plain language. The Wizard proposes fields (ID, type, LLM backend, system prompt), you confirm or edit, and it commits to `config.json`. No JSON hand-editing required.

If you prefer direct config editing, see [docs/templates/](docs/templates/) for three starter agents.

---

## Features

### Agent framework core
- **Per-module scheduler** — each agent runs its own tokio task; a crash in one doesn't affect others
- **Three task types** — LLM-tasks (reasoning), direct tool calls (no LLM, deterministic), chain tasks (cron-triggered tool sequences)
- **Explicit linking** — agents only call each other when the user links them (no surprise data flow)
- **Python plugin system** — write tools in `modules/<name>/module.py`, subprocess-isolated
- **Vector RAG** — cosine-similarity retrieval with keyword fallback, per-agent pools
- **Multi-backend LLM** — Ollama, OpenAI-compatible, Anthropic, Grok (xAI), OpenRouter

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
- **Hard daily USD cap** (`daily_budget_usd` in config) — blocks LLM calls once budget met, clear error to client
- Auto-resets at UTC midnight
- Per-model price table built in for Claude, GPT, Grok, OpenAI, Ollama families (unknown models cost $0)
- Live cost mini-card in Config tab with progress bar

### Quality alerts
- Background task polls valid-rate every 5 min
- If a backend/model drops below threshold → fires `notify.send` through a configured module (ntfy, gotify, telegram, etc.)
- Cooldown to prevent alert spam

### Backup-LLM fallback
- When primary backend hard-fails guardrail validation 2× in a row, automatically retries once with the module's configured `backup_llm`
- Logged as `fallback_triggered` event in the dashboard

### Config safety
- Rotating 3-slot config backup before every save (`config.json.bak-1/2/3`)
- One-click restore from UI
- Prevents accidental key/permission wipes from misclicks

---

## Architecture

```
Orchestrator (monitors all schedulers, fires cron, cleans tasks)
  |
  +-- ModulScheduler: chat.roland     (own heartbeat, own loop)
  +-- ModulScheduler: shell.ops       (own heartbeat, own loop)
  +-- ModulScheduler: cron.backup     (fires on schedule, no LLM)
  +-- ModulScheduler: websearch.neu2  (own heartbeat, own loop)
  |
  +-- Guardrail validator (pre-execute hook for every LLM tool call)
  +-- Quality alert loop (5-min poll, fires notify on threshold breach)
  +-- Cost tracker (daily USD cap, blocks LLM calls on exceed)
  +-- Wizard sessions (disk-first, archived on commit)
  +-- Watchdog (per-scheduler heartbeat, restart stuck schedulers)
```

### Task pipeline

Files move through three directories:
- `erstellt/` — created, waiting
- `gestartet/` — running
- `erledigt/` — done

Status transitions are atomic (write-new-then-delete-old), so crash recovery just re-scans the filesystem.

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
| GET | `/` | Admin dashboard |
| GET | `/wizard` | Wizard page |
| GET | `/chat/{modul_id}` | Per-agent chat page |
| GET/POST | `/api/config` | Get/save full configuration |
| GET | `/api/config/backups` | List 3 backup slots |
| POST | `/api/config/restore/{slot}` | Restore from a backup slot |
| POST | `/api/chat-stream` | Chat NDJSON stream |
| GET | `/api/aufgaben` | List all tasks |
| GET | `/api/status` | Scheduler health + task counts |
| GET | `/api/metrics` | Prometheus metrics |
| GET | `/api/tokens` | Token usage + cost (today + total) |

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
- **File access**: per-agent path whitelist
- **Shell commands**: command whitelist, shell metacharacter blocking
- **Permissions**: each agent declares `berechtigungen`, validated on every tool call
- **Module linking**: agents can only create tasks for explicitly linked agents
- **SSRF**: external URLs validated against private/metadata IP ranges before request
- **Secret redaction**: API keys/passwords redacted in all API responses, restored on save
- **Cost cap**: daily USD budget is a hard-stop (HTTP 402 on exceed)

---

## Development

- **Tests**: `cargo test` (124 unit + integration tests)
- **Format**: `cargo fmt`
- **Lint**: `cargo clippy`
- **Release build**: `cargo build --release`

No CI setup required to run locally. Contributions welcome.

---

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Martin Sättele. Free to use, modify, and distribute for any purpose including commercial; attribution required per the license terms.
