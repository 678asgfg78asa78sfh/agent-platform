# Agent Platform — Architecture

> If you want the "what it is and how to run it" view, read [README.md](README.md).
> This doc is the single source of truth for how the system is built.

## Core thesis

**The LLM is a tool the cycle uses, not the other way round.**

Classic AI-agent frameworks put the LLM in control: it chooses tools, loops,
recovers from errors, decides when to stop. This gives you hallucination
cascades, runaway loops, and stateful prompts the length of a novel.

Agent Platform inverts that: deterministic Rust code owns the control flow.
The LLM is a stateless function — given a prompt, produce either (a) a final
answer or (b) a strictly-validated tool call. Nothing else. The cycle
schedules tasks, routes tool calls through permission checks, enforces
budgets, records every side-effect to a tamper-proof audit log, and hands
results back.

## State backend

**SQLite + WAL mode** is the single source of truth for all persistent
state. `rusqlite` is bundled statically (no system libsqlite3 dependency),
so the binary stays self-contained.

Tables (see `src/store.rs::SCHEMA`):

| Table | Purpose |
|---|---|
| `tasks` | The pipeline. Status machine: `erstellt` → `gestartet` → `success`/`failed`/`cancelled`. Atomic claim via `BEGIN IMMEDIATE` + `UPDATE WHERE status='erstellt' AND faellig_ab_ts <= now`. |
| `audit_log` | Append-only. UPDATE/DELETE triggers block modification at DB level — forensic evidence for every side-effect tool call + config change. |
| `cron_state` | Minute-granular cron dedup. Survives restarts (was a JSON file pre-migration). |
| `token_stats` | Daily token + cost aggregates with atomic reservation window. `daily_budget_usd` cap is enforced per model via pre-call reservation, committed or released after the LLM call returns. |
| `token_calls` | Ring buffer (cap 200 via trigger) of recent LLM calls for the UI. |
| `idempotency` | Side-effect deduplication. Pre-mark `IN_PROGRESS` before a tool runs, overwrite with the result after success, delete on failure. Retry with identical `(task_id, tool, params)` hits the cache (exactly-once). Stuck `IN_PROGRESS` markers expire after 10 min. |
| `conversations` | Chat history per module. |

File-based storage is kept for two cases only:
- **Logs** (`agent-data/logs/YYYY-MM-DD.jsonl`) — append-only NDJSON, works
  with `tail`/`jq`/`grep` without opening SQLite.
- **Module home directories** (`agent-data/home/<module_id>/`) — user files
  the agent creates via `files.write`.

Legacy JSON state (`erstellt/gestartet/erledigt/`) from pre-SQLite releases
is migrated once at first startup and archived as `.migrated.<name>/`.

## Components

### Orchestrator (`cycle.rs`)
Top-level loop. Runs every 5 seconds:
1. Load temp-module specs from `agent-data/temp_modules/*.json` (two-phase
   commit: persist config first, then delete spec)
2. Ensure every configured module has a running scheduler task
3. Respawn if scheduler's heartbeat is stale (watchdog signal)
4. Tick cron every 30 seconds (decoupled from cleanup interval; no minutes
   can be skipped)
5. Cleanup every N seconds (configurable): old tasks, idle Python processes,
   log rotation, idempotency expiry, orphan-task failure

### ModulScheduler (`cycle.rs`)
One per module, independent tokio task. Each tick:
- Update heartbeat
- Claim the next due task for this module (SQL transaction, atomic)
- Spawn task execution in its own task with a `BusyGuard` that cleans up
  busy+handles maps on drop — even if the inner task panics

### Watchdog (`watchdog.rs`)
Checks every 10s: any scheduler whose heartbeat hasn't been updated within
`timeout_secs`? If yes:
1. Abort the scheduler's running task handles (via `AbortHandle` map)
2. Transition stuck tasks back to `erstellt` (unless they already finished —
   checked in SQL, not filesystem)
