#!/usr/bin/env bash
# Konklave — multi-LLM program-logic review for the agent codebase.
#
# Dispatches the source tree (or a diff) in parallel to several top-tier
# LLMs via OpenRouter and collects adversarial PROGRAM-LOGIC reviews from
# each. Core question: "is the program actually good as a whole?"
#
# The review explicitly focuses on:
#   - Is the architecture coherent? Does the concept survive in practice?
#   - Hidden logic flaws: races, silently-failing fallbacks, state
#     transitions that lose data, invariants that are not enforced.
#   - What is the dumbest and the smartest thing in the design?
#
# Anthropic models are deliberately excluded: the human orchestrator
# (you, the one reading the reports) already uses Claude; a Claude peer
# would add no perspective diversity.
#
# Model selection: the script pulls /v1/models on every run, validates
# the preset list against it, and warns if a model is no longer available.
# That way the list can't silently go stale. Use `--models` to override.
#
# Usage:
#   ./tools/konklave.sh                          # default: core layer, general, flagship preset
#   ./tools/konklave.sh --full                   # include web.rs + wizard.rs
#   ./tools/konklave.sh --focus concurrency      # focused review
#   ./tools/konklave.sh --preset cheap           # cheaper mix of models
#   ./tools/konklave.sh --models "openai/gpt-5.4,x-ai/grok-4"
#   ./tools/konklave.sh --diff HEAD~5..HEAD      # diff-style review
#   ./tools/konklave.sh --list-models            # list currently-chosen jurors, exit
#
# Presets (at most ONE model per provider → real lab diversity):
#   flagship  (default)  ~$1-2 per review, best available models
#   balanced             ~$0.30 per review, solid middle-tier mix
#   cheap                ~$0.05 per review, aggressively cheap but reasoning-capable
#
# Focus modes:
#   general       (default) broad adversarial program-logic review
#   concurrency   races, deadlocks, watchdog/scheduler timing, async bugs
#   resilience    what breaks under LLM down, disk full, config corrupt, crash recovery
#   security      tool sandbox, permission bypass, input validation, injection
#   ux            user flow: can they get stuck? recovery? defaults?
#   consistency   invariants, state-machine correctness, cross-file integrity
#
# Env: OPENROUTER_API_KEY (from ~/.konklave/env or ~/.bwdms-review/env)
# Output: reviews/<timestamp>_<focus>/<slug>.md + _manifest.md

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
    echo "ERROR: OPENROUTER_API_KEY not set." >&2
    echo "  → create ~/.konklave/env with: OPENROUTER_API_KEY=sk-or-..." >&2
    exit 2
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

command -v jq   >/dev/null || { echo "ERROR: jq not installed"   >&2; exit 2; }
command -v curl >/dev/null || { echo "ERROR: curl not installed" >&2; exit 2; }

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
        -h|--help)     sed -n '1,46p' "$0"; exit 0 ;;
        *)             echo "ERROR: unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$PRESET" in
    flagship|balanced|cheap) ;;
    *) echo "ERROR: --preset must be flagship|balanced|cheap" >&2; exit 2 ;;
esac
case "$FOCUS" in
    general|concurrency|resilience|security|ux|consistency) ;;
    *) echo "ERROR: --focus invalid (general|concurrency|resilience|security|ux|consistency)" >&2; exit 2 ;;
esac

# ===== 2. Model preset definitions ==========================================
# Each line is a slot preference PER PROVIDER. The script picks the best
# currently-available model per slot from /v1/models (priority: first regex
# group wins per provider prefix). This keeps the list robust as providers
# bump versions (gpt-5.4 → gpt-5.5 etc).
#
# Diversity rule: MAX 1 model per lab. The slots below are chosen so each
# slot covers a different lab.
#
# Priority regex per slot: if multiple models match, the scoring function
# below sorts — "qwen3-max-thinking" beats "qwen3-max" (explicit reasoning
# suffix), then alpha DESC (higher version wins: gpt-5.4 > gpt-5.1).

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

