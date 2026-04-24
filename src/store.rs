// src/store.rs — SQLite-basierte zentrale Persistenz-Schicht.
//
// Löst die Concurrency- und Transaktions-Probleme der alten JSON-File-Pipeline:
//   * Atomic task claim via `BEGIN IMMEDIATE` + `UPDATE ... WHERE status='erstellt'`
//     statt hard_link-Tricks → mehrere Scheduler können parallel claimen, exakt
//     einer gewinnt, kein Race-Fenster.
//   * Idempotency-Tabelle schließt at-least-once-Doppel-Execution bei Retry oder
//     Watchdog-Abort für Side-Effect-Tools (shell, notify, smtp, files.write).
//   * Persistente TokenStats → Daily Budget überlebt Prozess-Restart.
//   * Audit-Log als eigene Tabelle (nicht JSONL-File das im Operational-Log-
//     Cleanup mitgerotiert wird).
//   * cron_state dedupliziert atomar innerhalb einer Transaktion mit der Task-
//     Erstellung — kein Race zwischen persist und spawn.
//
// WAL-Mode + NORMAL sync + 64MB cache + mmap = solide Default-Performance für
// Single-Node-Deployment mit dutzenden Modulen. Busy-Timeout fängt den seltenen
// Fall von Lock-Contention unter Last.

use std::path::Path;
use std::sync::Arc;
use rusqlite::{params, OptionalExtension};
use r2d2::Pool;
use r2d2_sqlite::SqliteConnectionManager;
use serde::{Deserialize, Serialize};
use sha2::{Sha256, Digest};

pub type SqlitePool = Pool<SqliteConnectionManager>;

/// Alle SQL-Fehler im Store enden als String-Fehler — der Aufrufer (async Code)
/// will meist nur wissen ob es ging, nicht rusqlite::Error durchkonvertieren.
pub type StoreResult<T> = Result<T, String>;

fn e<E: std::fmt::Display>(msg: &str) -> impl Fn(E) -> String + '_ {
    move |err: E| format!("{}: {}", msg, err)
}

/// Baut einen Connection-Pool mit WAL + performante Pragmas.
///
/// `max_size` 16 ist bewusst groß gewählt: der Orchestrator + N Scheduler +
/// Watchdog + HTTP-Server wollen alle gleichzeitig eine Connection ziehen
/// können, und SQLite erlaubt unter WAL mehrere Reader parallel (+ einen
/// Writer), also skaliert das gut bis ein Dutzend Module.
pub fn open_pool(db_path: &Path) -> StoreResult<SqlitePool> {
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent).map_err(e("create_dir_all"))?;
    }
    let manager = SqliteConnectionManager::file(db_path)
        .with_init(|c| {
            // WAL für concurrent readers + eine atomic write lane.
            // synchronous=NORMAL ist der sweet spot für WAL: Daten-Durability bei
            // Checkpoint garantiert, zwischen Checkpoints maximal ein Commit
            // verloren bei Hard-Power-Loss (akzeptabel für task queue).
            c.execute_batch(
                "PRAGMA journal_mode=WAL;
                 PRAGMA synchronous=NORMAL;
                 PRAGMA busy_timeout=5000;
                 PRAGMA cache_size=-65536;
                 PRAGMA mmap_size=268435456;
                 PRAGMA foreign_keys=ON;
                 PRAGMA temp_store=MEMORY;"
            )?;
            Ok(())
        });
    let pool = Pool::builder()
        .max_size(16)
        .build(manager)
        .map_err(e("pool build"))?;

    // Schema anlegen (idempotent via IF NOT EXISTS)
    let conn = pool.get().map_err(e("pool get"))?;
    conn.execute_batch(SCHEMA).map_err(e("schema create"))?;

    // Migrations für existierende DBs — ALTER TABLE idempotent machen via
    // information_schema-Check. SQLite hat dafür PRAGMA table_info.
    let has_faellig: bool = conn.prepare("PRAGMA table_info(tasks)")
        .map_err(e("pragma"))?
        .query_map([], |r| r.get::<_, String>(1))
        .map_err(e("pragma query"))?
        .filter_map(|r| r.ok())
        .any(|col_name| col_name == "faellig_ab_ts");
    if !has_faellig {
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN faellig_ab_ts INTEGER NOT NULL DEFAULT 0",
            [],
        ).map_err(e("migration faellig_ab_ts"))?;
        // Bestehende erstellt-Tasks als "sofort fällig" markieren
        conn.execute(
            "UPDATE tasks SET faellig_ab_ts = erstellt_ts WHERE faellig_ab_ts = 0 AND status='erstellt'",
            [],
        ).map_err(e("migration default fall"))?;
    }

    Ok(pool)
}

