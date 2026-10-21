# Data quality report

Generated: 2026-10-21


## Record counts

- Total rows in `vehicles_clean`: 1,915,824
- Flagged suspect (any reason): 891,665 (46.5%)

### Suspect reasons (a row may have more than one)

| reason | rows |
|---|---|
| wyrejestrowany | 501,710 |
| prawdopodobnie_martwa_dusza | 395,679 |
| marka_brak | 182,530 |
| rok_produkcji_nieliczbowy | 30,632 |
| brak_modelu | 2,792 |
| rok_produkcji_nieprawdopodobny | 201 |
| rok_produkcji_prawdopodobnie_nieznany | 187 |

## Make/model mapping coverage

- Distinct raw `marka` values still marked `needs_review`: 665
- Distinct raw `model` values still marked `needs_review`: 67,922

These are not counted as "suspect" rows - they're mapped to a best-effort normalized value automatically (see clean.py's first-token heuristic) and are safe to use, but a human should review `mappings/marka_mapping.csv` / `mappings/model_mapping.csv` and fix any that look wrong.

### Top 25 unmapped (model, needs_review) combinations by volume

| marka | model (raw) | rows |
|---|---|---|
| FSO-WARSZAWA | 125P 1.5 | 42,388 |
| VOLKSWAGEN | GOLF | 37,735 |
| OPEL | ASTRA | 28,017 |
| FSO-WARSZAWA | POLONEZ 1.5 | 26,028 |
| VOLKSWAGEN | PASSAT | 21,323 |
| FORD | FOCUS | 18,938 |
| POLSKI FIAT | 126 P 650 | 18,223 |
| SKODA | FABIA | 18,208 |
| SKODA | OCTAVIA | 17,862 |
| OPEL | CORSA | 17,042 |
| AUDI | A4 | 16,269 |
| VOLKSWAGEN | POLO | 15,618 |
| FORD | FIESTA | 14,406 |
| FIAT | PUNTO | 14,389 |
| TOYOTA | YARIS | 12,984 |
| RENAULT | CLIO | 12,954 |
| FORD | MONDEO | 11,672 |
| RENAULT | MEGANE | 11,076 |
| SEAT | IBIZA | 10,896 |
| AUDI | A3 | 9,755 |
| FIAT | SEICENTO | 9,448 |
| SEAT | LEON | 9,368 |
| OPEL | VECTRA | 9,114 |
| FSO-WARSZAWA | POLONEZ CARO 1.6 | 9,003 |
| POLSKI FIAT | 125P 1500 | 8,997 |
