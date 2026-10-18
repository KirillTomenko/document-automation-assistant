from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest
from business_samples import make_input, pdf_snapshot
from pydantic import ValidationError

from document_automation.adapters.parsers import parse_pdf
from document_automation.contracts import ClassificationResult
from document_automation.operations import (
    OperationDecision,
    audit_operation,
    check_operation,
)
from document_automation.parsing import ParsedDocument, ParseDiagnostic
from document_automation.schemas import AuditEvent, ProcessingResult
from document_automation.workflows.work_request_inputs import (
    RawCandidate,
    RegisteredSource,
    WorkRequestInput,
)
from document_automation.workflows.work_request_processing import process_work_request


def change(model, **values):
    return type(model).model_validate({**model.model_dump(), **values})


def codes(result):
    return {(i.code, i.path) for i in result.issues}


@pytest.mark.parametrize(
    "scenario,state,value,code",
    [
        ("A", "found", date(2026, 11, 20), None),
        ("B", "missing", None, "required_missing"),
        ("C", "conflict", None, "conflicting_values"),
    ],
)
def test_abc_actual_processing(tmp_path, scenario, state, value, code):
    inputs = make_input(tmp_path, scenario)
    before = inputs.model_dump_json()
    result, events = process_work_request(inputs)
    assert inputs.model_dump_json() == before
    assert result.processing_status == "completed" and result.can_generate
    assert result.needs_review == (scenario != "A")
    assert result.data.requested_completion_date.state == state
    assert result.data.requested_completion_date.value == value
    assert codes(result) == ({(code, "requested_completion_date")} if code else set())
    assert result.data.customer_name.value == "ООО «Демо Офис»"
    assert len(result.data.customer_name.candidates) == 2
    assert result.data.request_title.value == "Подготовка офисной комнаты"
    assert result.data.contact_email.value == "office@example.com"
    assert (
        result.data.work_description.value
        == "Собрать два офисных стола и установить четыре настенные полки."
    )
    assert [
        (i.item_code.value, i.name.value, i.quantity.value, i.unit.value)
        for i in result.data.items
    ] == [
        ("W-01", "Сборка офисного стола", Decimal(2), "шт"),
        ("W-02", "Установка настенной полки", Decimal(4), "шт"),
    ]
    assert events == result.audit and events[-1].decision.allowed
    assert events[-1].decision.draft_required == result.needs_review
    assert result.generated_document is None
    assert ProcessingResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "attack",
    ["missing", "id", "text", "coordinate", "foreign", "substring", "wrong_context"],
)
def test_reject_forged_or_wrong_context_candidate(tmp_path, attack):
    inputs = make_input(tmp_path, extra_doc=("Комментарий: Вымышленный заказчик",))
    good = inputs.fields.customer_name[0]
    source, raw = good.source, good.raw_value
    if attack == "missing":
        source = None
    elif attack == "id":
        source = change(source, fragment_id="invented")
    elif attack == "text":
        source = change(source, text=source.text + " altered")
    elif attack == "coordinate":
        source = change(source, location={"format": "pdf", "page": 2})
    elif attack == "foreign":
        source = change(source, document_id="foreign")
    elif attack == "substring":
        raw = "Демо"
    else:
        source = inputs.sources[1].parsed.fragments[-1]
        raw = "Вымышленный заказчик"
    rejected = RawCandidate(raw_value=raw, source=source)
    inputs = change(
        inputs,
        fields=change(
            inputs.fields, customer_name=inputs.fields.customer_name + (rejected,)
        ),
    )
    result, _ = process_work_request(inputs)
    assert result.data.customer_name.state == "found"
    assert result.data.customer_name.value == "ООО «Демо Офис»"
    assert sum(not c.verified for c in result.data.customer_name.candidates) == 1
    assert ("unverified_source", "customer_name") in codes(result)
    assert not next(i for i in result.issues if i.code == "unverified_source").sources


def test_conflict_wins_over_unparseable_candidate(tmp_path):
    inputs = make_input(
        tmp_path, "C", extra_doc=("Желаемая дата завершения: 03/04/2026",)
    )
    result, _ = process_work_request(inputs)
    field = result.data.requested_completion_date
    assert field.state == "conflict" and field.value is None
    assert any(c.verified and not c.interpretations for c in field.candidates)
    assert ("invalid_value", "requested_completion_date") in codes(result)
    assert ("ambiguous_value", "requested_completion_date") not in codes(result)