const SCHEMA: &str = r#"
-- ══════════ Tasks / Pipeline ══════════
CREATE TABLE IF NOT EXISTS tasks (
    id              TEXT PRIMARY KEY,
    status          TEXT NOT NULL CHECK(status IN ('erstellt','gestartet','success','failed','cancelled')),
    modul           TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    erstellt_ts     INTEGER NOT NULL,
    -- Unix-Timestamp ab wann die Task fällig ist (sofort-tasks: gleich erstellt_ts).
    -- claim_one_for_modul filtert darauf, damit später-geplante Tasks nicht die
    -- Queue für sofort-fällige blockieren (Scheduler-Fairness-Bug Run SQLite-3).
    faellig_ab_ts   INTEGER NOT NULL DEFAULT 0,
    gestartet_ts    INTEGER,
    erledigt_ts     INTEGER,
    claim_token     TEXT,
    claimed_by      TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_status_modul     ON tasks(status, modul);
CREATE INDEX IF NOT EXISTS idx_tasks_claim_queue      ON tasks(status, modul, faellig_ab_ts) WHERE status='erstellt';
CREATE INDEX IF NOT EXISTS idx_tasks_erledigt_ts      ON tasks(erledigt_ts) WHERE status IN ('success','failed','cancelled');

-- ══════════ Audit-Log (append-only) ══════════
CREATE TABLE IF NOT EXISTS audit_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    action          TEXT NOT NULL,
    actor           TEXT NOT NULL,
    detail          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts  ON audit_log(ts);
CREATE TRIGGER IF NOT EXISTS audit_no_update
    BEFORE UPDATE ON audit_log BEGIN
    SELECT RAISE(FAIL, 'audit_log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
    BEFORE DELETE ON audit_log BEGIN
    SELECT RAISE(FAIL, 'audit_log is append-only');
END;

-- ══════════ Cron Dedup State ══════════
CREATE TABLE IF NOT EXISTS cron_state (
    modul               TEXT PRIMARY KEY,
    last_fire_minute    TEXT NOT NULL   -- "YYYY-MM-DD HH:MM"
);

-- ══════════ Token-Stats (daily) ══════════
CREATE TABLE IF NOT EXISTS token_stats (
    day_key         TEXT PRIMARY KEY,   -- "YYYY-MM-DD" UTC
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    calls           INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    reserved_usd    REAL    NOT NULL DEFAULT 0.0,
    reserved_calls  INTEGER NOT NULL DEFAULT 0
);

-- Letzte N Token-Calls für UI-Recent-List (200-Grenze via trigger)
CREATE TABLE IF NOT EXISTS token_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    backend         TEXT NOT NULL,
    model           TEXT NOT NULL,
    modul           TEXT NOT NULL,
    input_tokens    INTEGER NOT NULL,
    output_tokens   INTEGER NOT NULL,
    cost_usd        REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_calls_ts ON token_calls(ts);
CREATE TRIGGER IF NOT EXISTS token_calls_cap
    AFTER INSERT ON token_calls
    WHEN (SELECT COUNT(*) FROM token_calls) > 200
    BEGIN
        DELETE FROM token_calls WHERE id IN
            (SELECT id FROM token_calls ORDER BY ts ASC
             LIMIT (SELECT COUNT(*) FROM token_calls) - 200);
    END;

-- ══════════ Idempotency (exactly-once für Side-Effect-Tools) ══════════
CREATE TABLE IF NOT EXISTS idempotency (
    key             TEXT PRIMARY KEY,   -- sha256(task_id|tool|params)
    result_success  INTEGER NOT NULL,   -- 0/1
    result_data     TEXT NOT NULL,
    ts              INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idempotency_ts ON idempotency(ts);

-- ══════════ Conversations ══════════
CREATE TABLE IF NOT EXISTS conversations (
    modul_id        TEXT NOT NULL,
    convo_id        TEXT NOT NULL,
    data_json       TEXT NOT NULL,
    updated_ts      INTEGER NOT NULL,
    PRIMARY KEY (modul_id, convo_id)
);
CREATE INDEX IF NOT EXISTS idx_convos_updated ON conversations(modul_id, updated_ts DESC);

-- ══════════ Schema-Version (Migration-Marker) ══════════
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_ts INTEGER NOT NULL
);
INSERT OR IGNORE INTO schema_version(version, applied_ts) VALUES (1, strftime('%s','now'));
"#;

// ═══════════════════════════════════════════════════════════════════════════
// Task-API
// ═══════════════════════════════════════════════════════════════════════════

/// Opake Task-Repräsentation. Das konkrete Aufgabe-Struct lebt in types.rs;
/// der Store serialisiert es als JSON. Das erlaubt Schema-Evolution ohne
/// SQL-Migration für Task-Feld-Änderungen.
pub struct TaskRow {
    pub id: String,
    pub status: String,
    pub modul: String,
    pub payload_json: String,
    pub erstellt_ts: i64,
    pub gestartet_ts: Option<i64>,
    pub erledigt_ts: Option<i64>,
}

impl TaskRow {
    fn from_row(row: &rusqlite::Row) -> rusqlite::Result<Self> {
        Ok(TaskRow {
            id: row.get("id")?,
            status: row.get("status")?,
            modul: row.get("modul")?,
            payload_json: row.get("payload_json")?,
            erstellt_ts: row.get("erstellt_ts")?,
            gestartet_ts: row.get("gestartet_ts")?,
            erledigt_ts: row.get("erledigt_ts")?,
        })
    }
}

/// Speichert eine Aufgabe (insert-or-replace). Für neue Tasks und idempotente
/// Updates (gleicher Status, neuer Payload) ok. State-Transitions gehen über
/// `transition` damit sie atomar mit Status-Wechsel + Timestamp sind.
pub fn task_upsert(
    pool: &SqlitePool,
    id: &str, status: &str, modul: &str, payload_json: &str,
    erstellt_ts: i64, faellig_ab_ts: i64,
) -> StoreResult<()> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "INSERT INTO tasks (id, status, modul, payload_json, erstellt_ts, faellig_ab_ts)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)
         ON CONFLICT(id) DO UPDATE SET
           status=excluded.status,
           modul=excluded.modul,
           payload_json=excluded.payload_json,
           faellig_ab_ts=excluded.faellig_ab_ts",
        params![id, status, modul, payload_json, erstellt_ts, faellig_ab_ts],
    ).map_err(e("task_upsert"))?;
    Ok(())
}

