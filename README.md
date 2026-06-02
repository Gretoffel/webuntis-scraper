# WebUntis Scraper

Playwright-basierter Scraper für WebUntis. Lädt Stundenplan, Prüfungen /
Klausuren, Hausaufgaben, Absenzen und Nachrichten und speichert sie als
strukturiertes JSON.

## Warum Playwright *und* direkt HTTP?

WebUntis hat zwei relevante APIs:

- **JSON-RPC** unter `/WebUntis/jsonrpc.do` (klassisch, gut dokumentiert,
  z.B. `getOwnTimetableForWeek`, `getExamsForRange`, `getHomeWorkForRange`).
- **REST v1** unter `/WebUntis/api/rest/view/v1/...` (das neuere
  UI2020-Backend mit `/timetable/entries`, `/app/data`).

Beide erfordern einen gültigen Session-Cookie. Statt die komplexe
**React-SPA** von UI2020 mit Form-Selectors anzufassen (race-conditions
mit der JS-Hydration, instabile Selektoren), machen wir den Login
direkt gegen den JSON-RPC-`authenticate`-Endpoint. Das ist schnell,
zuverlässig und unabhängig vom gerenderten DOM.

Playwright wird nur kurz benutzt um die `school`-Cookies zu bootstrappen
(JSESSIONID etc.), die der Server bei einem GET auf die Login-URL setzt.
Diese Cookies + Username/Passwort gehen dann in den
`authenticate`-RPC → Session-ID. Alle weiteren Calls laufen über `httpx`.

Falls deine Schule SSO/2FA/Captcha vorschaltet, fällt der Scraper
automatisch auf den Form-Login zurück (Playwright klickt sich durch).
`--form-login` erzwingt diesen Pfad dauerhaft.

Bonus: `playwright-stealth` patcht typische Bot-Detection-Vektoren
(`navigator.webdriver`, `navigator.plugins`, `navigator.languages`, …).

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium
```

## Konfiguration

1. `config.example.json` nach `config.json` kopieren und anpassen:

   ```json
   {
     "server": "nese",          // Subdomain vor .webuntis.com
     "school": "htbla_kaindorf",// Wert hinter ?school=
     "username": "max.muster"
   }
   ```

   `server` + `school` findest du, indem du auf
   [webuntis.com](https://webuntis.com) deine Schule suchst - die
   Redirect-URL enthält beides, z.B.
   `https://nese.webuntis.com/WebUntis/?school=htbla_kaindorf`.

2. `.env.example` nach `.env` kopieren und das Passwort eintragen:

   ```ini
   UNTIS_PASSWORD=deinPasswort
   ```

## Nutzung

```powershell
# Standard-Lauf (JSON-RPC-Login, headless, Session wiederverwendet)
python -m src

# Erzwinge Form-Login (z.B. bei 2FA / SSO)
python -m src --form-login --no-headless --clear-session

# Anderes Zeitfenster
python -m src --days-back 7 --days-forward 30

# Rohdaten der API zusätzlich behalten
python -m src --keep-raw -v
```

Output landet in `out/untis_<timestamp>.json` sowie `out/latest.json`.
In `sessions/storage_state.json` werden Cookies gespeichert, damit
Folge-Läufe kein erneutes Login brauchen.

### Login-Fehler?

Falls du eine Fehlermeldung wie `Authenticate failed: Invalid username
or password (code=-1)` bekommst, obwohl die Credentials stimmen, prüfe:

1. **Server + Slug korrekt?** Auf `webuntis.com` deine Schule suchen -
   die Redirect-URL lautet `https://<server>.webuntis.com/WebUntis/?school=<slug>`.
2. **Sonderzeichen im Passwort?** `.env` unterstützt `=` und Quotes,
   aber führende Whitespaces werden getrimmt. Test mit `python -c "import
   os; print(repr(os.environ['UNTIS_PASSWORD']))"`.
3. **CAPTCHA / SSO / 2FA?** → `python -m src --form-login --no-headless`
4. **Verbose-Output:** `python -m src -v` zeigt den HTTP-Verkehr.

## Output-Schema

```jsonc
{
  "meta": {
    "school": "...", "server": "...", "user": "...",
    "generated_at": "2026-06-02", "window": {"start": "...", "end": "..."}
  },
  "timetable": {
    "source": "rest_v1" | "jsonrpc",
    "start": "2026-06-02", "end": "2026-06-16",
    "days": [
      {"date": "2026-06-02", "entries": [
        {
          "start": "2026-06-02T08:00", "end": "2026-06-02T08:45",
          "status": "REGULAR", "is_cancelled": false, "is_exam": false,
          "lesson_text": "", "subjects": [{"short":"M","long":"Math"}],
          "teachers": [...], "rooms": [...]
        }
      ]}
    ],
    "lessons": [...]   // bei jsonrpc-Fallback
  },
  "exams": {
    "source": "jsonrpc" | "timetable_fallback",
    "exams": [ { "id": 123, "date": "2026-06-10", "name": "Klausur", ... } ]
  },
  "homework":  { "items": [...] },
  "absences":  { "items": [...] },
  "messages":  { "items": [...] }
}
```

## Hinweise

- **2FA / Captcha**: Falls deine Schule OTP verlangt, einmalig mit
  `--no-headless --clear-session` laufen lassen, Code eintippen, dann
  ab sofort headless.
- **Prüfungen**: Der Endpoint `getExams` ist nur für Admins/Lehrer
  verfügbar. Für Schüler leiten wir Klausuren aus dem Stundenplan ab
  (`actType` enthält "Klausur") - siehe `timetable_fallback` in der
  Output-Source.
- **Rate-Limit**: Wir senden höchstens eine Anfrage alle 400 ms.
  Verzögern mit `--days-forward` reizen ist kein Problem.
- **Speicherort**: `sessions/` und `out/` sind in `.gitignore`.

## Projektstruktur

```
src/
  __init__.py
  main.py           # CLI
  config.py         # config.json + .env laden
  browser.py        # Playwright + stealth
  untis_client.py   # Login + JSON-RPC + REST v1
  normalize.py      # Rohdaten -> saubere Dicts
  scraper.py        # Orchestrierung
  exporter.py       # JSON-Ausgabe
```
