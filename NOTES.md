# CEPiK API — Phase 0 reconnaissance findings

Date of investigation: 2026-10-21. All findings below are empirical (curl / DuckDB
against live responses and a downloaded bulk file), not taken from CEPiK's own
documentation, which turned out to be thin and in places contradicted by what the
API actually returns.

## 0. Connectivity — TLS workaround required

`api.cepik.gov.pl` negotiates a DH key that modern OpenSSL (3.0+, default
`SECLEVEL=2`) refuses to accept:

```
curl: (35) OpenSSL/3.0.13: error:0A00018A:SSL routines::dh key too small
```

Workaround: lower the TLS security level for this host specifically
(`curl --ciphers "DEFAULT@SECLEVEL=1" ...`). `requests`/`httpx` use the same
OpenSSL under the hood, so `ingest.py` will need an explicit `SSLContext` /
`HTTPAdapter` with `SECLEVEL=1`, scoped to this one host — not a global
weakening. **Must re-verify this on Windows** (different OpenSSL/Schannel
build) before relying on it there, per the cross-platform rule.

## 1. `/slowniki` — dictionaries (only 6 exist)

| id | contents |
|---|---|
| `wojewodztwa` | voivodeship codes |
| `marka` | vehicle makes (7,922 raw entries, see below) |
| `rodzaj-pojazdu` | vehicle type (379 raw entries, see below) |
| `rodzaj-paliwa` | fuel type (12 entries, clean) |
| `pochodzenie-pojazdu` | vehicle origin / import status (10 entries, clean) |
| `sposob-produkcji` | "production method" — mixes `FABRYCZNY`/`SAM.` style values with what look like **years** (172 entries, murky, low priority) |

**No `powiaty` (county) dictionary and no `model` dictionary exist at all.**
Both are things the site needs — there is no canonical source for either;
county names and model strings can only be discovered from the vehicle data
itself, hand-cleaned.

### Voivodeship codes (confirms brief's example)

`wojewodztwa` dictionary, TERYT-style 2-digit codes:

```
02 DOLNOŚLĄSKIE        04 KUJAWSKO-POMORSKIE   06 LUBELSKIE
08 LUBUSKIE            10 ŁÓDZKIE              12 MAŁOPOLSKIE   <-- MVP
14 MAZOWIECKIE         16 OPOLSKIE             18 PODKARPACKIE
20 PODLASKIE           22 POMORSKIE            24 ŚLĄSKIE
26 ŚWIĘTOKRZYSKIE      28 WARMIŃSKO-MAZURSKIE  30 WIELKOPOLSKIE
32 ZACHODNIOPOMORSKIE  XX NIEOKREŚLONE (undetermined)
```