/// Atomic claim: genau EIN Caller bekommt die älteste FÄLLIGE `erstellt`-Task
/// für dieses Modul, alle anderen bekommen None. Nutzt `BEGIN IMMEDIATE` um
/// den SELECT+UPDATE-Block gegen Concurrent-Writer zu serialisieren.
///
/// Fairness-Garantie: die WHERE-Clause prüft `faellig_ab_ts <= now` direkt
/// in SQL — später-geplante Tasks blockieren nicht mehr die Queue für
/// sofort-fällige. Früher wurde ist_faellig() auf dem Client geprüft NACHDEM
/// der Task geclaimed war, und ein break stoppte das Weitersuchen; das
/// führte zu Starvation von späteren Tasks in derselben Queue.
pub fn claim_one_for_modul(pool: &SqlitePool, modul: &str) -> StoreResult<Option<TaskRow>> {
    let now = chrono::Utc::now().timestamp();
    let mut conn = pool.get().map_err(e("pool"))?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(e("begin"))?;

    let id: Option<String> = tx.query_row(
        "SELECT id FROM tasks
         WHERE status='erstellt' AND modul=?1 AND faellig_ab_ts<=?2
         ORDER BY faellig_ab_ts ASC, erstellt_ts ASC LIMIT 1",
        params![modul, now],
        |r| r.get(0),
    ).optional().map_err(e("claim select"))?;

    let Some(id) = id else {
        tx.commit().map_err(e("commit empty"))?;
        return Ok(None);
    };

    let claim_token = uuid::Uuid::new_v4().to_string();

    // Atomic transition: nur wenn Status noch 'erstellt' ist (sonst hat ein
    // anderer Caller schon gewonnen). Rowcount == 1 → unser Claim.
    let changed = tx.execute(
        "UPDATE tasks
         SET status='gestartet', gestartet_ts=?1, claim_token=?2
         WHERE id=?3 AND status='erstellt'",
        params![now, claim_token, id],
    ).map_err(e("claim update"))?;

    if changed == 0 {
        tx.commit().map_err(e("commit noop"))?;
        return Ok(None);
    }

    let row = tx.query_row(
        "SELECT id, status, modul, payload_json, erstellt_ts, gestartet_ts, erledigt_ts
         FROM tasks WHERE id=?1",
        params![id], TaskRow::from_row,
    ).map_err(e("claim reload"))?;
    tx.commit().map_err(e("commit"))?;
    Ok(Some(row))
}

/// Parse "wann"-Feld einer Aufgabe zu Unix-Timestamp. "sofort" → 0 (immer fällig).
/// Unbekannte Formate → 0 (konservativ: lieber sofort als nie — aber
/// ist_faellig() auf dem Cycle-Level fängt invalide Formate vor der Execution).
/// Gültige ISO-8601 → entsprechender Timestamp. Nutze im Aufruf zu task_upsert
/// damit claim_one_for_modul per SQL filtern kann.
pub fn parse_faellig_ab(wann: &str) -> i64 {
    match wann {
        "sofort" => 0,
        w if w.starts_with("20") => {
            w.parse::<chrono::DateTime<chrono::Utc>>()
                .map(|dt| dt.timestamp())
                .unwrap_or(0)
        }
        _ => 0,
    }
}

/// State-Transition mit Timestamp-Update + Payload-Ersetzung. Atomar: sollte
/// ein anderer Writer gleichzeitig schreiben, serialisiert SQLite das.
pub fn task_transition(
    pool: &SqlitePool,
    id: &str, new_status: &str, new_payload_json: &str,
) -> StoreResult<()> {
    let now = chrono::Utc::now().timestamp();
    let conn = pool.get().map_err(e("pool"))?;

    let (set_gestartet, set_erledigt) = match new_status {
        "gestartet" => (Some(now), None),
        "success" | "failed" | "cancelled" => (None, Some(now)),
        _ => (None, None),
    };

    if set_gestartet.is_some() {
        conn.execute(
            "UPDATE tasks SET status=?1, payload_json=?2, gestartet_ts=?3 WHERE id=?4",
            params![new_status, new_payload_json, set_gestartet, id],
        ).map_err(e("transition g"))?;
    } else if set_erledigt.is_some() {
        conn.execute(
            "UPDATE tasks SET status=?1, payload_json=?2, erledigt_ts=?3 WHERE id=?4",
            params![new_status, new_payload_json, set_erledigt, id],
        ).map_err(e("transition e"))?;
    } else {
        conn.execute(
            "UPDATE tasks SET status=?1, payload_json=?2 WHERE id=?3",
            params![new_status, new_payload_json, id],
        ).map_err(e("transition s"))?;
    }
    Ok(())
}

pub fn task_load_by_id(pool: &SqlitePool, id: &str) -> StoreResult<Option<TaskRow>> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.query_row(
        "SELECT id, status, modul, payload_json, erstellt_ts, gestartet_ts, erledigt_ts
         FROM tasks WHERE id=?1",
        params![id], TaskRow::from_row,
    ).optional().map_err(e("task_load_by_id"))
}

