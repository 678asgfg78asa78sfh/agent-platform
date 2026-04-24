use crate::types::{ModulConfig, AgentConfig, Aufgabe};
use crate::pipeline::Pipeline;
use crate::modules;
use crate::util;

/// Result of a tool execution: always SUCCESS or FAILED
#[derive(Debug)]
pub struct ToolResult {
    pub success: bool,
    pub data: String,
}

impl ToolResult {
    pub fn ok(data: String) -> Self { Self { success: true, data } }
    pub fn fail(msg: String) -> Self { Self { success: false, data: msg } }
}

/// Describes a tool that a module can use
#[derive(Debug, Clone)]
pub struct ToolDef {
    pub name: String,
    pub description: String,
    pub params: Vec<String>,
}

/// Returns the list of tools available for a given module type + its permissions
pub fn tools_for_module(modul: &ModulConfig) -> Vec<ToolDef> {
    let mut tools = vec![];
    let perms = &modul.berechtigungen;

    match modul.typ.as_str() {
        "chat" => {
            if perms.iter().any(|p| p == "aufgaben") {
                tools.push(ToolDef {
                    name: "aufgaben.erstellen".into(),
                    description: "Erstellt eine neue Aufgabe für ein anderes Modul".into(),
                    params: vec!["modul".into(), "anweisung".into(), "wann".into()],
                });
            }
            // RAG tools if permission includes any rag.*
            if perms.iter().any(|p| p.starts_with("rag.")) {
                tools.push(ToolDef {
                    name: "rag.suchen".into(),
                    description: "Durchsucht das Wissens-Archiv nach relevanten Informationen".into(),
                    params: vec!["query".into()],
                });
                tools.push(ToolDef {
                    name: "rag.speichern".into(),
                    description: "Speichert eine Information im Wissens-Archiv zum späteren Abruf".into(),
                    params: vec!["text".into()],
                });
            }
            // Agent spawn tool
            tools.push(ToolDef {
                name: "agent.spawn".into(),
                description: "Erstellt einen temporaeren Worker-Agent mit angepasstem Prompt fuer eine spezifische Aufgabe".into(),
                params: vec!["basis_modul".into(), "system_prompt".into(), "aufgabe".into()],
            });
        }
        // "mail" entfernt — IMAP/SMTP/POP3 sind jetzt Python-Module
        "filesystem" => {
            tools.push(ToolDef {
                name: "files.read".into(),
                description: "Liest den Inhalt einer Datei".into(),
                params: vec!["path".into()],
            });
            tools.push(ToolDef {
                name: "files.write".into(),
                description: "Schreibt Inhalt in eine Datei".into(),
                params: vec!["path".into(), "content".into()],
            });
            tools.push(ToolDef {
                name: "files.list".into(),
                description: "Listet Dateien in einem Verzeichnis".into(),
                params: vec!["path".into()],
            });
        }
        "websearch" => {
            tools.push(ToolDef {
                name: "web.search".into(),
                description: "Durchsucht das Web nach Informationen (DuckDuckGo, Brave, Google, Grok)".into(),
                params: vec!["query".into()],
            });
            tools.push(ToolDef {
                name: "http.get".into(),
                description: "Ruft eine bestimmte Webseite ab und gibt den Text zurück".into(),
                params: vec!["url".into()],
            });
        }
        "shell" => {
            tools.push(ToolDef {
                name: "shell.exec".into(),
                description: "Führt einen Shell-Befehl aus (nur Whitelist-Befehle erlaubt)".into(),
                params: vec!["command".into()],
            });
        }
        "notify" => {
            tools.push(ToolDef {
                name: "notify.send".into(),
                description: "Sendet eine Benachrichtigung (ntfy/gotify/telegram)".into(),
                params: vec!["message".into()],
            });
        }
        _ => {}
    }

    // All modules with aufgaben permission get aufgaben.erstellen
    if modul.typ != "chat" && perms.iter().any(|p| p == "aufgaben") {
        tools.push(ToolDef {
            name: "aufgaben.erstellen".into(),
            description: "Erstellt eine neue Aufgabe für ein anderes Modul".into(),
            params: vec!["modul".into(), "anweisung".into(), "wann".into()],
        });
    }

    // File-Tools NICHT mehr default für alle Module. Least-Privilege: explizite
    // Permission "files" (voller Zugriff auf allowed_paths) oder "files.home"
    // (nur das eigene Home-Verzeichnis) wird verlangt. Das typ=="filesystem"
    // Modul setzt die Tools selbst oben; andere Module müssen die Permission
    // aktiv in ihrer Config haben. Ohne diese Änderung hätte ein Prompt-
    // Injection-Angriff gegen jedes beliebige Modul (Chat, Websearch, Notify)
    // automatisch Filesystem-Zugriff — das war das "dümmste-Design" Finding.
    if modul.typ != "filesystem" {
        let has_files_perm = perms.iter().any(|p| p == "files" || p == "files.home" || p == "files.*");
        if has_files_perm {
            tools.push(ToolDef {
                name: "files.read".into(),
                description: "Liest eine Datei aus deinem Home-Verzeichnis".into(),
                params: vec!["path".into()],
            });
            tools.push(ToolDef {
                name: "files.write".into(),
                description: "Schreibt eine Datei in dein Home-Verzeichnis".into(),
                params: vec!["path".into(), "content".into()],
            });
            tools.push(ToolDef {
                name: "files.list".into(),
                description: "Listet Dateien in deinem Home-Verzeichnis".into(),
                params: vec!["path".into()],
            });
        }
    }

    tools
}

/// Baut die OpenAI-kompatible tools[] JSON-Liste fuer den API-Call
pub fn tools_as_openai_json(modul: &ModulConfig, py_modules: &[crate::loader::PyModuleMeta]) -> Vec<serde_json::Value> {
    let mut result = vec![];

    // Rust-Tools
    for t in tools_for_module(modul) {
        let mut props = serde_json::Map::new();
        let mut required = vec![];
        for p in &t.params {
            props.insert(p.clone(), serde_json::json!({"type": "string", "description": p}));
            required.push(serde_json::json!(p));
        }
        result.push(serde_json::json!({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": {
                    "type": "object",
                    "properties": props,
                    "required": required,
                }
            }
        }));
    }

    // Python-Tools — permission derived from linked modules OR legacy berechtigungen
    for py_mod in py_modules {
        let perm_key = format!("py.{}", py_mod.name);
        let has_perm = modul.berechtigungen.iter().any(|p| p == &perm_key || p == "py.*")
            || modul.linked_modules.iter().any(|link_id| {
                // Exact match OR "<py_name>.<instance>" prefix. Früher war hier
                // `link_id.contains(&py_mod.name)` — das gab einem Link
                // `chat.mail` Zugriff auf Python-Modul `mail`, und `mailadmin`
                // Zugriff auf `mail` (Substring-Kollision). Jetzt muss der
                // link_id entweder exakt der Modulname sein oder mit
                // "<name>." anfangen.
                link_id == &py_mod.name || link_id.starts_with(&format!("{}.", py_mod.name))
            });
        if !has_perm { continue; }

        for tool in &py_mod.tools {
            let mut props = serde_json::Map::new();
            let mut required = vec![];
            for p in &tool.params {
                props.insert(p.clone(), serde_json::json!({"type": "string", "description": p}));
                required.push(serde_json::json!(p));
            }
            result.push(serde_json::json!({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": props,
                        "required": required,
                    }
                }
            }));
        }
    }

    result
}

