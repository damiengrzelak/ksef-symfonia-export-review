# HANDOFF — stan prac i kolejne etapy (2026-11-07)

> Dla nowej sesji (np. Claude na komputerze z Windowsem). Uzupełnia `PLAN.md`
> (pierwotna budowa) — tu jest to, co ustalono później. Repo jest PUBLICZNE:
> żadnych tokenów, kluczy, haseł, numerów licencji ani danych klientów.

## 1. Kontekst

- Symfonia FK linia 50 (wielofirmowa), 4 firmy, baza lokalnie na komputerze
  w firmie (SQL `.\SAGEPL`). Program pobiera faktury z KSeF i robi pliki
  importu (hmffk / AMS), które księgowa importuje ręcznie w Symfonii.
- Część firm wystawia faktury w inFakcie (3 spółki).
- Bez licencji WebAPI (WAF) import do Symfonii zostaje jednym ręcznym krokiem —
  bezpośredni zapis do SQL Symfonii jest wykluczony.
- GAMMATEST: jedna z 4 baz firm. NIE zakładać, że to baza testowa — do
  potwierdzenia z firmą. Testy na żywo tylko na osobnej firmie testowej (kopii).

## 2. Żelazne zasady

- SQL Symfonii: wyłącznie SELECT na `sys.databases` i `FK.fk_kontrahenci`
  (patrz docstring `app/symfonia_sql.py`). Nigdy zapisów.
- Klucze/tokeny tylko w `config.enc` (Fernet + scrypt, hasło główne) albo
  w menedżerze haseł. Nigdy w repo, czacie, mailu.
- Program w firmie sam aktualizuje się z gałęzi `main` (`uruchom.pyw`).
  Większe zmiany robić na osobnej gałęzi, na `main` dopiero po teście.
- Prawdziwe dane (pliki importu, kopie bazy) nie opuszczają firmy.

## 3. Co jest zrobione (commit 61e35f1)