pub fn task_list_by_status(pool: &SqlitePool, status: &str) -> StoreResult<Vec<TaskRow>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT id, status, modul, payload_json, erstellt_ts, gestartet_ts, erledigt_ts
         FROM tasks WHERE status=?1 ORDER BY erstellt_ts ASC"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![status], TaskRow::from_row)
        .map_err(e("query"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(e("collect"))?;
    Ok(rows)
}

pub fn task_list_erledigt_recent(pool: &SqlitePool, limit: usize) -> StoreResult<Vec<TaskRow>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT id, status, modul, payload_json, erstellt_ts, gestartet_ts, erledigt_ts
         FROM tasks
         WHERE status IN ('success','failed','cancelled')
         ORDER BY erledigt_ts DESC LIMIT ?1"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![limit as i64], TaskRow::from_row)
        .map_err(e("query"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(e("collect"))?;
    Ok(rows)
}

/// Cleanup erledigter Tasks älter als max_age_days, zusätzlich cap bei max_count.
pub fn task_cleanup_erledigt(pool: &SqlitePool, max_count: usize, max_age_days: u32) -> StoreResult<usize> {
    let cutoff = chrono::Utc::now().timestamp() - (max_age_days as i64) * 86400;
    let conn = pool.get().map_err(e("pool"))?;

    // Älter als cutoff löschen
    let by_age = conn.execute(
        "DELETE FROM tasks
         WHERE status IN ('success','failed','cancelled')
           AND erledigt_ts IS NOT NULL AND erledigt_ts < ?1",
        params![cutoff],
    ).map_err(e("cleanup age"))?;

    // Wenn noch mehr als max_count, die ältesten oberhalb löschen
    let total: i64 = conn.query_row(
        "SELECT COUNT(*) FROM tasks WHERE status IN ('success','failed','cancelled')",
        [], |r| r.get(0),
    ).map_err(e("count"))?;

    let by_cap = if total > max_count as i64 {
        let overflow = total - max_count as i64;
        conn.execute(
            "DELETE FROM tasks WHERE id IN (
                SELECT id FROM tasks
                WHERE status IN ('success','failed','cancelled')
                ORDER BY erledigt_ts ASC LIMIT ?1
             )",
            params![overflow],
        ).map_err(e("cleanup cap"))?
    } else { 0 };

    Ok(by_age + by_cap)
}

// ═══════════════════════════════════════════════════════════════════════════
// Audit-Log
// ═══════════════════════════════════════════════════════════════════════════

pub fn audit(pool: &SqlitePool, action: &str, actor: &str, detail: &str) -> StoreResult<()> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "INSERT INTO audit_log (ts, action, actor, detail) VALUES (?1, ?2, ?3, ?4)",
        params![chrono::Utc::now().timestamp(), action, actor, detail],
    ).map_err(e("audit insert"))?;
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AuditEntry {
    pub ts: i64,
    pub action: String,
    pub actor: String,
    pub detail: String,
}

pub fn audit_recent(pool: &SqlitePool, limit: usize) -> StoreResult<Vec<AuditEntry>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT ts, action, actor, detail FROM audit_log ORDER BY ts DESC LIMIT ?1"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![limit as i64], |r| Ok(AuditEntry {
        ts: r.get(0)?, action: r.get(1)?, actor: r.get(2)?, detail: r.get(3)?,
    })).map_err(e("query"))?
      .collect::<Result<Vec<_>, _>>().map_err(e("collect"))?;
    Ok(rows)
}

/// Audit-Filter: optional action/actor prefix, Zeit-Range, limit. UI ruft's
/// mit unterschiedlichen Filtern aus dem Audit-Tab. Prepared-Statements
/// werden cached pro Kombination, aber der Set an Kombis ist klein.
pub fn audit_filtered(
    pool: &SqlitePool,
    action: Option<&str>,
    actor: Option<&str>,
    since_ts: Option<i64>,
    limit: usize,
) -> StoreResult<Vec<AuditEntry>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut sql = String::from("SELECT ts, action, actor, detail FROM audit_log WHERE 1=1");
    if action.is_some() { sql.push_str(" AND action LIKE ?"); }
    if actor.is_some() { sql.push_str(" AND actor LIKE ?"); }
    if since_ts.is_some() { sql.push_str(" AND ts >= ?"); }
    sql.push_str(" ORDER BY ts DESC LIMIT ?");
    let mut stmt = conn.prepare(&sql).map_err(e("prepare"))?;
    let mut args: Vec<Box<dyn rusqlite::ToSql>> = Vec::new();
    if let Some(a) = action { args.push(Box::new(format!("{}%", a))); }
    if let Some(a) = actor { args.push(Box::new(format!("{}%", a))); }
    if let Some(t) = since_ts { args.push(Box::new(t)); }
    args.push(Box::new(limit as i64));
    let borrowed: Vec<&dyn rusqlite::ToSql> = args.iter().map(|b| b.as_ref()).collect();
    let rows = stmt.query_map(borrowed.as_slice(), |r| Ok(AuditEntry {
        ts: r.get(0)?, action: r.get(1)?, actor: r.get(2)?, detail: r.get(3)?,
    })).map_err(e("query"))?
      .collect::<Result<Vec<_>, _>>().map_err(e("collect"))?;
    Ok(rows)
}

// ═══════════════════════════════════════════════════════════════════════════
// Cron-State
// ═══════════════════════════════════════════════════════════════════════════

/// Liefert true wenn der Cron für dieses Modul JETZT (minute_key) noch nicht
/// gefeuert hat — und markiert ihn atomar als gefeuert. Der Insert+Update in
/// einer Transaktion schließt die Race zwischen "check" und "spawn" die das
/// alte cron_state.json-JSON-File-basierte System hatte.
pub fn cron_try_claim(pool: &SqlitePool, modul: &str, minute_key: &str) -> StoreResult<bool> {
    let mut conn = pool.get().map_err(e("pool"))?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(e("begin"))?;
    let current: Option<String> = tx.query_row(
        "SELECT last_fire_minute FROM cron_state WHERE modul=?1",
        params![modul], |r| r.get(0),
    ).optional().map_err(e("cron select"))?;
    if let Some(c) = current {
        if c == minute_key {
            tx.commit().map_err(e("commit dup"))?;
            return Ok(false);
        }
    }
    tx.execute(
        "INSERT INTO cron_state (modul, last_fire_minute) VALUES (?1, ?2)
         ON CONFLICT(modul) DO UPDATE SET last_fire_minute=excluded.last_fire_minute",
        params![modul, minute_key],
    ).map_err(e("cron upsert"))?;
    tx.commit().map_err(e("commit"))?;
    Ok(true)
}

