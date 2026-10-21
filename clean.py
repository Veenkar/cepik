"""Normalize make/model and flag suspicious rows, without deleting anything.

Reads vehicles_raw, applies the hand-editable mapping tables in mappings/
(bootstrapping them with any newly-seen raw values on each run, marked
needs_review=true so a human can correct them), and rebuilds vehicles_clean
as a plain derived table - safe to re-run any time, no incremental state.

Suspect flagging (suspect=true, suspect_reasons lists all that apply) covers
exactly the three cases the project brief calls out:
  - rok_produkcji_nieliczbowy / rok_produkcji_nieprawdopodobny: production
    year is not a number, or outside a plausible range. See NOTES.md for the
    "ZABYTKOWY" and 1900-spike findings that motivate this.
  - brak_modelu: model is missing or a placeholder ("---").
  - wyrejestrowany: vehicle has been formally deregistered. Not a data
    *error*, but every "currently on the road" metric must exclude it - see
    NOTES.md on the "dead souls" problem (this only catches *formal*
    deregistration, not vehicles that are gone without paperwork).

Make/model mapping coverage (how many raw values are still needs_review) is
reported separately in reports/quality.md, not folded into the suspect count
- that's a data-completeness metric, not a data-defect one.
"""
from __future__ import annotations

import csv
import re
from datetime import date
from pathlib import Path

import duckdb

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "cepik.duckdb"
MAPPINGS_DIR = BASE_DIR / "mappings"
REPORTS_DIR = BASE_DIR / "reports"

MARKA_MAPPING_PATH = MAPPINGS_DIR / "marka_mapping.csv"
MODEL_MAPPING_PATH = MAPPINGS_DIR / "model_mapping.csv"

# The bulk file is loaded pre-filtered to passenger cars (ingest.py), but the
# API has no working server-side type filter (confirmed empirically - see
# NOTES.md) so API-sourced rows include every vehicle type. Filtering here,
# uniformly for both sources, is what actually enforces the MVP's
# "passenger cars only" scope.
PASSENGER_CAR_RODZAJ_VARIANTS = [
    "SAMOCHÓD OSOBOWY",
    "SAMOCHOD OSOBOWY",
    "SAMOCHAD OSOBOWY",
    "SAMOCHED OSOBOWY",
    "SAMOCHODY OSOBOWE",
    "OSOBOWY",
    "SAM.OSOBOWY",
]

MIN_PLAUSIBLE_YEAR = 1905  # first mass-produced cars; anything earlier in
# CEPiK data is a typo (see NOTES.md: 1069, 1077, 1771, 1880, ...)
SUSPECT_YEAR_SENTINEL = "1900"  # 187 Malopolskie rows have exactly this value
# - far more than any other implausible year, so it reads as a legacy
# "unknown year" placeholder rather than genuine 1900-built cars

# A handful of well-known make aliases, hand-curated as a starting point -
# extend mappings/marka_mapping.csv directly for anything else.
MARKA_ALIASES = {
    "VW": "VOLKSWAGEN",
    "MERCEDES": "MERCEDES-BENZ",
    "MERCEDES BENZ": "MERCEDES-BENZ",
}

# Known model-name special cases that the automatic first-token heuristic
# gets wrong - see NOTES.md's Nissan Note writeup. Keyed by (clean_marka,
# raw_model_normalized).
MODEL_OVERRIDES = {
    ("NISSAN", "E11 NOTE"): "NOTE",
}


def normalize_marka(raw: str) -> str:
    text = raw.strip().upper()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .-")
    return MARKA_ALIASES.get(text, text)


_PLACEHOLDER_MODEL_RE = re.compile(r"^[\s.\-]*$")  # '-', '--', '---', '', etc.


def is_placeholder_model(raw: str | None) -> bool:
    return raw is None or bool(_PLACEHOLDER_MODEL_RE.match(raw))


def normalize_model_base(clean_marka: str, raw: str) -> str:
    """First-token heuristic: group trims/engine variants under their base
    model name, without ever doing substring matching (that's what wrongly
    merges e.g. 'VERSA NOTE S' into 'NOTE' - see NOTES.md).

    Checks for placeholder values ('-', '---', ...) BEFORE stripping - stripping
    first would turn '-' into '' and silently swallow the placeholder."""
    if is_placeholder_model(raw):
        return raw.strip().upper() if raw else ""

    text = raw.strip().upper()
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" .-")
    if not text:
        return raw.strip().upper()

    override = MODEL_OVERRIDES.get((clean_marka, text))
    if override:
        return override

    tokens = text.split(" ")
    if tokens[0] == clean_marka and len(tokens) > 1:
        tokens = tokens[1:]
    return tokens[0]


def load_mapping(path: Path, key_cols: list[str], value_col: str) -> dict[tuple, dict]:
    if not path.exists():
        return {}
    mapping = {}
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            key = tuple(row[c] for c in key_cols)
            mapping[key] = row
    return mapping


