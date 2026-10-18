from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PyPdfError

from document_automation.contracts import PdfLocation
from document_automation.parsing import ParsedBlock

from .common import ParseFailure, parse_with


def _read(data, ctx):
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ParseFailure(
                "encrypted_file",
                "Encrypted PDFs are not supported, including empty-password encryption.",
            )
        if len(reader.pages) > ctx.limits.max_pdf_pages:
            raise ParseFailure("resource_limit", "PDF page limit exceeded.")
        blocks = []
        for index, page in enumerate(reader.pages, 1):
            ctx.count()
            location = PdfLocation(page=index)
            content = page.get_contents()
            if (
                content is not None
                and len(content.get_data()) > ctx.limits.max_pdf_stream_bytes
            ):
                raise ParseFailure(
                    "resource_limit", "Decoded PDF page stream limit exceeded."
                )
            text = page.extract_text() or ""
            fragment_id = ctx.fragment(text, location, f"pdf/page/{index}")
            blocks.append(
                ParsedBlock(
                    kind="page", location=location, text=text, fragment_id=fragment_id
                )
            )
            if not text.strip():
                ctx.warn(
                    "no_text",
                    "Page has no extractable text; it may be blank or image-only. OCR is not performed.",
                    location,
                )
            resources = page.get("/Resources", {})
            if hasattr(resources, "get_object"):
                resources = resources.get_object()
            if resources.get("/XObject"):
                ctx.warn(
                    "unsupported_content",
                    "Page contains graphics/Form objects; their visual content is not reconstructed.",
                    location,
                )
            if page.get("/Annots"):
                ctx.warn(
                    "unsupported_content",
                    "PDF annotations/form fields are not extracted separately.",
                    location,
                )
        return blocks, []
    except PyPdfError as exc:
        raise ParseFailure(
            "file_read_error", f"Invalid PDF: {type(exc).__name__}."
        ) from exc


def parse_pdf(path, limits=None):
    return parse_with(path, "pdf", _read, limits)
