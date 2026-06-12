"""Fast fact-check module for video scripts.

The module is deliberately conservative: it does not try to become a full
research agent. It extracts hard claims from a script, runs quick web searches,
and blocks expensive downstream steps when claims look unsupported or too exact.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


MODULE = {
    "name": "factcheck",
    "description": "Schneller Faktencheck fuer Video-/Voice-Skripte vor TTS/Render: extrahiert harte Claims, sucht Web-Belege und blockt riskante Aussagen.",
    "version": "1.0",
    "settings": {
        "enabled": {"type": "bool", "label": "Aktiv", "default": True},
        "max_claims": {"type": "number", "label": "Max Claims", "default": 14},
        "max_results_per_claim": {"type": "number", "label": "Suchtreffer pro Claim", "default": 5},
        "search_timeout_s": {"type": "number", "label": "Suchtimeout Sekunden", "default": 10},
        "min_score": {"type": "number", "label": "Mindestscore", "default": 72},
        "fail_on_unsupported": {"type": "bool", "label": "Unsupported Claims blocken", "default": True},
        "fail_on_conflict": {"type": "bool", "label": "Konflikte blocken", "default": True},
        "fail_if_search_broken": {"type": "bool", "label": "Bei Suchausfall blocken", "default": True},
    },
    "tools": [
        {
            "name": "factcheck.video_assets",
            "description": "Prueft VIDEO_ASSETS_JSON/script.txt vor TTS. JSON {assets_path?,script_path?,title?,query?,source_notes?,max_claims?}.",
            "params": ["query_json"],
        },
        {
            "name": "factcheck.script",
            "description": "Prueft freien Skripttext. JSON {script,title?,query?,max_claims?}.",
            "params": ["query_json"],
        },
        {
            "name": "factcheck.help",
            "description": "Zeigt Beispiele.",
            "params": [],
        },
    ],
}


STOPWORDS = {
    "aber",
    "alle",
    "also",
    "auch",
    "auf",
    "aus",
    "bei",
    "bis",
    "das",
    "dass",
    "dem",
    "den",
    "der",
    "des",
    "die",
    "ein",
    "eine",
    "einem",
    "einen",
    "einer",
    "eines",
    "erst",
    "fuer",
    "für",
    "hat",
    "hier",
    "ist",
    "kein",
    "keine",
    "mehr",
    "mit",
    "nach",
    "nicht",
    "oder",
    "pro",
    "rund",
    "seit",
    "sich",
    "sind",
    "und",
    "von",
    "vor",
    "was",
    "wenn",
    "wie",
    "wird",
    "wir",
    "zu",
    "zum",
    "zur",
    "the",
    "and",
    "for",
    "from",
    "that",
    "this",
    "with",
    "into",
    "over",
    "under",
    "are",
    "was",
    "were",
}

ABSOLUTE_RE = re.compile(
    r"\b("
    r"einzig(?:e|er|es|en|em)?|allein(?:e|iger|iges|igen|igem)?|kein(?:e|er|es|en|em)?|"
    r"niemand|immer|nie|weltweit|größte|groesste|höchste|hoechste|niedrigste|"
    r"genau|exakt|ausschließlich|ausschliesslich|monopol|single point of failure|"
    r"only|never|always|largest|biggest|highest|exactly|exclusive"
    r")\b",
    re.I,
)

NUMBER_RE = re.compile(
    r"(?<![\w])(?:\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)(?:\s*(?:%|Prozent|percent|Mrd\.?|Milliarden|Billion|Millionen|Million|Dollar|Euro|nm|Nanometer))?",
    re.I,
)


def handle_tool(tool_name: str, params: Any, config: dict[str, Any]) -> dict[str, Any]:
    try:
        if not bool_param(config.get("enabled"), True):
            return fail("factcheck ist deaktiviert.")
        if tool_name == "factcheck.video_assets":
            return factcheck_video_assets(params, config)
        if tool_name == "factcheck.script":
            return factcheck_script(params, config)
        if tool_name == "factcheck.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"FACTCHECK_FAILED: {exc}")


def factcheck_video_assets(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    assets = {}
    assets_path = text_value(payload.get("assets_path") or payload.get("video_assets_path") or payload.get("assets"))
    if assets_path:
        path = Path(assets_path).expanduser()
        if not path.exists():
            return fail(f"assets_path nicht gefunden: {path}")
        assets = read_json(path)
        if not isinstance(assets, dict):
            return fail(f"assets_path ist kein JSON-Objekt: {path}")

    script = text_value(payload.get("script") or payload.get("voice_script"))
    script_path = text_value(payload.get("script_path") or payload.get("script_file"))
    if not script and script_path:
        path = Path(script_path).expanduser()
        if not path.exists():
            return fail(f"script_path nicht gefunden: {path}")
        script = path.read_text(encoding="utf-8", errors="replace")
    if not script and assets:
        script = text_value(assets.get("voice_script"))
    if not script:
        return fail("Kein Skript gefunden.")

    title = text_value(payload.get("title") or assets.get("title"))
    query = text_value(payload.get("query") or payload.get("topic") or title)
    source_notes = payload.get("source_notes")
    if source_notes is None and assets:
        source_notes = assets.get("source_notes")
    # Primaerquelle: der DeepDive-Report (gecrawlte Belege). Claims, die hier
    # gut abgedeckt sind, sind durch die eigene Recherche gestuetzt — die
    # Websuche ist nur SEKUNDAERE Bestaetigung. Ohne das wuerden Nischen-Themen
    # (z.B. ein einzelnes GitHub-Repo) blocken, weil DuckDuckGo sie nicht kennt.
    primary = ""
    for key in ("deepdive_report_path", "deepdive_context_path", "report_path"):
        path = text_value(payload.get(key))
        if path and Path(path).expanduser().exists():
            try:
                primary += Path(path).expanduser().read_text(encoding="utf-8", errors="replace") + "\n"
            except Exception:
                pass
    payload = dict(payload)
    payload["_primary_source"] = primary
    return run_factcheck(script, title, query, source_notes, payload, config)


def factcheck_script(params: Any, config: dict[str, Any]) -> dict[str, Any]:
    payload = parse_payload(params)
    script = text_value(payload.get("script") or payload.get("text") or payload.get("voice_script"))
    if not script:
        return fail("script fehlt.")
    title = text_value(payload.get("title"))
    query = text_value(payload.get("query") or payload.get("topic") or title)
    return run_factcheck(script, title, query, payload.get("source_notes"), payload, config)


def run_factcheck(
    script: str,
    title: str,
    query: str,
    source_notes: Any,
    payload: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    started = time.time()
    max_claims = int_param(payload.get("max_claims"), int_param(config.get("max_claims"), 14, 1, 50), 1, 60)
    max_results = int_param(payload.get("max_results_per_claim"), int_param(config.get("max_results_per_claim"), 5, 1, 10), 1, 10)
    timeout_s = int_param(payload.get("search_timeout_s"), int_param(config.get("search_timeout_s"), 10, 3, 30), 3, 60)
    min_score = int_param(payload.get("min_score"), int_param(config.get("min_score"), 72, 0, 100), 0, 100)
    fail_on_unsupported = bool_param(payload.get("fail_on_unsupported"), bool_param(config.get("fail_on_unsupported"), True))
    fail_on_conflict = bool_param(payload.get("fail_on_conflict"), bool_param(config.get("fail_on_conflict"), True))
    fail_if_search_broken = bool_param(payload.get("fail_if_search_broken"), bool_param(config.get("fail_if_search_broken"), True))

    primary_source = normalize_word(str(payload.get("_primary_source") or ""))
    claims = extract_claims(script, max_claims)
    checked = []
    search_errors = []
    for claim in claims:
        # Primaerquellen-Check ZUERST: ist der Claim durch den DeepDive-Report
        # gedeckt (hohe Term-Abdeckung + ggf. Zahlen drin), gilt er als
        # recherche-gestuetzt und wird nicht geblockt.
        prim = primary_coverage(claim, primary_source) if primary_source else (0.0, False)
        if prim[0] >= 0.6 and (not claim.get("numbers") or prim[1]):
            verdict = dict(claim)
            verdict.update({
                "search_query": "primary:deepdive_report",
                "decision": "verified",
                "severity": "low",
                "reason": "Durch den DeepDive-Report (Primaerquelle) gestuetzt.",
                "primary_coverage": round(prim[0], 3),
                "suggested_rewrite": "",
                "evidence": [],
            })
            checked.append(verdict)
            continue
        search_query = build_query(claim["claim"], title or query)
        try:
            evidence = duckduckgo_search(search_query, max_results, timeout_s)
            verdict = evaluate_claim(claim, search_query, evidence)
            # Teil-Deckung in der Primaerquelle mildert ein Web-Negativ ab:
            # was die eigene Recherche teilweise stuetzt, darf hoechstens warnen.
            if prim[0] >= 0.4 and verdict.get("decision") in {"unsupported", "unknown"}:
                verdict["decision"] = "needs_softening"
                verdict["severity"] = "low"
                verdict["reason"] = (verdict.get("reason", "") + " | Teilweise durch DeepDive-Report gestuetzt.").strip(" |")
            # Zweite Chance mit EN-Fallback-Query: deutsche Komposita finden
            # englische Quellen nicht — erst wenn BEIDE Suchen nichts stuetzen,
            # darf der Claim blocken (stabilere Verdicts ueber Runden).
            if verdict.get("decision") in {"unsupported", "unknown"}:
                fallback_query = build_fallback_query(claim["claim"], title or query)
                if fallback_query and fallback_query != search_query:
                    more = duckduckgo_search(fallback_query, max_results, timeout_s)
                    if more:
                        merged = evidence + [e for e in more if e not in evidence]
                        combined_label = f"{search_query} | fallback: {fallback_query}"
                        verdict = evaluate_claim(claim, combined_label, merged)
            checked.append(verdict)
        except Exception as exc:
            search_errors.append({"claim": claim["claim"], "query": search_query, "error": str(exc)})
            checked.append(
                {
                    **claim,
                    "search_query": search_query,
                    "decision": "unknown",
                    "severity": "high",
                    "reason": f"Suche fehlgeschlagen: {exc}",
                    "evidence": [],
                    "suggested_rewrite": soften_claim(claim["claim"]),
                }
            )

    blocking = []
    warnings = []
    score = 100
    for item in checked:
        decision = item["decision"]
        severity = item.get("severity") or "medium"
        issue = {
            "claim": item["claim"],
            "decision": decision,
            "severity": severity,
            "reason": item.get("reason") or "",
            "suggested_rewrite": item.get("suggested_rewrite") or "",
            "search_query": item.get("search_query") or "",
        }
        if decision == "verified":
            continue
        if decision == "conflict":
            score -= 28 if severity == "high" else 18
            if fail_on_conflict:
                blocking.append(issue)
            else:
                warnings.append(issue)
        elif decision == "unsupported":
            score -= 18 if severity == "high" else 10
            if fail_on_unsupported and severity in {"high", "medium"}:
                blocking.append(issue)
            else:
                warnings.append(issue)
        elif decision == "needs_softening":
            score -= 8 if severity == "high" else 5
            warnings.append(issue)
        else:
            score -= 7 if severity == "high" else 3
            warnings.append(issue)

    if search_errors and fail_if_search_broken and len(search_errors) >= max(2, len(claims) // 2):
        blocking.append(
            {
                "claim": "factcheck_search",
                "decision": "unknown",
                "severity": "high",
                "reason": "Zu viele Suchfehler im Faktencheck.",
                "suggested_rewrite": "Workflow spaeter erneut starten oder Suchmodul pruefen.",
            }
        )
        score -= 25

    score = max(0, min(100, score))
    verified = sum(1 for c in checked if c["decision"] == "verified")
    conflicts = sum(1 for c in checked if c.get("decision") == "conflict")
    if checked and verified == 0 and conflicts == 0 and fail_if_search_broken:
        blocking.append(
            {
                "claim": "factcheck_coverage",
                "decision": "unsupported",
                "severity": "high",
                "reason": "Kein einziger harter Claim wurde durch Web-Snippets gestuetzt.",
                "suggested_rewrite": "Quellenlage nachziehen oder Claims deutlich abschwaechen.",
            }
        )
        score = min(score, 45)

    passed = not blocking and score >= min_score
    decision = "pass" if passed and not warnings else ("pass_with_warnings" if passed else "block")
    source_quality = source_notes_quality(source_notes)
    report = {
        "type": "video_factcheck",
        "pass": passed,
        "score": score,
        "min_score": min_score,
        "decision": decision,
        "summary": summary_text(checked, blocking, warnings, source_quality),
        "claims_checked": len(checked),
        "verified_claims": verified,
        "blocking_issues": blocking,
        "warnings": warnings[:20],
        "claims": checked,
        "source_quality": source_quality,
        "search_errors": search_errors,
        "elapsed_s": round(time.time() - started, 2),
    }
    return ok(report)


def extract_claims(script: str, max_claims: int) -> list[dict[str, Any]]:
    sentences = split_sentences(script)
    scored = []
    seen = set()
    for idx, sentence in enumerate(sentences):
        clean = collapse_ws(sentence)
        if len(clean) < 45 or len(clean) > 420:
            continue
        nums = extract_numbers(clean)
        absolute = bool(ABSOLUTE_RE.search(clean))
        if not nums and not absolute:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        priority = len(nums) * 3 + (5 if any(is_year(n) for n in nums) else 0) + (4 if absolute else 0)
        if re.search(r"\b(?:milliarden|billion|million|dollar|euro|prozent|percent|nanometer|euv|duv|chips act|tsmc|asml|nvidia|intel|samsung|huawei|smic|china|taiwan)\b", clean, re.I):
            priority += 4
        scored.append(
            {
                "id": f"c{len(scored)+1}",
                "claim": clean,
                "sentence_index": idx,
                "numbers": nums,
                "absolute_language": absolute,
                "priority": priority,
            }
        )
    scored.sort(key=lambda c: c["priority"], reverse=True)
    return scored[:max_claims]


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9])", text)
    return [p.strip() for p in parts if p.strip()]


def build_query(claim: str, context: str) -> str:
    nums = extract_numbers(claim)
    # NUR Claim-Terms: der Video-Titel als "Kontext" hat jede Query verschmutzt
    # (z.B. 'ki-infrastruktur-rennen' in jeder Suche) und die Trefferquote ruiniert.
    terms = key_terms(claim, limit=16)
    selected = []
    for n in nums[:4]:
        if n not in selected:
            selected.append(n)
    for term in terms:
        if term not in selected:
            selected.append(term)
        if len(selected) >= 14:
            break
    return " ".join(selected)[:240] or claim[:200]


DE_EN_TERMS = {
    "exportkontrollen": "export controls",
    "verschaerften": "tightened",
    "verschaerft": "tightened",
    "rechenzentren": "data centers",
    "rechenzentrum": "data center",
    "halbleiter": "semiconductors",
    "stromnetze": "power grid",
    "subventionen": "subsidies",
    "zoelle": "tariffs",
    "lieferkette": "supply chain",
    "seltene erden": "rare earths",
    "regierung": "government",
    "milliarden": "billion",
    "investitionen": "investment",
    "kuenstliche intelligenz": "artificial intelligence",
    "wettlauf": "race",
}


def english_terms(claim: str, limit: int = 8) -> list[str]:
    lower = normalize_word(claim)
    out: list[str] = []
    for de, en in DE_EN_TERMS.items():
        if de in lower and en not in out:
            out.append(en)
        if len(out) >= limit:
            break
    # Eigennamen/Akronyme aus dem Claim mitnehmen (USA, TSMC, Nvidia, ...)
    for m in re.finditer(r"\b[A-Z][A-Za-z]{1,14}\b|\b[A-Z]{2,6}\b", claim):
        w = m.group(0)
        if w not in out and len(out) < limit + 4:
            out.append(w)
    return out


def build_fallback_query(claim: str, context: str) -> str:
    nums = extract_numbers(claim)
    years = [n for n in nums if is_year(n)]
    terms = key_terms(claim, limit=10)
    lower = normalize_word(claim + " " + context)
    extra = english_terms(claim)
    if "asml" in lower:
        extra.extend(["ASML", "annual report", "EUV systems", "shipments"])
    if "chips" in lower and "act" in lower:
        extra.extend(["CHIPS and Science Act", "official", "funding"])
    if "intel" in lower and ("milliarden" in lower or "billion" in lower or "chips" in lower):
        extra.extend(["Intel", "CHIPS award", "official"])
    if "tsmc" in lower:
        extra.extend(["TSMC", "N2", "official", "production"])
    if "nvidia" in lower:
        extra.extend(["Nvidia", "annual revenue", "run rate"])
    if "legacy" in lower or "28 nanometer" in lower or "chinesische" in lower:
        extra.extend(["China", "legacy chips", "capacity", "2025"])
    selected = []
    source_terms = (extra + years[:3]) if extra else (years[:3] + terms)
    for value in source_terms:
        value = collapse_ws(str(value))
        if value and value not in selected:
            selected.append(value)
    return " ".join(selected)[:240]


def duckduckgo_search(query: str, max_results: int, timeout_s: int) -> list[dict[str, str]]:
    encoded = urllib.parse.quote_plus(query)
    url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return parse_ddg_lite(raw, max_results)


def parse_ddg_lite(raw: str, max_results: int) -> list[dict[str, str]]:
    results = []
    snippets = [
        strip_html(m.group(1))
        for m in re.finditer(r"class=['\"]result-snippet['\"][^>]*>(.*?)</td>", raw, flags=re.I | re.S)
    ]
    link_re = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
    result_idx = 0
    for match in link_re.finditer(raw):
        if len(results) >= max_results:
            break
        attrs = match.group(1)
        if not re.search(r"class=['\"][^'\"]*\bresult-link\b", attrs, flags=re.I):
            continue
        href_m = re.search(r"href=['\"]([^'\"]+)['\"]", attrs, flags=re.I)
        if not href_m:
            continue
        href = html.unescape(href_m.group(1))
        title = strip_html(match.group(2))
        if not title:
            continue
        parsed = urllib.parse.urlparse(href)
        if "uddg" in urllib.parse.parse_qs(parsed.query):
            real_url = urllib.parse.parse_qs(parsed.query)["uddg"][0]
        elif href.startswith("http"):
            real_url = href
        else:
            continue
        if "duckduckgo.com" in real_url or "duck.co" in real_url:
            continue
        results.append(
            {
                "title": title[:220],
                "url": real_url,
                "snippet": (snippets[result_idx] if result_idx < len(snippets) else "")[:500],
            }
        )
        result_idx += 1
    return results


ATTRIBUTION_RE = re.compile(
    r"(zufolge|laut\s|berichtet|behauptet|unbest(ae|ä)tigt|sch(ae|ä)tzung|angeblich|"
    r"einem bericht|dem bericht|hei(ss|ß)t es|so der bericht|nach angaben|demnach)",
    re.I,
)


def evaluate_claim(claim: dict[str, Any], search_query: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
    text = " ".join((e.get("title", "") + " " + e.get("snippet", "") + " " + e.get("url", "")) for e in evidence)
    claim_text = claim["claim"]
    nums = claim.get("numbers") or []
    evidence_nums = extract_numbers(text)
    terms = key_terms(claim_text, limit=18)
    term_ratio = coverage(terms, text)
    num_hits = [n for n in nums if number_supported(n, evidence_nums, text)]
    strict = bool(re.search(r"\b(genau|exakt|exactly|produzierte|produced|erhaelt|erhält|receives|holds|haelt|hält)\b", claim_text, re.I))
    over_claim = bool(re.search(r"\b(ueber|über|more than|over|greater than)\b", claim_text, re.I))

    decision = "unknown"
    severity = "medium"
    reason = ""
    if not evidence:
        decision = "unknown"
        severity = "high" if nums or claim.get("absolute_language") else "medium"
        reason = "Keine Suchtreffer gefunden."
    elif nums:
        if len(num_hits) == len(nums) and term_ratio >= 0.18:
            decision = "verified"
            severity = "low"
            reason = "Zahlen/Jahreszahlen tauchen in den Suchtreffern mit ausreichendem Themenbezug auf."
        elif term_ratio >= 0.28 and conflicting_number_hint(claim_text, nums, evidence_nums, over_claim, strict):
            decision = "conflict"
            severity = "high"
            reason = "Suchtreffer enthalten thematisch passende, aber abweichende Zahlen. Claim muss vor TTS geprueft oder umformuliert werden."
        elif term_ratio >= 0.24:
            decision = "needs_softening"
            severity = "medium" if claim.get("absolute_language") or strict else "low"
            reason = "Thema wird gefunden, aber nicht alle harten Zahlen/absoluten Formulierungen werden im Snippet gestuetzt."
        else:
            decision = "unsupported"
            severity = "high"
            reason = "Harte Zahlen/Jahreszahlen werden in den Suchtreffern nicht ausreichend gestuetzt."
    elif claim.get("absolute_language"):
        if term_ratio >= 0.42:
            decision = "needs_softening"
            severity = "medium"
            reason = "Absolute Formulierung hat Themenbezug, sollte aber ohne Primaerquelle weicher formuliert werden."
        else:
            decision = "unsupported"
            severity = "medium"
            reason = "Absolute Formulierung wird von den Suchtreffern nicht ausreichend getragen."
    else:
        decision = "verified" if term_ratio >= 0.38 else "unknown"
        severity = "low" if decision == "verified" else "medium"
        reason = "Themenabgleich ohne harte Zahlen."

    # Bereits zugeschriebene/unbestaetigte Aussagen sind per Normalizer-Vertrag
    # KEINE harten Fakten — sie duerfen warnen, aber nie blocken.
    if decision in {"unsupported", "unknown"} and ATTRIBUTION_RE.search(claim_text):
        decision = "needs_softening"
        severity = "low"
        reason = (reason + " | Aussage ist bereits als zugeschriebene/unbestaetigte Quelle formuliert.").strip(" |")
    # Nur-Jahreszahlen sind keine 'harten Zahlen': Snippets lassen Jahre oft weg.
    elif decision == "unsupported" and nums and all(is_year(n) for n in nums) and term_ratio >= 0.12:
        decision = "needs_softening"
        severity = "medium"
        reason = (reason + " | Nur Jahresangaben betroffen, Thema wird gefunden.").strip(" |")
    return {
        **claim,
        "search_query": search_query,
        "decision": decision,
        "severity": severity,
        "reason": reason,
        "term_coverage": round(term_ratio, 3),
        "number_hits": num_hits,
        "evidence_numbers": evidence_nums[:20],
        "evidence": evidence,
        "suggested_rewrite": "" if decision == "verified" else soften_claim(claim_text),
    }


def conflicting_number_hint(claim: str, nums: list[str], evidence_nums: list[str], over_claim: bool, strict: bool) -> bool:
    claimed_values = [to_float(n) for n in nums if not is_year(n)]
    evidence_values = [to_float(n) for n in evidence_nums if not is_year(n)]
    claimed_values = [v for v in claimed_values if v is not None]
    evidence_values = [v for v in evidence_values if v is not None]
    if not claimed_values or not evidence_values:
        return False
    for claimed in claimed_values:
        nearby = [v for v in evidence_values if abs(v - claimed) <= max(2.0, abs(claimed) * 0.35) and abs(v - claimed) > 0.01]
        if not nearby:
            continue
        if strict:
            return True
        if over_claim and any(v < claimed for v in nearby):
            return True
    return False


def source_notes_quality(source_notes: Any) -> dict[str, Any]:
    if not isinstance(source_notes, list):
        return {"count": 0, "risk": "unknown", "warnings": ["Keine source_notes vorhanden."]}
    joined = "\n".join(str(x) for x in source_notes)
    risky = len(re.findall(r"unknown_check_needed|lead_check_needed|commentary_or_social_check_needed|ungeprueft|ungeprüft", joined, re.I))
    strong = len(re.findall(r"primary_or_official|official|etablierte|established_or_primary|annual report|bericht", joined, re.I))
    risk = "low" if strong >= risky and strong > 0 else ("medium" if strong or risky <= 2 else "high")
    warnings = []
    if risky:
        warnings.append(f"{risky} Quellenhinweise sind als ungeprueft/Lead markiert.")
    if not strong:
        warnings.append("Keine klaren Primaer-/starken Quellen in source_notes erkannt.")
    return {"count": len(source_notes), "strong_signals": strong, "risk_signals": risky, "risk": risk, "warnings": warnings}


def summary_text(checked: list[dict[str, Any]], blocking: list[dict[str, Any]], warnings: list[dict[str, Any]], source_quality: dict[str, Any]) -> str:
    counts = {}
    for item in checked:
        counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    bits = [f"{len(checked)} Claims geprueft"]
    if counts:
        bits.append(", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    bits.append(f"blocker={len(blocking)}")
    bits.append(f"warnings={len(warnings)}")
    bits.append(f"source_risk={source_quality.get('risk')}")
    return "; ".join(bits)


def soften_claim(claim: str) -> str:
    text = claim.strip()
    replacements = [
        (r"\bgenau\s+", ""),
        (r"\bexakt\s+", ""),
        (r"\bkein anderes Unternehmen der Welt\b", "nach aktueller Quellenlage kaum ein anderes Unternehmen"),
        (r"\bniemand\b", "unklar bleibt, wer"),
        (r"\bimmer\b", "haeufig"),
        (r"\bnie\b", "kaum"),
        (r"\bhält rund\b", "wird haeufig mit rund"),
        (r"\bhaelt rund\b", "wird haeufig mit rund"),
        (r"\berhält\b", "wurde mit bis zu"),
        (r"\berhaelt\b", "wurde mit bis zu"),
        (r"\bproduzierte\b", "meldete fuer diesen Zeitraum"),
    ]
    for pattern, repl in replacements:
        text = re.sub(pattern, repl, text, flags=re.I)
    if text == claim.strip():
        text = "Vorsichtiger formulieren und Quelle nennen: " + text
    return collapse_ws(text)


def key_terms(text: str, limit: int = 16) -> list[str]:
    words = re.findall(r"[A-Za-zÄÖÜäöüß][\wÄÖÜäöüß\-]{3,}", text or "")
    scored = []
    seen = set()
    for word in words:
        w = normalize_word(word)
        if not w or w in STOPWORDS or len(w) < 4 or w in seen:
            continue
        seen.add(w)
        score = 1
        if word[:1].isupper():
            score += 2
        if re.search(r"tsmc|asml|euv|duv|nvidia|intel|samsung|huawei|smic|china|taiwan|chips|nanometer|dollar|euro", w, re.I):
            score += 3
        scored.append((score, w))
    scored.sort(reverse=True)
    return [w for _, w in scored[:limit]]


def coverage(terms: list[str], text: str) -> float:
    if not terms:
        return 0.0
    hay = normalize_word(text)
    hits = sum(1 for term in terms if term in hay)
    return hits / max(1, len(terms))


def primary_coverage(claim: dict[str, Any], primary_norm: str) -> tuple[float, bool]:
    """Wie gut ist der Claim durch die Primaerquelle (DeepDive-Report) gedeckt?
    Liefert (term_ratio, alle_zahlen_belegt). primary_norm ist bereits
    normalize_word-vorverarbeitet."""
    if not primary_norm:
        return (0.0, False)
    terms = key_terms(claim["claim"], limit=18)
    if not terms:
        return (0.0, False)
    hits = sum(1 for t in terms if t in primary_norm)
    ratio = hits / len(terms)
    nums = claim.get("numbers") or []
    nums_ok = all(normalize_word(str(n)) in primary_norm for n in nums) if nums else True
    return (ratio, nums_ok)


def extract_numbers(text: str) -> list[str]:
    out = []
    seen = set()
    for match in NUMBER_RE.finditer(text or ""):
        raw = collapse_ws(match.group(0))
        norm = normalize_number(raw)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def number_supported(number: str, evidence_numbers: list[str], evidence_text: str) -> bool:
    if number in evidence_numbers:
        return True
    n = to_float(number)
    if n is None:
        return False
    for ev in evidence_numbers:
        v = to_float(ev)
        if v is not None and abs(v - n) <= max(0.01, abs(n) * 0.01):
            return True
    # Years sometimes appear without suffix in snippets after normalization.
    if is_year(number) and re.search(rf"\b{re.escape(str(int(n)))}\b", evidence_text or ""):
        return True
    return False


def normalize_number(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?", raw)
    if not m:
        return ""
    num = m.group(0)
    if "," in num and "." in num:
        num = num.replace(".", "").replace(",", ".")
    elif "," in num:
        num = num.replace(",", ".")
    suffix = ""
    if re.search(r"%|prozent|percent", raw, re.I):
        suffix = "%"
    elif re.search(r"mrd|milliarden|billion", raw, re.I):
        suffix = " billion"
    elif re.search(r"millionen|million", raw, re.I):
        suffix = " million"
    elif re.search(r"nanometer|\bnm\b", raw, re.I):
        suffix = " nm"
    return (num + suffix).strip()


def to_float(number: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", number or "")
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def is_year(number: str) -> bool:
    n = to_float(number)
    return n is not None and 1900 <= n <= 2100 and not any(s in number for s in ("%", "billion", "million", "nm"))


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return collapse_ws(text)


def normalize_word(text: str) -> str:
    return (
        (text or "")
        .lower()
        .replace("ä", "ae")
        .replace("ö", "oe")
        .replace("ü", "ue")
        .replace("ß", "ss")
    )


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def parse_payload(params: Any) -> dict[str, Any]:
    if isinstance(params, dict):
        return params
    if isinstance(params, list) and params:
        raw = params[0]
    else:
        raw = params
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {"script": text}
    except Exception:
        return {"script": text}


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def text_value(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def int_param(value: Any, default: int, min_value: int, max_value: int) -> int:
    try:
        v = int(float(str(value).strip()))
    except Exception:
        v = default
    return max(min_value, min(max_value, v))


def bool_param(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on", "y"}


def ok(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": True, "data": data}


def fail(data: Any) -> dict[str, Any]:
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, indent=2)
    return {"success": False, "data": data}


def help_text() -> str:
    return """Beispiele:
factcheck.video_assets({"assets_path":"agent-data/home/.../video_assets.json","script_path":"agent-data/home/.../script.txt","title":"Halbleiter","max_claims":14})
factcheck.script({"title":"ASML Briefing","script":"ASML produzierte 2024 genau 42 EUV-Systeme..."})

Entscheidung:
- verified: Snippets stuetzen Claim grob.
- needs_softening: Themenbezug da, harte Zahl/absolute Formulierung nicht sauber genug.
- unsupported/conflict: Pipeline sollte vor TTS blocken.
"""


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req.get("tool", ""), req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
