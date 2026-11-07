# Synthetic quality dataset v1.1

Version `research-quality.synthetic.v1` is invalid for numeric compatibility.
QT-003 found that its string-valued provider fields materialized as missing
values even though verification metadata said `verified`; the original numeric
reader correctly refused them. The former dataset hash was
`ab5fbe2890107cde909f2b73569d8ca6c72136a62d27ac3611ffe7b466ddfa8a`,
development hash `c7038318685533b0a5d11fa50e48c4a304cbbfcdf56e06742e8162048d752f04`,
holdout hash `da978935544cb68f6cecc1f65ff1b46d6e8988e4fd613cef353d299675e666ff`,
and index file SHA256 `48fb7e581f3fbfbb4c1a3acefdb6aab18f7a7916f39a53c17d6174ecfcf2219e`.
Those hashes are retained only as defect/audit identifiers, not usable material.

The v1.1 recipes use native strict integer source values, rejecting bool, float
and strings. Case UUIDs and snapshot IDs have new identities instead of silently
reusing immutable v1 references. The regression saves every non-insufficient
source through the original EvidenceService, requires `status=ok`, and uses the
actual NumericResolver to compare the known value with its exact source integer.
Every counterexample contains an actual synthetic negative operating-cash value.
The method package and its manifest are unchanged. This repair does not replace
the separate QT-003 original proposal/admission/settlement gate.

This directory contains twelve unexecuted synthetic case recipes: three fictional
companies, each with normal research, a prior-failure recipe, insufficient
material, and a counterexample. The index freezes four development cases and
eight holdout cases, their precise fixture-only refs, material mode, expectations,
repeat count, file SHA256 and typed canonical content hash.

Every case is explicitly `synthetic`, `fixture_only_not_persisted`, and `not_run`.
Material recipes reproduce the original snapshot materializer's EvidenceRecord;
the `verified` flag is synthetic source metadata, not a claim that production
documents were reviewed. QT-003 must persist and re-read the material through the
original services. Prior failures have no fabricated Task ref. Expectations are
not model answers, human scores, candidate wins, or real research success.

`holdout_use_count=0` is the initial count of research-evaluation runs, not a claim
that developers have never inspected fixture structure. Durable use/change/
contamination tracking is a later service responsibility. Updating a case or the
4/8 split requires a new reviewed dataset version and hashes; do not silently
refresh old evaluation inputs. There is no real predictive or financial-quality
evidence in this synthetic set.

`tests.support.quality_dataset.write_dataset()` is an explicit authoring helper;
ordinary tests only load/validate fixtures and regenerate expectations in memory.
The candidate method manifest keeps execution compatibility pending QT-003;
importing C1 does not move any default or per-company method head.
