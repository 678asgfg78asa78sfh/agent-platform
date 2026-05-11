"""eBay.de search and listing analysis via eBay Browse API with public HTML fallback."""

import base64
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


MODULE = {
    "name": "ebay_de",
    "description": "Sucht und analysiert eBay.de-Angebote: Preise, Versand, Zustand, Ausreisser und Deal-Kandidaten.",
    "version": "1.1",
    "settings": {
        "client_id": {"type": "password", "label": "eBay Client ID optional", "default": ""},
        "client_secret": {"type": "password", "label": "eBay Client Secret optional", "default": ""},
        "access_token": {"type": "password", "label": "eBay OAuth Access Token optional", "default": ""},
        "marketplace_id": {"type": "string", "label": "Marketplace", "default": "EBAY_DE"},
        "country": {"type": "string", "label": "Land fuer Kontext", "default": "DE"},
        "zip": {"type": "string", "label": "PLZ optional", "default": ""},
        "max_results": {"type": "number", "label": "Max Ergebnisse", "default": 50},
        "default_delivery_country": {"type": "string", "label": "Versandland Standard", "default": "DE"},
        "default_location_country": {"type": "string", "label": "Artikelstandort Standard", "default": ""},
        "prefer_api": {"type": "bool", "label": "Browse API bevorzugen", "default": True},
        "allow_public_html": {"type": "bool", "label": "Public HTML Fallback erlauben", "default": True},
        "request_timeout_s": {"type": "number", "label": "HTTP Timeout Sekunden", "default": 20},
    },
    "tools": [
        {
            "name": "ebay_de.search",
            "description": "Sucht eBay.de-Angebote. JSON {query|url, limit, sort, buying_option/sofortkauf/auktion, condition/neu/gebraucht, seller_type/haendler/privat, shipping/free_shipping, delivery_country, postal_code, location_country, min_price, max_price, include_terms, exclude_terms}.",
            "params": ["query_json"],
        },
        {
            "name": "ebay_de.analyze",
            "description": "Wie search, aber Ausgabe priorisiert Preisbewertung, Filter-Report, Markt-Snapshot und Deal-Kandidaten.",
            "params": ["query_json"],
        },
        {
            "name": "ebay_de.item",
            "description": "Holt Details zu einem Item per item_id oder URL, bevorzugt Browse API.",
            "params": ["query_json"],
        },
        {
            "name": "ebay_de.parse_html",
            "description": "Analysiert gespeichertes/eingefuegtes eBay-Suchseiten-HTML. JSON {html, query?, condition?, shipping?, include_terms?, exclude_terms?}.",
            "params": ["html_json"],
        },
        {
            "name": "ebay_de.filters",
            "description": "Zeigt unterstuetzte eBay-Filter, deutsche Aliase und JSON-Beispiele fuer Marktanalysen.",
            "params": ["query_json"],
        },
    ],
}


API_BASE = "https://api.ebay.com/buy/browse/v1"
OAUTH_URL = "https://api.ebay.com/identity/v1/oauth2/token"
OAUTH_SCOPE = "https://api.ebay.com/oauth/api_scope"

TOKEN_CACHE = {"token": "", "expires_at": 0}

BUYING_OPTION_ALIASES = {
    "fixed": "FIXED_PRICE",
    "fixed_price": "FIXED_PRICE",
    "buy_it_now": "FIXED_PRICE",
    "bin": "FIXED_PRICE",
    "sofortkauf": "FIXED_PRICE",
    "festpreis": "FIXED_PRICE",
    "auction": "AUCTION",
    "auktion": "AUCTION",
    "versteigerung": "AUCTION",
    "best_offer": "BEST_OFFER",
    "preisvorschlag": "BEST_OFFER",
    "angebot": "BEST_OFFER",
    "classified": "CLASSIFIED_AD",
    "classified_ad": "CLASSIFIED_AD",
    "kleinanzeige": "CLASSIFIED_AD",
}

CONDITION_ALIASES = {
    "new": ["NEW"],
    "neu": ["NEW"],
    "brand_new": ["NEW"],
    "unused": ["NEW"],
    "used": ["USED"],
    "gebraucht": ["USED"],
    "second_hand": ["USED"],
    "refurbished": ["2000", "2500"],
    "refurb": ["2000", "2500"],
    "generaluberholt": ["2000", "2500"],
    "generalueberholt": ["2000", "2500"],
    "seller_refurbished": ["2500"],
    "parts": ["7000"],
    "defekt": ["7000"],
    "ersatzteile": ["7000"],
}

