# SECURE-Compartments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Mandanten-Isolation per benanntem `secure`-Compartment-Label durchsetzen — von außen kein Zugriff auf SECURE-Daten, SECURE koppelt nur mit SECURE-RAGs, kein Ausbrechen aus dem Home.

**Architecture:** Ein optionales `secure: Option<String>`-Label auf `ModulConfig` und `RagPool`. Durchsetzung „fail closed" an den vorhandenen In-Process-Choke-Points (Tool-Dispatch, Permission/Link, RAG-Binding, config-Übergabe, agent_meta) plus eine Startup-Validierung, die fehlkonfigurierte SECURE-Module deaktiviert. Keine Infra (OS-Sandbox = Phase 2, out of scope).

**Tech Stack:** Rust (axum, serde, tokio), Python-Module (IPC via stdin/stdout), `cargo test`.

**Spec:** `docs/superpowers/specs/2026-06-20-secure-compartments-design.md`

**Konvention:** Tests laufen mit `cargo test <filter>` (Binary-Crate `agent`). Commits pro Task. Wir sind auf Branch `main` → vor dem ersten Commit Feature-Branch `secure-compartments` anlegen (siehe Task 0).

---

### Task 0: Feature-Branch

- [ ] **Step 1: Branch anlegen**

```bash
cd /home/badmin/aistuff/agent
git checkout -b secure-compartments
```

- [ ] **Step 2: Baseline grün**

Run: `cargo test 2>&1 | tail -5`
Expected: bestehende Tests bestehen (kein neuer Code).

---

### Task 1: Datenmodell — `secure`-Feld + alle Literale

