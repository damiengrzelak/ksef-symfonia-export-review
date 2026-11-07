"""Świeżość plików hmffk czekających na import (app/swiezosc.py).

Scenariusz z produkcji: eksport przewidział numer karty nowego kontrahenta
(1001), a zanim księgowa zaimportowała plik, ktoś założył kartę ręcznie
w Symfonii (zajęła 1001). Plik musi sam dostać nowe numery, bez klikania.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import app.swiezosc as swiezosc_mod
from app.config import Profile
from app.exporter import Kind, run_export
from app.kontrahenci_nr import RejestrNumerow
from app.ledger import Ledger
from app.swiezosc import (
    STATUS_ZGODNY,
    WAZNOSC,
    KartaWPliku,
    PlikiDoImportu,
    odswiez_pliki,
    przepisz_konta,
)

from tests.test_exporter import StubClient, _meta

PROFIL = "5252445719"
BAZA = "GAMMATEST"
TERAZ = datetime(2026, 10, 30, 9, 0)

ISTNIEJACY = [  # karty, które są już w FK
    {"pozycja": 1, "kod": "A", "nazwa": "Alfa", "nip": "1111111111", "miejscowosc": ""},
    {"pozycja": 2, "kod": "B", "nazwa": "Beta", "nip": "2222222222", "miejscowosc": ""},
]


def _wiersz(pozycja: int, nazwa: str, nip: str) -> dict:
    return {"pozycja": pozycja, "kod": nazwa[:5], "nazwa": nazwa, "nip": nip, "miejscowosc": ""}


class FakeKartoteka:
    def __init__(self, wiersze: list[dict]):
        self.wiersze = list(wiersze)
        self.wywolania = 0

    def __call__(self, baza: str) -> list[dict]:
        assert baza == BAZA
        self.wywolania += 1
        return list(self.wiersze)


def _plik(tmp_path: Path, nazwa: str, konta: list[str]) -> Path:
    """Minimalny plik hmffk: linie Konto = jak w app/hmffk.py (CP1250 + CRLF)."""
    linie = ["INFO{", "}", ""]
    for konto in konta:
        linie += ["zapis{", "strona =WN", "kwota =123.00", f"Konto ={konto}", "}"]
    sciezka = tmp_path / nazwa
    sciezka.write_bytes(("\r\n".join(linie) + "\r\n").encode("cp1250"))
    return sciezka


def _konta_w_pliku(sciezka: Path) -> list[str]:
    tekst = sciezka.read_bytes().decode("cp1250")
    assert "\n" not in tekst.replace("\r\n", "")  # CRLF zachowane
    return [ln[len("Konto ="):] for ln in tekst.split("\r\n") if ln.startswith("Konto =")]


@pytest.fixture
def rejestr(tmp_path):
    rej = RejestrNumerow(tmp_path / "nr.db")
    rej.synchronizuj_z_wierszy(PROFIL, ISTNIEJACY)
    yield rej
    rej.close()


@pytest.fixture
def pliki(tmp_path):
    p = PlikiDoImportu(tmp_path / "pliki.db")
    yield p
    p.close()


def _eksport_z_nowymi(tmp_path, rejestr, pliki, nazwa_pliku="hmffk_1.TXT"):
    """Stan po eksporcie: Alfa (istniejąca, 1) + dwóch nowych (3, 4)."""
    rejestr.przydziel(PROFIL, "3333333333", nazwa="Gamma", nip="3333333333")
    rejestr.przydziel(PROFIL, "4444444444", nazwa="Delta", nip="4444444444")
    sciezka = _plik(tmp_path, nazwa_pliku, ["201-2-1", "201-2-3", "202-2-3", "201-2-4"])
    pliki.zarejestruj(PROFIL, sciezka, [
        KartaWPliku("1111111111", "Alfa", 1, ["201-2-1"]),
        KartaWPliku("3333333333", "Gamma", 3, ["201-2-3", "202-2-3"]),
        KartaWPliku("4444444444", "Delta", 4, ["201-2-4"]),
    ], teraz=TERAZ)
    return sciezka


def test_bez_oczekujacych_plikow_nie_laczy_sie_z_baza(rejestr, pliki):
    kartoteka = FakeKartoteka(ISTNIEJACY)
    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)
    assert kartoteka.wywolania == 0
    assert raport.sprawdzone == 0 and raport.jako_tekst() == []


def test_kartoteka_bez_zmian_plik_bez_zmian(tmp_path, rejestr, pliki):
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki)
    przed = sciezka.read_bytes()

    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=FakeKartoteka(ISTNIEJACY),
                           teraz=TERAZ)

    assert raport.sprawdzone == 1
    assert raport.zmiany == [] and raport.bledy == []
    assert sciezka.read_bytes() == przed
    assert len(pliki.oczekujace(PROFIL)) == 1  # nadal czeka na import


def test_reczna_karta_przed_importem_przenumerowuje_plik_i_rejestr(tmp_path, rejestr, pliki):
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki)
    # ktoś założył w Symfonii kartę „Ręczny” — dostała numer 3, przewidziany dla Gammy
    kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(3, "Ręczny", "9999999999")])

    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)

    # FK nada nowym kartom z pliku 4 i 5 (max+1, w kolejności sekcji)
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-4", "202-2-4", "201-2-5"]
    assert [(z.nazwa, z.stary, z.nowy) for z in raport.zmiany] == [
        ("Gamma", 3, 4), ("Delta", 4, 5)
    ]
    assert not any(z.mozliwie_zaimportowany for z in raport.zmiany)
    assert rejestr.pozycja(PROFIL, "9999999999") == 3
    assert rejestr.pozycja(PROFIL, "3333333333") == 4
    assert rejestr.pozycja(PROFIL, "4444444444") == 5
    assert rejestr.wpis(PROFIL, "3333333333").status == "auto"
    assert any("odświeżony" in t and "Gamma 3 -> 4" in t for t in raport.jako_tekst())

    # drugie odświeżenie: nic więcej do zmiany (idempotencja)
    raport2 = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)
    assert raport2.zmiany == []
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-4", "202-2-4", "201-2-5"]


def test_po_imporcie_plik_przestaje_byc_sledzony(tmp_path, rejestr, pliki):
    _eksport_z_nowymi(tmp_path, rejestr, pliki)
    po_imporcie = FakeKartoteka(ISTNIEJACY + [
        _wiersz(3, "Gamma", "3333333333"), _wiersz(4, "Delta", "4444444444"),
    ])

    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=po_imporcie, teraz=TERAZ)

    assert raport.zmiany == []
    assert len(raport.zakonczone) == 1
    assert pliki.oczekujace(PROFIL) == []
    # kolejne odświeżenia nie odpytują już bazy
    odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=po_imporcie, teraz=TERAZ)
    assert po_imporcie.wywolania == 1


def test_karta_w_fk_z_innym_numerem_poprawia_plik_i_ostrzega(tmp_path, rejestr, pliki):
    """Gamma jest już w FK, ale pod 7 — plik mógł zostać zaimportowany ze złym
    kontem; plik dostaje 7, raport każe sprawdzić dekret."""
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki)
    kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(7, "Gamma", "3333333333")])

    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)

    gamma = next(z for z in raport.zmiany if z.nazwa == "Gamma")
    assert (gamma.stary, gamma.nowy, gamma.mozliwie_zaimportowany) == (3, 7, True)
    # Delta wciąż nieznana FK -> następny po max(FK)=7
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-7", "202-2-7", "201-2-8"]
    assert any("JUŻ zaimportowany" in t and "Gamma" in t for t in raport.jako_tekst())


def test_dwa_pliki_numeracja_ciagla_w_kolejnosci_eksportu(tmp_path, rejestr, pliki):
    pierwszy = _eksport_z_nowymi(tmp_path, rejestr, pliki, "hmffk_1.TXT")
    rejestr.przydziel(PROFIL, "5555555555", nazwa="Epsilon", nip="5555555555")  # 5
    drugi = _plik(tmp_path, "hmffk_2.TXT", ["202-2-5"])
    pliki.zarejestruj(PROFIL, drugi, [KartaWPliku("5555555555", "Epsilon", 5, ["202-2-5"])],
                      teraz=TERAZ)
    kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(3, "Ręczny", "9999999999")])

    odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)

    assert _konta_w_pliku(pierwszy) == ["201-2-1", "201-2-4", "202-2-4", "201-2-5"]
    assert _konta_w_pliku(drugi) == ["202-2-6"]


def test_wpis_manual_nie_jest_przenumerowany(tmp_path, rejestr, pliki):
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki)
    rejestr.ustaw(PROFIL, "4444444444", 50, nazwa="Delta", nip="4444444444", status="manual")
    pliki.zarejestruj(PROFIL, sciezka, [
        KartaWPliku("1111111111", "Alfa", 1, ["201-2-1"]),
        KartaWPliku("3333333333", "Gamma", 3, ["201-2-3", "202-2-3"]),
        KartaWPliku("4444444444", "Delta", 50, ["201-2-50"]),
    ], teraz=TERAZ)
    _plik(tmp_path, sciezka.name, ["201-2-1", "201-2-3", "202-2-3", "201-2-50"])
    kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(3, "Ręczny", "9999999999")])

    odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)

    assert rejestr.pozycja(PROFIL, "4444444444") == 50
    # Gamma: następny po max(FK=3, rejestr bez kart do numeracji: manual 50) -> 51
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-51", "202-2-51", "201-2-50"]


def test_plik_zablokowany_ponawia_przy_nastepnym_odswiezeniu(tmp_path, rejestr, pliki, monkeypatch):
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki)
    kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(3, "Ręczny", "9999999999")])

    def zablokowany(*_a, **_k):
        raise PermissionError("plik otwarty w innym programie")

    monkeypatch.setattr(swiezosc_mod, "przepisz_konta", zablokowany)
    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)
    assert raport.zmiany == [] and len(raport.bledy) == 1
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-3", "202-2-3", "201-2-4"]

    monkeypatch.undo()
    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)
    assert raport.bledy == []
    assert _konta_w_pliku(sciezka) == ["201-2-1", "201-2-4", "202-2-4", "201-2-5"]


def test_plik_usuniety_albo_przeterminowany_przestaje_byc_sledzony(tmp_path, rejestr, pliki):
    sciezka = _eksport_z_nowymi(tmp_path, rejestr, pliki, "stary.TXT")
    kartoteka = FakeKartoteka(ISTNIEJACY)

    raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka,
                           teraz=TERAZ + WAZNOSC + timedelta(days=1))
    assert pliki.oczekujace(PROFIL) == []
    assert any("dłużej niż" in t for t in raport.jako_tekst())

    pliki.zarejestruj(PROFIL, sciezka, [KartaWPliku("1111111111", "Alfa", 1, ["201-2-1"])],
                      teraz=TERAZ)
    sciezka.unlink()
    odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=TERAZ)
    assert pliki.oczekujace(PROFIL) == []
    assert kartoteka.wywolania == 0


def test_przepisz_konta_zamiana_bez_lancucha(tmp_path):
    sciezka = _plik(tmp_path, "f.TXT", ["201-2-3", "201-2-4", "201-2-30"])
    assert przepisz_konta(sciezka, {"201-2-3": "201-2-4", "201-2-4": "201-2-5"}) == 2
    # 3->4 i 4->5 jednocześnie (bez 3->4->5), "201-2-30" nietknięte (całe konto, nie prefiks)
    assert _konta_w_pliku(sciezka) == ["201-2-4", "201-2-5", "201-2-30"]


def test_przenumeruj_zamiana_miejscami(rejestr):
    rejestr.przydziel(PROFIL, "X")  # 3
    rejestr.przydziel(PROFIL, "Y")  # 4
    rejestr.przenumeruj(PROFIL, {"X": 4, "Y": 3, "Z": 9})
    assert (rejestr.pozycja(PROFIL, "X"), rejestr.pozycja(PROFIL, "Y")) == (4, 3)
    assert rejestr.wpis(PROFIL, "Z").status == "auto" and rejestr.pozycja(PROFIL, "Z") == 9


def test_eksport_rejestruje_plik_a_odswiezenie_go_poprawia(tmp_path):
    """Całość: run_export (hmffk) z profilem z bazą Symfonii rejestruje plik;
    ręczna karta w FK przed importem -> konto nowego dostawcy w pliku poprawione."""
    xml = (Path(__file__).parent / "fixtures" / "fa3_basic_23.xml").read_bytes()
    client = StubClient([_meta("NR-0001")], {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    pliki = PlikiDoImportu(tmp_path / "pliki.db")
    rejestr.synchronizuj_z_wierszy(PROFIL, ISTNIEJACY)
    profile = Profile("Firma A", PROFIL, "tok", "firma-a", db_symfonia=BAZA)
    try:
        report = run_export(
            profile, client, ledger, date(2026, 1, 1), date(2026, 1, 31),
            {Kind.COST}, tmp_path / "out", fmt="hmffk", rejestr_nr=rejestr,
            pliki_importu=pliki,
        )
        assert [nr for _, _, nr, _ in report.nowi_kontrahenci] == [3]
        sciezka = next((tmp_path / "out" / "firma-a").glob("hmffk_*.TXT"))
        assert "Konto =202-2-3" in sciezka.read_bytes().decode("cp1250")
        assert [p.sciezka for p in pliki.oczekujace(PROFIL)] == [sciezka]

        kartoteka = FakeKartoteka(ISTNIEJACY + [_wiersz(3, "Ręczny", "9999999999")])
        raport = odswiez_pliki(PROFIL, BAZA, rejestr, pliki, czytaj=kartoteka, teraz=datetime.now())

        tekst = sciezka.read_bytes().decode("cp1250")
        assert "Konto =202-2-4" in tekst and "Konto =202-2-3" not in tekst
        assert [(z.stary, z.nowy) for z in raport.zmiany] == [(3, 4)]
    finally:
        ledger.close()
        rejestr.close()
        pliki.close()


def test_eksport_bez_bazy_symfonii_nie_rejestruje_pliku(tmp_path):
    xml = (Path(__file__).parent / "fixtures" / "fa3_basic_23.xml").read_bytes()
    client = StubClient([_meta("NR-0001")], {"NR-0001": xml})
    ledger = Ledger(tmp_path / "ledger.db")
    rejestr = RejestrNumerow(tmp_path / "nr.db")
    pliki = PlikiDoImportu(tmp_path / "pliki.db")
    try:
        run_export(
            Profile("Firma A", PROFIL, "tok", "firma-a"), client, ledger,
            date(2026, 1, 1), date(2026, 1, 31), {Kind.COST}, tmp_path / "out",
            fmt="hmffk", rejestr_nr=rejestr, pliki_importu=pliki,
        )
        assert pliki.oczekujace(PROFIL) == []
    finally:
        ledger.close()
        rejestr.close()
        pliki.close()