/// Parst einen OpenAI tool_calls Response. Returns (tool_name, params_vec).
///
/// Wenn `schema_required` gesetzt ist (z.B. `["path", "content"]` aus dem
/// tools_as_openai_json-Output), werden die Args GENAU in dieser Reihenfolge
/// sortiert — das ist der autoritative Pfad, Missbrauch durch LLM-Key-
/// Reordering ist unmöglich. Ohne Schema (Fallback) kommt die alte path_keys-
/// Heuristik zum Zug; die ist aber by-design schwächer und nur für den seltenen
/// Fall gedacht dass das Tool nicht in der Schema-Liste auffindbar ist.
///
/// Vorher war die Heuristik immer aktiv und erlaubte ein theoretisches Bypass:
/// ein LLM konnte `{inhalt: "/etc/passwd", ziel: "..."}` senden → weder Key
/// war in der path_keys-Liste → Reihenfolge war insertion-order-zufällig und
/// die Whitelist-Prüfung lief auf dem falsch zugeordneten Parameter. Kimi/Qwen/
/// GPT-Finding, Run 4.
pub fn parse_openai_tool_call(data: &serde_json::Value) -> Option<(String, Vec<String>)> {
    parse_openai_tool_call_with_schema(data, None)
}

/// Wie `parse_openai_tool_call`, aber mit explizitem Schema. Wenn
/// `schema_required` Some ist, wird die Reihenfolge der Args daraus abgeleitet
/// statt aus einer Heuristik.
pub fn parse_openai_tool_call_with_schema(
    data: &serde_json::Value,
    schema_required: Option<&[String]>,
) -> Option<(String, Vec<String>)> {
    let tool_calls = data.pointer("/choices/0/message/tool_calls")
        .and_then(|v| v.as_array());
    let ollama_calls = data.pointer("/choices/0/message/tool_calls")
        .or_else(|| data.pointer("/message/tool_calls"))
        .and_then(|v| v.as_array());

    let calls = tool_calls.or(ollama_calls)?;
    let call = calls.first()?;
    let name = call["function"]["name"].as_str()?.to_string();

    let args: serde_json::Value = match &call["function"]["arguments"] {
        serde_json::Value::String(s) => serde_json::from_str(s).unwrap_or_default(),
        v if v.is_object() => v.clone(),
        _ => serde_json::Value::Object(serde_json::Map::new()),
    };

    fn unescape_html(s: &str) -> String {
        s.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
         .replace("&quot;", "\"").replace("&#39;", "'").replace("&nbsp;", " ")
    }

    let params = if let Some(obj) = args.as_object() {
        if obj.is_empty() {
            vec![]
        } else if let Some(required) = schema_required {
            // AUTORITATIVE Reihenfolge aus Schema. Jedes required-Feld wird in der
            // Schema-Reihenfolge geholt (leerer String falls LLM es wegließ).
            // Extra-Args außerhalb des Schemas werden hinten angehängt — sie haben
            // keine definierte Position, aber Tool-Handler die Positions-
            // basiert arbeiten ignorieren sie sowieso.
            let mut result: Vec<String> = required.iter().map(|k| {
                obj.get(k)
                    .map(|v| if let Some(s) = v.as_str() { s.to_string() } else { v.to_string() })
                    .map(|s| unescape_html(&s))
                    .unwrap_or_default()
            }).collect();
            // Extra keys NICHT im Schema — hinten anhängen, aber in stabiler Reihenfolge
            let required_set: std::collections::HashSet<&str> =
                required.iter().map(|s| s.as_str()).collect();
            let mut extras: Vec<(String, String)> = obj.iter()
                .filter(|(k, _)| !required_set.contains(k.as_str()))
                .map(|(k, v)| {
                    let raw = if let Some(s) = v.as_str() { s.to_string() } else { v.to_string() };
                    (k.clone(), unescape_html(&raw))
                })
                .collect();
            extras.sort_by(|a, b| a.0.cmp(&b.0));
            result.extend(extras.into_iter().map(|(_, v)| v));
            result
        } else {
            // Fallback-Heuristik ohne Schema. Weniger sicher, aber besser als
            // reine Insertion-Order — wenn das Tool in der path_keys-Liste steht,
            // kommen path-artige Args zuerst.
            let path_keys = ["path", "pfad", "pfad_und_bereich", "pfad_und_zeile",
                             "file", "datei", "url", "name", "modul_name", "modul",
                             "query", "to", "wann", "loop_id", "basis_modul",
                             "ziel", "kriterien", "command"];
            let mut ordered = Vec::new();
            let mut remaining = Vec::new();

            for (k, v) in obj.iter() {
                let raw = if let Some(s) = v.as_str() { s.to_string() } else { v.to_string() };
                let val = unescape_html(&raw);
                if path_keys.contains(&k.to_lowercase().as_str()) {
                    ordered.push((k.clone(), val));
                } else {
                    remaining.push(val);
                }
            }

            ordered.sort_by_key(|(k, _)| {
                path_keys.iter().position(|pk| pk == &k.to_lowercase().as_str()).unwrap_or(999)
            });

            let mut result: Vec<String> = ordered.into_iter().map(|(_, v)| v).collect();
            result.extend(remaining);
            result
        }
    } else {
        vec![]
    };

    Some((name, params))
}

/// Liefert die `required`-Liste aus dem Schema eines Tools. Nutzt die Schemata
/// aus `tools_as_openai_json` — damit bleibt das die einzige Source-of-Truth
/// für Parameter-Reihenfolge eines Tools.
pub fn schema_required_for(
    tool_name: &str,
    modul: &ModulConfig,
    py_modules: &[crate::loader::PyModuleMeta],
) -> Option<Vec<String>> {
    for t in tools_as_openai_json(modul, py_modules) {
        let name = t.pointer("/function/name").and_then(|v| v.as_str())?.to_string();
        if name == tool_name {
            let req = t.pointer("/function/parameters/required")?
                .as_array()?
                .iter()
                .filter_map(|v| v.as_str().map(String::from))
                .collect::<Vec<_>>();
            return Some(req);
        }
    }
    None
}

/// Ergaenzt Python-Tool-Beschreibungen wenn das Modul die passende Berechtigung hat
pub fn append_python_tools(prompt: &mut String, modul: &ModulConfig, py_modules: &[crate::loader::PyModuleMeta]) {
    for py_mod in py_modules {
        // Berechtigung: "py.modulname" oder "py.*" OR linked to a module of that type.
        // Exact match statt substring, siehe tools_as_openai_json für Begründung.
        let perm_key = format!("py.{}", py_mod.name);
        let has_perm = modul.berechtigungen.iter().any(|p| p == &perm_key || p == "py.*")
            || modul.linked_modules.iter().any(|link_id| {
                link_id == &py_mod.name || link_id.starts_with(&format!("{}.", py_mod.name))
            });
        if !has_perm { continue; }

        for tool in &py_mod.tools {
            let params_str = tool.params.join(", ");
            prompt.push_str(&format!("[TOOL:{name}({params})]\n  {desc}\n\n",
                name = tool.name, params = params_str, desc = tool.description));
        }
    }
}