- `app/swiezosc.py` — rejestr plików hmffk czekających na import (SQLite
  `pliki_importu.db`, ważność 30 dni). Co 60 s (GUI) lub z Harmonogramu
  (`--odswiez-pliki BAZA`, kody 0/1/2) czyta kartotekę FK i przenumerowuje
  karty w plikach, gdy ktoś ręcznie dodał kontrahenta (problem „1000 vs 1001").
  FK nadaje nowym kartom max(pozycja)+1 w kolejności sekcji Kontrahent{}.
- `RejestrNumerow.synchronizuj_z_wierszy()` i `przenumeruj()`.
- GUI: przycisk „Odśwież pliki do importu”, timer; okienko tylko gdy plik
  mógł już zostać zaimportowany (inaczej tylko log).
- `Profile.infakt_api_key` — klucz inFaktu per firma, zaszyfrowany, maskowane
  pole w oknie profilu (z opcją usunięcia). Starsza wersja programu nie
  otworzy configu z tym polem.
- Testy: 694 przechodzi; 8 nie przechodziło już wcześniej (3 porównania
  z plikami wzorcowymi — CRLF, 5 testów XSD).

NIE rozwiązane: tysiące kliknięć przy nowych kontrahentach podczas importu.

## 4. Etap A — bez WebAPI (do zrobienia)

1. **A2 — szablon importu bez okienek.** `hmffk.AMS` ma `#POWIELONE_NAZWY "0"`,
   `#DANEKH "1"`; `SKsefInvoiceImport.AMS` ma `"1"` / `"2"`. Hipoteza
   (niepotwierdzona): `#DANEKH` steruje okienkami przy nowych kontrahentach.
   Potrzebny zrzut okienka i test na firmie testowej.
2. **A3 — okno weryfikacji w programie**: tylko podejrzani kontrahenci
   (duplikaty NIP, podobne nazwy), reszta bez pytania.
3. **A4 — nocny eksport** z Harmonogramu zadań („uruchom niezależnie od
   zalogowania”), logi.
4. **A5 — raport poranny**: co pobrano, co czeka na import, co do weryfikacji.
5. **A6 — cykliczna wysyłka zaległych faktur z inFaktu do KSeF.** Wymaga
   ustaleń: karencja, oznaczanie „nie wysyłaj”, zgoda księgowej; start
   w trybie próbnym. Program tylko zleca — wysyła inFakt.
6. **A7 — warstwa zapisu** pod przyszłe WebAPI.
7. Wdrożenie: konfiguracja „Bazy Symfonii (SQL)” w profilach, README,
   szkolenie księgowej. Klucze inFaktu wpisać po ustabilizowaniu wersji.

## 5. Etap B — z WebAPI (po zakupie licencji WAF wielofirmowej)

- Usługa WebAPI `127.0.0.1:8080`, nagłówki `Authorization: Application <GUID>`
  i `Session <token>`.
- `IFKContractorsController` (Get, GetByNIP, AddNew, GetPagedDocument),
  `IFKDocumentsBusinessIssueController` (NewSaleInvoice, NewPurchaseInvoice,
  korekty; `BusinessDocumentIssue`: AccountingScheme, ContractorPosition,
  ForManualVerification).
- Cel: kontrahenci i dokumenty bezpośrednio do Symfonii, ręcznie tylko wyjątki.

## 6. API — fakty

**inFakt v3** — `https://api.infakt.pl/api/v3`, nagłówek `X-inFakt-ApiKey`.
- `GET /invoices.json` (limit 100; `q[invoice_date_gteq]`; `q[status_eq]`
  dotyczy maili, nie KSeF), węzeł `ksef_data`.
- `POST /ksef2/documents/send.json` `{"uuids":[...]}`;
  `GET /ksef2/documents/{uuid}/status.json` → pending/sent/success/error.
- Limity na IP: GET 300/60 s, pozostałe 150/60 s; 429/503 → ponowić.
- Webhooki wymagają publicznego URL — nie używamy.
- Dokumentacja renderowana JS; działa kolekcja Postmana
  `docs.infakt.pl/api/collections/4951438/2s8ZDa31yd`.

**KSeF 2.0** — limity per NIP i IP, okno przesuwne, 429 z `Retry-After`:
- wysyłka w sesji interaktywnej 10/s, 30/min, 180/h;
- query metadata 8/16/20 (20/h istotne dla eksportu);
- max 10 000 faktur w sesji; faktura 1 MB (3 MB z załącznikiem).
- Przy 5000+ faktur/mies.: wysyłać ciągle małymi porcjami z pilnowaniem
  limitów, nie jednym nocnym zrzutem.
- Faktura wystawiona w Aplikacji Podatnika = już wysłana do KSeF.

## 7. Do ustalenia z firmą

- Czy GAMMATEST to firma produkcyjna; firma testowa + kopia zapasowa.
- Zrzut okienka przy nowym kontrahencie, przykładowy import księgowej.
- Edycja Windows, konto admina, logowanie do SQL, antywirus.
- Zasady wysyłki z inFaktu (karencja, „nie wysyłaj”).
- Godzina nocnego eksportu, folder, odbiorca raportu, osoba kontaktowa.
- Partner Symfonii: próbna licencja WAF, oferta wielofirmowa.
- Biała lista nie działa w 2 z 4 firm („an error occurred while updating the
  entries, see the inner exception”) — sprawdzić Podgląd zdarzeń.

## 8. Środowisko deweloperskie

- Windows: `python -m venv .venv`, `.venv\Scripts\activate`,
  `pip install -r requirements.txt`, `python -m pytest`.
- macOS/Linux: testy GUI z `QT_QPA_PLATFORM=offscreen`. Pod Wine Qt się nie
  ładuje (brak icuuc.dll) — testy bez Qt działają.
- Test na żywo: na komputerze firmy, w osobnym folderze (nie tym księgowej),
  na firmie testowej.
