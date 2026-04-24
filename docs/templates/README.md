# Agent Templates

Copy-paste starter configs. Each file describes one agent (oder einen kleinen
Agent-Verbund) das/der in deine `config.json` → `module` array gemerged werden
kann. Alle Templates sind mit der SQLite-basierten Pipeline + Idempotency
kompatibel — Side-Effect-Tools (shell.exec, notify.send, files.write,
aufgaben.erstellen, smtp.*) deduplizieren Retry-Aufrufe automatisch.

| File | What it does | Needs |
|---|---|---|
| `01-simple-chat.json` | Basic personal assistant chat on port 8091. | `grok` backend (or any LlmBackend id you rename to). |
| `02-daily-digest-cron.json` | Cron job fires every morning 8:00, searches, sends notification. | One `websearch` module + one `notify` module. |
| `03-python-coding-helper.json` | Python-specialized chat with editor, taskloop, websearch. | `claude-haiku` as primary + `grok` as backup (or rename). |
| `04-email-triage.json` | Alle 15min ungelesene Mails abrufen, LLM klassifiziert (WICHTIG/NORMAL/SPAM), nur WICHTIGE notify'en. | Python-IMAP-Modul + notify-Modul. |
| `05-web-monitor.json` | Checkt URL alle 10min, vergleicht via RAG mit Vorgänger, notify nur bei echter Änderung. | notify-Modul + optional embedding_backend. |
| `06-system-healthcheck.json` | Systemmetriken (df/free/uptime/systemctl) alle 5min, Alert bei Schwelle. | notify-Modul. Shell-Whitelist eng. |
| `07-rag-knowledge-base.json` | Chat-Assistent der Infos via rag speichert und beim Beantworten bezieht. | Optional embedding_backend für semantische Suche. |

## How to install

```bash
# 1. Pick a template:
cat docs/templates/01-simple-chat.json

# 2. Merge into your config.json:
# Either hand-edit, or use jq:
jq '.module += [input]' agent-data/config.json docs/templates/01-simple-chat.json > /tmp/merged.json \
  && mv /tmp/merged.json agent-data/config.json

# 3. Either restart the server, or use the KI-Wizard in the Admin-UI to create
#    the agent interactively (better UX, fewer typos).
```

## Better: use the Wizard

Instead of copy-pasting JSON, open `http://localhost:8090` → **"Neuer Agent"** → **"KI-Assistent"** and describe what you want in plain German/English. The Wizard builds the config for you, validates everything, and writes to `config.json` on commit. These templates are for users who prefer direct file edits.
