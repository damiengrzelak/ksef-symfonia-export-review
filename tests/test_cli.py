"""Testy CLI ograniczone do --sync-sql: ścieżka błędu musi przerywać eksport
(kod wyjścia 2) PRZED jakimkolwiek kontaktem z API KSeF — ochrona numeracji
kontrahentów. Pełny przebieg eksportu (KsefClient/run_export) nie jest tu
testowany — poza zakresem tego pliku."""

from app.cli import main
from app.kontrahenci_nr import RejestrNumerow
from app.symfonia_sql import SymfoniaSqlError


def test_sync_sql_blad_przerywa_eksport_kodem_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KSEF_TOKEN", "test-token")

    def fake_sync(self, profil_nip, baza):
        raise SymfoniaSqlError("brak sqlcmd w PATH")

    monkeypatch.setattr(RejestrNumerow, "synchronizuj_z_sql", fake_sync)

    kod = main([
        "--nip", "5252445719", "--cel", str(tmp_path), "--sync-sql", "TESTBAZA",
    ])

    assert kod == 2
    captured = capsys.readouterr()
    assert "TESTBAZA" in captured.err
    assert "brak sqlcmd w PATH" in captured.err


def test_sync_sql_zamyka_rejestr_nawet_przy_bledzie(tmp_path, monkeypatch):
    monkeypatch.setenv("KSEF_TOKEN", "test-token")
    zamkniety = []
    oryginalny_close = RejestrNumerow.close

    def sledzone_close(self):
        zamkniety.append(True)
        oryginalny_close(self)

    def fake_sync(self, profil_nip, baza):
        raise SymfoniaSqlError("timeout")

    monkeypatch.setattr(RejestrNumerow, "synchronizuj_z_sql", fake_sync)
    monkeypatch.setattr(RejestrNumerow, "close", sledzone_close)

    main(["--nip", "5252445719", "--cel", str(tmp_path), "--sync-sql", "TESTBAZA"])

    assert zamkniety == [True]


def test_odswiez_pliki_bez_tokena_kody_wyjscia(monkeypatch, capsys):
    """--odswiez-pliki działa bez tokena (Harmonogram zadań); 0 = zgodne/poprawione,
    1 = plik mógł już zostać zaimportowany ze złym kontem, 2 = błąd bazy."""
    from pathlib import Path

    import app.cli as cli_mod
    from app.swiezosc import RaportOdswiezenia, ZmianaNumeru

    monkeypatch.delenv("KSEF_TOKEN", raising=False)
    monkeypatch.setattr(cli_mod.getpass, "getpass", lambda *a: (_ for _ in ()).throw(
        AssertionError("odświeżanie nie może pytać o token")))

    wyniki = [
        RaportOdswiezenia(sprawdzone=1),
        RaportOdswiezenia(sprawdzone=1, zmiany=[
            ZmianaNumeru(Path("f.TXT"), "Gamma", 3, 4, mozliwie_zaimportowany=True)
        ]),
    ]
    monkeypatch.setattr(cli_mod, "odswiez_pliki", lambda nip, baza, rej, pliki: wyniki.pop(0))
    assert main(["--nip", "5252445719", "--odswiez-pliki", "TESTBAZA"]) == 0
    assert "zgodne z kartoteką" in capsys.readouterr().out
    assert main(["--nip", "5252445719", "--odswiez-pliki", "TESTBAZA"]) == 1
    assert "Gamma" in capsys.readouterr().out

    def blad(*a):
        raise SymfoniaSqlError("brak sqlcmd w PATH")

    monkeypatch.setattr(cli_mod, "odswiez_pliki", blad)
    assert main(["--nip", "5252445719", "--odswiez-pliki", "TESTBAZA"]) == 2
    assert "brak sqlcmd" in capsys.readouterr().err
