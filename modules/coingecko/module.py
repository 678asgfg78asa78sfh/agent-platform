"""CoinGecko market data module."""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    import job_history_common as job_history
except Exception:  # pragma: no cover
    job_history = None


MODULE = {
    "name": "coingecko",
    "description": "CoinGecko API Zugriff fuer Krypto-Preise, Markets, Coin-Metadaten, Charts, Trending und Globaldaten.",
    "version": "1.0",
    "settings": {
        "api_key": {"type": "password", "label": "CoinGecko API Key optional", "default": ""},
        "api_tier": {"type": "select", "label": "API Tier", "default": "demo", "options": ["demo", "pro", "public"]},
        "api_base": {"type": "string", "label": "API Base Override", "default": ""},
        "default_vs_currency": {"type": "string", "label": "Default VS Currency", "default": "usd"},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 20},
        "max_results": {"type": "number", "label": "Max Ergebnisse", "default": 20},
        "max_output_chars": {"type": "number", "label": "Max Ausgabezeichen", "default": 24000},
    },
    "tools": [
        {
            "name": "coingecko.price",
            "description": "Live-Preise. JSON {ids|symbols|names:'bitcoin,ethereum', vs:'usd,eur', include_market_cap:true}.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.markets",
            "description": "Coin-Market-Daten. JSON {vs:'usd', ids?, symbols?, category?, order?, per_page?, page?, sparkline?, price_change_percentage:'1h,24h,7d'}.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.search",
            "description": "Sucht CoinGecko Coins/Categories/Exchanges nach Text. JSON {query:'bitcoin', limit:10}.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.coin",
            "description": "Coin-Metadaten per Coin ID. JSON {id:'bitcoin', tickers:false, market_data:true, community_data:false, developer_data:false}.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.chart",
            "description": "Historische Preis-/Market-Cap-/Volumenpunkte. JSON {id:'bitcoin', vs:'usd', days:7, interval:'daily'}.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.trending",
            "description": "Trending Coins/NFTs/Categories von CoinGecko.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.global",
            "description": "Globaler Kryptomarkt: Market Cap, Volumen, Dominanz.",
            "params": ["query_json"],
        },
        {
            "name": "coingecko.help",
            "description": "Zeigt Beispiele und API-Key Hinweise.",
            "params": [],
        },
    ],
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "coingecko.price":
            return _price(params, config)
        if tool_name == "coingecko.markets":
            return _markets(params, config)
        if tool_name == "coingecko.search":
            return _search(params, config)
        if tool_name == "coingecko.coin":
            return _coin(params, config)
        if tool_name == "coingecko.chart":
            return _chart(params, config)
        if tool_name == "coingecko.trending":
            return _trending(params, config)
        if tool_name == "coingecko.global":
            return _global(params, config)
        if tool_name == "coingecko.help":
            return ok(help_text())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"CoinGecko Fehler: {exc}")


