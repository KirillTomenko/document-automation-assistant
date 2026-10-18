import json
from datetime import date
from decimal import Decimal

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from document_automation.contracts import Text
from document_automation.schemas import (
    AuditEvent,
    ClassificationResult,
    FieldCandidate,
    FieldResult,
    GeneratedDocument,
    InputDocument,
    ProcessingResult,
    SourceFragment,
    ValidationIssue,
    WorkItem,
    WorkRequestData,
)


def parse(model, value):
    return model.model_validate_json(json.dumps(value, ensure_ascii=False))


def candidate(value="x", verified=True):
    return {
        "raw_value": str(value),
        "interpretations": [value],
        "verified": verified,
        "source": {
            "fragment_id": "f1",
            "document_id": "d1",
            "text": str(value),
            "location": {"format": "pdf", "page": 1},
        }
        if verified
        else None,
    }


@pytest.mark.parametrize("value", [1, True, [], {}, None, "", "   "])
def test_text_rejects_wrong_types_and_empty_values(value):
    with pytest.raises(ValidationError):
        parse(FieldCandidate[Text], candidate(value))


@pytest.mark.parametrize(
    "model,value",
    [
        (FieldCandidate[date], candidate("2026-11-20")),
        (FieldCandidate[Decimal], candidate("2.5")),
        (FieldCandidate[Text], candidate("x")),
    ],
)
def test_json_native_values_round_trip(model, value):
    result = parse(model, value)
    assert model.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "model,value",
    [
        (FieldCandidate[date], "2026-11-20"),
        (FieldCandidate[Decimal], "2.5"),
        (FieldCandidate[Decimal], 2.5),
        (FieldCandidate[Decimal], True),
    ],
)
def test_python_input_is_strict(model, value):
    raw = candidate(value)
    raw["interpretations"] = (value,)
    with pytest.raises(ValidationError):
        model.model_validate(raw)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity", True, "abc"])
def test_decimal_rejects_nonfinite_or_invalid_json(bad):
    with pytest.raises(ValidationError):
        parse(FieldCandidate[Decimal], candidate(bad))


def test_python_typed_values_are_accepted():
    source = SourceFragment.model_validate(candidate()["source"])
    value = FieldCandidate[Decimal](
        raw_value="x", interpretations=(Decimal("2.5"),), source=source, verified=True
    )
    assert value.interpretations == (Decimal("2.5"),)


@pytest.mark.parametrize(
    "state,value,candidates,reason",
    [
        ("found", "x", [candidate()], None),
        ("missing", None, [], None),
        ("missing", None, [candidate(verified=False)], None),
        ("ambiguous", None, [{**candidate(), "interpretations": ["x", "y"]}], None),
        ("ambiguous", None, [{**candidate(), "interpretations": []}], None),
        ("ambiguous", None, [candidate()], "duplicate_item_code"),
        ("conflict", None, [candidate("x"), candidate("y")], None),
    ],
)
def test_valid_field_states(state, value, candidates, reason):
    result = parse(
        FieldResult[Text],
        dict(state=state, value=value, candidates=candidates, ambiguity_reason=reason),
    )
    assert result.state == state


@pytest.mark.parametrize(
    "state,value,candidates,reason",
    [
        ("invented", None, [], None),
        ("found", None, [candidate()], None),
        ("found", "x", [], None),
        ("found", "x", [candidate(verified=False)], None),
        ("found", "y", [candidate()], None),
        ("found", "x", [candidate(), candidate("y")], None),
        ("found", "x", [{**candidate(), "interpretations": ["x", "y"]}], None),
        ("found", "x", [candidate()], "duplicate_item_code"),
        ("missing", "x", [], None),
        ("missing", None, [candidate()], None),
        ("ambiguous", "x", [candidate()], "duplicate_item_code"),
        ("ambiguous", None, [], None),
        ("ambiguous", None, [candidate()], None),
        ("ambiguous", None, [candidate(), candidate("y")], None),
        ("conflict", None, [candidate(), candidate()], None),
        ("conflict", "x", [candidate(), candidate("y")], None),
        ("conflict", None, [candidate(), candidate("y", verified=False)], None),
    ],
)
def test_invalid_field_invariants(state, value, candidates, reason):
    with pytest.raises(ValidationError):
        parse(
            FieldResult[Text],
            dict(
                state=state, value=value, candidates=candidates, ambiguity_reason=reason
            ),
        )


@pytest.mark.parametrize(
    "change",
    ["missing_source", "missing_text", "wrong_quote", "bad_page", "bad_format"],
)
def test_provenance_is_required_and_typed(change):
    raw = candidate()
    if change == "missing_source":
        raw["source"] = None
    if change == "missing_text":
        del raw["source"]["text"]
    if change == "wrong_quote":
        raw["source"]["text"] = "different"
    if change == "bad_page":
        raw["source"]["location"]["page"] = True
    if change == "bad_format":
        raw["source"]["location"]["format"] = "png"
    with pytest.raises(ValidationError):
        parse(FieldCandidate[Text], raw)