3. Count the abort as a retry; exceeding `task.retry` marks `FAILED`
4. Clear heartbeat so orchestrator respawns a fresh scheduler

### LLM Router (`llm.rs`)
Multi-backend chat dispatch:
- **Ollama**: local, `/api/tags` + `/api/chat`
- **OpenAI-compatible**: OpenAI, OpenRouter, llama.cpp server, LM Studio
- **Anthropic**: Claude with **prompt caching** (system prompts ≥4kB get
  `cache_control: ephemeral` → 90% discount on repeated input tokens)
- **Grok (xAI)**

Each LLM call flows through: daily-budget reservation → chat dispatch →
token tracking (with correct cost model for cached reads at 10%, writes
at 125%) → result. Connection pool is keyed per timeout.

### Tool system (`tools.rs` + `modules/`)

Two layers:

**1. Built-in Rust tools** — compiled into the binary:
- `files.read/write/list` (sandboxed to `allowed_paths` via
  canonical-path prefix match; `/etc/`, `~/.ssh`, `id_rsa` etc. are
  shell-arg blacklisted)
- `web.search`, `http.get` (SSRF-protected: every redirect hop validated,
  not just the initial URL)
- `shell.exec` (command whitelist + metacharacter block + sensitive-path
  argument blacklist)
- `notify.send` (ntfy, gotify, telegram)
- `rag.suchen/speichern` (keyword + optional vector search, per-pool)
- `aufgaben.erstellen` (create tasks for linked modules)
- `agent.spawn` (create temp sub-agent; inherits only safe permissions,
  no `files`/`shell`/`notify`/`agent.spawn`)

**2. Python plugin modules** — external processes in `modules/<name>/module.py`:
- Standard interface: one `module.py` with a `MODULE` dict (description,
  settings schema, tools) and a `handle_tool(name, params, config)` function
- Communication over stdin/stdout JSON lines
- Process pool keeps Python running (idle timeout); tolerates stdout
  pollution from `print()` during init
- Discovered at startup via `loader::discover_modules`; the wizard exposes
  each plugin's metadata to the user

Every side-effect tool call goes through **one** unified dispatcher
(`exec_tool_unified`) which handles: idempotency gate, audit log, permission
check, Rust execution, Python fallback.

### Permission model

Granted in two ways:

- **Explicit** via `ModulConfig.berechtigungen`: string list like
  `["rag.personal", "aufgaben", "py.imap"]`
- **Type-implicit** via `ModulConfig.typ` — but **only for persistent
  modules**. Temp-agents (persistent=false) get zero implicit grants;
  every permission must be explicit. This closes the privilege-escalation
  vector where an agent.spawn could have inherited `shell` access via
  `typ: "shell"` alone.

Python module access: requires either `py.<name>` in `berechtigungen` or
an exact-match `linked_modules` entry (prefix `<name>.<instance>` also
allowed). Substring matching was removed — `chat.mail` no longer grants
access to `py.mail`.

### Guardrail (`guardrail.rs`)
Deterministic pre-execution validator. Catches hallucinated tool names
(Levenshtein suggestions for typos), malformed JSON, missing required
params. Supplies structured error feedback back to the LLM for retry, up
to N times; hard-fails and optionally falls back to a backup LLM. Events
logged to `agent-data/guardrail-events/` with per-backend/per-model
aggregates, alert threshold + notify integration.

The guardrail runs on every LLM response — including the legacy
`<tool>name(params)</tool>` fallback path, which is synthesized into the
same OpenAI-shape `tool_calls` structure before validation so the
schema-based parameter ordering applies uniformly.

### Web server (`web.rs`)
Axum-based, serves:
- `/` — admin dashboard (single-page, embedded HTML) with tabs: Config,
  Tasks, Monitor, Cron, Quality, Audit
- `/setup` — first-run wizard (redirected to from `/` when no backend is
  reachable)
- `/chat/<modul_id>` — chat UI for chat modules
- `/wizard` — conversational agent-creation wizard
- REST APIs for all of the above plus `/api/tokens/by-modul|by-backend`,
  `/api/audit`, `/api/module-capabilities/<id>`