@pytest.mark.parametrize(
    "quantity,state,value",
    [
        (0, "found", Decimal(0)),
        (-2, "found", Decimal(-2)),
        ("1 000", "ambiguous", None),
        (True, "ambiguous", None),
    ],
)
def test_invalid_quantities(tmp_path, quantity, state, value):
    inputs = make_input(tmp_path, rows=[["W-01", "Работа", quantity, "шт"]])
    result, _ = process_work_request(inputs)
    assert result.data.items[0].quantity.state == state
    assert result.data.items[0].quantity.value == value
    assert ("invalid_value", "items[0].quantity") in codes(result)


@pytest.mark.parametrize("value", ["31.02.2026", "03/04/2026", "2026-13-01", "46346"])
def test_invalid_dates(tmp_path, value):
    result, _ = process_work_request(
        make_input(tmp_path, doc_date=value, excel_date="")
    )
    assert result.data.requested_completion_date.state == "ambiguous"
    assert ("invalid_value", "requested_completion_date") in codes(result)


@pytest.mark.parametrize("email", ["bad", "x@y", "x y@example.com", "x@@example.com"])
def test_invalid_email_is_found(tmp_path, email):
    result, _ = process_work_request(make_input(tmp_path, email=email))
    assert result.data.contact_email.state == "found"
    assert ("invalid_value", "contact_email") in codes(result)


@pytest.mark.parametrize("unit", ["кг", "часа", "pieces"])
def test_invalid_unit_is_found(tmp_path, unit):
    result, _ = process_work_request(
        make_input(tmp_path, rows=[["W-01", "Работа", 1, unit]])
    )
    assert result.data.items[0].unit.state == "found"
    assert ("invalid_value", "items[0].unit") in codes(result)


def test_normalized_equality_and_excel_types(tmp_path):
    inputs = make_input(
        tmp_path,
        pdf_customer="  ООО\u00a0«Демо   Офис» ",
        doc_customer="ооо «демо офис»",
        email="User@EXAMPLE.COM",
        excel_date=datetime(2026, 11, 20),
        rows=[
            [" w-01 ", "Работа   один", "2,5", "часов"],
            ["W-02", "Вторая", 0.1, "шт."],
        ],
    )
    result, _ = process_work_request(inputs)
    assert not result.needs_review
    assert result.data.customer_name.value == "ООО «Демо Офис»"
    assert result.data.contact_email.value == "User@example.com"
    assert result.data.items[0].quantity.value == Decimal("2.5")
    assert result.data.items[1].quantity.value == Decimal("0.1")
    assert result.data.items[0].unit.value == "ч"
    assert result.data.items[0].item_code.value == "W-01"


def test_empty_partial_and_duplicate_rows(tmp_path):
    inputs = make_input(
        tmp_path,
        rows=[
            ["w-01", "Первая", 1, "шт"],
            [None] * 4,
            ["W-01", "Вторая", None, "шт"],
            [None, "Третья", 3, "шт"],
        ],
    )
    result, _ = process_work_request(inputs)
    assert len(result.data.items) == 3
    for index in (0, 1):
        assert (
            result.data.items[index].item_code.ambiguity_reason == "duplicate_item_code"
        )
    assert result.data.items[1].name.candidates[0].source.location.cell == "B4"
    assert ("required_missing", "items[1].quantity") in codes(result)
    assert ("required_missing", "items[2].item_code") in codes(result)


def test_empty_items_and_optional_email(tmp_path):
    result, _ = process_work_request(make_input(tmp_path, rows=[], email=""))
    assert codes(result) == {("required_missing", "items")}
    assert result.data.contact_email.state == "missing"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda b: setattr(b["Работы"]["C2"], "value", "=1+1"),
        lambda b: b["Работы"].merge_cells("C2:D2"),
    ],
)
def test_unsupported_cells(tmp_path, mutation):
    inputs = make_input(tmp_path, mutate_book=mutation)
    result, _ = process_work_request(inputs)
    assert result.data.items[0].quantity.state == "missing"
    assert ("unsupported_content", "items[0].quantity") in codes(result)


def test_wrong_sheet_header_and_cell(tmp_path):
    inputs = make_input(
        tmp_path, mutate_book=lambda b: setattr(b["Работы"]["C1"], "value", "Цена")
    )
    result, _ = process_work_request(inputs)
    assert not result.data.items
    assert ("unknown_document", "documents") in codes(result)
    good = make_input(tmp_path)
    raw = good.items[0].quantity[0]
    wrong = change(
        raw,
        source=change(
            raw.source, location={"format": "xlsx", "sheet": "Работы", "cell": "C3"}
        ),
    )
    items = (change(good.items[0], quantity=(wrong,)),) + good.items[1:]
    result, _ = process_work_request(change(good, items=items))
    assert not result.data.items[0].quantity.candidates[0].verified


