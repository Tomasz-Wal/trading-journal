# Tomasz Trading Journal — server version

Ta wersja NIE zapisuje danych w przeglądarce.

Architektura:
- Render: FastAPI + strona WWW
- Supabase PostgreSQL: trejdy/opisy/statystyki
- Supabase Storage: screenshoty
- Ten sam feed działa na komputerze i telefonie

## 1. Supabase

Utwórz projekt w Supabase.

W SQL Editor uruchom cały plik `schema.sql`.

Potem:
Storage -> New bucket
Nazwa:
`trade-screenshots`

Bucket może być PRIVATE. Backend pobiera screenshoty przez service role key.

## 2. Dane z Supabase

Project Settings -> API:

Skopiuj:
- Project URL -> `SUPABASE_URL`
- service_role key -> `SUPABASE_SERVICE_ROLE_KEY`

UWAGA: service_role key nie może być umieszczony w kodzie strony ani udostępniany publicznie.
Trzymamy go wyłącznie jako Environment Variable na Render.

## 3. Render

Najłatwiej utworzyć osobny Web Service z tego repozytorium.

Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn main:app --host 0.0.0.0 --port $PORT`

Environment Variables:
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `JOURNAL_KEY` — dowolny długi, URL-safe sekret, np. 30+ znaków
- `STORAGE_BUCKET=trade-screenshots`

## 4. Adres strony

Po deployu:

`https://TWOJ-SERWIS.onrender.com/journal/TWOJ_JOURNAL_KEY`

Ten sam adres działa na komputerze i telefonie.
Trejdy zapisują się w Supabase, więc zmiana urządzenia nie zmienia feedu.

## 5. Co już działa

- feed najnowszych trejdów
- screenshot
- instrument
- LONG / SHORT
- data i czas
- setup
- entry / exit
- qty
- PnL
- rating 1–5
- tagi
- opis
- wniosek
- edycja
- usuwanie
- filtry
- wyszukiwarka
- Total PnL
- Win Rate
- Avg Trade
- dostęp z wielu urządzeń

## 6. Integracja z istniejącym MNQ Copilotem

Kod można też wkleić do obecnego `main.py`, ale bezpieczniej najpierw uruchomić Journal jako osobny Render Web Service.
Po potwierdzeniu działania można połączyć oba projekty pod jednym backendem.
