"""Download CEPiK vehicle data and load it into DuckDB.

Two sources, both idempotent (safe to re-run):

  --bulk                            one-time historical snapshot from
                                     /pliki (stale, see NOTES.md section 3 -
                                     dated 2022-04-17 for every voivodeship
                                     as of this writing). Loops over all 16
                                     real voivodeships unless --voivodeship
                                     restricts it to one.
  --voivodeship CODE --api-from/-to  incremental data from the /pojazdy API
                                     for one voivodeship and date range
                                     (first-registration date), paginated.
                                     One voivodeship at a time - /pojazdy
                                     doesn't accept more than one.

Rows are deduplicated by pojazd_id (primary key + ON CONFLICT DO NOTHING), so
re-running either step never creates duplicates. All vehicle types are kept
in vehicles_raw; filtering to passenger cars only happens for the bulk file
at load time (MVP scope, see NOTES.md) since the API has no working
server-side type filter (confirmed empirically - a rodzaj-pojazdu query
param is silently ignored).

See NOTES.md for why both sources need care: the bulk file is stale/
incomplete (missing 3 counties), and the API returns inconsistent total
counts for identical repeated queries.
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "cepik.duckdb"

API_BASE = "https://api.cepik.gov.pl"
CONTACT_EMAIL = "veenkar@gmail.com"
USER_AGENT = (
    f"cepik-stats-pl/0.1 (niekomercyjny projekt osobisty; kontakt: {CONTACT_EMAIL})"
)

# All 16 real voivodeship TERYT codes (excludes "XX" = undetermined, which
# is not a real region and has no bulk file worth ingesting). Source:
# /slowniki/wojewodztwa, see NOTES.md.
ALL_VOIVODESHIP_CODES = {
    "02": "DOLNOŚLĄSKIE", "04": "KUJAWSKO-POMORSKIE", "06": "LUBELSKIE",
    "08": "LUBUSKIE", "10": "ŁÓDZKIE", "12": "MAŁOPOLSKIE",
    "14": "MAZOWIECKIE", "16": "OPOLSKIE", "18": "PODKARPACKIE",
    "20": "PODLASKIE", "22": "POMORSKIE", "24": "ŚLĄSKIE",
    "26": "ŚWIĘTOKRZYSKIE", "28": "WARMIŃSKO-MAZURSKIE",
    "30": "WIELKOPOLSKIE", "32": "ZACHODNIOPOMORSKIE",
}

# Passenger-car "rodzaj" values observed in the Malopolskie bulk file
# (canonical + known misspellings/variants with non-trivial occurrence
# counts - see NOTES.md "rodzaj-pojazdu dictionary is severely dirty").
# Deliberately an allow-list, not a substring match, so we don't pull in
# e.g. SAM.CIEZAROWO-OSOBOWY (a cargo/passenger hybrid truck).
PASSENGER_CAR_RODZAJ_VARIANTS = [
    "SAMOCHÓD OSOBOWY",
    "SAMOCHOD OSOBOWY",
    "SAMOCHAD OSOBOWY",
    "SAMOCHED OSOBOWY",
    "SAMOCHODY OSOBOWE",
    "OSOBOWY",
    "SAM.OSOBOWY",
]

RETRY_STATUS = {404, 429, 500, 502, 503, 504}  # 404 included: this backend is
# flakily inconsistent across its load-balancer pool members (see NOTES.md) -
# a transient 404 on a route that should exist is worth one retry
MAX_RETRIES = 8
REQUEST_DELAY_SECONDS = 1.5  # minimum gap between successful requests
CHUNK_FAILURE_COOLDOWN_SECONDS = 90  # extra pause after giving up on a chunk,
# on top of each request's own backoff, to let a sustained 429 burst clear
API_PAGE_LIMIT = 100


class CepikTLSAdapter(HTTPAdapter):
    """api.cepik.gov.pl serves a DH key modern OpenSSL (SECLEVEL 2+) rejects
    outright. Lowering the security level only for this one host - see
    NOTES.md section 0. Needs re-verifying on Windows."""

    def _weak_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
        return ctx

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._weak_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._weak_context()
        return super().proxy_manager_for(*args, **kwargs)


def make_session() -> requests.Session:
    session = requests.Session()
    session.mount("https://api.cepik.gov.pl", CepikTLSAdapter())
    session.headers["User-Agent"] = USER_AGENT
    return session


def request_with_retry(
    session: requests.Session, url: str, params: dict | None = None
) -> requests.Response:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=60)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            wait = min(2**attempt, 60)
            print(f"    [retry {attempt}/{MAX_RETRIES}] connection error: {exc} - waiting {wait}s")
            time.sleep(wait)
            continue
        if resp.status_code in RETRY_STATUS:
            wait = min(2**attempt, 60)
            print(f"    [retry {attempt}/{MAX_RETRIES}] HTTP {resp.status_code} - waiting {wait}s")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        time.sleep(REQUEST_DELAY_SECONDS)
        return resp
    raise RuntimeError(f"giving up on {url} after {MAX_RETRIES} retries") from last_exc


SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS ingest_log_seq START 1;

CREATE TABLE IF NOT EXISTS vehicles_raw (
    pojazd_id BIGINT PRIMARY KEY,
    marka VARCHAR,
    model VARCHAR,
    typ VARCHAR,
    wariant VARCHAR,
    wersja VARCHAR,
    kategoria_pojazdu VARCHAR,
    rodzaj_pojazdu VARCHAR,
    podrodzaj_pojazdu VARCHAR,
    przeznaczenie_pojazdu VARCHAR,
    pochodzenie_pojazdu VARCHAR,
    rok_produkcji VARCHAR,
    sposob_produkcji VARCHAR,
    data_pierwszej_rejestracji DATE,
    data_pierwszej_rejestracji_w_kraju DATE,
    data_ostatniej_rejestracji_w_kraju DATE,
    data_wyrejestrowania DATE,
    przyczyna_wyrejestrowania VARCHAR,
    rodzaj_paliwa VARCHAR,
    pojemnosc_skokowa_silnika DOUBLE,
    masa_wlasna INTEGER,
    wojewodztwo_kod VARCHAR,
    rejestracja_wojewodztwo VARCHAR,
    rejestracja_powiat VARCHAR,
    rejestracja_gmina VARCHAR,
    source VARCHAR,
    ingested_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingest_log (
    id INTEGER PRIMARY KEY DEFAULT nextval('ingest_log_seq'),
    source VARCHAR,
    params VARCHAR,
    started_at TIMESTAMP,
    finished_at TIMESTAMP,
    rows_fetched INTEGER,
    rows_inserted INTEGER,
    status VARCHAR
);
"""


def ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_SQL)


def already_done(con: duckdb.DuckDBPyConnection, source: str, params: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM ingest_log WHERE source = ? AND params = ? AND status = 'success' LIMIT 1",
        [source, params],
    ).fetchone()
    return row is not None


def log_run(
    con: duckdb.DuckDBPyConnection,
    source: str,
    params: str,
    started_at: datetime,
    rows_fetched: int,
    rows_inserted: int,
    status: str,
) -> None:
    con.execute(
        """INSERT INTO ingest_log (source, params, started_at, finished_at, rows_fetched, rows_inserted, status)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [source, params, started_at, datetime.now(), rows_fetched, rows_inserted, status],
    )


# ---------------------------------------------------------------------------
# Bulk file ingestion
# ---------------------------------------------------------------------------


def find_bulk_file_url(session: requests.Session, voivodeship_code: str) -> tuple[str, str]:
    resp = request_with_retry(session, f"{API_BASE}/pliki")
    payload = resp.json()
    file_id = f"pojazdy_{voivodeship_code}"
    for entry in payload["data"]:
        if entry["id"] == file_id:
            attrs = entry["attributes"]
            return attrs["url-do-pliku"], attrs["data-utworzenia-pliku"]
    raise RuntimeError(f"no bulk file entry found for {file_id} in /pliki response")


def download_bulk_file(session: requests.Session, voivodeship_code: str) -> tuple[Path, str]:
    url, file_date = find_bulk_file_url(session, voivodeship_code)
    dest_dir = RAW_DIR / "bulk" / voivodeship_code
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_name = url.rsplit("/", 1)[-1]
    zip_path = dest_dir / zip_name
    csv_path = dest_dir / zip_name.replace(".zip", ".csv")

    if csv_path.exists():
        print(f"  bulk file already downloaded and extracted: {csv_path}")
        return csv_path, file_date

    print(f"  downloading {url} ...")
    resp = request_with_retry(session, url)
    zip_path.write_bytes(resp.content)
    print(f"  downloaded {zip_path} ({zip_path.stat().st_size:,} bytes), extracting ...")
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest_dir)
    return csv_path, file_date


def load_bulk_file(con: duckdb.DuckDBPyConnection, csv_path: Path, file_date: str, voivodeship_code: str) -> None:
    params = f"voivodeship={voivodeship_code};file={csv_path.name}"
    if already_done(con, "bulk", params):
        print(f"  already loaded: {params} - skipping (use --force to redo)")
        return

    started_at = datetime.now()
    print(f"  loading {csv_path} into vehicles_raw (passenger cars only, MVP scope) ...")

    rodzaj_list_sql = ", ".join(f"'{v}'" for v in PASSENGER_CAR_RODZAJ_VARIANTS)
    before = con.execute("SELECT COUNT(*) FROM vehicles_raw WHERE source = 'bulk_2022'").fetchone()[0]

    con.execute(
        f"""
        INSERT INTO vehicles_raw
        SELECT
            pojazd_id,
            marka,
            model,
            typ,
            wariant,
            wersja,
            kategoria AS kategoria_pojazdu,
            rodzaj AS rodzaj_pojazdu,
            podrodzaj AS podrodzaj_pojazdu,
            przeznaczenie AS przeznaczenie_pojazdu,
            pochodzenie AS pochodzenie_pojazdu,
            rok_produkcji,
            sposob_produkcji,
            try_cast(data_pierwszej_rej AS DATE) AS data_pierwszej_rejestracji,
            try_cast(data_pierwszej_rej_w_kraju AS DATE) AS data_pierwszej_rejestracji_w_kraju,
            try_cast(data_rejestracji_ost AS DATE) AS data_ostatniej_rejestracji_w_kraju,
            try_cast(data_wyrejestrowania AS DATE) AS data_wyrejestrowania,
            przyczyna_wyrejestrowania,
            rodzaj_paliwa,
            try_cast(pojemnosc_silnika AS DOUBLE) AS pojemnosc_skokowa_silnika,
            try_cast(masa_wlasna AS INTEGER) AS masa_wlasna,
            akt_miejsce_rej_wojew_teryt AS wojewodztwo_kod,
            akt_miejsce_rej_wojwe AS rejestracja_wojewodztwo,
            akt_miejsce_rej_powiat AS rejestracja_powiat,
            akt_miejsce_rej_gmina AS rejestracja_gmina,
            'bulk_2022' AS source,
            now() AS ingested_at
        FROM read_csv('{csv_path.as_posix()}', header=true, delim=',', quote='"', all_varchar=true)
        WHERE rodzaj IN ({rodzaj_list_sql})
        ON CONFLICT (pojazd_id) DO NOTHING
        """
    )

    after = con.execute("SELECT COUNT(*) FROM vehicles_raw WHERE source = 'bulk_2022'").fetchone()[0]
    inserted = after - before
    log_run(con, "bulk", params, started_at, inserted, inserted, "success")
    print(f"  inserted {inserted:,} passenger-car rows from bulk file dated {file_date}")


# ---------------------------------------------------------------------------
# API ingestion
# ---------------------------------------------------------------------------

API_FIELD_MAP = {
    "marka": "marka",
    "model": "model",
    "typ": "typ",
    "wariant": "wariant",
    "wersja": "wersja",
    "kategoria-pojazdu": "kategoria_pojazdu",
    "rodzaj-pojazdu": "rodzaj_pojazdu",
    "podrodzaj-pojazdu": "podrodzaj_pojazdu",
    "przeznaczenie-pojazdu": "przeznaczenie_pojazdu",
    "pochodzenie-pojazdu": "pochodzenie_pojazdu",
    "rok-produkcji": "rok_produkcji",
    "sposob-produkcji": "sposob_produkcji",
    "data-pierwszej-rejestracji": "data_pierwszej_rejestracji",
    "data-pierwszej-rejestracji-w-kraju": "data_pierwszej_rejestracji_w_kraju",
    "data-ostatniej-rejestracji-w-kraju": "data_ostatniej_rejestracji_w_kraju",
    "data-wyrejestrowania-pojazdu": "data_wyrejestrowania",
    "przyczyna-wyrejestrowania-pojazdu": "przyczyna_wyrejestrowania",
    "rodzaj-paliwa": "rodzaj_paliwa",
    "pojemnosc-skokowa-silnika": "pojemnosc_skokowa_silnika",
    "masa-wlasna": "masa_wlasna",
    "wojewodztwo-kod": "wojewodztwo_kod",
    "rejestracja-wojewodztwo": "rejestracja_wojewodztwo",
    "rejestracja-powiat": "rejestracja_powiat",
    "rejestracja-gmina": "rejestracja_gmina",
}

DB_COLUMNS = [
    "pojazd_id",
    *API_FIELD_MAP.values(),
    "source",
    "ingested_at",
]


def month_chunks(date_from: date, date_to: date):
    cur = date_from
    while cur <= date_to:
        chunk_end = min(date(cur.year + (cur.month == 12), cur.month % 12 + 1, 1) - timedelta(days=1), date_to)
        yield cur, chunk_end
        cur = chunk_end + timedelta(days=1)


def fetch_api_range(con: duckdb.DuckDBPyConnection, session: requests.Session, date_from: date, date_to: date, force: bool, voivodeship_code: str) -> None:
    raw_dir = RAW_DIR / "api" / voivodeship_code
    raw_dir.mkdir(parents=True, exist_ok=True)

    for chunk_from, chunk_to in month_chunks(date_from, date_to):
        params_key = f"voivodeship={voivodeship_code};from={chunk_from};to={chunk_to}"
        if not force and already_done(con, "api", params_key):
            print(f"  already fetched {chunk_from}..{chunk_to} - skipping (use --force to redo)")
            continue

        started_at = datetime.now()
        print(f"  fetching {chunk_from}..{chunk_to} ...")
        total_fetched = 0
        total_inserted = 0
        page = 1
        try:
            while True:
                query = {
                    "wojewodztwo": voivodeship_code,
                    "data-od": chunk_from.strftime("%Y%m%d"),
                    "data-do": chunk_to.strftime("%Y%m%d"),
                    "pokaz-wszystkie-pola": "true",
                    "tylko-zarejestrowane": "false",  # keep deregistered rows too; clean.py flags them
                    "limit": API_PAGE_LIMIT,
                    "page": page,
                }
                resp = request_with_retry(session, f"{API_BASE}/pojazdy", params=query)
                payload = resp.json()

                page_path = raw_dir / f"{chunk_from}_{chunk_to}_page{page}.json"
                page_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

                records = payload.get("data", [])
                total_fetched += len(records)
                total_inserted += insert_api_records(con, records)

                if not records or "next" not in payload.get("links", {}):
                    break
                page += 1
        except Exception as exc:
            # A sustained burst of 429s (observed in practice - this backend's
            # rate limit is not always a quick concurrency-only throttle, see
            # NOTES.md) shouldn't kill the whole multi-month job. Log the
            # partial progress as 'failed' (not 'success', so a later re-run
            # retries this chunk) and move on to the next one. Rows already
            # inserted in this chunk stay - pojazd_id is a primary key, so
            # re-fetching the same chunk later never duplicates them.
            log_run(con, "api", params_key, started_at, total_fetched, total_inserted, f"failed: {exc}")
            print(f"    {chunk_from}..{chunk_to}: FAILED after page {page} ({exc}); {total_inserted:,} rows inserted so far; continuing to next chunk")
            print(f"    cooling down {CHUNK_FAILURE_COOLDOWN_SECONDS}s before continuing ...")
            time.sleep(CHUNK_FAILURE_COOLDOWN_SECONDS)
            continue

        log_run(con, "api", params_key, started_at, total_fetched, total_inserted, "success")
        print(f"    {chunk_from}..{chunk_to}: fetched {total_fetched:,}, inserted {total_inserted:,} new rows")


def insert_api_records(con: duckdb.DuckDBPyConnection, records: list[dict]) -> int:
    if not records:
        return 0

    rows = []
    for rec in records:
        attrs = rec["attributes"]
        row = [int(rec["id"])]
        for api_field in API_FIELD_MAP:
            row.append(attrs.get(api_field))
        row.append("api")
        row.append(datetime.now())
        rows.append(row)

    before = con.execute("SELECT COUNT(*) FROM vehicles_raw").fetchone()[0]
    placeholders = ", ".join(["?"] * len(DB_COLUMNS))
    con.executemany(
        f"""INSERT INTO vehicles_raw ({', '.join(DB_COLUMNS)})
            VALUES ({placeholders})
            ON CONFLICT (pojazd_id) DO NOTHING""",
        rows,
    )
    after = con.execute("SELECT COUNT(*) FROM vehicles_raw").fetchone()[0]
    return after - before


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bulk", action="store_true", help="download+load the bulk file(s)")
    parser.add_argument(
        "--voivodeship", choices=sorted(ALL_VOIVODESHIP_CODES), default=None,
        help="restrict to one voivodeship TERYT code (required for --api-from/--api-to, "
             "since /pojazdy only accepts one at a time; --bulk loops over all codes if omitted)",
    )
    parser.add_argument("--api-from", type=parse_date, help="fetch API data from this date (YYYY-MM-DD)")
    parser.add_argument("--api-to", type=parse_date, help="fetch API data up to this date (YYYY-MM-DD)")
    parser.add_argument("--force", action="store_true", help="re-run even if already logged as done")
    args = parser.parse_args()

    if not args.bulk and not (args.api_from and args.api_to):
        parser.error("pass --bulk and/or --api-from/--api-to")
    if (args.api_from or args.api_to) and not args.voivodeship:
        parser.error("--api-from/--api-to require --voivodeship (one code at a time)")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(DB_PATH))
    ensure_schema(con)
    session = make_session()

    if args.bulk:
        codes = [args.voivodeship] if args.voivodeship else sorted(ALL_VOIVODESHIP_CODES)
        print(f"== bulk file ingestion: {len(codes)} voivodeship(s) ==")
        for code in codes:
            print(f"-- {code} {ALL_VOIVODESHIP_CODES[code]} --")
            csv_path, file_date = download_bulk_file(session, code)
            load_bulk_file(con, csv_path, file_date, code)

    if args.api_from and args.api_to:
        print(f"== API ingestion: {args.voivodeship} {args.api_from} .. {args.api_to} ==")
        fetch_api_range(con, session, args.api_from, args.api_to, args.force, args.voivodeship)

    total = con.execute("SELECT COUNT(*) FROM vehicles_raw").fetchone()[0]
    print(f"\nvehicles_raw total rows: {total:,}")
    con.close()


if __name__ == "__main__":
    main()
