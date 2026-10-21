"""Build the DuckDB tables build_site.py reads from.

Everything here is a full rebuild from vehicles_clean (CREATE OR REPLACE) -
no incremental state, so re-running is always safe.

Two different base filters are used on purpose, and this is the single place
that decision is made (see NOTES.md on "dead souls" and the per-page
methodology notes in templates/ that describe these filters to site
visitors):

  - ACTIVE_FILTER ("currently on the road"): excludes formally deregistered
    vehicles, rows with an unusable production year (non-numeric, out of
    plausible range, or the suspicious 1900 sentinel - see clean.py), and
    likely_dead_soul rows (old + decades of no registration activity - added
    after FSO-Warszawa 125P, a car last produced in 1991, showed up as the
    #2 most common model in Malopolskie before this filter existed). This is
    a heuristic, not a guarantee - some genuine well-kept classics will be
    excluded, and some true dead souls that are younger than the age cutoff
    will still slip through. Used for "how many X are on the road" and
    "average age" style metrics.
  - historical trend metrics intentionally do NOT exclude deregistered
    vehicles: a car first registered in 2010 and scrapped in 2020 is still a
    real first-registration event in 2010.
"""
from __future__ import annotations

from pathlib import Path

import duckdb

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "cepik.duckdb"

MIN_VEHICLES_FOR_PAGE = 10  # thin-content floor, applied again at build time

ACTIVE_FILTER = """
    is_passenger_car
    AND NOT is_deregistered
    AND NOT year_is_non_numeric
    AND NOT year_out_of_range
    AND NOT year_is_sentinel
    AND NOT likely_dead_soul
"""


def build_vehicle_counts(con: duckdb.DuckDBPyConnection) -> None:
    """make x model x production year x voivodeship x county, currently-active vehicles only."""
    con.execute("DROP TABLE IF EXISTS agg_vehicle_counts")
    con.execute(
        f"""
        CREATE TABLE agg_vehicle_counts AS
        SELECT
            clean_marka AS marka,
            clean_model AS model,
            try_cast(rok_produkcji AS INTEGER) AS rok_produkcji,
            wojewodztwo_kod,
            rejestracja_wojewodztwo AS wojewodztwo,
            rejestracja_powiat AS powiat,
            COUNT(*) AS n_vehicles
        FROM vehicles_clean
        WHERE {ACTIVE_FILTER}
          AND NOT model_missing
          AND NOT marka_missing
        GROUP BY 1, 2, 3, 4, 5, 6
        """
    )


def build_registration_trend(con: duckdb.DuckDBPyConnection) -> None:
    """First-registration-in-Poland counts per year, split new/used and
    domestic/imported. Deliberately includes vehicles later deregistered -
    this is about historical registration events, not current fleet size."""
    con.execute("DROP TABLE IF EXISTS agg_registration_trend")
    con.execute(
        f"""
        CREATE TABLE agg_registration_trend AS
        SELECT
            marka,
            model,
            year(data_pierwszej_rejestracji_w_kraju) AS rok_rejestracji,
            CASE WHEN pochodzenie_pojazdu ILIKE '%NOWY%' THEN 'NOWY' ELSE 'UZYWANY' END AS stan,
            CASE WHEN pochodzenie_pojazdu ILIKE '%IMPORT%' THEN 'IMPORT' ELSE 'KRAJOWY' END AS pochodzenie,
            COUNT(*) AS n_vehicles
        FROM (
            SELECT clean_marka AS marka, clean_model AS model, data_pierwszej_rejestracji_w_kraju, pochodzenie_pojazdu
            FROM vehicles_clean
            WHERE is_passenger_car
              AND NOT year_is_non_numeric
              AND NOT year_out_of_range
              AND data_pierwszej_rejestracji_w_kraju IS NOT NULL
              AND NOT model_missing
              AND NOT marka_missing
        )
        GROUP BY 1, 2, 3, 4, 5
        """
    )


