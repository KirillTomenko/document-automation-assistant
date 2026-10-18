from io import BytesIO
from zipfile import ZipFile

from docx import Document
from docx.text.paragraph import Paragraph

from document_automation.contracts import DocxLocation
from document_automation.parsing import ParsedBlock, ParsedCell, ParsedRow, ParsedTable

from .common import ParseFailure, parse_with


def _read(data, ctx):
    document = Document(BytesIO(data))
    if document.element.body is None:
        raise ParseFailure("file_read_error", "DOCX document has no body.")
    # Explicitly report content skipped by the high-level body iterator.
    excluded = {
        "drawing",
        "pict",
        "object",
        "fldSimple",
        "instrText",
        "ins",
        "del",
        "sdt",
        "altChunk",
    }
    found = sorted(
        {el.tag.rsplit("}", 1)[-1] for el in document.element.iter()} & excluded
    )
    if found:
        ctx.warn(
            "unsupported_content",
            "DOCX body contains unsupported constructs: "
            + ", ".join(found)
            + ". Supported surrounding text is retained.",
        )
    with ZipFile(BytesIO(data)) as archive:
        parts = [
            n
            for n in archive.namelist()
            if n.startswith(
                (
                    "word/header",
                    "word/footer",
                    "word/footnotes",
                    "word/endnotes",
                    "word/comments",
                    "word/embeddings",
                )
            )
        ]
    if parts:
        ctx.warn(
            "unsupported_content",
            "Headers, footers, notes, comments or embedded parts are present and are not extracted.",
        )
    table_index = 0

    def walk(container, depth=0, parent=None):
        nonlocal table_index
        blocks = []
        paragraph_index = 0
        for item in container.iter_inner_content():
            ctx.count()
            if isinstance(item, Paragraph):
                paragraph_index += 1
                location = DocxLocation(paragraph=paragraph_index, **(parent or {}))
                text = item.text
                prefix = (
                    f"table/{parent['table']}/row/{parent['row']}/column/{parent['column']}"
                    if parent
                    else "body"
                )
                key = f"docx/{prefix}/paragraph/{paragraph_index}"
                fid = ctx.fragment(text, location, key)
                blocks.append(
                    ParsedBlock(
                        kind="paragraph", location=location, text=text, fragment_id=fid
                    )
                )
                continue
            if depth >= ctx.limits.max_table_depth:
                raise ParseFailure(
                    "resource_limit", "DOCX nested table depth exceeded."
                )
            table_index += 1
            number = table_index
            rows = []
            seen = {}
            for row_index, row in enumerate(item.rows, 1):
                cells = []
                for column, cell in enumerate(row.cells, row.grid_cols_before + 1):
                    ctx.count()
                    loc = DocxLocation(table=number, row=row_index, column=column)
                    # Merged cells are repeated by python-docx's grid API. Keep one copy.
                    if cell._tc in seen:
                        anchor = seen[cell._tc]
                        ctx.warn(
                            "unsupported_content",
                            "Merged DOCX grid cell aliases "
                            + anchor
                            + "; text is preserved once at its anchor.",
                            loc,
                        )
                        cells.append(
                            ParsedCell(location=loc, usable=False, merged_range=anchor)
                        )
                    else:
                        seen[cell._tc] = (
                            f"table/{number}/row/{row_index}/column/{column}"
                        )
                        cells.append(
                            ParsedCell(
                                location=loc,
                                blocks=tuple(
                                    walk(
                                        cell,
                                        depth + 1,
                                        {
                                            "table": number,
                                            "row": row_index,
                                            "column": column,
                                        },
                                    )
                                ),
                            )
                        )
                rows.append(ParsedRow(index=row_index, cells=tuple(cells)))
            table = ParsedTable(table_id=f"docx/table/{number}", rows=tuple(rows))
            blocks.append(
                ParsedBlock(
                    kind="table", location=DocxLocation(table=number), table=table
                )
            )
        return blocks

    return walk(document), []


def parse_docx(path, limits=None):
    return parse_with(path, "docx", _read, limits)
