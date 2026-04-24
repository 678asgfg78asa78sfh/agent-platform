# Agent Platform — TODO

## Nächste Session: Mail-System + Aufgaben-Board

### 1. Aufgaben-Board (Frontend)
- [ ] "Neue Aufgabe" Button entfernen
- [ ] Cancel-Button pro Aufgabe (verschiebt nach erledigt mit status=Cancelled)
- [ ] Edit-Button pro Aufgabe (Anweisung bearbeiten, solange status=Erstellt)
- [ ] Aufgaben liegen als JSON-Dateien vor → direkt editierbar
- [ ] Live-Refresh (polling alle 3s)

### 2. IMAP Modul (Python: modules/imap/)
- [ ] module.py mit Standard-Interface
- [ ] Tools: imap.fetch, imap.search, imap.folders
- [ ] Settings: host, port, email, password, ordner, tls
- [ ] Filter-System in Settings:
  - [ ] DKIM valid?
  - [ ] DMARC valid?
  - [ ] ARC valid?
  - [ ] Absender-Whitelist
  - [ ] Keyword im Betreff
  - [ ] Secret Phrase im Footer
  - [ ] IP-Whitelist
- [ ] Auto-Download Option (Cycle ruft periodisch ab, KEIN LLM nötig)
- [ ] Mails in SQLite ablegen (nicht als einzelne Dateien)

### 3. SMTP Modul (Python: modules/smtp/)
- [ ] module.py mit Standard-Interface
- [ ] Tools: smtp.send
- [ ] Settings: host, port, email, password, tls, from_name
- [ ] Template-System für Standard-Mails

### 4. POP3 Modul (Python: modules/pop3/)
- [ ] module.py wie IMAP aber mit POP3 Protokoll
- [ ] Gleiche Filter wie IMAP
- [ ] Gleicher SQLite-Mailstore

### 5. Mail-Speicher (SQLite)
- [ ] agent-data/mail.db
- [ ] Tabellen: mails (id, from, to, subject, body, date, folder, read, flags)
- [ ] Token-effizient: KI sucht per SQL statt alle Mails zu lesen
- [ ] Tools: mail.search(query), mail.read(id), mail.list(folder, limit)
- [ ] Wird von IMAP/POP3 befüllt, von Chat-KI durchsucht

### 6. System-Prompt Template
- [ ] Default-Templates pro Modultyp in modules/templates/
- [ ] Reset-Button im Frontend → setzt auf Default zurück
- [ ] Template-Variablen: {bot_name}, {tools_list}, {module_count}

### 7. Altes Rust Mail-Modul entfernen
- [ ] src/modules/mail.rs raus (ersetzt durch Python IMAP/SMTP)
- [ ] tools.rs: imap.search, imap.read, smtp.send Rust-Handler entfernen

## Backlog

### Architektur
- [ ] Cron-Scheduling implementieren (cycle.rs, aktuell TODO)
- [ ] Webhook-Modul implementieren (eingehende HTTP-Hooks)
- [ ] API Auth (Bearer Token auf Admin-Port)
- [ ] RAG Stopword-Filter (aktuell matchen "der", "ist", "und" auf alles)
- [ ] Vector-RAG mit Ollama Embeddings (codrag Modul)

### Module-Ideen
- [ ] RSS Feed Reader
- [ ] Screenshot/OCR
- [ ] Git-Integration (status, diff, commit)
- [ ] Kalender (CalDAV)
- [ ] Monitoring (Service-Healthchecks als Cron)
