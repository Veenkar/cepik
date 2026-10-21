# CEPiK w liczbach

Statyczna strona prezentująca statystyki samochodów osobowych zarejestrowanych
w Polsce, na podstawie danych z [CEPiK](https://api.cepik.gov.pl)
(Centralna Ewidencja Pojazdów i Kierowców). Obejmuje wszystkie 16 województw,
tylko samochody osobowe.

Hierarchia stron: nationwide (`/model/<marka>/<model>/`) → województwo
(`/model/<marka>/<model>/<wojewodztwo>/`) → powiat
(`/model/<marka>/<model>/<wojewodztwo>/<powiat>/`), plus `/wojewodztwo/<slug>/`
i `/powiat/<wojewodztwo>/<powiat>/` jako strony zbiorcze dla regionu.

Zobacz [NOTES.md](NOTES.md) dla pełnych wyników rekonesansu API (co działa, co
nie, jakie są ograniczenia danych) oraz kluczowych decyzji dotyczących
pipeline'u.

## Wymagania

- Python 3.10+
- Brak zewnętrznych usług, brak serwera bazy danych (DuckDB jest plikiem)

## Instalacja

```
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Użycie

Cały pipeline jednym poleceniem:

```
python run_all.py
```

Albo krok po kroku:

```
python ingest.py --bulk                                                         # jednorazowy zrzut historyczny z /pliki, wszystkie 16 województw
python ingest.py --voivodeship 12 --api-from 2026-10-01 --api-to 2026-10-20     # dane bieżące z /pojazdy, jedno województwo na raz
python clean.py                                                                  # normalizacja marka/model, flagowanie podejrzanych rekordów
python aggregate.py                                                              # tabele zagregowane do DuckDB
python build_site.py                                                             # generuje site/
```

`ingest.py --bulk` bez `--voivodeship` pętli po wszystkich 16 kodach
województw (pomija te już wczytane). `--api-from`/`--api-to` wymagają
`--voivodeship` - `/pojazdy` przyjmuje tylko jedno województwo na zapytanie.

Podgląd lokalny:

```
cd site
python -m http.server 8000 --bind 127.0.0.1
```

i otwórz http://127.0.0.1:8000/ w przeglądarce.

## Pełny backfill historyczny

`ingest.py --bulk` wczytuje jednorazowy zrzut z 2022-04-17 (najnowszy
dostępny z `/pliki` - zobacz NOTES.md, ten mechanizm wygląda na
zaniechany), dla wszystkich 16 województw. Dociągnięcie całej historii od
tej daty do teraz przez `/pojazdy` to wiele godzin na województwo ze
względu na throttling API (zobacz NOTES.md) - `run_all.py` robi to
automatycznie tylko dla Małopolskiego (patrz `API_INCREMENTAL_VOIVODESHIP`
w `run_all.py`); dla innych województw uruchom to świadomie, jako osobne,
długie zadanie:

```
python ingest.py --voivodeship 14 --api-from 2022-04-18 --api-to 2026-10-20
```

Bezpiecznie przerwać i wznowić w każdej chwili - już pobrane dane (po
pojazd_id) nie zdublują się.

## Struktura repozytorium

- `ingest.py` - pobieranie danych (bulk + API) do `data/cepik.duckdb`
- `clean.py` - normalizacja marka/model (`mappings/*.csv`), flagowanie podejrzanych rekordów
- `aggregate.py` - tabele zagregowane (liczby pojazdów, rankingi, wiek, trend)
- `build_site.py` - generator strony statycznej (`templates/` → `site/`)
- `run_all.py` - całość: ingest → clean → aggregate → build_site, z kontrolą sanity (±20%)
- `mappings/` - ręcznie edytowalne tabele normalizacji marka/model
- `reports/quality.md` - raport jakości danych (generowany przez `clean.py`)
- `NOTES.md` - wyniki rekonesansu API i kluczowe decyzje projektowe

## Znane ograniczenia (szczegóły w NOTES.md)

- Dane CEPiK zawierają "martwe dusze" - pojazdy fizycznie nieistniejące, ale
  nigdy formalnie niewyrejestrowane. Filtrowanie po `data_wyrejestrowania`
  wykrywa tylko formalne wyrejestrowania, nie rozwiązuje problemu w pełni.
- Trzy powiaty ziemskie w Małopolskim (krakowski, nowosądecki, tarnowski)
  nie występują osobno w danych CEPiK - niezweryfikowane, czy analogiczny
  problem dotyczy innych województw.
- API CEPiK ma niestabilne liczniki wyników i throttling, który bywa znacznie
  silniejszy niż sugerowałby krótki test.