pub fn cron_prune_stale(pool: &SqlitePool, keep_moduls: &[String]) -> StoreResult<()> {
    if keep_moduls.is_empty() {
        let conn = pool.get().map_err(e("pool"))?;
        conn.execute("DELETE FROM cron_state", []).map_err(e("prune"))?;
        return Ok(());
    }
    let placeholders = keep_moduls.iter().map(|_| "?").collect::<Vec<_>>().join(",");
    let sql = format!("DELETE FROM cron_state WHERE modul NOT IN ({})", placeholders);
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare(&sql).map_err(e("prepare"))?;
    let params: Vec<&dyn rusqlite::ToSql> = keep_moduls.iter().map(|s| s as &dyn rusqlite::ToSql).collect();
    stmt.execute(params.as_slice()).map_err(e("exec"))?;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════════
// Token-Stats (persistent daily budget)
// ═══════════════════════════════════════════════════════════════════════════

#[derive(Debug, Clone, Default, Serialize)]
pub struct DayStats {
    pub day_key: String,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub calls: u64,
    pub cost_usd: f64,
    pub reserved_usd: f64,
    pub reserved_calls: u64,
}

fn today_key() -> String {
    chrono::Utc::now().format("%Y-%m-%d").to_string()
}

pub fn token_day_get(pool: &SqlitePool) -> StoreResult<DayStats> {
    let day_key = today_key();
    let conn = pool.get().map_err(e("pool"))?;
    let existing: Option<DayStats> = conn.query_row(
        "SELECT day_key, input_tokens, output_tokens, calls, cost_usd, reserved_usd, reserved_calls
         FROM token_stats WHERE day_key=?1",
        params![day_key],
        |r| Ok(DayStats {
            day_key: r.get(0)?, input_tokens: r.get::<_,i64>(1)? as u64,
            output_tokens: r.get::<_,i64>(2)? as u64, calls: r.get::<_,i64>(3)? as u64,
            cost_usd: r.get(4)?, reserved_usd: r.get(5)?,
            reserved_calls: r.get::<_,i64>(6)? as u64,
        }),
    ).optional().map_err(e("day_get"))?;
    Ok(existing.unwrap_or(DayStats { day_key, ..Default::default() }))
}

/// Atomar: Reservation addieren WENN projected total unter Budget bleibt. Sonst Err.
/// Gibt true/false zurück ob die Reservation gebucht wurde (plus aktuelle Stats).
pub fn token_reserve(pool: &SqlitePool, estimated_usd: f64, budget: Option<f64>) -> StoreResult<Result<f64, String>> {
    let day_key = today_key();
    let mut conn = pool.get().map_err(e("pool"))?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(e("begin"))?;
    let (cost, reserved): (f64, f64) = tx.query_row(
        "SELECT COALESCE(cost_usd,0), COALESCE(reserved_usd,0)
         FROM token_stats WHERE day_key=?1",
        params![day_key], |r| Ok((r.get(0)?, r.get(1)?)),
    ).optional().map_err(e("reserve sel"))?.unwrap_or((0.0, 0.0));

    if let Some(cap) = budget {
        if cap > 0.0 && cost + reserved + estimated_usd > cap {
            tx.commit().map_err(e("commit deny"))?;
            return Ok(Err(format!(
                "Daily USD budget would be exceeded: committed ${:.4} + reserved ${:.4} + this call ${:.4} > ${:.2}",
                cost, reserved, estimated_usd, cap
            )));
        }
    }
    tx.execute(
        "INSERT INTO token_stats (day_key, reserved_usd, reserved_calls) VALUES (?1, ?2, 1)
         ON CONFLICT(day_key) DO UPDATE SET
            reserved_usd = reserved_usd + excluded.reserved_usd,
            reserved_calls = reserved_calls + 1",
        params![day_key, estimated_usd],
    ).map_err(e("reserve upsert"))?;
    tx.commit().map_err(e("commit ok"))?;
    Ok(Ok(cost + reserved + estimated_usd))
}

pub fn token_release_reservation(pool: &SqlitePool, estimated_usd: f64) -> StoreResult<()> {
    let day_key = today_key();
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "UPDATE token_stats
         SET reserved_usd = MAX(0, reserved_usd - ?1),
             reserved_calls = MAX(0, reserved_calls - 1)
         WHERE day_key=?2",
        params![estimated_usd, day_key],
    ).map_err(e("release"))?;
    Ok(())
}

/// Actual commit nach einem LLM-Call: Reservation auflösen, Actual-Kosten addieren.
pub fn token_commit_actual(
    pool: &SqlitePool,
    estimated_usd: f64, actual_usd: f64,
    input_tokens: u64, output_tokens: u64,
    backend: &str, model: &str, modul: &str,
) -> StoreResult<()> {
    let day_key = today_key();
    let now = chrono::Utc::now().timestamp();
    let mut conn = pool.get().map_err(e("pool"))?;
    let tx = conn.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)
        .map_err(e("begin"))?;
    tx.execute(
        "INSERT INTO token_stats (day_key, input_tokens, output_tokens, calls, cost_usd)
         VALUES (?1, ?2, ?3, 1, ?4)
         ON CONFLICT(day_key) DO UPDATE SET
            input_tokens  = input_tokens  + excluded.input_tokens,
            output_tokens = output_tokens + excluded.output_tokens,
            calls         = calls + 1,
            cost_usd      = cost_usd + excluded.cost_usd,
            reserved_usd  = MAX(0, reserved_usd - ?5),
            reserved_calls = MAX(0, reserved_calls - 1)",
        params![day_key, input_tokens as i64, output_tokens as i64, actual_usd, estimated_usd],
    ).map_err(e("token upsert"))?;
    tx.execute(
        "INSERT INTO token_calls (ts, backend, model, modul, input_tokens, output_tokens, cost_usd)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![now, backend, model, modul, input_tokens as i64, output_tokens as i64, actual_usd],
    ).map_err(e("calls insert"))?;
    tx.commit().map_err(e("commit"))?;
    Ok(())
}