# ===== 3. OpenRouter: live model fetch + filter =============================

MODELS_JSON="$(curl -sS https://openrouter.ai/api/v1/models)" || {
    echo "ERROR: /v1/models fetch failed" >&2
    exit 3
}

# Base filter: context >= 100k, not anthropic/, not *:free (rate-limited),
# has pricing, no pseudo-models (pricing == -1), output price <= MAX_OUT_PRICE
# of the chosen preset (prevents flagship picking $180/M output variants like
# gpt-5.4-pro).
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

# Per slot: (a) match slot regex, (b) filter out restriction variants
# (-mini, -flash, -distill, -vl, date-stamps etc. — those are smaller/
# specialised, not the flagship), (c) reward capability suffixes
# (-thinking, -reasoning, -speciale, -codex, -pro, -max, -plus, -ultra),
# (d) alphabetically highest ID wins last.
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
            if (.id | test("-(max|pro|codex)-(thinking|reasoning)")) then 6
            elif (.id | test("-(thinking|reasoning)-(max|pro|codex)")) then 6
            elif (.id | test("-speciale")) then 5
            elif (.id | test("-(thinking|reasoning)")) then 4
            elif (.id | test("-(codex|max|pro|plus|ultra)")) then 3
            elif ((.id | split("/")[1] | test("-")) | not) then 2
            else 1
            end,
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
            echo "  ⚠ no model available for slot »$slot«, skipping" >&2
            continue
        fi
        SELECTED+=("$pick")
    done
fi