/// Build the system prompt section describing available tools
pub fn tools_prompt(modul: &ModulConfig) -> String {
    let tools = tools_for_module(modul);
    if tools.is_empty() {
        return String::new();
    }

    let mut prompt = String::from("\n\nDu hast folgende Tools zur Verfügung:\n\n");
    for t in &tools {
        let params_str = t.params.join(", ");
        prompt.push_str(&format!("[TOOL:{name}({params})]\n  {desc}\n\n",
            name = t.name, params = params_str, desc = t.description));
    }
    prompt.push_str(
        "WICHTIG - So benutzt du Tools:\n\
         Wenn du ein Tool nutzen willst, antworte AUSSCHLIESSLICH mit dem Tool-Call und NICHTS ANDEREM:\n\
         <tool>name(param1, param2)</tool>\n\n\
         EXAKTE SYNTAX: <tool> dann Toolname, dann Klammer auf, Parameter, Klammer zu, dann </tool>\n\
         KEIN anderes Format! KEIN <tool:.../>! KEIN Markdown! NUR <tool>...</tool>\n\n"
    );

    // Typspezifische Beispiele
    match modul.typ.as_str() {
        "chat" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'merk dir X' → <tool>rag.speichern(X)</tool>\n\
                 - 'was weisst du über Y' → <tool>rag.suchen(Y)</tool>\n\
                 - 'erstelle eine Aufgabe' → <tool>aufgaben.erstellen(modul, anweisung, sofort)</tool>\n\n");
        }
        "filesystem" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'liste /home/user/docs' → <tool>files.list(/home/user/docs)</tool>\n\
                 - 'lies /tmp/test.txt' → <tool>files.read(/tmp/test.txt)</tool>\n\
                 - 'schreibe in /tmp/out.txt' → <tool>files.write(/tmp/out.txt, inhalt hier)</tool>\n\n");
        }
        "websearch" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'suche nach Rust' → <tool>web.search(Rust programming)</tool>\n\
                 - 'öffne URL' → <tool>http.get(https://example.com)</tool>\n\n");
        }
        "mail" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'suche Mails von Chef' → <tool>imap.search(FROM chef)</tool>\n\
                 - 'lies Mail 42' → <tool>imap.read(42)</tool>\n\n");
        }
        "shell" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'zeige Festplatten' → <tool>shell.exec(df -h)</tool>\n\
                 - 'git status' → <tool>shell.exec(git status)</tool>\n\n");
        }
        "notify" => {
            prompt.push_str(
                "Beispiele:\n\
                 - 'sag Bescheid' → <tool>notify.send(Aufgabe erledigt)</tool>\n\n");
        }
        _ => {}
    }

    prompt.push_str(
        "REGELN:\n\
         - Wenn du ein Tool brauchst, antworte NUR mit dem <tool>...</tool> Tag. Kein Text davor oder danach.\n\
         - Du bekommst das Tool-Ergebnis zurück und antwortest dann dem User basierend auf dem Ergebnis.\n\
         - Für normale Gespräche ohne Tool-Bedarf antworte direkt ohne Tool-Call.\n\
         - VERTRAUE dem Tool-Ergebnis! Wenn das Tool SUCCESS meldet, hat es funktioniert. Erfinde KEINE Fehler.\n"
    );
    prompt
}

/// Parse tool calls from LLM response. Supports:
///   <tool>name(params)</tool>          — standard format
///   <tool:name(params)/>               — Gemma4 alternative
///   <tool>name(key=value, ...)</tool>  — named params
pub fn parse_tool_call(text: &str) -> Option<(String, Vec<String>)> {
    // Standard format: <tool>name(params)</tool>
    if let (Some(start), Some(end)) = (text.find("<tool>"), text.find("</tool>")) {
        if end > start {
            let inner = text[start + 6..end].trim();
            return parse_tool_inner(inner);
        }
    }

    // Gemma4 alternative: <tool:name(params)/> or <tool:name(key="value")/>
    if let Some(start) = text.find("<tool:") {
        let after = &text[start + 6..];
        if let Some(end) = after.find("/>") {
            let inner = after[..end].trim();
            return parse_tool_inner(inner);
        }
    }

    None
}

fn parse_tool_inner(inner: &str) -> Option<(String, Vec<String>)> {
    let paren_start = inner.find('(')?;
    let name = inner[..paren_start].trim().to_string();
    let paren_end = inner.rfind(')')?;
    let params_str = &inner[paren_start + 1..paren_end];

    if params_str.trim().is_empty() {
        return Some((name, vec![]));
    }

    // Erster Param: bis zum ersten Komma (oder alles wenn kein Komma)
    // Rest: RAW, unverändert — damit HTML/Code nicht zerstört wird
    let params = if let Some(comma) = params_str.find(',') {
        let first = params_str[..comma].trim();
        let rest = params_str[comma + 1..].trim();
        // Ersten Param: key=value strippen, Quotes strippen
        let first = clean_param(first);
        // Rest bleibt roh (kann HTML, Code, etc. enthalten)
        // Aber wenn rest AUCH key=value ist (z.B. query="hello"), dann strippen
        let rest = if !rest.contains('<') && !rest.contains('{') && !rest.contains('\n') {
            // Sieht nicht nach Code/HTML aus → normal parsen (weitere Komma-Splits)
            let mut parts = vec![first];
            for p in rest.split(',') {
                parts.push(clean_param(p.trim()));
            }
            return Some((name, parts));
        } else {
            // Sieht nach Code/HTML aus → NICHT splitten, roh lassen
            rest.to_string()
        };
        vec![first, rest]
    } else {
        // Nur ein Parameter
        vec![clean_param(params_str.trim())]
    };

    Some((name, params))
}

fn clean_param(s: &str) -> String {
    // key=value strippen (z.B. query="Alpha" → Alpha)
    let s = if let Some(eq_pos) = s.find('=') {
        let after = s[eq_pos + 1..].trim();
        // Nur strippen wenn der Key ein einfaches Wort ist (kein HTML-Attribut)
        let key = s[..eq_pos].trim();
        if key.chars().all(|c| c.is_alphanumeric() || c == '_') {
            after
        } else {
            s // HTML-Attribut wie style="..." → nicht anfassen
        }
    } else {
        s
    };
    s.trim_matches('"').trim_matches('\'').to_string()
}