#[derive(Debug, Clone, Serialize)]
pub struct TokenCallRow {
    pub ts: i64,
    pub backend: String,
    pub model: String,
    pub modul: String,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cost_usd: f64,
}

/// Aggregiert die letzten N Tage pro Modul: Total-Tokens, Calls, Cost. UI nutzt
/// das für die "Top-Burner"-Liste im Token-Tab. Wenn UI pro-Task-Cost will
/// (zu welcher Aufgabe gehörte der Call), dann müssten wir task_id in
/// token_calls speichern — aktuell nicht, Scope für nächste Runde.
pub fn tokens_by_modul(pool: &SqlitePool, days: i64) -> StoreResult<Vec<serde_json::Value>> {
    let cutoff = chrono::Utc::now().timestamp() - days * 86400;
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT modul,
                COUNT(*) AS calls,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
         FROM token_calls WHERE ts >= ?1
         GROUP BY modul ORDER BY cost_usd DESC, calls DESC"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![cutoff], |r| Ok(serde_json::json!({
        "modul": r.get::<_, String>(0)?,
        "calls": r.get::<_, i64>(1)?,
        "input_tokens": r.get::<_, i64>(2)?,
        "output_tokens": r.get::<_, i64>(3)?,
        "cost_usd": r.get::<_, f64>(4)?,
    }))).map_err(e("query"))?
      .collect::<Result<Vec<_>, _>>().map_err(e("collect"))?;
    Ok(rows)
}

pub fn tokens_by_backend(pool: &SqlitePool, days: i64) -> StoreResult<Vec<serde_json::Value>> {
    let cutoff = chrono::Utc::now().timestamp() - days * 86400;
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT backend, model,
                COUNT(*) AS calls,
                SUM(input_tokens) AS input_tokens,
                SUM(output_tokens) AS output_tokens,
                SUM(cost_usd) AS cost_usd
         FROM token_calls WHERE ts >= ?1
         GROUP BY backend, model ORDER BY cost_usd DESC"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![cutoff], |r| Ok(serde_json::json!({
        "backend": r.get::<_, String>(0)?,
        "model": r.get::<_, String>(1)?,
        "calls": r.get::<_, i64>(2)?,
        "input_tokens": r.get::<_, i64>(3)?,
        "output_tokens": r.get::<_, i64>(4)?,
        "cost_usd": r.get::<_, f64>(5)?,
    }))).map_err(e("query"))?
      .collect::<Result<Vec<_>, _>>().map_err(e("collect"))?;
    Ok(rows)
}

pub fn token_calls_recent(pool: &SqlitePool, limit: usize) -> StoreResult<Vec<TokenCallRow>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT ts, backend, model, modul, input_tokens, output_tokens, cost_usd
         FROM token_calls ORDER BY ts DESC LIMIT ?1"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![limit as i64], |r| Ok(TokenCallRow {
        ts: r.get(0)?, backend: r.get(1)?, model: r.get(2)?, modul: r.get(3)?,
        input_tokens: r.get(4)?, output_tokens: r.get(5)?, cost_usd: r.get(6)?,
    })).map_err(e("query"))?
      .collect::<Result<Vec<_>, _>>().map_err(e("collect"))?;
    Ok(rows)
}

/// Summiert alle Stats seit Prozess-Start. Für UI-Anzeige.
pub fn token_all_time(pool: &SqlitePool) -> StoreResult<(u64, u64, u64, f64)> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.query_row(
        "SELECT COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0),
                COALESCE(SUM(calls),0), COALESCE(SUM(cost_usd),0.0)
         FROM token_stats",
        [], |r| Ok((
            r.get::<_,i64>(0)? as u64, r.get::<_,i64>(1)? as u64,
            r.get::<_,i64>(2)? as u64, r.get::<_,f64>(3)?,
        )),
    ).map_err(e("all_time"))
}

// ═══════════════════════════════════════════════════════════════════════════
// Idempotency (exactly-once für Side-Effect-Tools)
// ═══════════════════════════════════════════════════════════════════════════

/// Key: sha256 über (task_id + "|" + tool_name + "|" + params joined). Stabil über
/// Prozess-Restarts. 32-Byte-Hash → hex-encoded, 64 chars.
pub fn idempotency_key(task_id: &str, tool_name: &str, params: &[String]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(task_id.as_bytes());
    hasher.update(b"|");
    hasher.update(tool_name.as_bytes());
    hasher.update(b"|");
    for (i, p) in params.iter().enumerate() {
        if i > 0 { hasher.update(b"\x1f"); } // unit separator
        hasher.update(p.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

/// Sentinel-Wert für "Tool-Execution läuft gerade / wurde unterbrochen". Wird
/// VOR der eigentlichen Tool-Ausführung als Marker geschrieben und nach Erfolg
/// durch das echte Result ersetzt. Bleibt er stehen (Prozess-Crash, Watchdog-
/// Abort zwischen Execute und Store), signalisiert ein Retry-Caller beim
/// Lookup "Status ambiguous — bitte manuell klären".
pub const IDEMPOTENCY_IN_PROGRESS: &str = "__IDEMPOTENCY_IN_PROGRESS__";

pub fn idempotency_get(pool: &SqlitePool, key: &str) -> StoreResult<Option<(bool, String)>> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.query_row(
        "SELECT result_success, result_data FROM idempotency WHERE key=?1",
        params![key],
        |r| Ok((r.get::<_,i64>(0)? != 0, r.get(1)?)),
    ).optional().map_err(e("idempotency_get"))
}

pub fn idempotency_store(pool: &SqlitePool, key: &str, success: bool, data: &str) -> StoreResult<()> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "INSERT OR REPLACE INTO idempotency (key, result_success, result_data, ts)
         VALUES (?1, ?2, ?3, ?4)",
        params![key, success as i64, data, chrono::Utc::now().timestamp()],
    ).map_err(e("idempotency_store"))?;
    Ok(())
}