if [ ${#SELECTED[@]} -eq 0 ]; then
    echo "ERROR: no models selected. --list-models to debug." >&2
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

# ===== 4. Source assembly ===================================================

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="reviews/${TS}_${FOCUS}"
mkdir -p "$OUT_DIR"
PAYLOAD_FILE="$OUT_DIR/_payload.txt"

CORE_FILES=(
    AGENT.md
    src/main.rs
    src/cycle.rs
    src/pipeline.rs
    src/store.rs
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
)

append_file () {
    local f="$1"
    if [ ! -f "$f" ]; then
        echo "  ⚠ missing: $f" >&2
        return
    fi
    {
        echo
        echo "================================================================"
        echo "FILE: $f   ($(wc -l <"$f") lines)"
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
        if [ -z "$DIFF_RANGE" ]; then echo "ERROR: --diff needs range" >&2; exit 2; fi
        echo "================================================================" >> "$PAYLOAD_FILE"
        echo "GIT DIFF: $DIFF_RANGE"                                              >> "$PAYLOAD_FILE"
        echo "================================================================" >> "$PAYLOAD_FILE"
        git diff "$DIFF_RANGE" --no-color -- src/ AGENT.md                       >> "$PAYLOAD_FILE" || true
        ;;
esac

PAYLOAD_BYTES=$(wc -c < "$PAYLOAD_FILE")
PAYLOAD_LINES=$(wc -l < "$PAYLOAD_FILE")
echo "→ payload: $PAYLOAD_BYTES bytes, $PAYLOAD_LINES lines"

# Hard cap (all reviewers have ≥ 128k ctx; 400k chars ≈ 100k tokens fits safely)
MAX_BYTES=400000
if [ "$PAYLOAD_BYTES" -gt "$MAX_BYTES" ]; then
    echo "  ⚠ >$((MAX_BYTES/1000))kB, truncating to $((MAX_BYTES/1000))kB" >&2
    head -c "$MAX_BYTES" "$PAYLOAD_FILE" > "$PAYLOAD_FILE.tmp"
    echo -e "\n\n[… TRUNCATED at ${MAX_BYTES} bytes …]" >> "$PAYLOAD_FILE.tmp"
    mv "$PAYLOAD_FILE.tmp" "$PAYLOAD_FILE"
fi
echo

# ===== 5. Prompts ===========================================================
# Shared context header for every focus mode: describes what the program
# does so the reviewer doesn't have to reverse-engineer it.

CONTEXT_HEADER='You are reviewing a Rust AI agent orchestrator ("agent-platform").
The code below contains the architecture + core implementation.

Brief summary of what the program DOES (from AGENT.md):
- A single binary. Modules run tasks, use LLMs as a tool (not as boss).
  Tasks live in a SQLite state machine (status: erstellt → gestartet →
  success/failed/cancelled) with atomic claim via BEGIN IMMEDIATE.
- One scheduler per module, each with its own LLM + tool permissions + home dir.
- Tool call: LLM produces OpenAI-compatible function_call, cycle validates
  against whitelist + permission, executes, pipes result back to the LLM.
- Watchdog checks per-scheduler heartbeats, aborts hung tasks, requeues with
  retry-count.
- Guardrail: audit-log + valid-rate checks + alert threshold with cooldown.
- Embedded HTML/CSS/JS frontend (no SPA), NDJSON streaming.
- Config is a JSON file with 3 rotating backups.
- Idempotency table deduplicates side-effect tool calls (exactly-once).
- Anthropic prompt caching active; per-model budget reservation.

Your task: NOT code style, NOT formatting, NOT naming, NOT missing tests.
Your job is: **is the program as a whole actually good?** Is the logic
coherent? Are there hidden flaws? Would this hold up in real use, or does
it only look good on paper?

Be adversarial. No flattery, no filler. Find what is really there.
'

GENERAL_PROMPT="$CONTEXT_HEADER"'
Evaluate the program on these axes, in this priority order:

1. **Is the core concept sound?** Does the architecture make sense?
   Does it hold at 10x complexity, or are there hidden brittleness
   points that will force a rewrite?

2. **Hidden logic flaws** (most important):
   - Race conditions between watchdog, scheduler, orchestrator
   - Fallbacks that fail silently (primary down → backup down → ?)
   - State transitions that lose tasks (crash between tx commits?)
   - Tool permission checks that can be bypassed (config corruption?
     JSON-key-order tricks? Path traversal?)
   - Budget caps that do not actually cap
   - Concurrent writes on shared state files

3. **Missing invariants** — what SHOULD always hold but is not enforced?
   (Every mutation → audit log? Every cron fire → dedup? Every LLM call → budget cap?)

4. **UX at the logic level**:
   - Can the user click into a state they cannot recover from via UI?
   - If an LLM backend is down for 24h, what is the recovery path?
   - Are there destructive actions without confirmation?

5. **Architectural smells**:
   - Over-abstraction (abstractions with one concrete impl)
   - Under-abstraction (same logic copy-pasted in 5 files)
   - Coupling that should be inverted (high-level depending on low-level internals)

**Output format (Markdown):**

## Verdict
One paragraph: is the program good? What is the dominant impression?

## Top strengths (2-3 bullets, 1 sentence each)

## Critical concerns (max 5, sorted by impact)
Per concern:
- **Title**
- Severity: High | Medium | Low
- Where: file:line or component name
- Why: 1-2 sentences with a concrete failure mode
- Fix direction: 1 sentence

## Missing invariants (max 3)
Things that should always hold but are not enforced.

## Dumbest design decision
One thing. 2 sentences. Blunt.

## Smartest design decision
One thing. 2 sentences. Credit where due.

Max 1500 words. No preamble, no "great project" praise.
---
CODE:
'

CONCURRENCY_PROMPT="$CONTEXT_HEADER"'
Focus ONLY on concurrency, races, async correctness. Ignore everything else.

Concrete things to check:
- Watchdog (120s timeout) vs tool execution (can take longer) —
  does a zombie scheduler emerge if a tool runs > 120s?
- cron_state: who holds it, how is it written atomically, what happens
  on concurrent write from two tasks?
- Pipeline `tasks.status` transitions (erstellt → gestartet →
  success/failed/cancelled) — atomicity of the claim `UPDATE` under
  `BEGIN IMMEDIATE`? If two schedulers race on the same row, does
  exactly one win, or can both pick it up before commit?
- RwLock<AgentConfig>: any await points held under read() / write()?
  Possible starvation when the web thread waits for write?
- tokio::spawn panics — do they propagate, or swallowed by runtime?
  Who restarts the scheduler?
- Heartbeat map: how is it synchronised, any stale-read problem?
- Shared HashMap caches without a tenant prefix?

**Output:** `## Races` / `## Deadlock risks` / `## Zombie states` /
`## Summary`. Per finding: file:line, exact timeline that leads to the
bug, fix approach. Max 1200 words.
---
CODE:
'

RESILIENCE_PROMPT="$CONTEXT_HEADER"'
Focus ONLY on failure modes: what happens when the world breaks?

Play through adversarial scenarios:
- OpenAI API down for 6h: does the agent stay functional (fallback backend)?
  What if the backup is also down — is there a third stage, or does
  everything hang?
- Disk almost full (99%): can the pipeline still do state transitions,
  or does rename() fail with ENOSPC leaving a task in limbo forever?
- config.json corrupt (user edited manually, invalid JSON):
  does main.rs load a default config, or does startup crash?
  What about the 3 backups — are they auto-tried on parse error?
- Python subprocess (loader.rs PyProcessPool) dies mid tool call:
  is the pool restarted? What does the next caller see?
- OS kill during pipeline.rs commit phase: is the move atomic or
  does the task end up in an intermediate state (2 copies, or 0)?
- NTP drift by 1 hour: do cron dedup checks crash, or do they suddenly
  fire 24 tasks twice?
- LLM returns garbage JSON for a function_call: does parse_openai_
  tool_call() crash or silent-drop?

**Output:** `## Hard failure modes` / `## Silent failure modes` /
`## Recovery gaps` / `## Summary`. Per scenario: what happens, where
in code, how to fix. Max 1200 words.
---
CODE:
'

SECURITY_PROMPT="$CONTEXT_HEADER"'
Focus ONLY on security in a program-logic sense (not style, not XSS in
templates — that is a separate run).

Attack scenarios:
- Tool permission bypass: can an LLM (or a user via chat API) invoke a
  tool that is NOT whitelisted for its module?
  tools_for_module() / exec_tool_unified() — where is what checked?
- Path traversal: files module has `home/<modul_id>/` as a sandbox.
  Can "../../etc/passwd" escape? safe_relative_path() — how tight is it?
  What about symlinks, Windows-style paths, NUL bytes, Unicode tricks
  (RTL override, fullwidth chars)?
- Python module injection: loader.rs spawns Python with what as input?
  shell=True? Argument quoting? Can a tool parameter trigger code execution?
- Chat API without auth? Who may access /api/chat/*? Can an external
  network peer (if web.rs binds 0.0.0.0) remote-control modules?
- Config API: can an unprivileged request overwrite the config? Are
  secrets (API keys) leaked in any response?
- Rate limit: security.rs uses a token bucket — is the bucket key
  correct (IP? user? module?)? Can it be bypassed via X-Forwarded-For
  when behind a proxy?
- Audit log: is it append-only, or can a later mutation overwrite
  earlier lines?
- Secret leakage: are API keys present in logs, in /api/config
  responses, in frontend HTML, in error messages?

**Output:** `## Critical` / `## High` / `## Medium` / `## Summary`.
Per finding: file:line, concrete attack path (which request triggers
what), fix. Max 1200 words.
---
CODE:
'

UX_PROMPT="$CONTEXT_HEADER"'
Focus on **program-logic UX**, not colours/fonts. I.e. can the user
actually work with the system? Where would they get stuck?

Play through scenarios:
- A module has a config error: does the UI show a clear error, or just
  "nothing happens"? Can the user help themselves?
- A task is stuck in `status='gestartet'` because of a scheduler crash:
  can the user see it in the dashboard? Can they manually requeue, or
  is the only recovery a raw UPDATE against the SQLite tasks table?
- User A changes wizard config while agent is running task Y: what
  happens? Counter-intuitive effects?
- Destructive actions (delete module, remove LLM backend, config
  restore): are there confirmation prompts? Backups?
- Chat UI: what does the user see when an LLM hangs, when a tool
  hangs, when the NDJSON stream breaks? Can they cancel, or must
  they close the tab?
- Fresh onboarding: how long from fresh start to first running module?
  Is the wizard flow consistent?
- Error messages: are they actionable ("XYZ failed because ABC, fix:
  do Z") or cryptic ("error 42")?
- Default values: do they make sense for a new user (e.g. daily cap
  $5, not $0 or $1000)?

**Output:** `## Dead ends` (user stuck) / `## Silent failures`
(user does not notice something is broken) / `## Missing affordances`
(should exist, does not) / `## Summary`.
Max 1200 words.
---
CODE:
'

CONSISTENCY_PROMPT="$CONTEXT_HEADER"'
Focus on invariants and state consistency. What should always hold?
What is actually enforced?

To check:
- Every task has EXACTLY ONE row in the `tasks` table with a single
  `status` value ∈ {erstellt, gestartet, success, failed, cancelled}.
  Is this enforced by schema + atomic transitions, or are there
  code paths that can produce duplicate rows, ghost rows without
  a valid status, or rows in an impossible state combination?
- Every mutation → log event? Or only some?
- Cron dedup: does every cron slot fire at most once, or can an
  unlucky restart sequence fire twice?
- Token tracker: is every LLM call tracked, or does some path (e.g.
  streaming abort) leak cost without accounting?
- Permission whitelist: do the two checks (tools_as_openai_json vs
  exec_tool_unified) agree, or is there drift?
- LLM backend list: if a user deletes backend X but a module still
  references it — what happens on the next run? Graceful error toast
  or panic?
- Daily cap: reset at UTC midnight — what if the agent has a running
  task across midnight, is it counted on day A or day B or both?
- Config backup rotation: always exactly N backups, or can it drift
  to ∞ or 0 under edge cases?
- Types invariants: enum fields that should always be certain
  combinations (e.g. LogTyp::Error needs module ID) — type-enforced
  or only by convention?

**Output:** `## Broken invariants` / `## Implicit invariants (should
be explicit)` / `## Drift between parallel checks` / `## Summary`.
Per finding: file:line, concrete state that breaks it, fix. Max 1200 words.
---
CODE:
'

# Select the prompt based on focus
case "$FOCUS" in
    general)      PROMPT="$GENERAL_PROMPT" ;;
    concurrency)  PROMPT="$CONCURRENCY_PROMPT" ;;
    resilience)   PROMPT="$RESILIENCE_PROMPT" ;;
    security)     PROMPT="$SECURITY_PROMPT" ;;
    ux)           PROMPT="$UX_PROMPT" ;;
    consistency)  PROMPT="$CONSISTENCY_PROMPT" ;;
esac

# Write prompt + payload to one file so jq can read it via --rawfile (ARG_MAX
# limit ~128KB; payload can be larger than that).
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

    # max_tokens generous enough for reasoning-heavy models that internally
    # "think" and still produce real output.
    # --rawfile instead of --arg so ARG_MAX does not limit us.
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
        -H "HTTP-Referer: https://github.com/local/agent-platform" \
        -H "X-Title: Konklave agent-platform review" \
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

echo "Dispatching ${#SELECTED[@]} reviewers in parallel:"
for m in "${SELECTED[@]}"; do
    send_one "$m" &
done
wait
echo

# ===== 7. Manifest ==========================================================

{
    echo "# Konklave review — $TS"
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

# Clean up intermediate files (payload + full prompt, ~600kB per run)
rm -f "$PAYLOAD_FILE" "$FULL_PROMPT_FILE"

echo "✓ done — reviews in $OUT_DIR"
echo "  manifest: $OUT_DIR/_manifest.md"
