"""Headless browser fetcher for pages that need a real browser DOM."""
import html
import ipaddress
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from html.parser import HTMLParser


MODULE = {
    "name": "browser",
    "description": "Laedt Webseiten mit headless Google Chrome und extrahiert Titel, Text, Datumshinweise und Links.",
    "version": "1.0",
    "settings": {
        "timeout_s": {"type": "number", "label": "Timeout Sekunden", "default": 25},
        "max_chars": {"type": "number", "label": "Max Textzeichen", "default": 12000},
        "allow_private_networks": {
            "type": "bool",
            "label": "Private/LAN URLs erlauben",
            "default": False,
        },
    },
    "tools": [
        {
            "name": "browser.fetch",
            "description": "Oeffnet eine HTTP(S)-URL headless, rendert die Seite und liefert lesbaren Text plus Quellenmetadaten.",
            "params": ["url"],
        }
    ],
}


class ReadableHtmlParser(HTMLParser):
    def __init__(self, base_url):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.title_parts = []
        self.text_parts = []
        self.links = []
        self.meta = {}
        self.date_candidates = []
        self._skip_stack = []
        self._in_title = False
        self._in_a = False
        self._a_href = ""
        self._a_text = []
        self._in_time = False

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attr = {k.lower(): v for k, v in attrs if k}
        if tag in {"script", "style", "svg", "canvas", "noscript"}:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            self._handle_meta(attr)
        elif tag == "time":
            self._in_time = True
            if attr.get("datetime"):
                self.date_candidates.append(attr["datetime"])
        elif tag == "a":
            href = (attr.get("href") or "").strip()
            if href:
                self._in_a = True
                self._a_href = href
                self._a_text = []
        elif tag in {"p", "div", "section", "article", "header", "footer", "li", "br", "h1", "h2", "h3"}:
            self.text_parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self._skip_stack:
            if self._skip_stack[-1] == tag:
                self._skip_stack.pop()
            return
        if tag == "title":
            self._in_title = False
        elif tag == "time":
            self._in_time = False
        elif tag == "a" and self._in_a:
            url = urllib.parse.urljoin(self.base_url, self._a_href)
            label = _collapse_ws(" ".join(self._a_text))
            if _is_http_url(url) and label:
                self.links.append({"text": label[:140], "url": url})
            self._in_a = False
            self._a_href = ""
            self._a_text = []

    def handle_data(self, data):
        if self._skip_stack:
            return
        value = data.strip()
        if not value:
            return
        if self._in_title:
            self.title_parts.append(value)
        if self._in_a:
            self._a_text.append(value)
        if self._in_time:
            self.date_candidates.append(value)
        self.text_parts.append(value)

    def _handle_meta(self, attr):
        key = (
            attr.get("name")
            or attr.get("property")
            or attr.get("itemprop")
            or attr.get("http-equiv")
            or ""
        ).lower()
        content = (attr.get("content") or "").strip()
        if not key or not content:
            return
        if key in {
            "description",
            "og:description",
            "twitter:description",
            "author",
            "article:author",
            "publisher",
            "og:site_name",
        }:
            self.meta[key] = content
        if "date" in key or "published" in key or "modified" in key or key in {"article:published_time", "article:modified_time"}:
            self.meta[key] = content
            self.date_candidates.append(content)

    def readable(self):
        title = _collapse_ws(" ".join(self.title_parts))
        text = _normalize_text("\n".join(self.text_parts))
        dates = _unique(self.date_candidates + _extract_dates(text))[:12]
        links = []
        seen = set()
        for link in self.links:
            url = link["url"]
            if url in seen:
                continue
            seen.add(url)
            links.append(link)
            if len(links) >= 40:
                break
        return title, self.meta, dates, text, links


def handle_tool(tool_name, params, config):
    if tool_name != "browser.fetch":
        return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}

    url = _first_param(params, "url")
    if not url:
        return {"success": False, "data": "Keine URL angegeben."}

    timeout_s = int(config.get("timeout_s") or 25)
    max_chars = int(config.get("max_chars") or 12000)
    allow_private = bool(config.get("allow_private_networks") or False)

    try:
        normalized_url = _validate_url(url, allow_private)
        page_html, engine_note = _fetch_with_chrome(normalized_url, timeout_s)
        if not page_html.strip():
            page_html, engine_note = _fetch_with_urllib(normalized_url, timeout_s), "urllib fallback"
        return _format_page(normalized_url, page_html, max_chars, engine_note)
    except socket.gaierror as exc:
        return {
            "success": True,
            "data": (
                f"URL_UNAVAILABLE\n"
                f"URL: {url}\n"
                f"Reason: DNS lookup failed ({exc}).\n"
                "Hinweis: Die Quelle wurde versucht, ist aber aktuell nicht aufloesbar. "
                "Nicht als Beleg nutzen; mit anderen Quellen fortfahren."
            ),
        }
    except Exception as exc:
        return {"success": False, "data": f"Browser fetch fehlgeschlagen: {exc}"}


