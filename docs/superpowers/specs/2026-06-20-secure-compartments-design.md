# SECURE-Compartments — Design-Spec

**Datum:** 2026-06-20
**Status:** Freigegeben (Design), Implementierung ausstehend
**Ziel:** Mandanten-/Zonen-Isolation für ein Firmen-LLM auf dem bestehenden Harness. Markiere ich etwas als `secure`, darf von **außen** keine andere LLM auf dessen Daten/Ordner/RAG zugreifen; SECURE-Dinge koppeln nur mit ebenfalls-SECURE-Ressourcen; kein Ausbrechen aus der Ordnerstruktur.

---

## 1. Kontext & Ist-Zustand (Audit-Zusammenfassung)

Das Harness hat **gute Pro-Modul-Primitive**:

- Module bekommen nur ihre **eigenen** `settings` (nicht die volle config) — `tools.rs:2297`.
- Secrets nur per Alias (`${api.X}` / `${cred.X.Y}`) aus den eigenen Settings, **mit Audit-Log** (`audit_*_vault_uses`, `tools.rs:2356`).
- Tool-ACL pro Modul (`py.<name>` / `linked_modules`); **kein** Modul nutzt `py.*`.
- Pro-Modul-Home `agent-data/home/<id>/` (sanitized) — `pipeline.rs:369`.
- RAG hart an **einen** Pool gebunden, kein Pool-Override über Params — `tools.rs:1182`.
- FS-Confinement in built-ins: `coding`/`filesystem` via realpath+`commonpath`, `shell.exec` nur Whitelist.
- Starke Außengrenze: Bearer-Auth, SSRF-Block, Secret-Redaction, Rate-Limit (`security.rs`).

**Die Lecks, die ein Firmen-Tenant nicht verträgt:**

1. **Geteilter RAG-Pool** — 5/6 Chats → `DeepDive`, `chat.thomas` → `shared`. Daten bluten zwischen LLMs.
2. **Cross-Links** zwischen Chats + fremden Tool-Instanzen (z. B. `chat.thomas` → `chat.grok`).
3. **`agent_meta` breit verlinkt** — liefert `config_snapshot` + `api_auth_token` + alle Instanzen (`tools.rs:2388`).
4. **Kein Compartment-Begriff** — Isolation ist heute *pro Modul*, nicht *pro Zone*. Keine harte Wand.
5. **`data_dir` (geteilte Basis) geht an jedes Modul** — FS-Trennung ist nur kooperativ.

---

## 2. Bedrohungsmodell & Scope

**Phase 1 (dieser Spec) — logische Durchsetzung.** Gegner = eine LLM, die über ihre **erlaubten Tools** versucht, fremde Daten zu lesen/zu exfiltrieren oder aus dem Home auszubrechen. Durchsetzung an den vorhandenen In-Process-Choke-Points. Keine Infra-Änderung.

**Phase 2 (NICHT in diesem Spec) — OS-Sandbox.** Separate uid / Namespaces / seccomp pro Zone gegen bösartigen Modul-*Code* (ein Python-Modul, das `home_dir` ignoriert und direkt `/etc/...` öffnet). Plus Vault-Tagging. Bewusst out of scope.

**Annahme/Caveat:** Eine secure-Zone, die ein **Cloud-LLM-Backend** nutzt (DeepSeek/Grok), schickt ihre Prompts aus der Box. Für echte Vertraulichkeit braucht eine secure-Zone ein **lokales** Backend (llama.cpp). Der Validator warnt davor (siehe §6).

---

## 3. Datenmodell