/// Execute a tool call with permission checking
pub async fn execute_tool(
    tool_name: &str,
    params: &[String],
    modul: &ModulConfig,
    config: &AgentConfig,
    pipeline: &Pipeline,
) -> ToolResult {
    // Permission-Check NUR fuer bekannte Rust-Tools.
    // Unbekannte Tools fallen durch zum "Unbekanntes Tool" default,
    // damit der Python-Fallback in cycle.rs/web.rs greifen kann.
    // Python-Tool Permissions werden dort via has_permission_with_py geprueft.
    let is_known_rust_tool = matches!(tool_name,
        "rag.suchen" | "rag.speichern" | "aufgaben.erstellen" |
        "files.read" | "files.write" | "files.list" |
        "web.search" | "http.get" | "shell.exec" | "notify.send" |
        "agent.spawn"
    );
    if is_known_rust_tool && !has_permission(modul, tool_name) {
        return ToolResult::fail(format!("DENIED: Modul '{}' hat keine Berechtigung für Tool '{}'", modul.name, tool_name));
    }

    match tool_name {
        // RAG tools
        "rag.suchen" => {
            let query = params.first().map(|s| s.as_str()).unwrap_or("");
            let pool = modul.rag_pool.as_deref().unwrap_or("shared");
            // Embedding handled by caller (cycle.rs/web.rs) when embedding_backend is configured
            modules::rag::suchen(&pipeline.base, pool, query, None).await
        }
        "rag.speichern" => {
            let text = params.first().map(|s| s.as_str()).unwrap_or("");
            let pool = modul.rag_pool.as_deref().unwrap_or("shared");
            // Embedding handled by caller (cycle.rs/web.rs) when embedding_backend is configured
            modules::rag::speichern(&pipeline.base, pool, text, None, None).await
        }

        // Aufgaben
        "aufgaben.erstellen" => {
            let target_modul = params.first().map(|s| s.as_str()).unwrap_or("");
            let anweisung = params.get(1).map(|s| s.as_str()).unwrap_or("");
            let wann = params.get(2).map(|s| s.as_str()).unwrap_or("sofort");

            if anweisung.is_empty() && !target_modul.is_empty() {
                // Only one param given — treat it as anweisung for own module
                let aufgabe = Aufgabe::neu(&modul.id, target_modul, wann, &modul.name);
                match pipeline.speichern(&aufgabe) {
                    Ok(_) => ToolResult::ok(format!("Aufgabe erstellt: {} fuer Modul '{}'", aufgabe.id, modul.id)),
                    Err(e) => ToolResult::fail(format!("Aufgabe erstellen fehlgeschlagen: {}", e)),
                }
            } else if anweisung.is_empty() {
                ToolResult::fail("aufgaben.erstellen braucht mindestens eine Anweisung".into())
            } else {
                let target = if target_modul.is_empty() { &modul.id } else { target_modul };
                // Linking check: target must be in linked_modules (or be self)
                if target != &modul.id {
                    if !modul.linked_modules.contains(&target.to_string()) {
                        return ToolResult::fail(format!(
                            "DENIED: Modul '{}' ist nicht mit '{}' verlinkt. Erlaubte Links: {:?}",
                            modul.id, target, modul.linked_modules
                        ));
                    }
                }
                let aufgabe = Aufgabe::neu(target, anweisung, wann, &modul.name);
                match pipeline.speichern(&aufgabe) {
                    Ok(_) => ToolResult::ok(format!("Aufgabe erstellt: {} fuer Modul '{}'", aufgabe.id, target)),
                    Err(e) => ToolResult::fail(format!("Aufgabe erstellen fehlgeschlagen: {}", e)),
                }
            }
        }

        // File tools — jedes Modul hat automatisch Zugriff auf sein Home-Verzeichnis
        "files.read" => {
            let path = params.first().map(|s| s.as_str()).unwrap_or("");
            let home = pipeline.home_dir(&modul.id).to_string_lossy().to_string();
            let mut allowed: Vec<String> = modul.settings.allowed_paths.clone().unwrap_or_default();
            allowed.push(home);
            let allowed_refs: Vec<&str> = allowed.iter().map(|s| s.as_str()).collect();
            let max_size = modul.settings.max_file_size.unwrap_or(4000) as usize;
            modules::files::read_file(path, &allowed_refs, max_size).await
        }
        "files.write" => {
            let path = params.first().map(|s| s.as_str()).unwrap_or("");
            // Content ist der zweite Parameter — wird vom Parser roh gelassen (HTML/Code safe)
            let content = params.get(1).map(|s| s.as_str()).unwrap_or("");
            let home = pipeline.home_dir(&modul.id).to_string_lossy().to_string();
            let mut allowed: Vec<String> = modul.settings.allowed_paths.clone().unwrap_or_default();
            allowed.push(home);
            let allowed_refs: Vec<&str> = allowed.iter().map(|s| s.as_str()).collect();
            let allow_write = modul.settings.allow_write.unwrap_or(true);
            modules::files::write_file(path, content, &allowed_refs, allow_write).await
        }
        "files.list" => {
            let path = params.first().map(|s| s.as_str()).unwrap_or("");
            let home = pipeline.home_dir(&modul.id).to_string_lossy().to_string();
            let mut allowed: Vec<String> = modul.settings.allowed_paths.clone().unwrap_or_default();
            allowed.push(home);
            let allowed_refs: Vec<&str> = allowed.iter().map(|s| s.as_str()).collect();
            modules::files::list_dir(path, &allowed_refs).await
        }

        // Web tools
        "web.search" => {
            let query = params.first().map(|s| s.as_str()).unwrap_or("");
            modules::web::search(&modul.settings, query).await
        }
        "http.get" => {
            let url = params.first().map(|s| s.as_str()).unwrap_or("");
            modules::web::http_get(url).await
        }

        // Mail: IMAP/SMTP/POP3 sind jetzt Python-Module → Fallback handled es

        // Shell tools — kein sh -c! Direkter Aufruf ohne Shell-Interpretation.
        "shell.exec" => {
            let command = params.first().map(|s| s.as_str()).unwrap_or("");
            let allowed = modul.settings.allowed_commands.as_ref()
                .map(|v| v.iter().map(|s| s.as_str()).collect::<Vec<_>>())
                .unwrap_or_default();
            let working_dir = modul.settings.working_dir.as_deref().unwrap_or(".");
            if command.is_empty() {
                ToolResult::fail("Kein Befehl angegeben".into())
            } else {
                // Shell-Metazeichen blocken um Injection zu verhindern
                let dangerous = [';', '|', '&', '`', '$', '(', ')', '<', '>', '{', '}', '!', '\\', '\n'];
                if command.chars().any(|c| dangerous.contains(&c)) {
                    ToolResult::fail(format!("DENIED: Befehl enthält unerlaubte Zeichen: {}", command))
                } else {
                    let parts: Vec<&str> = command.split_whitespace().collect();
                    let cmd_name = parts.first().copied().unwrap_or("");
                    if allowed.is_empty() || !allowed.contains(&cmd_name) {
                        ToolResult::fail(format!("DENIED: Befehl '{}' nicht in der Whitelist: {:?}", cmd_name, allowed))
                    } else if args_touch_sensitive_paths(&parts[1..]) {
                        // Whitelist gilt nur für command-name. Zusätzlich: Args
                        // dürfen nicht auf sensible System-Pfade zeigen. `cat`
                        // whitelisted → `cat /etc/shadow` würde sonst laufen.
                        // GLM-Finding Run SQLite-4.
                        ToolResult::fail(format!("DENIED: shell.exec-Argument zeigt auf geschützten Pfad (/etc/, /root/, ~/.ssh, /sys/, /proc/k*). command: {}", command))
                    } else {
                        let output = tokio::process::Command::new(cmd_name)
                            .args(&parts[1..])
                            .current_dir(working_dir)
                            .output()
                            .await;
                        match output {
                            Ok(o) => {
                                let stdout = String::from_utf8_lossy(&o.stdout);
                                let stderr = String::from_utf8_lossy(&o.stderr);
                                let text = format!("exit: {}\nstdout:\n{}\nstderr:\n{}", o.status, stdout, stderr);
                                let truncated = util::safe_truncate_owned(&text, 4000);
                                if o.status.success() { ToolResult::ok(truncated) } else { ToolResult::fail(truncated) }
                            }
                            Err(e) => ToolResult::fail(format!("Shell Fehler: {}", e)),
                        }
                    }
                }
            }
        }

        // Notify tools
        "notify.send" => {
            let message = params.first().map(|s| s.as_str()).unwrap_or("");
            if message.is_empty() {
                return ToolResult::fail("Keine Nachricht angegeben".into());
            }
            let notify_type = modul.settings.notify_type.as_deref().unwrap_or("ntfy");
            let url = modul.settings.notify_url.as_deref().unwrap_or("");
            let token = modul.settings.notify_token.as_deref().unwrap_or("");
            let topic = modul.settings.notify_topic.as_deref().unwrap_or("agent");
            if url.is_empty() {
                return ToolResult::fail("notify_url nicht konfiguriert".into());
            }
            let client = reqwest::Client::builder()
                .timeout(std::time::Duration::from_secs(10))
                .build()
                .unwrap_or_else(|_| reqwest::Client::new());
            let result = match notify_type {
                "ntfy" => {
                    let endpoint = format!("{}/{}", url.trim_end_matches('/'), topic);
                    client.post(&endpoint).body(message.to_string()).send().await
                }
                "gotify" => {
                    let endpoint = format!("{}/message?token={}", url.trim_end_matches('/'), token);
                    client.post(&endpoint)
                        .json(&serde_json::json!({"title": "Agent", "message": message, "priority": 5}))
                        .send().await
                }
                "telegram" => {
                    let endpoint = format!("https://api.telegram.org/bot{}/sendMessage", token);
                    client.post(&endpoint)
                        .json(&serde_json::json!({"chat_id": topic, "text": message}))
                        .send().await
                }
                _ => return ToolResult::fail(format!("Unbekannter notify_type: {}", notify_type)),
            };
            match result {
                Ok(resp) if resp.status().is_success() => ToolResult::ok(format!("Benachrichtigung gesendet via {}", notify_type)),
                Ok(resp) => ToolResult::fail(format!("Notify fehlgeschlagen: HTTP {}", resp.status())),
                Err(e) => ToolResult::fail(format!("Notify Fehler: {}", e)),
            }
        }

        "agent.spawn" => {
            let basis_id = params.first().map(|s| s.as_str()).unwrap_or("");
            let prompt = params.get(1).map(|s| s.as_str()).unwrap_or("");
            let aufgabe_text = params.get(2).map(|s| s.as_str()).unwrap_or("");

            if basis_id.is_empty() || prompt.is_empty() || aufgabe_text.is_empty() {
                return ToolResult::fail("agent.spawn braucht: basis_modul, system_prompt, aufgabe".into());
            }

            // Check: caller must not be a temp agent spawning more temp agents
            if !modul.persistent {
                return ToolResult::fail("DENIED: Temp-Agenten koennen keine weiteren Agenten spawnen".into());
            }

            // Find basis module
            let basis = config.module.iter().find(|m| m.id == basis_id || m.name == basis_id);
            let Some(basis) = basis else {
                return ToolResult::fail(format!("Basis-Modul '{}' nicht gefunden", basis_id));
            };

            // Temp-Agent Permissions: striktes Least-Privilege. Nur rag.* und
            // websearch werden geerbt — alles andere wird gestrippt.
            //
            // `aufgaben` wird EXPLIZIT ausgeschlossen auch wenn der Parent es hat:
            // sonst könnte der Temp-Agent via aufgaben.erstellen einen Task für den
            // Creator (seinen einzigen linked_module) erstellen mit beliebigem
            // Anweisungs-Text. Der Creator führt diesen Text in seinem vollen
            // Security-Kontext (files/shell/notify/agent.spawn) aus. Prompt-Injection
            // im Spawn-Prompt → Creator-Execution = vollständige Privilege-Escalation
            // (GLM-Finding Run 6).
            //
            // Das Ergebnis des Temp-Agents fließt weiterhin via `zurueck_an` zurück
            // zum Creator — dafür braucht der Temp-Agent keine aufgaben-Permission.
            let safe_inherit: std::collections::HashSet<&str> =
                ["rag", "rag.*", "websearch"].into_iter().collect();
            let stripped_perms: Vec<String> = modul.berechtigungen.iter()
                .filter(|p| {
                    let s: &str = p;
                    safe_inherit.contains(s)
                        || s.starts_with("rag.")
                        // keine "aufgaben" — sonst Task-Routing-Privilege-Escalation
                        // keine "files*", "shell*", "notify*", "agent.*", "py.*" — siehe oben
                })
                .cloned()
                .collect();

            // Create temp module config
            let temp_id = format!("temp.{}.{}", modul.id, &uuid::Uuid::new_v4().to_string()[..8]);
            let temp_modul = crate::types::ModulConfig {
                id: temp_id.clone(),
                typ: basis.typ.clone(),
                name: temp_id.clone(),
                display_name: format!("TEMP: {}", basis.display_name),
                llm_backend: basis.llm_backend.clone(),
                backup_llm: basis.backup_llm.clone(),
                berechtigungen: stripped_perms, // sichere Teilmenge, nicht full-inherit
                linked_modules: vec![modul.id.clone()],       // only link back to creator
                persistent: false,
                spawned_by: Some(modul.id.clone()),
                spawn_ttl_s: Some(300), // 5 min default
                created_at: Some(chrono::Utc::now().timestamp() as u64),
                timeout_s: basis.timeout_s,
                retry: 0,
                scheduler_interval_ms: Some(2000),
                max_concurrent_tasks: Some(1),
                token_budget: modul.token_budget,
                token_budget_warning: modul.token_budget_warning,
                settings: basis.settings.clone(),
                identity: crate::types::ModulIdentity {
                    bot_name: format!("Worker-{}", &temp_id[..12]),
                    greeting: String::new(),
                    system_prompt: prompt.to_string(),
                },
                rag_pool: basis.rag_pool.clone(),
            };

            // Create the task for the temp agent
            let aufgabe = crate::types::Aufgabe::llm_call(
                aufgabe_text, &temp_id, &modul.id,
                Some(modul.id.clone()), // route result back to creator
            );
            let aufgabe_id = aufgabe.id.clone();

            // We can't modify config here (we only have &AgentConfig), so we store
            // the temp module spec as a JSON file that the orchestrator will pick up
            let temp_dir = pipeline.base.join("temp_modules");
            std::fs::create_dir_all(&temp_dir).ok();
            let spec_path = temp_dir.join(format!("{}.json", temp_id));
            let spec = serde_json::json!({
                "module": temp_modul,
                "task": aufgabe,
            });
            let spec_json = match serde_json::to_string_pretty(&spec) {
                Ok(j) => j,
                Err(e) => return ToolResult::fail(format!("Temp-Agent serialisieren fehlgeschlagen: {}", e)),
            };
            match crate::util::atomic_write(&spec_path, spec_json.as_bytes()) {
                Ok(_) => {
                    pipeline.log("agent.spawn", Some(&aufgabe_id), crate::types::LogTyp::Info,
                        &format!("Temp-Agent {} gespawnt (basis: {}, ttl: 300s)", temp_id, basis_id));
                    ToolResult::ok(format!("Temp-Agent '{}' erstellt. Task '{}' wird ausgefuehrt, Ergebnis kommt zurueck.", temp_id, &aufgabe_id[..8]))
                }
                Err(e) => ToolResult::fail(format!("Temp-Agent erstellen fehlgeschlagen: {}", e)),
            }
        }

        _ => {
            // Kein Rust-Tool gefunden → Python-Module checken
            ToolResult::fail(format!("Unbekanntes Tool: {} (kein Rust-Modul, Python-Fallback wird vom Cycle gehandled)", tool_name))
        }
    }
}

