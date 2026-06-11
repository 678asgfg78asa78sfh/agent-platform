"""Grok server-side search tools via xAI Responses API."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import job_history_common as job_history
except Exception:  # pragma: no cover
    job_history = None


MODULE = {
    "name": "grok_search",
    "description": "Nutzt xAI/Grok server-side Tools web_search und x_search ueber die Responses API.",
    "version": "1.0",
    "settings": {
        "api_key": {"type": "password", "label": "xAI API Key", "default": ""},
        "api_base": {"type": "string", "label": "xAI API Base", "default": "https://api.x.ai"},
        "model": {"type": "string", "label": "Grok Model", "default": "grok-4.3"},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 60},
        "python_timeout_s": {"type": "number", "label": "Python Timeout Sekunden", "default": 120},
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 24000},
        "enable_image_understanding": {"type": "bool", "label": "Bilder in Search verstehen", "default": False},
        "enable_video_understanding": {"type": "bool", "label": "Videos in X Search verstehen", "default": False},
    },
    "tools": [
        {
            "name": "grok_search.web",
            "description": (
                "Grok Web Search ueber xAI. Param Suchtext oder JSON "
                "{\"query\":\"xAI latest news\",\"allowed_domains\":[\"x.ai\"]}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "grok_search.x",
            "description": (
                "Grok X Search ueber xAI. Param Suchtext oder JSON "
                "{\"query\":\"what are people saying about xAI\","
                "\"allowed_x_handles\":[\"elonmusk\"],\"from_date\":\"2026-05-01\"}."
            ),
            "params": ["query_json"],
        },
        {
            "name": "grok_search.research",
            "description": "Grok Recherche mit web_search und x_search gemeinsam. Param Suchtext oder JSON.",
            "params": ["query_json"],
        },
        {
            "name": "grok_search.help",
            "description": "Zeigt Beispiele und unterstuetzte Filter.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "grok_search.web":
            return _run(params, config, "web")
        if tool_name == "grok_search.x":
            return _run(params, config, "x")
        if tool_name == "grok_search.research":
            return _run(params, config, "both")
        if tool_name == "grok_search.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"Grok Search Fehler: {exc}")


def _run(params, config, mode):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "topic", "text", "prompt")
    if not query:
        return fail('Kein Query. Beispiel: grok_search.x({"query":"black hole"})')

    tools = build_tools(mode, payload, config)
    api_base = str(config.get("api_base") or "https://api.x.ai").rstrip("/")
    model = first_text(payload, "model") or str(config.get("model") or "grok-4.3")
    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": system_prompt(mode),
            },
            {
                "role": "user",
                "content": query,
            },
        ],
        "tools": tools,
    }
    if cfg_int(payload.get("max_output_tokens"), 0, 0, 200000) > 0:
        body["max_output_tokens"] = cfg_int(payload.get("max_output_tokens"), 0, 1, 200000)

    success, data, err = call_responses(api_base, config, body)
    tool_name = {"web": "grok_search.web", "x": "grok_search.x", "both": "grok_search.research"}[mode]
    if not success:
        record_history(tool_name, query, payload, "failed", config, [], err, {"model": model, "tools": tool_types(tools)})
        return fail(f"GROK_SEARCH_FAILED\nmode: {mode}\nquery: {query}\nerror: {err}")

    text, tool_calls, sources = extract_response(data)
    history_sources = sources_to_history(sources, mode)
    record_history(
        tool_name,
        query,
        payload,
        "success",
        config,
        history_sources,
        "",
        {"model": model, "tools": tool_types(tools), "server_tool_calls": len(tool_calls)},
    )
    return ok(limit_output(format_result(mode, query, model, text, tool_calls, sources, data), config))


def build_tools(mode, payload, config):
    tools = []
    if mode in {"web", "both"}:
        tool = {"type": "web_search"}
        filters = {}
        allowed_domains = list_value(payload.get("allowed_domains") or payload.get("domains"), limit=5)
        excluded_domains = list_value(payload.get("excluded_domains"), limit=5)
        if allowed_domains:
            filters["allowed_domains"] = allowed_domains
        elif excluded_domains:
            filters["excluded_domains"] = excluded_domains
        if filters:
            tool["filters"] = filters
        if cfg_bool(payload.get("enable_image_understanding", config.get("enable_image_understanding")), False):
            tool["enable_image_understanding"] = True
        tools.append(tool)

    if mode in {"x", "both"}:
        tool = {"type": "x_search"}
        allowed_handles = clean_handles(list_value(payload.get("allowed_x_handles") or payload.get("from_user"), limit=10))
        excluded_handles = clean_handles(list_value(payload.get("excluded_x_handles"), limit=10))
        if allowed_handles:
            tool["allowed_x_handles"] = allowed_handles
        elif excluded_handles:
            tool["excluded_x_handles"] = excluded_handles
        for key in ("from_date", "to_date"):
            value = first_text(payload, key)
            if value:
                tool[key] = value
        if cfg_bool(payload.get("enable_image_understanding", config.get("enable_image_understanding")), False):
            tool["enable_image_understanding"] = True
        if cfg_bool(payload.get("enable_video_understanding", config.get("enable_video_understanding")), False):
            tool["enable_video_understanding"] = True
        tools.append(tool)
    return tools


def call_responses(api_base, config, body):
    api_key = first_text(config, "api_key") or os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if not api_key:
        return False, {}, "api_key fehlt. Setze api_key im Modul oder XAI_API_KEY."
    timeout = cfg_int(config.get("request_timeout_s"), 60, 5, 300)
    url = api_base + "/v1/responses"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        return True, json.loads(raw or "{}"), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return False, {}, f"HTTP {exc.code}: {truncate(raw, 800)}"
    except urllib.error.URLError as exc:
        return False, {}, f"request failed: {exc}"
    except json.JSONDecodeError as exc:
        return False, {}, f"response json parse failed: {exc}"


def extract_response(data):
    text_parts = []
    tool_calls = []
    sources = []

    if isinstance(data.get("output_text"), str) and data.get("output_text"):
        text_parts.append(data["output_text"])

    for item in data.get("output") or []:
        item_type = str(item.get("type") or "")
        if item_type.endswith("_call"):
            tool_calls.append(
                {
                    "type": item_type,
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                    "status": item.get("status", ""),
                }
            )
        for content in item.get("content") or []:
            if isinstance(content.get("text"), str):
                text_parts.append(content["text"])
            for ann in content.get("annotations") or []:
                src = annotation_source(ann)
                if src:
                    sources.append(src)

    for citation in data.get("citations") or []:
        src = citation_source(citation)
        if src:
            sources.append(src)

    return dedupe_lines("\n".join(text_parts).strip()), tool_calls, dedupe_sources(sources)


def annotation_source(ann):
    if not isinstance(ann, dict):
        return None
    url = ann.get("url") or ann.get("source_url")
    if not url:
        return None
    return {
        "url": str(url),
        "title": str(ann.get("title") or ann.get("text") or url),
        "type": str(ann.get("type") or "citation"),
    }


def citation_source(citation):
    if isinstance(citation, str):
        return {"url": citation, "title": citation, "type": "citation"}
    if not isinstance(citation, dict):
        return None
    url = citation.get("url") or citation.get("source_url")
    if not url:
        return None
    return {
        "url": str(url),
        "title": str(citation.get("title") or citation.get("name") or url),
        "type": str(citation.get("type") or "citation"),
    }


def sources_to_history(sources, mode):
    if job_history is None:
        return []
    out = []
    for src in sources:
        out.append(
            job_history.source(
                source_type="grok_x_search" if mode == "x" else "grok_web_search",
                source_url=src.get("url", ""),
                source_title=src.get("title", ""),
                source_name="xAI Grok",
                metadata={"citation_type": src.get("type", ""), "mode": mode},
            )
        )
    return out


def record_history(tool, query, params, status, config, sources, error="", metrics=None):
    if job_history is None:
        return
    try:
        job_history.record_job(
            module="grok_search",
            tool=tool,
            query=query,
            params=params,
            status=status,
            config=config,
            sources=sources,
            summary=f"{tool} {status}",
            error=error,
            metrics=metrics or {},
        )
    except Exception:
        pass


def format_result(mode, query, model, text, tool_calls, sources, data):
    lines = [
        "GROK_SEARCH",
        f"mode: {mode}",
        f"model: {model}",
        f"query: {query}",
        f"server_tool_calls: {len(tool_calls)}",
        f"sources: {len(sources)}",
    ]
    usage = data.get("usage") or {}
    if usage:
        usage_parts = []
        for key in ("input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"):
            if usage.get(key) is not None:
                usage_parts.append(f"{key}={usage.get(key)}")
        if usage_parts:
            lines.append("usage: " + " ".join(usage_parts))
    if tool_calls:
        lines.append("")
        lines.append("TOOL_CALLS")
        for call in tool_calls[:12]:
            label = call.get("name") or call.get("type")
            args = truncate(str(call.get("arguments") or ""), 240)
            status = call.get("status") or ""
            lines.append(f"- {label} status={status} args={args}")
    if sources:
        lines.append("")
        lines.append("SOURCES")
        for idx, src in enumerate(sources[:20], 1):
            lines.append(f"{idx}. {src.get('title', '')}\n   {src.get('url', '')}")
    lines.append("")
    lines.append("ANSWER")
    lines.append(text or "(keine Textantwort im Response)")
    return "\n".join(lines)


def help_text():
    return "\n".join(
        [
            "GROK_SEARCH_HELP",
            "Tools:",
            "- grok_search.web({\"query\":\"xAI latest news\",\"allowed_domains\":[\"x.ai\"]})",
            "- grok_search.x({\"query\":\"what are people saying about xAI\",\"from_date\":\"2026-05-01\"})",
            "- grok_search.research({\"query\":\"latest Grok API agent tools\"})",
            "",
            "Filter:",
            "- web: allowed_domains oder excluded_domains, max 5",
            "- x: allowed_x_handles oder excluded_x_handles, max 10",
            "- x: from_date/to_date im Format YYYY-MM-DD",
            "- x: enable_image_understanding, enable_video_understanding",
            "",
            "Hinweis: Nutzt xAI Responses API mit server-side web_search/x_search, nicht die X API v2.",
        ]
    )


def system_prompt(mode):
    if mode == "x":
        return (
            "Nutze X Search fuer aktuelle oeffentliche X-Posts. "
            "Fasse Meinungen, starke Signale und Unsicherheiten kurz zusammen. "
            "Nenne Quellen/Zitate wenn vorhanden."
        )
    if mode == "web":
        return (
            "Nutze Web Search fuer aktuelle Web-Recherche. "
            "Gib eine knappe, belastbare Zusammenfassung mit Quellen aus."
        )
    return (
        "Nutze Web Search und X Search fuer aktuelle Recherche. "
        "Trenne Web-Fakten von X-Meinungsbild und markiere Unsicherheiten."
    )


def parse_payload(params, default_key="query"):
    if isinstance(params, dict):
        return dict(params)
    if not params:
        return {}
    raw = params[0]
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {default_key: text} if text else {}


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key) if isinstance(payload, dict) else None
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def list_value(value, limit=10):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        parts = [str(v).strip() for v in value]
    else:
        parts = re.split(r"[,\n;]+", str(value))
        parts = [p.strip() for p in parts]
    out = []
    for part in parts:
        if part and part not in out:
            out.append(part)
        if len(out) >= limit:
            break
    return out


def clean_handles(handles):
    out = []
    for handle in handles:
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", str(handle).lstrip("@"))
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out


def cfg_bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "ja", "on", "y"}:
        return True
    if text in {"0", "false", "no", "nein", "off", "n"}:
        return False
    return default


def cfg_int(value, default, min_value=None, max_value=None):
    try:
        num = int(float(str(value)))
    except Exception:
        num = default
    if min_value is not None:
        num = max(min_value, num)
    if max_value is not None:
        num = min(max_value, num)
    return num


def tool_types(tools):
    return [str(t.get("type", "")) for t in tools]


def dedupe_lines(text):
    lines = []
    for line in str(text or "").splitlines():
        if lines and lines[-1] == line:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def dedupe_sources(sources):
    seen = set()
    out = []
    for src in sources:
        url = str(src.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(src)
    return out


def truncate(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def limit_output(text, config):
    limit = cfg_int(config.get("max_output_chars", 24000), 24000, 2000, 100000)
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def ok(data):
    return {"success": True, "data": data}


def fail(data):
    return {"success": False, "data": data}


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
