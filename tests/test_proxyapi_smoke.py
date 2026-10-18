import asyncio
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from document_automation import proxyapi_smoke as smoke
from document_automation.adapters.openai_extractor import (
    DEFAULT_BASE_URL,
    PROXY_BASE_URL,
    OpenAIExtractor,
    checked_base_url,
)
from document_automation.extraction import ExtractionError, ExtractionLimits
from document_automation.service import configured_extractor

ROOT = Path(__file__).resolve().parents[1]
SECRET = "synthetic-key-not-a-real-secret"


@pytest.mark.parametrize(
    "url", [DEFAULT_BASE_URL, PROXY_BASE_URL, PROXY_BASE_URL + "/"]
)
def test_exact_allowed_urls(url):
    assert checked_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://api.proxyapi.ru/v1",
        "https://evil.example/v1",
        "https://api.proxyapi.ru.evil.example/v1",
        "https://api.proxyapi.ru:443/v1",
        "https://user:password@api.proxyapi.ru/v1",
        PROXY_BASE_URL + "?key=" + SECRET,
        PROXY_BASE_URL + "#fragment",
        "https://api.proxyapi.ru/v1/../evil",
        "https://api.proxyapi.ru/%76%31",
        "https://api.proxyapi.ru/openai/v1",
        "//api.proxyapi.ru/v1",
        " " + PROXY_BASE_URL,
        "",
        None,
    ],
)
def test_destination_rejected_without_echo(url):
    with pytest.raises(ValueError, match="^Unsupported API base URL$"):
        OpenAIExtractor(SECRET, "model", base_url=url)


def test_environment_configuration(monkeypatch):
    monkeypatch.setenv("DAA_MODE", "openai")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert configured_extractor().base_url == DEFAULT_BASE_URL
    monkeypatch.setenv("OPENAI_BASE_URL", PROXY_BASE_URL)
    monkeypatch.setenv("OPENAI_MODEL", smoke.MODEL)
    adapter = configured_extractor()
    assert adapter.base_url == PROXY_BASE_URL and adapter.model == smoke.MODEL
    assert not adapter.allow_paid


def envelope(**changes):
    data = dict(
        status="completed",
        usage=dict(input_tokens=100, output_tokens=50, total_tokens=150),
        output=[
            dict(
                type="message",
                content=[
                    dict(
                        type="output_text",
                        text='{"classifications":[],"candidates":[]}',
                    )
                ],
            )
        ],
    )
    data.update(changes)
    return data


def bounded(handler):
    return smoke.BoundedExtractor(SECRET, transport=httpx.MockTransport(handler))


def test_two_requests_and_exact_contract_no_hidden_retries():
    requests = []

    def handler(request):
        requests.append(request)
        assert str(request.url) == PROXY_BASE_URL + "/responses"
        assert request.headers["authorization"] == "Bearer " + SECRET
        body = json.loads(request.content)
        assert body["model"] == smoke.MODEL
        assert body["max_output_tokens"] == 8000
        assert body["tools"] == [] and body["store"] is False
        assert body["text"]["format"]["strict"] is True
        assert body["text"]["format"]["schema"]["additionalProperties"] is False
        assert SECRET not in request.content.decode()
        return httpx.Response(200, json=envelope())

    adapter = bounded(handler)

    async def exercise():
        for _ in range(2):
            await adapter.extract(
                "{}", ExtractionLimits(attempts=3, max_output_tokens=16000)
            )
        with pytest.raises(ExtractionError, match="smoke_call_limit"):
            await adapter.extract("{}", smoke.LIMITS)

    asyncio.run(exercise())
    assert len(requests) == adapter.calls == 2
    assert adapter.spent == 2 * smoke.cost(100, 50)


@pytest.mark.parametrize(
    "condition,error",
    [
        ("budget", "smoke_budget_limit"),
        ("input", "smoke_input_limit"),
        ("time", "smoke_total_timeout"),
    ],
)
def test_preflight_rejects_without_transport(condition, error):
    def forbidden(request):
        pytest.fail("No request may be sent")

    adapter = bounded(forbidden)
    payload = "{}"
    if condition == "budget":
        adapter.spent = Decimal("19.99")
    elif condition == "time":
        adapter.deadline = 0
    else:
        payload = "x" * 32000
    with pytest.raises(ExtractionError, match=error):
        asyncio.run(adapter.extract(payload, smoke.LIMITS))
    assert adapter.calls == 0 and adapter.stopped