/// True wenn das Tool Seiteneffekte nach außen hat (filesystem, process, network).
/// Für diese wird ein Audit-Log geschrieben — reines Lesen (http.get, web.search,
/// files.read, rag.suchen) nicht, sonst wird der Audit-Trail unlesbar.
/// Blacklist sensibler Pfade für shell.exec-Argumente. Schützt gegen den
/// Fall dass ein Command whitelisted ist (cat, ls, grep, head, tail) aber
/// auf einen sensiblen Pfad angewendet wird. Der Check ist bewusst breit —
/// false positives (`ls /etc/updated-at` wird geblockt) sind OK, weil es
/// trivial eine eigene Whitelist-Erweiterung pro Module gibt; false negatives
/// (ein geschütztes File wird geleaked) nicht.
fn args_touch_sensitive_paths(args: &[&str]) -> bool {
    const BLOCKED_PREFIXES: &[&str] = &[
        "/etc/",       // passwd, shadow, ssh-configs, systemd-units
        "/root/",      // root home
        "/sys/",       // kernel state
        "/proc/kcore", // kernel memory
        "/proc/kmsg",
        "/dev/mem",
        "/dev/kmem",
        "/boot/",      // kernel + initramfs
    ];
    const BLOCKED_SUFFIXES: &[&str] = &[
        "/.ssh",
        "/.aws",
        "/.gnupg",
        "/.docker/config.json",
        "/authorized_keys",
        "/id_rsa",
        "/id_ed25519",
    ];
    for arg in args {
        let a = arg.trim_matches(|c: char| c == '"' || c == '\'');
        if BLOCKED_PREFIXES.iter().any(|p| a.starts_with(p) || a.contains(&format!("={}", p))) {
            return true;
        }
        if BLOCKED_SUFFIXES.iter().any(|s| a.contains(s)) {
            return true;
        }
    }
    false
}

/// Positiv-Liste reiner READ-Tools. Alles andere wird als Side-Effect behandelt
/// und bekommt Idempotency + Audit-Log. Default-Deny-Style — wenn ein neues
/// Tool (besonders Python-Module) auftaucht, fällt es automatisch in die Side-
/// Effect-Kategorie und wird sauber geschützt. Das vorherige Hardcoded-Liste-
/// Modell hatte eine Lücke: Python-Tool mail.send wurde weder dedupliziert
/// noch auditiert (OpenAI-Finding Run SQLite-4).
fn tool_has_side_effect(tool_name: &str) -> bool {
    const PURE_READS: &[&str] = &[
        "files.read", "files.list",
        "web.search", "http.get",
        "rag.suchen",
        "imap.search", "imap.read", "imap.list",  // mail reads
        "pop3.list", "pop3.read",
    ];
    !PURE_READS.contains(&tool_name)
}

