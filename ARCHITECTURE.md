# Agent Platform v1.0 — Architecture

## Overview

The Agent Platform is a Rust application that orchestrates multiple AI agents. Each agent has its own scheduler, identity, permissions, and LLM backend. The platform prioritizes deterministic script execution over LLM calls to minimize token usage.

## Core Components

### Orchestrator (`cycle.rs`)
The main coordinator. Runs a loop every 5 seconds that:
1. Reads module configs from the shared config
2. Ensures each module has a running ModulScheduler
3. Restarts crashed schedulers
4. Runs cron checks every 60 seconds
5. Cleans up completed tasks and expired temp agents

### ModulScheduler (`cycle.rs`)
One per module. Runs independently as a tokio task:
- Updates its heartbeat every tick
- Scans `erstellt/` and `gestartet/` for tasks belonging to THIS module only
- Spawns task execution as separate tokio tasks
- Respects `scheduler_interval_ms` from module config

### Pipeline (`pipeline.rs`)
File-based task management:
- `erstellt/` — waiting tasks
- `gestartet/` — in-progress tasks
- `erledigt/` — completed/failed tasks
- Atomic status transitions (write new, then delete old)
- Startup deduplication (handles crash recovery)
- Auto-cleanup of old completed tasks

### LLM Router (`llm.rs`)
Multi-backend LLM abstraction:
- Ollama, OpenAI-compatible, Anthropic, Grok
- Automatic fallback to backup backend
- Embedding API support (`embed()` method)
- Client pooling per timeout value

### Tool System (`tools.rs`)
Permission-checked tool execution:
- Rust built-in tools: files, shell, notify, RAG, aufgaben
- Python module tools: subprocess-based, process-pooled
- OpenAI Function Calling format for LLM integration
- Linking enforcement for inter-module task creation

### RAG (`modules/rag.rs`)
Retrieval-Augmented Generation:
- Vector search via cosine similarity (when embeddings configured)
- Keyword fallback (when no embeddings)
- Per-pool storage (shared, private)
- JSON file storage per entry

### Web API (`web.rs`)
Axum-based HTTP server:
- Admin dashboard (embedded HTML)
- Chat with real NDJSON streaming
- Config management
- Task CRUD
- Prompt preview API
- Per-module chat instances on separate ports

### Python Module Loader (`loader.rs`)
- Discovery: scans `modules/` for `module.py` files
- Communication: JSON over stdin/stdout
- Process Pool: persistent processes, idle cleanup
- Isolation: subprocess per module, kill-on-drop

### Watchdog (`watchdog.rs`)
Monitors each scheduler's heartbeat individually. If a scheduler hasn't updated in 120 seconds, frees its busy slot so the orchestrator can restart it.

## Data Flow

### Chat Request
```
User → POST /api/chat → Spawn tool loop →
  → LLM call (with tools) →
    → Tool call? → Execute tool → Send status via stream →
    → Continue loop until final answer →
  → Stream final text to user
```

### Cron Task
```
Orchestrator.tick_cron() → Check schedule → Create Direct task →
  → ModulScheduler picks up → Execute tool (no LLM) → Done
```

### Module-to-Module
```
Agent A → aufgaben.erstellen(B, "do X") →
  → Check linked_modules → Create task in erstellt/ →
  → B's Scheduler picks up → Execute → route_ergebnis → 
  → New task for A with result
```

## File Structure

```
agent/
├── src/
│   ├── main.rs          # Entry point, config loading, startup
│   ├── cycle.rs          # Orchestrator + ModulScheduler + cron
│   ├── llm.rs            # Multi-backend LLM router + embeddings
│   ├── pipeline.rs       # File-based task pipeline
│   ├── tools.rs          # Tool definitions + execution + permissions
│   ├── web.rs            # HTTP API + streaming chat + dashboard
│   ├── watchdog.rs       # Per-scheduler health monitoring
│   ├── loader.rs         # Python module discovery + process pool
│   ├── util.rs           # Shared utilities
│   ├── types.rs          # All data structures
│   ├── frontend.html     # Admin dashboard (embedded)
│   ├── chat.html         # Standalone chat UI (embedded)
│   └── modules/          # Rust module implementations
│       ├── mod.rs
│       ├── files.rs      # Filesystem tools
│       ├── web.rs        # Web search + HTTP
│       └── rag.rs        # RAG storage + search
├── modules/              # Python plugin modules
├── agent-data/           # Runtime data (tasks, logs, config)
└── Cargo.toml
```
