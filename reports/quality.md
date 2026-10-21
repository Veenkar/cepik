# Data quality report

Generated: 2026-10-21


## Record counts

- Total rows in `vehicles_clean`: 22,687,093
- Flagged suspect (any reason): 10,421,398 (45.9%)

### Suspect reasons (a row may have more than one)

| reason | rows |
|---|---|
| wyrejestrowany | 5,635,753 |
| prawdopodobnie_martwa_dusza | 5,179,316 |
| marka_brak | 2,342,275 |
| rok_produkcji_nieliczbowy | 342,532 |
| brak_modelu | 205,978 |
| rok_produkcji_nieprawdopodobny | 6,265 |
| rok_produkcji_prawdopodobnie_nieznany | 5,284 |

## Make/model mapping coverage

- Distinct raw `marka` values still marked `needs_review`: 989
- Distinct raw `model` values still marked `needs_review`: 348,789

These are not counted as "suspect" rows - they're mapped to a best-effort normalized value automatically (see clean.py's first-token heuristic) and are safe to use, but a human should review `mappings/marka_mapping.csv` / `mappings/model_mapping.csv` and fix any that look wrong.

### Top 25 unmapped (model, needs_review) combinations by volume

| marka | model (raw) | rows |
|---|---|---|
| VOLKSWAGEN | GOLF | 515,336 |
| VOLKSWAGEN | PASSAT | 346,296 |
| OPEL | ASTRA | 333,219 |
| FSO-WARSZAWA | 125P 1.5 | 276,079 |
| POLSKI FIAT | 126 P 650 | 251,252 |
| FSO-WARSZAWA | POLONEZ 1.5 | 217,611 |
| FORD | FOCUS | 205,653 |
| SKODA | OCTAVIA | 201,513 |
| VOLKSWAGEN | POLO | 197,764 |
| OPEL | CORSA | 194,579 |
| AUDI | A4 | 191,715 |
| SKODA | FABIA | 191,060 |
| FORD | MONDEO | 179,707 |
| FORD | FIESTA | 165,154 |
| POLSKI FIAT | 125P 1500 | 164,752 |
| OPEL | VECTRA | 163,053 |
| TOYOTA | YARIS | 157,356 |
| RENAULT | CLIO | 156,586 |
| FIAT | PUNTO | 147,801 |
| RENAULT | MEGANE | 143,663 |
| TOYOTA | COROLLA | 116,861 |
| FORD | ESCORT | 105,783 |
| FIAT | SEICENTO | 102,103 |
| RENAULT | MEGANE SCENIC | 100,008 |
| SEAT | IBIZA | 96,315 |