SELLER_TYPE_ALIASES = {
    "business": "BUSINESS",
    "haendler": "BUSINESS",
    "handler": "BUSINESS",
    "gewerblich": "BUSINESS",
    "commercial": "BUSINESS",
    "privat": "INDIVIDUAL",
    "private": "INDIVIDUAL",
    "individual": "INDIVIDUAL",
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "ebay_de.search":
            return _search(params, config, analysis_first=False)
        if tool_name == "ebay_de.analyze":
            return _search(params, config, analysis_first=True)
        if tool_name == "ebay_de.item":
            return _item(params, config)
        if tool_name == "ebay_de.parse_html":
            return _parse_html_tool(params, config)
        if tool_name == "ebay_de.filters":
            return ok(filter_help())
        return fail(f"Unbekanntes Tool: {tool_name}")
    except Exception as exc:
        return fail(f"eBay.de Fehler: {exc}")


def _search(params, config, analysis_first=False):
    payload = parse_payload(params)
    query = first_text(payload, "query", "q", "keyword", "keywords", "text")
    url = first_text(payload, "url")
    if url and not query:
        query = query_from_url(url)
    if not query and not url:
        return fail('Kein Query. Beispiel: ebay_de.search({"query":"Ryzen 5 3600","limit":50})')

    limit = cfg_int(payload.get("limit", config.get("max_results", 50)), 50, 1, 200)
    source = ""
    raw = {}
    listings = []
    errors = []

    prefer_api = cfg_bool(payload.get("prefer_api", config.get("prefer_api")), True)
    if prefer_api:
        ok_api, api_data, err = browse_search(query, payload, config, limit)
        if ok_api:
            source = "browse_api"
            raw = api_data
            listings = normalize_api_items(api_data)
        elif err:
            errors.append(err)

    if not listings and cfg_bool(config.get("allow_public_html"), True):
        target_url = url or build_search_url(query, payload, limit)
        ok_html, body, err = fetch_public_html(target_url, config)
        if ok_html:
            source = "public_html"
            listings = parse_search_html(body)
            raw = {"url": target_url}
        elif err:
            errors.append(err)

    if not listings:
        setup_missing = not has_api_credentials(config)
        lines = [
            "EBAY_DE_SEARCH_FAILED",
            f"query: {query}",
            "reason: Keine Listings erhalten.",
        ]
        if setup_missing:
            lines.append("setup_required: eBay Browse API Credentials fehlen im Modul oder in EBAY_CLIENT_ID/EBAY_CLIENT_SECRET/EBAY_ACCESS_TOKEN.")
        lines.extend(f"- {e}" for e in errors)
        lines.append("hint: Fuer stabile Ergebnisse eBay Developer Client ID/Secret oder access_token in den Modul-Settings setzen.")
        return fail("\n".join(lines))

    listings = dedupe_listings(listings)
    filter_report = build_filter_report(payload, config, raw)
    listings, dropped = apply_local_filters(listings, payload, filter_report)
    filter_report["dropped"] = dropped
    listings = listings[:limit]
    analysis = analyze_listings(listings)
    text = format_result(query, listings, analysis, source, raw, errors, analysis_first=analysis_first, filter_report=filter_report)
    return ok(limit_output(text, config))


def _item(params, config):
    payload = parse_payload(params)
    item_id = first_text(payload, "item_id", "id")
    url = first_text(payload, "url")
    if url and not item_id:
        item_id = item_id_from_url(url)
    if not item_id and not url:
        return fail("item_id oder url fehlt.")

    token = get_access_token(config)
    if token and item_id:
        req_url = f"{API_BASE}/item/{urllib.parse.quote(item_id)}"
        ok_api, data, err = api_get(req_url, config, token)
        if ok_api:
            item = normalize_api_item(data)
            return ok(format_item(item, source="browse_api"))
        if err:
            return fail(f"EBAY_DE_ITEM_FAILED\n{err}")

    if url and cfg_bool(config.get("allow_public_html"), True):
        ok_html, body, err = fetch_public_html(url, config)
        if ok_html:
            return ok("EBAY_DE_ITEM_HTML\n" + summarize_item_html(body, url))
        return fail(f"EBAY_DE_ITEM_FAILED\n{err}")
    return fail("Kein Browse API Token vorhanden und keine URL fuer HTML-Fallback.")


def _parse_html_tool(params, config):
    payload = parse_payload(params, "html")
    body = first_text(payload, "html", "body", "content")
    query = first_text(payload, "query", "q") or "(pasted html)"
    if not body:
        return fail("html fehlt.")
    listings = parse_search_html(body)
    if not listings:
        return fail("Keine eBay-Listings im HTML erkannt.")
    filter_report = build_filter_report(payload, config, {})
    listings, dropped = apply_local_filters(listings, payload, filter_report)
    filter_report["dropped"] = dropped
    if not listings:
        return fail("Keine eBay-Listings nach Filterung uebrig.")
    analysis = analyze_listings(listings)
    return ok(limit_output(format_result(query, listings, analysis, "pasted_html", {}, [], analysis_first=True, filter_report=filter_report), config))


def browse_search(query, payload, config, limit):
    token = get_access_token(config)
    if not token:
        return False, None, "Browse API nicht genutzt: access_token oder client_id/client_secret fehlen."

    params = {
        "q": query,
        "limit": str(min(limit, 200)),
        "offset": str(cfg_int(payload.get("offset", 0), 0, 0, 10000)),
    }
    sort = browse_sort(payload.get("sort"))
    if sort:
        params["sort"] = sort
    filters = build_api_filters(payload, config)
    if filters:
        params["filter"] = ",".join(filters)
    if payload.get("category_ids"):
        params["category_ids"] = str(payload.get("category_ids"))
    if payload.get("fieldgroups"):
        params["fieldgroups"] = str(payload.get("fieldgroups"))

    url = API_BASE + "/item_summary/search?" + urllib.parse.urlencode(params)
    ok, data, err = api_get(url, config, token)
    if ok and isinstance(data, dict):
        data["_request"] = {"url": url, "filters": filters, "sort": params.get("sort", "")}
    return ok, data, err


def api_get(url, config, token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "X-EBAY-C-MARKETPLACE-ID": str(config.get("marketplace_id") or "EBAY_DE"),
    }
    endctx = end_user_context(config)
    if endctx:
        headers["X-EBAY-C-ENDUSERCTX"] = endctx
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=cfg_int(config.get("request_timeout_s"), 20, 5, 60)) as resp:
            return True, json.loads(resp.read().decode("utf-8", errors="replace")), ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, None, f"Browse API HTTP {exc.code}: {truncate(body, 1000)}"
    except Exception as exc:
        return False, None, f"Browse API Fehler: {exc}"