/// Unified tool dispatcher used by both cycle.rs (LLM tasks, direct tasks) and
/// web.rs (chat). Handles: Idempotency-Check für Side-Effect-Tools, Audit-Trail,
/// RAG embedding pre-compute, Rust tool exec, Python fallback with permission check.
///
/// `task_id`: Wenn `Some`, wird der Aufruf mit task_id+tool+params gehasht und
/// gegen die `idempotency`-Tabelle geprüft. Beim Cache-Hit (Side-Effect-Tool
/// lief schon mal mit exakt diesen Inputs) kommt das gespeicherte Result direkt
/// zurück — der eigentliche Tool-Call wird NICHT nochmal ausgeführt. Das ist
/// die exactly-once-Garantie gegen at-least-once Retries (Watchdog-Abort nach
/// Seiteneffekt-Completion, Crash-Recovery, Guardrail-Retry).
/// Für nicht-seiteneffektbehaftete Tools (files.read, http.get, web.search,
/// rag.suchen) wird kein Idempotency-Check gemacht — die sind von Natur aus
/// idempotent und ihre Ergebnisse können sich zwischen Calls legitim ändern.
pub async fn exec_tool_unified(
    tool_name: &str,
    params: &[String],
    modul_id: &str,
    task_id: Option<&str>,
    pipeline: &Pipeline,
    llm: &crate::llm::LlmRouter,
    py_modules: &[crate::loader::PyModuleMeta],
    py_pool: &crate::loader::PyProcessPool,
    config_snapshot: &AgentConfig,
) -> (bool, String) {
    // ══════ Idempotency-Gate ══════
    // Nur für Side-Effect-Tools UND nur wenn wir eine task_id haben (Scheduler-
    // Pfad). Chat-Flow (web.rs) läuft ohne task_id → keine Deduplication, was ok
    // ist weil Chat synchron ist und keine Retry-Loops hat.
    //
    // Two-Phase-Protokoll gegen Watchdog-Abort-mid-execute:
    //  1. Lookup: Cache-Hit mit echtem Result → return cached (exactly-once)
    //  2. Lookup: IN_PROGRESS-Marker → FAIL mit "ambiguous" (ehrlicher als
    //     blindes Re-Execute eines ggf. schon-geschehenen Side-Effects —
    //     User soll manual resolven)
    //  3. Sonst: Mark als IN_PROGRESS, execute, danach echtes Result schreiben
    //     (oder bei Failure: marker löschen damit legitimer Retry klappen darf).
    let idempotency_key = match task_id {
        Some(tid) if tool_has_side_effect(tool_name) => {
            let key = crate::store::idempotency_key(tid, tool_name, params);
            if let Ok(Some((success, data))) = crate::store::idempotency_get(&pipeline.store.pool, &key) {
                if data == crate::store::IDEMPOTENCY_IN_PROGRESS {
                    pipeline.log(modul_id, Some(tid), crate::types::LogTyp::Warning,
                        &format!("Idempotency: {} vorheriger Versuch unterbrochen (crash/abort mid-execute). FAIL — manuelles Resolve nötig, dann Idempotency-Key {} löschen.", tool_name, &key[..16]));
                    return (false, format!("AMBIGUOUS: Vorherige Ausführung von {} wurde unterbrochen. Unklar ob der Seiteneffekt stattfand. Manuelle Prüfung nötig; Retry nach DELETE FROM idempotency WHERE key='{}...'.", tool_name, &key[..16]));
                }
                tracing::info!("Idempotency cache-hit für {} ({}): skip re-execute", tool_name, &key[..16]);
                pipeline.log(modul_id, Some(tid), crate::types::LogTyp::Info,
                    &format!("Idempotency: {} bereits ausgeführt, return cached", tool_name));
                return (success, data);
            }
            // Pre-Mark IN_PROGRESS, BEVOR wir den side-effecting Call machen.
            let _ = crate::store::idempotency_mark_in_progress(&pipeline.store.pool, &key);
            Some(key)
        }
        _ => None,
    };

    // ══════ Audit-Trail ══════
    // Side-Effect-Tool-Calls vor Ausführung in audit_log-SQL-Tabelle schreiben.
    // Die Tabelle hat UPDATE/DELETE-Trigger die Modifikation verweigern —
    // append-only by DB-constraint, nicht by convention.
    if tool_has_side_effect(tool_name) {
        let params_preview = params.iter()
            .map(|p| crate::util::safe_truncate(p, 200))
            .collect::<Vec<_>>()
            .join(", ");
        pipeline.audit(
            "tool_exec",
            modul_id,
            &format!("{}({})", tool_name, crate::util::safe_truncate(&params_preview, 600)),
        );
    }

    // Eigentliche Tool-Execution in einer inner-fn damit wir am Ende einen
    // einzigen Exit-Punkt haben für idempotency_store.
    let result = exec_tool_unified_inner(
        tool_name, params, modul_id, pipeline, llm, py_modules, py_pool, config_snapshot,
    ).await;

    // ══════ Idempotency-Commit ══════
    // Pre-Mark wurde oben gesetzt. Jetzt:
    //   - Success → echtes Result überschreibt den Marker (cached für Retry)
    //   - Failure → Marker LÖSCHEN, damit ein späterer legitimer Retry (z.B.
    //     nach Config-Fix) neu versuchen darf. Failure-Results zu cachen würde
    //     den User festfahren.
    if let Some(key) = idempotency_key {
        if result.0 {
            let _ = crate::store::idempotency_store(&pipeline.store.pool, &key, true, &result.1);
        } else {
            let _ = crate::store::idempotency_delete(&pipeline.store.pool, &key);
        }
    }

    result
}

/// Inner Dispatcher ohne Idempotency/Audit — wird vom Wrapper `exec_tool_unified`
/// umhüllt. Getrennt damit der Idempotency-Commit am Ende in EINEM Exit-Pfad passiert.
async fn exec_tool_unified_inner(
    tool_name: &str,
    params: &[String],
    modul_id: &str,
    pipeline: &Pipeline,
    llm: &crate::llm::LlmRouter,
    py_modules: &[crate::loader::PyModuleMeta],
    py_pool: &crate::loader::PyProcessPool,
    config_snapshot: &AgentConfig,
) -> (bool, String) {
    // For RAG tools, pre-compute embedding if configured
    if tool_name == "rag.speichern" || tool_name == "rag.suchen" {
        let pool = config_snapshot.module.iter()
            .find(|m| m.id == modul_id || m.name == modul_id)
            .and_then(|m| m.rag_pool.as_deref())
            .unwrap_or("shared")
            .to_string();
        if let Some(embed_id) = config_snapshot.embedding_backend.clone() {
            let text = params.first().map(|s| s.as_str()).unwrap_or("");
            if tool_name == "rag.speichern" {
                let embedding = match llm.embed(&embed_id, text).await {
                    Ok(v) => Some(v),
                    Err(e) => { tracing::warn!("Embed: {}", e); None }
                };
                let result = crate::modules::rag::speichern(&pipeline.base, &pool, text, embedding, Some(embed_id)).await;
                return (result.success, result.data);
            } else {
                let query_vec = match llm.embed(&embed_id, text).await {
                    Ok(v) => Some(v),
                    Err(e) => { tracing::warn!("Embed: {}", e); None }
                };
                let result = crate::modules::rag::suchen(&pipeline.base, &pool, text, query_vec.as_deref()).await;
                return (result.success, result.data);
            }
        }
    }

    let modul = config_snapshot.module.iter()
        .find(|m| m.id == modul_id || m.name == modul_id)
        .cloned();

    if let Some(ref m) = modul {
        let result = execute_tool(tool_name, params, m, config_snapshot, pipeline).await;
        if result.success || !result.data.contains("Unbekanntes Tool") {
            return (result.success, result.data);
        }
    }

    if let Some(ref m) = modul {
        if !has_permission_with_py(m, tool_name, py_modules) {
            return (false, format!("DENIED: Modul '{}' hat keine Berechtigung für Tool '{}'", m.id, tool_name));
        }
    }
    let mut instance_config = modul.as_ref()
        .map(|m| serde_json::to_value(&m.settings).unwrap_or_default())
        .unwrap_or_default();
    let home = pipeline.home_dir(modul_id);
    if let serde_json::Value::Object(ref mut map) = instance_config {
        map.insert("home_dir".into(), serde_json::json!(home.to_string_lossy()));
    }
    if let Some(py_result) = execute_python_tool(tool_name, params, py_modules, &instance_config, py_pool).await {
        return (py_result.success, py_result.data);
    }

    (false, format!("Tool '{}' nicht gefunden", tool_name))
}