def _price(params, config):
    payload = parse_payload(params)
    query = first_text(payload, "ids", "symbols", "names", "query", "q", "coin", "coins")
    if not query:
        return fail('Kein Coin. Beispiel: coingecko.price({"ids":"bitcoin,ethereum","vs":"usd,eur"})')
    vs = first_text(payload, "vs", "vs_currency", "vs_currencies") or config.get("default_vs_currency") or "usd"
    api_params = {"vs_currencies": normalize_csv(vs)}
    if payload.get("ids"):
        api_params["ids"] = normalize_csv(payload.get("ids"))
    elif payload.get("names"):
        api_params["names"] = normalize_csv(payload.get("names"))
    elif payload.get("symbols"):
        api_params["symbols"] = normalize_csv(payload.get("symbols"))
        api_params["include_tokens"] = first_text(payload, "include_tokens") or "top"
    else:
        api_params["ids"] = normalize_csv(query)
    for key in ("include_market_cap", "include_24hr_vol", "include_24hr_change", "include_last_updated_at"):
        api_params[key] = bool_param(payload.get(key, True))

    success, data, err, url = api_get("/simple/price", api_params, config)
    if not success:
        record_history("coingecko.price", query, payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_PRICE_FAILED\nquery: {query}\nerror: {err}")
    sources = price_sources(data, api_params)
    record_history("coingecko.price", query, payload, "success", config, sources, "", {"url": url, "coins": len(data)})
    return ok(limit_output(format_price(data, api_params), config))


def _markets(params, config):
    payload = parse_payload(params)
    vs = first_text(payload, "vs", "vs_currency") or config.get("default_vs_currency") or "usd"
    per_page = cfg_int(payload.get("per_page", payload.get("limit", config.get("max_results", 20))), 20, 1, 250)
    api_params = {
        "vs_currency": vs,
        "order": first_text(payload, "order", "sort") or "market_cap_desc",
        "per_page": per_page,
        "page": cfg_int(payload.get("page", 1), 1, 1, 10000),
        "sparkline": bool_param(payload.get("sparkline", False)),
        "price_change_percentage": first_text(payload, "price_change_percentage", "change") or "1h,24h,7d,30d",
        "locale": first_text(payload, "locale") or "en",
    }
    for key in ("ids", "names", "symbols", "category"):
        if payload.get(key):
            api_params[key] = normalize_csv(payload.get(key))
    if payload.get("include_tokens"):
        api_params["include_tokens"] = first_text(payload, "include_tokens")

    query = first_text(payload, "ids", "symbols", "names", "category", "query", "q") or "markets"
    success, data, err, url = api_get("/coins/markets", api_params, config)
    if not success:
        record_history("coingecko.markets", query, payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_MARKETS_FAILED\nquery: {query}\nerror: {err}")
    sources = market_sources(data)
    record_history("coingecko.markets", query, payload, "success", config, sources, "", {"url": url, "coins": len(data)})
    return ok(limit_output(format_markets(data, vs), config))


def _search(params, config):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "coin", "text")
    if not query:
        return fail('Kein Suchtext. Beispiel: coingecko.search({"query":"bitcoin"})')
    success, data, err, url = api_get("/search", {"query": query}, config)
    if not success:
        record_history("coingecko.search", query, payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_SEARCH_FAILED\nquery: {query}\nerror: {err}")
    limit = cfg_int(payload.get("limit", config.get("max_results", 20)), 20, 1, 100)
    coins = (data.get("coins") or [])[:limit]
    sources = [
        job_history.source(
            source_type="coingecko_coin",
            source_url=f"https://www.coingecko.com/en/coins/{coin.get('id','')}",
            source_title=coin.get("name", ""),
            source_id=coin.get("id", ""),
            source_name=coin.get("symbol", ""),
            score=coin.get("market_cap_rank"),
            metadata={"api_symbol": coin.get("api_symbol", ""), "thumb": coin.get("thumb", "")},
        )
        for coin in coins
        if job_history is not None
    ]
    record_history("coingecko.search", query, payload, "success", config, sources, "", {"url": url, "coins": len(coins)})
    return ok(limit_output(format_search(data, limit), config))


def _coin(params, config):
    payload = parse_payload(params)
    coin_id = first_text(payload, "id", "coin_id", "query", "coin")
    if not coin_id:
        return fail('Coin ID fehlt. Beispiel: coingecko.coin({"id":"bitcoin"})')
    api_params = {
        "localization": bool_param(payload.get("localization", False)),
        "tickers": bool_param(payload.get("tickers", False)),
        "market_data": bool_param(payload.get("market_data", True)),
        "community_data": bool_param(payload.get("community_data", False)),
        "developer_data": bool_param(payload.get("developer_data", False)),
        "sparkline": bool_param(payload.get("sparkline", False)),
    }
    success, data, err, url = api_get(f"/coins/{urlquote(coin_id)}", api_params, config)
    if not success:
        record_history("coingecko.coin", coin_id, payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_COIN_FAILED\nid: {coin_id}\nerror: {err}")
    sources = [coin_source(data)]
    record_history("coingecko.coin", coin_id, payload, "success", config, sources, "", {"url": url})
    return ok(limit_output(format_coin(data), config))


def _chart(params, config):
    payload = parse_payload(params)
    coin_id = first_text(payload, "id", "coin_id", "query", "coin") or "bitcoin"
    vs = first_text(payload, "vs", "vs_currency") or config.get("default_vs_currency") or "usd"
    api_params = {
        "vs_currency": vs,
        "days": first_text(payload, "days") or "7",
    }
    if payload.get("interval"):
        api_params["interval"] = first_text(payload, "interval")
    if payload.get("precision"):
        api_params["precision"] = first_text(payload, "precision")
    success, data, err, url = api_get(f"/coins/{urlquote(coin_id)}/market_chart", api_params, config)
    if not success:
        record_history("coingecko.chart", coin_id, payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_CHART_FAILED\nid: {coin_id}\nerror: {err}")
    src = job_history.source(
        source_type="coingecko_chart",
        source_url=f"https://www.coingecko.com/en/coins/{coin_id}",
        source_title=f"{coin_id} market chart",
        source_id=coin_id,
        source_name="CoinGecko",
        metadata={"vs_currency": vs, "days": api_params["days"], "points": len(data.get("prices") or [])},
    ) if job_history is not None else None
    record_history("coingecko.chart", coin_id, payload, "success", config, [src] if src else [], "", {"url": url, "points": len(data.get("prices") or [])})
    return ok(limit_output(format_chart(coin_id, vs, data), config))


def _trending(params, config):
    payload = parse_payload(params)
    success, data, err, url = api_get("/search/trending", {}, config)
    if not success:
        record_history("coingecko.trending", "trending", payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_TRENDING_FAILED\nerror: {err}")
    coins = [item.get("item", {}) for item in data.get("coins") or []]
    sources = [coin_source(c) for c in coins if c]
    record_history("coingecko.trending", "trending", payload, "success", config, sources, "", {"url": url, "coins": len(coins)})
    return ok(limit_output(format_trending(data), config))


def _global(params, config):
    payload = parse_payload(params)
    success, data, err, url = api_get("/global", {}, config)
    if not success:
        record_history("coingecko.global", "global", payload, "failed", config, [], err, {"url": url})
        return fail(f"COINGECKO_GLOBAL_FAILED\nerror: {err}")
    src = job_history.source(
        source_type="coingecko_global",
        source_url="https://www.coingecko.com/",
        source_title="Global cryptocurrency market data",
        source_name="CoinGecko",
        metadata=data.get("data") or {},
    ) if job_history is not None else None
    record_history("coingecko.global", "global", payload, "success", config, [src] if src else [], "", {"url": url})
    return ok(limit_output(format_global(data.get("data") or {}), config))


def api_get(path, params, config):
    base = api_base(config).rstrip("/")
    url = base + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"Accept": "application/json", "User-Agent": "aistuff-coingecko/1.0"}
    key = str(config.get("api_key") or os.environ.get("COINGECKO_API_KEY") or "").strip()
    tier = str(config.get("api_tier") or "demo").strip().lower()
    if key:
        headers["x-cg-pro-api-key" if tier == "pro" else "x-cg-demo-api-key"] = key
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg_int(config.get("request_timeout_s"), 20, 5, 60)) as resp:
            return True, json.loads(resp.read().decode("utf-8", errors="replace")), "", url
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, None, f"HTTP {exc.code}: {truncate(body, 1200)}", url
    except Exception as exc:
        return False, None, f"HTTP Fehler: {exc}", url


def api_base(config):
    override = str(config.get("api_base") or "").strip()
    if override:
        return override
    tier = str(config.get("api_tier") or "demo").strip().lower()
    return "https://pro-api.coingecko.com/api/v3" if tier == "pro" else "https://api.coingecko.com/api/v3"


def format_price(data, params):
    lines = [
        "COINGECKO_PRICE",
        f"vs_currencies: {params.get('vs_currencies','')}",
        f"coins: {len(data)}",
        "",
        "PRICES",
    ]
    for coin_id, values in data.items():
        parts = []
        for key, val in values.items():
            if key.endswith("_market_cap"):
                parts.append(f"{key}={num(val)}")
            elif key.endswith("_24h_vol"):
                parts.append(f"{key}={num(val)}")
            elif key.endswith("_24h_change"):
                parts.append(f"{key}={pct(val)}")
            elif key == "last_updated_at":
                parts.append(f"{key}={unix_iso(val)}")
            else:
                parts.append(f"{key}={num(val)}")
        lines.append(f"- {coin_id}: " + " | ".join(parts))
    return "\n".join(lines)


def format_markets(rows, vs):
    lines = ["COINGECKO_MARKETS", f"vs_currency: {vs}", f"coins: {len(rows)}", "", "MARKETS"]
    for idx, row in enumerate(rows, 1):
        lines.append(
            f"{idx}. rank={row.get('market_cap_rank')} {row.get('name')} ({str(row.get('symbol','')).upper()}) "
            f"price={num(row.get('current_price'))} {vs.upper()} mcap={num(row.get('market_cap'))} vol24h={num(row.get('total_volume'))}"
        )
        lines.append(
            f"   change: 1h={pct(row.get('price_change_percentage_1h_in_currency'))} "
            f"24h={pct(row.get('price_change_percentage_24h'))} "
            f"7d={pct(row.get('price_change_percentage_7d_in_currency'))} "
            f"30d={pct(row.get('price_change_percentage_30d_in_currency'))} last_updated={row.get('last_updated','')}"
        )
        lines.append(f"   id={row.get('id','')} url=https://www.coingecko.com/en/coins/{row.get('id','')}")
    return "\n".join(lines)


def format_search(data, limit):
    lines = ["COINGECKO_SEARCH", f"coins: {min(len(data.get('coins') or []), limit)}", "", "COINS"]
    for idx, coin in enumerate((data.get("coins") or [])[:limit], 1):
        lines.append(
            f"{idx}. rank={coin.get('market_cap_rank')} {coin.get('name')} ({str(coin.get('symbol','')).upper()}) "
            f"id={coin.get('id','')} api_symbol={coin.get('api_symbol','')}"
        )
        lines.append(f"   url=https://www.coingecko.com/en/coins/{coin.get('id','')}")
    if data.get("categories"):
        lines.append("")
        lines.append("CATEGORIES")
        for cat in data.get("categories", [])[:10]:
            lines.append(f"- {cat.get('name','')} id={cat.get('id','')}")
    return "\n".join(lines)


def format_coin(data):
    market = data.get("market_data") or {}
    usd = (market.get("current_price") or {}).get("usd")
    eur = (market.get("current_price") or {}).get("eur")
    lines = [
        "COINGECKO_COIN",
        f"id: {data.get('id','')}",
        f"name: {data.get('name','')} ({str(data.get('symbol','')).upper()})",
        f"rank: {data.get('market_cap_rank','')}",
        f"price_usd: {num(usd)}",
        f"price_eur: {num(eur)}",
        f"market_cap_usd: {num((market.get('market_cap') or {}).get('usd'))}",
        f"volume_24h_usd: {num((market.get('total_volume') or {}).get('usd'))}",
        f"change_24h: {pct(market.get('price_change_percentage_24h'))}",
        f"change_7d: {pct(market.get('price_change_percentage_7d'))}",
        f"change_30d: {pct(market.get('price_change_percentage_30d'))}",
        f"homepage: {first_list((data.get('links') or {}).get('homepage'))}",
        f"url: https://www.coingecko.com/en/coins/{data.get('id','')}",
    ]
    desc = (data.get("description") or {}).get("en") or ""
    if desc:
        lines.append("description: " + truncate(strip_html(desc), 900))
    return "\n".join(lines)


def format_chart(coin_id, vs, data):
    prices = data.get("prices") or []
    caps = data.get("market_caps") or []
    volumes = data.get("total_volumes") or []
    lines = ["COINGECKO_CHART", f"id: {coin_id}", f"vs_currency: {vs}", f"points: {len(prices)}"]
    if prices:
        first_ts, first_price = prices[0]
        last_ts, last_price = prices[-1]
        change = ((last_price - first_price) / first_price * 100) if first_price else None
        lines.extend(
            [
                f"first: {ms_iso(first_ts)} price={num(first_price)}",
                f"last: {ms_iso(last_ts)} price={num(last_price)}",
                f"change_period: {pct(change)}",
                "",
                "RECENT_POINTS",
            ]
        )
        for point in prices[-10:]:
            lines.append(f"- {ms_iso(point[0])} price={num(point[1])}")
    if caps:
        lines.append(f"last_market_cap: {num(caps[-1][1])}")
    if volumes:
        lines.append(f"last_volume: {num(volumes[-1][1])}")
    return "\n".join(lines)


def format_trending(data):
    lines = ["COINGECKO_TRENDING", "", "COINS"]
    for idx, item in enumerate(data.get("coins") or [], 1):
        coin = item.get("item") or {}
        lines.append(
            f"{idx}. rank={coin.get('market_cap_rank')} score={coin.get('score')} "
            f"{coin.get('name','')} ({str(coin.get('symbol','')).upper()}) id={coin.get('id','')}"
        )
        lines.append(f"   url=https://www.coingecko.com/en/coins/{coin.get('id','')}")
    if data.get("categories"):
        lines.append("")
        lines.append("CATEGORIES")
        for cat in data.get("categories", [])[:10]:
            lines.append(f"- {cat.get('name','')} market_cap_1h_change={pct(cat.get('market_cap_1h_change'))}")
    return "\n".join(lines)


def format_global(data):
    lines = [
        "COINGECKO_GLOBAL",
        f"active_cryptocurrencies: {data.get('active_cryptocurrencies','')}",
        f"markets: {data.get('markets','')}",
        f"total_market_cap_usd: {num((data.get('total_market_cap') or {}).get('usd'))}",
        f"total_volume_usd: {num((data.get('total_volume') or {}).get('usd'))}",
        f"market_cap_change_24h: {pct(data.get('market_cap_change_percentage_24h_usd'))}",
        "dominance:",
    ]
    dom = data.get("market_cap_percentage") or {}
    for symbol, value in sorted(dom.items(), key=lambda kv: kv[1], reverse=True)[:12]:
        lines.append(f"- {symbol.upper()}: {pct(value)}")
    return "\n".join(lines)


def price_sources(data, params):
    if job_history is None:
        return []
    sources = []
    for coin_id, values in data.items():
        sources.append(
            job_history.source(
                source_type="coingecko_price",
                source_url=f"https://www.coingecko.com/en/coins/{coin_id}",
                source_title=f"{coin_id} price",
                source_id=coin_id,
                source_name="CoinGecko",
                metadata={"vs_currencies": params.get("vs_currencies"), "values": values},
            )
        )
    return sources


def market_sources(rows):
    return [coin_source(row) for row in rows if row]


def coin_source(data):
    if job_history is None:
        return {}
    coin_id = data.get("id", "")
    return job_history.source(
        source_type="coingecko_coin",
        source_url=f"https://www.coingecko.com/en/coins/{coin_id}",
        source_title=data.get("name", ""),
        source_id=coin_id,
        source_name=str(data.get("symbol", "")).upper(),
        score=data.get("market_cap_rank"),
        metadata={
            "current_price": data.get("current_price"),
            "market_cap": data.get("market_cap"),
            "total_volume": data.get("total_volume"),
            "last_updated": data.get("last_updated"),
        },
    )


def record_history(tool, query, params, status, config, sources, error="", metrics=None):
    if job_history is None:
        return
    try:
        job_history.record_job(
            module="coingecko",
            tool=tool,
            query=query,
            params=params,
            status=status,
            config=config,
            sources=[s for s in sources if s],
            summary=f"{tool} {status}",
            error=error,
            metrics=metrics or {},
        )
    except Exception:
        pass


def help_text():
    return "\n".join(
        [
            "COINGECKO_HELP",
            "API docs: https://docs.coingecko.com/reference/endpoint-overview",
            "settings: api_tier=public|demo|pro, api_key optional, default_vs_currency=usd",
            'price: {"ids":"bitcoin,ethereum","vs":"usd,eur"}',
            'markets: {"vs":"usd","symbols":"btc,eth","per_page":20,"price_change_percentage":"1h,24h,7d,30d"}',
            'search: {"query":"solana","limit":10}',
            'coin: {"id":"bitcoin","market_data":true,"developer_data":false}',
            'chart: {"id":"bitcoin","vs":"usd","days":30,"interval":"daily"}',
        ]
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
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def normalize_csv(value):
    if isinstance(value, (list, tuple, set)):
        parts = [str(x).strip() for x in value if str(x).strip()]
    else:
        parts = [x.strip() for x in str(value or "").replace(";", ",").split(",") if x.strip()]
    return ",".join(parts)


def bool_param(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "false"
    return "true" if str(value).strip().lower() in {"1", "true", "yes", "ja", "on"} else "false"


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


def urlquote(value):
    return urllib.parse.quote(str(value or "").strip(), safe="")


def num(value):
    if value in (None, ""):
        return "n/a"
    try:
        val = float(value)
        if abs(val) >= 1_000_000_000:
            return f"{val/1_000_000_000:.3f}B"
        if abs(val) >= 1_000_000:
            return f"{val/1_000_000:.3f}M"
        if abs(val) >= 1000:
            return f"{val:,.2f}"
        if abs(val) >= 1:
            return f"{val:.4f}"
        return f"{val:.8f}"
    except Exception:
        return str(value)


def pct(value):
    if value in (None, ""):
        return "n/a"
    try:
        return f"{float(value):.2f}%"
    except Exception:
        return str(value)


def unix_iso(value):
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(value or "")


def ms_iso(value):
    try:
        return datetime.fromtimestamp(float(value) / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return str(value or "")


def first_list(value):
    if isinstance(value, list):
        return next((str(x) for x in value if x), "")
    return str(value or "")


def strip_html(text):
    import re

    return re.sub(r"<[^>]+>", " ", str(text or ""))


def truncate(text, limit):
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def limit_output(text, config):
    limit = cfg_int(config.get("max_output_chars", 24000), 24000, 2000, 80000)
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
                result = handle_tool(req["tool"], req.get("params", []), req.get("config", {}))
                print(json.dumps(result), flush=True)
            else:
                print(json.dumps({"error": f"Unknown action: {req.get('action')}"}), flush=True)
        except Exception as exc:
            print(json.dumps({"error": str(exc)}), flush=True)