def _first_param(params, key):
    if isinstance(params, dict):
        return str(params.get(key) or params.get("0") or "").strip()
    if not params:
        return ""
    raw = str(params[0]).strip()
    m = re.match(rf"^\s*{re.escape(key)}\s*[:=]\s*(.+)$", raw, flags=re.I | re.S)
    return (m.group(1) if m else raw).strip().strip("\"'")


def _validate_url(url, allow_private):
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Nur http/https URLs sind erlaubt.")
    host = parsed.hostname
    if not host:
        raise ValueError("URL ohne Host.")
    if not allow_private:
        _reject_private_host(host)
    return urllib.parse.urlunparse(parsed)


def _reject_private_host(host):
    infos = socket.getaddrinfo(host, None)
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(f"Private oder lokale Adresse blockiert: {host} -> {ip}")


def _fetch_with_chrome(url, timeout_s):
    chrome = _chrome_path()
    if not chrome:
        raise RuntimeError("google-chrome/chromium nicht gefunden")
    with tempfile.TemporaryDirectory(prefix="agent-browser-") as user_data_dir:
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-extensions",
            "--hide-scrollbars",
            "--blink-settings=imagesEnabled=false",
            "--window-size=1365,900",
            f"--user-data-dir={user_data_dir}",
            "--virtual-time-budget=7000",
            "--dump-dom",
            url,
        ]
        env = os.environ.copy()
        env.setdefault("LANG", "C.UTF-8")
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
            env=env,
        )
        if proc.returncode != 0 and not proc.stdout.strip():
            err = proc.stderr.strip().splitlines()[-1:] or ["Chrome Fehler"]
            raise RuntimeError(err[0][:300])
        return proc.stdout, "headless chrome"


def _fetch_with_urllib(url, timeout_s):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 AgentBrowser/1.0"
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _chrome_path():
    for path in (
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.exists(path):
            return path
    return ""


def _format_page(url, page_html, max_chars, engine_note):
    parser = ReadableHtmlParser(url)
    parser.feed(page_html)
    title, meta, dates, text, links = parser.readable()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[Text gekuerzt]"

    lines = [
        f"URL: {url}",
        f"Engine: {engine_note}",
        f"Titel: {title or '(kein Titel gefunden)'}",
    ]
    for key in ("description", "og:description", "author", "article:author", "publisher", "og:site_name"):
        if meta.get(key):
            lines.append(f"{key}: {meta[key]}")
    if dates:
        lines.append("Datumshinweise: " + "; ".join(dates))
    lines.append("\nText:\n" + (text or "(kein lesbarer Text extrahiert)"))
    if links:
        lines.append("\nLinks:")
        for link in links[:25]:
            lines.append(f"- {link['text']} | {link['url']}")
    return {"success": True, "data": "\n".join(lines)}


def _is_http_url(url):
    return urllib.parse.urlparse(url).scheme in {"http", "https"}


def _collapse_ws(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()


def _normalize_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [_collapse_ws(line) for line in text.splitlines()]
    lines = [line for line in lines if line and len(line) > 1]
    return "\n".join(lines)


def _extract_dates(text):
    patterns = [
        r"\b(?:19|20)\d{2}[-/.](?:0?[1-9]|1[0-2])[-/.](?:0?[1-9]|[12]\d|3[01])\b",
        r"\b(?:0?[1-9]|[12]\d|3[01])[.](?:0?[1-9]|1[0-2])[.](?:19|20)\d{2}\b",
        r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, (?:19|20)\d{2}\b",
        r"\b\d{1,2}\. (?:Januar|Februar|Maerz|März|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember) (?:19|20)\d{2}\b",
    ]
    found = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text, flags=re.I))
    return found


def _unique(values):
    result = []
    seen = set()
    for value in values:
        value = _collapse_ws(str(value))
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