/// Markiert einen Idempotency-Key als "in-progress" — schreibt den Sentinel-
/// Wert IDEMPOTENCY_IN_PROGRESS. Soll VOR dem Tool-Call gerufen werden; nach
/// der Tool-Ausführung ersetzt `idempotency_store` den Marker durch das echte
/// Result. Wenn zwischen Mark und Store ein Crash/Abort passiert, sieht ein
/// Retry den Marker und kann entscheiden (aktuell: FAIL mit Grund, User-
/// Manual-Resolve — besser als blindes Re-Execute eines möglicherweise
/// bereits-ausgeführten Side-Effects).
pub fn idempotency_mark_in_progress(pool: &SqlitePool, key: &str) -> StoreResult<()> {
    idempotency_store(pool, key, false, IDEMPOTENCY_IN_PROGRESS)
}

pub fn idempotency_delete(pool: &SqlitePool, key: &str) -> StoreResult<()> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute("DELETE FROM idempotency WHERE key=?1", params![key])
        .map_err(e("idempotency_delete"))?;
    Ok(())
}

/// Löscht Idempotency-Einträge älter als retention_days. Standard: 30 Tage.
pub fn idempotency_cleanup(pool: &SqlitePool, retention_days: u32) -> StoreResult<usize> {
    let cutoff = chrono::Utc::now().timestamp() - (retention_days as i64) * 86400;
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute("DELETE FROM idempotency WHERE ts < ?1", params![cutoff])
        .map_err(e("idem cleanup"))
}

/// Löscht "stale" IN_PROGRESS-Marker älter als `timeout_secs`. Schützt vor
/// dem Dead-End-Szenario: Prozess crashed mid-execute → Marker bleibt →
/// jeder Retry kriegt AMBIGUOUS-Fehler bis ein Mensch den Eintrag manuell
/// löscht. Nach `timeout_secs` (Default 10 min — länger als jeder realistische
/// Tool-Call inkl. LLM-Retries) wird der Marker automatisch gelöscht; der
/// nächste Retry kann dann wieder normal durchlaufen. Side-Effect-Tools
/// müssen trotzdem selbst idempotent sein für den Fall dass der Erst-Call
/// tatsächlich durchging, aber das ist für die meisten Tools (rag.speichern,
/// files.write mit selbem Content, notify.send mit dedup-key) ok.
/// GPT-Finding Run SQLite-8.
pub fn idempotency_expire_in_progress(pool: &SqlitePool, timeout_secs: i64) -> StoreResult<usize> {
    let cutoff = chrono::Utc::now().timestamp() - timeout_secs;
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "DELETE FROM idempotency WHERE result_data=?1 AND ts < ?2",
        params![IDEMPOTENCY_IN_PROGRESS, cutoff],
    ).map_err(e("idem expire"))
}

// ═══════════════════════════════════════════════════════════════════════════
// Conversations
// ═══════════════════════════════════════════════════════════════════════════

pub fn convo_save(pool: &SqlitePool, modul_id: &str, convo_id: &str, data_json: &str) -> StoreResult<()> {
    let now = chrono::Utc::now().timestamp();
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "INSERT INTO conversations (modul_id, convo_id, data_json, updated_ts)
         VALUES (?1, ?2, ?3, ?4)
         ON CONFLICT(modul_id, convo_id) DO UPDATE SET
            data_json=excluded.data_json,
            updated_ts=excluded.updated_ts",
        params![modul_id, convo_id, data_json, now],
    ).map_err(e("convo_save"))?;
    Ok(())
}

pub fn convo_load(pool: &SqlitePool, modul_id: &str, convo_id: &str) -> StoreResult<Option<String>> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.query_row(
        "SELECT data_json FROM conversations WHERE modul_id=?1 AND convo_id=?2",
        params![modul_id, convo_id], |r| r.get(0),
    ).optional().map_err(e("convo_load"))
}