def test_omitted_prepared_row_is_not_silently_lost(tmp_path):
    inputs = make_input(tmp_path)
    result, _ = process_work_request(change(inputs, items=inputs.items[:1]))
    assert len(result.data.items) == 2
    assert ("required_missing", "items[1].name") in codes(result)


def test_technical_failure_and_foreign_registration(tmp_path):
    inputs = make_input(tmp_path)
    broken = ParsedDocument(
        filename="broken.docx",
        document=None,
        status="failed",
        diagnostics=(
            ParseDiagnostic(
                code="file_read_error", severity="error", message="synthetic failure"
            ),
        ),
    )
    for source in (
        change(inputs.sources[1], parsed=broken),
        change(inputs.sources[1], run_id="foreign"),
    ):
        result, events = process_work_request(
            change(inputs, sources=(inputs.sources[0], source, inputs.sources[2]))
        )
        assert (
            result.processing_status == "failed"
            and result.data is None
            and not result.can_generate
        )
        assert not events[-1].decision.allowed and result.generated_document is None
        assert any(e.event == "failed" for e in events)


@pytest.mark.parametrize("kind", ["missing", "unknown", "forged", "duplicate"])
def test_document_classifications(tmp_path, kind):
    inputs = make_input(tmp_path)
    if kind == "missing":
        inputs = change(
            inputs,
            sources=inputs.sources[1:],
            classifications=inputs.classifications[1:],
        )
    elif kind == "unknown":
        inputs = change(inputs, classifications=inputs.classifications[1:])
    elif kind == "forged":
        c = inputs.classifications[0]
        c = change(c, evidence=(change(c.evidence[0], text="invented"),))
        inputs = change(inputs, classifications=(c,) + inputs.classifications[1:])
    else:
        p = pdf_snapshot("Карточка заказчика\nЗаказчик: Второй")
        inputs = change(
            inputs,
            sources=inputs.sources
            + (RegisteredSource(run_id=inputs.run_id, source_id="extra", parsed=p),),
            classifications=inputs.classifications
            + (
                ClassificationResult(
                    document_id=p.document.document_id,
                    document_type="customer_card",
                    evidence=p.fragments,
                ),
            ),
        )
    result, _ = process_work_request(inputs)
    assert result.needs_review and result.processing_status == "completed"
    expected = "duplicate_document" if kind == "duplicate" else "missing_document"
    assert (expected, "documents") in codes(result)


def test_guards_and_audit_do_not_mutate_result(tmp_path):
    result, _ = process_work_request(make_input(tmp_path))
    before = result.model_dump_json()
    for operation in ("modify_confirmed", "execute_external_action", "export_document"):
        decision = check_operation(operation, run_id=result.run_id, result=result)
        assert not decision.allowed
        event = AuditEvent(
            run_id=result.run_id,
            timestamp=datetime.now().astimezone(),
            stage="review",
            event="operation_decision",
            message="Operation denied",
            decision=decision,
        )
        assert AuditEvent.model_validate_json(event.model_dump_json()) == event
    assert result.model_dump_json() == before
    assert check_operation(
        "read_source", run_id="A", source_id="x", registered_sources=(("A", "x"),)
    ).allowed
    assert not check_operation(
        "read_source", run_id="B", source_id="x", registered_sources=(("A", "x"),)
    ).allowed
    assert not check_operation("create_draft", run_id="other", result=result).allowed
    assert not check_operation("create_draft", run_id="A").allowed


def test_input_cannot_assert_verified_or_permissions(tmp_path):
    inputs = make_input(tmp_path)
    raw = inputs.fields.customer_name[0].model_dump()
    for key in ("verified", "allowed"):
        with pytest.raises(ValidationError):
            RawCandidate.model_validate({**raw, key: True})
    assert WorkRequestInput.model_validate_json(inputs.model_dump_json()) == inputs
    assert WorkRequestInput.model_json_schema()


