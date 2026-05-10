"""X Comments module: opinion/reply search via X API v2 recent search."""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from x_api_common import (  # noqa: E402
    build_search_query,
    cfg_bool,
    cost_line,
    fail,
    first_text,
    index_users,
    load_runtime_config,
    metrics_text,
    ok,
    parse_payload,
    rate_line,
    record_from_post,
    run_module,
    search_recent,
    usage_tweets,
)


DEFAULT_OPINION_QUERY = (
    "(better OR worse OR best OR worst OR prefer OR preferred OR sucks OR love OR hate "
    "OR good OR bad OR broken OR impressive OR overrated OR underrated OR besser OR schlechter "
    "OR gut OR schlecht OR liebe OR hasse OR nervt OR meh OR kacke OR geil)"
)

POSITIVE = {
    "love",
    "like",
    "good",
    "great",
    "best",
    "better",
    "impressive",
    "solid",
    "works",
    "besser",
    "gut",
    "geil",
    "stark",
    "ueberzeugt",
    "genial",
}
NEGATIVE = {
    "hate",
    "bad",
    "worse",
    "worst",
    "sucks",
    "broken",
    "garbage",
    "overrated",
    "meh",
    "schlecht",
    "schlechter",
    "hasse",
    "nervt",
    "kacke",
    "mies",
}
COMPARATIVE = {
    "better",
    "worse",
    "best",
    "worst",
    "prefer",
    "preferred",
    "vs",
    "over",
    "instead",
    "besser",
    "schlechter",
    "als",
    "statt",
}