**Files:**
- Modify: `src/types.rs:241` (ModulConfig), `src/types.rs:464` (RagPool)
- Modify (Literale `secure: None` ergänzen): `src/util.rs:562,588,614`, `src/guardrail.rs:807`, `src/cycle.rs:2383`, `src/wizard.rs:980,2094`, `src/tools.rs:1521,2690`, `src/web.rs:7352`
- Test: `src/types.rs` (#[cfg(test)])

- [ ] **Step 1: Failing test (serde round-trip RagPool)**

In `src/types.rs` im (oder neuem) `#[cfg(test)] mod tests`:

```rust
#[test]
fn rag_pool_secure_roundtrips_and_defaults_none() {
    let without: RagPool = serde_json::from_str(
        r#"{"id":"p","name":"shared","typ":"Shared"}"#,
    ).unwrap();
    assert_eq!(without.secure, None);

    let with: RagPool = serde_json::from_str(
        r#"{"id":"p","name":"acme","typ":"Private","secure":"acme"}"#,
    ).unwrap();
    assert_eq!(with.secure.as_deref(), Some("acme"));
}
```

- [ ] **Step 2: Run — erwartet Compile-Fehler (Feld existiert nicht)**

Run: `cargo test rag_pool_secure_roundtrips 2>&1 | tail -15`
Expected: FAIL — `no field secure on type RagPool`.

- [ ] **Step 3: Feld in beide Structs einfügen**

`src/types.rs` nach `pub rag_pool: Option<String>,` (Z. 241) in `ModulConfig`:

```rust
    /// Compartment-Label (Mandanten-Isolation). None = public.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secure: Option<String>,
```

`src/types.rs` in `RagPool` nach `pub typ: RagTyp,` (Z. 464):

```rust
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub secure: Option<String>,
```

- [ ] **Step 4: Alle ModulConfig-Literale fixen**

In JEDER der folgenden `ModulConfig { … }`-Konstruktionen `secure: None,` ergänzen (Python-Temp-Agent und Wizard-Module starten public; SECURE wird per Config gesetzt):
`src/util.rs:562`, `:588`, `:614`; `src/guardrail.rs:807`; `src/cycle.rs:2383`; `src/wizard.rs:980`, `:2094`; `src/tools.rs:1521`, `:2690`; `src/web.rs:7352`.

Beispiel (Test-Helper `src/tools.rs:2690` `make_modul`): nach `rag_pool: None,` einfügen `secure: None,`.

- [ ] **Step 5: Run — grün**

Run: `cargo test rag_pool_secure_roundtrips 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -5` → kompiliert (alle Literale gefixt).

- [ ] **Step 6: Commit**

```bash
git add src/types.rs src/util.rs src/guardrail.rs src/cycle.rs src/wizard.rs src/tools.rs src/web.rs
git commit -m "feat(secure): add secure compartment label to ModulConfig + RagPool"
```

---

### Task 2: security.rs — Compartment-Helfer

**Files:**
- Modify: `src/security.rs` (neue Sektion + Tests)

- [ ] **Step 1: Failing tests**

In `src/security.rs` im `#[cfg(test)] mod tests`:

```rust
#[test]
fn test_access_allowed_matrix() {
    assert!(access_allowed(None, None));                    // public→public
    assert!(access_allowed(Some("acme"), Some("acme")));    // gleiche Zone
    assert!(!access_allowed(Some("acme"), Some("globex"))); // andere Zone
    assert!(!access_allowed(Some("acme"), None));           // Zone→public (kein Ausbruch)
    assert!(!access_allowed(None, Some("acme")));           // public→Zone (von außen)
}

#[test]
fn test_compartment_breaking_tools() {
    assert!(is_compartment_breaking_tool("agent.spawn"));
    assert!(is_compartment_breaking_tool("shell.exec"));
    assert!(is_compartment_breaking_tool("agent_meta.status"));
    assert!(is_compartment_breaking_tool("module_builder.create"));
    assert!(!is_compartment_breaking_tool("rag.suchen"));
    assert!(!is_compartment_breaking_tool("websearch"));
}
```

- [ ] **Step 2: Run — FAIL (Funktionen fehlen)**

Run: `cargo test test_access_allowed_matrix test_compartment_breaking_tools 2>&1 | tail -15`
Expected: FAIL — `cannot find function access_allowed`.

- [ ] **Step 3: Implementierung**

In `src/security.rs` neue Sektion (z. B. nach der Path-Sanitization-Sektion):

```rust
// ─── Compartments (secure tenant isolation) ───────────

/// Exakte Tool-Namen, die ein SECURE-Compartment brechen (NIE für secure actor).
pub const COMPARTMENT_BREAKING_TOOLS: &[&str] = &["agent.spawn", "shell.exec"];

/// Modul-Familien (Prefix `name.`), die ein Compartment brechen.
pub const COMPARTMENT_BREAKING_PREFIXES: &[&str] = &["agent_meta.", "module_builder."];

/// True, wenn `tool_name` innerhalb einer SECURE-Zone verboten ist.
pub fn is_compartment_breaking_tool(tool_name: &str) -> bool {
    COMPARTMENT_BREAKING_TOOLS.contains(&tool_name)
        || COMPARTMENT_BREAKING_PREFIXES
            .iter()
            .any(|p| tool_name.starts_with(p))
}

/// Zugriff nur innerhalb desselben Compartments. None = public.
/// public↔public erlaubt; jeder Cross-Label-Zugriff (auch public↔secure) verboten.
pub fn access_allowed(actor: Option<&str>, target: Option<&str>) -> bool {
    actor == target
}
```

- [ ] **Step 4: Run — PASS**

Run: `cargo test test_access_allowed_matrix test_compartment_breaking_tools 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/security.rs
git commit -m "feat(secure): compartment access helpers in security.rs"
```

---

### Task 3: RAG fail-closed (R3)

**Files:**
- Modify: `src/tools.rs:1180-1192` (`rag.suchen` + `rag.speichern`), Tests in `src/tools.rs`

- [ ] **Step 1: Failing test**

In `src/tools.rs` `#[cfg(test)] mod tests` (nutzt `make_modul`):

```rust
#[test]
fn secure_rag_pool_resolution_fails_closed() {
    use crate::types::RagPool;
    let pools = vec![
        RagPool { id: "acme".into(), name: "acme".into(), typ: crate::types::RagTyp::Private, secure: Some("acme".into()) },
        RagPool { id: "shared".into(), name: "shared".into(), typ: crate::types::RagTyp::Shared, secure: None },
    ];

    // public Modul → unverändert: Fallback auf bound/"shared"
    let mut public = make_modul("chat", vec!["rag.shared".into()]);
    public.rag_pool = Some("shared".into());
    assert_eq!(resolve_rag_pool(&public, &pools).as_deref(), Ok("shared"));

    // secure Modul mit passendem Pool → ok
    let mut sec_ok = make_modul("chat", vec![]);
    sec_ok.secure = Some("acme".into());
    sec_ok.rag_pool = Some("acme".into());
    assert_eq!(resolve_rag_pool(&sec_ok, &pools).as_deref(), Ok("acme"));

    // secure Modul, Pool fehlt → Fehler (kein shared-Fallback)
    let mut sec_nopool = make_modul("chat", vec![]);
    sec_nopool.secure = Some("acme".into());
    sec_nopool.rag_pool = None;
    assert!(resolve_rag_pool(&sec_nopool, &pools).is_err());

    // secure Modul, Pool nicht secure / falsches Label → Fehler
    let mut sec_wrong = make_modul("chat", vec![]);
    sec_wrong.secure = Some("acme".into());
    sec_wrong.rag_pool = Some("shared".into());
    assert!(resolve_rag_pool(&sec_wrong, &pools).is_err());
}
```

- [ ] **Step 2: Run — FAIL (`resolve_rag_pool` fehlt)**

Run: `cargo test secure_rag_pool_resolution_fails_closed 2>&1 | tail -15`
Expected: FAIL — `cannot find function resolve_rag_pool`.

- [ ] **Step 3: Implementierung**

In `src/tools.rs` (Modul-Ebene, vor `execute_tool`):

```rust
/// Liefert den zu benutzenden RAG-Pool-Namen — fail closed für SECURE-Module.
/// public: heutiges Verhalten (bound oder "shared"). secure: Pool MUSS gesetzt,
/// existieren und dasselbe Label tragen — sonst Err (kein shared-Fallback).
pub fn resolve_rag_pool<'a>(
    modul: &'a crate::types::ModulConfig,
    pools: &'a [crate::types::RagPool],
) -> Result<String, String> {
    match modul.secure.as_deref() {
        None => Ok(modul.rag_pool.as_deref().unwrap_or("shared").to_string()),
        Some(label) => {
            let pool_id = modul.rag_pool.as_deref().ok_or_else(|| {
                format!("DENIED: secure-Modul '{}' hat keinen RAG-Pool (kein shared-Fallback)", modul.id)
            })?;
            let pool = pools.iter().find(|p| p.id == pool_id).ok_or_else(|| {
                format!("DENIED: RAG-Pool '{}' existiert nicht", pool_id)
            })?;
            if pool.secure.as_deref() == Some(label) {
                Ok(pool_id.to_string())
            } else {
                Err(format!(
                    "DENIED: secure-Modul '{}' (Zone {}) darf nicht auf Pool '{}' (Zone {:?})",
                    modul.id, label, pool_id, pool.secure
                ))
            }
        }
    }
}
```

Dann `rag.suchen`/`rag.speichern` (Z. ~1182/1188) umstellen — statt `let pool = modul.rag_pool.as_deref().unwrap_or("shared");`:

```rust
        "rag.suchen" => {
            let query = params.first().map(|s| s.as_str()).unwrap_or("");
            let pool = match resolve_rag_pool(modul, &config.rag_pools) {
                Ok(p) => p,
                Err(e) => return ToolResult::fail(e),
            };
            modules::rag::suchen(&pipeline.base, &pool, query, None).await
        }
        "rag.speichern" => {
            let text = params.first().map(|s| s.as_str()).unwrap_or("");
            let pool = match resolve_rag_pool(modul, &config.rag_pools) {
                Ok(p) => p,
                Err(e) => return ToolResult::fail(e),
            };
            modules::rag::speichern(&pipeline.base, &pool, text, None, None).await
        }
```

- [ ] **Step 4: Run — PASS**

Run: `cargo test secure_rag_pool_resolution_fails_closed 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add src/tools.rs
git commit -m "feat(secure): RAG binding fails closed for secure modules (R3)"
```

---

### Task 4: Dispatch-Gate (R1 + R4)

**Files:**
- Modify: `src/tools.rs:2286-2319` (Block um `has_permission_with_py` / `settings_module`)
- Test: `src/tools.rs`

Kontext: actor = `modul` (Option<&ModulConfig>), target = `settings_module` (Option<&ModulConfig>, ab Z. 2297). Built-in/eigenes Tool → `settings_module=None` → target-Label = actor-Label (gleiche Zone, erlaubt; FS via R5 begrenzt).

- [ ] **Step 1: Failing test**

```rust
#[test]
fn dispatch_gate_blocks_cross_zone_and_breaking_tools() {
    let mut acme = make_modul("chat", vec![]);
    acme.id = "chat.acme".into();
    acme.secure = Some("acme".into());

    let public_target = make_modul("websearch", vec![]); // secure=None
    let mut acme_target = make_modul("rss_verwaltung", vec![]);
    acme_target.secure = Some("acme".into());

    // acme → public-Tool: verboten
    assert!(!compartment_call_allowed(Some(&acme), Some(&public_target), "rss.fetch"));
    // acme → acme-Tool: erlaubt
    assert!(compartment_call_allowed(Some(&acme), Some(&acme_target), "rss.fetch"));
    // acme → compartment-brechendes Tool: verboten (auch ohne target)
    assert!(!compartment_call_allowed(Some(&acme), None, "agent.spawn"));
    assert!(!compartment_call_allowed(Some(&acme), None, "agent_meta.status"));
    // public → public: erlaubt
    let pub_actor = make_modul("chat", vec![]);
    assert!(compartment_call_allowed(Some(&pub_actor), Some(&public_target), "websearch"));
}
```

- [ ] **Step 2: Run — FAIL**

Run: `cargo test dispatch_gate_blocks_cross_zone 2>&1 | tail -15`
Expected: FAIL — `cannot find function compartment_call_allowed`.

- [ ] **Step 3: Implementierung — Helper + Einbau**

Helper (Modul-Ebene in `src/tools.rs`):

```rust
/// R1+R4: Darf `actor` `tool_name` auf `target` aufrufen?
/// target=None ⇒ eigenes/built-in Tool (Ziel-Zone = actor-Zone).
pub fn compartment_call_allowed(
    actor: Option<&crate::types::ModulConfig>,
    target: Option<&crate::types::ModulConfig>,
    tool_name: &str,
) -> bool {
    let actor_zone = actor.and_then(|m| m.secure.as_deref());
    if actor_zone.is_some() && crate::security::is_compartment_breaking_tool(tool_name) {
        return false; // R4
    }
    let target_zone = target.and_then(|m| m.secure.as_deref()).or(actor_zone);
    crate::security::access_allowed(actor_zone, target_zone) // R1
}
```

Einbau in `execute_tool_with_modules` direkt nach der Auflösung von `settings_module` (nach Z. ~2298, vor der Config-Zusammenstellung):

```rust
    if !compartment_call_allowed(modul.as_ref(), settings_module, tool_name) {
        let who = modul.as_ref().map(|m| m.id.as_str()).unwrap_or("?");
        return (
            false,
            format!("DENIED: Compartment-Grenze — '{}' darf Tool '{}' nicht aufrufen", who, tool_name),
        );
    }
```

- [ ] **Step 4: Run — PASS**

Run: `cargo test dispatch_gate_blocks_cross_zone 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add src/tools.rs
git commit -m "feat(secure): dispatch gate blocks cross-zone + breaking tools (R1/R4)"
```

---

### Task 5: Cross-Zone-Links (R2)

**Files:**
- Modify: `src/tools.rs:2607-2629` (`has_permission_with_py`)
- Test: `src/tools.rs`

Ziel: Ein `linked_modules`-Eintrag zählt nur als Permission, wenn das verlinkte Modul dieselbe Zone hat. Da `has_permission_with_py` heute keine Modul-Liste kennt, erweitern wir die Signatur um `config: &AgentConfig` (Lookup der Link-Zonen). Aufrufer in `tools.rs:2287` mitziehen.

- [ ] **Step 1: Failing test**

```rust
#[test]
fn cross_zone_link_is_not_permission() {
    // py-Modul "rss_verwaltung" existiert als Tool-Provider
    let py = vec![crate::loader::PyModuleMeta {
        name: "rss_verwaltung".into(), version: "1".into(), path: std::path::PathBuf::new(),
        tools: vec![crate::loader::PyToolMeta { name: "rss_verwaltung.fetch".into(), description: String::new(), params: vec![] }],
    }];
    let mut cfg = crate::types::AgentConfig::default();
    // public Link-Ziel
    let mut pub_link = make_modul("rss_verwaltung", vec![]);
    pub_link.id = "rss_verwaltung.default".into();
    cfg.module.push(pub_link);

    let mut acme = make_modul("chat", vec![]);
    acme.secure = Some("acme".into());
    acme.linked_modules = vec!["rss_verwaltung.default".into()];

    // acme verlinkt ein PUBLIC Modul → keine Permission
    assert!(!has_permission_with_py(&acme, "rss_verwaltung.fetch", &py, &cfg));
}
```

> Falls `AgentConfig`/`PyModuleMeta`/`PyToolMeta` keine `Default`/öffentlichen Felder haben: minimalen Konstruktor analog vorhandener Tests verwenden (siehe `src/loader.rs` Test-Helfer).

- [ ] **Step 2: Run — FAIL (Signatur/Compile)**

Run: `cargo test cross_zone_link_is_not_permission 2>&1 | tail -15`
Expected: FAIL.

- [ ] **Step 3: Implementierung**

`has_permission_with_py` Signatur erweitern und Link-Match um einen Zonen-Check ergänzen:

```rust
pub fn has_permission_with_py(
    modul: &ModulConfig,
    tool_name: &str,
    py_modules: &[crate::loader::PyModuleMeta],
    config: &AgentConfig,
) -> bool {
    let perms = &modul.berechtigungen;
    for py_mod in py_modules {
        for tool in &py_mod.tools {
            if tool.name == tool_name {
                let perm = format!("py.{}", py_mod.name);
                let actor_zone = modul.secure.as_deref();
                let link_ok = modul.linked_modules.iter().any(|link_id| {
                    let matches_name = link_id == &py_mod.name
                        || link_id.starts_with(&format!("{}.", py_mod.name));
                    if !matches_name { return false; }
                    // R2: Link-Ziel muss dieselbe Zone haben
                    let link_zone = config
                        .module.iter().find(|m| &m.id == link_id)
                        .and_then(|m| m.secure.as_deref());
                    crate::security::access_allowed(actor_zone, link_zone)
                });
                let has_perm = perms.iter().any(|p| p == &perm || p == "py.*") || link_ok;
                return has_perm;
            }
        }
    }
    has_permission(modul, tool_name)
}
```

Aufrufer `src/tools.rs:2287` anpassen: `if !has_permission_with_py(m, tool_name, py_modules, config_snapshot) {`.

- [ ] **Step 4: Run — PASS**

Run: `cargo test cross_zone_link_is_not_permission 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add src/tools.rs
git commit -m "feat(secure): cross-zone links no longer grant tool permission (R2)"
```

---

### Task 6: config-Übergabe — Home-only-Marker (R5)

**Files:**
- Modify: `src/tools.rs:2303-2347` (Config-Zusammenstellung)
- Test: `src/tools.rs` (Helper extrahieren für Testbarkeit)

- [ ] **Step 1: Failing test**

```rust
#[test]
fn secure_module_config_is_home_confined() {
    let mut acme = make_modul("chat", vec![]);
    acme.secure = Some("acme".into());
    let mut map = serde_json::Map::new();
    apply_secure_markers(&mut map, Some(&acme));
    assert_eq!(map.get("secure").and_then(|v| v.as_str()), Some("acme"));
    assert_eq!(map.get("confine_home_only").and_then(|v| v.as_bool()), Some(true));

    let public = make_modul("chat", vec![]);
    let mut map2 = serde_json::Map::new();
    apply_secure_markers(&mut map2, Some(&public));
    assert!(map2.get("secure").is_none());
    assert!(map2.get("confine_home_only").is_none());
}
```

- [ ] **Step 2: Run — FAIL**

Run: `cargo test secure_module_config_is_home_confined 2>&1 | tail -15`
Expected: FAIL — `cannot find function apply_secure_markers`.

- [ ] **Step 3: Implementierung**

Helper (Modul-Ebene):

```rust
/// R5: secure-Module bekommen Home-only-Marker; project_root/modules_dir
/// werden vom Aufrufer für secure NICHT injiziert.
pub fn apply_secure_markers(
    map: &mut serde_json::Map<String, serde_json::Value>,
    modul: Option<&crate::types::ModulConfig>,
) {
    if let Some(label) = modul.and_then(|m| m.secure.as_deref()) {
        map.insert("secure".into(), serde_json::json!(label));
        map.insert("confine_home_only".into(), serde_json::json!(true));
    }
}
```

In `tools.rs:2304` (innerhalb `if let serde_json::Value::Object(ref mut map)`): die `project_root`/`modules_dir`-Inserts (Z. 2320-2328) in `if modul.as_ref().and_then(|m| m.secure.as_deref()).is_none() { … }` wrappen, und am Ende des Blocks `apply_secure_markers(map, modul.as_ref());` aufrufen.

- [ ] **Step 4: Run — PASS**

Run: `cargo test secure_module_config_is_home_confined 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add src/tools.rs
git commit -m "feat(secure): home-only markers + no project_root for secure modules (R5)"
```

---

### Task 7: agent_meta nie für secure (Verstärkung R4)

**Files:**
- Modify: `src/tools.rs:2388` (`execute_python_tool`, agent_meta-Block)
- Test: durch Task 4 (`compartment_call_allowed` blockt `agent_meta.*`) bereits abgedeckt; hier nur Defense-in-Depth.

- [ ] **Step 1: Guard einbauen**

In `execute_python_tool`, bevor `platform_config` für `agent_meta` gebaut wird (Z. ~2388), wenn das Tool über einen secure-Pfad käme, keinen elevated snapshot bauen. Da `execute_python_tool` `instance_config` (mit `secure`-Marker aus Task 6) erhält:

```rust
                let actor_is_secure = instance_config
                    .get("secure").and_then(|v| v.as_str()).is_some();
                let platform_config = if py_mod.name == "agent_meta" && !actor_is_secure {
                    // … bestehender elevated-snapshot-Block …
```

(Erreicht agent_meta secure dennoch, liefert es nur das normale `instance_config` ohne `config_snapshot`/`api_auth_token`. Der harte Block sitzt in Task 4.)

- [ ] **Step 2: Build + bestehende Tests grün**

Run: `cargo build 2>&1 | tail -3 && cargo test 2>&1 | tail -5`
Expected: kompiliert, Tests grün.

- [ ] **Step 3: Commit**

```bash
git add src/tools.rs
git commit -m "feat(secure): never hand agent_meta elevation to a secure actor"
```

---

### Task 8: validate_compartments + Startup-Deaktivierung

**Files:**
- Modify: `src/security.rs` (Validator + Typen + Tests)
- Modify: `src/cycle.rs:380-385` (blockierte Module nicht scheduling)
- Modify: `src/main.rs` (nach Config-Load: validieren + loggen)

- [ ] **Step 1: Failing test**

In `src/security.rs` tests:

```rust
#[test]
fn validate_flags_misconfigured_secure_module() {
    use crate::types::{AgentConfig, RagPool, RagTyp};
    let mut cfg = AgentConfig::default();
    cfg.rag_pools = vec![RagPool { id: "acme".into(), name: "acme".into(), typ: RagTyp::Private, secure: Some("acme".into()) }];

    // sauberes secure-Modul
    let mut good = test_modul("chat.acme");
    good.secure = Some("acme".into());
    good.rag_pool = Some("acme".into());
    // kaputtes: kein secure RAG
    let mut bad = test_modul("chat.bad");
    bad.secure = Some("acme".into());
    bad.rag_pool = None;
    cfg.module = vec![good, bad];

    let v = validate_compartments(&cfg);
    let blocked = blocked_module_ids(&cfg);
    assert!(v.iter().any(|x| x.module_id == "chat.bad" && x.severity == Severity::Error));
    assert!(!v.iter().any(|x| x.module_id == "chat.acme" && x.severity == Severity::Error));
    assert!(blocked.contains("chat.bad"));
    assert!(!blocked.contains("chat.acme"));
}
```

> `test_modul(id)` als `#[cfg(test)]`-Helfer in `security.rs` analog zu `tools.rs::make_modul` ergänzen (Voll-Literal mit `secure: None`).

- [ ] **Step 2: Run — FAIL**

Run: `cargo test validate_flags_misconfigured_secure_module 2>&1 | tail -15`
Expected: FAIL — Typen/Funktionen fehlen.

- [ ] **Step 3: Implementierung**

In `src/security.rs`:

```rust
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Severity { Error, Warning }

#[derive(Debug, Clone)]
pub struct CompartmentViolation {
    pub module_id: String,
    pub severity: Severity,
    pub message: String,
}

/// Prüft alle secure-Module gegen die Invarianten. Errors ⇒ Modul wird nicht aktiviert.
pub fn validate_compartments(cfg: &AgentConfig) -> Vec<CompartmentViolation> {
    let mut out = vec![];
    for m in &cfg.module {
        let Some(zone) = m.secure.as_deref() else { continue };
        // R3: secure RAG-Pool
        match m.rag_pool.as_deref() {
            None => out.push(CompartmentViolation { module_id: m.id.clone(), severity: Severity::Error,
                message: format!("secure-Modul ohne RAG-Pool (Zone {zone})") }),
            Some(pid) => match cfg.rag_pools.iter().find(|p| p.id == pid) {
                None => out.push(CompartmentViolation { module_id: m.id.clone(), severity: Severity::Error,
                    message: format!("RAG-Pool '{pid}' existiert nicht") }),
                Some(p) if p.secure.as_deref() != Some(zone) => out.push(CompartmentViolation {
                    module_id: m.id.clone(), severity: Severity::Error,
                    message: format!("RAG-Pool '{pid}' nicht in Zone {zone}") }),
                _ => {}
            },
        }
        // R2: Cross-Zone-Links
        for link in &m.linked_modules {
            if let Some(lm) = cfg.module.iter().find(|x| &x.id == link) {
                if lm.secure.as_deref() != Some(zone) {
                    out.push(CompartmentViolation { module_id: m.id.clone(), severity: Severity::Error,
                        message: format!("Cross-Zone-Link auf '{link}' (Zone {:?})", lm.secure) });
                }
            }
        }
        // R4: compartment-brechende Tools in Permissions
        for perm in &m.berechtigungen {
            if is_compartment_breaking_tool(perm) {
                out.push(CompartmentViolation { module_id: m.id.clone(), severity: Severity::Error,
                    message: format!("compartment-brechende Permission '{perm}'") });
            }
        }
        // WARN: nicht-lokales LLM-Backend
        if let Some(b) = cfg.llm_backends.iter().find(|b| b.id == m.llm_backend) {
            if validate_llm_backend_url(&b.typ, &b.url).is_ok() && !is_self_hosted_llm_url(&b.url) {
                out.push(CompartmentViolation { module_id: m.id.clone(), severity: Severity::Warning,
                    message: format!("nicht-lokales LLM-Backend '{}' — Prompts verlassen die Box", m.llm_backend) });
            }
        }
    }
    out
}

/// Modul-IDs mit Error-Verstoß (werden nicht aktiviert).
pub fn blocked_module_ids(cfg: &AgentConfig) -> std::collections::HashSet<String> {
    validate_compartments(cfg).into_iter()
        .filter(|v| v.severity == Severity::Error)
        .map(|v| v.module_id).collect()
}
```

> `LlmBackend`-Felder (`id`, `typ`, `url`) ggf. an die echte Struct anpassen (siehe `src/types.rs` `LlmBackend`).

In `src/cycle.rs:380` die `modul_ids`-Sammlung um den Block ergänzen:

```rust
            let blocked = crate::security::blocked_module_ids(&cfg);
            let modul_ids: Vec<String> = cfg.module.iter()
                .filter(|m| m.typ != "enhancer")
                .filter(|m| !blocked.contains(&m.id))
                .map(|m| m.id.clone()).collect();
```

In `src/main.rs` nach dem Laden der Config: über `validate_compartments(&config)` iterieren und jede Violation via `tracing::error!`/`warn!` loggen (Errors zusätzlich: „Modul deaktiviert").

- [ ] **Step 4: Run — PASS**

Run: `cargo test validate_flags_misconfigured_secure_module 2>&1 | tail -5`
Expected: PASS. Dann `cargo build 2>&1 | tail -3`.

- [ ] **Step 5: Commit**

```bash
git add src/security.rs src/cycle.rs src/main.rs
git commit -m "feat(secure): startup validator disables misconfigured secure modules"
```

---

### Task 9: Python coding — confine_home_only

**Files:**
- Modify: `modules/coding/module.py:954-978` (`home_dir`/Pfad-Resolver)
- Test: `modules/coding/` ad-hoc (Python) oder manueller IPC-Test

- [ ] **Step 1: Failing test (Python, ad-hoc)**

`modules/coding/test_confine.py`:

```python
import json, subprocess, sys, os, tempfile
def call(payload):
    p = subprocess.run([sys.executable, "modules/coding/module.py"],
        input=json.dumps(payload)+"\n", capture_output=True, text=True, timeout=30)
    return p.stdout
home = tempfile.mkdtemp()
# secure: allow_project_root muss ignoriert werden
out = call({"action":"handle_tool","tool":"coding.read",
    "params":["/etc/hostname"],
    "config":{"home_dir":home,"project_root":"/","allow_project_root":True,
              "secure":"acme","confine_home_only":True}})
assert "/etc/hostname" not in out or "DENIED" in out or "außerhalb" in out.lower(), out
print("OK")
```

- [ ] **Step 2: Run — FAIL (liest evtl. /etc/hostname)**

Run: `python3 modules/coding/test_confine.py`
Expected: AssertionError (oder Datei-Inhalt sichtbar).

- [ ] **Step 3: Implementierung**

In `modules/coding/module.py` dort, wo `allow_project_root` ausgewertet wird (Z. ~969/977): wenn `cfg_bool(config.get("confine_home_only"), False)` true ist, `allow_project_root` als `False` behandeln (kein Projekt-Root, nur `home_dir`).

- [ ] **Step 4: Run — PASS**

Run: `python3 modules/coding/test_confine.py`
Expected: `OK`.

- [ ] **Step 5: Commit**

```bash
git add modules/coding/module.py modules/coding/test_confine.py
git commit -m "feat(secure): coding module honors confine_home_only"
```

---

### Task 10: Web — Compartment-Status-Endpoint + Badge

**Files:**
- Modify: `src/web.rs:1807` (Route registrieren), neue Handler-Funktion in `src/web.rs`
- Modify: `src/chat.html` (Badge im Panel)

- [ ] **Step 1: Handler + Route**

Handler in `src/web.rs`:

```rust
/// Compartment-Übersicht + Validierungs-Verstöße fürs UI.
async fn security_compartments(State(s): State<Arc<AppState>>) -> Json<serde_json::Value> {
    let cfg = s.config.read().await;
    let violations: Vec<serde_json::Value> = crate::security::validate_compartments(&cfg)
        .into_iter().map(|v| serde_json::json!({
            "module_id": v.module_id,
            "severity": format!("{:?}", v.severity),
            "message": v.message,
        })).collect();
    let zones: Vec<serde_json::Value> = cfg.module.iter()
        .filter_map(|m| m.secure.as_deref().map(|z| serde_json::json!({"id": m.id, "zone": z})))
        .collect();
    Json(serde_json::json!({"zones": zones, "violations": violations}))
}
```

Route bei den `/api/planner/*`-Routen (Z. 1807) ergänzen:

```rust
        .route("/api/security/compartments", axum::routing::get(security_compartments))
```

- [ ] **Step 2: Run — manuell prüfen**

Run: `cargo build 2>&1 | tail -3`
Dann (Agent läuft): `curl -s localhost:<web_port>/api/security/compartments -H "Authorization: Bearer <token>" | jq .`
Expected: `{"zones":[...],"violations":[...]}`.

- [ ] **Step 3: Badge im Chat-Panel**

In `src/chat.html` dort, wo Instanz-Infos gerendert werden: wenn die Instanz ein `secure`-Label hat, „🔒 <zone>"-Badge anzeigen (Daten aus `/api/security/compartments`).

- [ ] **Step 4: Playwright-Check (Memory-Regel: Frontend im Browser prüfen)**

Browser auf `localhost:<web_port>` öffnen, Panel laden, Badge sichtbar wenn eine Zone konfiguriert ist.

- [ ] **Step 5: Commit**

```bash
git add src/web.rs src/chat.html
git commit -m "feat(secure): /api/security/compartments endpoint + zone badge"
```

---

### Task 11: Integrationstest acme vs public

**Files:**
- Create: `src/tests_secure.rs` (oder in `src/tools.rs` tests) — End-to-End über `execute_tool`/`compartment_call_allowed`/`resolve_rag_pool`

- [ ] **Step 1: Test**

```rust
#[test]
fn acme_and_public_are_isolated() {
    use crate::types::{RagPool, RagTyp};
    let pools = vec![
        RagPool { id: "acme".into(), name: "acme".into(), typ: RagTyp::Private, secure: Some("acme".into()) },
        RagPool { id: "shared".into(), name: "shared".into(), typ: RagTyp::Shared, secure: None },
    ];
    let mut acme = make_modul("chat", vec![]);
    acme.id = "chat.acme".into(); acme.secure = Some("acme".into()); acme.rag_pool = Some("acme".into());
    let mut pubc = make_modul("chat", vec![]);
    pubc.id = "chat.pub".into(); pubc.rag_pool = Some("shared".into());

    // RAG: acme nur acme-Pool, public nur shared
    assert_eq!(resolve_rag_pool(&acme, &pools).as_deref(), Ok("acme"));
    assert_eq!(resolve_rag_pool(&pubc, &pools).as_deref(), Ok("shared"));

    // public darf acme-Tool nicht rufen, acme darf public-Tool nicht rufen
    let acme_tool = { let mut m = make_modul("rss_verwaltung", vec![]); m.secure = Some("acme".into()); m };
    let pub_tool = make_modul("rss_verwaltung", vec![]);
    assert!(!compartment_call_allowed(Some(&pubc), Some(&acme_tool), "rss.fetch"));
    assert!(!compartment_call_allowed(Some(&acme), Some(&pub_tool), "rss.fetch"));
    // gleiche Zone ok
    assert!(compartment_call_allowed(Some(&acme), Some(&acme_tool), "rss.fetch"));
}
```

- [ ] **Step 2: Run — PASS**

Run: `cargo test acme_and_public_are_isolated 2>&1 | tail -5`
Expected: PASS.

- [ ] **Step 3: Full suite grün**

Run: `cargo test 2>&1 | tail -8`
Expected: alle Tests grün.

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "test(secure): end-to-end acme vs public isolation"
```

---

## Self-Review-Ergebnis (gegen Spec)

- **§3 Datenmodell** → Task 1. ✓
- **§4 R1 Dispatch** → Task 4. **R2 Links** → Task 5. **R3 RAG** → Task 3. **R4 Deny-Tools** → Task 4 + Task 7 (agent_meta) + Task 8 (Validator). **R5 FS** → Task 6 (Marker) + Task 9 (Python). ✓
- **§6 Fail-closed Validierung** → Task 8. ✓
- **§7 Python-Seite** → Task 9. ✓
- **§8 UI** → Task 10. ✓
- **§9 Tests** → Tasks 2–8, 11. ✓
- **§10 acme-Beispiel** → Task 11 (als Test). ✓
- **§11 Telegram/Web-Eingang als actor** → offen (separater Task in einer Folge-Iteration; HTTP-Eingang als `access_allowed`-Check). Bewusst nicht in dieser Stufe, da Eingangs-Routing eigene Analyse braucht.

**Offene Annahmen (in Execution verifizieren):** exakte `LlmBackend`-Feldnamen (Task 8), `AgentConfig::default()`/`PyModuleMeta`-Konstruktion in Tests (Task 5/8), genauer Einfügepunkt der project_root-Inserts (Task 6).
