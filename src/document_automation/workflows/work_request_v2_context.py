"""Finite, application-owned v2 structures. No model assertion creates evidence."""

import re

from openpyxl.utils.cell import column_index_from_string, coordinate_from_string

from document_automation.contracts import (
    ClassificationResult,
    ContractModel,
    PdfLocation,
    SourceFragment,
)
from document_automation.parsing import ParsedBlock, ParsedCell, ParsedDocument
from document_automation.workflows.work_request_context import (
    ALIASES,
    TABLE_ALIASES,
    _key,
)


class ValueEvidence(ContractModel):
    field: str
    raw_value: str
    source: SourceFragment
    cell: ParsedCell | None = None
    section: str | None = None


class RowEvidence(ContractModel):
    row_id: str
    values: tuple[ValueEvidence, ...]
    unsupported_fields: tuple[str, ...] = ()


class DocumentEvidence(ContractModel):
    parsed: ParsedDocument
    classifications: tuple[ClassificationResult, ...]
    values: tuple[ValueEvidence, ...]
    rows: tuple[RowEvidence, ...]
    unsupported: bool
    unsupported_sources: tuple[SourceFragment, ...] = ()


def pdf_lines(parsed):
    """Add exact, reproducible line/cell fragments only for v2; retain page evidence."""
    if parsed.document.format != "pdf":
        return parsed
    fragments, blocks = list(parsed.fragments), list(parsed.blocks)
    for page in parsed.blocks:
        for number, line in enumerate(page.text.splitlines(), 1):
            if not line.strip():
                continue
            entries = [(None, line)]
            delimiter = "|" if "|" in line else "\t" if "\t" in line else None
            if delimiter:
                cells = line.split(delimiter)
                if delimiter == "|":
                    if not cells[0].strip():
                        cells = cells[1:]
                    if not cells[-1].strip():
                        cells = cells[:-1]
                entries.extend(enumerate(cells, 1))
            for column, text in entries:
                text = text.strip() if column else text
                if not text.strip():
                    continue
                location = PdfLocation(
                    page=page.location.page, line=number, column=column
                )
                fid = f"{parsed.document.document_id}:v2/pdf/{location.page}/{number}/{column or 0}"
                fragments.append(
                    SourceFragment(
                        fragment_id=fid,
                        document_id=parsed.document.document_id,
                        text=text,
                        location=location,
                    )
                )
                blocks.append(
                    ParsedBlock(
                        kind="paragraph", location=location, text=text, fragment_id=fid
                    )
                )
    return ParsedDocument(
        **{
            **parsed.model_dump(),
            "fragments": tuple(fragments),
            "blocks": tuple(blocks),
        }
    )


def _scalar_boundary(cells):
    """A standalone labeled row, never a label embedded in a work-item row.

    Only an explicit label/value pair in adjacent cells or one `label: value`
    cell closes a table. The generic customer label needs a new card heading.
    """
    occupied = [i for i, c in enumerate(cells) if c[0].strip() or not c[3]]
    if not occupied:
        return False
    first = occupied[0]
    text, source, _, usable = cells[first]
    if not usable or source is None:
        return False
    labels = [
        label
        for names in ALIASES.values()
        for label in names
        if label != "Наименование"
    ]
    if len(occupied) == 1:
        return _key(text) == "карточка заказчика" or any(
            re.fullmatch(
                r"\s*" + re.escape(label) + r"\s*:\s*\S.*", text, re.IGNORECASE
            )
            for label in labels
        )
    return (
        occupied == [first, first + 1]
        and _key(text) in {_key(label) for label in labels}
        and cells[first + 1][3]
        and cells[first + 1][1] is not None
    )