@pytest.mark.parametrize(
    "failure", ["401", "429", "500", "redirect", "timeout", "transport", "json"]
)
def test_errors_stop_and_do_not_leak_secret(failure, caplog):
    requests = []

    def handler(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout(SECRET)
        if failure == "transport":
            raise httpx.ConnectError(SECRET)
        if failure == "redirect":
            return httpx.Response(
                307, headers={"location": "https://evil.example/"}, text=SECRET
            )
        return httpx.Response(200 if failure == "json" else int(failure), text=SECRET)

    adapter = bounded(handler)
    with pytest.raises(ExtractionError, match="smoke_usage_unknown") as exc:
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert SECRET not in str(exc.value) + caplog.text + json.dumps(adapter.records)
    assert adapter.stopped and adapter.spent > 0
    with pytest.raises(ExtractionError, match="smoke_call_limit"):
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert len(requests) == 1


def test_structured_output_validation_is_distinguished_and_diagnostic_is_redacted():
    private_value = "private-model-output-must-not-be-persisted"
    text = json.dumps({"classifications": private_value, "candidates": []})
    adapter = bounded(
        lambda r: httpx.Response(
            200,
            json=envelope(
                output=[
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ]
            ),
        )
    )
    with pytest.raises(ExtractionError, match="smoke_schema_validation"):
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    record = adapter.records[0]
    assert record["failure_stage"] == "structured_output_validation"
    assert record["validation_errors"] == [
        {"path": "classifications", "type": "tuple_type"}
    ]
    assert private_value not in json.dumps(record)
    assert "input" not in json.dumps(record["validation_errors"])


@pytest.mark.parametrize(
    "field,row,validator_code",
    [
        ("name", None, "candidate_item_row_scope"),
        ("name", 1, "candidate_item_row_scope"),
        ("name", 100001, "candidate_item_row_scope"),
        ("customer_name", 4, "candidate_scalar_row_scope"),
    ],
)
def test_candidate_row_scope_diagnostic_is_safe_and_schema_does_not_encode_it(
    field, row, validator_code
):
    from jsonschema import Draft202012Validator
    from pydantic import ValidationError

    private_value = "PRIVATE_CANDIDATE_VALUE"
    valid_scalar = {
        "field": "customer_name",
        "document_id": "doc",
        "fragment_id": "fragment",
        "raw_value": "safe synthetic text",
        "row": None,
    }
    invalid = {
        **valid_scalar,
        "field": field,
        "raw_value": private_value,
        "row": row,
    }
    payload = {
        "classifications": [],
        "candidates": [valid_scalar.copy() for _ in range(4)] + [invalid],
    }
    schema = smoke.Extraction.model_json_schema()
    Draft202012Validator(schema).validate(payload)

    with pytest.raises(ValidationError) as exc:
        smoke.Extraction.model_validate_json(json.dumps(payload))

    diagnostic = smoke._safe_validation_errors(exc.value)
    assert diagnostic == [
        {
            "path": "candidates.4",
            "type": "value_error",
            "validator_code": validator_code,
        }
    ]
    assert private_value not in json.dumps(diagnostic)
    assert "requires source row" not in json.dumps(diagnostic)
    assert "cannot have row" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "field,row,validator_code",
    [
        ("name", None, "candidate_item_row_scope"),
        ("customer_name", 4, "candidate_scalar_row_scope"),
    ],
)
def test_candidate_row_scope_summary_keeps_only_path_and_validator_code(
    tmp_path, monkeypatch, field, row, validator_code
):
    private_value = "PRIVATE_V_CANDIDATE_CONTENT"
    original = smoke.mock_response

    async def malformed_v(request):
        response = await original(request)
        body = response.json()
        output = json.loads(body["output"][0]["content"][0]["text"])
        assert len(output["candidates"]) > 4
        output["candidates"][4].update(field=field, raw_value=private_value, row=row)
        body["output"][0]["content"][0]["text"] = json.dumps(output)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(smoke, "mock_response", malformed_v)
    output_dir = tmp_path / "v-rejected"
    summary = asyncio.run(smoke.run_experiment(ROOT, output_dir, scenario="V"))

    assert summary["scenarios"][0]["processing_status"] == "failed"
    assert summary["scenarios"][0]["error_code"] == "smoke_schema_validation"
    assert summary["calls"][0]["validation_errors"] == [
        {
            "path": "candidates.4",
            "type": "value_error",
            "validator_code": validator_code,
        }
    ]
    assert private_value not in json.dumps(summary)
    assert len(summary["calls"]) == 1
    assert summary["calls"][0]["usage"] is not None
    assert not list(output_dir.glob("*.docx"))
    assert not list(output_dir.glob("*-result.json"))


