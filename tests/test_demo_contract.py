import copy
import json
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from document_automation.schemas import ProcessingResult, WorkItem, WorkRequestData
from document_automation.workflows.work_request import demo_contract


def parse(raw):
    return ProcessingResult.model_validate_json(json.dumps(raw, ensure_ascii=False))


def test_exact_contract_fields_and_sources():
    contract = demo_contract()
    assert (
        set(contract["fields"])
        == set(WorkRequestData.model_fields)
        == {
            "customer_name",
            "request_title",
            "work_description",
            "requested_completion_date",
            "contact_email",
            "items",
        }
    )
    assert (
        set(contract["item_fields"])
        == set(WorkItem.model_fields)
        == {"item_code", "name", "quantity", "unit"}
    )
    assert [
        name for name, rule in contract["fields"].items() if not rule["required"]
    ] == ["contact_email"]
    assert all(rule["required"] for rule in contract["item_fields"].values())
    assert {
        name for name, rule in contract["fields"].items() if len(rule["sources"]) > 1
    } == {
        "customer_name",
        "requested_completion_date",
    }
    assert contract["item_fields"]["unit"]["allowed"] == ["шт", "ч", "кв.м", "компл."]
    assert (
        contract["fields"]["requested_completion_date"]["sources"]["work_items"]
        == "Заявка!B2"
    )
    assert contract["document_types"] == {
        "customer_card": "pdf",
        "work_description": "docx",
        "work_items": "xlsx",
    }
    assert json.loads(json.dumps(contract, ensure_ascii=False)) == contract
    contract["item_fields"]["unit"]["allowed"].append("invented")
    assert demo_contract()["item_fields"]["unit"]["allowed"] == [
        "шт",
        "ч",
        "кв.м",
        "компл.",
    ]


@pytest.mark.parametrize(
    "key,state,expected_date,code",
    [
        ("A", "found", date(2026, 11, 20), None),
        ("B", "missing", None, "required_missing"),
        ("C", "conflict", None, "conflicting_values"),
    ],
)
def test_business_expected_results(scenarios, key, state, expected_date, code):
    result = parse(scenarios[key]["result"])
    assert result.processing_status == "completed" and result.can_generate
    assert result.needs_review is (key != "A")
    assert result.generated_document is None  # Fixture is not a generated DOCX.
    data = result.data
    assert data.customer_name.value == "ООО «Демо Офис»"
    assert data.request_title.value == "Подготовка офисной комнаты"
    assert (
        data.work_description.value
        == "Собрать два офисных стола и установить четыре настенные полки."
    )
    assert data.contact_email.value == "office@example.com"
    assert data.requested_completion_date.state == state
    assert data.requested_completion_date.value == expected_date
    assert [
        (i.item_code.value, i.name.value, i.quantity.value, i.unit.value)
        for i in data.items
    ] == [
        ("W-01", "Сборка офисного стола", Decimal("2"), "шт"),
        ("W-02", "Установка настенной полки", Decimal("4"), "шт"),
    ]
    assert data.items_state == "found"
    assert len(result.issues) == (0 if code is None else 1)
    if code:
        assert (result.issues[0].code, result.issues[0].path) == (
            code,
            "requested_completion_date",
        )
    assert {c.source.document_id for c in data.customer_name.candidates} == {
        "pdf",
        "docx",
    }
    for row, item in enumerate(data.items, 2):
        for column, name in zip("ABCD", WorkItem.model_fields):
            location = getattr(item, name).candidates[0].source.location
            assert (location.sheet, location.cell) == ("Работы", f"{column}{row}")
    if key == "C":
        assert [
            c.interpretations[0] for c in data.requested_completion_date.candidates
        ] == [date(2026, 11, 20), date(2026, 11, 22)]
    expected = scenarios[key]["expected_docx"]
    assert (
        expected["date_text"]
        == {"A": "20.11.2026", "B": "[НЕ УКАЗАНО]", "C": "[ПРОТИВОРЕЧИЕ В ИСТОЧНИКАХ]"}[
            key
        ]
    )
    assert (
        expected["draft"] is result.needs_review
    )  # Target only; rendering not tested.


@pytest.mark.parametrize(
    "key,stage,code",
    [("T1", "extraction", "llm_unavailable"), ("T2", "parsing", "file_read_error")],
)
def test_technical_expected_results(scenarios, key, stage, code):
    raw = scenarios[key]["result"]
    result = parse(raw)
    assert result.processing_status == "failed"
    assert (
        result.data is None
        and result.generated_document is None
        and not result.can_generate
    )
    assert (result.audit[0].stage, result.audit[0].error_code) == (stage, code)
    raw["audit"] = []
    with pytest.raises(ValidationError):
        parse(raw)