def get_access_token(config):
    token = str(config.get("access_token") or os.environ.get("EBAY_ACCESS_TOKEN") or "").strip()
    if token:
        return token
    if TOKEN_CACHE["token"] and TOKEN_CACHE["expires_at"] > time.time() + 60:
        return TOKEN_CACHE["token"]

    client_id = str(config.get("client_id") or os.environ.get("EBAY_CLIENT_ID") or "").strip()
    client_secret = str(config.get("client_secret") or os.environ.get("EBAY_CLIENT_SECRET") or "").strip()
    if not client_id or not client_secret:
        return ""

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": OAUTH_SCOPE}).encode("utf-8")
    req = urllib.request.Request(
        OAUTH_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        token = data.get("access_token", "")
        if token:
            TOKEN_CACHE["token"] = token
            TOKEN_CACHE["expires_at"] = time.time() + int(data.get("expires_in", 7200))
        return token
    except Exception:
        return ""


def has_api_credentials(config):
    access_token = str(config.get("access_token") or os.environ.get("EBAY_ACCESS_TOKEN") or "").strip()
    if access_token:
        return True
    client_id = str(config.get("client_id") or os.environ.get("EBAY_CLIENT_ID") or "").strip()
    client_secret = str(config.get("client_secret") or os.environ.get("EBAY_CLIENT_SECRET") or "").strip()
    return bool(client_id and client_secret)


def fetch_public_html(url, config):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=cfg_int(config.get("request_timeout_s"), 20, 5, 60)) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            if looks_blocked(body):
                return False, "", "Public HTML wurde von eBay blockiert/Captcha/Access Denied."
            return True, body, ""
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        if exc.code in {401, 403, 429}:
            return False, "", f"Public HTML HTTP {exc.code}: eBay blockiert den Abruf. Nutze Browse API Credentials."
        return False, "", f"Public HTML HTTP {exc.code}: {truncate(body, 500)}"
    except Exception as exc:
        return False, "", f"Public HTML Fehler: {exc}"


def parse_search_html(body):
    chunks = re.split(r"<li\b", body)
    items = []
    for chunk in chunks:
        if "s-item" not in chunk or "s-item__title" not in chunk:
            continue
        html_chunk = "<li" + chunk
        title = extract_class_text(html_chunk, "s-item__title")
        if not title or title.lower() in {"shop on ebay", "auf ebay kaufen"}:
            continue
        price_text = extract_class_text(html_chunk, "s-item__price")
        url = extract_link(html_chunk)
        if not url:
            continue
        shipping = extract_class_text(html_chunk, "s-item__shipping")
        condition = extract_class_text(html_chunk, "SECONDARY_INFO") or extract_class_text(html_chunk, "s-item__subtitle")
        seller = extract_class_text(html_chunk, "s-item__seller-info-text")
        location = extract_class_text(html_chunk, "s-item__location")
        bids = extract_class_text(html_chunk, "s-item__bids")
        time_left = extract_class_text(html_chunk, "s-item__time-left")
        price = parse_eur(price_text)
        shipping_price = parse_shipping(shipping)
        buying_options = "AUCTION" if bids or time_left else "FIXED_PRICE"
        items.append(
            {
                "title": clean_text(title),
                "price_text": clean_text(price_text),
                "price_eur": price,
                "shipping_text": clean_text(shipping),
                "shipping_eur": shipping_price,
                "free_shipping": shipping_price == 0 if shipping_price is not None else None,
                "total_eur": round(price + shipping_price, 2) if price is not None and shipping_price is not None else price,
                "condition": clean_text(condition),
                "seller": clean_text(seller),
                "location": clean_text(location),
                "bids": clean_text(bids),
                "time_left": clean_text(time_left),
                "url": clean_url(url),
                "item_id": item_id_from_url(url),
                "buying_options": buying_options,
                "source": "public_html",
            }
        )
    return items


def normalize_api_items(data):
    return [normalize_api_item(item) for item in data.get("itemSummaries", [])]


def normalize_api_item(item):
    price = money_value(item.get("price"))
    shipping = shipping_from_api(item)
    total = round(price + shipping, 2) if price is not None and shipping is not None else price
    seller = item.get("seller") or {}
    location = item.get("itemLocation") or {}
    seller_type = ""
    if isinstance(seller, dict):
        seller_type = seller.get("sellerAccountType", "") or seller.get("sellerType", "")
    return {
        "title": item.get("title", ""),
        "price_text": money_text(item.get("price")),
        "price_eur": price,
        "shipping_text": shipping_text_from_api(item),
        "shipping_eur": shipping,
        "free_shipping": shipping == 0 if shipping is not None else None,
        "total_eur": total,
        "condition": item.get("condition", ""),
        "condition_id": item.get("conditionId", ""),
        "seller": seller.get("username", "") if isinstance(seller, dict) else "",
        "seller_feedback": seller.get("feedbackPercentage", "") if isinstance(seller, dict) else "",
        "seller_account_type": seller_type,
        "location": ", ".join(str(location.get(k, "")) for k in ("city", "country") if location.get(k)),
        "item_location_country": location.get("country", "") if isinstance(location, dict) else "",
        "url": item.get("itemWebUrl") or item.get("itemAffiliateWebUrl") or "",
        "item_id": item.get("itemId", ""),
        "buying_options": ",".join(item.get("buyingOptions") or []),
        "bid_count": item.get("bidCount", ""),
        "source": "browse_api",
    }