MODULE = {
    "name": "x_comments",
    "description": "Sucht X Replies/Posts nach Meinungen, Vergleichen und Kommentaren zu einem Thema.",
    "version": "1.0",
    "settings": {
        "bearer_token": {"type": "password", "label": "X Bearer Token", "default": ""},
        "max_results": {"type": "number", "label": "Max Posts pro Suche", "default": 20},
        "max_pages": {"type": "number", "label": "Max Seiten pro Suche", "default": 1},
        "lang": {"type": "string", "label": "Sprache optional (z.B. en/de)", "default": ""},
        "include_retweets": {"type": "bool", "label": "Retweets einschliessen", "default": False},
        "include_replies": {"type": "bool", "label": "Replies einschliessen", "default": True},
        "include_author_details": {
            "type": "bool",
            "label": "Autoren expandieren (kostet User Reads)",
            "default": False,
        },
        "add_opinion_terms": {"type": "bool", "label": "Opinion-Woerter automatisch anfuegen", "default": True},
        "opinion_query": {"type": "text", "label": "Opinion Query", "default": DEFAULT_OPINION_QUERY},
        "sort_order": {
            "type": "select",
            "label": "Sortierung",
            "default": "relevancy",
            "options": ["relevancy", "recency"],
        },
        "api_base": {"type": "string", "label": "X API Base", "default": "https://api.x.com"},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 20},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 30},
    },
    "tools": [
        {
            "name": "x_comments.opinions",
            "description": (
                "Sucht oeffentliche X Posts/Replies nach Meinungen. Param: Suchtext oder JSON "
                "{\"query\":\"google gemini\",\"max_results\":20,\"lang\":\"en\","
                "\"only_replies\":false}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "x_comments.for_post",
            "description": (
                "Sucht Replies in einer Conversation oder direkt zu einem Post. Param JSON: "
                "{\"post_id\":\"123\",\"max_results\":20} oder {\"conversation_id\":\"123\"}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "x_comments.usage",
            "description": "Zeigt X API Post-Verbrauch fuer Budgetkontrolle.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name, params, config):
    if tool_name == "x_comments.opinions":
        return _opinions(params, config)
    if tool_name == "x_comments.for_post":
        return _for_post(params, config)
    if tool_name == "x_comments.usage":
        return usage_tweets(config, os.path.dirname(__file__))
    return fail(f"Unbekanntes Tool: {tool_name}")


def _opinions(params, config):
    payload = parse_payload(params)
    base_query = first_text(payload, "query", "topic", "q", "text")
    conversation_id = first_text(payload, "conversation_id", "conversation")
    post_id = first_text(payload, "post_id", "tweet_id", "id")

    if conversation_id or post_id:
        return _for_post([payload], config)
    if not base_query:
        return fail(
            "Kein Suchbegriff. Beispiel: google gemini oder "
            "{\"query\":\"google gemini\",\"max_results\":20,\"lang\":\"en\"}"
        )

    cfg = load_runtime_config(config, os.path.dirname(__file__))
    add_terms = cfg_bool(payload.get("add_opinion_terms", cfg.get("add_opinion_terms")), True)
    query = _group_query(base_query)
    if add_terms:
        opinion_query = first_text(payload, "opinion_query", "opinion_terms") or cfg.get("opinion_query") or DEFAULT_OPINION_QUERY
        query = f"{query} {opinion_query}".strip()

    query, err = build_search_query(query, payload, cfg, default_include_replies=True)
    if err:
        return fail(err)

    include_authors = cfg_bool(payload.get("include_author_details", cfg.get("include_author_details")), False)
    success, result, _headers, error = search_recent(
        query,
        payload,
        cfg,
        os.path.dirname(__file__),
        include_authors=include_authors,
    )
    if not success:
        return fail(error)
    return ok(_format_opinions(result, mode="topic"))


def _for_post(params, config):
    payload = parse_payload(params)
    cfg = load_runtime_config(config, os.path.dirname(__file__))

    conversation_id = first_text(payload, "conversation_id", "conversation")
    post_id = first_text(payload, "post_id", "tweet_id", "id")
    direct_only = cfg_bool(payload.get("direct_replies_only"), False)

    if direct_only:
        if not post_id:
            return fail("Fuer direct_replies_only wird post_id/tweet_id benoetigt.")
        query = f"in_reply_to_tweet_id:{post_id}"
    else:
        target = conversation_id or post_id
        if not target:
            return fail("post_id oder conversation_id fehlt.")
        query = f"conversation_id:{target} is:reply"

    extra = first_text(payload, "query", "contains")
    if extra:
        query = f"{query} {_group_query(extra)}"
    if cfg_bool(payload.get("add_opinion_terms", False), False):
        opinion_query = first_text(payload, "opinion_query", "opinion_terms") or cfg.get("opinion_query") or DEFAULT_OPINION_QUERY
        query = f"{query} {opinion_query}".strip()

    query, err = build_search_query(query, payload, cfg, default_include_replies=True)
    if err:
        return fail(err)

    include_authors = cfg_bool(payload.get("include_author_details", cfg.get("include_author_details")), False)
    success, result, _headers, error = search_recent(
        query,
        payload,
        cfg,
        os.path.dirname(__file__),
        include_authors=include_authors,
    )
    if not success:
        return fail(error)
    return ok(_format_opinions(result, mode="conversation"))


def _format_opinions(result, mode):
    users = index_users(result)
    records = [record_from_post(post, users) for post in result.get("posts") or []]
    enriched = []
    counts = Counter()
    hit_counts = Counter()

    for rec in records:
        opinion = classify_opinion(rec.get("text", ""))
        rec["stance"] = opinion["stance"]
        rec["opinion_hits"] = opinion["hits"]
        rec["claim"] = opinion["claim"]
        rec["opinion_score"] = rec.get("score", 0) + 10 * len(opinion["hits"])
        enriched.append(rec)
        counts[opinion["stance"]] += 1
        for hit in opinion["hits"]:
            hit_counts[hit] += 1

    enriched.sort(key=lambda item: (item.get("opinion_score", 0), item.get("created_at", "")), reverse=True)

    lines = [
        "X_COMMENTS_OPINIONS",
        f"mode: {mode}",
        f"query: {result.get('query', '')}",
        f"posts_returned: {len(enriched)}",
    ]
    cost = cost_line(result.get("estimated_cost"))
    if cost:
        lines.append(cost)
    rate = rate_line(result.get("headers"))
    if rate:
        lines.append(rate)
    if not result.get("include_authors"):
        lines.append("cost_guard: author details disabled; URLs use /i/web/status to avoid user expansion reads.")

    if counts:
        lines.append(
            "stance_counts: "
            + ", ".join(f"{name}={count}" for name, count in sorted(counts.items()))
        )
    if hit_counts:
        lines.append("top_opinion_terms: " + ", ".join(f"{k}={v}" for k, v in hit_counts.most_common(12)))

    partial_errors = result.get("partial_errors") or []
    for err in partial_errors[:2]:
        lines.append(f"partial_error: {err.get('title', '')} {err.get('detail', '')}".strip())

    if not enriched:
        lines.append("Keine passenden Posts/Replies gefunden.")
        return "\n".join(lines)

    lines.append("")
    lines.append("OPINION_EXAMPLES")
    for idx, rec in enumerate(enriched, 1):
        author = f"@{rec['username']}" if rec.get("username") else f"author_id={rec.get('author_id', '')}"
        hits = ", ".join(rec.get("opinion_hits") or [])
        lines.extend(
            [
                f"{idx}. stance={rec.get('stance')} opinion_score={rec.get('opinion_score', 0)} {rec.get('created_at', '')} lang={rec.get('lang', '')} {author}",
                f"   metrics: {metrics_text(rec.get('metrics'))}",
                f"   conversation_id: {rec.get('conversation_id', '')}",
                f"   url: {rec.get('url', '')}",
                f"   hits: {hits or '-'}",
                f"   claim: {rec.get('claim', '')}",
                f"   text: {rec.get('text', '')}",
            ]
        )

    return "\n".join(lines)


def classify_opinion(text):
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    lower = clean.lower()
    words = set(re.findall(r"[a-zA-Z0-9_]+", lower))

    hits = []
    for term in sorted(POSITIVE | NEGATIVE | COMPARATIVE):
        if term.lower() in words or re.search(r"\b" + re.escape(term.lower()) + r"\b", lower):
            hits.append(term)

    has_pos = any(hit in POSITIVE for hit in hits)
    has_neg = any(hit in NEGATIVE for hit in hits)
    has_comp = any(hit in COMPARATIVE for hit in hits) or " vs " in f" {lower} "
    if has_comp:
        stance = "comparative"
    elif has_pos and not has_neg:
        stance = "positive"
    elif has_neg and not has_pos:
        stance = "negative"
    elif has_pos and has_neg:
        stance = "mixed"
    elif "?" in clean:
        stance = "question"
    else:
        stance = "neutral"

    return {"stance": stance, "hits": hits[:12], "claim": _claim_sentence(clean, hits)}


def _claim_sentence(text, hits):
    if not text:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", text)
    for part in parts:
        lower = part.lower()
        if any(re.search(r"\b" + re.escape(hit.lower()) + r"\b", lower) for hit in hits):
            return part[:260]
    return text[:260]


def _group_query(query):
    query = str(query or "").strip()
    if not query:
        return query
    if query.startswith("(") and query.endswith(")"):
        return query
    return f"({query})"


if __name__ == "__main__":
    run_module(MODULE, handle_tool)
