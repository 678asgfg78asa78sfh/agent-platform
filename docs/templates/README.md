# Agent Templates

Copy-paste starter configs. Each file describes one agent (or a small
group of connected agents) that can be merged into your `config.json`
→ `module` array. All templates are compatible with the SQLite-backed
pipeline + idempotency — side-effect tools (shell.exec, notify.send,
files.write, aufgaben.erstellen, smtp.*) automatically dedupe retry
calls.

| File | What it does | Needs |
|---|---|---|
| `01-simple-chat.json` | Basic personal assistant chat on port 8091. | `grok` backend (or any LlmBackend id you rename to). |
| `02-daily-digest-cron.json` | Cron job fires every morning 8:00, searches, sends notification. | One `websearch` module + one `notify` module. |
| `03-python-coding-helper.json` | Python-specialized chat with editor, taskloop, websearch. | `claude-haiku` as primary + `grok` as backup (or rename). |
| `04-email-triage.json` | Every 15 min: fetch unseen mails, LLM classifies (IMPORTANT/NORMAL/SPAM), notify only for IMPORTANT. | Python IMAP module + notify module. |
| `05-web-monitor.json` | Checks URL every 10 min, compares via RAG with previous snapshot, notify only on real change. | notify module + optional embedding_backend. |
| `06-system-healthcheck.json` | System metrics (df/free/uptime/systemctl) every 5 min, alert on threshold. | notify module. Tight shell whitelist. |
| `07-rag-knowledge-base.json` | Chat assistant that stores info via rag and retrieves it for answers. | Optional embedding_backend for semantic search. |

## How to install

```bash
# 1. Pick a template:
cat docs/templates/01-simple-chat.json

# 2. Merge into your config.json:
# Either hand-edit, or use jq:
jq '.module += [input]' agent-data/config.json docs/templates/01-simple-chat.json > /tmp/merged.json \
  && mv /tmp/merged.json agent-data/config.json

# 3. Either restart the server, or use the AI Wizard in the admin UI to
#    create the agent interactively (better UX, fewer typos).
```

## Better: use the Wizard

Instead of copy-pasting JSON, open `http://localhost:8090` → **"New Agent"**
→ **"AI Assistant"** and describe what you want in plain English or German.
The Wizard builds the config for you, validates everything, and writes to
`config.json` on commit. These templates are for users who prefer direct
file edits.
