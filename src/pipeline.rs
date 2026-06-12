// src/pipeline.rs — dünner Adapter vom Agent-API zum SQLite-Store.
//
// Historisch war das der JSON-File-basierte State-Store (erstellt/gestartet/
// erledigt-Ordner, atomic_write, hard_link-claim). Seit dem SQLite-Umbau
// ist der eigentliche State im `Store`; Pipeline hält nur noch Backwards-
// Compat-API + Home-/Convo-/Log-/Audit-Helfer und den Migrationspfad.
//
// Die JSON-Pipeline bleibt als Fallback im Migrations-Pfad: beim ersten
// Start nach dem Upgrade wird `erstellt/`, `gestartet/`, `erledigt/` ein-
// gelesen und nach SQL geschoben, danach gelöscht.

use crate::security::safe_id;
use crate::store::Store;
use crate::types::{Aufgabe, AufgabeStatus, LogEvent, LogTyp};
use chrono::Utc;
use std::path::{Path, PathBuf};

pub struct Pipeline {
    pub base: PathBuf,
    pub store: Store,
    /// Globaler Config-Write-Lock — serialisiert ALLE config.json-Writes
    /// (web-API save/restore, Orchestrator run_cleanup, wizard-commit,
    /// load_temp_modules). Sonst last-write-wins zwischen den drei Schreibern
    /// → stiller Datenverlust von User-Edits wenn Orchestrator gleichzeitig
    /// cleanuped (GLM-Finding Run SQLite-5).
    pub config_write_lock: std::sync::Arc<tokio::sync::Mutex<()>>,
}

fn status_key(s: &AufgabeStatus) -> &'static str {
    match s {
        AufgabeStatus::Erstellt => "erstellt",
        AufgabeStatus::Gestartet => "gestartet",
        AufgabeStatus::Success => "success",
        AufgabeStatus::Failed => "failed",
        AufgabeStatus::Cancelled => "cancelled",
    }
}

fn status_from_key(k: &str) -> AufgabeStatus {
    match k {
        "gestartet" => AufgabeStatus::Gestartet,
        "success" => AufgabeStatus::Success,
        "failed" => AufgabeStatus::Failed,
        "cancelled" => AufgabeStatus::Cancelled,
        _ => AufgabeStatus::Erstellt,
    }
}

impl Pipeline {
    /// Öffnet die Pipeline mit SQLite-Backend unter `base/tasks.db`. Wenn beim
    /// ersten Start alte JSON-Ordner existieren, werden sie migriert.
    pub fn new(base: &Path) -> std::io::Result<Self> {
        std::fs::create_dir_all(base)?;
        std::fs::create_dir_all(base.join("home"))?;
        std::fs::create_dir_all(base.join("logs"))?;

        let db_path = base.join("tasks.db");
        let db_fresh = !db_path.exists();
        let store = Store::open(&db_path)
            .map_err(|e| std::io::Error::other(format!("Store open: {}", e)))?;

        let p = Self {
            base: base.into(),
            store,
            config_write_lock: std::sync::Arc::new(tokio::sync::Mutex::new(())),
        };

        if db_fresh {
            // Wenn JSON-State existiert → einmalig migrieren. Danach die Ordner
            // umbenennen (nicht löschen), damit der User sie sichten kann falls
            // was fehlt.
            let migrated = p.migrate_from_json();
            if migrated > 0 {
                tracing::info!(
                    "Pipeline: {} Aufgaben aus JSON-Ordnern nach SQLite migriert",
                    migrated
                );
            }
        }

        Ok(p)
    }

