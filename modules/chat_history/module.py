"""Chat-Historie Modul — context-sichere Suche in gespeicherten Chat-Conversations."""
import datetime
import json
import os
import re
import sqlite3
import sys


MODULE = {
    "name": "chat_history",
    "description": "Durchsucht gespeicherte Chats context-sicher: erst Conversations finden, dann relevante Ausschnitte laden.",
    "version": "1.0",
    "settings": {
        "max_context_tokens": {
            "type": "number",
            "label": "Max Kontext Tokens fuer Warnung",
            "default": 32000,
        },
        "max_return_tokens": {
            "type": "number",
            "label": "Max Rueckgabe Tokens",
            "default": 4500,
        },
        "max_results": {"type": "number", "label": "Max Treffer", "default": 8},
        "max_list": {"type": "number", "label": "Max Conversations in Liste", "default": 25},
        "snippet_chars": {"type": "number", "label": "Snippet Zeichen", "default": 900},
    },
    "tools": [
        {
            "name": "chat.historie_liste",
            "description": "Listet gespeicherte Chat-Conversations mit ID, Modul, Titel und Umfang. Optional Filtertext angeben.",
            "params": ["filter"],
        },
        {
            "name": "chat.historie_suche",
            "description": "Sucht in alten Chats und gibt nur relevante Snippets plus Referenzen zurueck. Nutze danach chat.historie_auszug fuer Details.",
            "params": ["query"],
        },
        {
            "name": "chat.historie_auszug",
            "description": "Laedt einen gezielten Ausschnitt aus einer Conversation-Referenz, z.B. KevinChat/abc123#m4, optional mit Suchfrage.",
            "params": ["convo_ref", "query"],
        },
    ],
}


def handle_tool(tool_name, params, config):
    try:
        if tool_name == "chat.historie_liste":
            return _list_conversations(_first(params, "filter"), config)
        if tool_name == "chat.historie_suche":
            return _search_conversations(_first(params, "query"), config)
        if tool_name == "chat.historie_auszug":
            return _extract_conversation(_first(params, "convo_ref"), _second(params, "query"), config)
        return {"success": False, "data": f"Unbekanntes Tool: {tool_name}"}
    except Exception as exc:
        return {"success": False, "data": f"Chat-Historie Fehler: {exc}"}


def _list_conversations(filter_text, config):
    rows = _load_conversations(config)
    needle = _norm(filter_text)
    if needle:
        rows = [c for c in rows if needle in _norm(_conversation_index_text(c))]

    max_list = _int_setting(config, "max_list", 25)
    rows = sorted(rows, key=lambda c: c["updated_ts"], reverse=True)
    total = len(rows)
    shown = rows[:max_list]

    lines = [
        "CHAT_HISTORY_LIST",
        f"available_conversations: {total}",
        f"returned: {len(shown)}",
    ]
    if needle and total > max_list:
        lines.append(
            f"CONTEXT_WARNING: Filter passt auf {total} Conversations; Liste wurde auf {max_list} gekuerzt. Nutze chat.historie_suche mit engerer Frage."
        )
    elif not needle and total > max_list:
        lines.append(
            f"CONTEXT_WARNING: Es gibt {total} Conversations; Liste wurde auf {max_list} gekuerzt. Nutze filter oder chat.historie_suche."
        )
    lines.append("")

    for idx, conv in enumerate(shown, 1):
        lines.append(_conversation_line(idx, conv))

    if not shown:
        lines.append("Keine gespeicherten Chats gefunden.")
    return {"success": True, "data": "\n".join(lines)}


