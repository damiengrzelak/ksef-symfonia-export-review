"""Świeżość plików hmffk czekających na import do Symfonii.

PROBLEM: eksport przewiduje numer karty nowego kontrahenta (max+1, jak FK)
i wpisuje go w konto rozrachunkowe ("Konto =201-2-<pozycja>"). Jeśli między
eksportem a importem ktoś założy kartę RĘCZNIE w Symfonii, FK nada nowym
kontrahentom z pliku inne numery niż przewidziane — dokumenty trafią na konta
cudzych kontrahentów.

ROZWIĄZANIE (automatyczne, bez klikania): każdy zapisany plik hmffk jest
rejestrowany jako „oczekujący na import” razem z kartami, które zawiera
(klucz, przewidziany numer, konta). `odswiez_pliki` — wołane cyklicznie przez
GUI (timer), na żądanie (przycisk) albo z CLI (Harmonogram zadań) — czyta
kartotekę FK przez SQL (tylko odczyt) i dla każdego oczekującego pliku:

- karta już jest w FK -> właściwy numer to numer z FK;
- karty jeszcze nie ma -> numer przewidywany od nowa: max(FK, reszta
  rejestru)+1, w kolejności plików (od najstarszego) i sekcji Kontrahent{}
  w pliku (tak nadaje FK — patrz app/kontrahenci_nr.py);
- różnica -> linie "Konto =" w pliku są przepisywane (zapis atomowy),
  a rejestr numerów dostaje nowe pozycje (synchronizacja + przenumerowanie).

Plik przestaje być śledzony, gdy WSZYSTKIE jego karty są w FK z numerami
równymi plikowi (zaimportowany albo co najmniej zgodny), gdy zniknął z dysku,
albo po WAZNOSC (porzucony). Karta obecna już w FK z innym numerem niż w pliku
to sytuacja, której nie da się rozstrzygnąć bez odczytu dokumentów (plik mógł
już zostać zaimportowany ze złym kontem) — plik jest poprawiany, a raport
dostaje ostrzeżenie do sprawdzenia dekretu.

Numer karty pojawia się w pliku hmffk WYŁĄCZNIE w liniach "Konto =" (sekcja
Kontrahent{} identyfikuje kartę kluczem `id`, nie numerem — app/hmffk.py),
więc przepisanie tych linii wystarcza.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from app.ams_transform import write_txt_atomic
from app.config import app_data_dir
from app.kontrahenci_nr import RejestrNumerow, _digits, _klucz_seed
from app.symfonia_sql import czytaj_kartoteke

logger = logging.getLogger(__name__)

# plik nieimportowany dłużej niż tyle uznajemy za porzucony — inaczej jego
# karty blokowałyby przewidywanie numerów w kolejnych plikach
WAZNOSC = timedelta(days=30)

STATUS_OCZEKUJE = "oczekuje"
STATUS_ZGODNY = "zgodny"
STATUS_BRAK_PLIKU = "brak_pliku"
STATUS_WYGASLY = "wygasly"

_PREFIKS_KONTA = "Konto ="


@dataclass
class KartaWPliku:
    klucz: str
    nazwa: str
    pozycja: int
    konta: list[str]  # pełne konta rozrachunkowe z tym numerem, np. ["201-2-1001"]


@dataclass
class PlikOczekujacy:
    id: int
    profil_nip: str
    sciezka: Path
    utworzono: datetime
    karty: list[KartaWPliku]


@dataclass
class ZmianaNumeru:
    plik: Path
    nazwa: str
    stary: int
    nowy: int
    # karta była już w FK z innym numerem — plik mógł zostać zaimportowany ze
    # złym kontem, dekret trzeba sprawdzić
    mozliwie_zaimportowany: bool = False


@dataclass
class RaportOdswiezenia:
    sprawdzone: int = 0
    zmiany: list[ZmianaNumeru] = field(default_factory=list)
    zakonczone: list[Path] = field(default_factory=list)
    bledy: list[str] = field(default_factory=list)
    ostrzezenia: list[str] = field(default_factory=list)

    @property
    def odswiezone_pliki(self) -> list[Path]:
        return sorted({z.plik for z in self.zmiany})

    def jako_tekst(self) -> list[str]:
        wynik: list[str] = []
        for plik in self.odswiezone_pliki:
            zmiany = [z for z in self.zmiany if z.plik == plik]
            wynik.append(
                f"Plik {plik.name} odświeżony — kartoteka w Symfonii zmieniła się od "
                "eksportu: " + "; ".join(f"{z.nazwa} {z.stary} -> {z.nowy}" for z in zmiany)
            )
        podejrzane = [z for z in self.zmiany if z.mozliwie_zaimportowany]
        if podejrzane:
            wynik.append(
                "Jeśli któryś z tych plików był JUŻ zaimportowany, sprawdź konto "
                "rozrachunkowe dokumentów: "
                + "; ".join(
                    f"{z.nazwa} (w pliku było {z.stary}, karta w Symfonii ma {z.nowy})"
                    for z in podejrzane
                )
            )
        wynik += self.ostrzezenia
        wynik += [f"BŁĄD: {b}" for b in self.bledy]
        return wynik


class PlikiDoImportu:
    """Rejestr plików hmffk czekających na import — SQLite w katalogu danych
    aplikacji (osobno od rejestru numerów: inny cykl życia wpisów)."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path else app_data_dir() / "pliki_importu.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS pliki(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   profil_nip TEXT,
                   sciezka TEXT,
                   utworzono TEXT,
                   status TEXT DEFAULT 'oczekuje'
               )"""
        )
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS karty(
                   plik_id INTEGER,
                   kolejnosc INTEGER,
                   klucz TEXT,
                   nazwa TEXT,
                   pozycja INTEGER,
                   konta TEXT,
                   PRIMARY KEY (plik_id, klucz)
               )"""
        )
        self._conn.commit()

    def zarejestruj(
        self, profil_nip: str, sciezka: Path, karty: list[KartaWPliku],
        teraz: datetime | None = None,
    ) -> int:
        """Rejestruje zapisany plik. `karty` w kolejności sekcji Kontrahent{}
        w pliku. Ponowna rejestracja tej samej ścieżki zastępuje poprzednią."""
        teraz = teraz or datetime.now()
        sciezka_txt = str(Path(sciezka))
        with self._conn:
            for (stary_id,) in self._conn.execute(
                "SELECT id FROM pliki WHERE profil_nip=? AND sciezka=?", (profil_nip, sciezka_txt)
            ).fetchall():
                self._conn.execute("DELETE FROM karty WHERE plik_id=?", (stary_id,))
                self._conn.execute("DELETE FROM pliki WHERE id=?", (stary_id,))
            cur = self._conn.execute(
                "INSERT INTO pliki(profil_nip, sciezka, utworzono, status) VALUES (?, ?, ?, ?)",
                (profil_nip, sciezka_txt, teraz.isoformat(timespec="seconds"), STATUS_OCZEKUJE),
            )
            plik_id = cur.lastrowid
            self._conn.executemany(
                "INSERT INTO karty(plik_id, kolejnosc, klucz, nazwa, pozycja, konta) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (plik_id, i, k.klucz, k.nazwa, k.pozycja, "\n".join(k.konta))
                    for i, k in enumerate(karty)
                ],
            )
        return plik_id

    def oczekujace(self, profil_nip: str) -> list[PlikOczekujacy]:
        wynik: list[PlikOczekujacy] = []
        for plik_id, sciezka, utworzono in self._conn.execute(
            "SELECT id, sciezka, utworzono FROM pliki WHERE profil_nip=? AND status=? ORDER BY id",
            (profil_nip, STATUS_OCZEKUJE),
        ).fetchall():
            karty = [
                KartaWPliku(klucz, nazwa or "", pozycja, [k for k in (konta or "").split("\n") if k])
                for klucz, nazwa, pozycja, konta in self._conn.execute(
                    "SELECT klucz, nazwa, pozycja, konta FROM karty WHERE plik_id=? "
                    "ORDER BY kolejnosc",
                    (plik_id,),
                ).fetchall()
            ]
            wynik.append(PlikOczekujacy(
                plik_id, profil_nip, Path(sciezka), datetime.fromisoformat(utworzono), karty
            ))
        return wynik

    def ustaw_status(self, plik_id: int, status: str) -> None:
        with self._conn:
            self._conn.execute("UPDATE pliki SET status=? WHERE id=?", (status, plik_id))

    def zapisz_karty(self, plik_id: int, karty: list[KartaWPliku]) -> None:
        with self._conn:
            self._conn.executemany(
                "UPDATE karty SET pozycja=?, konta=? WHERE plik_id=? AND klucz=?",
                [(k.pozycja, "\n".join(k.konta), plik_id, k.klucz) for k in karty],
            )

    def close(self) -> None:
        self._conn.close()