def test_no_silent_extra_fields_or_mutation():
    raw = candidate()
    raw["confidence"] = 1
    with pytest.raises(ValidationError):
        parse(FieldCandidate[Text], raw)
    result = parse(FieldCandidate[Text], candidate())
    with pytest.raises(ValidationError):
        result.verified = False


def test_unknown_classification_may_lack_evidence():
    result = ClassificationResult(
        document_id="d1", document_type="unknown", evidence=()
    )
    assert not result.evidence
    with pytest.raises(ValidationError):
        ClassificationResult(
            document_id="d1", document_type="customer_card", evidence=()
        )


@pytest.mark.parametrize("scenario", ["A", "B", "C", "T1", "T2"])
def test_processing_round_trip_and_json_schema(scenarios, scenario):
    raw = scenarios[scenario]["result"]
    result = parse(ProcessingResult, raw)
    assert ProcessingResult.model_validate_json(result.model_dump_json()) == result
    assert ProcessingResult.model_validate(result.model_dump()) == result
    for mode in ("validation", "serialization"):
        schema = ProcessingResult.model_json_schema(mode=mode)
        Draft202012Validator.check_schema(schema)
        payload = raw if mode == "validation" else json.loads(result.model_dump_json())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(payload)


@pytest.mark.parametrize(
    "model",
    [
        InputDocument,
        SourceFragment,
        ClassificationResult,
        FieldCandidate[Text],
        FieldCandidate[date],
        FieldCandidate[Decimal],
        FieldResult[Text],
        FieldResult[date],
        FieldResult[Decimal],
        WorkItem,
        WorkRequestData,
        ValidationIssue,
        ProcessingResult,
        GeneratedDocument,
        AuditEvent,
    ],
)
def test_all_public_json_schemas_are_valid(model):
    for mode in ("validation", "serialization"):
        schema = model.model_json_schema(mode=mode)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        json.dumps(schema)


@pytest.mark.parametrize(
    "change",
    ["missing_document", "wrong_format", "wrong_fragment", "wrong_source_type"],
)
def test_processing_provenance_references(happy, change):
    c = happy["data"]["request_title"]["candidates"][0]
    if change == "missing_document":
        c["source"]["document_id"] = "absent"
    if change == "wrong_format":
        c["source"]["location"] = {"format": "pdf", "page": 1}
    if change == "wrong_fragment":
        c["source"]["fragment_id"] = "docx:1"
    if change == "wrong_source_type":
        c["source"].update(
            document_id="pdf",
            fragment_id="pdf:title",
            location={"format": "pdf", "page": 1},
        )
    with pytest.raises(ValidationError):
        parse(ProcessingResult, happy)


def test_json_schema_is_not_a_replacement_for_semantic_validation():
    raw = {"state": "found", "value": "invented", "candidates": []}
    Draft202012Validator(FieldResult[Text].model_json_schema()).validate(raw)
    with pytest.raises(ValidationError):
        parse(FieldResult[Text], raw)


def test_structural_schema_rejects_bad_json_primitive(happy):
    happy["needs_review"] = "false"
    assert list(
        Draft202012Validator(ProcessingResult.model_json_schema()).iter_errors(happy)
    )
    with pytest.raises(ValidationError):
        parse(ProcessingResult, happy)


@pytest.mark.parametrize("value", ["20.11.2026", "2026-02-30", 123, True])
def test_schema_accepts_only_normalized_iso_json_dates(value):
    with pytest.raises(ValidationError):
        parse(FieldCandidate[date], candidate(value))


@pytest.mark.parametrize(
    "change", ["naive_time", "missing_model", "missing_error", "foreign_run"]
)
def test_audit_contract(happy, change):
    event = happy["audit"][0]
    if change == "naive_time":
        event["timestamp"] = "2026-10-16T12:00:00"
    if change == "missing_model":
        event["event"] = "model_used"
    if change == "missing_error":
        event["event"] = "failed"
    if change == "foreign_run":
        event["run_id"] = "other"
    with pytest.raises(ValidationError):
        parse(ProcessingResult, happy)


@pytest.mark.parametrize("change", ["bad_hash", "bad_format", "extra_field"])
def test_document_contract(happy, change):
    doc = happy["documents"][0]
    if change == "bad_hash":
        doc["sha256"] = "not-a-hash"
    if change == "bad_format":
        doc["format"] = "png"
    if change == "extra_field":
        doc["secret"] = "synthetic"
    with pytest.raises(ValidationError):
        parse(InputDocument, doc)


def test_classification_cannot_use_other_document_evidence(happy):
    classification = happy["classifications"][0]
    classification["evidence"][0]["document_id"] = "other"
    with pytest.raises(ValidationError):
        parse(ClassificationResult, classification)