def _search_conversations(query, config):
    query = (query or "").strip()
    if not query:
        return {"success": False, "data": "Kein Suchbegriff. Nutze z.B. chat.historie_suche(DeepSeek Tool-Limit)"}

    conversations = _load_conversations(config)
    terms = _terms(query)
    scored = []
    candidate_chars = 0
    for conv in conversations:
        for msg_idx, msg in enumerate(conv["messages"]):
            text = _message_text(msg)
            score = _score(query, terms, text, conv)
            if score <= 0:
                continue
            candidate_chars += len(text)
            scored.append((score, conv, msg_idx, text))

    scored.sort(key=lambda x: (x[0], x[1]["updated_ts"]), reverse=True)
    max_results = _int_setting(config, "max_results", 8)
    snippet_chars = _int_setting(config, "snippet_chars", 900)
    return_budget_chars = _tokens_to_chars(_int_setting(config, "max_return_tokens", 4500))
    context_budget_tokens = _int_setting(config, "max_context_tokens", 32000)
    context_budget_chars = _tokens_to_chars(context_budget_tokens)

    returned = []
    used_chars = 0
    for score, conv, msg_idx, text in scored:
        snippet = _best_snippet(text, terms, snippet_chars)
        block = _format_hit(len(returned) + 1, score, conv, msg_idx, snippet, text)
        block_chars = len(block)
        if returned and (used_chars + block_chars > return_budget_chars or len(returned) >= max_results):
            break
        returned.append(block)
        used_chars += block_chars

    lines = [
        "CHAT_HISTORY_SEARCH",
        f"query: {query}",
        f"searched_conversations: {len(conversations)}",
        f"matches: {len(scored)}",
        f"returned: {len(returned)}",
        f"estimated_candidate_tokens: {_estimate_tokens(candidate_chars)}",
        f"estimated_return_tokens: {_estimate_tokens(used_chars)}",
    ]
    if candidate_chars > context_budget_chars:
        lines.append(
            f"CONTEXT_WARNING: Trefferbasis waere ca. {_estimate_tokens(candidate_chars)} Tokens und ueberschreitet max_context_tokens={context_budget_tokens}. Ich gebe nur Snippets/Refs zurueck; nutze chat.historie_auszug(ref, query) fuer gezielte Details."
        )
    if len(scored) > len(returned):
        lines.append(
            f"CONTEXT_WARNING: {len(scored) - len(returned)} weitere Treffer wurden wegen max_results/max_return_tokens nicht ausgegeben."
        )
    lines.append("")

    if returned:
        lines.extend(returned)
    else:
        lines.append("Keine relevanten Chatstellen gefunden.")
    return {"success": True, "data": "\n\n".join(lines)}


def _extract_conversation(convo_ref, query, config):
    ref = (convo_ref or "").strip()
    if not ref:
        return {"success": False, "data": "Keine Conversation-Referenz. Beispiel: KevinChat/mow2uba01v3ts#m4"}

    conversations = _load_conversations(config)
    modul_id, convo_id, center_idx = _parse_ref(ref)
    matches = [
        c
        for c in conversations
        if (not modul_id or c["modul_id"] == modul_id) and c["convo_id"] == convo_id
    ]
    if not matches and not modul_id:
        matches = [c for c in conversations if c["convo_id"] == convo_id]
    if not matches:
        return {"success": False, "data": f"Conversation nicht gefunden: {convo_ref}"}

    conv = matches[0]
    terms = _terms(query or conv["title"])
    selected = _select_messages(conv, terms, center_idx)

    context_budget_tokens = _int_setting(config, "max_context_tokens", 32000)
    return_budget_chars = _tokens_to_chars(_int_setting(config, "max_return_tokens", 4500))
    full_chars = sum(len(_message_text(m)) for _, m in selected)
    out_blocks = []
    used_chars = 0
    for msg_idx, msg in selected:
        text = _message_text(msg)
        block = _format_message_extract(conv, msg_idx, msg, text)
        if out_blocks and used_chars + len(block) > return_budget_chars:
            break
        out_blocks.append(block)
        used_chars += len(block)

    lines = [
        "CHAT_HISTORY_EXTRACT",
        f"conversation: {conv['modul_id']}/{conv['convo_id']}",
        f"title: {conv['title']}",
        f"updated: {_format_ts(conv['updated_ts'])}",
        f"messages_total: {len(conv['messages'])}",
        f"messages_selected: {len(out_blocks)}",
        f"estimated_selected_tokens: {_estimate_tokens(full_chars)}",
    ]
    if _estimate_tokens(full_chars) > context_budget_tokens:
        lines.append(
            f"CONTEXT_WARNING: Auszug wuerde max_context_tokens={context_budget_tokens} ueberschreiten; Rueckgabe wurde auf relevante Messages gekuerzt."
        )
    if len(out_blocks) < len(selected):
        lines.append(
            "CONTEXT_WARNING: Weitere relevante Messages wurden wegen max_return_tokens nicht ausgegeben. Query enger stellen oder konkrete #mN Referenz nutzen."
        )
    lines.append("")
    lines.extend(out_blocks or ["Keine passenden Messages im Chat gefunden."])
    return {"success": True, "data": "\n\n".join(lines)}


