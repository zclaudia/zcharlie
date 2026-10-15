# Costco parent-equity evidence

This evidence resolves the historical scope mismatch for Costco. The selected FY2023 Form 10-K reports parent stockholders’ equity, noncontrolling interests (NCI), and total equity on separate rows in its consolidated balance sheet. The historical total-equity facts remain intact; parent equity is now supported by its own reported fact and can be used for the FY2023 opening balance and FY2023 close.

## Filed amounts and periods

| Balance date | Parent stockholders’ equity | NCI | Reported total equity | Reconciliation |
| --- | ---: | ---: | ---: | --- |
| 2022-08-28 | USD 20,642 million | USD 5 million | USD 20,647 million | 20,642 + 5 = 20,647 |
| 2023-09-03 | USD 25,058 million | USD 0 million (`—` in the filing) | USD 25,058 million | 25,058 + 0 = 25,058 |

The parent line is labelled “Total Costco stockholders’ equity” and carries `us-gaap:StockholdersEquity`. The NCI line is separately tagged `us-gaap:MinorityInterest`. “TOTAL EQUITY” is separately tagged `us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest`. These labels and tags establish the two scopes without deriving parent equity by subtraction.

The August 28, 2022 balance is the actual instant reported at the start of Costco FY2023, which ran August 29, 2022 through September 3, 2023. The September 3, 2023 close is the opening balance date for FY2024 (beginning September 4, 2023). Both are point-in-time USD facts with an inline XBRL scale of 6, or USD millions.

## SEC filing capture

The source is Costco Wholesale Corporation’s Form 10-K, accession `0000909832-23-000042`, filed October 11, 2023. Its balance-sheet instant contexts are `c-13` for August 28, 2022 and `c-12` for September 3, 2023. The reviewed source excerpts, XBRL identifiers, and bridge values are stored in [costco-parent-equity-reviewed-facts.json](../../../../tests/fixtures/materials/annual_validation/costco-parent-equity-reviewed-facts.json).

A fresh response was captured from the official SEC EDGAR archive at `2026-10-15T14:43:54Z`: HTTP 200, 1,502,730 bytes, SHA-256 `4ad0ed774dc46baca606e6d6db5f86d841211c3de97986861df71194e6bd8cc1`. The ignored raw capture and response metadata are under `data/hk2-latest-evidence/2026-10-15/raw/costco-equity/`. The reviewed JSON includes compact excerpts and the raw-document hash so its evidence test does not depend on checking the ignored 1.5 MB filing into source control.

## Integration contract

Use the two entries in `reviewed_parent_equity_rows` as parent-scope facts: FY2022 (`category=beginning_equity`, `beginning_of_year_for_fy=2023`, value `20,642`) and FY2023 (`category=equity`, value `25,058`). Preserve the prior FY2022 and FY2023 total-equity entries and their original `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` tag in the historical reviewed-facts record. The `reported_total_and_nci` entries retain both reconciliations and the source taxonomy for every component.

Focused tests in `tests/unit/test_costco_parent_equity_review.py` verify source excerpts, units, date contexts, parent-only tag selection, both reconciliations, rejection of mixed scope or period, and retention of the original total values. They run without reading the ignored raw SEC capture.