@pytest.mark.parametrize(
    "scenario,doc_date,excel_date,expected",
    [
        ("A", "20.11.2026", "2026-11-20", "found"),
        ("B", "", "", "missing"),
        ("A", "03/04/2026", "", "ambiguous"),
        ("C", "20.11.2026", "2026-11-22", "conflict"),
    ],
)
def test_unverified_candidate_retained_for_all_states(
    tmp_path, scenario, doc_date, excel_date, expected
):
    inputs = make_input(tmp_path, scenario, doc_date=doc_date, excel_date=excel_date)
    rejected = RawCandidate(raw_value="2099-01-01")
    inputs = change(
        inputs,
        fields=change(
            inputs.fields,
            requested_completion_date=inputs.fields.requested_completion_date
            + (rejected,),
        ),
    )
    result, _ = process_work_request(inputs)
    field = result.data.requested_completion_date
    assert field.state == expected
    assert (
        not field.candidates[-1].verified
        and field.candidates[-1].raw_value == "2099-01-01"
    )
    assert ("unverified_source", "requested_completion_date") in codes(result)


def test_description_preserves_paragraphs_and_requires_complete_section(tmp_path):
    inputs = make_input(
        tmp_path, extra_doc=("", "  Второй   абзац.  ", "Примечания:", "Не описание")
    )
    p = inputs.sources[1].parsed
    second = next(f for f in p.fragments if "Второй" in f.text)
    candidates = inputs.fields.work_description + (
        RawCandidate(raw_value=second.text, source=second),
    )
    incomplete, _ = process_work_request(inputs)
    assert incomplete.data.work_description.state == "ambiguous"
    assert ("unsupported_content", "work_description") in codes(incomplete)
    result, _ = process_work_request(
        change(inputs, fields=change(inputs.fields, work_description=candidates))
    )
    assert not result.needs_review
    assert result.data.work_description.value.endswith("полки.\n\nВторой абзац.")
    assert len(result.data.work_description.candidates) == 2
    outside = next(f for f in p.fragments if f.text == "Не описание")
    result, _ = process_work_request(
        change(
            inputs,
            fields=change(
                inputs.fields,
                work_description=candidates
                + (RawCandidate(raw_value=outside.text, source=outside),),
            ),
        )
    )
    assert not result.data.work_description.candidates[-1].verified


def test_arbitrary_label_is_not_enough_for_wrong_semantic_type(tmp_path):
    inputs = make_input(tmp_path)
    pdf = inputs.sources[0].parsed.fragments[0]
    candidate = RawCandidate(raw_value="ООО «Демо Офис»", source=pdf)
    result, _ = process_work_request(
        change(inputs, fields=change(inputs.fields, request_title=(candidate,)))
    )
    assert result.data.request_title.state == "missing"
    assert ("unverified_source", "request_title") in codes(result)


def test_duplicate_workbooks_do_not_merge_or_choose(tmp_path):
    inputs = make_input(tmp_path)
    other = make_input(tmp_path, rows=[["W-99", "Другая работа", 1, "шт"]])
    p = other.sources[2].parsed
    inputs = change(
        inputs,
        sources=inputs.sources
        + (RegisteredSource(run_id="A", source_id="extra", parsed=p),),
        classifications=inputs.classifications + (other.classifications[2],),
        items=inputs.items + other.items,
    )
    result, _ = process_work_request(inputs)
    assert result.data.items == ()
    assert ("duplicate_document", "documents") in codes(result)
    assert ("required_missing", "items") in codes(result)


def test_foreign_row_and_reused_registration_id(tmp_path):
    inputs = make_input(tmp_path)
    wrong_row = change(inputs.items[0], document_id="foreign")
    result, _ = process_work_request(
        change(inputs, items=(wrong_row,) + inputs.items[1:])
    )
    assert result.data.items[0].item_code.state == "missing"
    assert ("unverified_source", "items") in codes(result)
    source = change(
        inputs.sources[1], run_id="foreign", source_id=inputs.sources[0].source_id
    )
    result, _ = process_work_request(
        change(inputs, sources=(inputs.sources[0], source, inputs.sources[2]))
    )
    assert result.processing_status == "failed"


@pytest.mark.parametrize(
    "operation",
    [
        "read_source",
        "create_draft",
        "modify_confirmed",
        "export_document",
        "execute_external_action",
    ],
)
def test_every_denial_has_a_nonterminal_audit_event(operation):
    decision, event = audit_operation(operation, run_id="test")
    assert not decision.allowed
    assert event.event == "operation_decision" and event.error_code is None
    assert event.decision == decision
    assert event.run_id == "test"