/// Fuehrt einen Tool-Call in einem Python-Modul aus (mit Permission-Check)
pub async fn execute_python_tool(
    tool_name: &str,
    params: &[String],
    py_modules: &[crate::loader::PyModuleMeta],
    instance_config: &serde_json::Value,
    py_pool: &crate::loader::PyProcessPool,
) -> Option<ToolResult> {
    for py_mod in py_modules {
        for tool in &py_mod.tools {
            if tool.name == tool_name {
                match py_pool.call(&py_mod.path, &py_mod.name, tool_name, params, instance_config).await {
                    Ok((success, data)) => {
                        return Some(if success { ToolResult::ok(data) } else { ToolResult::fail(data) });
                    }
                    Err(e) => {
                        // Pool call failed — fall back to one-shot spawn
                        match crate::loader::call_python_tool(&py_mod.path, tool_name, params, instance_config).await {
                            Ok((success, data)) => {
                                return Some(if success { ToolResult::ok(data) } else { ToolResult::fail(data) });
                            }
                            Err(e2) => {
                                tracing::warn!("Python pool failed ({}), one-shot also failed: {}", e, e2);
                                return Some(ToolResult::fail(format!("Python-Modul Fehler: {}", e2)));
                            }
                        }
                    }
                }
            }
        }
    }
    None // Kein Python-Modul hat dieses Tool
}

/// Check if a module has permission to use a tool
/// py_modules wird gebraucht um Tool→Modulname aufzuloesen
pub fn has_permission_with_py(modul: &ModulConfig, tool_name: &str, py_modules: &[crate::loader::PyModuleMeta]) -> bool {
    let perms = &modul.berechtigungen;
    // Fuer Python-Tools: finde den Modulnamen der dieses Tool hat
    for py_mod in py_modules {
        for tool in &py_mod.tools {
            if tool.name == tool_name {
                let perm = format!("py.{}", py_mod.name);
                // Exact match statt substring (war Bypass: "chat.mail" matched py_mod "mail").
                let has_perm = perms.iter().any(|p| p == &perm || p == "py.*")
                    || modul.linked_modules.iter().any(|link_id| {
                        link_id == &py_mod.name || link_id.starts_with(&format!("{}.", py_mod.name))
                    });
                return has_perm;
            }
        }
    }
    // Kein Python-Tool → Rust-Permission-Check
    has_permission(modul, tool_name)
}

