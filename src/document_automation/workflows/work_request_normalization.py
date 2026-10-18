"""Contract-specific normalization. Original evidence is never changed."""

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

from document_automation.parsing import ParsedCell
from document_automation.workflows.work_request import demo_contract


def spaces(value: str) -> str:
    return " ".join(value.split())


def normalize(field: str, raw: str, cell: ParsedCell | None = None):
    """Return (typed interpretations, invalid_value). No guessed alternatives."""
    value = raw.strip()
    if field == "requested_completion_date":
        if cell is not None and cell.value.kind in ("date", "datetime"):
            value = cell.value.value
            return ((value.date() if isinstance(value, datetime) else value,), False)
        for pattern, fmt in (
            (r"[0-9]{2}\.[0-9]{2}\.[0-9]{4}", "%d.%m.%Y"),
            (r"[0-9]{4}-[0-9]{2}-[0-9]{2}", "%Y-%m-%d"),
        ):
            if re.fullmatch(pattern, value):
                try:
                    return ((datetime.strptime(value, fmt).date(),), False)
                except ValueError:
                    break
        return ((), True)
    if field == "quantity":
        numeric = cell is not None and cell.value.kind in ("integer", "number")
        if cell is not None and cell.value.kind not in ("integer", "number", "string"):
            return ((), True)
        if numeric:
            value = cell.stored_value or str(cell.value.value)
        elif not re.fullmatch(r"[+-]?[0-9]+(?:[.,][0-9]+)?", value):
            return ((), True)
        try:
            quantity = Decimal(value.replace(",", "."))
        except InvalidOperation:
            return ((), True)
        if not quantity.is_finite():
            return ((), True)
        return ((quantity,), quantity <= 0)
    if field == "contact_email":
        local, sep, domain = value.rpartition("@")
        if sep:
            value = local + sep + domain.lower()
        return (
            (value,),
            re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value) is None,
        )
    if field == "item_code":
        value = value.translate(
            str.maketrans("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ")
        )
        return ((value,), re.fullmatch(r"[A-Z0-9-]+", value) is None)
    if field == "unit":
        rule = demo_contract()["item_fields"]["unit"]
        value = value.lower()
        value = rule["aliases"].get(value, value)
        return ((value,), value not in rule["allowed"])
    if field == "work_description":
        value = "\n".join(spaces(p) for p in value.splitlines())
    else:
        value = spaces(value)
    return ((value,), False)
