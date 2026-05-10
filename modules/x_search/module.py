"""X Search module: recent public Posts via X API v2."""
import os
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
    source_domain,
    usage_tweets,
)


MODULE = {
    "name": "x_search",
    "description": "Sucht oeffentliche X Posts nach Themen/News und bereitet sie RAG-tauglich auf.",
    "version": "1.0",
    "settings": {
        "bearer_token": {"type": "password", "label": "X Bearer Token", "default": ""},
        "max_results": {"type": "number", "label": "Max Posts pro Suche", "default": 20},
        "max_pages": {"type": "number", "label": "Max Seiten pro Suche", "default": 1},
        "lang": {"type": "string", "label": "Sprache optional (z.B. en/de)", "default": ""},
        "include_retweets": {"type": "bool", "label": "Retweets einschliessen", "default": False},
        "include_replies": {"type": "bool", "label": "Replies einschliessen", "default": False},
        "include_author_details": {
            "type": "bool",
            "label": "Autoren expandieren (kostet User Reads)",
            "default": False,
        },
        "require_links": {"type": "bool", "label": "Nur Posts mit Links", "default": False},
        "sort_order": {
            "type": "select",
            "label": "Sortierung",
            "default": "recency",
            "options": ["recency", "relevancy"],
        },
        "api_base": {"type": "string", "label": "X API Base", "default": "https://api.x.com"},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 20},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 30},
    },
    "tools": [
        {
            "name": "x_search.search",
            "description": (
                "Sucht aktuelle oeffentliche X Posts. Param: Suchtext oder JSON "
                "{\"query\":\"black hole\",\"max_results\":20,\"lang\":\"en\","
                "\"since_hours\":48,\"require_links\":true}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "x_search.usage",
            "description": "Zeigt X API Post-Verbrauch fuer Budgetkontrolle.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name, params, config):
    if tool_name == "x_search.search":
        return _search(params, config)
    if tool_name == "x_search.usage":
        return usage_tweets(config, os.path.dirname(__file__))
    return fail(f"Unbekanntes Tool: {tool_name}")


def _search(params, config):
    payload = parse_payload(params)
    base_query = first_text(payload, "query", "topic", "q", "text")
    if not base_query:
        return fail(
            "Kein Suchbegriff. Beispiel: black hole oder "
            "{\"query\":\"black hole\",\"max_results\":20,\"lang\":\"en\"}"
        )

    cfg = load_runtime_config(config, os.path.dirname(__file__))
    query, err = build_search_query(base_query, payload, cfg, default_include_replies=False)
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
    return ok(_format_search_result(result))


def _format_search_result(result):
    users = index_users(result)
    records = [record_from_post(post, users) for post in result.get("posts") or []]
    records.sort(key=lambda item: (item.get("created_at", ""), item.get("score", 0)), reverse=True)

    domains = Counter()
    for rec in records:
        for url in rec.get("urls") or []:
            domain = source_domain(url)
            if domain:
                domains[domain] += 1

    lines = [
        "X_SEARCH_RESULTS",
        f"query: {result.get('query', '')}",
        f"posts_returned: {len(records)}",
    ]
    cost = cost_line(result.get("estimated_cost"))
    if cost:
        lines.append(cost)
    rate = rate_line(result.get("headers"))
    if rate:
        lines.append(rate)
    if not result.get("include_authors"):
        lines.append("cost_guard: author details disabled; URLs use /i/web/status to avoid user expansion reads.")
    if domains:
        top_domains = ", ".join(f"{domain}={count}" for domain, count in domains.most_common(8))
        lines.append(f"linked_domains: {top_domains}")

    partial_errors = result.get("partial_errors") or []
    for err in partial_errors[:2]:
        lines.append(f"partial_error: {err.get('title', '')} {err.get('detail', '')}".strip())

    if not records:
        lines.append("Keine Posts gefunden.")
        return "\n".join(lines)

    lines.append("")
    lines.append("POSTS")
    for idx, rec in enumerate(records, 1):
        author = f"@{rec['username']}" if rec.get("username") else f"author_id={rec.get('author_id', '')}"
        refs = _refs_text(rec.get("referenced_tweets") or [])
        lines.extend(
            [
                f"{idx}. score={rec.get('score', 0)} {rec.get('created_at', '')} lang={rec.get('lang', '')} {author}",
                f"   metrics: {metrics_text(rec.get('metrics'))}",
                f"   url: {rec.get('url', '')}",
            ]
        )
        if refs:
            lines.append(f"   references: {refs}")
        if rec.get("urls"):
            lines.append("   links: " + " | ".join(rec.get("urls")[:4]))
        lines.append(f"   text: {rec.get('text', '')}")

    return "\n".join(lines)


def _refs_text(refs):
    parts = []
    for ref in refs:
        if isinstance(ref, dict) and ref.get("type") and ref.get("id"):
            parts.append(f"{ref['type']}:{ref['id']}")
    return ", ".join(parts[:4])


if __name__ == "__main__":
    run_module(MODULE, handle_tool)