def build_filter_report(payload, config, raw):
    request = raw.get("_request", {}) if isinstance(raw, dict) else {}
    api_filters = request.get("filters") or build_api_filters(payload, config)
    local_filters = []
    for label, keys in (
        ("buying_option", ("buying_option", "buyingOptions", "buying", "kaufart")),
        ("condition", ("condition", "conditions", "condition_ids", "condition_id", "zustand")),
        ("seller_type", ("seller_type", "sellerAccountTypes", "seller_account_type", "seller", "verkaeufer", "verkaufer")),
        ("shipping", ("shipping", "versand", "delivery_cost", "free_shipping", "shipping_max", "max_shipping", "max_delivery_cost")),
        ("location_country", ("location_country", "item_location_country", "standort_land", "standort")),
        ("include_terms", ("include_terms", "must_include", "include")),
        ("exclude_terms", ("exclude_terms", "exclude", "not_terms")),
    ):
        value = select_value(payload, *keys)
        if value not in (None, ""):
            local_filters.append(f"{label}={format_filter_value(value)}")
    return {"api_filters": api_filters, "local_filters": local_filters, "request_url": request.get("url", "")}


def apply_local_filters(listings, payload, report):
    include_terms = normalize_terms(select_value(payload, "include_terms", "must_include", "include"))
    exclude_terms = normalize_terms(select_value(payload, "exclude_terms", "exclude", "not_terms"))
    buying = normalize_buying_options(select_value(payload, "buying_option", "buyingOptions", "buying", "kaufart"))
    condition_ids = normalize_condition_ids(select_value(payload, "condition", "conditions", "condition_ids", "condition_id", "zustand"))
    seller_types = normalize_seller_types(select_value(payload, "seller_type", "sellerAccountTypes", "seller_account_type", "seller", "verkaeufer", "verkaufer"))
    location_country = normalize_country(select_value(payload, "location_country", "item_location_country", "standort_land", "standort"))
    ship_mode = shipping_mode(payload)
    shipping_max = parse_optional_float(select_value(payload, "shipping_max", "max_shipping", "max_delivery_cost", "versand_max"))
    if ship_mode == "free":
        shipping_max = 0.0

    kept = []
    dropped = {}
    for item in listings:
        reason = ""
        searchable = searchable_text(item)
        if include_terms and not all(term in searchable for term in include_terms):
            reason = "include_terms"
        elif exclude_terms and any(term in searchable for term in exclude_terms):
            reason = "exclude_terms"
        elif buying and not buying_matches(item, buying):
            reason = "buying_option"
        elif condition_ids and not condition_matches(item, condition_ids):
            reason = "condition"
        elif seller_types and not seller_type_matches(item, seller_types):
            reason = "seller_type"
        elif not shipping_matches(item, ship_mode, shipping_max, payload):
            reason = "shipping"
        elif location_country and not location_matches(item, location_country):
            reason = "location_country"

        if reason:
            dropped[reason] = dropped.get(reason, 0) + 1
        else:
            kept.append(item)

    return kept, dropped


def searchable_text(item):
    parts = [
        item.get("title", ""),
        item.get("condition", ""),
        item.get("seller", ""),
        item.get("location", ""),
        item.get("shipping_text", ""),
    ]
    return normalize_alias(" ".join(str(x) for x in parts if x))


def buying_matches(item, wanted):
    existing = normalize_buying_options(item.get("buying_options"))
    if not existing:
        return True
    return bool(set(existing) & set(wanted))


def condition_matches(item, wanted):
    condition_id = str(item.get("condition_id") or "").strip()
    condition_text = normalize_alias(item.get("condition"))
    if condition_id and condition_id in wanted:
        return True
    if "NEW" in wanted and any(token in condition_text for token in ("neu", "new", "unbenutzt", "brand_new")):
        return True
    if "USED" in wanted and any(token in condition_text for token in ("gebraucht", "used", "pre_owned", "sehr_gut", "gut", "akzeptabel")):
        return True
    if any(x in {"2000", "2500"} for x in wanted) and any(token in condition_text for token in ("refurb", "generaluberholt", "generalueberholt")):
        return True
    if "7000" in wanted and any(token in condition_text for token in ("defekt", "ersatzteil", "parts")):
        return True
    if not condition_id and not condition_text:
        return True
    return False


def seller_type_matches(item, wanted):
    seller_type = normalize_seller_types(item.get("seller_account_type"))
    if not seller_type:
        return True
    return bool(set(seller_type) & set(wanted))


def shipping_matches(item, ship_mode, shipping_max, payload):
    if not ship_mode and shipping_max is None:
        return True
    shipping = item.get("shipping_eur")
    text = normalize_alias(item.get("shipping_text"))
    strict = cfg_bool(payload.get("strict_shipping"), True)
    if ship_mode == "free":
        if shipping == 0:
            return True
        if shipping is None and not strict:
            return True
        return False
    if ship_mode == "paid":
        if isinstance(shipping, (int, float)) and shipping > 0:
            return True
        if shipping is None and not strict:
            return True
        return False
    if ship_mode == "pickup":
        if any(token in text for token in ("abholung", "pickup", "selbstabholung")):
            return True
        return not strict and not text
    if shipping_max is not None:
        if isinstance(shipping, (int, float)):
            return float(shipping) <= shipping_max
        return not strict
    return True