    /// Migriert existierende JSON-Task-Files nach SQLite. Idempotent — wird nur
    /// beim allerersten Start ohne tasks.db aufgerufen. Danach werden die
    /// Quell-Ordner als `.migrated` archiviert statt gelöscht (forensik-freundlich).
    fn migrate_from_json(&self) -> usize {
        let mut count = 0usize;
        for ordner in &["erstellt", "gestartet", "erledigt"] {
            let dir = self.base.join(ordner);
            if !dir.exists() {
                continue;
            }
            if let Ok(entries) = std::fs::read_dir(&dir) {
                for entry in entries.flatten() {
                    let path = entry.path();
                    if !path.extension().is_some_and(|e| e == "json") {
                        continue;
                    }
                    let Ok(content) = std::fs::read_to_string(&path) else {
                        continue;
                    };
                    let Ok(a) = serde_json::from_str::<Aufgabe>(&content) else {
                        continue;
                    };
                    if self.speichern_raw(&a).is_ok() {
                        count += 1;
                    }
                }
            }
            // Ordner archivieren statt löschen (bei Problemen kann der User den
            // alten State noch einsehen/manuell wiederherstellen)
            let archived = self.base.join(format!(".migrated.{}", ordner));
            let _ = std::fs::rename(&dir, &archived);
        }
        count
    }

    // ═══════════════════ Task-API (auf Store gemappt) ═══════════════════

    pub fn speichern(&self, aufgabe: &Aufgabe) -> std::io::Result<()> {
        self.speichern_raw(aufgabe)
    }

    pub fn reschedule(&self, aufgabe: &mut Aufgabe, wann: String) -> std::io::Result<()> {
        aufgabe.wann = wann;
        aufgabe.status = AufgabeStatus::Erstellt;
        aufgabe.gestartet = None;
        aufgabe.erledigt = None;
        self.speichern_raw(aufgabe)
    }

    fn speichern_raw(&self, aufgabe: &Aufgabe) -> std::io::Result<()> {
        let json = serde_json::to_string(aufgabe)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        let faellig_ab_ts = crate::store::parse_faellig_ab(&aufgabe.wann);
        crate::store::task_upsert(
            &self.store.pool,
            &aufgabe.id,
            status_key(&aufgabe.status),
            &aufgabe.modul,
            &json,
            aufgabe.erstellt.timestamp(),
            faellig_ab_ts,
        )
        .map_err(|e| std::io::Error::other(format!("store: {}", e)))?;
        Ok(())
    }

    /// State-Transition: Status ändern, Timestamp setzen, persist. Atomar in SQL.
    /// Vorheriges "write-new, delete-old"-Pattern entfällt komplett; es kann kein
    /// Window mehr geben in dem der Task in zwei "Ordnern" existiert.
    pub fn verschieben(
        &self,
        aufgabe: &mut Aufgabe,
        neuer_status: AufgabeStatus,
    ) -> std::io::Result<()> {
        aufgabe.status = neuer_status;
        match &aufgabe.status {
            AufgabeStatus::Gestartet => aufgabe.gestartet = Some(Utc::now()),
            AufgabeStatus::Success | AufgabeStatus::Failed | AufgabeStatus::Cancelled => {
                aufgabe.erledigt = Some(Utc::now())
            }
            _ => {}
        }
        let json = serde_json::to_string(aufgabe)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        crate::store::task_transition(
            &self.store.pool,
            &aufgabe.id,
            status_key(&aufgabe.status),
            &json,
        )
        .map_err(|e| std::io::Error::other(format!("store: {}", e)))?;
        Ok(())
    }

    fn decode_rows(rows: Vec<crate::store::TaskRow>) -> Vec<Aufgabe> {
        rows.into_iter()
            .filter_map(|r| serde_json::from_str::<Aufgabe>(&r.payload_json).ok())
            .collect()
    }

    pub fn laden(&self, ordner: &str) -> Vec<Aufgabe> {
        match crate::store::task_list_by_status(&self.store.pool, ordner) {
            Ok(r) => Self::decode_rows(r),
            Err(_) => vec![],
        }
    }

    pub fn erstellt(&self) -> Vec<Aufgabe> {
        self.laden("erstellt")
    }
    pub fn gestartet(&self) -> Vec<Aufgabe> {
        self.laden("gestartet")
    }
    pub fn erledigt(&self) -> Vec<Aufgabe> {
        match crate::store::task_list_erledigt_recent(&self.store.pool, 500) {
            Ok(r) => Self::decode_rows(r),
            Err(_) => vec![],
        }
    }

