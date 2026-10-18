# Word artifacts

One completed `ProcessingResult` can produce two independently authorized artifacts.
`generate_document` creates the editable work request; `generate_verification_report`
creates the detailed source and validation report. Neither operation reruns extraction
or calls an LLM. Source uploads are never overwritten.

The work request contains only supported business fields, valid work-item values,
and a short Russian clarification list. It contains no audit, source coordinates,
or technical error codes. Missing and invalid values are left blank. Review drafts
are visibly marked. The existing substantive-content rule still blocks an empty
work request, while the report can be created from completed diagnostic results.

The verification report includes run identity, input files, processing and review
state, field values/states, readable problems, candidate snippets and coordinates,
and technical audit details in a separate final section. It states that provenance
shows origin and rule checks only; it does not establish factual truth.

Missing request title and completion date have editable blank lines, with explicit
instructions for manual completion or source reconciliation. Item clarification
messages identify the position number, confirmed name and field; invalid values
are quoted in those messages, never placed in reliable-value cells.

The evidence register prints each equal (document, location, text) tuple once as
«Источник N». Fields, candidates, classifications and issues refer to those entries.
Unverified claims have a separate register; all candidates, fragment IDs, normalized
interpretations and the complete audit snapshot remain. Unit aliases are limited to
the mappings in DEMO_CONTRACT.md, with original spelling retained in each candidate.

Each export requires actual bytes, matching SHA-256, run identity, immutable result
fingerprint, and full content comparison against its deterministic renderer.
Metadata alone never grants export. A failed processing result cannot generate either
artifact. SQLite stores the two artifact metadata records independently.

Tests cover A/B/C/V, empty results, failed processing, independent authorization and
exports, matching run IDs, evidence, tampering and review labels. Visual rendering is
performed with `render_docx.py` and LibreOffice when available; Microsoft Word itself
is not part of automated verification.