def test_responses_mock_receives_explicit_candidate_row_instructions():
    requests = []

    def handler(request):
        requests.append(request)
        body = json.loads(request.content)
        instructions = body["instructions"]
        assert "name, quantity, unit, item_code require row" in instructions
        assert "JSON integer from 2 to 100000 inclusive" in instructions
        assert (
            "actual Excel row in the supplied XLSX fragment's cell coordinate"
            in instructions
        )
        assert (
            "customer_name, request_title, work_description, requested_completion_date, contact_email"
            in instructions
        )
        assert (
            "require row=null, even if the scalar value is sourced from Excel"
            in instructions
        )
        assert "Never guess or invent a row number" in instructions
        assert "use an ordinal item number, or renumber rows" in instructions
        assert (
            "If the supplied evidence does not establish a valid Excel row, do not create that item candidate"
            in instructions
        )
        assert body["text"]["format"]["schema"] == smoke.Extraction.model_json_schema()
        return httpx.Response(200, json=envelope())

    asyncio.run(bounded(handler).extract("{}", smoke.LIMITS))
    assert len(requests) == 1


def test_responses_request_schema_is_strict_and_json_schema_valid():
    from jsonschema import Draft202012Validator

    adapter = smoke.BoundedExtractor(SECRET)
    emitted = adapter.request_body("{}", smoke.LIMITS)["text"]["format"]
    schema = emitted["schema"]
    assert emitted["type"] == "json_schema" and emitted["strict"] is True
    assert schema == smoke.Extraction.model_json_schema()
    assert schema["type"] == "object"  # Structured Outputs root cannot be anyOf.
    Draft202012Validator.check_schema(schema)

    supported = {
        "$defs",
        "$ref",
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "anyOf",
        "title",
        "minLength",
        "maxLength",
        "pattern",
        "minItems",
        "maxItems",
    }
    pending = [schema]
    while pending:
        node = pending.pop()
        assert set(node).issubset(supported)
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False
            assert set(node.get("required", ())) == set(node.get("properties", {}))
            pending.extend(node["properties"].values())
        if "items" in node:
            pending.append(node["items"])
        pending.extend(node.get("anyOf", ()))
        if "$defs" in node:
            pending.extend(node["$defs"].values())


@pytest.mark.parametrize(
    "mutate,expected",
    [
        ("missing", {"path": "classifications", "type": "missing"}),
        ("wrong_type", {"path": "classifications", "type": "tuple_type"}),
        ("extra", {"path": "unexpected", "type": "extra_forbidden"}),
        ("candidate", {"path": "candidates.0", "type": "model_type"}),
    ],
)
def test_schema_failures_are_safe_charge_usage_and_block_both_docx(
    tmp_path, monkeypatch, mutate, expected
):
    private_text = "PRIVATE_DOCUMENT_RESPONSE_SENTINEL"

    async def response(request):
        body = json.loads(request.content)
        payload = body["input"][0]["content"][0]["text"]
        raw = json.loads(await smoke.DemoExtractor().extract(payload, smoke.LIMITS))
        if mutate == "missing":
            raw.pop("classifications")
        elif mutate == "wrong_type":
            raw["classifications"] = private_text
        elif mutate == "extra":
            raw["unexpected"] = private_text
        else:
            raw["candidates"][0] = private_text
        text = json.dumps(raw, ensure_ascii=False)
        input_tokens, output_tokens = 100, 2000
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ],
            },
        )

    bounded_type = smoke.BoundedExtractor

    def mock_only_bounded(key, *, call_limit, budget, transport=None):
        return bounded_type(
            key,
            call_limit=call_limit,
            budget=budget,
            transport=httpx.MockTransport(response),
        )

    monkeypatch.setattr(smoke, "BoundedExtractor", mock_only_bounded)
    output_dir = tmp_path / "schema-rejected"
    summary = asyncio.run(smoke.run_experiment(ROOT, output_dir, scenario="A"))

    assert summary["scenarios"][0]["processing_status"] == "failed"
    assert summary["scenarios"][0]["error_code"] == "smoke_schema_validation"
    call = summary["calls"][0]
    assert call["failure_stage"] == "structured_output_validation"
    assert expected in call["validation_errors"]
    assert call["usage"] == {
        "input_tokens": 100,
        "output_tokens": 2000,
        "total_tokens": 2100,
    }
    assert Decimal(call["estimated_actual_rub"]) == smoke.cost(100, 2000)
    assert summary["accounted_rub"] == str(smoke.cost(100, 2000))
    assert private_text not in json.dumps(summary)
    assert not list(output_dir.glob("*-result.json"))
    assert not list(output_dir.glob("*-work.docx"))
    assert not list(output_dir.glob("*-report.docx"))


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": True, "output_tokens": 0, "total_tokens": 1},
        {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0},
        {"input_tokens": 1, "output_tokens": 1, "total_tokens": 5},
    ],
)
def test_unknown_or_invalid_usage_stops(usage):
    adapter = bounded(lambda r: httpx.Response(200, json=envelope(usage=usage)))
    with pytest.raises(ExtractionError, match="smoke_usage_unknown"):
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert adapter.stopped and adapter.records[0]["usage"] is None


