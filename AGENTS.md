# AGENTS.md

## Project goal

Build a portfolio-ready MVP for automating document workflows:

PDF / DOCX / XLSX
→ classification
→ extraction
→ normalization
→ validation
→ needs_review
→ DOCX generation
→ provenance / audit.

The primary source of truth for project scope is PROJECT_BRIEF.md.

## Working rules

- Read PROJECT_BRIEF.md before making architectural decisions.
- Do not expand MVP scope without explicit approval.
- Do not invent missing business requirements.
- Prefer simple, testable architecture.
- Keep business logic independent from the LLM provider.
- Use Pydantic schemas for structured data.
- Missing or ambiguous data must produce needs_review.
- Do not hallucinate missing values.
- Preserve provenance for important extracted values.
- Add tests for every important workflow.
- Do not add MCP, multi-agent architecture, OCR, SAP/1C integrations,
  or enterprise RBAC unless explicitly requested.
- Use synthetic or anonymized demo data only.

## Development workflow

Before significant implementation:

1. Read PROJECT_BRIEF.md.
2. Read docs/DECISIONS.md if it exists.
3. Explain the proposed change briefly.
4. Implement the smallest useful increment.
5. Run relevant tests.
6. Report what changed, what was tested, and what remains uncertain.

## Definition of Done

A feature is not complete until:
- it works end-to-end;
- relevant tests pass;
- errors are handled;
- documentation is updated when needed;
- no secrets or sensitive data are committed.