#!/usr/bin/env bash
# Konklave — multi-LLM program-logic review for the aistuff agent.
#
# Sendet den Rust-Codebase (oder einen Diff) parallel an mehrere Top-LLMs
# via OpenRouter und sammelt pro Modell ein adversariales PROGRAMM-LOGIK-
# Review ein. Kernfrage: "Ist das Programm als Ganzes geil?"
#
# Der Review fokussiert AUSDRÜCKLICH auf:
#   - Ist die Architektur kohärent? Hält das Konzept in der Praxis?
#   - Versteckte Logik-Flaws: Races, Fallbacks-die-schweigend-fehlschlagen,
#     State-Transitions die Daten verlieren, nicht-enforcte Invarianten.
#   - Was ist das Dümmste und das Klügste im Design?
#
# Anthropic-Modelle sind bewusst ausgeschlossen: der menschliche Orchestrator
# (ich, Claude) synthetisiert die Reviews, ein Claude-Peer bringt keine
# Perspektiv-Diversität.
#
# Modell-Auswahl: Das Tool zieht bei jedem Lauf `/v1/models` von OpenRouter,
# validiert die Preset-Liste dagegen und warnt wenn ein Modell nicht mehr
# existiert. So bleibt die Liste nicht länger als nötig veraltet. Mit
# `--models` kann man manuell überschreiben.
#
# Usage:
#   ./tools/konklave.sh                          # default: core-Layer, general, flagship preset
#   ./tools/konklave.sh --full                   # inkl. web.rs + wizard.rs
#   ./tools/konklave.sh --focus concurrency      # fokussierter Review
#   ./tools/konklave.sh --preset cheap           # günstiger Modell-Mix
#   ./tools/konklave.sh --models "openai/gpt-5.4,x-ai/grok-4"
#   ./tools/konklave.sh --diff HEAD~5..HEAD      # klassischer Diff-Review
#   ./tools/konklave.sh --list-models            # zeigt aktive Modelle + Preise, exit
#
# Presets (Pro Provider wird MAX EIN Modell gewählt → echte Lab-Diversität):
#   flagship  (default)  ~$1-2/Review, beste verfügbaren Modelle
#   balanced             ~$0.30/Review, solide Mischung
#   cheap                ~$0.05/Review, aggressiv günstig, Reasoning-starke
#
# Focus-Modi:
#   general       (default) breiter adversarialer Programm-Logik-Review
#   concurrency   Races, Deadlocks, Watchdog/Scheduler-Timing, Async-Bugs
#   resilience    Was bricht bei LLM-Down, Disk-Full, Config-Corrupt, Crash-Recovery
#   security      Tool-Sandbox, Permission-Bypass, Input-Validation, Injection
#   ux            Nutzer-Flow: Kann er sich festfahren? Recovery? Defaults?
#   consistency   Invarianten, State-Machine-Korrektheit, Cross-File-Integrität
#
# Env: OPENROUTER_API_KEY (aus ~/.konklave/env oder ~/.bwdms-review/env)
# Output: reviews/<ts>_<focus>/<slug>.md + _manifest.md

set -euo pipefail

# ===== 0. Env + repo root ===================================================

if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    for f in "$HOME/.konklave/env" "$HOME/.bwdms-review/env"; do
        if [ -f "$f" ]; then
            # shellcheck disable=SC1090
            . "$f"
            break
        fi
    done
fi
if [ -z "${OPENROUTER_API_KEY:-}" ]; then
    echo "ERROR: OPENROUTER_API_KEY nicht gesetzt." >&2
    echo "  → ~/.konklave/env anlegen mit: OPENROUTER_API_KEY=sk-or-..." >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

command -v jq   >/dev/null || { echo "ERROR: jq nicht installiert"   >&2; exit 2; }
command -v curl >/dev/null || { echo "ERROR: curl nicht installiert" >&2; exit 2; }

# ===== 1. Arg parsing =======================================================

PRESET="flagship"
FOCUS="general"
MODE="core"           # core | full | diff | doc-only
DIFF_RANGE=""
MODELS_OVERRIDE=""
LIST_MODELS_ONLY=false