def test_metadata_cannot_authorize_export_and_denial_does_not_fail_processing(tmp_path):
    result, _ = process_work_request(make_input(tmp_path))
    result = change(
        result,
        generated_document=dict(
            run_id="A",
            filename="work_request.docx",
            sha256="a" * 64,
            template_sha256="b" * 64,
            needs_review=False,
        ),
    )
    decision, event = audit_operation("export_document", run_id="A", result=result)
    assert not decision.allowed
    updated = change(result, audit=result.audit + (event,))
    assert updated.processing_status == "completed" and not updated.needs_review


@pytest.mark.parametrize(
    "values",
    [
        dict(
            operation="execute_external_action",
            allowed=True,
            reason="prohibited_in_mvp",
        ),
        dict(operation="export_document", allowed=True, reason="processing_completed"),
        dict(operation="read_source", allowed=False, reason="source_registered"),
    ],
)
def test_forged_decision_is_rejected(values):
    with pytest.raises(ValidationError):
        OperationDecision(**values)


def test_audit_details_exclusive_and_compatible():
    base = dict(
        run_id="test",
        timestamp=datetime.now().astimezone(),
        stage="review",
        message="test",
    )
    decision = check_operation("modify_confirmed", run_id="test")
    with pytest.raises(ValidationError):
        AuditEvent(**base, event="operation_decision")
    with pytest.raises(ValidationError):
        AuditEvent(**base, event="completed", decision=decision)
    assert AuditEvent(**base, event="completed").decision is None


@pytest.mark.parametrize("index", [0, 2])
def test_repeated_identical_document_is_a_business_duplicate(tmp_path, index):
    inputs = make_input(tmp_path)
    extra = change(inputs.sources[index], source_id="same-content-again")
    result, _ = process_work_request(change(inputs, sources=inputs.sources + (extra,)))
    assert result.processing_status == "completed"
    assert ("duplicate_document", "documents") in codes(result)
    if index == 2:
        assert not result.data.items


def test_inconsistent_same_identity_fails(tmp_path):
    inputs = make_input(tmp_path)
    p = inputs.sources[0].parsed
    other = pdf_snapshot("Карточка заказчика\nЗаказчик: Другой")
    f = change(
        other.fragments[0],
        document_id=p.document.document_id,
        fragment_id=p.fragments[0].fragment_id,
    )
    other = change(
        other,
        document=p.document,
        fragments=(f,),
        blocks=(change(other.blocks[0], fragment_id=f.fragment_id),),
    )
    source = RegisteredSource(run_id="A", source_id="inconsistent", parsed=other)
    result, events = process_work_request(
        change(inputs, sources=inputs.sources + (source,))
    )
    assert result.processing_status == "failed"
    assert any(e.error_code == "inconsistent_source_identity" for e in events)


def test_no_io_or_external_effects_during_processing_and_decisions(
    tmp_path, monkeypatch
):
    import builtins
    import socket
    from pathlib import Path

    inputs = make_input(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("Stage 3A must not perform I/O")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    result, _ = process_work_request(inputs)
    for operation in ("modify_confirmed", "export_document", "execute_external_action"):
        assert not audit_operation(operation, run_id="A", result=result)[0].allowed


def test_unknown_operation_is_explicitly_rejected():
    with pytest.raises(ValidationError):
        check_operation("invented_operation", run_id="test")


def test_scenario_a_with_real_pdf_docx_xlsx(tmp_path):
    path = Path(__file__).parent / "fixtures" / "business" / "customer.pdf"
    before = path.read_bytes()
    parsed = parse_pdf(path)
    assert parsed.status == "completed" and not parsed.diagnostics
    assert parsed.document.sha256 == sha256(before).hexdigest()
    assert len(parsed.fragments) == 1
    source = parsed.fragments[0]
    assert source.location.page == 1
    assert source.text.splitlines() == [
        "Карточка заказчика",
        "Заказчик: ООО «Демо Офис»",
        "Контактный email: office@example.com",
    ]
    inputs = make_input(tmp_path, parsed_pdf=parsed)
    assert all(s.parsed.status == "completed" for s in inputs.sources)
    result, _ = process_work_request(inputs)
    assert result.processing_status == "completed"
    assert not result.needs_review and not result.issues
    customer = result.data.customer_name
    assert customer.state == "found" and customer.value == "ООО «Демо Офис»"
    assert len(customer.candidates) == 2
    assert all(c.verified for c in customer.candidates)
    assert {c.source.location.format for c in customer.candidates} == {"pdf", "docx"}
    assert (
        next(c.source for c in customer.candidates if c.source.location.format == "pdf")
        == source
    )
    assert result.data.contact_email.value == "office@example.com"
    assert not any(issue.code == "unverified_source" for issue in result.issues)
    assert path.read_bytes() == before