@pytest.mark.parametrize("inputs,outputs", [(32001, 1), (1, 8001), (200000, 1)])
def test_reported_limits_or_spend_exceeded(inputs, outputs):
    adapter = bounded(
        lambda r: httpx.Response(
            200,
            json=envelope(
                usage=dict(
                    input_tokens=inputs,
                    output_tokens=outputs,
                    total_tokens=inputs + outputs,
                )
            ),
        )
    )
    with pytest.raises(ExtractionError, match="smoke_usage_or_budget_limit"):
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert adapter.stopped and adapter.spent == smoke.cost(inputs, outputs)


@pytest.mark.parametrize(
    "data",
    [
        [],
        envelope(status="incomplete"),
        envelope(output=None),
        envelope(output=[None]),
        envelope(output=[dict(type="message", content=[None])]),
        envelope(output=[dict(type="message", content=[dict(type="refusal")])]),
        envelope(
            output=[
                dict(type="message", content=[dict(type="output_text", text=SECRET)])
            ]
        ),
        envelope(
            output=[
                dict(
                    type="message",
                    content=[dict(type="output_text", text='{"actions":[]}')],
                )
            ]
        ),
    ],
)
def test_response_schema_and_secret_echo_are_rejected(data, caplog):
    adapter = bounded(lambda r: httpx.Response(200, json=data))
    with pytest.raises(ExtractionError) as exc:
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert adapter.stopped
    assert SECRET not in str(exc.value) + caplog.text + json.dumps(adapter.records)


def test_cost_and_full_input_estimate():
    assert smoke.cost(32000, 8000) == Decimal("6.632")
    adapter = bounded(lambda r: None)
    body = adapter.request_body("привет", smoke.LIMITS)
    assert smoke.input_bound(body) > len(body["instructions"].encode()) + len(
        "привет".encode()
    )
    assert smoke.LIMITS.attempts == 1 and smoke.LIMITS.timeout_seconds == 60


