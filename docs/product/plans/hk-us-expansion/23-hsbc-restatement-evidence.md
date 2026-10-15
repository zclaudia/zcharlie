# HSBC IFRS 4 / IFRS 17 opening-equity evidence

## Reviewed fact

For the FY2022 return-on-equity denominator, HSBC's 1 January 2022 ordinary shareholders' equity on the adopted IFRS 17 basis is **$166,601 million**. HSBC did not publish that figure as a separate ordinary-equity subtotal in its transition table; it is an exact derivation from the audited opening balance sheet: IFRS 17 total shareholders' equity of $189,015 million less unchanged other equity instruments of $22,414 million.

The original IFRS 4 closing balance remains **$175,836 million** at 31 December 2021. The IFRS 17 transition reduced ordinary shareholders' equity by **$9,235 million**, so the restated opening value is used for FY2022 while the contemporaneous FY2021 IFRS 4 value is retained as its own fact.

## Primary-source proof

HSBC's [Annual Report and Accounts 2023](https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2023/annual/pdfs/hsbc-holdings-plc/240226-annual-report-and-accounts-2023.pdf?download=1) states that IFRS 17 was adopted on 1 January 2023 and that comparatives were restated from the transition date, 1 January 2022. Note 38's transition table on PDF page 424 (printed page 422) reports opening total shareholders' equity of $198,250 million under IFRS 4 and $189,015 million under IFRS 17. Its transition columns give +$92,738m, +$4,558m, -$99,631m, -$8,847m and +$1,947m, which sum to -$9,235m and reconcile $198,250m to $189,015m.

The consolidated balance sheet in Note 38 on PDF page 427 (printed page 425) shows $22,414 million of other equity instruments under both IFRS 4 and IFRS 17 at 1 January 2022. The derived ordinary-equity balances are therefore:

| Basis at 1 January 2022 | Total shareholders' equity | Other equity instruments | Ordinary shareholders' equity |
| --- | ---: | ---: | ---: |
| IFRS 4, as originally reported | $198,250m | $(22,414)m | $175,836m |
| IFRS 17, restated opening balance | $189,015m | $(22,414)m | $166,601m |

The source's APM reconciliation on PDF page 135 (printed page 133) independently labels the FY2021 ordinary-equity row as $175,836 million and shows the two inputs: total shareholders' equity of $198,250 million and preference shares and other equity instruments of $(22,414) million. Its adjacent footnote says FY2021 remains on an IFRS 4 basis. This confirms both the old ordinary-equity fact and the scope of the subtraction.

HSBC's [official reporting index](https://www.hsbc.com/investors/results-and-announcements/all-reporting/group?page=1&reporting-type=annual&take=20&year=2024) dates the Annual Results 2023 documents to 21 February 2024 and lists the Annual Report and Accounts 2023. The report itself identifies it as the 2023 annual report and records Board approval on 21 February 2024.

## Frozen capture and reproduction

The reviewed report capture is `data/hk2-latest-evidence/2026-10-15/raw/hsbc-restatement/hsbc-annual-report-2023.pdf`, downloaded from the HSBC issuer URL above on 15 October 2026 at 14:44:20 UTC. The successful HTTP response was 200, the body was 7,034,549 bytes, and its SHA-256 is `2c03736c288399da08b665d72616e45b93c99764ec47891d9c2bc8fa5175c6c5`. The publication-index capture is beside it as `official-report-index-2023.html` with its own response sidecar.

Run `PYTHONPATH=src python tools/review_hsbc_restatement.py` to re-extract the cited report pages, validate both raw captures against their sidecars, and print the reviewed bridge JSON. `--write` recreates [the reviewed fixture](../../../../tests/fixtures/materials/annual_validation/hsbc-restatement-reviewed-facts.json). The fixture stores the original IFRS 4 fact, the restated IFRS 17 opening fact, the balance-sheet inputs, the full published transition bridge, exact source excerpts, source URL, publication evidence, capture timestamp, byte count and digest.

The FY2022 opening fact has `metric: equity_ordinary`, `year: 2022` and `period_end: 2021-12-31`; it is intended only as FY2022's `beginning_equity_fact`. It must not replace the FY2021 equity fact in the historical series. The transition bridge remains attached to the record so the distinction between contemporaneous IFRS 4 and retrospectively restated IFRS 17 values stays reviewable.
