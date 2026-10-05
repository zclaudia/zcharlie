# Golden case: 000858.SZ (五粮液)

Captured from the live ifind-proxy on 2026-10-05 (retrieved_at 2026-10-05T15:24:20.983676+00:00).

- provider_raw.json: sanitized raw proxy responses (ifind-raw.v1)
- pool.json / market.json / financial.json: canonical inputs (ifind-canonical.v1)
- golden_scores.json / golden_report_context.json: deterministic kernel
  output frozen as the offline golden expectation (scoring v1.2)

Sanitization: credential-bearing keys dropped, credential-shaped values
redacted; the full written payload was re-scanned against credential
patterns and the live key before this directory was accepted. Declared
sample traits: baseline.