**Małopolskie = `12`.** (Brief's own example, 14 = Mazowieckie, is confirmed correct.)
Dictionary also reports `liczba-wystapien` (occurrence count) per voivodeship —
Małopolskie: 3,455,028 (all vehicle types, all time, nationwide dictionary
scope — not directly a "cars on the road" number).

### `marka` (make) dictionary is dirty at the source

7,922 raw entries. Canonical `NISSAN` has 710,808 occurrences, but sits next to
garbage entries like `NISSAN-MULTITEL PAGLIERO`, `NISSAN/CARGO-VIT`,
`NISSAN/KONAR`, `ZHENGZHOU-NISSAN` (1 occurrence each/few) — these look like
conversion/retrofit vehicle entries or data-entry noise, not real makes. The
make mapping table (Phase 2) needs to treat this dictionary as a starting
point, not ground truth.

### `rodzaj-pojazdu` (vehicle type) dictionary is *severely* dirty

379 raw entries for what should be a small, closed set. The canonical
passenger-car value `SAMOCHÓD OSOBOWY` (28,554,266 occurrences nationwide) has
at least 16 misspelled/garbled siblings, e.g.: `SAMOCHOD OSOBOWY`,
`SAMOCHAD OSOBOWY`, `SAMOCHED OSOBOWY`, `SAMOCHODY OSOBOWE`,
`SAMOCHUD CIEIAROWO-OSOBOWY`, `SAMOCHŁD CIAROWO-OSOBOWY`, etc. (sizes 2 to
188,976). **MVP filter for "passenger car" must not be a single exact-match
string** — needs an explicit allow-list of known variants (small, since the
canonical value dominates) rather than LIKE-matching, to avoid silently
pulling in e.g. `SAM.CIĘŻAROWO-OSOBOWY` (a cargo/passenger hybrid truck, not a
car).

## 2. `/pojazdy` — the live query API

Query shape: `?wojewodztwo=<code>&data-od=YYYYMMDD&data-do=YYYYMMDD` plus
optional flags. Defaults the API fills in silently if omitted (visible in the
echoed `meta.json-ld-id`):

- `typ-daty=1` — the date range filters on **first registration date in
  Poland** (`data-pierwszej-rejestracji-w-kraju`), not "last touched" or
  "currently valid as of". Other `typ-daty` values exist but weren't explored
  (low priority for MVP; would matter for re-registration/import analysis).
- `tylko-zarejestrowane=true` — **excludes vehicles that have since been
  formally deregistered** (confirmed empirically: with this flag, 0 records in
  a sample had `data-wyrejestrowania-pojazdu` populated; with
  `tylko-zarejestrowane=false`, most did). This is the main lever for
  "currently on the road" counts via the API.
- `pokaz-wszystkie-pola=false` — **hides 50+ fields by default**, including
  the ones the whole project depends on: `rejestracja-powiat`,
  `rejestracja-gmina`, `data-wyrejestrowania-pojazdu`,
  `przyczyna-wyrejestrowania-pojazdu`. **Must always pass
  `pokaz-wszystkie-pola=true`.**
- Pagination: `limit` (100 used in testing, works), `page`; response includes
  `links.next`/`links.last` and `meta.count`.

Full field schema (69 fields) fetched from
`https://api.cepik.gov.pl/www/schema/vehicle.json` — includes make/model/type,
engine/mass/emissions specs, current registration location
(gmina/powiat/wojewodztwo), **owner's residence location at gmina/powiat/
wojewodztwo granularity only** (no name, address, plate, or VIN anywhere —
consistent with CEPiK's "anonymised" claim), and deregistration date + reason.

### ⚠️ The API's result counts are not reproducible

Three back-to-back, functionally identical requests
(`wojewodztwo=12&data-od=20100101&data-do=20100110`, varying only an unrelated
flag or nothing at all) returned **`meta.count` = 21, then 945, then 1279**
within about a minute. This is not a pagination artifact — it's the same
narrow 10-day/one-voivodeship query reporting wildly different total record
counts on repeat. This looks like backend sharding/caching inconsistency
(the server sits behind an F5/BIG-IP load balancer routing to multiple pool
members — visible in the `Set-Cookie: BIGipServer...` header, with different
members observed across calls). **This is a serious reliability risk for any
ingestion strategy built directly on `/pojazdy`** — exhaustive paging of a
date range cannot currently be trusted to be complete or stable. Needs to
inform `run_all.py`'s "stop if count changes >±20%" safeguard, and probably
needs its own dedicated consistency check in `ingest.py` (e.g., re-fetch page
1's count a second time before trusting it).

### Rate limiting

Empirically, the limit is **concurrency-based**, not a simple request
counter: a request issued while a prior one hadn't fully round-tripped
produced `HTTP 429` with:

```json
{"errors":[{"error-code":"API-ERR-100011",
  "error-reason":"Aplikacja przetwarza maksymalną liczbę równoległych połączeń.",
  "error-solution":"Spróbuj za chwile."}]}
```

Recovered immediately after a few seconds' pause. The `X-Rate-Limit-Remaining:
19` response header stayed constant at 19 across every call in this session
(dozens of requests, serial) — either a very generous/irrelevant quota or a
different tier than what's actually throttling. **No evidence of a daily/
longer-term cap was found, but we only tested lightly; `ingest.py` should
assume one exists and use conservative serial requests with exponential
backoff on 429, plus a descriptive `User-Agent` with contact info as the brief
requires.**

## 3. `/pliki` — bulk files are stale and incomplete

`/pliki` lists 17 files (16 voivodeships + 1 "unknown voivodeship" bucket).
**Every single one is dated `2022-04-17`** — both in the listing's
`data-utworzenia-pliku` and the file's `Last-Modified` header — i.e. **~4.5
years old** as of today, despite the parent `/slowniki` metadata advertising
`sy:updatePeriod: weekly`. The file-list endpoint's own
`schema:dateModified` is `2019-05-13`. **The bulk-file feature appears
abandoned.** This directly contradicts the brief's assumption ("prefer bulk
files over paginating the API") — they cannot serve as the live/current data
source. They could still work as a one-time historical backfill, but every
day since 2022-04-17 would have to come from the (unreliable) `/pojazdy` API
regardless, so the benefit of using them at all is now in question — see
"Decision needed" below.

### Downloaded and inspected the Małopolskie file (`pojazdy_12_2022-04-17.zip`)

- 195 MB zipped → 985 MB CSV, single file inside.
- **UTF-8 encoded** (verified via real diacritic bytes, e.g. `ŁAD`, `CIĘŻ.`,
  `KRAKÓW-ŚRÓDMIEŚCIE` decode cleanly as UTF-8; not cp1250). One encoding risk
  removed, but `ingest.py` must still open everything with explicit
  `encoding="utf-8"` per project rules regardless of source.
- Comma-delimited, 71 columns, header row + 2,720,251 data rows (**all
  vehicle types**, not just cars).
- Of those, 1,905,100 rows are passenger cars (`rodzaj = 'SAMOCHÓD OSOBOWY'`
  exact match, canonical value only).
- Column names differ from the live API's field names (e.g. `akt_miejsce_rej_powiat`
  instead of `rejestracja-powiat`, `data_wyrejestrowania` instead of
  `data-wyrejestrowania-pojazdu`) — a mapping table between the two field
  naming schemes will be needed if both sources are ever combined.

### Deregistration field confirmed usable

Among the 1,905,100 Małopolskie passenger-car rows: **501,671 (26.3%)** already
have `data_wyrejestrowania` populated (i.e. formally deregistered — scrapped,
exported, etc., with `przyczyna_wyrejestrowania` giving a reason such as
`PRZEKAZANIE POJAZDU DO DEMONTAŻU` [handed over for dismantling]). **Filtering
`data_wyrejestrowania IS NULL`** is the correct, data-backed way to approximate
"currently registered" — but this only catches vehicles that went through
*formal* deregistration. It does **not** solve the "dead souls" problem
(vehicles physically gone but never formally deregistered) — that remains an
irreducible data quality issue per CEPiK's known limitation. We can only
mitigate it with plausibility heuristics (implausible production year, no
registration activity in decades), never fully solve it. **Every "vehicles
currently on the road" metric on the site needs a visible methodology note
saying so.**

### Production-year field has garbage worth flagging (not deleting)

- One record's `rok_produkcji` is the literal string **`ZABYTKOWY`** ("vintage/
  classic") — not a year at all.
- Implausible years present: `1069`, `1077`, `1084`, `1091`, `1096`, `1192`,
  `1771`, `1880`, `1881` (1-2 occurrences each — clearly fat-finger typos,
  e.g. transposed digits of a real year).
- **187 records at exactly `1900`** — far more than any other implausible year,
  strongly suggesting `1900` is a legacy "unknown year" sentinel value rather
  than 187 genuine century-old cars.
- → Phase 2 should flag (not drop) rows with `rok_produkcji` outside a sane
  range (e.g. before ~1920 or after current year + 1), and specifically flag
  `1900` with a distinct reason since it looks systemic rather than random.

**Update after running clean.py on the full ingested dataset**: non-numeric
`rok_produkcji` values are far more common than the single `ZABYTKOWY`
example found during this recon suggested — **30,632 rows (1.6% of all
passenger-car rows)** have a non-numeric value here. The actual value set is
`FABRYCZNY`, `SKŁADAK`, `PRODUKCJA JEDNOSTKOWA`, `ZABYTKOWY` — all of these
read like **`sposob_produkcji` (production method) values that ended up in
the `rok_produkcji` field instead**, for vehicles where an exact production
year apparently doesn't cleanly apply (kit-built, one-off/custom-built,
vintage). This lines up with the `sposob-produkcji` dictionary weirdness
noted above (that dictionary mixes `FABRYCZNY`-style values with literal
years) - it looks like CEPiK's schema lets either field hold either kind of
value for these edge cases, not a random data-entry error. clean.py flags
these as `rok_produkcji_nieliczbowy` and excludes them from any year/age
based metric; they're real vehicles and still counted in total fleet size.

### Model field is mostly clean, make/model variants are not

- `model` is blank/`---` for only ~0.07% of passenger-car rows — good.
- 363 distinct `marka` values just among Małopolskie passenger cars (vs 7,922
  nationwide) — still needs the hand-maintained mapping table the brief
  specifies.
- Worked example from the brief itself — **Nissan Note**, Małopolskie only:
  17 distinct raw `model` strings referring to the same car: `NOTE`,
  `NOTE 1.5 DCI`, `NISSAN NOTE`, `NOTE 1.4 KAT`, `NOTE 1,5 DCI`, `E11 NOTE`,
  `NOTE TEKNA`, `NOTE 1.6 KAT.`, etc. **Also one genuine false-positive trap**:
  `VERSA NOTE S` (1 row) — that's a *different* Nissan model (Versa Note), which
  a naive `model LIKE '%NOTE%'` match would wrongly fold into Note's count.
  Confirms the brief's instinct: the mapping table needs exact-string entries,
  not substring/fuzzy matching.
- Currently-active (non-deregistered) Nissan Note count in Małopolskie, after
  correctly excluding Versa Note: **1,405** vehicles.

### County (powiat) data has a real gap — needs a decision

Distinct `akt_miejsce_rej_powiat` values for Małopolskie passenger cars: only
**20** — 19 named counties plus a literal placeholder `xxxxxxxxx` (14,301
rows, undetermined). Małopolskie officially has **22** powiats (19 land
counties + 3 city counties: Kraków, Tarnów, Nowy Sącz).

**Three land counties are completely absent — `krakowski`, `nowosądecki`,
`tarnowski`** — not just rare, but zero rows, across all 2.72M records in the
bulk file *and* in a fresh 2023 sample pulled live from `/pojazdy` just now.
Checked explicitly: known towns in those land counties (Skawina, Zabierzów,
Krzeszowice, Liszki — all in powiat krakowski) do not appear under any `gmina`
value anywhere in the data. All three missing counties happen to share a name
root with a city county (Kraków/Nowy Sącz/Tarnów) — suggesting their vehicles
may be structurally attributed to the city's registration authority in this
field, rather than being a one-off export bug. Root cause wasn't confirmed
(would need either CEPiK support contact or deeper probing we didn't spend
rate-limit budget on), but the symptom is solid and reproducible from two
independent sources.

**This needs a decision before Phase 4 URL design**: do we (a) publish 19
county pages and silently fold the missing 3 into Kraków/Tarnów/Nowy Sącz
pages with a methodology footnote, (b) publish all 22 official powiat names
but show "no data available" for the 3 affected ones, or (c) investigate
further first? Recommend (a) for MVP with a clear footnote — matches what the
data actually supports — but flagging for your call since it affects the
county URL list.

## 4. Privacy check

No field anywhere in the 69-field schema or the bulk CSV contains a name,
address, registration plate, or VIN. Owner-location fields exist but are at
voivodeship/powiat/gmina granularity only (and were `null` for every record in
our live-API sample, populated only in the older bulk file at the same coarse
granularity). Consistent with CEPiK's "anonymised" claim. The brief's
"skip pages with <10 vehicles" rule naturally guards against re-identification
via rare make/model × small-gmina combinations, but should be applied as a
general floor on *any* published aggregate, not just model pages.

### Another bulk-file quirk found while building the pipeline: `marka` is sometimes NULL with the make folded into `model`

~141,000 Małopolskie passenger-car rows (7.4% of the active fleet) have
`marka IS NULL` while `model` holds a full descriptive string that includes
the make, e.g. `model = 'POLSKI FIAT 126P 600'`. This is concentrated in
older/domestic vehicles (Polski Fiat, FSO) going by a manual sample - looks
like a historical data-entry convention for cars that predate CEPiK's
current make/model split, not random corruption. clean.py flags these with
a new suspect reason, `marka_brak`, and excludes them from every make/model
grouped table (agg_vehicle_counts, rankings, age-by-model) since they can't
be attributed to a make - but they're still counted in the site's overall
"total active vehicles" figure, with a footnote, since excluding them there
too would understate the fleet size by a meaningful amount.

## Open questions / decisions needed before Phase 1

1. **Primary ingestion source.** Given `/pliki` is ~4.5 years stale and
   missing 3 counties' worth of data, and `/pojazdy` gives inconsistent counts
   on repeat queries — neither source is clean. Proposed approach: use the
   stale bulk file as a one-time historical seed (filtered to passenger cars,
   deregistration-aware) *only if* its 2022 snapshot is good enough for
   "production year distribution" style stats where recency matters less, and
   pull everything post-2022 (and ideally a reconciliation sample of pre-2022
   too) from `/pojazdy` with careful retry/backoff and a stability check
   (re-querying counts before trusting them). Open to other approaches if you
   see a better one — wanted your sign-off before writing `ingest.py` since
   this is the central architectural choice for the whole pipeline.
2. **Missing counties (krakowski/nowosądecki/tarnowski)** — see above, needs a
   call on how to present this on `/powiat/` pages.
3. **`sposob-produkcji` weirdness** (years mixed into a "production method"
   field) — low priority, not blocking, flagged for awareness only.

## Addendum: expansion to all 16 voivodeships (2026-10-21)

After the Małopolskie MVP was working end-to-end, scope was expanded to
nationwide coverage. Key decisions, consistent with the approach above:

- **Data**: all 16 voivodeships bulk-seeded from `/pliki` (same stale
  2022-04-17 snapshot, same passenger-car filter). The live `/pojazdy`
  incremental refresh stays scoped to Małopolskie only
  (`API_INCREMENTAL_VOIVODESHIP` in `run_all.py`) — repeating the earlier
  decision to not hammer the rate-limited API 16x on every pipeline run.
  The other 15 voivodeships' data stays at the 2022-04-17 bulk snapshot
  until someone deliberately runs `ingest.py --voivodeship <code>
  --api-from ... --api-to ...` for them.
- **URL structure**: adopted the brief's own original example literally -
  `/model/<marka>/<model>/` is now nationwide, `/model/<marka>/<model>/
  <wojewodztwo>/` is the voivodeship intersection (matches `.../malopolskie/`
  from the brief), and `/model/<marka>/<model>/<wojewodztwo>/<powiat>/` goes
  one level deeper. `/powiat/` pages are namespaced under their voivodeship
  (`/powiat/<wojewodztwo>/<powiat>/`) since powiat names aren't guaranteed
  globally unique and the real administrative hierarchy nests them that way
  anyway. New `/wojewodztwo/<slug>/` page type added for per-region
  collective stats (wasn't in the brief's examples, but directly serves the
  "collective stats" ask).
- **Known gap not yet re-verified nationwide**: the three missing Małopolskie
  land counties (krakowski/nowosądecki/tarnowski) were confirmed for that
  one voivodeship specifically. Whether other voivodeships have an
  analogous gap (a land county's vehicles folding into its namesake city
  county) has **not** been checked yet - `build_site.py`'s
  `MISSING_COUNTY_NOTES` only has entries for Małopolskie. Worth a
  dedicated pass if this matters (e.g. before publishing).