def _load_conversations(config):
    db_path = _db_path(config)
    if not os.path.exists(db_path):
        return []
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT modul_id, convo_id, data_json, updated_ts FROM conversations ORDER BY updated_ts DESC"
        ).fetchall()
    finally:
        con.close()

    conversations = []
    for modul_id, convo_id, data_json, updated_ts in rows:
        try:
            data = json.loads(data_json)
        except Exception:
            continue
        messages = data.get("messages") if isinstance(data, dict) else []
        if not isinstance(messages, list):
            messages = []
        conversations.append(
            {
                "modul_id": str(modul_id),
                "convo_id": str(convo_id),
                "title": str(data.get("title") or "(ohne Titel)") if isinstance(data, dict) else "(ohne Titel)",
                "updated_ts": int(updated_ts or 0),
                "updated": str(data.get("updated") or "") if isinstance(data, dict) else "",
                "messages": messages,
            }
        )
    return conversations


def _db_path(config):
    data_dir = str(config.get("data_dir") or config.get("home_dir") or "agent-data")
    if os.path.basename(data_dir) != "agent-data" and os.path.exists(os.path.join(data_dir, "agent-data")):
        data_dir = os.path.join(data_dir, "agent-data")
    return os.path.join(data_dir, "tasks.db")


def _conversation_index_text(conv):
    parts = [conv["modul_id"], conv["convo_id"], conv["title"]]
    for msg in conv["messages"][:20]:
        parts.append(_message_text(msg)[:500])
    return "\n".join(parts)


def _conversation_line(idx, conv):
    chars = sum(len(_message_text(m)) for m in conv["messages"])
    return (
        f"[{idx}] ref: {conv['modul_id']}/{conv['convo_id']}\n"
        f"    updated: {_format_ts(conv['updated_ts'])}\n"
        f"    title: {conv['title']}\n"
        f"    messages: {len(conv['messages'])}, estimated_tokens: {_estimate_tokens(chars)}"
    )


def _format_hit(idx, score, conv, msg_idx, snippet, full_text):
    role = str(conv["messages"][msg_idx].get("role") or "?")
    return (
        f"[{idx}] ref: {conv['modul_id']}/{conv['convo_id']}#m{msg_idx}\n"
        f"    score: {score:.2f}, role: {role}, updated: {_format_ts(conv['updated_ts'])}\n"
        f"    title: {conv['title']}\n"
        f"    message_tokens: {_estimate_tokens(len(full_text))}\n"
        f"    snippet: {snippet}"
    )


def _format_message_extract(conv, msg_idx, msg, text):
    role = str(msg.get("role") or "?")
    return (
        f"--- ref: {conv['modul_id']}/{conv['convo_id']}#m{msg_idx} role={role} ---\n"
        f"{text}"
    )


def _select_messages(conv, terms, center_idx):
    messages = conv["messages"]
    selected = set()
    if center_idx is not None and 0 <= center_idx < len(messages):
        for idx in range(max(0, center_idx - 2), min(len(messages), center_idx + 3)):
            selected.add(idx)
    if terms:
        scored = []
        for idx, msg in enumerate(messages):
            text = _message_text(msg)
            score = _score_terms(terms, text)
            if score > 0:
                scored.append((score, idx))
        scored.sort(reverse=True)
        for _, idx in scored[:8]:
            selected.add(idx)
            if idx > 0:
                selected.add(idx - 1)
            if idx + 1 < len(messages):
                selected.add(idx + 1)
    if not selected and messages:
        selected.update(range(min(6, len(messages))))
    return [(idx, messages[idx]) for idx in sorted(selected)]


