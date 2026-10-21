"""Static site generator: reads the agg_* tables aggregate.py built and
renders templates/ into site/.

Nationwide, drilling down through voivodeship to county. Page types:
  - site/index.html                                          (nationwide)
  - site/model/<marka>/index.html                            (nationwide)
  - site/model/<marka>/<model>/index.html                    (nationwide)
  - site/model/<marka>/<model>/<wojewodztwo>/index.html       (region, matches the brief's .../malopolskie/ example)
  - site/model/<marka>/<model>/<wojewodztwo>/<powiat>/index.html
  - site/wojewodztwo/<wojewodztwo>/index.html
  - site/powiat/<wojewodztwo>/<powiat>/index.html             (namespaced under voivodeship)
  - site/sitemap.xml, site/robots.txt

Pages with fewer than MIN_VEHICLES_FOR_PAGE vehicles are skipped entirely
(thin-content guard, per the project brief) - tracked and reported at the
end, not silently dropped.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "cepik.duckdb"
TEMPLATES_DIR = BASE_DIR / "templates"
SITE_DIR = BASE_DIR / "site"

MIN_VEHICLES_FOR_PAGE = 10
TOP_N_MAKES_HOME = 20
TOP_N_MODELS_HOME = 30
TOP_N_PER_POWIAT = 20
TOP_N_POWIATY_PER_MODEL = 20
SITE_BASE_URL = ""  # same-origin relative links; set e.g. "https://example.pl" before publishing

# ---------------------------------------------------------------------------
# Polish text helpers
# ---------------------------------------------------------------------------

_DIACRITICS = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)


def slugify(text: str) -> str:
    text = text.translate(_DIACRITICS)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


# Hand-curated: the three city counties (Krakow, Nowy Sacz, Tarnow) are
# proper nouns with irregular locative case and are conventionally referred
# to without "powiat" at all in everyday Polish. The rest are regular
# adjectives (ends in "-i" nominative -> "-im" locative), handled by a simple
# rule. See NOTES.md on the three land counties (krakowski, nowosadecki,
# tarnowski) that are entirely missing from CEPiK's data for this
# voivodeship - this dict only needs entries for powiats we actually have
# data for.
POWIAT_IRREGULAR = {
    "KRAKÓW": "w Krakowie",
    "TARNÓW": "w Tarnowie",
    "NOWY SĄCZ": "w Nowym Sączu",
}


def powiat_phrase(nazwa: str) -> str:
    """Full locative prepositional phrase, e.g. 'w powiecie krakowskim' or
    'w Krakowie'."""
    if nazwa in POWIAT_IRREGULAR:
        return POWIAT_IRREGULAR[nazwa]
    adj = nazwa.lower()
    if adj.endswith("i"):
        locative = adj[:-1] + "im"
    else:
        locative = adj  # shouldn't happen for real powiat names, but don't crash
    return f"w powiecie {locative}"


def wojewodztwo_phrase(nazwa: str) -> str:
    """Full locative prepositional phrase, e.g. 'w województwie małopolskim'.
    Unlike powiat names, all 16 voivodeship names are regular neuter
    adjectives ending in '-ie' (nominative) -> '-im' (locative) - no
    exceptions needed, verified against the full list in NOTES.md."""
    adj = nazwa.lower()
    locative = adj[:-2] + "im" if adj.endswith("ie") else adj
    return f"w województwie {locative}"


def capitalize_first(text: str) -> str:
    return text[0].upper() + text[1:] if text else text


def fmt_int(n) -> str:
    if n is None:
        return "-"
    return f"{int(n):,}".replace(",", " ")


def fmt_float(n) -> str:
    if n is None:
        return "-"
    return f"{float(n):.1f}".replace(".", ",")


def ordinal(n) -> str:
    return f"{int(n)}."


# ---------------------------------------------------------------------------
# Jinja setup
# ---------------------------------------------------------------------------


def make_env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), trim_blocks=True, lstrip_blocks=True)
    env.filters["fmt_int"] = fmt_int
    env.filters["fmt_float"] = fmt_float
    env.filters["ordinal"] = ordinal
    return env


def render_page(env: Environment, template_name: str, out_path: Path, context: dict) -> None:
    template = env.get_template(template_name)
    html = template.render(**context)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------


def get_wojewodztwa(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT DISTINCT wojewodztwo FROM agg_vehicle_counts WHERE wojewodztwo IS NOT NULL ORDER BY wojewodztwo"
    ).fetchall()
    return [r[0] for r in rows]


def get_powiaty_by_wojewodztwo(con: duckdb.DuckDBPyConnection) -> dict[str, list[str]]:
    rows = con.execute(
        """
        SELECT DISTINCT wojewodztwo, powiat FROM agg_vehicle_counts
        WHERE powiat IS NOT NULL AND powiat != 'xxxxxxxxx' AND wojewodztwo IS NOT NULL
        ORDER BY wojewodztwo, powiat
        """
    ).fetchall()
    out: dict[str, list[str]] = {}
    for woj, powiat in rows:
        out.setdefault(woj, []).append(powiat)
    return out


# ---------------------------------------------------------------------------
# Page builders
# ---------------------------------------------------------------------------


def build_index(env: Environment, con: duckdb.DuckDBPyConnection, data_updated_at: str, wojewodztwa: list[str]) -> None:
    total_active, total_no_marka = con.execute("SELECT total_active, total_active_no_marka FROM agg_totals").fetchone()
    n_powiats = con.execute(
        "SELECT COUNT(DISTINCT powiat) FROM agg_vehicle_counts WHERE powiat IS NOT NULL AND powiat != 'xxxxxxxxx'"
    ).fetchone()[0]

    top_makes = con.execute(
        f"""
        SELECT marka, SUM(n_vehicles) AS n_vehicles
        FROM agg_vehicle_counts GROUP BY 1 ORDER BY 2 DESC LIMIT {TOP_N_MAKES_HOME}
        """
    ).fetchall()
    top_models = con.execute(
        f"""
        SELECT marka, model, SUM(n_vehicles) AS n_vehicles
        FROM agg_vehicle_counts GROUP BY 1, 2 ORDER BY 3 DESC LIMIT {TOP_N_MODELS_HOME}
        """
    ).fetchall()
    woj_totals = con.execute(
        "SELECT wojewodztwo, total_active FROM agg_totals_by_wojewodztwo ORDER BY wojewodztwo"
    ).fetchall()
    woj_totals_by_name = dict(woj_totals)

    context = {
        "title": "CEPiK w liczbach — statystyki samochodów osobowych w Polsce",
        "description": "Ile samochodów danej marki i modelu jeździ w Polsce? Statystyki na podstawie danych CEPiK, według województw, powiatów i roku produkcji.",
        "canonical_url": f"{SITE_BASE_URL}/" if SITE_BASE_URL else None,
        "root": "",
        "data_updated_at": data_updated_at,
        "total_active": total_active,
        "total_no_marka": total_no_marka,
        "n_powiats": n_powiats,
        "n_wojewodztwa": len(wojewodztwa),
        "top_makes": [{"marka": m, "marka_slug": slugify(m), "n_vehicles": n} for m, n in top_makes],
        "top_models": [{"marka": m, "model": mo, "marka_slug": slugify(m), "model_slug": slugify(mo), "n_vehicles": n} for m, mo, n in top_models],
        "wojewodztwa": [
            {"nazwa": w, "slug": slugify(w), "n_vehicles": woj_totals_by_name.get(w, 0)}
            for w in wojewodztwa
        ],
    }
    render_page(env, "index.html", SITE_DIR / "index.html", context)


def build_model_pages(env: Environment, con: duckdb.DuckDBPyConnection, data_updated_at: str) -> tuple[int, int, int, int]:
    """Nationwide model pages, drilling down through
    model/<marka>/<model>/<wojewodztwo>/<powiat>/ - see NOTES.md for why this
    now matches the brief's original region-intersection example exactly
    once more than one voivodeship is loaded."""
    models = con.execute(
        """
        SELECT marka, model, SUM(n_vehicles) AS total
        FROM agg_vehicle_counts
        GROUP BY 1, 2
        HAVING total >= ?
        ORDER BY marka, model
        """,
        [MIN_VEHICLES_FOR_PAGE],
    ).fetchall()

    built = 0
    built_woj = 0
    built_powiat = 0
    skipped_thin = con.execute(
        "SELECT COUNT(*) FROM (SELECT marka, model FROM agg_vehicle_counts GROUP BY 1, 2 HAVING SUM(n_vehicles) < ?)",
        [MIN_VEHICLES_FOR_PAGE],
    ).fetchone()[0]

    skipped_bad_slug = 0
    for marka, model, total in models:
        if not marka or not model:
            skipped_bad_slug += 1
            continue
        marka_slug, model_slug = slugify(marka), slugify(model)
        if not marka_slug or not model_slug:
            # A handful of garbage "model" values (e.g. '?', '/', '-', '???')
            # slugify to an empty string - skip rather than generate a
            # malformed URL. Found via a real broken-link scan; see NOTES.md.
            skipped_bad_slug += 1
            continue

        by_year = con.execute(
            """
            SELECT rok_produkcji, SUM(n_vehicles) AS n_vehicles
            FROM agg_vehicle_counts WHERE marka = ? AND model = ?
            GROUP BY 1 ORDER BY 1
            """,
            [marka, model],
        ).fetchall()

        by_woj = con.execute(
            """
            SELECT wojewodztwo, SUM(n_vehicles) AS n_vehicles
            FROM agg_vehicle_counts WHERE marka = ? AND model = ?
            GROUP BY 1 ORDER BY 2 DESC
            """,
            [marka, model],
        ).fetchall()
        woj_pages = {w for (w, n) in by_woj if n >= MIN_VEHICLES_FOR_PAGE}

        age_row = con.execute(
            "SELECT median_age_years, mean_age_years FROM agg_age_by_model WHERE marka = ? AND model = ?",
            [marka, model],
        ).fetchone()
        median_age, mean_age = age_row if age_row else (None, None)

        related_models = con.execute(
            """
            SELECT model, SUM(n_vehicles) AS n_vehicles
            FROM agg_vehicle_counts WHERE marka = ? AND model != ?
            GROUP BY 1 HAVING n_vehicles >= ? ORDER BY 2 DESC LIMIT 15
            """,
            [marka, model, MIN_VEHICLES_FOR_PAGE],
        ).fetchall()

        url_path = f"model/{marka_slug}/{model_slug}/"
        context = {
            "title": f"{marka} {model} — ile jeździ w Polsce? Statystyki CEPiK",
            "description": f"Ile {marka} {model} jest zarejestrowanych w Polsce? Rozkład roku produkcji, wiek pojazdów i popularność w województwach.",
            "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
            "root": "../../..",
            "data_updated_at": data_updated_at,
            "marka": marka, "model": model, "marka_slug": marka_slug, "model_slug": model_slug,
            "total": total, "median_age": median_age, "mean_age": mean_age,
            "by_year": [{"rok_produkcji": y, "n_vehicles": n} for y, n in by_year],
            "chart_years": [y for y, n in by_year],
            "chart_counts": [n for y, n in by_year],
            "by_wojewodztwo": [
                {"wojewodztwo": w, "wojewodztwo_slug": slugify(w), "n_vehicles": n, "has_page": w in woj_pages}
                for w, n in by_woj
            ],
            "related_models": [{"model": m, "model_slug": slugify(m), "n_vehicles": n} for m, n in related_models],
        }
        render_page(env, "model.html", SITE_DIR / url_path / "index.html", context)
        built += 1

        for wojewodztwo, n_in_woj in by_woj:
            if n_in_woj < MIN_VEHICLES_FOR_PAGE:
                continue
            n_powiat = build_model_wojewodztwo_page(
                env, con, data_updated_at, marka, model, marka_slug, model_slug, wojewodztwo, n_in_woj
            )
            built_woj += 1
            built_powiat += n_powiat

    if skipped_bad_slug:
        print(f"  skipped {skipped_bad_slug} marka/model combos with an empty/missing slug (garbage data, e.g. '?', '/')")
    return built, built_woj, built_powiat, skipped_thin


def build_model_wojewodztwo_page(env, con, data_updated_at, marka, model, marka_slug, model_slug, wojewodztwo, total_in_woj) -> int:
    """model x voivodeship intersection - the brief's own worked example
    (.../malopolskie/). Returns how many model x powiat pages it built below it."""
    woj_slug = slugify(wojewodztwo)
    phrase = wojewodztwo_phrase(wojewodztwo)

    by_year = con.execute(
        """
        SELECT rok_produkcji, SUM(n_vehicles) AS n_vehicles FROM agg_vehicle_counts
        WHERE marka = ? AND model = ? AND wojewodztwo = ? GROUP BY 1 ORDER BY 1
        """,
        [marka, model, wojewodztwo],
    ).fetchall()

    # No LIMIT here on purpose: this result decides which model x powiat
    # pages actually get built below, so it must include every powiat with
    # enough vehicles - not just the top N. (A wojewodztwo has at most a few
    # dozen powiaty, so this is cheap regardless.) The HTML table only shows
    # the top TOP_N_POWIATY_PER_MODEL of these for readability - a separate,
    # purely cosmetic truncation applied when building the template context.
    # A previous version used one LIMIT'd query for both purposes, which
    # made the powiat.html "top models" links point at pages that were never
    # built - caught by a full link-integrity scan (see NOTES.md).
    all_by_powiat = con.execute(
        """
        SELECT powiat, SUM(n_vehicles) AS n_vehicles
        FROM agg_vehicle_counts WHERE marka = ? AND model = ? AND wojewodztwo = ? AND powiat != 'xxxxxxxxx'
        GROUP BY 1 ORDER BY 2 DESC
        """,
        [marka, model, wojewodztwo],
    ).fetchall()
    powiat_pages = {p for (p, n) in all_by_powiat if n >= MIN_VEHICLES_FOR_PAGE}
    by_powiat = all_by_powiat[:TOP_N_POWIATY_PER_MODEL]

    rank_row = con.execute(
        "SELECT rank FROM agg_ranking_model_wojewodztwo WHERE wojewodztwo = ? AND marka = ? AND model = ?",
        [wojewodztwo, marka, model],
    ).fetchone()
    rank = rank_row[0] if rank_row else None

    age_row = con.execute(
        "SELECT median_age_years, mean_age_years FROM agg_age_by_model_wojewodztwo WHERE wojewodztwo = ? AND marka = ? AND model = ?",
        [wojewodztwo, marka, model],
    ).fetchone()
    median_age, mean_age = age_row if age_row else (None, None)

    url_path = f"model/{marka_slug}/{model_slug}/{woj_slug}/"
    context = {
        "title": f"{marka} {model} {phrase} — statystyki CEPiK",
        "description": f"Ile {marka} {model} jest zarejestrowanych {phrase}? Rozkład roku produkcji i ranking popularności w powiatach.",
        "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
        "root": "../../../..",
        "data_updated_at": data_updated_at,
        "marka": marka, "model": model, "marka_slug": marka_slug, "model_slug": model_slug,
        "wojewodztwo": wojewodztwo, "wojewodztwo_slug": woj_slug,
        "w_wojewodztwie": phrase, "w_wojewodztwie_cap": capitalize_first(phrase),
        "total": total_in_woj, "rank": rank, "median_age": median_age, "mean_age": mean_age,
        "by_year": [{"rok_produkcji": y, "n_vehicles": n} for y, n in by_year],
        "by_powiat": [
            {"powiat": p, "powiat_slug": slugify(p), "n_vehicles": n, "has_page": p in powiat_pages}
            for p, n in by_powiat
        ],
    }
    render_page(env, "model_wojewodztwo.html", SITE_DIR / url_path / "index.html", context)

    built_powiat = 0
    for powiat, n_in_powiat in all_by_powiat:
        if n_in_powiat < MIN_VEHICLES_FOR_PAGE:
            continue
        build_model_powiat_page(
            env, con, data_updated_at, marka, model, marka_slug, model_slug,
            wojewodztwo, woj_slug, powiat, n_in_powiat,
        )
        built_powiat += 1
    return built_powiat


def build_model_powiat_page(env, con, data_updated_at, marka, model, marka_slug, model_slug, wojewodztwo, woj_slug, powiat, total_in_powiat) -> None:
    powiat_slug = slugify(powiat)

    by_year = con.execute(
        """
        SELECT rok_produkcji, n_vehicles FROM agg_vehicle_counts
        WHERE marka = ? AND model = ? AND wojewodztwo = ? AND powiat = ? ORDER BY rok_produkcji
        """,
        [marka, model, wojewodztwo, powiat],
    ).fetchall()

    rank_row = con.execute(
        "SELECT rank FROM agg_ranking_model_powiat WHERE wojewodztwo = ? AND powiat = ? AND marka = ? AND model = ?",
        [wojewodztwo, powiat, marka, model],
    ).fetchone()
    rank = rank_row[0] if rank_row else None

    url_path = f"model/{marka_slug}/{model_slug}/{woj_slug}/{powiat_slug}/"
    phrase = powiat_phrase(powiat)
    context = {
        "title": f"{marka} {model} {phrase} — statystyki CEPiK",
        "description": f"Ile {marka} {model} jest zarejestrowanych {phrase}? Rozkład roku produkcji i ranking popularności.",
        "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
        "root": "../../../../..",
        "data_updated_at": data_updated_at,
        "marka": marka, "model": model, "marka_slug": marka_slug, "model_slug": model_slug,
        "wojewodztwo": wojewodztwo, "wojewodztwo_slug": woj_slug,
        "powiat": powiat, "powiat_slug": powiat_slug,
        "w_powiecie": phrase, "w_powiecie_cap": capitalize_first(phrase),
        "total": total_in_powiat, "rank": rank,
        "by_year": [{"rok_produkcji": y, "n_vehicles": n} for y, n in by_year],
    }
    render_page(env, "model_powiat.html", SITE_DIR / url_path / "index.html", context)


def build_make_pages(env: Environment, con: duckdb.DuckDBPyConnection, data_updated_at: str) -> int:
    """One page per make, listing its models - exists mainly so the homepage's
    top-makes links (and model.html's breadcrumb) have somewhere real to go,
    not a page type called for in the brief's URL examples."""
    makes = con.execute(
        """
        SELECT marka, SUM(n_vehicles) AS total
        FROM agg_vehicle_counts GROUP BY 1 HAVING total >= ? ORDER BY marka
        """,
        [MIN_VEHICLES_FOR_PAGE],
    ).fetchall()

    built = 0
    for marka, total in makes:
        if not marka:
            continue
        marka_slug = slugify(marka)
        if not marka_slug:
            continue
        models = con.execute(
            """
            SELECT model, SUM(n_vehicles) AS n_vehicles
            FROM agg_vehicle_counts WHERE marka = ?
            GROUP BY 1 ORDER BY 2 DESC
            """,
            [marka],
        ).fetchall()

        url_path = f"model/{marka_slug}/"
        context = {
            "title": f"{marka} — modele i statystyki w Polsce",
            "description": f"Ile samochodów {marka} jest zarejestrowanych w Polsce? Lista modeli wraz z liczbą pojazdów.",
            "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
            "root": "../..",
            "data_updated_at": data_updated_at,
            "marka": marka, "marka_slug": marka_slug, "total": total,
            "models": [
                {"model": m, "model_slug": slugify(m), "n_vehicles": n, "has_page": n >= MIN_VEHICLES_FOR_PAGE and bool(slugify(m))}
                for m, n in models
            ],
        }
        render_page(env, "marka.html", SITE_DIR / url_path / "index.html", context)
        built += 1
    return built


# Known county-data gaps, by voivodeship - see NOTES.md. Only verified for
# Malopolskie so far; other voivodeships may have analogous gaps that
# haven't been investigated (flagged in NOTES.md as a follow-up, not
# something to silently assume doesn't exist elsewhere).
MISSING_COUNTY_NOTES = {
    ("MAŁOPOLSKIE", "KRAKÓW"): "krakowskiego",
    ("MAŁOPOLSKIE", "TARNÓW"): "tarnowskiego",
    ("MAŁOPOLSKIE", "NOWY SĄCZ"): "nowosądeckiego",
}


def build_powiat_pages(env: Environment, con: duckdb.DuckDBPyConnection, data_updated_at: str, powiaty_by_woj: dict[str, list[str]]) -> int:
    built = 0
    for wojewodztwo, powiaty in powiaty_by_woj.items():
        woj_slug = slugify(wojewodztwo)
        for powiat in powiaty:
            total = con.execute(
                "SELECT SUM(n_vehicles) FROM agg_vehicle_counts WHERE wojewodztwo = ? AND powiat = ?",
                [wojewodztwo, powiat],
            ).fetchone()[0]
            if not total or total < MIN_VEHICLES_FOR_PAGE:
                continue

            age_row = con.execute(
                "SELECT median_age_years, mean_age_years FROM agg_age_by_powiat WHERE wojewodztwo = ? AND powiat = ?",
                [wojewodztwo, powiat],
            ).fetchone()
            median_age, mean_age = age_row if age_row else (None, None)

            top_makes = con.execute(
                f"SELECT marka, n_vehicles, rank FROM agg_ranking_marka_powiat WHERE wojewodztwo = ? AND powiat = ? ORDER BY rank LIMIT {TOP_N_PER_POWIAT}",
                [wojewodztwo, powiat],
            ).fetchall()
            top_models = con.execute(
                f"SELECT marka, model, n_vehicles, rank FROM agg_ranking_model_powiat WHERE wojewodztwo = ? AND powiat = ? ORDER BY rank LIMIT {TOP_N_PER_POWIAT}",
                [wojewodztwo, powiat],
            ).fetchall()
            model_has_page = {(m, mo) for m, mo, n, r in top_models if n >= MIN_VEHICLES_FOR_PAGE and slugify(m) and slugify(mo)}

            powiat_slug = slugify(powiat)
            phrase = powiat_phrase(powiat)
            other_powiaty = [p for p in powiaty if p != powiat]

            missing = MISSING_COUNTY_NOTES.get((wojewodztwo, powiat))
            note = None
            if missing:
                note = (
                    f"Uwaga: w danych CEPiK dla tego powiatu mogą być ujęte również "
                    f"pojazdy z sąsiedniego powiatu {missing} (ziemskiego) — ten "
                    f"powiat nie występuje osobno w udostępnionych danych. Zobacz NOTES.md."
                )

            url_path = f"powiat/{woj_slug}/{powiat_slug}/"
            context = {
                "title": f"Najpopularniejsze samochody {phrase} — statystyki CEPiK",
                "description": f"Jakie marki i modele samochodów są najpopularniejsze {phrase}? Ranking na podstawie danych CEPiK.",
                "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
                "root": "../../..",
                "data_updated_at": data_updated_at,
                "nazwa": powiat, "odmiana": phrase, "w_powiecie": phrase, "w_powiecie_cap": capitalize_first(phrase),
                "powiat_slug": powiat_slug,
                "wojewodztwo": wojewodztwo, "wojewodztwo_slug": woj_slug,
                "total": total, "median_age": median_age, "mean_age": mean_age, "note": note,
                "top_makes": [{"rank": r, "marka": m, "marka_slug": slugify(m), "n_vehicles": n} for m, n, r in top_makes],
                "top_models": [
                    {"rank": r, "marka": m, "model": mo, "marka_slug": slugify(m), "model_slug": slugify(mo), "n_vehicles": n, "has_page": (m, mo) in model_has_page}
                    for m, mo, n, r in top_models
                ],
                "other_powiaty": [{"nazwa": p, "slug": slugify(p)} for p in other_powiaty],
            }
            render_page(env, "powiat.html", SITE_DIR / url_path / "index.html", context)
            built += 1
    return built


def build_wojewodztwo_pages(env: Environment, con: duckdb.DuckDBPyConnection, data_updated_at: str, wojewodztwa: list[str], powiaty_by_woj: dict[str, list[str]]) -> int:
    built = 0
    for wojewodztwo in wojewodztwa:
        row = con.execute(
            "SELECT total_active, n_powiats FROM agg_totals_by_wojewodztwo WHERE wojewodztwo = ?",
            [wojewodztwo],
        ).fetchone()
        total, n_powiats = row if row else (0, 0)
        if not total or total < MIN_VEHICLES_FOR_PAGE:
            continue

        age_row = con.execute("SELECT median_age_years, mean_age_years FROM agg_age_by_wojewodztwo WHERE wojewodztwo = ?", [wojewodztwo]).fetchone()
        median_age, mean_age = age_row if age_row else (None, None)

        top_makes = con.execute(
            f"SELECT marka, n_vehicles, rank FROM agg_ranking_marka_wojewodztwo WHERE wojewodztwo = ? ORDER BY rank LIMIT {TOP_N_PER_POWIAT}",
            [wojewodztwo],
        ).fetchall()
        top_models = con.execute(
            f"SELECT marka, model, n_vehicles, rank FROM agg_ranking_model_wojewodztwo WHERE wojewodztwo = ? ORDER BY rank LIMIT {TOP_N_PER_POWIAT}",
            [wojewodztwo],
        ).fetchall()
        model_has_page = {(m, mo) for m, mo, n, r in top_models if n >= MIN_VEHICLES_FOR_PAGE}

        woj_slug = slugify(wojewodztwo)
        phrase = wojewodztwo_phrase(wojewodztwo)
        powiaty = sorted(powiaty_by_woj.get(wojewodztwo, []))
        other_wojewodztwa = [w for w in wojewodztwa if w != wojewodztwo]

        url_path = f"wojewodztwo/{woj_slug}/"
        context = {
            "title": f"Najpopularniejsze samochody {phrase} — statystyki CEPiK",
            "description": f"Jakie marki i modele samochodów są najpopularniejsze {phrase}? Ranking na podstawie danych CEPiK.",
            "canonical_url": f"{SITE_BASE_URL}/{url_path}" if SITE_BASE_URL else None,
            "root": "../..",
            "data_updated_at": data_updated_at,
            "nazwa": wojewodztwo, "w_wojewodztwie": phrase, "w_wojewodztwie_cap": capitalize_first(phrase),
            "wojewodztwo_slug": woj_slug,
            "total": total, "n_powiats": n_powiats, "median_age": median_age, "mean_age": mean_age,
            "top_makes": [{"rank": r, "marka": m, "marka_slug": slugify(m), "n_vehicles": n} for m, n, r in top_makes],
            "top_models": [
                {"rank": r, "marka": m, "model": mo, "marka_slug": slugify(m), "model_slug": slugify(mo), "n_vehicles": n, "has_page": (m, mo) in model_has_page}
                for m, mo, n, r in top_models
            ],
            "powiaty": [{"nazwa": p, "slug": slugify(p)} for p in powiaty],
            "other_wojewodztwa": [{"nazwa": w, "slug": slugify(w)} for w in other_wojewodztwa],
        }
        render_page(env, "wojewodztwo.html", SITE_DIR / url_path / "index.html", context)
        built += 1
    return built


def build_sitemap_and_robots(all_urls: list[str]) -> None:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for url in all_urls:
        loc = f"{SITE_BASE_URL}/{url}" if SITE_BASE_URL else f"/{url}"
        lines.append(f"  <url><loc>{loc}</loc></url>")
    lines.append("</urlset>")
    (SITE_DIR / "sitemap.xml").write_text("\n".join(lines) + "\n", encoding="utf-8")

    robots = "User-agent: *\nAllow: /\nSitemap: " + (f"{SITE_BASE_URL}/sitemap.xml" if SITE_BASE_URL else "/sitemap.xml") + "\n"
    (SITE_DIR / "robots.txt").write_text(robots, encoding="utf-8")


def main() -> None:
    con = duckdb.connect(str(DB_PATH), read_only=True)
    env = make_env()
    data_updated_at = date.today().strftime("%d.%m.%Y")

    SITE_DIR.mkdir(parents=True, exist_ok=True)

    wojewodztwa = get_wojewodztwa(con)
    powiaty_by_woj = get_powiaty_by_wojewodztwo(con)
    print(f"wojewodztwa with data: {len(wojewodztwa)}, powiaty: {sum(len(v) for v in powiaty_by_woj.values())}")

    print("building index ...")
    build_index(env, con, data_updated_at, wojewodztwa)

    print("building model pages ...")
    built_models, built_model_woj, built_model_powiat, skipped_thin = build_model_pages(env, con, data_updated_at)
    print(
        f"  built {built_models} nationwide model pages, {built_model_woj} model-x-wojewodztwo pages, "
        f"{built_model_powiat} model-x-powiat pages, skipped {skipped_thin} thin models (<{MIN_VEHICLES_FOR_PAGE} vehicles)"
    )

    print("building make pages ...")
    built_makes = build_make_pages(env, con, data_updated_at)
    print(f"  built {built_makes} make pages")

    print("building wojewodztwo pages ...")
    built_woj = build_wojewodztwo_pages(env, con, data_updated_at, wojewodztwa, powiaty_by_woj)
    print(f"  built {built_woj} wojewodztwo pages")

    print("building powiat pages ...")
    built_powiaty = build_powiat_pages(env, con, data_updated_at, powiaty_by_woj)
    print(f"  built {built_powiaty} powiat pages")

    all_urls = ["", *[p.relative_to(SITE_DIR).parent.as_posix() + "/" for p in SITE_DIR.rglob("index.html") if p != SITE_DIR / "index.html"]]
    build_sitemap_and_robots(sorted(set(all_urls)))
    print(f"wrote sitemap.xml with {len(set(all_urls))} URLs, robots.txt")

    con.close()


if __name__ == "__main__":
    main()
