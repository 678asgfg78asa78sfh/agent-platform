# Naechste Session — Offene Punkte

## KRITISCH: Cycle-Architektur

### Problem
Der Cycle ist single-threaded. Eine langsame Aufgabe (Gemma4 generiert Code, 3+ Min)
blockiert den gesamten Cycle — kein Heartbeat, keine neuen Aufgaben, nichts.

### Loesung: Ein Thread/Loop pro KI-Instanz
- Jede Modul-Instanz (chat.roland, shell.ops, etc.) bekommt eigenen tokio::spawn Task
- Jede Instanz hat eine eigene Aufgaben-Queue
- Wenn eine Instanz busy ist → Status "BUSY" im Dashboard sichtbar
- Neue Anfragen an busy Instanzen:
  - Wenn Backup-LLM konfiguriert → Backup uebernimmt
  - Wenn kein Backup → Aufgabe wartet in Queue (nicht blockiert andere)
- Der Heartbeat-Check laeuft unabhaengig weiter

### Umsetzung in cycle.rs
```
ALT:  Ein loop → tick() → sequentiell alle Aufgaben
NEU:  Pro Instanz ein loop → eigene Queue → parallel
      Main-Cycle verteilt nur noch Aufgaben an Instanz-Queues
```

## Status-Sichtbarkeit
- Dashboard zeigt pro Instanz: IDLE / BUSY (Aufgabe X) / ERROR
- Chat zeigt wenn Roland busy ist: "Roland arbeitet gerade..."
- Aufgaben-Board zeigt welche Instanz gerade was macht

## Ollama Single-Slot Problem
- Ollama kann nur 1 Request gleichzeitig verarbeiten
- Wenn Roland Code generiert und jemand fragt "wie spaet ist es" → Fehler
- Loesung: Queue pro LLM-Backend, nicht pro Instanz
- Oder: Ollama mit OLLAMA_NUM_PARALLEL=2 starten (braucht mehr RAM)

## Sonstige offene Punkte
- Cron-Scheduling (cycle.rs TODO)
- RAG Stopword-Filter
- API Auth (Bearer Token)