def location_matches(item, country):
    item_country = normalize_country(item.get("item_location_country"))
    location_text = normalize_alias(item.get("location"))
    if item_country:
        return item_country == country
    if not location_text:
        return True
    country_aliases = {
        "DE": ("de", "deutschland", "germany"),
        "AT": ("at", "osterreich", "oesterreich", "austria"),
        "CH": ("ch", "schweiz", "switzerland"),
    }
    return any(token in location_text for token in country_aliases.get(country, (country.lower(),)))


def market_snapshot(listings):
    snapshot = {
        "condition": {},
        "seller_type": {},
        "shipping": {},
        "buying": {},
        "location": {},
    }
    for item in listings:
        count_bucket(snapshot["condition"], condition_bucket(item))
        count_bucket(snapshot["seller_type"], seller_type_bucket(item))
        count_bucket(snapshot["shipping"], shipping_bucket(item))
        for buying in normalize_buying_options(item.get("buying_options")) or ["unknown"]:
            count_bucket(snapshot["buying"], buying)
        loc = normalize_country(item.get("item_location_country")) or clean_text(item.get("location")) or "unknown"
        count_bucket(snapshot["location"], loc)
    return snapshot


def condition_bucket(item):
    cid = str(item.get("condition_id") or "")
    text = normalize_alias(item.get("condition"))
    if cid.startswith("1") or any(x in text for x in ("neu", "new", "unbenutzt", "brand_new")):
        return "new"
    if cid in {"2000", "2500"} or any(x in text for x in ("refurb", "generaluberholt", "generalueberholt")):
        return "refurbished"
    if cid == "7000" or any(x in text for x in ("defekt", "ersatzteil", "parts")):
        return "parts"
    if cid or any(x in text for x in ("gebraucht", "used", "pre_owned", "sehr_gut", "gut", "akzeptabel")):
        return "used"
    return "unknown"


def seller_type_bucket(item):
    seller_type = normalize_seller_types(item.get("seller_account_type"))
    return seller_type[0].lower() if seller_type else "unknown"


def shipping_bucket(item):
    shipping = item.get("shipping_eur")
    if shipping == 0:
        return "free"
    if isinstance(shipping, (int, float)):
        return "paid"
    return "unknown"


def count_bucket(target, key):
    target[key] = target.get(key, 0) + 1


def analyze_listings(listings):
    priced = [x for x in listings if isinstance(x.get("total_eur"), (int, float))]
    prices = sorted(float(x["total_eur"]) for x in priced)
    analysis = {"count": len(listings), "priced_count": len(priced)}
    if not prices:
        return analysis
    q1 = percentile(prices, 25)
    q3 = percentile(prices, 75)
    iqr = q3 - q1
    low_fence = q1 - 1.5 * iqr
    high_fence = q3 + 1.5 * iqr
    median = statistics.median(prices)
    mean = statistics.fmean(prices)
    analysis.update(
        {
            "min": min(prices),
            "max": max(prices),
            "mean": mean,
            "median": median,
            "q1": q1,
            "q3": q3,
            "iqr": iqr,
            "low_fence": low_fence,
            "high_fence": high_fence,
            "recommended_max": round(median * 0.95, 2),
            "fair_band_low": round(q1, 2),
            "fair_band_high": round(q3, 2),
        }
    )
    deals = []
    outliers = []
    for item in priced:
        price = float(item["total_eur"])
        if price <= median * 0.9:
            deals.append(item)
        if price < low_fence or price > high_fence:
            outliers.append(item)
    deals.sort(key=lambda x: float(x["total_eur"]))
    outliers.sort(key=lambda x: float(x["total_eur"]))
    analysis["deals"] = deals[:8]
    analysis["outliers"] = outliers[:8]
    return analysis


def format_result(query, listings, analysis, source, raw, errors, analysis_first=False, filter_report=None):
    blocks = []
    summary = [
        "EBAY_DE_ANALYSIS" if analysis_first else "EBAY_DE_SEARCH",
        f"query: {query}",
        f"source: {source}",
        f"results: {len(listings)}",
        f"priced_results: {analysis.get('priced_count', 0)}",
    ]
    if raw.get("url"):
        summary.append(f"url: {raw['url']}")
    if errors:
        summary.append("fallback_notes:")
        summary.extend(f"- {e}" for e in errors)
    blocks.append("\n".join(summary))

    blocks.append(format_filter_report(filter_report or {}))
    blocks.append(format_market_snapshot(listings))
    blocks.append(format_analysis(analysis))
    blocks.append(format_deals(analysis))
    blocks.append(format_listings(listings))
    return "\n\n".join(blocks)


def format_filter_report(report):
    lines = ["FILTER_REPORT"]
    api_filters = report.get("api_filters") or []
    local_filters = report.get("local_filters") or []
    dropped = report.get("dropped") or {}
    if api_filters:
        lines.append("api_filters: " + ", ".join(api_filters))
    else:
        lines.append("api_filters: none")
    if local_filters:
        lines.append("local_filters: " + ", ".join(dict.fromkeys(local_filters)))
    else:
        lines.append("local_filters: none")
    if dropped:
        lines.append("dropped_by_local_filter: " + ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())))
    return "\n".join(lines)


def format_market_snapshot(listings):
    snap = market_snapshot(listings)
    lines = ["MARKET_SNAPSHOT"]
    lines.append("condition: " + format_counts(snap["condition"]))
    lines.append("seller_type: " + format_counts(snap["seller_type"]))
    lines.append("shipping: " + format_counts(snap["shipping"]))
    lines.append("buying: " + format_counts(snap["buying"]))
    lines.append("location: " + format_counts(snap["location"], limit=6))
    return "\n".join(lines)


