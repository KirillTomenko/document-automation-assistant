"""Format dispatch only; no classification or business extraction."""

from pathlib import Path

from document_automation.parsing import ParsedDocument, ParseDiagnostic

from .docx import parse_docx
from .pdf import parse_pdf
from .xlsx import parse_xlsx

__all__ = ["parse_document", "parse_pdf", "parse_docx", "parse_xlsx"]


def parse_document(path, limits=None):
    path = Path(path)
    parser = {".pdf": parse_pdf, ".docx": parse_docx, ".xlsx": parse_xlsx}.get(
        path.suffix.lower()
    )
    if parser is None:
        return ParsedDocument(
            filename=path.name or "input",
            document=None,
            status="failed",
            diagnostics=(
                ParseDiagnostic(
                    code="unsupported_format",
                    severity="error",
                    message="Only PDF, DOCX and XLSX are supported.",
                ),
            ),
        )
    return parser(path, limits=limits)