    pub fn laden_by_id(&self, id: &str) -> std::io::Result<Option<Aufgabe>> {
        match crate::store::task_load_by_id(&self.store.pool, id) {
            Ok(Some(r)) => Ok(serde_json::from_str::<Aufgabe>(&r.payload_json).ok()),
            Ok(None) => Ok(None),
            Err(e) => Err(std::io::Error::other(e)),
        }
    }

    /// Neuer Modul-spezifischer Claim — atomar via SQL.
    pub fn claim_for_modul(&self, modul_id: &str) -> std::io::Result<Option<Aufgabe>> {
        match crate::store::claim_one_for_modul(&self.store.pool, modul_id) {
            Ok(Some(r)) => Ok(serde_json::from_str::<Aufgabe>(&r.payload_json).ok()),
            Ok(None) => Ok(None),
            Err(e) => Err(std::io::Error::other(e)),
        }
    }

    /// Rückwärts-Kompatibel: früher hatte dedup einen Job beim Start. Jetzt
    /// ist der State eindeutig in SQL (PRIMARY KEY auf id), also no-op.
    pub fn deduplizieren(&self) {
        // State-Machine-Dedup ist dank PRIMARY KEY + Check-Constraint garantiert.
    }

    /// Nach einem Prozess-Restart kann keine in-memory Task-Handle mehr zu
    /// `gestartet`-Records existieren. Diese Tasks bleiben sonst dauerhaft als
    /// laufend sichtbar, obwohl der LLM-/Tool-Call hart unterbrochen wurde.
    pub fn fail_started_after_restart(&self) -> usize {
        let mut count = 0usize;
        for mut aufgabe in self.gestartet() {
            if aufgabe.status != AufgabeStatus::Gestartet {
                continue;
            }
            // Tasks mit verbleibenden Retries werden nach einem Neustart NEU
            // EINGEREIHT statt gekillt — eine laufende Pipeline (Video-DeepDive
            // etc.) ueberlebt damit einen Server-Neustart, statt komplett zu
            // sterben. Seiteneffekt-Tools sind idempotency-gecacht, laufen also
            // nicht teuer doppelt. Nur erschoepfte Retries failen hart.
            if aufgabe.retry_count < aufgabe.retry {
                aufgabe.retry_count += 1;
                aufgabe.gestartet = None;
                if let Err(e) = self.verschieben(&mut aufgabe, AufgabeStatus::Erstellt) {
                    self.log(
                        "startup",
                        Some(&aufgabe.id),
                        LogTyp::Error,
                        &format!("Restart-Requeue failed: {}", e),
                    );
                } else {
                    self.log(
                        "startup",
                        Some(&aufgabe.id),
                        LogTyp::Warning,
                        &format!(
                            "Aufgabe nach Neustart neu eingereiht statt gekillt (Retry {}/{})",
                            aufgabe.retry_count, aufgabe.retry
                        ),
                    );
                }
                continue;
            }
            aufgabe.ergebnis = Some(
                "FAILED: Server-Neustart hat die laufende Aufgabe unterbrochen (Retries erschoepft)."
                    .into(),
            );
            match self.verschieben(&mut aufgabe, AufgabeStatus::Failed) {
                Ok(_) => {
                    count += 1;
                    self.route_restart_failure_to_chat(&aufgabe);
                    self.log(
                        "startup",
                        Some(&aufgabe.id),
                        LogTyp::Failed,
                        "Gestartete Aufgabe nach Server-Neustart als Failed markiert",
                    );
                    self.audit(
                        "task.restart_recovery",
                        "system",
                        &format!("task={}", aufgabe.id),
                    );
                }
                Err(e) => self.log(
                    "startup",
                    Some(&aufgabe.id),
                    LogTyp::Error,
                    &format!("Restart-Recovery failed: {}", e),
                ),
            }
        }
        count
    }