def format_analysis(analysis):
    lines = ["PRICE_ANALYSIS"]
    if not analysis.get("priced_count"):
        lines.append("Keine auswertbaren Preise.")
        return "\n".join(lines)
    for key in ("min", "q1", "median", "mean", "q3", "max", "fair_band_low", "fair_band_high", "recommended_max"):
        lines.append(f"{key}: {eur(analysis[key])}")
    return "\n".join(lines)


def format_deals(analysis):
    lines = ["DEAL_CANDIDATES"]
    deals = analysis.get("deals") or []
    if not deals:
        lines.append("Keine klaren Unter-Median-Kandidaten.")
    for idx, item in enumerate(deals, 1):
        lines.append(f"{idx}. total={eur(item.get('total_eur'))} price={eur(item.get('price_eur'))} shipping={eur(item.get('shipping_eur'))} | {item.get('title')}")
        lines.append(f"   condition={item.get('condition','')} seller={item.get('seller','')} seller_type={item.get('seller_account_type','')} location={item.get('location','')} item_id={item.get('item_id','')}")
        lines.append(f"   url={item.get('url','')}")
    outliers = analysis.get("outliers") or []
    if outliers:
        lines.append("")
        lines.append("OUTLIERS")
        for item in outliers[:5]:
            lines.append(f"- total={eur(item.get('total_eur'))} | {item.get('title')}")
    return "\n".join(lines)


def format_listings(listings):
    lines = ["LISTINGS"]
    for idx, item in enumerate(listings, 1):
        lines.append(f"{idx}. total={eur(item.get('total_eur'))} price={eur(item.get('price_eur'))} shipping={eur(item.get('shipping_eur'))} | {item.get('title','')}")
        meta = []
        for key in ("condition", "condition_id", "buying_options", "seller", "seller_feedback", "seller_account_type", "location", "item_location_country", "free_shipping", "bids", "bid_count", "time_left"):
            if key in item and item.get(key) not in (None, ""):
                meta.append(f"{key}={item[key]}")
        if meta:
            lines.append("   " + " | ".join(meta))
        lines.append(f"   item_id={item.get('item_id','')} url={item.get('url','')}")
    return "\n".join(lines)


def format_item(item, source):
    lines = [f"EBAY_DE_ITEM\nsource: {source}"]
    for key in ("item_id", "title", "price_text", "shipping_text", "total_eur", "condition", "condition_id", "seller", "seller_feedback", "seller_account_type", "location", "item_location_country", "buying_options", "url"):
        if item.get(key) not in (None, ""):
            val = eur(item[key]) if key == "total_eur" else item[key]
            lines.append(f"{key}: {val}")
    return "\n".join(lines)


def filter_help():
    return "\n".join(
        [
            "EBAY_DE_FILTERS",
            "buying_option: sofortkauf|fixed|auction|auktion|best_offer|preisvorschlag",
            "condition: neu|new|gebraucht|used|refurbished|defekt oder konkrete conditionIds",
            "seller_type: haendler|business|privat|private",
            "shipping: free|kostenlos|paid|mit_versandkosten|pickup plus shipping_max",
            "location: location_country/item_location_country=DE, delivery_country=DE, postal_code/plz=12345",
            "price: min_price/max_price, sort=price_asc|price_desc|new|ending",
            "quality: include_terms=[...], exclude_terms=[defekt,bundle,...], strict_shipping=true|false",
            'example: {"query":"Ryzen 5 3600","limit":80,"buying_option":"sofortkauf","condition":"gebraucht","seller_type":"privat","shipping":"free","location_country":"DE","sort":"price_asc"}',
        ]
    )


def build_search_url(query, payload, limit):
    params = {
        "_nkw": query,
        "_sacat": str(payload.get("category", payload.get("sacat", 0))),
        "_from": "R40",
        "_ipg": str(min(limit, 240)),
    }
    page = payload.get("page") or payload.get("_pgn")
    if page:
        params["_pgn"] = str(page)
    sop = html_sort(payload.get("sort"))
    if sop:
        params["_sop"] = sop
    buying = normalize_buying_options(select_value(payload, "buying_option", "buyingOptions", "buying", "kaufart"))
    if "FIXED_PRICE" in buying:
        params["LH_BIN"] = "1"
    if "AUCTION" in buying:
        params["LH_Auction"] = "1"
    condition_ids = normalize_condition_ids(select_value(payload, "condition", "conditions", "condition_ids", "condition_id", "zustand"))
    if len(condition_ids) == 1 and condition_ids[0].isdigit():
        params["LH_ItemCondition"] = condition_ids[0]
    if shipping_mode(payload) == "free":
        params["LH_FS"] = "1"
    min_price = select_value(payload, "min_price", "price_min", "_udlo")
    max_price = select_value(payload, "max_price", "price_max", "_udhi")
    if min_price not in (None, ""):
        params["_udlo"] = str(min_price)
    if max_price not in (None, ""):
        params["_udhi"] = str(max_price)
    return "https://www.ebay.de/sch/i.html?" + urllib.parse.urlencode(params)