while [ $# -gt 0 ]; do
    case "$1" in
        --preset)      PRESET="$2"; shift 2 ;;
        --focus)       FOCUS="$2"; shift 2 ;;
        --core)        MODE="core"; shift ;;
        --full)        MODE="full"; shift ;;
        --doc-only)    MODE="doc-only"; shift ;;
        --diff)        MODE="diff"; DIFF_RANGE="$2"; shift 2 ;;
        --models)      MODELS_OVERRIDE="$2"; shift 2 ;;
        --list-models) LIST_MODELS_ONLY=true; shift ;;
        -h|--help)     sed -n '1,55p' "$0"; exit 0 ;;
        *)             echo "ERROR: unbekanntes Argument: $1" >&2; exit 2 ;;
    esac
done

case "$PRESET" in
    flagship|balanced|cheap) ;;
    *) echo "ERROR: --preset muss flagship|balanced|cheap sein" >&2; exit 2 ;;
esac
case "$FOCUS" in
    general|concurrency|resilience|security|ux|consistency) ;;
    *) echo "ERROR: --focus ungültig (general|concurrency|resilience|security|ux|consistency)" >&2; exit 2 ;;
esac

# ===== 2. Modell-Preset-Definitionen ========================================
# Jede Zeile ist ein Wunsch-Slot PRO PROVIDER. Das Script wählt das beste
# aktuell-verfügbare Modell pro Slot aus /v1/models (priorität: erste
# Regex-Gruppe gewinnt pro Provider-Prefix). So bleibt die Liste robust
# wenn Provider Versionen bumpen (gpt-5.4 → gpt-5.5 etc.).
#
# Diversitäts-Regel: MAX 1 Modell pro Lab. Die Slots unten sind bereits so
# gewählt dass jedes Slot ein anderes Lab abdeckt.
#
# Prioritäts-Regex pro Slot: Wenn mehrere Modelle matchen, wird nach
# length(id) DESC sortiert — heuristisch: "qwen3-max-thinking" gewinnt
# gegen "qwen3-max" (das "thinking" Suffix = bewussteres Reasoning).
# Dann nach alphabetischer ID (höhere Version gewinnt: gpt-5.4 > gpt-5.1).

flagship_slots=(
    "^openai/gpt-5(\.|$)"
    "^google/gemini-3(\.|$)"
    "^x-ai/grok-4(\.|$|-)"
    "^qwen/qwen3.*(thinking|max|plus)"
    "^deepseek/deepseek-(v|r)"
    "^moonshotai/kimi.*thinking"
    "^z-ai/glm-"
)

balanced_slots=(
    "^openai/gpt-5(\.[0-2])"
    "^google/gemini-(2\.5|3\.0)-pro"
    "^qwen/qwen3.*thinking"
    "^moonshotai/kimi.*thinking"
    "^deepseek/deepseek-v"
    "^z-ai/glm-"
)

cheap_slots=(
    "^deepseek/deepseek-v"
    "^x-ai/grok-4.*fast"
    "^z-ai/glm-"
    "^qwen/qwen3.*(coder|thinking)"
    "^moonshotai/kimi.*(thinking|k2)"
)

case "$PRESET" in
    flagship)  SLOTS=("${flagship_slots[@]}"); MAX_OUT_PRICE=0.00003 ;;  # <= $30/M out
    balanced)  SLOTS=("${balanced_slots[@]}"); MAX_OUT_PRICE=0.000005 ;; # <= $5/M  out
    cheap)     SLOTS=("${cheap_slots[@]}");    MAX_OUT_PRICE=0.000002 ;; # <= $2/M  out
esac

# ===== 3. OpenRouter: Live-Modelle ziehen + filtern =========================

MODELS_JSON="$(curl -sS https://openrouter.ai/api/v1/models)" || {
    echo "ERROR: /v1/models fetch fehlgeschlagen" >&2
    exit 3
}