def _konto_z_numerem(konto: str, nowy: int) -> str:
    """"201-2-1001" -> "201-2-<nowy>" (numer karty to ostatni człon konta)."""
    prefiks, _, _ = konto.rpartition("-")
    return f"{prefiks}-{nowy}" if prefiks else str(nowy)


def przepisz_konta(sciezka: Path, zamiany: dict[str, str]) -> int:
    """Podmienia wartości linii "Konto =<konto>" wg słownika (jedno przejście,
    więc zamiana A->B, B->C nie tworzy łańcucha). Plik CP1250 + CRLF jak przy
    eksporcie, zapis atomowy. Zwraca liczbę zmienionych linii."""
    tekst = Path(sciezka).read_bytes().decode("cp1250")
    linie = tekst.split("\r\n")
    zmienione = 0
    for i, linia in enumerate(linie):
        if not linia.startswith(_PREFIKS_KONTA):
            continue
        nowe = zamiany.get(linia[len(_PREFIKS_KONTA):])
        if nowe is not None:
            linie[i] = _PREFIKS_KONTA + nowe
            zmienione += 1
    if zmienione:
        write_txt_atomic(Path(sciezka), "\r\n".join(linie).encode("cp1250"))
    return zmienione


def odswiez_pliki(
    profil_nip: str,
    baza: str,
    rejestr: RejestrNumerow,
    pliki: PlikiDoImportu,
    *,
    czytaj: Callable[[str], list[dict]] = czytaj_kartoteke,
    teraz: datetime | None = None,
) -> RaportOdswiezenia:
    """Doprowadza oczekujące pliki profilu do zgodności z bieżącą kartoteką FK
    (patrz docstring modułu). Bez oczekujących plików nie łączy się z bazą.
    Błędy SQL (SymfoniaSqlError) i bezpiecznika synchronizacji
    (SynchronizacjaOdrzucona) propagują się — nic nie zostaje zmienione."""
    teraz = teraz or datetime.now()
    raport = RaportOdswiezenia()

    oczekujace: list[PlikOczekujacy] = []
    for plik in pliki.oczekujace(profil_nip):
        if teraz - plik.utworzono > WAZNOSC:
            pliki.ustaw_status(plik.id, STATUS_WYGASLY)
            raport.ostrzezenia.append(
                f"Plik {plik.sciezka.name} czeka na import dłużej niż {WAZNOSC.days} dni — "
                "przestaje być automatycznie odświeżany (wyeksportuj go ponownie przed importem)"
            )
        elif not plik.sciezka.exists():
            pliki.ustaw_status(plik.id, STATUS_BRAK_PLIKU)
        else:
            oczekujace.append(plik)
    if not oczekujace:
        return raport
    raport.sprawdzone = len(oczekujace)

    wiersze = czytaj(baza)
    fk_poz: dict[str, int] = {}
    for w in wiersze:
        klucz = _klucz_seed(w["nazwa"], _digits(w["nip"]), w["miejscowosc"])
        fk_poz.setdefault(klucz, w["pozycja"])

    # rejestr numerów = stan FK (karty założone ręcznie stają się znane)
    rejestr.synchronizuj_z_wierszy(profil_nip, wiersze)

    # karty nieznane FK: numery od nowa, w kolejności importu (plik po pliku,
    # sekcja po sekcji); wpisy manual zostają tam, gdzie ustawiła je księgowa
    do_numeracji: list[str] = []
    for plik in oczekujace:
        for karta in plik.karty:
            if karta.klucz in fk_poz or karta.klucz in do_numeracji:
                continue
            wpis = rejestr.wpis(profil_nip, karta.klucz)
            if wpis is not None and wpis.status == "manual":
                continue
            do_numeracji.append(karta.klucz)
    pozostale = [w.pozycja for w in rejestr.lista(profil_nip) if w.klucz not in do_numeracji]
    nastepny = max([0, *fk_poz.values(), *pozostale]) + 1
    przewidziane = {klucz: nastepny + i for i, klucz in enumerate(do_numeracji)}
    rejestr.przenumeruj(profil_nip, przewidziane)

    for plik in oczekujace:
        zamiany: dict[str, str] = {}
        zmiany: list[ZmianaNumeru] = []
        nowe: dict[str, int] = {}  # klucz -> nowy numer
        for karta in plik.karty:
            if karta.klucz in fk_poz:
                docelowy = fk_poz[karta.klucz]
            elif karta.klucz in przewidziane:
                docelowy = przewidziane[karta.klucz]
            else:
                docelowy = rejestr.pozycja(profil_nip, karta.klucz) or karta.pozycja
            if docelowy == karta.pozycja:
                continue
            for konto in karta.konta:
                zamiany[konto] = _konto_z_numerem(konto, docelowy)
            nowe[karta.klucz] = docelowy
            zmiany.append(ZmianaNumeru(
                plik.sciezka, karta.nazwa or karta.klucz, karta.pozycja, docelowy,
                mozliwie_zaimportowany=karta.klucz in fk_poz,
            ))
        if zamiany:
            try:
                przepisz_konta(plik.sciezka, zamiany)
            except OSError as exc:
                # np. plik otwarty właśnie przez import w Symfonii — następna próba
                # przy kolejnym odświeżeniu
                raport.bledy.append(f"Nie udało się odświeżyć pliku {plik.sciezka.name}: {exc}")
                continue
            for karta in plik.karty:
                if karta.klucz in nowe:
                    karta.konta = [_konto_z_numerem(k, nowe[karta.klucz]) for k in karta.konta]
                    karta.pozycja = nowe[karta.klucz]
            pliki.zapisz_karty(plik.id, plik.karty)
            raport.zmiany += zmiany
            logger.info(
                "Odświeżono plik %s: %s", plik.sciezka.name,
                ", ".join(f"{z.nazwa} {z.stary}->{z.nowy}" for z in zmiany),
            )
        if all(fk_poz.get(k.klucz) == k.pozycja for k in plik.karty):
            pliki.ustaw_status(plik.id, STATUS_ZGODNY)
            raport.zakonczone.append(plik.sciezka)
    return raport
