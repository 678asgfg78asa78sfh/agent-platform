"""Tavily Search — Web-Suche via Tavily API (besser als DuckDuckGo Scraping)."""
import json, sys, os, urllib.request

MODULE = {
    "name": "tavily",
    "description": "Web-Suche via Tavily API — liefert strukturierte Ergebnisse mit Snippets",
    "version": "1.0",
    "settings": {
        "api_key": {"type": "password", "label": "Tavily API Key", "default": ""},
        "max_results": {"type": "number", "label": "Max Ergebnisse", "default": 5},
        "search_depth": {"type": "select", "label": "Suchtiefe", "default": "basic", "options": ["basic", "advanced"]},
    },
    "tools": [
        {"name": "tavily.search", "description": "Sucht im Web via Tavily API", "params": ["query"]},
    ],
}

def handle_tool(tool_name, params, config):
    if tool_name == "tavily.search":
        query = params[0] if params else ""
        if not query.strip():
            return {"success": False, "data": "Kein Suchbegriff"}
        return _search(query, config)
    return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

def _load_own_config():
    """Eigene config.json laden (fuer API-Keys etc.)"""
    cfg_path = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(cfg_path):
        try:
            return json.load(open(cfg_path))
        except:
            pass
    return {}

def _search(query, config):
    # Eigene Config mergen (hat Vorrang fuer api_key etc.)
    own = _load_own_config()
    for k, v in own.items():
        if v:  # Nur nicht-leere Werte
            config[k] = v
    api_key = config.get("api_key", "")
    if not api_key:
        return {"success": False, "data": "Tavily API Key nicht konfiguriert. Bitte in den Modul-Settings eintragen."}

    try:
        body = json.dumps({
            "query": query,
            "max_results": config.get("max_results", 5),
            "search_depth": config.get("search_depth", "basic"),
            "include_answer": True,
        }).encode()

        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())

        results = []
        answer = data.get("answer", "")
        if answer:
            results.append(f"Antwort: {answer}\n")

        for r in data.get("results", []):
            title = r.get("title", "")
            url = r.get("url", "")
            snippet = r.get("content", "")[:200]
            results.append(f"{title}\n  {url}\n  {snippet}")

        if not results:
            return {"success": True, "data": f"Keine Ergebnisse fuer: {query}"}
        return {"success": True, "data": f"Tavily Ergebnisse fuer '{query}':\n\n" + "\n\n".join(results)}

    except urllib.error.HTTPError as e:
        return {"success": False, "data": f"Tavily API HTTP {e.code}"}
    except Exception as e:
        return {"success": False, "data": f"Tavily Fehler: {e}"}


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