def build_api_filters(payload, config=None):
    config = config or {}
    filters = []
    buying = normalize_buying_options(select_value(payload, "buying_option", "buyingOptions", "buying", "kaufart"))
    if buying:
        filters.append(f"buyingOptions:{{{'|'.join(buying)}}}")
    condition_ids = normalize_condition_ids(select_value(payload, "condition", "conditions", "condition_ids", "condition_id", "zustand"))
    broad_conditions = [x for x in condition_ids if x in {"NEW", "USED"}]
    specific_conditions = [x for x in condition_ids if x not in {"NEW", "USED"}]
    if broad_conditions:
        filters.append(f"conditions:{{{'|'.join(broad_conditions)}}}")
    if specific_conditions:
        filters.append(f"conditionIds:{{{'|'.join(specific_conditions)}}}")
    seller_types = normalize_seller_types(select_value(payload, "seller_type", "sellerAccountTypes", "seller_account_type", "seller", "verkaeufer", "verkaufer"))
    if seller_types:
        filters.append(f"sellerAccountTypes:{{{'|'.join(seller_types)}}}")
    location_country = normalize_country(select_value(payload, "location_country", "item_location_country", "standort_land", "standort") or config.get("default_location_country"))
    if location_country:
        filters.append(f"itemLocationCountry:{location_country}")
    delivery_country = normalize_country(select_value(payload, "delivery_country", "ship_to_country", "versandland") or config.get("default_delivery_country") or config.get("country") or "DE")
    delivery_postal = select_value(payload, "delivery_postal_code", "postal_code", "zip", "plz") or config.get("zip")
    ship_mode = shipping_mode(payload)
    shipping_max = select_value(payload, "shipping_max", "max_shipping", "max_delivery_cost", "versand_max")
    if ship_mode == "free":
        shipping_max = 0
    if shipping_max not in (None, ""):
        filters.append(f"maxDeliveryCost:{shipping_max}")
        if delivery_country:
            filters.append(f"deliveryCountry:{delivery_country}")
        if delivery_postal:
            filters.append(f"deliveryPostalCode:{delivery_postal}")
    elif delivery_country and payload_has_any(payload, "delivery_country", "ship_to_country", "versandland"):
        filters.append(f"deliveryCountry:{delivery_country}")
        if delivery_postal:
            filters.append(f"deliveryPostalCode:{delivery_postal}")
    min_price = select_value(payload, "min_price", "price_min", "_udlo")
    max_price = select_value(payload, "max_price", "price_max", "_udhi")
    if min_price is not None or max_price is not None:
        lo = "" if min_price is None else str(min_price)
        hi = "" if max_price is None else str(max_price)
        filters.append(f"price:[{lo}..{hi}]")
        filters.append("priceCurrency:EUR")
    pickup_zip = select_value(payload, "pickup_zip", "pickup_postal_code", "pickup_plz")
    pickup_country = normalize_country(select_value(payload, "pickup_country") or config.get("country") or "DE")
    radius = select_value(payload, "pickup_radius_km", "radius_km", "distance_km")
    if pickup_zip and radius:
        filters.append(f"pickupCountry:{pickup_country}")
        filters.append(f"pickupPostalCode:{pickup_zip}")
        filters.append(f"pickupRadius:{radius}")
        filters.append("pickupRadiusUnit:KILOMETERS")
    if payload.get("filter"):
        filters.append(str(payload.get("filter")))
    return filters


def browse_sort(value):
    value = str(value or "").strip().lower()
    return {
        "price_asc": "price",
        "price_desc": "-price",
        "new": "newlyListed",
        "newly": "newlyListed",
        "ending": "endingSoonest",
    }.get(value, value if value in {"price", "-price", "newlyListed", "endingSoonest"} else "")


def html_sort(value):
    value = str(value or "").strip().lower()
    return {"price_asc": "15", "price_desc": "16", "new": "10", "ending": "1"}.get(value, "")


def end_user_context(config):
    country = str(config.get("country") or "").strip()
    zip_code = str(config.get("zip") or "").strip()
    if country and zip_code:
        return "contextualLocation=" + urllib.parse.quote(f"country={country},zip={zip_code}", safe="")
    if country:
        return "contextualLocation=" + urllib.parse.quote(f"country={country}", safe="")
    return ""


def extract_class_text(chunk, class_name):
    pattern = re.compile(r'class="[^"]*' + re.escape(class_name) + r'[^"]*"[^>]*>(.*?)</[^>]+>', re.I | re.S)
    match = pattern.search(chunk)
    if not match:
        return ""
    return strip_tags(match.group(1))


def extract_link(chunk):
    match = re.search(r'<a\b[^>]*class="[^"]*s-item__link[^"]*"[^>]*href="([^"]+)"', chunk, re.I | re.S)
    if not match:
        match = re.search(r'href="([^"]*?/itm/[^"]+)"', chunk, re.I)
    return html.unescape(match.group(1)) if match else ""