def generated(run="A", review=False):
    return dict(
        run_id=run,
        filename="work_request_needs_review.docx" if review else "work_request.docx",
        sha256="a" * 64,
        template_sha256="b" * 64,
        needs_review=review,
    )


def test_generated_metadata_requires_human_review(happy):
    happy["generated_document"] = generated()
    assert parse(happy).generated_document.human_review_required is True
    happy["generated_document"]["human_review_required"] = False
    with pytest.raises(ValidationError):
        parse(happy)


@pytest.mark.parametrize("value", [1, 0, "true", None])
def test_human_review_flag_does_not_coerce(happy, value):
    happy["generated_document"] = generated()
    happy["generated_document"]["human_review_required"] = value
    with pytest.raises(ValidationError):
        parse(happy)


@pytest.mark.parametrize("key", ["T1", "T2"])
def test_failed_cannot_contain_generated_document(scenarios, key):
    raw = scenarios[key]["result"]
    raw["generated_document"] = generated(run=key)
    with pytest.raises(ValidationError):
        parse(raw)


def test_review_draft_metadata(scenarios):
    raw = scenarios["C"]["result"]
    raw["generated_document"] = generated(run="C", review=True)
    assert parse(raw).generated_document.needs_review
    raw["generated_document"]["filename"] = "work_request.docx"
    with pytest.raises(ValidationError):
        parse(raw)


def test_missing_optional_email_does_not_require_review(happy):
    happy["data"]["contact_email"] = dict(state="missing", value=None, candidates=[])
    assert not parse(happy).needs_review


def test_required_missing_is_data_not_schema_failure(happy):
    happy["data"]["items"] = []
    with pytest.raises(ValidationError):
        parse(happy)
    happy.update(
        needs_review=True,
        issues=[dict(code="required_missing", path="items", message="Нет позиций")],
    )
    assert parse(happy).data.items_state == "missing"


@pytest.mark.parametrize(
    "state,values",
    [
        ("missing", []),
        ("found", ["x"]),
        ("ambiguous", [["x", "y"]]),
        ("conflict", ["x", "y"]),
    ],
)
def test_unverified_source_remains_issue_after_state_recalculation(
    happy, state, values
):
    original = happy["data"]["contact_email"]["candidates"][0]
    candidates = []
    for index, value in enumerate(values):
        c = copy.deepcopy(original)
        c["interpretations"] = value if isinstance(value, list) else [value]
        c["source"]["fragment_id"] = f"email:{index}"
        candidates.append(c)
    rejected = {
        **copy.deepcopy(original),
        "verified": False,
        "source": None,
        "interpretations": ["rejected"],
    }
    field = dict(
        state=state,
        value="x" if state == "found" else None,
        candidates=candidates + [rejected],
    )
    happy["data"]["contact_email"] = field
    happy.update(
        needs_review=True,
        issues=[
            dict(
                code="unverified_source",
                path="contact_email",
                message="Не подтверждено",
            )
        ],
    )
    if state in ("ambiguous", "conflict"):
        happy["issues"].append(
            dict(
                code="ambiguous_value"
                if state == "ambiguous"
                else "conflicting_values",
                path="contact_email",
                message="Проверить",
            )
        )
    result = parse(happy)
    assert result.data.contact_email.state == state and result.needs_review
    assert result.data.contact_email.value != "rejected"
    happy["issues"] = [i for i in happy["issues"] if i["code"] != "unverified_source"]
    with pytest.raises(ValidationError):
        parse(happy)


def test_case_only_customer_variants_are_not_conflicts(happy):
    c = happy["data"]["customer_name"]["candidates"][1]
    c["interpretations"] = [c["interpretations"][0].lower()]
    assert parse(happy).data.customer_name.state == "found"


@pytest.mark.parametrize("key", ["B", "C"])
def test_issues_and_review_cannot_be_suppressed(scenarios, key):
    raw = scenarios[key]["result"]
    raw["needs_review"] = False
    with pytest.raises(ValidationError):
        parse(raw)
    raw["issues"] = []
    with pytest.raises(ValidationError):
        parse(raw)


def test_invalid_business_value_is_preserved_with_issue(happy):
    field = happy["data"]["items"][0]["quantity"]
    field["value"] = "0"
    field["candidates"][0]["interpretations"] = ["0"]
    field["candidates"][0]["raw_value"] = "0"
    field["candidates"][0]["source"]["text"] = "0"
    happy.update(
        needs_review=True,
        issues=[
            dict(
                code="invalid_value",
                path="items[0].quantity",
                message="Количество должно быть больше нуля",
            )
        ],
    )
    assert parse(happy).data.items[0].quantity.value == Decimal("0")