def write_mapping(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    rows_sorted = sorted(rows, key=lambda r: tuple(r[c] for c in fieldnames))
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows_sorted)


def bootstrap_marka_mapping(con: duckdb.DuckDBPyConnection) -> dict[tuple, dict]:
    MAPPINGS_DIR.mkdir(parents=True, exist_ok=True)
    existing = load_mapping(MARKA_MAPPING_PATH, ["raw_marka"], "clean_marka")

    observed = con.execute(
        "SELECT DISTINCT marka FROM vehicles_raw WHERE marka IS NOT NULL"
    ).fetchall()

    changed = False
    for (raw,) in observed:
        if (raw,) not in existing:
            # Every freshly-bootstrapped mapping starts as needs_review=true,
            # whether or not normalize_marka() actually changed anything -
            # "unreviewed" is a statement about human attention, not about
            # how much the text transform did. Flip to false by hand in
            # mappings/marka_mapping.csv once you've checked a row.
            existing[(raw,)] = {
                "raw_marka": raw,
                "clean_marka": normalize_marka(raw),
                "needs_review": "true",
            }
            changed = True

    if changed or not MARKA_MAPPING_PATH.exists():
        write_mapping(
            MARKA_MAPPING_PATH,
            list(existing.values()),
            ["raw_marka", "clean_marka", "needs_review"],
        )
    return existing


def bootstrap_model_mapping(con: duckdb.DuckDBPyConnection, marka_mapping: dict[tuple, dict]) -> dict[tuple, dict]:
    existing = load_mapping(MODEL_MAPPING_PATH, ["clean_marka", "raw_model"], "clean_model")

    observed = con.execute(
        "SELECT DISTINCT marka, model FROM vehicles_raw WHERE model IS NOT NULL AND marka IS NOT NULL"
    ).fetchall()

    changed = False
    for raw_marka, raw_model in observed:
        clean_marka = marka_mapping.get((raw_marka,), {}).get("clean_marka", normalize_marka(raw_marka))
        key = (clean_marka, raw_model)
        if key not in existing:
            clean_guess = normalize_model_base(clean_marka, raw_model)
            is_override = (clean_marka, raw_model.strip().upper()) in MODEL_OVERRIDES
            existing[key] = {
                "clean_marka": clean_marka,
                "raw_model": raw_model,
                "clean_model": clean_guess,
                "needs_review": "false" if is_override else "true",
            }
            changed = True

    if changed or not MODEL_MAPPING_PATH.exists():
        write_mapping(
            MODEL_MAPPING_PATH,
            list(existing.values()),
            ["clean_marka", "raw_model", "clean_model", "needs_review"],
        )
    return existing


def rebuild_vehicles_clean(con: duckdb.DuckDBPyConnection) -> None:
    rodzaj_list_sql = ", ".join(f"'{v}'" for v in PASSENGER_CAR_RODZAJ_VARIANTS)
    con.execute("DROP TABLE IF EXISTS vehicles_clean")
    con.execute(
        f"""
        CREATE TABLE vehicles_clean AS
        SELECT
            v.*,
            mk.clean_marka,
            md.clean_model,
            mk.needs_review AS marka_needs_review,
            md.needs_review AS model_needs_review,
            (v.rodzaj_pojazdu IN ({rodzaj_list_sql})) AS is_passenger_car,
            CASE WHEN try_cast(v.rok_produkcji AS INTEGER) IS NULL THEN true ELSE false END AS year_is_non_numeric,
            CASE
                WHEN try_cast(v.rok_produkcji AS INTEGER) IS NOT NULL
                     AND (try_cast(v.rok_produkcji AS INTEGER) < {MIN_PLAUSIBLE_YEAR}
                          OR try_cast(v.rok_produkcji AS INTEGER) > year(current_date) + 1)
                THEN true
                ELSE false
            END AS year_out_of_range,
            CASE WHEN v.rok_produkcji = '{SUSPECT_YEAR_SENTINEL}' THEN true ELSE false END AS year_is_sentinel,
            CASE WHEN v.model IS NULL OR regexp_matches(trim(v.model), '^[.-]*$') THEN true ELSE false END AS model_missing,
            CASE WHEN v.marka IS NULL THEN true ELSE false END AS marka_missing,
            CASE WHEN v.data_wyrejestrowania IS NOT NULL THEN true ELSE false END AS is_deregistered,
            list_filter(
                [
                    CASE WHEN try_cast(v.rok_produkcji AS INTEGER) IS NULL THEN 'rok_produkcji_nieliczbowy' END,
                    CASE WHEN try_cast(v.rok_produkcji AS INTEGER) IS NOT NULL
                              AND (try_cast(v.rok_produkcji AS INTEGER) < {MIN_PLAUSIBLE_YEAR}
                                   OR try_cast(v.rok_produkcji AS INTEGER) > year(current_date) + 1)
                         THEN 'rok_produkcji_nieprawdopodobny' END,
                    CASE WHEN v.rok_produkcji = '{SUSPECT_YEAR_SENTINEL}' THEN 'rok_produkcji_prawdopodobnie_nieznany' END,
                    CASE WHEN v.model IS NULL OR regexp_matches(trim(v.model), '^[.-]*$') THEN 'brak_modelu' END,
                    CASE WHEN v.marka IS NULL THEN 'marka_brak' END,
                    CASE WHEN v.data_wyrejestrowania IS NOT NULL THEN 'wyrejestrowany' END
                ],
                x -> x IS NOT NULL
            ) AS suspect_reasons
        FROM vehicles_raw v
        LEFT JOIN read_csv('{MARKA_MAPPING_PATH.as_posix()}', header=true, all_varchar=true) mk
            ON v.marka = mk.raw_marka
        LEFT JOIN read_csv('{MODEL_MAPPING_PATH.as_posix()}', header=true, all_varchar=true) md
            ON mk.clean_marka = md.clean_marka AND v.model = md.raw_model
        """
    )
    con.execute("ALTER TABLE vehicles_clean ADD COLUMN suspect BOOLEAN")
    con.execute("UPDATE vehicles_clean SET suspect = (len(suspect_reasons) > 0)")