Neues optionales Feld `secure: Option<String>` (Label = Compartment-Name, `None` = „public"):

- `ModulConfig` (`types.rs:~230`): `#[serde(default, skip_serializing_if = "Option::is_none")] pub secure: Option<String>`
- `RagPool` (`types.rs:~899`): dasselbe Feld.

`None` ⇒ exakt heutiges Verhalten (rückwärtskompatibel; bestehende Config unverändert „public").

---

## 4. Invarianten (präzise Semantik)

`comp(x) = x.secure.as_deref()`.

**Zentrale Regel:** `access_allowed(actor, target) := (actor == target)`.
Damit gilt automatisch:

| actor → target | erlaubt? |
|---|---|
| public → public (`None`,`None`) | ✅ (heute) |
| acme → acme | ✅ |
| public → acme | ❌ (von außen rein) |
| acme → public | ❌ (Zone bricht nicht aus) |
| acme → globex | ❌ |

Anwendung auf die Choke-Points:

- **R1 — Tool-Call:** actor = aufrufendes Modul; target = Modul, das das Tool *besitzt* (`settings_module` / py-Instanz). `access_allowed` muss true sein.
- **R2 — Links:** ein `linked_modules`-Eintrag, dessen Compartment ≠ dem des Moduls ist, ist **ungültig** (Deny + Validator-Fehler).
- **R3 — RAG:** ist `comp(modul) = Some(label)`, dann **muss** `modul.rag_pool` gesetzt sein, der Pool existieren und `comp(pool) == Some(label)`. Sonst **harter Fehler, kein `shared`-Fallback**. Public-Module: unverändert.
- **R4 — Compartment-brechende Tools:** für jeden secure actor unbedingt verboten (Deny-Liste, unabhängig von Links/Tags): `agent_meta.*`, `module_builder.*`, `agent.spawn`, `shell.exec`.
- **R5 — FS:** secure-Module bekommen **kein** `project_root`/`modules_dir` injiziert; ihr Config-Objekt trägt `"secure":"<label>"` + `"confine_home_only":true`; die zentralen File-Tools (`filesystem.*`, `coding.*`, `files::read_file` `allowed_refs`) erzwingen Home-only und ignorieren `allow_project_root`.

**Regel-3-Konsequenz (bewusst):** Eine secure-Zone nutzt **eigene, gleich-getaggte Instanzen** der Tools, die sie braucht (das Harness arbeitet ohnehin per-Instanz). Kein Zugriff auf public-Tools. Das macht Egress zu einer expliziten Pro-Zone-Entscheidung.

---

## 5. Enforcement-Punkte (Code)

| Stelle | Datei:Zeile | Änderung |
|---|---|---|
| Helpers | `security.rs` (neu) | `compartment_label(&ModulConfig)`, `access_allowed(Option<&str>,Option<&str>)`, `is_compartment_breaking_tool(&str)`, `const COMPARTMENT_BREAKING_TOOLS` |
| Tool-Dispatch | `tools.rs:2280` (`execute_tool_with_modules`) | vor Dispatch: actor- vs. target-Compartment prüfen (R1); Deny-Tools für secure actor (R4) |
| Perm/Link | `tools.rs:2607` (`has_permission_with_py`) | Cross-Zone-Link zählt nicht als Permission (R2) |
| RAG | `tools.rs:1182` (`rag.suchen`/`rag.speichern`) | R3: secure ⇒ Pool-Label muss matchen, kein `shared`-Fallback (fail closed) |
| config-Übergabe | `tools.rs:2303–2347` | R5: `secure`/`confine_home_only` setzen, `project_root`/`modules_dir` für secure weglassen |
| agent_meta | `tools.rs:2388` | elevated snapshot nie für secure actor bauen (zusätzlich zu R4) |
| File-Tools | `tools.rs` (`filesystem`/`files::read_file`, built-in Rust) + `modules/coding/module.py` | `confine_home_only` respektieren; `allow_project_root` für secure hart aus |
| Startup-Validierung | `types.rs` + Loader/`main.rs` | `validate_compartments` (siehe §6) |

---

## 6. Fail-closed-Validierung — der „SECURED-Haken"

Funktion `validate_compartments(&AgentConfig) -> Vec<CompartmentViolation>` beim Config-Laden **und** bei jedem Config-Save.

Pro secure-Modul geprüft:

- **FEHLER (Modul wird deaktiviert, nicht gestartet):**
  - kein/fehlender/falsch-getaggter `rag_pool` (R3).
  - `linked_modules`-Eintrag mit anderem Compartment (R2).
  - Compartment-brechendes Tool in Permissions/Links (R4).
- **WARNUNG (läuft, aber sichtbar markiert):**
  - secure-Modul nutzt ein **nicht-lokales** `llm_backend` (Prompts verlassen die Box). Lokal = Backend-URL besteht `security::is_self_hosted_llm_url` (loopback/privat/CGNAT/ULA).

Verhalten: Verstöße werden laut geloggt, das betroffene secure-Modul wird **nicht aktiviert** (Scheduler startet nicht), statt leaky zu laufen. Ergebnis über `GET /api/security/compartments` fürs UI abrufbar. Kein stiller Fallback — das ist der Kern der Security-Eigenschaft.

---

## 7. Python-Modul-Seite

- Das an secure-Module gereichte `instance_config` enthält `"secure":"<label>"` und `"confine_home_only":true`.
- `modules/coding/module.py`: `home_dir()`/Pfad-Resolver liest `confine_home_only` → `allow_project_root` wird ignoriert, nur `home_dir` erlaubt. (coding hat den `commonpath`-Check bereits — nur die Erweiterungs-Flags hart abklemmen.) Die built-in `filesystem`/`shell`-Tools (Rust) werden in §5 abgeklemmt, nicht hier.
- Module brauchen sonst **keine** Änderung: die Wand sitzt im Rust-Dispatch davor.

---

## 8. UI

- `secure`-Textfeld im Modul-Config-Editor und im RAG-Pool-Editor (vorhandener Editor; `secure` ist kein Secret → keine Redaction nötig).
- Badge „🔒 <label>" im Chat-/Redaktions-Panel (`chat.html`) pro Instanz.
- Validator-Ergebnis (`/api/security/compartments`) im UI als Warn-/Fehlerliste sichtbar machen.

---

## 9. Tests

- **Unit (`security.rs`):** `access_allowed`-Matrix (public/public, acme/acme, acme/globex, acme/public, public/acme); `is_compartment_breaking_tool`.
- **RAG fail-closed:** secure-Modul + public/fehlender Pool ⇒ Deny statt `shared`.
- **Dispatch:** secure actor ruft Deny-Tool ⇒ Deny; secure actor ruft fremd-getaggtes Tool ⇒ Deny; secure→eigenes Tool ⇒ ok.
- **Validierung:** fehlkonfiguriertes secure-Modul ⇒ Violation + deaktiviert; sauberes ⇒ keine.
- **Integration:** Zone „acme" (chat + rag + tools) und ein public-Chat; assert: public liest acme-Home/RAG nicht, acme erreicht public nicht; acme-Daten landen nur im acme-Pool.

---

## 10. Anwendungsbeispiel (Migration)

Bestehende 6 Chats bleiben **public** (`secure:None`) — nichts ändert sich für sie.

Firmen-Tenant „acme" aufsetzen:

1. RAG-Pool `acme` mit `secure:"acme"`.
2. Chat-Instanz `chat.acme` mit `secure:"acme"`, `rag_pool:"acme"`, **lokalem** llm_backend.
3. Nur benötigte Tools als `secure:"acme"`-Instanzen verlinken (kein `agent_meta`, kein `module_builder`, kein Egress außer bewusst).
4. Validator muss grün sein, sonst startet `chat.acme` nicht.

Ergebnis: `chat.acme` sieht/erreicht ausschließlich acme-Ressourcen; keine andere LLM (public oder globex) kommt an acme-Daten.

---

## 11. Offene Punkte / Annahmen

- **Egress-Tools** (websearch, smtp, …) in einer secure-Zone sind erlaubt, *wenn* als same-label-Instanz angelegt — bewusste Pro-Zone-Entscheidung des Betreibers, nicht vom System verboten. (Optional Phase 2: globaler „no-egress"-Schalter pro Zone.)
- **Telegram-/Web-Eingang:** ein eingehender Chat-Request adressiert eine Instanz; Cross-Zone-Routing über die HTTP-Schicht muss denselben `access_allowed`-Check nutzen (Eingangs-Route = actor public). Wird in der Plan-Phase als eigener Task aufgenommen.
- **`data_dir`-Residual:** secure-Python-Module erhalten weiterhin `data_dir` (geteilte Basis), weil viele Module ihr Home daraus berechnen (`home(config)`). Gut gebaute Module bleiben in ihrem Home; ein *bösartiges* Modul könnte `data_dir` zum Pfadbau missbrauchen. Das ist genau der Phase-2-Fall (OS-Sandbox) und in Phase 1 (Bedrohung = LLM-via-Tools) bewusst akzeptiert. Die built-in Rust-File-Tools (`files.read/write/list`) sind unabhängig davon schon hart aufs Modul-Home begrenzt (`modules/files.rs`).
- **Phase 2** (OS-Sandbox, Vault-Tagging, no-egress-Switch) ist dokumentiert, aber nicht Teil dieser Umsetzung.