## Wizards

Two distinct wizards:

**Setup Wizard** (`/setup`, `src/setup.html`): first-run only. Presents
5 LLM-backend presets (OpenRouter, Ollama, OpenAI, Anthropic, llama.cpp),
tests the user's config with a real "say hi" round-trip, saves on
success. Config stays empty until the user picks something here — no
fake Ollama placeholder polluting the config.json.

**Agent Creation Wizard** (`/wizard`, `src/wizard.rs`): LLM-driven
dialog for building module configs. Has access to `wizard.list_py_modules`
(with full metadata: description, settings schema, tools per plugin) and
understands the full permission model. Builds `ModulConfig` via
`wizard.propose(field, value, reasoning)` calls; validates before
committing.

## Config safety

`config.json` is protected by:
- Rotating 3-slot backup before every save (`config.json.bak-1/2/3`)
- Fallback chain on load: corrupt current → try bak-1 → bak-2 → bak-3
  → only then defaults. Previous versions silently fell back to an empty
  default config and wiped all modules on a single parse error.
- Atomic writes (temp file + rename) with unique per-call temp names so
  concurrent writers don't collide on the same intermediate file
- **Global config-write mutex** in `Pipeline.config_write_lock`; all
  writers (web API, orchestrator cleanup, wizard commit, temp-module
  load) acquire it in the same order (mutex first, then `RwLock::write`)
  to prevent both last-write-wins and lock inversion

## Budget enforcement

`daily_budget_usd` is a hard cap, not advisory:

1. Pre-call `check_daily_budget` computes a model-aware reservation
   (estimated input × input_price + max_output × output_price) and
   atomically reserves it in `token_stats.reserved_usd`. Parallel calls
   each see the accumulating reservation and fail fast if the total
   would exceed the budget.
2. Post-call `track_tokens` commits the actual cost and releases the
   reservation in a single SQL transaction.
3. On error, `release_reservation` returns the reserved amount.

Persistence means the cap applies across restarts. A call that would
push over the cap fails with a structured error before touching the LLM.

## Testing

152 tests, covering in particular the invariants that matter under
concurrency: atomic claim (50 threads × 50 tasks → exactly 50 winners),
audit-log immutability (UPDATE/DELETE fail), cron dedup persistence
across restarts, idempotency key stability, atomic token reservation
enforcement, path traversal blocking (prefix-collision, non-canonical
paths), Python permission exact-match (no substring leaks), typ-
permission no-leak to temp-agents, schema-order parameter parsing.

## What lives where

```
src/
  main.rs           startup, config loading with backup fallback
  cycle.rs          Orchestrator + ModulScheduler + BusyGuard + cron tick
  watchdog.rs       heartbeat watchdog with retry-counted requeue
  pipeline.rs       Pipeline adapter over Store (task API)
  store.rs          SQLite schema + all DB operations
  llm.rs            multi-backend LLM dispatch + prompt caching
  tools.rs          unified tool dispatcher, permission checks, built-in tools
  guardrail.rs      deterministic LLM-output validator + events
  wizard.rs         conversational agent-creation wizard
  web.rs            axum HTTP server + admin dashboard routes
  frontend.html     single-page admin UI (tabs, modals)
  setup.html        first-run backend setup
  wizard.html       wizard UI
  chat.html         per-module chat UI
  modules/          Rust-native tool implementations
  security.rs       safe_id, path sanitization, rate limiter, SSRF check
  util.rs           atomic_write, truncation helpers
  loader.rs         Python plugin discovery + process pool

modules/            Python plugins (one dir per module)
  <name>/module.py  MODULE dict + handle_tool function
  <name>/config.json  instance config (gitignored, per-deployment)

docs/templates/     starter agent configs (JSON, merge into config.json)
tools/konklave.sh   multi-LLM adversarial code review via OpenRouter
agent-data/         runtime state (gitignored): tasks.db, logs/, home/
```