# Grundfilter:  Context >= 100k, nicht anthropic/, nicht *:free (rate-limited),
# hat Preise, keine Pseudomodelle (pricing == -1), Output-Preis <= MAX_OUT_PRICE
# des gewählten Presets (verhindert dass flagship $180/M Output-Varianten wie
# gpt-5.4-pro picks).
candidates_json="$(echo "$MODELS_JSON" | jq --argjson max_out "$MAX_OUT_PRICE" '[.data[]
    | select(.context_length >= 100000)
    | select((.id | startswith("anthropic/")) | not)
    | select((.id | endswith(":free")) | not)
    | select((.id | startswith("openrouter/")) | not)
    | select((.pricing.completion | tonumber? // -1) > 0)
    | select((.pricing.completion | tonumber?) <= $max_out)
    | {
        id: .id,
        ctx: .context_length,
        in: (.pricing.prompt     | tonumber? // 0),
        out: (.pricing.completion | tonumber? // 0)
      }
    ]')"

# Pro Slot: (a) match slot-regex, (b) Restriktions-Varianten wegfiltern
# (-mini, -flash, -distill, -vl, date-stamps etc. — die sind kleiner/
# spezialisierter, nicht das Flagship), (c) Capability-Suffixe belohnen
# (-thinking, -reasoning, -speciale, -codex, -pro, -max, -plus, -ultra),
# (d) höchste Version ID-alphabetisch zuletzt gewinnt.
pick_for_slot () {
    local rx="$1"
    echo "$candidates_json" | jq -r --arg rx "$rx" '
        [.[]
         | select(.id | test($rx))
         | select((.id | test("-(mini|nano|lite|small|flash|air|turbo|image|chat|customtools|multi-agent|image-2)($|-)")) | not)
         | select((.id | test("-(distill|vl)-")) | not)
         | select((.id | test("-[0-9]{4}$|-[0-9]{4}-")) | not)
        ]
        | sort_by(
            # Tier 1 (höchste Priorität): Reasoning-Kombis mit Max/Pro/Codex
            if (.id | test("-(max|pro|codex)-(thinking|reasoning)")) then 6
            elif (.id | test("-(thinking|reasoning)-(max|pro|codex)")) then 6
            # Tier 2: reines -speciale (DeepSeek-spezifisch für reasoning)
            elif (.id | test("-speciale")) then 5
            # Tier 3: -thinking / -reasoning solo
            elif (.id | test("-(thinking|reasoning)")) then 4
            # Tier 4: -codex / -max / -pro / -plus / -ultra solo
            elif (.id | test("-(codex|max|pro|plus|ultra)")) then 3
            # Tier 5: Basis-ID ohne Suffix (oft das Flagship selbst, z.B. grok-4.20)
            elif ((.id | split("/")[1] | test("-")) | not) then 2
            else 1
            end,
            # Sekundär: Version ID DESC (gpt-5.4 > gpt-5.3)
            .id
        )
        | reverse
        | .[0] // empty
        | .id
    '
}

price_for () {
    local id="$1"
    echo "$candidates_json" | jq -r --arg id "$id" '
        (.[] | select(.id == $id)) as $m
        | if $m == null then "?" else
            "in=$\(($m.in  * 1000000) | . * 100 | round / 100)/M  out=$\(($m.out * 1000000) | . * 100 | round / 100)/M  ctx=\(($m.ctx / 1000 | round))k"
          end
    '
}

SELECTED=()
if [ -n "$MODELS_OVERRIDE" ]; then
    IFS=',' read -r -a override_arr <<< "$MODELS_OVERRIDE"
    for m in "${override_arr[@]}"; do
        m="${m// /}"
        SELECTED+=("$m")
    done
else
    for slot in "${SLOTS[@]}"; do
        pick="$(pick_for_slot "$slot")"
        if [ -z "$pick" ]; then
            echo "  ⚠ kein Modell für Slot »$slot« verfügbar, skip" >&2
            continue
        fi
        SELECTED+=("$pick")
    done
fi

if [ ${#SELECTED[@]} -eq 0 ]; then
    echo "ERROR: keine Modelle selektiert. --list-models zum Debuggen." >&2
    exit 3
fi

echo "→ preset:  $PRESET"
echo "→ focus:   $FOCUS"
echo "→ mode:    $MODE${DIFF_RANGE:+ ($DIFF_RANGE)}"
echo "→ models:"
for m in "${SELECTED[@]}"; do
    printf "    %-40s %s\n" "$m" "$(price_for "$m")"
done
echo

if [ "$LIST_MODELS_ONLY" = true ]; then
    exit 0
fi

# ===== 4. Source-Assembly ===================================================

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="reviews/${TS}_${FOCUS}"
mkdir -p "$OUT_DIR"
PAYLOAD_FILE="$OUT_DIR/_payload.txt"

CORE_FILES=(
    AGENT.md
    ARCHITECTURE.md
    src/main.rs
    src/cycle.rs
    src/pipeline.rs
    src/llm.rs
    src/tools.rs
    src/loader.rs
    src/guardrail.rs
    src/watchdog.rs
    src/security.rs
    src/types.rs
    src/util.rs
    src/modules/mod.rs
    src/modules/files.rs
    src/modules/rag.rs
    src/modules/web.rs
)

FULL_EXTRA=(
    src/web.rs
    src/wizard.rs
)

DOC_ONLY=(
    AGENT.md
    ARCHITECTURE.md
)

append_file () {
    local f="$1"
    if [ ! -f "$f" ]; then
        echo "  ⚠ fehlt: $f" >&2
        return
    fi
    {
        echo
        echo "================================================================"
        echo "FILE: $f   ($(wc -l <"$f") Zeilen)"
        echo "================================================================"
        cat "$f"
    } >> "$PAYLOAD_FILE"
}

: > "$PAYLOAD_FILE"

case "$MODE" in
    core)
        for f in "${CORE_FILES[@]}"; do append_file "$f"; done
        ;;
    full)
        for f in "${CORE_FILES[@]}" "${FULL_EXTRA[@]}"; do append_file "$f"; done
        ;;
    doc-only)
        for f in "${DOC_ONLY[@]}"; do append_file "$f"; done
        ;;
    diff)
        if [ -z "$DIFF_RANGE" ]; then echo "ERROR: --diff braucht range" >&2; exit 2; fi
        echo "================================================================" >> "$PAYLOAD_FILE"
        echo "GIT DIFF: $DIFF_RANGE"                                              >> "$PAYLOAD_FILE"
        echo "================================================================" >> "$PAYLOAD_FILE"
        git diff "$DIFF_RANGE" --no-color -- src/ AGENT.md ARCHITECTURE.md       >> "$PAYLOAD_FILE" || true
        ;;
esac

PAYLOAD_BYTES=$(wc -c < "$PAYLOAD_FILE")
PAYLOAD_LINES=$(wc -l < "$PAYLOAD_FILE")
echo "→ payload: $PAYLOAD_BYTES bytes, $PAYLOAD_LINES lines"

# Hard cap (alle Reviewer ≥ 128k ctx; 400k chars ≈ 100k tokens passt sicher)
MAX_BYTES=400000
if [ "$PAYLOAD_BYTES" -gt "$MAX_BYTES" ]; then
    echo "  ⚠ >$((MAX_BYTES/1000))kB, trunciere auf $((MAX_BYTES/1000))kB" >&2
    head -c "$MAX_BYTES" "$PAYLOAD_FILE" > "$PAYLOAD_FILE.tmp"
    echo -e "\n\n[… TRUNCATED bei ${MAX_BYTES} bytes …]" >> "$PAYLOAD_FILE.tmp"
    mv "$PAYLOAD_FILE.tmp" "$PAYLOAD_FILE"
fi
echo

# ===== 5. Prompts ===========================================================
# Gemeinsamer Kontext-Header für alle Focus-Modi: beschreibt was das
# Programm tut, damit der Reviewer nicht aus dem Code selbst rückwärts
# rekonstruieren muss.

CONTEXT_HEADER='Du reviewst einen Rust AI-Agent-Orchestrator ("aistuff").
Der Code unten enthält die Architektur + Kern-Implementierung.

Kurz was das Programm TUT (aus AGENT.md):
- Ein Binary, kein Docker, kein Node. Module führen Aufgaben aus, nutzen
  LLMs als Werkzeug (nicht als Boss). Tasks sind JSON-Dateien auf der
  Platte in erstellt/ → gestartet/ → erledigt/ (File-basierte State
  Machine, Crash-Recovery by design).
- Pro Modul: eigenes LLM + eigene Tool-Permissions + eigener Scheduler.
- Tool-Call: LLM produziert OpenAI-kompatibles function_call, Cycle
  validiert gegen Whitelist + Permission, führt aus, pipet Ergebnis
  zurück ins LLM.
- Watchdog prüft Heartbeats pro Modul-Scheduler, markiert Dead als busy.
- Guardrail: Audit-Log + Valid-Rate-Checks + Alert-Threshold mit Cooldown.
- Embedded HTML/CSS/JS Frontend (keine SPA), NDJSON-Streaming.
- Config ist ein JSON-File mit 3 rotierenden Backups.
- Token-Tracker rechnet LLM-Kosten mit, es gibt eine Daily-USD-Cap.

Deine Aufgabe: NICHT Code-Style, NICHT Formatierung, NICHT Naming,
NICHT fehlende Tests. Dein Job ist: **ist das Programm als Ganzes
*geil*?** Ist die Logik kohärent? Sind versteckte Flaws drin? Würde
das in echt funktionieren, oder sieht es nur auf dem Papier gut aus?

Sei adversarial. Nicht loben, nicht Füllwerk. Finde was wirklich dran ist.
'

GENERAL_PROMPT="$CONTEXT_HEADER"'
Evaluier das Programm auf diesen Achsen, in dieser Prioritäts-Reihenfolge:

1. **Ist das Kern-Konzept tragfähig?** Macht die Architektur Sinn?
   Hält sie bei 10x Komplexität, oder sind versteckte Brittleness-Punkte
   drin die ein Rewrite erzwingen werden?

2. **Versteckte Logik-Flaws** (wichtigster Punkt):
   - Race-Conditions zwischen Watchdog, Scheduler, Orchestrator
   - Fallbacks die stumm fehlschlagen (primary down → backup down → ?)
   - State-Transitions die Tasks verlieren (Crash zwischen tx commits?)
   - Tool-Permission-Checks die umgehbar sind (Config-Korruption?
     JSON-Key-Order-Tricks? Path-Traversal?)
   - Budget-Caps die nichts cappen
   - Concurrent writes auf gemeinsame State-Files

3. **Fehlende Invarianten** — was SOLLTE immer gelten aber wird nicht erzwungen?
   (Jede Mutation → Audit-Log? Jeder Cron-Fire → Dedup? Jeder LLM-Call → Budget-Cap?)

4. **UX-auf-Logik-Ebene**:
   - Kann der User sich in einen Zustand klicken aus dem er nicht
     per UI wieder rauskommt?
   - Wenn ein LLM-Backend 24h down ist, was ist der Recovery-Pfad?
   - Gibt es destruktive Aktionen ohne Bestätigung?

5. **Architektur-Smells**:
   - Over-Abstraction (Abstraktionen mit einer konkreten Impl)
   - Under-Abstraction (gleiche Logik 5x copy-pasted)
   - Coupling das invertiert sein sollte (high-level → low-level Details)

**Output-Format (Markdown):**

## Verdict
Ein Absatz: ist das Programm geil oder nicht? Was ist der dominante Eindruck?

## Top Stärken (2-3 Bullets, je 1 Satz)

## Kritische Concerns (max 5, nach Impact sortiert)
Pro Concern:
- **Titel**
- Severity: High | Medium | Low
- Wo: file:line oder Komponenten-Name
- Warum: 1-2 Sätze mit konkretem Failure-Mode
- Fix-Richtung: 1 Satz

## Fehlende Invarianten (max 3)
Dinge die immer gelten sollten aber nicht erzwungen werden.

## Dümmste Design-Entscheidung
Eine Sache. 2 Sätze. Direkt.

## Klügste Design-Entscheidung
Eine Sache. 2 Sätze. Credit where due.

Max 1500 Wörter. Kein Preamble, kein "tolles Projekt"-Lob.
---
CODE:
'

CONCURRENCY_PROMPT="$CONTEXT_HEADER"'
Fokus NUR auf Concurrency, Races, Async-Korrektheit. Ignoriere alles andere.

Konkret zu prüfen:
- Watchdog (120s Timeout) vs. Tool-Execution (kann länger dauern) —
  entsteht ein Zombie-Scheduler wenn Tool > 120s läuft?
- cron_state.json: wer hält es, wie wird atomic geschrieben, was
  passiert bei gleichzeitigem Write aus zwei Tasks?
- Pipeline erstellt/ → gestartet/ → erledigt/ — Atomarität der Moves?
  Was wenn rename() crasht mitten drin? Was wenn eine zweite Instanz
  dieselbe Datei pickt (claim_atomic)?
- RwLock<AgentConfig>: await-Points unter read() / write() gehalten?
  Starvation möglich wenn Web-Thread Write wartet?
- tokio::spawn Panics — propagieren sie, oder verschluckt sie die
  Runtime? Wer restartet den Scheduler?
- Heartbeat-Map: wird wie synchronisiert, hat stale-read-Problem?
- Shared HashMap caches ohne tenant-prefix (gibts sowas?)

**Output:** `## Races` / `## Deadlock-Risiken` / `## Zombie-States` /
`## Summary`. Jeder Finding: file:line, exakt welche Timeline zum
Bug führt, Fix-Ansatz. Max 1200 Wörter.
---
CODE:
'

RESILIENCE_PROMPT="$CONTEXT_HEADER"'
Fokus NUR auf Failure-Modes: was passiert wenn die Welt bricht?

Szenarien adversarial durchspielen:
- OpenAI-API down für 6h: bleibt Agent arbeitsfähig (Fallback-Backend)?
  Was wenn auch Backup down ist — gibt es eine dritte Stufe oder
  hängt alles?
- Disk fast voll (99%): kann Pipeline noch State-Transitions machen,
  oder fällt rename() mit ENOSPC und Task ist für immer im limbo?
- config.json korrupt (User hat manuell editiert, falsches JSON):
  lädt main.rs eine Default-Config, oder crasht der Start?
  Was mit den 3 Backups — werden sie bei Parse-Error automatisch
  durchprobiert?
- Python-Subprocess (loader.rs PyProcessPool) stirbt während Tool-Call:
  wird der Pool restartet? Was sieht der nächste Caller?
- OS-Kill während commit-Phase von pipeline.rs: ist der Move atomar
  oder bleibt Task in Zwischenzustand (2 Kopien, oder 0 Kopien)?
- NTP-Drift um 1 Stunde: crashen die Cron-Dedup-Checks oder feuern
  sie plötzlich 24 Tasks doppelt?
- LLM antwortet mit garbage-JSON für einen function_call: parse_openai_
  tool_call() crashed oder silent-drops?

**Output:** `## Hart-Failure-Modes` / `## Silent-Failure-Modes` /
`## Recovery-Gaps` / `## Summary`. Pro Szenario: was passiert, wo
im Code, wie fix. Max 1200 Wörter.
---
CODE:
'

SECURITY_PROMPT="$CONTEXT_HEADER"'
Fokus NUR auf Sicherheit im Programm-Logik-Sinn (nicht Style, nicht XSS
in Templates — das ist ein separater Run).

Angriffs-Szenarien:
- Tool-Permission-Bypass: Kann ein LLM (oder User via Chat-API) ein
  Tool aufrufen das für sein Modul NICHT whitelisted ist?
  tools_for_module() / exec_tool_unified() — wo wird gegen was gecheckt?
- Path-Traversal: files-Modul hat `home/<modul_id>/` als Sandbox.
  Kann ein "../../etc/passwd" rausbrechen? safe_relative_path() — wie
  tight ist es? Was mit Symlinks, Windows-style paths, NUL-bytes,
  Unicode-Tricks (RTL override, fullwidth chars)?
- Python-Module Injection: loader.rs spawnt Python mit was als Input?
  shell=True? Argument-Quoting? Kann ein Tool-Parameter Code-Execution
  triggern?
- Chat-API ohne Auth? Wer darf /api/chat/*? Kann ein externer
  Netzteilnehmer (falls web.rs auf 0.0.0.0) Module fernsteuern?
- Config-API: kann ein nicht-privilegierter Request die Config
  überschreiben? Wird das config-Secret (API-Keys) in irgendeiner
  Response geleaked?
- Rate-Limit: security.rs hat Token-Bucket — ist die Bucket-Key
  korrekt (IP? User? Modul?)? Kann man sie umgehen via X-Forwarded-For
  wenn hinter Proxy?
- Audit-Log: ist es append-only, oder kann eine spätere Mutation
  frühere Zeilen überschreiben?
- Secret-Leakage: stehen API-Keys in Logs, in /api/config-Responses,
  in Frontend-HTML, in Error-Messages?

**Output:** `## Critical` / `## High` / `## Medium` / `## Summary`.
Pro Finding: file:line, Angriffs-Pfad konkret (welches Request was
triggert), Fix. Max 1200 Wörter.
---
CODE:
'

UX_PROMPT="$CONTEXT_HEADER"'
Fokus auf **Programm-Logik-UX**, nicht Farben/Schriftgrößen. Also:
kann der Nutzer sinnvoll mit dem System arbeiten? Wo würde er hängenbleiben?

Szenarien durchgehen:
- Ein Modul hat Config-Fehler: zeigt die UI einen klaren Fehler,
  oder nur ein "nichts passiert"? Kann der User sich selbst helfen?
- Eine Aufgabe ist in gestartet/ steckengeblieben weil Scheduler-Crash:
  sieht der User das? Kann er manuell re-queuen? Oder ist die einzige
  Recovery "rm agent-data/gestartet/xxx.json auf der CLI"?
- User-A stellt Wizard-Config um während Agent gerade Task Y läuft:
  was passiert? Kontra-Intuitive Effekte?
- Destruktive Aktionen (Modul löschen, LLM-Backend entfernen, Config-
  Restore): gibt es Confirmation-Prompts? Backups?
- Chat-UI: was sieht der User wenn LLM hängt, wenn Tool hängt, wenn
  NDJSON-Stream abbricht? Kann er abbrechen, oder muss er den Tab
  schließen?
- Neu-Onboarding: wie lange vom frischen Start bis erstem laufenden
  Modul? Ist die Wizard-Flow-Logik konsistent?
- Error-Messages: sind sie actionable ("XYZ failed because ABC, fix:
  do Z") oder kryptisch ("error 42")?
- Default-Werte: sind sie sinnvoll für einen Neu-Nutzer (z.B.
  Daily-Cap $5, nicht $0 oder $1000)?

**Output:** `## Dead-Ends` (User hängt fest) / `## Silent-Failures`
(User merkt nicht dass was kaputt ist) / `## Missing-Affordances`
(sollte existieren, existiert nicht) / `## Summary`.
Max 1200 Wörter.
---
CODE:
'

CONSISTENCY_PROMPT="$CONTEXT_HEADER"'
Fokus auf Invarianten und Zustands-Konsistenz. Was soll immer gelten?
Was wird wirklich erzwungen?

Zu prüfen:
- Jede Aufgabe ist in GENAU EINEM der drei Ordner (erstellt|gestartet|
  erledigt). Wird das erzwungen, oder gibt es Pfade wo sie in zwei
  Ordnern gleichzeitig existieren kann?
- JEDE Mutation → Log-Event? Oder nur manche?
- Cron dedup: feuert JEDER Cron-Schedule max 1x pro Slot, oder kann
  eine unglücklich getimte Restart-Sequence doppelt feuern?
- Token-Tracker: wird JEDER LLM-Call getrackt, oder hat irgendein
  Pfad (z.B. Streaming-Abort) einen Leak wo Cost unterschlagen wird?
- Permission-Whitelist: stimmen die beiden Checks (tools_as_openai_
  json vs. exec_tool_unified) miteinander überein, oder gibt es Drift?
- LLM-Backend-Liste: wenn User backend X löscht aber ein Modul noch
  darauf verweist — was passiert beim nächsten Run? Graceful
  "Fehler-Toast" oder Panic?
- Daily-Cap: Reset bei UTC-Mitternacht — was wenn Agent über
  Mitternacht einen laufenden Task hat, wird der gezählt in Tag A
  oder Tag B oder beiden?
- Config-Backup-Rotation: immer genau N Backups, oder kann es
  unter Edge-Cases ∞ viele oder 0 werden?
- Types-Invarianten: enum-Felder die immer bestimmte Kombinationen
  sein sollten (z.B. LogTyp::Error braucht Modul-ID) — type-enforced
  oder nur by-convention?

**Output:** `## Gebrochene Invarianten` / `## Implicite Invarianten
(die man sehen will)` / `## Drift zwischen parallelen Checks` /
`## Summary`. Pro Finding: file:line, konkreter Zustand der sie bricht,
Fix. Max 1200 Wörter.
---
CODE:
'

# Prompt nach Focus auswählen
case "$FOCUS" in
    general)      PROMPT="$GENERAL_PROMPT" ;;
    concurrency)  PROMPT="$CONCURRENCY_PROMPT" ;;
    resilience)   PROMPT="$RESILIENCE_PROMPT" ;;
    security)     PROMPT="$SECURITY_PROMPT" ;;
    ux)           PROMPT="$UX_PROMPT" ;;
    consistency)  PROMPT="$CONSISTENCY_PROMPT" ;;
esac

# Prompt + Payload in gemeinsame Datei schreiben, damit jq sie via
# --rawfile lesen kann (ARG_MAX-Limit ~128KB, Payload kann größer sein).
FULL_PROMPT_FILE="$OUT_DIR/_prompt.txt"
{ echo -n "$PROMPT"; cat "$PAYLOAD_FILE"; } > "$FULL_PROMPT_FILE"

# ===== 6. Dispatch in parallel ==============================================

send_one () {
    local model="$1"
    local slug
    slug="$(echo "$model" | tr '/.:' '___')"
    local out="$OUT_DIR/${slug}.md"
    local err="$OUT_DIR/${slug}.err"
    local body_file="$OUT_DIR/${slug}.req.json"

    # max_tokens groß genug für Reasoning-Modelle die intern thinken
    # + noch echten Output produzieren sollen.
    # --rawfile statt --arg damit der ARG_MAX nicht limitiert.
    jq -n \
        --arg model  "$model" \
        --rawfile prompt "$FULL_PROMPT_FILE" \
        '{
            model: $model,
            messages: [{role: "user", content: $prompt}],
            temperature: 0.2,
            max_tokens: 12000,
            stream: false
        }' > "$body_file"

    local t0 http_code t1
    t0=$(date +%s)
    http_code=$(curl -sS -o "$out.raw" -w "%{http_code}" \
        -X POST https://openrouter.ai/api/v1/chat/completions \
        -H "Authorization: Bearer $OPENROUTER_API_KEY" \
        -H "Content-Type: application/json" \
        -H "HTTP-Referer: https://github.com/local/aistuff" \
        -H "X-Title: Konklave aistuff review" \
        --data-binary "@$body_file" 2>"$err") || true
    t1=$(date +%s)
    rm -f "$body_file"

    {
        echo "<!-- konklave | model: $model | focus: $FOCUS | mode: $MODE | http: $http_code | dur: $((t1-t0))s -->"
        echo
        if [ "$http_code" = "200" ]; then
            jq -r '.choices[0].message.content // "(empty choices)"' < "$out.raw" 2>/dev/null \
                || cat "$out.raw"
        else
            echo "## ERROR ($http_code)"
            echo
            echo '```'
            head -c 3000 "$out.raw"
            echo
            echo '```'
        fi
    } > "$out"
    rm -f "$out.raw"
    printf "  %-40s  http=%s  %ss\n" "$model" "$http_code" "$((t1-t0))"
}

echo "Dispatching ${#SELECTED[@]} Reviewer parallel:"
for m in "${SELECTED[@]}"; do
    send_one "$m" &
done
wait
echo

# ===== 7. Manifest ==========================================================

{
    echo "# Konklave Review — $TS"
    echo
    echo "- **Focus:**  $FOCUS"
    echo "- **Mode:**   $MODE${DIFF_RANGE:+ ($DIFF_RANGE)}"
    echo "- **Preset:** $PRESET"
    echo "- **Payload:** $PAYLOAD_BYTES bytes, $PAYLOAD_LINES lines"
    echo
    echo "## Jurors"
    echo
    for m in "${SELECTED[@]}"; do
        echo "- \`$m\` — $(price_for "$m")"
    done
    echo
    echo "## Reviews"
    echo
    for f in "$OUT_DIR"/*.md; do
        [ "$(basename "$f")" = "_manifest.md" ] && continue
        echo "- [\`$(basename "$f")\`]($(basename "$f"))"
    done
} > "$OUT_DIR/_manifest.md"

# Zwischenprodukte aufräumen (Payload + Full-Prompt, ~600KB pro Run)
rm -f "$PAYLOAD_FILE" "$FULL_PROMPT_FILE"

echo "✓ fertig — Reviews in $OUT_DIR"
echo "  manifest: $OUT_DIR/_manifest.md"