def test_default_dry_run_actual_pipeline_artifacts_and_no_network(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("DAA_ALLOW_PAID", "true")

    async def forbidden(*args, **kwargs):
        pytest.fail("Default dry-run must never use network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "output"))
    assert summary["mode"] == "dry-run/mock" and len(summary["calls"]) == 2
    assert [s["needs_review"] for s in summary["scenarios"]] == [False, True]
    assert all(s["expected_business_match"] for s in summary["scenarios"])
    assert len(list((tmp_path / "output").glob("*.docx"))) == 4
    for file in (tmp_path / "output").glob("*.json"):
        assert SECRET not in file.read_text(encoding="utf-8")
    for scenario in ("A", "V"):
        data = json.loads(
            (tmp_path / "output" / f"{scenario}-result.json").read_text(
                encoding="utf-8"
            )
        )
        assert data["run_id"] == next(
            s["run_id"] for s in summary["scenarios"] if s["scenario"] == scenario
        )


def test_single_a_dry_run_is_one_slot_ten_ruble_cap_and_only_a(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    async def forbidden(*args, **kwargs):
        pytest.fail("Mock A mode must not use network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "a-only", scenario="A"))
    assert summary["mode"] == "dry-run/mock"
    assert summary["scenario_mode"] == "A"
    assert summary["call_limit"] == 1 and summary["budget_rub"] == "10"
    assert [s["scenario"] for s in summary["scenarios"]] == ["A"]
    assert len(summary["calls"]) == 1
    assert Decimal(summary["calls"][0]["reserved_rub"]) <= Decimal("10")
    assert summary["scenarios"][0]["expected_business_match"] is True
    assert len(list((tmp_path / "a-only").glob("*.docx"))) == 2


def test_single_a_cannot_send_second_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    adapter = smoke.BoundedExtractor(
        SECRET,
        call_limit=smoke.A_CALL_LIMIT,
        budget=smoke.A_BUDGET,
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await adapter.extract("{}", smoke.LIMITS)
        with pytest.raises(ExtractionError, match="smoke_call_limit"):
            await adapter.extract("{}", smoke.LIMITS)

    asyncio.run(exercise())
    assert adapter.calls == len(requests) == 1
    assert adapter.budget == Decimal("10")


def test_single_v_dry_run_is_one_slot_ten_ruble_cap_and_only_v(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    async def forbidden(*args, **kwargs):
        pytest.fail("Mock V mode must not use network")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "v-only", scenario="V"))
    assert summary["mode"] == "dry-run/mock"
    assert summary["scenario_mode"] == "V"
    assert summary["call_limit"] == 1 and summary["budget_rub"] == "10"
    assert [s["scenario"] for s in summary["scenarios"]] == ["V"]
    assert len(summary["calls"]) == 1
    assert Decimal(summary["calls"][0]["reserved_rub"]) <= Decimal("10")
    assert summary["scenarios"][0]["needs_review"] is True
    assert summary["scenarios"][0]["expected_business_match"] is True
    assert len(list((tmp_path / "v-only").glob("*.docx"))) == 2


def test_single_v_cannot_send_second_request():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=envelope())

    adapter = smoke.BoundedExtractor(
        SECRET,
        call_limit=smoke.V_CALL_LIMIT,
        budget=smoke.V_BUDGET,
        transport=httpx.MockTransport(handler),
    )

    async def exercise():
        await adapter.extract("{}", smoke.LIMITS)
        with pytest.raises(ExtractionError, match="smoke_call_limit"):
            await adapter.extract("{}", smoke.LIMITS)

    asyncio.run(exercise())
    assert adapter.calls == len(requests) == 1
    assert adapter.budget == Decimal("10")


def test_real_requires_both_confirmation_and_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("DAA_ALLOW_PAID", raising=False)
    with pytest.raises(ValueError, match="not explicitly enabled"):
        asyncio.run(
            smoke.run_experiment(
                ROOT, tmp_path / "output", real=True, paid_confirmation="A+V"
            )
        )
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize(
    "scenario,confirmation", [("A", "A+V"), ("V", "A"), ("A+V", "V")]
)
def test_real_confirmation_must_match_selected_scope(tmp_path, scenario, confirmation):
    with pytest.raises(ValueError, match="Paid confirmation does not match"):
        asyncio.run(
            smoke.run_experiment(
                ROOT,
                tmp_path / scenario,
                scenario=scenario,
                real=True,
                paid_confirmation=confirmation,
            )
        )
    assert not (tmp_path / scenario).exists()


def test_total_run_deadline_stops_without_exception_leak(tmp_path, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(1)
        raise ValueError(SECRET)

    monkeypatch.setattr(smoke, "process_paths", slow)
    monkeypatch.setattr(smoke, "TOTAL_SECONDS", 0.01)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "output"))
    assert summary["error"] == "experiment_stopped" and summary["calls"] == []
    assert SECRET not in json.dumps(summary)


def test_per_request_deadline_stops_pending_transport():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=envelope())

    adapter = bounded(slow)
    adapter.deadline = adapter.clock() + 0.5
    with pytest.raises(ExtractionError, match="smoke_usage_unknown"):
        asyncio.run(adapter.extract("{}", smoke.LIMITS))
    assert adapter.calls == 1 and adapter.stopped
    assert adapter.records[0]["failure_stage"] == "responses_timeout"


def test_pipeline_failure_stops_before_v(tmp_path, monkeypatch):
    async def error(request):
        return httpx.Response(500, text=SECRET)

    monkeypatch.setattr(smoke, "mock_response", error)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "output"))
    assert len(summary["scenarios"]) == len(summary["calls"]) == 1
    assert summary["scenarios"][0]["processing_status"] == "failed"
    assert not list((tmp_path / "output").glob("*.docx"))
    assert SECRET not in json.dumps(summary)


def test_valid_json_with_business_mismatch_stops_without_substitution(
    tmp_path, monkeypatch
):
    original = smoke.mock_response

    async def incomplete(request):
        response = await original(request)
        body = response.json()
        output = json.loads(body["output"][0]["content"][0]["text"])
        output["candidates"] = [
            c for c in output["candidates"] if c["field"] != "request_title"
        ]
        body["output"][0]["content"][0]["text"] = json.dumps(output)
        return httpx.Response(200, json=body)

    monkeypatch.setattr(smoke, "mock_response", incomplete)
    summary = asyncio.run(smoke.run_experiment(ROOT, tmp_path / "output"))
    assert len(summary["calls"]) == 1
    assert summary["scenarios"][0]["expected_business_match"] is False
    result = json.loads((tmp_path / "output/A-result.json").read_text(encoding="utf-8"))
    assert result["data"]["request_title"]["state"] == "missing"