fn has_permission(modul: &ModulConfig, tool_name: &str) -> bool {
    let perms = &modul.berechtigungen;
    // Typ-basierte Permission-Grants nur für persistent-Module. Für Temp-
    // Agents (persistent=false) gilt das typ-Feld NICHT als impliziter Grant,
    // sonst hätte der stripped_perms-Schutz in agent.spawn keine Wirkung auf
    // shell/filesystem/websearch/notify-Typen (Temp-Agent erbt basis.typ und
    // hätte trotz gestripter berechtigungen automatisch typ-basierten Zugriff
    // — GLM-Finding Run SQLite-6). Temp-Agents müssen jede Permission explizit
    // via `berechtigungen` haben.
    let typ_grants = modul.persistent;

    match tool_name {
        "rag.suchen" | "rag.speichern" => {
            perms.iter().any(|p| p.starts_with("rag."))
        }
        "aufgaben.erstellen" => {
            perms.iter().any(|p| p == "aufgaben")
        }
        "files.read" | "files.write" | "files.list" => {
            (typ_grants && modul.typ == "filesystem")
                || perms.iter().any(|p| p == "files" || p == "files.home" || p == "files.*")
        }
        "web.search" | "http.get" => {
            (typ_grants && modul.typ == "websearch") || perms.iter().any(|p| p == "websearch")
        }
        "shell.exec" => {
            (typ_grants && modul.typ == "shell") || perms.iter().any(|p| p == "shell")
        }
        "notify.send" => {
            (typ_grants && modul.typ == "notify") || perms.iter().any(|p| p == "notify")
        }
        "agent.spawn" => {
            // Nur persistent modules mit expliziter agent.spawn-Berechtigung dürfen spawnen.
            modul.persistent && perms.iter().any(|p| p == "agent.spawn" || p == "agent.*")
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{ModulSettings, ModulIdentity};

    fn make_modul(typ: &str, berechtigungen: Vec<String>) -> ModulConfig {
        ModulConfig {
            id: "test".into(),
            typ: typ.into(),
            name: "test".into(),
            display_name: "Test".into(),
            llm_backend: "x".into(),
            backup_llm: None,
            berechtigungen,
            timeout_s: 30,
            retry: 0,
            settings: ModulSettings::default(),
            identity: ModulIdentity::default(),
            rag_pool: None,
            linked_modules: vec![],
            persistent: true,
            spawned_by: None,
            spawn_ttl_s: None,
            created_at: None,
            scheduler_interval_ms: None,
            max_concurrent_tasks: None,
            token_budget: None,
            token_budget_warning: None,
        }
    }

    #[test]
    fn test_parse_tool_call_standard() {
        let input = "<tool>rag.suchen(Rust programming)</tool>";
        let (name, params) = parse_tool_call(input).unwrap();
        assert_eq!(name, "rag.suchen");
        assert_eq!(params, vec!["Rust programming"]);
    }

    #[test]
    fn test_parse_tool_call_with_text() {
        let input = "Ich werde jetzt suchen: <tool>web.search(test query)</tool>";
        let (name, params) = parse_tool_call(input).unwrap();
        assert_eq!(name, "web.search");
        assert_eq!(params, vec!["test query"]);
    }

    #[test]
    fn test_parse_tool_call_gemma_format() {
        let input = "<tool:rag.suchen(hello)/>";
        let (name, params) = parse_tool_call(input).unwrap();
        assert_eq!(name, "rag.suchen");
        assert_eq!(params, vec!["hello"]);
    }

    #[test]
    fn test_parse_tool_call_two_params() {
        let input = "<tool>files.write(/tmp/test.txt, hello world)</tool>";
        let (name, params) = parse_tool_call(input).unwrap();
        assert_eq!(name, "files.write");
        assert_eq!(params.len(), 2);
        assert_eq!(params[0], "/tmp/test.txt");
        assert_eq!(params[1], "hello world");
    }

    #[test]
    fn test_parse_tool_call_no_params() {
        let input = "<tool>sysinfo.overview()</tool>";
        let (name, params) = parse_tool_call(input).unwrap();
        assert_eq!(name, "sysinfo.overview");
        assert!(params.is_empty());
    }

    #[test]
    fn test_parse_tool_call_none_when_no_tool() {
        assert!(parse_tool_call("Just a normal message").is_none());
        assert!(parse_tool_call("").is_none());
    }

    #[test]
    fn test_parse_openai_tool_call_basic() {
        let data = serde_json::json!({
            "choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {
                "name": "files.read",
                "arguments": "{\"path\": \"/tmp/test.txt\"}"
            }}]}}]
        });
        let (name, params) = parse_openai_tool_call(&data).unwrap();
        assert_eq!(name, "files.read");
        assert_eq!(params, vec!["/tmp/test.txt"]);
    }

    #[test]
    fn test_parse_openai_tool_call_path_before_content() {
        // Ensure path-like params come before content params
        let data = serde_json::json!({
            "choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {
                "name": "editor.create",
                "arguments": "{\"inhalt\": \"file content here\", \"pfad\": \"/tmp/test.txt\"}"
            }}]}}]
        });
        let (name, params) = parse_openai_tool_call(&data).unwrap();
        assert_eq!(name, "editor.create");
        // pfad should come first, inhalt second
        assert_eq!(params[0], "/tmp/test.txt");
        assert!(params[1].contains("file content"));
    }

    #[test]
    fn test_schema_ordering_overrides_llm_keyorder() {
        // Regression (Round 5): mit Schema soll die required-Reihenfolge bindend sein,
        // NICHT die path_keys-Heuristik. Ein LLM das args in "nicht-standard" Reihen-
        // folge sendet ({content, path}) muss in Schema-Reihenfolge [path, content]
        // resultieren.
        let data = serde_json::json!({
            "choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {
                "name": "files.write",
                "arguments": "{\"content\": \"hello\", \"path\": \"/tmp/x.txt\"}"
            }}]}}]
        });
        let schema = vec!["path".to_string(), "content".to_string()];
        let (_, params) = parse_openai_tool_call_with_schema(&data, Some(&schema)).unwrap();
        assert_eq!(params[0], "/tmp/x.txt", "path muss erster Parameter sein (schema order)");
        assert_eq!(params[1], "hello");
    }

    #[test]
    fn test_schema_ordering_non_standard_keys() {
        // Wenn ein Tool Parameter "ziel" und "inhalt" hat (beides nicht in path_keys),
        // fällt die Heuristik auf Insertion-Order zurück und könnte fehlzuordnen.
        // Mit Schema ist die Reihenfolge trotzdem korrekt, auch bei vertauschten Keys.
        let data = serde_json::json!({
            "choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {
                "name": "custom.write",
                "arguments": "{\"inhalt\": \"payload\", \"ziel\": \"/safe/out\"}"
            }}]}}]
        });
        let schema = vec!["ziel".to_string(), "inhalt".to_string()];
        let (_, params) = parse_openai_tool_call_with_schema(&data, Some(&schema)).unwrap();
        assert_eq!(params[0], "/safe/out", "ziel muss erster Parameter sein");
        assert_eq!(params[1], "payload");
    }

    #[test]
    fn test_schema_ordering_missing_required_param_is_empty_string() {
        let data = serde_json::json!({
            "choices": [{"message": {"tool_calls": [{"id": "call_1", "function": {
                "name": "x.y",
                "arguments": "{\"a\": \"one\"}"
            }}]}}]
        });
        let schema = vec!["a".to_string(), "b".to_string(), "c".to_string()];
        let (_, params) = parse_openai_tool_call_with_schema(&data, Some(&schema)).unwrap();
        assert_eq!(params, vec!["one".to_string(), String::new(), String::new()]);
    }

    #[test]
    fn test_has_permission_rag() {
        let modul = make_modul("chat", vec!["rag.shared".into()]);
        assert!(has_permission(&modul, "rag.suchen"));
        assert!(has_permission(&modul, "rag.speichern"));
        assert!(!has_permission(&modul, "shell.exec"));
    }

    #[test]
    fn test_has_permission_files_requires_explicit_grant() {
        // Nach Least-Privilege-Fix: Modul ohne "files"/"files.home"/"files.*" und
        // nicht typ=="filesystem" darf KEINE Dateien anfassen. Schützt vor Prompt-
        // Injection-Bypass über Chat/Websearch/Notify-Modul.
        let chat_no_perm = make_modul("chat", vec![]);
        assert!(!has_permission(&chat_no_perm, "files.read"));
        assert!(!has_permission(&chat_no_perm, "files.write"));
        assert!(!has_permission(&chat_no_perm, "files.list"));

        let chat_with_home = make_modul("chat", vec!["files.home".into()]);
        assert!(has_permission(&chat_with_home, "files.read"));
        assert!(has_permission(&chat_with_home, "files.write"));

        let chat_with_full = make_modul("chat", vec!["files".into()]);
        assert!(has_permission(&chat_with_full, "files.read"));

        // typ==filesystem kriegt es weiterhin automatisch (ist ja die Kernfunktion)
        let fs = make_modul("filesystem", vec![]);
        assert!(has_permission(&fs, "files.read"));
    }

    fn py_mod(name: &str, tool_names: &[&str]) -> crate::loader::PyModuleMeta {
        crate::loader::PyModuleMeta {
            name: name.into(),
            description: "test".into(),
            version: "1.0".into(),
            settings: Default::default(),
            tools: tool_names.iter().map(|n| crate::loader::PyToolDef {
                name: (*n).into(),
                description: "t".into(),
                params: vec![],
            }).collect(),
            path: std::path::PathBuf::new(),
        }
    }

    #[test]
    fn test_has_permission_py_exact_match_only() {
        // Regression: used to be `link_id.contains(&py_mod.name)` which let
        // "chat.mail" grant access to py_mod "mail". Must be exact or "<name>." prefix.
        let mut modul = make_modul("chat", vec![]);
        modul.linked_modules = vec!["chat.mail".into()]; // NOT a link to py_mod "mail"

        let py_mods = vec![py_mod("mail", &["mail.send"])];
        assert!(!has_permission_with_py(&modul, "mail.send", &py_mods),
            "chat.mail link must NOT grant access to py.mail tools");
    }

    #[test]
    fn test_has_permission_py_substring_collision_blocked() {
        // py_mod "mail" — a link to "mailadmin.something" used to match (substring).
        let mut modul = make_modul("chat", vec![]);
        modul.linked_modules = vec!["mailadmin.inst1".into()];
        let py_mods = vec![py_mod("mail", &["mail.send"])];
        assert!(!has_permission_with_py(&modul, "mail.send", &py_mods),
            "'mailadmin' link must NOT match py_mod 'mail'");
    }

    #[test]
    fn test_has_permission_py_instance_prefix_grants() {
        // Link "mail.privat" SHOULD match py_mod "mail".
        let mut modul = make_modul("chat", vec![]);
        modul.linked_modules = vec!["mail.privat".into()];
        let py_mods = vec![py_mod("mail", &["mail.send"])];
        assert!(has_permission_with_py(&modul, "mail.send", &py_mods));
    }

    #[test]
    fn test_has_permission_py_exact_name_grants() {
        let mut modul = make_modul("chat", vec![]);
        modul.linked_modules = vec!["mail".into()]; // exactly the py_mod name
        let py_mods = vec![py_mod("mail", &["mail.send"])];
        assert!(has_permission_with_py(&modul, "mail.send", &py_mods));
    }

    #[test]
    fn test_has_permission_py_explicit_grant() {
        let modul = make_modul("chat", vec!["py.mail".into()]);
        let py_mods = vec![py_mod("mail", &["mail.send"])];
        assert!(has_permission_with_py(&modul, "mail.send", &py_mods));
    }

    #[test]
    fn test_typ_permission_does_not_leak_to_temp_agents() {
        // Regression: Temp-Agents (persistent=false) dürfen keine typ-basierten
        // impliziten Permission-Grants bekommen — sonst wäre der stripped_perms-
        // Schutz in agent.spawn wertlos für shell/filesystem/websearch/notify.
        // GLM-Finding Run SQLite-6.
        let mut temp_shell = make_modul("shell", vec![]);
        temp_shell.persistent = false;  // Temp-Agent
        assert!(!has_permission(&temp_shell, "shell.exec"),
            "Temp-Agent mit typ=shell ohne berechtigungen darf shell.exec NICHT");

        let mut temp_fs = make_modul("filesystem", vec![]);
        temp_fs.persistent = false;
        assert!(!has_permission(&temp_fs, "files.read"),
            "Temp-Agent mit typ=filesystem ohne berechtigungen darf files.read NICHT");

        let mut temp_web = make_modul("websearch", vec![]);
        temp_web.persistent = false;
        assert!(!has_permission(&temp_web, "web.search"),
            "Temp-Agent mit typ=websearch ohne berechtigungen darf web.search NICHT");

        let mut temp_notify = make_modul("notify", vec![]);
        temp_notify.persistent = false;
        assert!(!has_permission(&temp_notify, "notify.send"),
            "Temp-Agent mit typ=notify ohne berechtigungen darf notify.send NICHT");

        // Persistent (User-konfiguriert) ist OK
        let persistent_shell = make_modul("shell", vec![]);  // default persistent=true
        assert!(has_permission(&persistent_shell, "shell.exec"),
            "Persistent shell-Modul darf via typ shell.exec");

        // Temp-Agent MIT expliziter Permission darf trotzdem
        let mut temp_explicit = make_modul("chat", vec!["shell".into()]);
        temp_explicit.persistent = false;
        assert!(has_permission(&temp_explicit, "shell.exec"),
            "Temp-Agent mit expliziter shell-Permission darf");
    }
}
