"""DuckDuckGo Web-Suche — kein pip noetig, nutzt DuckDuckGo Lite (HTML scraping)."""
import json, sys, urllib.request, urllib.parse, re

MODULE = {
    "name": "duckduckgo_search",
    "description": "Web-Suche via DuckDuckGo (kein API-Key noetig)",
    "version": "1.0",
    "settings": {
        "max_results": {"type": "number", "label": "Max Ergebnisse", "default": 8},
    },
    "tools": [
        {"name": "duckduckgo.search", "description": "Sucht im Web via DuckDuckGo", "params": ["query"]},
    ],
}

def handle_tool(tool_name, params, config):
    if tool_name == "duckduckgo.search":
        query = params[0] if params else ""
        if not query.strip():
            return {"success": False, "data": "Kein Suchbegriff"}
        max_results = config.get("max_results", 8)
        return _search(query, max_results)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _search(query, max_results=8):
    try:
        encoded = urllib.parse.quote_plus(query)
        url = f"https://lite.duckduckgo.com/lite/?q={encoded}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
        })
        resp = urllib.request.urlopen(req, timeout=10)
        html = resp.read().decode("utf-8", errors="replace")

        results = []
        # DDG Lite: Links mit class='result-link', Snippets mit class="result-snippet"
        link_pattern = re.compile(r"class='result-link'[^>]*>(.*?)</a>", re.DOTALL)
        href_pattern = re.compile(r'href="([^"]*)"')
        snippet_pattern = re.compile(r'class="result-snippet"[^>]*>(.*?)</td>', re.DOTALL)

        links = link_pattern.finditer(html)
        snippets = snippet_pattern.finditer(html)

        snippet_list = [_strip_html(m.group(1)) for m in snippets]

        for i, link_match in enumerate(links):
            if len(results) >= max_results:
                break
            title = _strip_html(link_match.group(1)).strip()
            if not title:
                continue

            # URL aus dem umgebenden <a> Tag extrahieren
            start = max(0, link_match.start() - 500)
            context = html[start:link_match.start()]
            href_m = list(href_pattern.finditer(context))
            raw_url = href_m[-1].group(1) if href_m else ""

            # DDG redirect URL auflösen
            if "uddg=" in raw_url:
                uddg = raw_url.split("uddg=")[1].split("&")[0]
                real_url = urllib.parse.unquote(uddg)
            elif raw_url.startswith("http"):
                real_url = raw_url
            else:
                continue

            # Interne DDG links skippen
            if "duckduckgo.com" in real_url or "duck.co" in real_url:
                continue

            snippet = snippet_list[i] if i < len(snippet_list) else ""
            results.append(f"{title}\n  {real_url}\n  {snippet.strip()}")

        if not results:
            return {"success": True, "data": f"Keine Ergebnisse fuer: {query}"}
        return {"success": True, "data": f"DuckDuckGo Ergebnisse fuer '{query}':\n\n" + "\n\n".join(results)}

    except Exception as e:
        return {"success": False, "data": f"DuckDuckGo Fehler: {e}"}

def _strip_html(text):
    return re.sub(r'<[^>]+>', '', text).strip()


if __name__ == "__main__":
    for line in sys.stdin:
        try:
            req = json.loads(line.strip())
            if req.get("action") == "describe":
                print(json.dumps(MODULE), flush=True)
            elif req.get("action") == "handle_tool":
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as e:
            print(json.dumps({"error": str(e)}), flush=True)