def _score(query, terms, text, conv):
    score = _score_terms(terms, text)
    text_norm = _norm(text)
    query_norm = _norm(query)
    if query_norm and query_norm in text_norm:
        score += 8
    title_norm = _norm(conv["title"])
    if query_norm and query_norm in title_norm:
        score += 5
    for term in terms:
        if term in title_norm:
            score += 2
        if term in _norm(conv["modul_id"]):
            score += 1
    return score


def _score_terms(terms, text):
    if not terms:
        return 0
    norm = _norm(text)
    score = 0
    for term in terms:
        if term in norm:
            score += 1 + min(5, norm.count(term))
    return score


def _best_snippet(text, terms, max_chars):
    if len(text) <= max_chars:
        return _one_line(text)
    norm = _norm(text)
    pos = -1
    for term in terms:
        pos = norm.find(term)
        if pos >= 0:
            break
    if pos < 0:
        pos = 0
    start = max(0, pos - max_chars // 3)
    end = min(len(text), start + max_chars)
    if end - start < max_chars:
        start = max(0, end - max_chars)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + _one_line(text[start:end]) + suffix


def _message_text(msg):
    if not isinstance(msg, dict):
        return str(msg)
    content = msg.get("content")
    if content is None:
        content = msg.get("text", "")
    if isinstance(content, (dict, list)):
        content = json.dumps(content, ensure_ascii=False)
    return str(content)


def _parse_ref(ref):
    center_idx = None
    if "#m" in ref:
        ref, msg_part = ref.rsplit("#m", 1)
        try:
            center_idx = int(re.match(r"\d+", msg_part).group(0))
        except Exception:
            center_idx = None
    if "/" in ref:
        modul_id, convo_id = ref.split("/", 1)
    else:
        modul_id, convo_id = "", ref
    return modul_id.strip(), convo_id.strip(), center_idx


def _terms(text):
    stop = {
        "der", "die", "das", "und", "oder", "ein", "eine", "einer", "eines", "ist", "war",
        "was", "wie", "wo", "wer", "wann", "ich", "du", "wir", "uns", "mit", "von", "zu",
        "nach", "im", "in", "am", "an", "auf", "fuer", "für", "bitte", "mal", "den", "dem",
        "des", "the", "and", "or", "to", "of", "a", "in", "on",
    }
    values = []
    for term in re.findall(r"[\wÄÖÜäöüß.-]{2,}", _norm(text)):
        if term not in stop and len(term) >= 2:
            values.append(term)
    seen = set()
    out = []
    for term in values:
        if term not in seen:
            seen.add(term)
            out.append(term)
    return out[:16]


def _norm(text):
    return str(text or "").lower().replace("ß", "ss")


def _one_line(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _estimate_tokens(chars):
    return max(1, int((int(chars) + 3) / 4))


def _tokens_to_chars(tokens):
    return max(1000, int(tokens) * 4)


def _format_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(int(ts), datetime.timezone.utc).isoformat()
    except Exception:
        return "?"


def _int_setting(config, key, default):
    try:
        value = int(config.get(key, default))
        return value if value > 0 else default
    except Exception:
        return default


def _first(params, key):
    if isinstance(params, dict):
        return str(params.get(key) or params.get("0") or "").strip()
    if not params:
        return ""
    raw = str(params[0]).strip()
    m = re.match(rf"^\s*{re.escape(key)}\s*[:=]\s*(.+)$", raw, flags=re.I | re.S)
    return (m.group(1) if m else raw).strip().strip("\"'")


def _second(params, key):
    if isinstance(params, dict):
        return str(params.get(key) or params.get("1") or "").strip()
    if len(params or []) < 2:
        return ""
    raw = str(params[1]).strip()
    m = re.match(rf"^\s*{re.escape(key)}\s*[:=]\s*(.+)$", raw, flags=re.I | re.S)
    return (m.group(1) if m else raw).strip().strip("\"'")


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