    fn route_restart_failure_to_chat(&self, aufgabe: &Aufgabe) {
        let Some(route) = aufgabe.zurueck_an.as_deref() else {
            return;
        };
        let Some((modul_id, Some(convo_id))) = crate::util::parse_chat_route(route) else {
            return;
        };
        let mut convo = self.convo_load(&modul_id, &convo_id).unwrap_or_else(|| {
            serde_json::json!({
                "id": convo_id,
                "title": "Task Ergebnis",
                "messages": [],
                "updated": chrono::Utc::now().to_rfc3339(),
            })
        });
        if !convo.get("messages").is_some_and(|v| v.is_array()) {
            convo["messages"] = serde_json::json!([]);
        }
        let body = format!(
            "[Ergebnis von {}]: {}",
            aufgabe.modul,
            aufgabe
                .ergebnis
                .as_deref()
                .unwrap_or("FAILED: Server-Neustart hat die laufende Aufgabe unterbrochen.")
        );
        if let Some(messages) = convo["messages"].as_array_mut() {
            messages.push(serde_json::json!({"role": "assistant", "content": body}));
        }
        if convo
            .get("title")
            .and_then(|v| v.as_str())
            .map(|s| s.trim().is_empty())
            .unwrap_or(true)
        {
            convo["title"] = serde_json::json!("Task Ergebnis");
        }
        convo["updated"] = serde_json::json!(chrono::Utc::now().to_rfc3339());
        if let Err(e) = self.convo_save(&modul_id, &convo) {
            self.log(
                "startup",
                Some(&aufgabe.id),
                LogTyp::Warning,
                &format!("Restart-Recovery Chat-Routing fehlgeschlagen: {}", e),
            );
            return;
        }
        let source = format!("task:{}", aufgabe.id);
        let _ = self.notification_add(
            &modul_id,
            Some(&convo_id),
            "system",
            Some("Aufgabe unterbrochen"),
            "Eine laufende Aufgabe wurde durch Server-Neustart abgebrochen und in den Chat geschrieben.",
            Some(&source),
        );
    }

    pub fn cleanup_erledigt(&self, max_count: usize, max_alter_tage: u32) {
        if let Ok(n) =
            crate::store::task_cleanup_erledigt(&self.store.pool, max_count, max_alter_tage)
        {
            if n > 0 {
                tracing::info!("Cleanup: {} alte Aufgaben geloescht", n);
            }
        }
    }

    // ═══════════════════ Home-Verzeichnisse (bleiben Dateien) ═══════════════════

    fn sanitize_id(id: &str) -> String {
        safe_id(id).unwrap_or_else(|| "_unsafe_".to_string())
    }

    pub fn home_dir(&self, modul_id: &str) -> PathBuf {
        let safe = Self::sanitize_id(modul_id);
        let home = self.base.join("home").join(&safe);
        std::fs::create_dir_all(&home).ok();
        home
    }

    // ═══════════════════ Conversations (jetzt in SQL) ═══════════════════

    pub fn convo_list(&self, modul_id: &str) -> Vec<serde_json::Value> {
        match crate::store::convo_list(&self.store.pool, modul_id) {
            Ok(jsons) => jsons
                .into_iter()
                .filter_map(|j| serde_json::from_str::<serde_json::Value>(&j).ok())
                .collect(),
            Err(_) => vec![],
        }
    }

    pub fn convo_load(&self, modul_id: &str, convo_id: &str) -> Option<serde_json::Value> {
        let safe_cid = safe_id(convo_id)?;
        crate::store::convo_load(&self.store.pool, modul_id, &safe_cid)
            .ok()
            .flatten()
            .and_then(|j| serde_json::from_str::<serde_json::Value>(&j).ok())
    }

