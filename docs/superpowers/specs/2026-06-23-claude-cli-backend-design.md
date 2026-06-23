# Spec: `claude -p` als LLM-Backend (Plain-LLM-Modus)

**Datum:** 2026-06-23
**Status:** approved (Design freigegeben)

## Ziel

Einen weiteren LLM-Provider hinzufügen, der lokal die **Claude Code CLI** (`claude -p`)
als reinen Text-LLM anspricht — als Drop-in neben den bestehenden HTTP-Backends
(llama.cpp, DeepSeek, Grok, Anthropic). claude -p läuft im **Plain-LLM-Modus**: eigene
Tools aus, kein Datei-/Shell-Zugriff, gibt nur Antworttext zurück. Das Tool-Calling macht
weiterhin die Turn-Engine des Agenten über Text-Parsing (wie bei llama.cpp), siehe
`parse_dsml_tool_call` in `tools.rs`.

## Nicht-Ziel

- Kein agentischer Sub-Agent-Modus (claude -p führt KEINE eigene Tool-Schleife aus).
- Kein echtes Token-Streaming im MVP (kommt der Antworttext als ein Block).
- Keine Embeddings über claude -p.

## Kosten / Abo

`claude -p` lädt pro Aufruf einen großen Kontext (System-Prompt + Tool-Defs), der CLI-Wert
`total_cost_usd` liegt bei ~$0.05–0.09 pro Call. **Bei einem Claude-Abo ist das nur ein
fiktiver Wert — es wird pro Call nichts berechnet.** Relevant sind nur die Abo-Rate-Limits
(5-Stunden-Fenster). `cost_cap` bleibt daher optional, kein erzwungener Default.

## Architektur

Neuer Enum-Wert `LlmTyp::ClaudeCode`. Anders als alle bisherigen Typen ist das **kein
HTTP-Call**, sondern ein Subprozess. Ein gekapselter Helfer in `llm.rs`:

```
async fn claude_cli_chat(backend: &LlmBackend, messages: &[serde_json::Value])
    -> Result<ClaudeCliResult, String>
```

wird von beiden Chat-Pfaden aufgerufen (non-streaming + `chat_stream`). Im Stream-Pfad wird
das Gesamtergebnis einmalig über `on_chunk` gesendet.

### Subprozess-Aufruf (pro Call)

```
claude -p \
  --output-format json \
  --model   <backend.model> \
  --system-prompt <system-part der messages> \
  --allowedTools "" \
  <flattened conversation via stdin>
```

- `--system-prompt`: alle `role=="system"`-Messages, zusammengefügt. Enthält Persona +
  Tool-Anweisungen als Text → claude antwortet wie der Agent, inkl. DSML-Tool-Calls im Text.
- `--allowedTools ""`: Claude-Codes eigene Tools aus → reiner Text.
- stdin: restliche Messages (user/assistant/tool) geflattet mit Rollen-Markern.
- Env: Subprozess erbt `PATH`/`HOME` des Agenten (claude-Auth liegt in `~/.claude`).
- Timeout: `backend.timeout_s`; bei Überschreitung Prozess killen → Err.

### Conversation-Flattener (reine Funktion, testbar)

`fn flatten_messages(messages) -> (system_prompt: String, prompt: String)`

- Textextraktion je Message über das vorhandene `content_value_text`.
- `system` → `system_prompt` (per `\n\n` verbunden).
- sonst → `prompt`-Zeilen als `"<Role>: <text>"`, per `\n\n` verbunden.

### Output-Parsing (reine Funktion, testbar)

`fn parse_claude_cli_json(stdout) -> Result<ClaudeCliResult, String>`

JSON-Felder: `result` → Antworttext, `is_error`/`subtype` → Fehler, `total_cost_usd` →
Cost-Stat (fiktiv bei Abo), `usage.{input_tokens,output_tokens,cache_*}` → Token-Stats.

## Config-Mapping (`LlmBackend`)

| Feld | Bedeutung bei ClaudeCode |
|------|--------------------------|
| `typ` | `"claude_code"` (neuer Serde-Wert) |
| `url` | Pfad zum `claude`-Binary; leer/Default → `"claude"` (PATH) |
| `api_key` | ignoriert (eigene Auth) |
| `model` | an `--model` (`sonnet`/`opus`/`claude-opus-4-8`) |
| `timeout_s` | Subprozess-Timeout |
| `tool_choice_supported` | als `false` behandeln (Text-Tool-Parsing) |
| `max_tokens` | ignoriert (claude -p hat kein --max-tokens) |
| `cost_cap` | optional |

## Sicherheit

- `validate_llm_backend_url` (`security.rs`): ClaudeCode von der URL-Prüfung ausnehmen
  (kein URL; `url` ist hier ein Binary-Pfad).
- Subprozess wird mit fixem Argv (kein Shell-Interpolieren) gespawnt → keine Command-Injection
  über Modell/Prompt.
- Plain-Modus (`--allowedTools ""`) → kein Datei-/Shell-Zugriff durch claude.

## Edge Cases & Fehlerbehandlung

- claude-Binary nicht gefunden → klare Err-Meldung.
- Non-zero Exit / leeres stdout / nicht-JSON → Err mit stderr-Auszug (max 500 Zeichen).
- `is_error==true` im JSON → Err mit `result`/`subtype`.
- Timeout → Prozess killen, Err.

## Tests

- Unit: `flatten_messages` (system-Trennung, Rollen-Marker, content-Array via
  `content_value_text`, leere Liste).
- Unit: `parse_claude_cli_json` (Erfolg, `is_error`, kaputtes JSON, fehlendes `result`).
- Manuell: Backend in config anlegen, ein Chat über das Frontend, Antwort + Tool-Call prüfen.

## Build / Deploy

`cargo build --release` (0 Fehler), Agent nach Neustart-Regeln neu starten (nur wenn kein
Workflow läuft, per PID killen, verwaiste module.py aufräumen, detached starten).