def build_rankings(con: duckdb.DuckDBPyConnection) -> None:
    """Top makes and top models per county, currently-active vehicles only."""
    con.execute("DROP TABLE IF EXISTS agg_ranking_marka_powiat")
    con.execute(
        f"""
        CREATE TABLE agg_ranking_marka_powiat AS
        SELECT
            powiat,
            marka,
            n_vehicles,
            RANK() OVER (PARTITION BY powiat ORDER BY n_vehicles DESC) AS rank
        FROM (
            SELECT rejestracja_powiat AS powiat, clean_marka AS marka, COUNT(*) AS n_vehicles
            FROM vehicles_clean
            WHERE {ACTIVE_FILTER}
              AND NOT marka_missing
            GROUP BY 1, 2
        )
        """
    )

    con.execute("DROP TABLE IF EXISTS agg_ranking_model_powiat")
    con.execute(
        f"""
        CREATE TABLE agg_ranking_model_powiat AS
        SELECT
            powiat,
            marka,
            model,
            n_vehicles,
            RANK() OVER (PARTITION BY powiat ORDER BY n_vehicles DESC) AS rank
        FROM (
            SELECT rejestracja_powiat AS powiat, clean_marka AS marka, clean_model AS model, COUNT(*) AS n_vehicles
            FROM vehicles_clean
            WHERE {ACTIVE_FILTER}
              AND NOT model_missing
              AND NOT marka_missing
            GROUP BY 1, 2, 3
        )
        """
    )


def build_age_stats(con: duckdb.DuckDBPyConnection) -> None:
    """Median/mean age per model and per county, currently-active vehicles only."""
    con.execute("DROP TABLE IF EXISTS agg_age_by_model")
    con.execute(
        f"""
        CREATE TABLE agg_age_by_model AS
        SELECT
            marka,
            model,
            median(age) AS median_age_years,
            avg(age) AS mean_age_years,
            COUNT(*) AS n_vehicles
        FROM (
            SELECT
                clean_marka AS marka,
                clean_model AS model,
                year(current_date) - try_cast(rok_produkcji AS INTEGER) AS age
            FROM vehicles_clean
            WHERE {ACTIVE_FILTER}
              AND NOT model_missing
              AND NOT marka_missing
        )
        GROUP BY 1, 2
        """
    )

    con.execute("DROP TABLE IF EXISTS agg_age_by_powiat")
    con.execute(
        f"""
        CREATE TABLE agg_age_by_powiat AS
        SELECT
            powiat,
            median(age) AS median_age_years,
            avg(age) AS mean_age_years,
            COUNT(*) AS n_vehicles
        FROM (
            SELECT
                rejestracja_powiat AS powiat,
                year(current_date) - try_cast(rok_produkcji AS INTEGER) AS age
            FROM vehicles_clean
            WHERE {ACTIVE_FILTER}
        )
        GROUP BY 1
        """
    )


def build_totals(con: duckdb.DuckDBPyConnection) -> None:
    """Overall active-fleet size, INCLUDING the ~7% of rows with no
    identifiable make (see NOTES.md) - agg_vehicle_counts excludes those
    since they can't be attributed to a make/model, which would otherwise
    make the homepage total undercount the real fleet size."""
    con.execute("DROP TABLE IF EXISTS agg_totals")
    con.execute(
        f"""
        CREATE TABLE agg_totals AS
        SELECT
            COUNT(*) AS total_active,
            SUM(CASE WHEN marka_missing THEN 1 ELSE 0 END) AS total_active_no_marka
        FROM vehicles_clean
        WHERE {ACTIVE_FILTER}
        """
    )


def main() -> None:
    con = duckdb.connect(str(DB_PATH))
    print("building agg_vehicle_counts ...")
    build_vehicle_counts(con)
    print("building agg_registration_trend ...")
    build_registration_trend(con)
    print("building rankings ...")
    build_rankings(con)
    print("building age stats ...")
    build_age_stats(con)
    print("building totals ...")
    build_totals(con)

    for table in [
        "agg_vehicle_counts",
        "agg_registration_trend",
        "agg_ranking_marka_powiat",
        "agg_ranking_model_powiat",
        "agg_age_by_model",
        "agg_age_by_powiat",
        "agg_totals",
    ]:
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {n:,} rows")

    con.close()


if __name__ == "__main__":
    main()