    pub fn convo_save(&self, modul_id: &str, convo: &serde_json::Value) -> std::io::Result<()> {
        let id_raw = convo["id"].as_str().unwrap_or("unknown");
        let id = safe_id(id_raw).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige convo-ID")
        })?;
        let json = serde_json::to_string(convo)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        crate::store::convo_save(&self.store.pool, modul_id, &id, &json)
            .map_err(|e| std::io::Error::other(e))
    }

    pub fn convo_delete(&self, modul_id: &str, convo_id: &str) -> std::io::Result<()> {
        let safe_cid = safe_id(convo_id).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige convo-ID")
        })?;
        crate::store::convo_delete(&self.store.pool, modul_id, &safe_cid)
            .map_err(|e| std::io::Error::other(e))
    }

    // ═══════════════════ Notifications (SQLite) ═══════════════════

    pub fn notification_add(
        &self,
        modul_id: &str,
        convo_id: Option<&str>,
        kind: &str,
        title: Option<&str>,
        body: &str,
        source: Option<&str>,
    ) -> std::io::Result<String> {
        let safe_mid = safe_id(modul_id).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige modul-ID")
        })?;
        let safe_cid = match convo_id {
            Some(cid) if !cid.trim().is_empty() => Some(safe_id(cid).ok_or_else(|| {
                std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige convo-ID")
            })?),
            _ => None,
        };
        crate::store::notification_create(
            &self.store.pool,
            &safe_mid,
            safe_cid.as_deref(),
            kind,
            title,
            body,
            source,
        )
        .map_err(|e| std::io::Error::other(e))
    }

    pub fn notification_list(
        &self,
        modul_id: &str,
        convo_id: Option<&str>,
        include_read: bool,
        limit: usize,
    ) -> Vec<crate::types::NotificationItem> {
        let Some(safe_mid) = safe_id(modul_id) else {
            return vec![];
        };
        let safe_cid = convo_id.and_then(safe_id);
        crate::store::notification_list(
            &self.store.pool,
            &safe_mid,
            safe_cid.as_deref(),
            include_read,
            limit,
        )
        .unwrap_or_default()
    }

    pub fn notification_mark_read(
        &self,
        modul_id: &str,
        notification_id: &str,
        read: bool,
    ) -> std::io::Result<()> {
        let safe_mid = safe_id(modul_id).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige modul-ID")
        })?;
        let safe_id = safe_id(notification_id).ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "ungültige notification-ID",
            )
        })?;
        crate::store::notification_mark_read(&self.store.pool, &safe_mid, &safe_id, read)
            .map_err(|e| std::io::Error::other(e))
    }

    pub fn notification_delete(
        &self,
        modul_id: &str,
        notification_id: &str,
    ) -> std::io::Result<()> {
        let safe_mid = safe_id(modul_id).ok_or_else(|| {
            std::io::Error::new(std::io::ErrorKind::InvalidInput, "ungültige modul-ID")
        })?;
        let safe_id = safe_id(notification_id).ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "ungültige notification-ID",
            )
        })?;
        crate::store::notification_delete(&self.store.pool, &safe_mid, &safe_id)
            .map_err(|e| std::io::Error::other(e))
    }

    // ═══════════════════ Audit-Log (jetzt in SQL, append-only via Trigger) ═══════════════════

    pub fn audit(&self, action: &str, actor: &str, detail: &str) {
        let _ = crate::store::audit(&self.store.pool, action, actor, detail);
    }

    // ═══════════════════ Operational-Log (bleibt JSONL-File für tail-friendly) ═══════════════════

    pub fn log(&self, modul: &str, aufgabe_id: Option<&str>, typ: LogTyp, msg: &str) {
        let event = LogEvent {
            zeit: Utc::now(),
            modul: modul.into(),
            aufgabe_id: aufgabe_id.map(String::from),
            typ,
            nachricht: msg.into(),
        };
        let date = Utc::now().format("%Y-%m-%d").to_string();
        let log_file = self.base.join("logs").join(format!("{date}.jsonl"));
        let line = match serde_json::to_string(&event) {
            Ok(s) => s + "\n",
            Err(_) => return,
        };
        use std::io::Write;
        if let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_file)
        {
            let _ = f.write_all(line.as_bytes());
        }
        match event.typ {
            LogTyp::Error | LogTyp::Failed => tracing::error!("[{}] {}", modul, msg),
            LogTyp::Warning => tracing::warn!("[{}] {}", modul, msg),
            _ => tracing::info!("[{}] {}", modul, msg),
        }
    }

    pub fn logs_laden(&self, datum: &str) -> Vec<LogEvent> {
        let path = self.base.join("logs").join(format!("{datum}.jsonl"));
        if let Ok(content) = std::fs::read_to_string(&path) {
            content
                .lines()
                .filter_map(|l| serde_json::from_str(l).ok())
                .collect()
        } else {
            vec![]
        }
    }

    /// Log- und Audit-File-Rotation basierend auf retention_days. Audit-SQL-
    /// Tabelle wird separat bereinigt (idempotency_cleanup gibt es dafür).
    pub fn cleanup_logs(&self, retention_days: u32) {
        if retention_days == 0 {
            return;
        }
        let cutoff = Utc::now() - chrono::Duration::days(retention_days as i64);
        let cutoff_str = cutoff.format("%Y-%m-%d").to_string();
        let dir = self.base.join("logs");
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                let stem = path.file_stem().and_then(|s| s.to_str()).unwrap_or("");
                if stem.len() == 10 && stem < cutoff_str.as_str() {
                    if std::fs::remove_file(&path).is_ok() {
                        tracing::info!("Log rotation: {:?} geloescht", path);
                    }
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::types::{Aufgabe, AufgabeStatus};

    fn tmp_pipeline() -> (tempfile::TempDir, Pipeline) {
        let dir = tempfile::tempdir().unwrap();
        let p = Pipeline::new(dir.path()).unwrap();
        (dir, p)
    }

    #[test]
    fn test_pipeline_create_and_load() {
        let (_dir, pipeline) = tmp_pipeline();
        let aufgabe = Aufgabe::neu("test.modul", "Test Anweisung", "sofort", "unit-test");
        let id = aufgabe.id.clone();
        pipeline.speichern(&aufgabe).unwrap();
        let loaded = pipeline.erstellt();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, id);
        assert_eq!(loaded[0].anweisung, "Test Anweisung");
    }

    #[test]
    fn test_pipeline_verschieben() {
        let (_dir, pipeline) = tmp_pipeline();
        let mut aufgabe = Aufgabe::neu("test", "Test", "sofort", "test");
        pipeline.speichern(&aufgabe).unwrap();
        assert_eq!(pipeline.erstellt().len(), 1);

        pipeline
            .verschieben(&mut aufgabe, AufgabeStatus::Gestartet)
            .unwrap();
        assert_eq!(pipeline.erstellt().len(), 0);
        assert_eq!(pipeline.gestartet().len(), 1);
        assert!(aufgabe.gestartet.is_some());

        aufgabe.ergebnis = Some("Done".into());
        pipeline
            .verschieben(&mut aufgabe, AufgabeStatus::Success)
            .unwrap();
        assert_eq!(pipeline.gestartet().len(), 0);
        assert_eq!(pipeline.erledigt().len(), 1);
        assert!(aufgabe.erledigt.is_some());
    }

    #[test]
    fn test_pipeline_laden_by_id() {
        let (_dir, pipeline) = tmp_pipeline();
        let aufgabe = Aufgabe::neu("test", "Findme", "sofort", "test");
        let id = aufgabe.id.clone();
        pipeline.speichern(&aufgabe).unwrap();

        let found = pipeline.laden_by_id(&id).unwrap();
        assert!(found.is_some());
        assert_eq!(found.unwrap().anweisung, "Findme");

        let not_found = pipeline.laden_by_id("nonexistent").unwrap();
        assert!(not_found.is_none());
    }

    #[test]
    fn test_claim_for_modul_atomic() {
        let (_dir, pipeline) = tmp_pipeline();
        let a1 = Aufgabe::neu("mail.privat", "task1", "sofort", "u");
        let a2 = Aufgabe::neu("mail.privat", "task2", "sofort", "u");
        pipeline.speichern(&a1).unwrap();
        pipeline.speichern(&a2).unwrap();

        let c1 = pipeline.claim_for_modul("mail.privat").unwrap();
        assert!(c1.is_some());
        let c2 = pipeline.claim_for_modul("mail.privat").unwrap();
        assert!(c2.is_some());
        let c3 = pipeline.claim_for_modul("mail.privat").unwrap();
        assert!(c3.is_none(), "keine erstellt-tasks mehr");

        assert_eq!(pipeline.erstellt().len(), 0);
        assert_eq!(pipeline.gestartet().len(), 2);
    }

    #[test]
    fn test_fail_started_after_restart_marks_running_tasks_failed() {
        let (_dir, pipeline) = tmp_pipeline();
        let mut aufgabe = Aufgabe::neu("chat.deepdive", "running", "sofort", "chat:test");
        pipeline.speichern(&aufgabe).unwrap();
        pipeline
            .verschieben(&mut aufgabe, AufgabeStatus::Gestartet)
            .unwrap();

        let recovered = pipeline.fail_started_after_restart();
        assert_eq!(recovered, 1);
        assert_eq!(pipeline.gestartet().len(), 0);
        let loaded = pipeline.laden_by_id(&aufgabe.id).unwrap().unwrap();
        assert_eq!(loaded.status, AufgabeStatus::Failed);
        assert!(
            loaded
                .ergebnis
                .as_deref()
                .unwrap_or("")
                .contains("Server-Neustart")
        );
    }

    #[test]
    fn test_restart_requeues_task_with_retries_left() {
        let (_dir, pipeline) = tmp_pipeline();
        let mut aufgabe = Aufgabe::neu("video.workflow", "deepdive", "sofort", "workflow_trigger");
        aufgabe.retry = 2; // Workflow-Tasks bekommen Retries
        pipeline.speichern(&aufgabe).unwrap();
        pipeline
            .verschieben(&mut aufgabe, AufgabeStatus::Gestartet)
            .unwrap();

        // Neustart: Task hat Retries -> neu eingereiht, NICHT gefailt
        let failed = pipeline.fail_started_after_restart();
        assert_eq!(failed, 0, "Task mit Retries darf nicht failen");
        assert_eq!(pipeline.gestartet().len(), 0);
        let loaded = pipeline.laden_by_id(&aufgabe.id).unwrap().unwrap();
        assert_eq!(loaded.status, AufgabeStatus::Erstellt, "muss neu eingereiht sein");
        assert_eq!(loaded.retry_count, 1);

        // Zweiter Neustart waehrend wieder gestartet: retry_count 1 -> 2 (noch ok)
        let mut again = loaded;
        pipeline.verschieben(&mut again, AufgabeStatus::Gestartet).unwrap();
        pipeline.fail_started_after_restart();
        let l2 = pipeline.laden_by_id(&aufgabe.id).unwrap().unwrap();
        assert_eq!(l2.status, AufgabeStatus::Erstellt);
        assert_eq!(l2.retry_count, 2);

        // Dritter: retry_count 2 == retry 2 -> jetzt failt es hart
        let mut again2 = l2;
        pipeline.verschieben(&mut again2, AufgabeStatus::Gestartet).unwrap();
        let failed3 = pipeline.fail_started_after_restart();
        assert_eq!(failed3, 1);
        let l3 = pipeline.laden_by_id(&aufgabe.id).unwrap().unwrap();
        assert_eq!(l3.status, AufgabeStatus::Failed);
        assert!(l3.ergebnis.as_deref().unwrap_or("").contains("erschoepft"));
    }

    #[test]
    fn test_pipeline_cleanup_erledigt() {
        let (_dir, pipeline) = tmp_pipeline();
        for i in 0..5 {
            let mut aufgabe = Aufgabe::neu("test", &format!("Task {}", i), "sofort", "test");
            aufgabe.status = AufgabeStatus::Success;
            aufgabe.erledigt = Some(Utc::now() - chrono::Duration::days(i as i64));
            pipeline.speichern(&aufgabe).unwrap();
        }
        assert_eq!(pipeline.erledigt().len(), 5);
        pipeline.cleanup_erledigt(3, 30);
        assert!(pipeline.erledigt().len() <= 3);
    }

    #[test]
    fn test_migration_from_json() {
        let dir = tempfile::tempdir().unwrap();
        let erstellt_dir = dir.path().join("erstellt");
        std::fs::create_dir_all(&erstellt_dir).unwrap();
        let a = Aufgabe::neu("legacy", "migrate me", "sofort", "u");
        let path = erstellt_dir.join(format!("{}.json", a.id));
        std::fs::write(&path, serde_json::to_string(&a).unwrap()).unwrap();

        let p = Pipeline::new(dir.path()).unwrap();
        let loaded = p.erstellt();
        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].anweisung, "migrate me");
        // Alt-Ordner wurde archiviert
        assert!(!erstellt_dir.exists());
        assert!(dir.path().join(".migrated.erstellt").exists());
    }
}
