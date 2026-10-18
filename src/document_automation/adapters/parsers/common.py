"""Read-only bounded input and common fragment construction."""

import warnings
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from document_automation.contracts import InputDocument, SourceFragment
from document_automation.parsing import ParsedDocument, ParseDiagnostic, ParserLimits


class ParseFailure(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class Context:
    def __init__(self, document, limits):
        self.document = document
        self.limits = limits
        self.fragments = []
        self.diagnostics = []
        self.text_chars = 0
        self.blocks = 0

    def count(self):
        self.blocks += 1
        if self.blocks > self.limits.max_blocks:
            raise ParseFailure(
                "resource_limit", "Maximum structural block count exceeded."
            )

    def fragment(self, text, location, key):
        self.text_chars += len(text)
        if self.text_chars > self.limits.max_text_chars:
            raise ParseFailure(
                "resource_limit", "Maximum extracted text size exceeded."
            )
        if not text.strip():
            return None
        fragment_id = f"{self.document.document_id}:{key}"
        self.fragments.append(
            SourceFragment(
                fragment_id=fragment_id,
                document_id=self.document.document_id,
                text=text,
                location=location,
            )
        )
        return fragment_id

    def warn(self, code, message, location=None):
        self.diagnostics.append(
            ParseDiagnostic(
                code=code, severity="warning", message=message, location=location
            )
        )


def check_zip(data, fmt, limits):
    if data.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        encrypted = all(
            name.encode("utf-16-le") in data
            for name in ("EncryptedPackage", "EncryptionInfo")
        )
        raise ParseFailure(
            "encrypted_file" if encrypted else "unsupported_format",
            "Encrypted OOXML container is not supported."
            if encrypted
            else "Legacy or unknown OLE Office container is not supported.",
        )
    try:
        with ZipFile(BytesIO(data)) as archive:
            info = archive.infolist()
            if (
                len(info) > limits.max_zip_members
                or sum(i.file_size for i in info) > limits.max_zip_bytes
            ):
                raise ParseFailure(
                    "resource_limit", "OOXML expanded archive limit exceeded."
                )
            if any(i.flag_bits & 1 for i in info):
                raise ParseFailure(
                    "encrypted_file", "Encrypted ZIP members are not supported."
                )
            names = [i.filename for i in info]
            if len(names) != len(set(names)):
                raise ParseFailure(
                    "file_read_error", "Duplicate OOXML archive entries."
                )
            expected = "word/document.xml" if fmt == "docx" else "xl/workbook.xml"
            if expected not in names:
                other = "xl/workbook.xml" if fmt == "docx" else "word/document.xml"
                code = "unsupported_format" if other in names else "file_read_error"
                raise ParseFailure(
                    code, "OOXML structure does not match the requested format."
                )
    except BadZipFile as exc:
        raise ParseFailure("file_read_error", "Invalid OOXML ZIP container.") from exc


def parse_with(path, fmt, reader, limits=None):
    limits = limits or ParserLimits()
    path = Path(path)
    document = None
    try:
        if path.suffix.lower() != f".{fmt}":
            raise ParseFailure("unsupported_format", f"Expected .{fmt} input.")
        with path.open("rb") as stream:
            data = stream.read(limits.max_file_bytes + 1)
        if len(data) > limits.max_file_bytes:
            raise ParseFailure("resource_limit", "Maximum input file size exceeded.")
        digest = sha256(data).hexdigest()
        document = InputDocument(
            document_id=f"sha256:{digest}",
            filename=path.name,
            format=fmt,
            sha256=digest,
        )
        if not data:
            raise ParseFailure("empty_file", "Input file contains zero bytes.")
        if fmt != "pdf":
            check_zip(data, fmt, limits)
        elif not data.startswith(b"%PDF-"):
            raise ParseFailure(
                "unsupported_format", "Input does not have a PDF signature."
            )
        ctx = Context(document, limits)
        with warnings.catch_warnings(record=True) as library_warnings:
            warnings.simplefilter("always")
            blocks, sheets = reader(data, ctx)
        if library_warnings:
            ctx.warn(
                "unsupported_content",
                "Reader emitted warnings; some source content may be unsupported. Warning classes: "
                + ", ".join(sorted({w.category.__name__ for w in library_warnings})),
            )
        if not ctx.fragments:
            ctx.warn(
                "empty_content",
                "No usable non-whitespace text was extracted; inspect other diagnostics and preserved structure.",
            )
        return ParsedDocument(
            filename=path.name,
            document=document,
            status="completed",
            fragments=tuple(ctx.fragments),
            blocks=tuple(blocks),
            sheets=tuple(sheets),
            diagnostics=tuple(ctx.diagnostics),
        )
    except ParseFailure as exc:
        diagnostic = ParseDiagnostic(code=exc.code, severity="error", message=str(exc))
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        EOFError,
        RuntimeError,
        SyntaxError,
        BadZipFile,
    ) as exc:
        # No document text, absolute paths or provider secrets in diagnostics.
        diagnostic = ParseDiagnostic(
            code="file_read_error",
            severity="error",
            message=f"Cannot read {fmt}: {type(exc).__name__}.",
        )
    return ParsedDocument(
        filename=path.name or "input",
        document=document,
        status="failed",
        diagnostics=(diagnostic,),
    )
