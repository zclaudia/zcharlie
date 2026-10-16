# AF-P1 synthetic browser evidence

These screenshots and browser checks use a disposable SQLite database, synthetic accounts, synthetic prices and synthetic exchange rules. The fixture starts the real FastAPI application and `ResearchWorker`; it does not connect to live providers or use a production database.

Run from the repository root:

```sh
tests/browser/run.sh account_fit.mjs
```

The run exercised real HTTP admission and worker completion for a candidate ranking with 105 synthetic screen members (100 hits and 5 truncated) and account fit flows at 1440px and 390px. It covered real page-two pagination, a delayed page-two response released after changing the financial-status filter, frozen ticker/name rendering, blocked and passing financial checks while the overall result remains incomplete, an explicit hold, unknown price/fee inputs, same-run recovery after reload before worker completion, hidden-to-visible polling recovery, storage failure before send, late context response rejection, a server-accepted request whose first response is deliberately aborted and recovered with the same body/UUID, a double-click producing one POST, a worker failure followed by explicit regeneration, and same-account regeneration after editing a fee. It also verified that form controls remain mounted during polling and that neither viewport scrolls horizontally.

The fixture seeds its ranking source rows directly as synthetic screen evidence in the temporary database; it does not run a market data provider or candidate-intake pipeline. The mobile fit screenshot shows both status lines, while the fixed bottom action bar overlaps some lower checks in the viewport. Desktop blocked/stale and mobile pass/incomplete are captured separately. The unrelated stock-search request can return 503 because the isolated fixture has no live search provider.

The ranking screenshots are captured after the actual ranking view has loaded its first 50 rows and the frozen ticker/name assertion passes; they show the result status, filter and rendered rows rather than the pre-submit form.

Screenshots: `synthetic-desktop-1440.png`, `synthetic-mobile-390.png`, `synthetic-ranking-desktop-1440.png`, and `synthetic-ranking-mobile-390.png`.

The 2026-10-16 implementation review added real 1440px recovery cases: native purpose input/change events during an accepted run, terminal fit GET returning one controlled 503 followed by same-result reload, a second sessionStorage write failure followed by explicit same-body prepared resend, a delayed accepted POST receipt after a form edit, and independent account/operation polling for both fit and ranking. The final overlap probe captures A's real queued GET response, submits B, then releases A while B is still queued; B must complete, A must remain recoverable in its own slot, and switching back must recover A's original operation. The common baseline flow and layout assertions run at both 1440px and 390px. These checks were independently rerun by the primary agent.

Generation-time and current evidence labels are independent. The synthetic rule injection used by the API differs from the default live-source reader used during worker completion in this fixture, so these labels can differ in the ranking screenshot; arithmetic consumes the frozen rule inputs. This fixture is not evidence of a configured production rule provider.

The targeted follow-up review also exercises delayed real account-context responses while editing fee fields in both viewports. The final 390px recovery block adds transient run-status GET retries, a permanent 404 that makes only one automatic request and then resumes the same run after reload, and a held terminal fit GET across A→B→A. After A recovers and submits a new operation, a controlled 503 for the old GET must neither replace the new pending slot nor show an obsolete retry control. Editing during that terminal GET must survive recovery as `stale_scenario`. The controlled failures and delays wrap real API calls; successful API responses and worker completion are not mocked. See the [follow-up review](../../plans/account-fit-p0-p1/followup-review-2026-10-16.md) for independent acceptance and its test timing boundaries.