def strip_tags(text):
    text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def clean_text(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_url(url):
    url = html.unescape(url or "")
    return url.split("?hash=", 1)[0] if "?hash=" in url else url


def parse_eur(text):
    text = clean_text(text)
    if not text:
        return None
    nums = re.findall(r"(\d{1,3}(?:\.\d{3})*,\d{2}|\d+[,.]\d{2}|\d+)", text)
    if not nums:
        return None
    return german_float(nums[0])


def parse_shipping(text):
    text = clean_text(text).lower()
    if not text:
        return None
    if "kostenlos" in text or "free" in text:
        return 0.0
    value = parse_eur(text)
    return value


def german_float(text):
    s = str(text).strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    return float(s)


def money_value(obj):
    if not isinstance(obj, dict):
        return None
    try:
        return float(obj.get("value"))
    except Exception:
        return None


def money_text(obj):
    if not isinstance(obj, dict):
        return ""
    return f"{obj.get('value', '')} {obj.get('currency', '')}".strip()


def shipping_from_api(item):
    shipping = item.get("shippingOptions") or []
    values = []
    for opt in shipping:
        cost = opt.get("shippingCost") if isinstance(opt, dict) else None
        val = money_value(cost)
        if val is not None:
            values.append(val)
    return min(values) if values else None


def shipping_text_from_api(item):
    val = shipping_from_api(item)
    if val is None:
        return ""
    return "Kostenloser Versand" if val == 0 else f"{val:.2f} EUR Versand"


def percentile(values, p):
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    k = (len(values) - 1) * (p / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return values[int(k)]
    return values[f] * (c - k) + values[c] * (k - f)


def eur(value):
    if value is None or value == "":
        return "n/a"
    try:
        return f"{float(value):.2f} EUR"
    except Exception:
        return str(value)


def query_from_url(url):
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    return (qs.get("_nkw") or qs.get("q") or [""])[0].replace("+", " ")


def item_id_from_url(url):
    match = re.search(r"/itm/(?:[^/?#]+/)?(\d{8,})", url or "")
    if match:
        return match.group(1)
    match = re.search(r"(?:itemId|itemid|itm)=([0-9]+)", url or "")
    return match.group(1) if match else ""


def looks_blocked(body):
    text = body[:5000].lower()
    return any(marker in text for marker in ["access denied", "captcha", "pardon our interruption", "robot", "akamai"])


def summarize_item_html(body, url):
    title = ""
    title_m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if title_m:
        title = clean_text(strip_tags(title_m.group(1)))
    price = parse_eur(body[:200000])
    return f"url: {url}\ntitle: {title}\nfirst_price_seen: {eur(price)}"


def dedupe_listings(items):
    out = []
    seen = set()
    for item in items:
        key = item.get("item_id") or item.get("url") or item.get("title")
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


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
    return {default_key: text}


def first_text(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def select_value(payload, *keys):
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def payload_has_any(payload, *keys):
    return any(key in payload and payload.get(key) not in (None, "") for key in keys)


def value_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        out = []
        for item in value:
            out.extend(value_list(item))
        return out
    text = clean_text(value)
    if not text:
        return []
    return [part for part in (clean_text(x) for x in re.split(r"[|,;]", text)) if part]


def normalize_alias(value):
    text = clean_text(value).lower()
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "é": "e",
        "è": "e",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def normalize_buying_options(value):
    out = []
    for part in value_list(value):
        upper = clean_text(part).upper()
        key = normalize_alias(part)
        if upper in {"FIXED_PRICE", "AUCTION", "BEST_OFFER", "CLASSIFIED_AD"}:
            out.append(upper)
        elif key in BUYING_OPTION_ALIASES:
            out.append(BUYING_OPTION_ALIASES[key])
    return unique(out)


def normalize_condition_ids(value):
    out = []
    for part in value_list(value):
        key = normalize_alias(part)
        upper = clean_text(part).upper()
        if re.fullmatch(r"\d{3,4}", key):
            out.append(key)
        elif upper in {"NEW", "USED"}:
            out.append(upper)
        elif key in CONDITION_ALIASES:
            out.extend(CONDITION_ALIASES[key])
    return unique(out)


def normalize_seller_types(value):
    out = []
    for part in value_list(value):
        upper = clean_text(part).upper()
        key = normalize_alias(part)
        if upper in {"BUSINESS", "INDIVIDUAL"}:
            out.append(upper)
        elif key in SELLER_TYPE_ALIASES:
            out.append(SELLER_TYPE_ALIASES[key])
    return unique(out)


def normalize_terms(value):
    return [normalize_alias(x) for x in value_list(value)]


def normalize_country(value):
    text = clean_text(value)
    if not text:
        return ""
    key = normalize_alias(text)
    mapping = {
        "de": "DE",
        "deutschland": "DE",
        "germany": "DE",
        "at": "AT",
        "oesterreich": "AT",
        "osterreich": "AT",
        "austria": "AT",
        "ch": "CH",
        "schweiz": "CH",
        "switzerland": "CH",
    }
    if key in mapping:
        return mapping[key]
    if re.fullmatch(r"[a-z]{2}", key):
        return key.upper()
    return text.upper()


def shipping_mode(payload):
    if cfg_bool(payload.get("free_shipping"), False):
        return "free"
    value = select_value(payload, "shipping", "versand", "delivery_cost", "versandkosten")
    key = normalize_alias(value)
    if key in {"free", "kostenlos", "gratis", "ohne_versand", "ohne_versandkosten", "versandkostenfrei"}:
        return "free"
    if key in {"paid", "kostenpflichtig", "mit_versandkosten", "versandkosten", "plus_versand"}:
        return "paid"
    if key in {"pickup", "abholung", "selbstabholung", "local_pickup"}:
        return "pickup"
    return ""


def parse_optional_float(value):
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "."))
    except Exception:
        return None


def unique(items):
    out = []
    seen = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def format_counts(counts, limit=10):
    if not counts:
        return "none"
    pairs = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return ", ".join(f"{key}={val}" for key, val in pairs)


def format_filter_value(value):
    if isinstance(value, (list, tuple, set)):
        return "|".join(clean_text(x) for x in value)
    return clean_text(value)


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


def cfg_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def truncate(text, limit):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def limit_output(text, config):
    return truncate(text, cfg_int(config.get("max_output_chars", 16000), 16000, 2000, 60000))


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
