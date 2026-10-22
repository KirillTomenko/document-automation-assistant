"""Schema metadata is the machine-readable contract; no parallel JSON rule file."""

from copy import deepcopy
from datetime import date
from decimal import Decimal

from pydantic import Field

from document_automation.contracts import ContractModel, FieldResult, Text


def rule(required: bool, sources: dict[str, str], normalization: str, **details):
    return Field(
        json_schema_extra={
            "demo": {
                "required": required,
                "sources": sources,
                "normalization": normalization,
                **details,
            }
        }
    )


class WorkItem(ContractModel):
    item_code: FieldResult[Text] = rule(
        True,
        {"work_items": "Работы!A{row}"},
        "Trim; uppercase Latin; Latin letters, digits and hyphen only.",
        unique=True,
        duplicate_state="ambiguous",
    )
    name: FieldResult[Text] = rule(
        True,
        {"work_items": "Работы!B{row}"},
        "Trim and collapse spaces; preserve wording.",
    )
    quantity: FieldResult[Decimal] = rule(
        True,
        {"work_items": "Работы!C{row}"},
        "Excel number or decimal string with dot/comma; no thousands separators; Decimal.",
        exclusive_minimum="0",
    )
    unit: FieldResult[Text] = rule(
        True,
        {"work_items": "Работы!D{row}"},
        "Trim; lowercase; apply aliases.",
        allowed=["шт", "ч", "кв.м", "компл."],
        aliases={
            "шт.": "шт",
            "час": "ч",
            "часов": "ч",
            "кв. м": "кв.м",
            "кв.м.": "кв.м",
            "м2": "кв.м",
            "м²": "кв.м",
            "компл": "компл.",
            "комплект": "компл.",
            "комплекта": "компл.",
            "комплектов": "компл.",
        },
    )


class CustomerNameResult(FieldResult[Text]):
    def comparison_key(self, value):
        return value.casefold()


class WorkRequestData(ContractModel):
    customer_name: CustomerNameResult = rule(
        True,
        {"customer_card": "Заказчик", "work_description": "Заказчик"},
        "Trim; NBSP to space; collapse spaces; compare case-insensitively; preserve name and legal form.",
        equal_display_order=["customer_card", "work_description"],
    )
    request_title: FieldResult[Text] = rule(
        True,
        {"work_description": "Название заявки"},
        "Trim; collapse whitespace and line breaks.",
    )
    work_description: FieldResult[Text] = rule(
        True,
        {"work_description": "Описание работ"},
        "Preserve paragraphs and wording; trim paragraphs and normalize internal spaces.",
    )
    requested_completion_date: FieldResult[date] = rule(
        True,
        {"work_description": "Желаемая дата завершения", "work_items": "Заявка!B2"},
        "DD.MM.YYYY, YYYY-MM-DD or Excel date; date; no comparison to today's date.",
        display_format="DD.MM.YYYY",
    )
    contact_email: FieldResult[Text] = rule(
        False,
        {"customer_card": "Контактный email"},
        "Trim; lowercase domain; name@domain without spaces, dot in domain; no delivery check.",
    )
    items: tuple[WorkItem, ...] = rule(
        True,
        {"work_items": "Работы"},
        "Headers row 1; data from row 2; skip empty rows; keep partial rows and input order.",
        minimum_items=1,
        unsupported=["formulas", "merged_cells"],
    )

    @property
    def items_state(self) -> str:
        """Derived technical metadata, not an additional input/business field."""
        return "found" if self.items else "missing"


def demo_contract(version="1.0") -> dict:
    """Fresh JSON-serializable description derived from model field metadata."""
    contract = deepcopy(
        {
            "workflow": "work_request",
            "version": "1.0",
            "document_types": {
                "customer_card": "pdf",
                "work_description": "docx",
                "work_items": "xlsx",
            },
            "fields": {
                name: field.json_schema_extra["demo"]
                for name, field in WorkRequestData.model_fields.items()
            },
            "item_fields": {
                name: field.json_schema_extra["demo"]
                for name, field in WorkItem.model_fields.items()
            },
        }
    )
    if version == "2.0":
        contract["version"] = version
        contract["document_types"] = {
            kind: ["pdf", "docx", "xlsx"] for kind in contract["document_types"]
        }
        for rule_set in (contract["fields"], contract["item_fields"]):
            for field in rule_set.values():
                field["sources"] = dict.fromkeys(
                    contract["document_types"], "Verified labeled field or table cell"
                )
    elif version != "1.0":
        raise ValueError("Unsupported work_request contract version")
    return contract
