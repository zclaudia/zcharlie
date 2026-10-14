"""One adapter per evidence source: re-derive, verify, and return what to save.

``EvidenceService.save_evidence`` is a receipt/dispatch shell; every source
branch lives here. Each module exposes the same entry point,
``materialize_expected(session, record) -> (record_to_save, actor)``:

* the submitted record is never trusted - the adapter re-materializes the
  facts from the stored immutable row and requires strict field equality
  before the submission may be saved (only the user-report upload, which is
  by definition unverified, skips re-derivation);
* the returned record is the one the shell persists, so submission-owned
  fields outside the compared witnesses (id, origin ref, correction refs)
  keep their existing meaning.
"""