def inspect_document(parsed):
    parsed = pdf_lines(parsed)
    by_id = {f.fragment_id: f for f in parsed.fragments}
    by_location = {f.location: f for f in parsed.fragments}
    values, sections, grids = [], {}, []
    unsupported = any(
        d.code in ("unsupported_content", "no_text") for d in parsed.diagnostics
    )
    roles = {}

    def role(kind, source):
        roles.setdefault(kind, {})[source.fragment_id] = source

    def add(field, value, source, cell=None, section=None):
        if not value.strip():
            return
        values.append(
            ValueEvidence(
                field=field,
                raw_value=value.strip(),
                source=source,
                cell=cell,
                section=section,
            )
        )
        role(
            "customer_card"
            if field in ("customer_name", "contact_email")
            else "work_description",
            source,
        )

    def labeled(line, source, card=False):
        for field, labels in ALIASES.items():
            for label in labels:
                if label == "Наименование" and not card:
                    continue
                match = re.fullmatch(
                    r"\s*" + re.escape(label) + r"\s*:\s*(.*?)\s*", line, re.IGNORECASE
                )
                if match:
                    add(field, match[1], source)
                    return True
        return False

    def text_group(lines):
        current, card = None, False
        for text, source in lines:
            for line in text.splitlines():
                key = _key(line)
                if key == "карточка заказчика":
                    card, current = True, None
                    role("customer_card", source)
                elif key in {_key(s) for s in ALIASES["work_description"]}:
                    current = f"{source.fragment_id}:{len(sections)}"
                    sections[current] = []
                    card = False
                    role("work_description", source)
                elif labeled(line, source, card):
                    current = None
                elif ":" in line or key in {
                    "реквизиты",
                    "стоимость",
                    "условия оплаты",
                    "примечания",
                    "подписи",
                    "перечень работ",
                }:
                    current, card = None, False
                elif current is not None and line.strip():
                    add("work_description", line, source, section=current)
                    sections[current].append(line.strip())

    def pair(cells, card):
        for index, (text, source, cell, usable) in enumerate(cells):
            if not usable or source is None:
                continue
            labeled(text, source, card)
            for field, labels in ALIASES.items():
                if _key(text) not in {
                    _key(label) for label in labels if label != "Наименование" or card
                }:
                    continue
                if index + 1 < len(cells):
                    value, ref, typed, valid = cells[index + 1]
                    if ref is not None and valid:
                        add(field, value, ref, typed)

    def scalar_grid(grid):
        card, in_items = False, False
        for _, cells in grid:
            keys = {_key(c[0]) for c in cells}
            header_fields = {
                field
                for field, labels in TABLE_ALIASES.items()
                if keys & {_key(label) for label in labels}
            }
            if len(header_fields) >= 2:
                in_items = True
                card = False
            if in_items:
                if not _scalar_boundary(cells):
                    continue
                in_items = False
            if "карточка заказчика" in keys:
                card = True
                for text, source, _, _ in cells:
                    if source and _key(text) == "карточка заказчика":
                        role("customer_card", source)
            elif keys & {
                _key(label)
                for field in (
                    "request_title",
                    "work_description",
                    "requested_completion_date",
                )
                for label in ALIASES[field]
            } or keys & {
                "реквизиты",
                "стоимость",
                "условия оплаты",
                "примечания",
                "подписи",
            }:
                card = False
            pair(cells, card)

    if parsed.document.format == "pdf":
        for page in sorted(
            {b.location.page for b in parsed.blocks if b.kind == "page"}
        ):
            lines = [
                f
                for f in parsed.fragments
                if f.location.page == page
                and f.location.line
                and f.location.column is None
            ]
            # A table closes an open description section; no table text becomes description.
            segment = []
            grid = []
            for f in lines:
                delimiter = "|" if "|" in f.text else "\t" if "\t" in f.text else None
                if delimiter:
                    if segment:
                        text_group(segment)
                        segment = []
                    parts = f.text.split(delimiter)
                    if delimiter == "|":
                        if not parts[0].strip():
                            parts = parts[1:]
                        if not parts[-1].strip():
                            parts = parts[:-1]
                    cells = []
                    for index, part in enumerate(parts, 1):
                        ref = by_location.get(
                            PdfLocation(page=page, line=f.location.line, column=index)
                        )
                        cells.append((part.strip(), ref, None, True))
                    if grid and f.location.line != grid[-1][0] + 1:
                        grids.append((f"pdf/{page}/{grid[0][0]}", grid))
                        grid = []
                    grid.append((f.location.line, cells))
                else:
                    if grid:
                        grids.append((f"pdf/{page}/{grid[0][0]}", grid))
                        grid = []
                        if f.text.strip() and ":" not in f.text:
                            unsupported = True
                    segment.append((f.text, f))
            if grid:
                grids.append((f"pdf/{page}/{grid[0][0]}", grid))
            if segment:
                text_group(segment)
    elif parsed.document.format == "docx":
        segment = []
        for block in parsed.blocks:
            if block.kind == "paragraph":
                if block.fragment_id:
                    segment.append((block.text, by_id[block.fragment_id]))
                continue
            text_group(segment)
            segment = []
            grid, simple = [], True
            width = len(block.table.rows[0].cells) if block.table.rows else 0
            for row in block.table.rows:
                cells = []
                for cell in row.cells:
                    plain = [b for b in cell.blocks if b.text.strip()]
                    valid = (
                        cell.usable
                        and not any(b.kind == "table" for b in cell.blocks)
                        and len(plain) <= 1
                    )
                    simple &= valid and len(row.cells) == width
                    b = plain[0] if plain else None
                    cells.append(
                        (
                            b.text if b else "",
                            by_id.get(b.fragment_id) if b else None,
                            None,
                            valid,
                        )
                    )
                grid.append((row.index, cells))
            if simple:
                grids.append((block.table.table_id, grid))
                scalar_grid(grid)
            else:
                unsupported = True
        text_group(segment)
    else:
        for sheet in parsed.sheets:
            grid = []
            for row in sheet.table.rows:
                cells = []
                previous = None
                for cell in row.cells:
                    column = column_index_from_string(
                        coordinate_from_string(cell.location.cell)[0]
                    )
                    # Preserve empty physical gaps: a distant cell is not "immediately right".
                    if previous is not None and column != previous + 1:
                        cells.append(("", None, None, False))
                    previous = column
                    source = by_id.get(cell.fragment_id)
                    text = source.text if source else ""
                    cells.append((text, source, cell, cell.usable))
                grid.append((row.index, cells))
            scalar_grid(grid)
            grids.append((f"xlsx/{sheet.name}", grid))

    layouts, unsupported_sources = [], []
    unsupported_headers = set()
    for table_id, grid in grids:
        for index, (_, cells) in enumerate(grid):
            columns, refs, duplicate = {}, [], False
            for column, (text, ref, _, usable) in enumerate(cells):
                for field, labels in TABLE_ALIASES.items():
                    if (
                        usable
                        and ref
                        and _key(text) in {_key(label) for label in labels}
                    ):
                        duplicate |= field in columns
                        columns[field] = column
                        refs.append(ref)
            if {"name", "quantity", "unit"} <= columns.keys():
                layouts.append((table_id, grid, index, columns, refs, duplicate))
            elif "name" in columns and len(columns) >= 2:
                # Exact work-name header plus another item header is a partial
                # business table, even if a different file supplies valid items.
                # Generic auxiliary headings (e.g. parameter/value) do not qualify.
                unsupported = True
                unsupported_sources.extend(refs)
                unsupported_headers.add((table_id, grid[index][0]))
    rows = []
    if len(layouts) == 1 and not layouts[0][-1]:
        table_id, grid, index, columns, refs, _ = layouts[0]
        for ref in refs:
            role("work_items", ref)
        width = len(grid[index][1])
        for row_index, cells in grid[index + 1 :]:
            if (table_id, row_index) in unsupported_headers:
                break
            if not any(c[0].strip() or not c[3] for c in cells):
                continue
            if _scalar_boundary(cells):
                break
            if len(cells) != width:
                unsupported = True
                continue
            name = cells[columns["name"]][0]
            if _key(name.strip().strip("*")) in {"итого", "всего"}:
                continue
            row_values, invalid = [], []
            for field, column in columns.items():
                text, source, cell, usable = cells[column]
                if not usable:
                    invalid.append(field)
                elif source and text.strip():
                    row_values.append(
                        ValueEvidence(
                            field=field,
                            raw_value=text.strip(),
                            source=source,
                            cell=cell,
                        )
                    )
            rows.append(
                RowEvidence(
                    row_id=f"{table_id}/row/{row_index}",
                    values=tuple(row_values),
                    unsupported_fields=tuple(invalid),
                )
            )
    elif layouts:
        unsupported = True
        unsupported_sources.extend(ref for layout in layouts for ref in layout[4])
    elif parsed.document.format == "pdf" and grids:
        unsupported = True

    # Remove the duplicate observation of the same literal labeled value.
    unique = {}
    for value in values:
        unique[
            (value.field, value.source.fragment_id, value.raw_value, value.section)
        ] = value
    classifications = tuple(
        ClassificationResult(
            document_id=parsed.document.document_id,
            document_type=kind,
            evidence=tuple(refs.values()),
        )
        for kind, refs in sorted(roles.items())
    )
    if not classifications:
        classifications = (
            ClassificationResult(
                document_id=parsed.document.document_id,
                document_type="unknown",
                evidence=(),
            ),
        )
    return DocumentEvidence(
        parsed=parsed,
        classifications=classifications,
        values=tuple(unique.values()),
        rows=tuple(rows),
        unsupported=unsupported,
        unsupported_sources=tuple(dict.fromkeys(unsupported_sources)),
    )