def write_quality_report(con: duckdb.DuckDBPyConnection) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    total = con.execute("SELECT COUNT(*) FROM vehicles_clean").fetchone()[0]
    suspect_total = con.execute("SELECT COUNT(*) FROM vehicles_clean WHERE suspect").fetchone()[0]
    reason_counts = con.execute(
        """
        SELECT reason, COUNT(*) c
        FROM (SELECT unnest(suspect_reasons) AS reason FROM vehicles_clean)
        GROUP BY 1 ORDER BY c DESC
        """
    ).fetchall()

    marka_needs_review = con.execute(
        "SELECT COUNT(DISTINCT raw_marka) FROM read_csv(?, header=true, all_varchar=true) WHERE needs_review = 'true'",
        [str(MARKA_MAPPING_PATH)],
    ).fetchone()[0]
    model_needs_review_rows = con.execute(
        """
        SELECT clean_marka, raw_model, COUNT(*) OVER () AS total_flagged
        FROM read_csv(?, header=true, all_varchar=true)
        WHERE needs_review = 'true'
        """,
        [str(MODEL_MAPPING_PATH)],
    ).fetchall()
    model_needs_review_total = model_needs_review_rows[0][2] if model_needs_review_rows else 0

    top_unmapped_models = con.execute(
        """
        SELECT marka, model, COUNT(*) c
        FROM vehicles_clean
        WHERE model_needs_review = 'true'
        GROUP BY 1, 2
        ORDER BY c DESC
        LIMIT 25
        """
    ).fetchall()

    lines = []
    lines.append("# Data quality report\n")
    lines.append(f"Generated: {date.today().isoformat()}\n")
    lines.append(f"\n## Record counts\n")
    lines.append(f"- Total rows in `vehicles_clean`: {total:,}")
    lines.append(f"- Flagged suspect (any reason): {suspect_total:,} ({suspect_total / total:.1%})")
    lines.append("\n### Suspect reasons (a row may have more than one)\n")
    lines.append("| reason | rows |")
    lines.append("|---|---|")
    for reason, c in reason_counts:
        lines.append(f"| {reason} | {c:,} |")

    lines.append("\n## Make/model mapping coverage\n")
    lines.append(f"- Distinct raw `marka` values still marked `needs_review`: {marka_needs_review:,}")
    lines.append(f"- Distinct raw `model` values still marked `needs_review`: {model_needs_review_total:,}")
    lines.append(
        "\nThese are not counted as \"suspect\" rows - they're mapped to a "
        "best-effort normalized value automatically (see clean.py's "
        "first-token heuristic) and are safe to use, but a human should "
        "review `mappings/marka_mapping.csv` / `mappings/model_mapping.csv` "
        "and fix any that look wrong.\n"
    )

    lines.append("### Top 25 unmapped (model, needs_review) combinations by volume\n")
    lines.append("| marka | model (raw) | rows |")
    lines.append("|---|---|---|")
    for marka, model, c in top_unmapped_models:
        lines.append(f"| {marka} | {model} | {c:,} |")

    (REPORTS_DIR / "quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {REPORTS_DIR / 'quality.md'}")


def main() -> None:
    con = duckdb.connect(str(DB_PATH))
    print("bootstrapping make mapping ...")
    marka_mapping = bootstrap_marka_mapping(con)
    print(f"  {len(marka_mapping)} distinct raw make values known")
    print("bootstrapping model mapping ...")
    model_mapping = bootstrap_model_mapping(con, marka_mapping)
    print(f"  {len(model_mapping)} distinct (make, model) combinations known")
    print("rebuilding vehicles_clean ...")
    rebuild_vehicles_clean(con)
    print("writing quality report ...")
    write_quality_report(con)
    con.close()


if __name__ == "__main__":
    main()
