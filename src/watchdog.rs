use std::sync::Arc;
use crate::pipeline::Pipeline;
use crate::types::{LogTyp, AufgabeStatus};
use crate::cycle::{BusyMap, HandleMap, HeartbeatMap};

pub struct Watchdog {
    heartbeats: HeartbeatMap,
    timeout_secs: u64,
    pipeline: Arc<Pipeline>,
    busy: BusyMap,
    handles: HandleMap,
}

impl Watchdog {
    pub fn new(
        heartbeats: HeartbeatMap,
        timeout_secs: u64,
        pipeline: Arc<Pipeline>,
        busy: BusyMap,
        handles: HandleMap,
    ) -> Self {
        Self { heartbeats, timeout_secs, pipeline, busy, handles }
    }

    pub async fn run(&self) {
        self.pipeline.log("watchdog", None, LogTyp::Info,
            &format!("Watchdog gestartet (timeout: {}s pro Scheduler)", self.timeout_secs));
        loop {
            tokio::time::sleep(tokio::time::Duration::from_secs(10)).await;
            let now = chrono::Utc::now().timestamp() as u64;
            let hb_snapshot: Vec<(String, u64)> = {
                let hb = self.heartbeats.read().await;
                hb.iter().map(|(k, v)| (k.clone(), *v)).collect()
            };
            for (modul_id, last_beat) in hb_snapshot.iter() {
                if *last_beat == 0 { continue; }
                let diff = now - last_beat;
                if diff <= self.timeout_secs { continue; }

                self.pipeline.log("watchdog", None, LogTyp::Error,
                    &format!("Scheduler '{}' antwortet seit {}s nicht!", modul_id, diff));

                // KRITISCH (Double-Execution-Fix): zuerst die laufenden Task-Handles
                // abort()en, dann — und erst dann — die BusyMap freigeben. Ohne Abort
                // würde der alte Task weiterlaufen während der neue Scheduler denselben
                // Task aus gestartet/ re-picked. Ergebnis: doppelte Mail-Sends, doppelte
                // Writes, doppelte Tool-Calls. Mit Abort: der alte Task wird beim
                // nächsten await-Point hart beendet, er läuft nicht zu Ende.
                let handles_to_abort: Vec<(String, tokio::task::AbortHandle)> = {
                    let mut h = self.handles.write().await;
                    if let Some(map) = h.remove(modul_id) {
                        map.into_iter().collect()
                    } else {
                        Vec::new()
                    }
                };
                let aborted = handles_to_abort.len();
                for (aufgabe_id, handle) in &handles_to_abort {
                    handle.abort();
                    // Task zurück auf erstellt — ABER nur wenn er wirklich noch Gestartet
                    // ist. Nach SQLite-Migration ist der Status die einzige Source-of-
                    // Truth (atomic transitions); einen "exists-in-erledigt/"-Check auf
                    // dem Filesystem machen wie früher geht nicht mehr (der Ordner wurde
                    // archiviert). Status-Check via laden_by_id reicht — wenn der Task
                    // zwischen unserem Abort und dem Recovery noch auf Success umgesprungen
                    // ist (weil er gerade vor dem abort den Transition-Call gemacht hat),
                    // sehen wir's und machen nichts.
                    match self.pipeline.laden_by_id(aufgabe_id) {
                        Ok(Some(mut a)) if a.status == AufgabeStatus::Gestartet => {
                            // Watchdog-Requeue zählt als Retry-Versuch. Sonst
                            // würde ein Task mit hängendem LLM-Backend endlos
                            // requeued (Watchdog abort → erstellt → claim →
                            // hang → abort → ...), ohne jemals retry_count zu
                            // erreichen. Bei Überschreitung: FAIL statt
                            // Requeue (GLM-Finding Run SQLite-5).
                            a.retry_count += 1;
                            if a.retry_count > a.retry {
                                a.ergebnis = Some(format!(
                                    "FAILED: Watchdog-Abort + retry-Limit erreicht ({}/{})",
                                    a.retry_count, a.retry,
                                ));
                                if let Err(e) = self.pipeline.verschieben(&mut a, AufgabeStatus::Failed) {
                                    self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Error,
                                        &format!("FAIL-Transition failed: {}", e));
                                } else {
                                    self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Failed,
                                        &format!("Task nach {} Watchdog-Aborts dauerhaft fehlgeschlagen", a.retry_count));
                                }
                            } else if let Err(e) = self.pipeline.verschieben(&mut a, AufgabeStatus::Erstellt) {
                                self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Error,
                                    &format!("Rückführung in erstellt failed: {}", e));
                            } else {
                                self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Warning,
                                    &format!("Task nach Scheduler-Timeout aborted + zurück (retry {}/{})",
                                        a.retry_count, a.retry));
                            }
                        }
                        Ok(Some(a)) => {
                            // Schon Success/Failed/Cancelled — Task ist durch, nichts zu tun.
                            self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Info,
                                &format!("Task bereits {:?} — kein Requeue", a.status));
                        }
                        Ok(None) => {
                            // Task existiert nicht mehr (gelöscht, cleanup) — ignore
                        }
                        Err(e) => {
                            self.pipeline.log("watchdog", Some(aufgabe_id), LogTyp::Error,
                                &format!("laden_by_id failed: {}", e));
                        }
                    }
                }

                // Erst nach erfolgreichem Abort die BusyMap freigeben — sonst könnte der
                // Orchestrator-Tick dazwischen den Task erneut spawnen.
                let mut busy = self.busy.write().await;
                if let Some(ids) = busy.remove(modul_id) {
                    if !ids.is_empty() {
                        self.pipeline.log("watchdog", None, LogTyp::Warning,
                            &format!("{} stuck task(s) fuer '{}' freigegeben (aborted={})",
                                ids.len(), modul_id, aborted));
                    }
                }
                // Heartbeat zurücksetzen damit der nächste Tick nicht sofort wieder
                // feuert — der neu gespawnte Scheduler aktualisiert ihn beim Start.
                drop(busy);
                let mut hb = self.heartbeats.write().await;
                hb.remove(modul_id);
            }
        }
    }
}
