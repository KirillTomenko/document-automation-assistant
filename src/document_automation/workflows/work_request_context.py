"""Application-owned context checks shared by extraction and validation."""

import re

from openpyxl.utils.cell import (
    column_index_from_string,
    coordinate_from_string,
    get_column_letter,
)

from document_automation.workflows.work_request_normalization import spaces


def _cells(parsed):
    return {
        (c.location.sheet, c.location.cell): c
        for s in parsed.sheets
        for r in s.table.rows
        for c in r.cells
    }


def _label_values(text, label):
    return re.findall(
        r"(?mi)^[ \t]*" + re.escape(label) + r"[ \t]*:[ \t]*([^\r\n]*)", text
    )


ALIASES = {
    "customer_name": (
        "Заказчик",
        "Наименование заказчика",
        "Организация-заказчик",
        "Наименование",
    ),
    "contact_email": ("Контактный email", "Email", "E-mail", "Электронная почта"),
    "request_title": ("Название заявки", "Наименование заявки"),
    "requested_completion_date": ("Желаемая дата завершения", "Дата завершения работ"),
    "work_description": (
        "Описание работ",
        "Состав работ",
        "Перечень выполняемых работ",
        "Этапы работ",
    ),
}
TABLE_ALIASES = {
    "item_code": ("Код позиции", "Код работы"),
    "name": ("Наименование работ", "Наименование работы", "Описание работ"),
    "quantity": ("Количество", "Кол-во"),
    "unit": ("Единица", "Ед. изм.", "Единица измерения"),
}


def label_values(text, field, source_format=None):
    return [
        value
        for label in ALIASES[field]
        if label != "Наименование"
        or (
            source_format == "pdf"
            and any(_key(line) == "карточка заказчика" for line in text.splitlines())
        )
        for value in _label_values(text, label)
    ]


def _key(text):
    return spaces(text).rstrip(":").casefold()


def _description_sections(parsed):
    """Explicit headings, also inside multiline paragraphs; unknown labels stop a section."""
    sections, current = [], None
    headings = {_key(s) for s in ALIASES["work_description"]}
    for block in parsed.blocks:
        if block.kind != "paragraph":
            current = None
            continue
        for line in block.text.splitlines() or [""]:
            if _key(line) in headings:
                current = []
                sections.append(current)
            elif ":" in line or _key(line) in {
                "реквизиты",
                "стоимость",
                "условия оплаты",
                "примечания",
                "подписи",
            }:
                current = None
            elif current is not None:
                current.append(block.model_copy(update={"text": line}))
    return sections


def item_layout(parsed):
    """One unambiguous table per workbook. Never infer item_code from ordinal №."""
    layouts = []
    for sheet in parsed.sheets:
        for row in sheet.table.rows:
            columns, evidence, duplicate = {}, [], False
            for cell in row.cells:
                if not cell.usable or cell.value.kind != "string":
                    continue
                for field, labels in TABLE_ALIASES.items():
                    if _key(cell.value.value) in {_key(label) for label in labels}:
                        duplicate |= field in columns
                        columns[field] = coordinate_from_string(cell.location.cell)[0]
                        evidence.append(cell.fragment_id)
            if {"name", "quantity", "unit"} <= columns.keys():
                layouts.append((sheet.name, row.index, columns, evidence, duplicate))
    if len(layouts) != 1 or layouts[0][4]:
        return None
    return layouts[0][:4]


def date_cell(parsed, source, cells):
    column, row = coordinate_from_string(source.location.cell)
    index = column_index_from_string(column)
    if index <= 1:
        return False
    label = cells.get((source.location.sheet, f"{get_column_letter(index - 1)}{row}"))
    return (
        label is not None
        and label.usable
        and label.value.kind == "string"
        and (
            _key(label.value.value)
            in {_key(s) for s in ALIASES["requested_completion_date"]}
        )
    )


def is_summary_row(row, columns):
    name_cell = next(
        (c for c in row.cells if c.location.cell == f"{columns['name']}{row.index}"),
        None,
    )
    return bool(
        name_cell
        and name_cell.usable
        and name_cell.value.kind == "string"
        and _key(name_cell.value.value.strip().strip("*")) in {"итого", "всего"}
    )


def _context_matches(field, candidate, parsed, row=None, cells=None):
    source, raw = candidate.source, candidate.raw_value
    if source.location.format == "xlsx":
        cells = cells if cells is not None else _cells(parsed)
        expected = (source.location.sheet, source.location.cell)
        cell = cells.get(expected)
        if field == "requested_completion_date":
            structure_ok = date_cell(parsed, source, cells)
        elif field in TABLE_ALIASES and row is not None:
            layout = item_layout(parsed)
            source_row = next(
                (
                    r
                    for s in parsed.sheets
                    for r in s.table.rows
                    if s.name == source.location.sheet and r.index == row
                ),
                None,
            )
            structure_ok = bool(
                layout
                and source_row
                and not is_summary_row(source_row, layout[2])
                and row > layout[1]
                and field in layout[2]
                and expected == (layout[0], f"{layout[2][field]}{row}")
            )
        else:
            return False, None
        return bool(
            structure_ok
            and cell
            and cell.usable
            and cell.fragment_id == source.fragment_id
            and raw == source.text
        ), cell
    if field not in ALIASES:
        return False, None
    if any(
        raw.strip() == value.strip()
        for value in label_values(source.text, field, source.location.format)
    ):
        return raw in source.text, None
    if field == "work_description" and source.location.format == "docx":
        return any(
            block.fragment_id == source.fragment_id and raw == block.text
            for section in _description_sections(parsed)
            for block in section
        ), None
    return False, None


def type_evidence(parsed):
    """Content evidence independent of the model's classification assertion."""
    if parsed.document.format == "xlsx":
        layout = item_layout(parsed)
        return ("work_items", tuple(layout[3])) if layout else ("unknown", ())
    fields = (
        ("customer_name", "contact_email")
        if parsed.document.format == "pdf"
        else ("request_title", "work_description", "requested_completion_date")
    )
    ids = [
        f.fragment_id
        for f in parsed.fragments
        if any(label_values(f.text, name, parsed.document.format) for name in fields)
        or (parsed.document.format == "pdf" and "Карточка заказчика" in f.text)
        or (
            parsed.document.format == "docx"
            and any(
                _key(line) in {_key(s) for s in ALIASES["work_description"]}
                for line in f.text.splitlines()
            )
        )
    ]
    kind = "customer_card" if parsed.document.format == "pdf" else "work_description"
    return (kind, tuple(ids)) if ids else ("unknown", ())