pub fn convo_list(pool: &SqlitePool, modul_id: &str) -> StoreResult<Vec<String>> {
    let conn = pool.get().map_err(e("pool"))?;
    let mut stmt = conn.prepare_cached(
        "SELECT data_json FROM conversations WHERE modul_id=?1 ORDER BY updated_ts DESC"
    ).map_err(e("prepare"))?;
    let rows = stmt.query_map(params![modul_id], |r| r.get::<_, String>(0))
        .map_err(e("query"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(e("collect"))?;
    Ok(rows)
}

pub fn convo_delete(pool: &SqlitePool, modul_id: &str, convo_id: &str) -> StoreResult<()> {
    let conn = pool.get().map_err(e("pool"))?;
    conn.execute(
        "DELETE FROM conversations WHERE modul_id=?1 AND convo_id=?2",
        params![modul_id, convo_id],
    ).map_err(e("convo_delete"))?;
    Ok(())
}

// ═══════════════════════════════════════════════════════════════════════════
// Store handle (Arc-wrapper für Axum/Orchestrator)
// ═══════════════════════════════════════════════════════════════════════════

#[derive(Clone)]
pub struct Store {
    pub pool: Arc<SqlitePool>,
}

impl Store {
    pub fn open(db_path: &Path) -> StoreResult<Self> {
        Ok(Self { pool: Arc::new(open_pool(db_path)?) })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn in_memory_pool() -> SqlitePool {
        // :memory: DB für schnelle Unit-Tests. WAL funktioniert auf :memory: nicht,
        // deshalb nur im Test das pragma weglassen via direktem manager.
        let manager = SqliteConnectionManager::memory()
            .with_init(|c| { c.execute_batch("PRAGMA foreign_keys=ON;")?; Ok(()) });
        let pool = Pool::builder().max_size(4).build(manager).unwrap();
        let conn = pool.get().unwrap();
        conn.execute_batch(SCHEMA).unwrap();
        pool
    }

    #[test]
    fn claim_is_atomic_exactly_one_winner() {
        let pool = in_memory_pool();
        task_upsert(&pool, "t1", "erstellt", "mail", "{}", 100, 0).unwrap();

        // Ein Claim gewinnt, der zweite bekommt None
        let got1 = claim_one_for_modul(&pool, "mail").unwrap();
        assert!(got1.is_some());
        assert_eq!(got1.unwrap().status, "gestartet");

        let got2 = claim_one_for_modul(&pool, "mail").unwrap();
        assert!(got2.is_none(), "zweiter Claim muss leer sein");
    }

    #[test]
    fn concurrent_claims_serialize() {
        // Viele Tasks, viele Worker-Threads, am Ende hat jeder Task genau
        // einen Gewinner (der ihn claimed) oder bleibt in erstellt.
        let pool = Arc::new(in_memory_pool());
        for i in 0..50 {
            task_upsert(&pool, &format!("t{}", i), "erstellt", "worker", "{}", i as i64, 0).unwrap();
        }

        let mut handles = vec![];
        for _ in 0..10 {
            let p = pool.clone();
            handles.push(std::thread::spawn(move || {
                let mut claimed = 0;
                while let Ok(Some(_)) = claim_one_for_modul(&p, "worker") {
                    claimed += 1;
                }
                claimed
            }));
        }
        let total: i64 = handles.into_iter().map(|h| h.join().unwrap()).sum();
        assert_eq!(total, 50, "genau 50 claims insgesamt, keine doppelten");
    }

    #[test]
    fn audit_is_append_only() {
        let pool = in_memory_pool();
        audit(&pool, "tool_exec", "test", "detail").unwrap();
        let conn = pool.get().unwrap();
        let err = conn.execute("UPDATE audit_log SET action='tampered'", []);
        assert!(err.is_err(), "UPDATE auf audit_log muss fehlschlagen (Trigger)");
        let err2 = conn.execute("DELETE FROM audit_log", []);
        assert!(err2.is_err(), "DELETE auf audit_log muss fehlschlagen (Trigger)");
    }

    #[test]
    fn cron_dedup_blocks_double_fire() {
        let pool = in_memory_pool();
        let key = "2026-04-23 10:00";
        assert!(cron_try_claim(&pool, "m1", key).unwrap(), "erster Fire erlaubt");
        assert!(!cron_try_claim(&pool, "m1", key).unwrap(), "zweiter Fire blockiert");
        assert!(cron_try_claim(&pool, "m1", "2026-04-23 10:01").unwrap(), "neue Minute erlaubt");
    }

    #[test]
    fn idempotency_roundtrip() {
        let pool = in_memory_pool();
        let key = idempotency_key("task-1", "shell.exec", &["ls".into()]);
        assert!(idempotency_get(&pool, &key).unwrap().is_none());
        idempotency_store(&pool, &key, true, "output").unwrap();
        let got = idempotency_get(&pool, &key).unwrap().unwrap();
        assert!(got.0);
        assert_eq!(got.1, "output");
    }

    #[test]
    fn idempotency_key_stability() {
        let k1 = idempotency_key("t1", "files.write", &["/tmp/x".into(), "hi".into()]);
        let k2 = idempotency_key("t1", "files.write", &["/tmp/x".into(), "hi".into()]);
        assert_eq!(k1, k2, "selbe Inputs → selber Key");
        let k3 = idempotency_key("t1", "files.write", &["/tmp/y".into(), "hi".into()]);
        assert_ne!(k1, k3, "anderer Pfad → anderer Key");
    }

    #[test]
    fn token_reserve_enforces_cap_atomically() {
        let pool = in_memory_pool();
        let budget = Some(0.5);
        // Erste Reservation geht
        let r1 = token_reserve(&pool, 0.2, budget).unwrap();
        assert!(r1.is_ok());
        // Zweite Reservation hat 0.2 + 0.2 = 0.4 ≤ 0.5 → geht
        let r2 = token_reserve(&pool, 0.2, budget).unwrap();
        assert!(r2.is_ok());
        // Dritte wäre 0.4 + 0.2 = 0.6 > 0.5 → Err
        let r3 = token_reserve(&pool, 0.2, budget).unwrap();
        assert!(r3.is_err(), "dritte Reservation muss blockieren");
    }

    #[test]
    fn token_commit_releases_reservation() {
        let pool = in_memory_pool();
        token_reserve(&pool, 0.1, Some(1.0)).unwrap().unwrap();
        let before = token_day_get(&pool).unwrap();
        assert!((before.reserved_usd - 0.1).abs() < 1e-9);
        token_commit_actual(&pool, 0.1, 0.15, 100, 50, "b", "m", "mod").unwrap();
        let after = token_day_get(&pool).unwrap();
        assert!((after.reserved_usd).abs() < 1e-9, "reservation weg");
        assert!((after.cost_usd - 0.15).abs() < 1e-9, "actual gebucht");
        assert_eq!(after.calls, 1);
    }
}
