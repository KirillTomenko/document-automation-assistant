"""Opt-in, bounded A or A/V experiment. Default transport is in-memory."""

import argparse
import asyncio
import json
import os
import re
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from document_automation.adapters.openai_extractor import (
    PROXY_BASE_URL,
    OpenAIExtractor,
)
from document_automation.extraction import (
    DemoExtractor,
    Extraction,
    ExtractionError,
    ExtractionLimits,
    process_paths,
)
from document_automation.generation import (
    generate_document,
    generate_verification_report,
    verify_document,
    verify_verification_report,
)

MODEL = "openai/gpt-4.1-mini"
_SAFE_VALIDATOR_CODES = {
    "item candidate requires source row": "candidate_item_row_scope",
    "scalar candidate cannot have row": "candidate_scalar_row_scope",
}
INPUT_LIMIT = 32_000
OUTPUT_LIMIT = 8_000
CALL_LIMIT = 2
TOTAL_SECONDS = 150
BUDGET = Decimal("20")
A_CALL_LIMIT = 1
A_BUDGET = Decimal("10")
V_CALL_LIMIT = 1
V_BUDGET = Decimal("10")
SCENARIOS = {"A": ("A",), "V": ("V",), "A+V": ("A", "V")}
# Public ProxyAPI catalogue, checked 2026-10-18. Recheck before paid execution.
INPUT_PRICE = Decimal("104")
OUTPUT_PRICE = Decimal("413")
LIMITS = ExtractionLimits(
    attempts=1, max_output_tokens=OUTPUT_LIMIT, timeout_seconds=60, total_seconds=60
)


def cost(input_tokens, output_tokens):
    # No cache discounts assumed; reserve against the full input price.
    return (input_tokens * INPUT_PRICE + output_tokens * OUTPUT_PRICE) / 1_000_000


def input_bound(body):
    """Conservative byte-BPE upper estimate, including schema and 1024 framing tokens.

    GPT-4.1 byte BPE cannot use more text tokens than UTF-8 bytes. Count the
    instructions, input text and schema without double-counting JSON escaping.
    Add 1024 tokens for role markers and Responses framing.
    This is a local guard, not a provider-side max_input_tokens parameter.
    """
    text = body["instructions"] + json.dumps(body["text"], ensure_ascii=False)
    text += "".join(c["text"] for m in body["input"] for c in m["content"])
    return len(text.encode("utf-8")) + 1024


class Usage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)

    @model_validator(mode="after")
    def consistent(self):
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("Inconsistent usage")
        return self


class BoundedExtractor(OpenAIExtractor):
    """One serial experiment; reserve before send, stop on any uncertain outcome."""

    def __init__(
        self,
        key,
        *,
        call_limit=CALL_LIMIT,
        budget=BUDGET,
        transport=None,
        clock=time.monotonic,
    ):
        super().__init__(
            key, MODEL, allow_paid=True, transport=transport, base_url=PROXY_BASE_URL
        )
        self.clock = clock
        self.deadline = clock() + TOTAL_SECONDS
        self.call_limit = call_limit
        self.budget = budget
        self.calls = 0
        self.spent = Decimal(0)
        self.stopped = False
        self.records = []

    async def extract(self, payload, limits):
        if self.stopped or self.calls >= self.call_limit:
            raise ExtractionError("smoke_call_limit")
        bound = input_bound(self.request_body(payload, LIMITS))
        reserve = cost(bound, OUTPUT_LIMIT)
        if bound > INPUT_LIMIT:
            self.stopped = True
            raise ExtractionError("smoke_input_limit")
        if self.spent + reserve > self.budget:
            self.stopped = True
            raise ExtractionError("smoke_budget_limit")
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            self.stopped = True
            raise ExtractionError("smoke_total_timeout")
        self.calls += 1  # A sent request consumes its slot even on error/timeout.
        record = {
            "call": self.calls,
            "input_token_upper_estimate": bound,
            "reserved_rub": str(reserve),
            "usage": None,
            "estimated_actual_rub": None,
        }
        self.records.append(record)
        self.usage = None
        try:
            try:
                async with asyncio.timeout(min(60, remaining)):
                    output = await super().extract(payload, LIMITS)
            except TimeoutError:
                record["failure_stage"] = "responses_timeout"
                self.stopped = True
                raise ExtractionError("smoke_timeout") from None
            except ExtractionError as exc:
                record["failure_stage"] = _adapter_failure_stage(exc.code)
                record["adapter_error_code"] = exc.code
                self.stopped = True
                raise
            try:
                Extraction.model_validate_json(output)
            except ValidationError as exc:
                record["failure_stage"] = "structured_output_validation"
                record["validation_errors"] = _safe_validation_errors(exc)
                self.stopped = True
                raise ExtractionError("smoke_schema_validation") from None
            return output
        except ValidationError:
            # Defensive: only adapter envelope parsing should reach this block.
            record["failure_stage"] = "response_decoding"
            self.stopped = True
            raise ExtractionError("smoke_response_decoding") from None
        except BaseException:
            record.setdefault("failure_stage", "cancelled_or_internal_error")
            self.stopped = True
            raise
        finally:
            try:
                usage = Usage.model_validate(self.usage)
            except ValidationError:
                # Unknown billed cost: conservatively consume reservation and
                # stop. Never claim that a missing response means a free call.
                self.spent += reserve
                self.stopped = True
                record["usage_unknown"] = True
                raise ExtractionError("smoke_usage_unknown") from None
            amount = cost(usage.input_tokens, usage.output_tokens)
            self.spent += amount
            record["usage"] = usage.model_dump()
            record["estimated_actual_rub"] = str(amount)
            if (
                usage.input_tokens > bound
                or usage.input_tokens > INPUT_LIMIT
                or usage.output_tokens > OUTPUT_LIMIT
                or self.spent >= self.budget
            ):
                self.stopped = True
                raise ExtractionError("smoke_usage_or_budget_limit")


def _adapter_failure_stage(code):
    if code == "extraction_timeout":
        return "responses_timeout"
    if code == "openai_unavailable":
        return "responses_api"
    if code in {
        "openai_incomplete",
        "openai_unexpected_output",
        "openai_refusal",
        "openai_invalid_output",
        "openai_secret_in_output",
        "extraction_output_limit",
    }:
        return "response_decoding"
    return "adapter_error"


def _safe_validation_errors(exc):
    """Return schema locations/types only; never persist rejected values or input."""
    safe = []
    for error in exc.errors(
        include_url=False, include_context=True, include_input=False
    )[:10]:
        parts = []
        for part in error.get("loc", ()):
            if isinstance(part, int):
                parts.append(str(part))
            elif isinstance(part, str) and re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", part
            ):
                parts.append(part)
        diagnostic = {
            "path": ".".join(parts),
            "type": str(error.get("type", "unknown"))[:64],
        }
        # Pydantic's context contains the rejected exception and its message.
        # Match only known static validator messages; never copy context to disk.
        context = error.get("ctx") or {}
        validator_error = context.get("error")
        validator_code = (
            _SAFE_VALIDATOR_CODES.get(str(validator_error))
            if isinstance(validator_error, ValueError)
            else None
        )
        if validator_code:
            diagnostic["validator_code"] = validator_code
        safe.append(diagnostic)
    return safe


async def mock_response(request):
    body = json.loads(request.content)
    payload = body["input"][0]["content"][0]["text"]
    output = await DemoExtractor().extract(payload, LIMITS)
    inputs, outputs = input_bound(body), len(output.encode())
    return httpx.Response(
        200,
        json={
            "status": "completed",
            "usage": dict(
                input_tokens=inputs,
                output_tokens=outputs,
                total_tokens=inputs + outputs,
            ),
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": output}],
                }
            ],
        },
    )


def business_signature(result):
    """Compare existing field states/values and issues, not provider wording/audit."""
    if result.data is None:
        return None
    data = result.data.model_dump(mode="json")
    return {
        "fields": {
            k: (v["state"], v["value"]) for k, v in data.items() if k != "items"
        },
        "items": [
            {k: (v["state"], v["value"]) for k, v in row.items()}
            for row in data["items"]
        ],
        "issues": sorted((i.path, i.code) for i in result.issues),
        "needs_review": result.needs_review,
    }


async def run_experiment(
    root, output_dir, *, scenario="A+V", real=False, paid_confirmation=None
):
    if scenario not in SCENARIOS:
        raise ValueError("Unsupported experiment scenario")
    call_limit, budget = {
        "A": (A_CALL_LIMIT, A_BUDGET),
        "V": (V_CALL_LIMIT, V_BUDGET),
        "A+V": (CALL_LIMIT, BUDGET),
    }[scenario]
    # Dry-run never reads a real key and cannot access a network transport.
    if real:
        if paid_confirmation != scenario:
            raise ValueError("Paid confirmation does not match selected scenario")
        key = os.environ.get("OPENAI_API_KEY", "")
        if (
            os.environ.get("DAA_ALLOW_PAID", "").lower() != "true"
            or len(key) < 16
            or any(c.isspace() for c in key)
            or os.environ.get("OPENAI_BASE_URL") != PROXY_BASE_URL
            or os.environ.get("OPENAI_MODEL") != MODEL
        ):
            raise ValueError("Paid experiment configuration is not explicitly enabled")
        transport = None
    else:
        key, transport = "synthetic-mock-key", httpx.MockTransport(mock_response)
    adapter = BoundedExtractor(
        key, call_limit=call_limit, budget=budget, transport=transport
    )
    output_dir.mkdir(parents=True, exist_ok=False)
    summary = {
        "mode": "real" if real else "dry-run/mock",
        "usage_kind": "provider-reported" if real else "simulated; not billed",
        "model": MODEL,
        "scenario_mode": scenario,
        "call_limit": call_limit,
        "budget_rub": str(budget),
        "prices_rub_per_million": [str(INPUT_PRICE), str(OUTPUT_PRICE)],
        "prices_checked": "2026-10-18; recheck before paid run",
        "scenarios": [],
        "calls": adapter.records,
    }
    try:
        async with asyncio.timeout(TOTAL_SECONDS):
            for case in SCENARIOS[scenario]:
                files = tuple(
                    root / "demo" / case / f
                    for f in ("customer.pdf", "description.docx", "items.xlsx")
                )
                run_id = uuid4().hex
                result = await process_paths(files, run_id, adapter, LIMITS)
                entry = {
                    "scenario": case,
                    "run_id": run_id,
                    "processing_status": result.processing_status,
                    "needs_review": result.needs_review,
                }
                summary["scenarios"].append(entry)
                if result.processing_status == "failed" or adapter.stopped:
                    entry["error_code"] = result.audit[-1].error_code
                    break
                expected = await process_paths(files, run_id, DemoExtractor(), LIMITS)
                entry["expected_business_match"] = business_signature(
                    result
                ) == business_signature(expected)
                # Both artifacts use this same result; no additional extraction.
                result, work = generate_document(result)
                if not verify_document(result, work):
                    raise ValueError("Work artifact verification failed")
                result, report = generate_verification_report(result)
                if not verify_verification_report(result, report):
                    raise ValueError("Report artifact verification failed")
                serialized = result.model_dump_json(indent=2)
                if key in serialized:
                    raise ValueError("Secret in experiment output")
                (output_dir / f"{case}-result.json").write_text(
                    serialized, encoding="utf-8"
                )
                (output_dir / f"{case}-work.docx").write_bytes(work)
                (output_dir / f"{case}-report.docx").write_bytes(report)
                if not entry["expected_business_match"]:
                    break
    except Exception:
        # Never serialize arbitrary exception text, headers, response bodies or env.
        summary["error"] = "experiment_stopped"
    finally:
        summary["accounted_rub"] = str(adapter.spent)
        (output_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=tuple(SCENARIOS),
        default="A+V",
        help="Synthetic set to run (default A+V; use A for the bounded single test)",
    )
    confirmation = parser.add_mutually_exclusive_group()
    confirmation.add_argument("--confirm-paid-a", action="store_true")
    confirmation.add_argument("--confirm-paid-v", action="store_true")
    confirmation.add_argument("--confirm-paid-a-v", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    directory = root / "tmp" / "proxyapi-smoke" / uuid4().hex
    try:
        summary = asyncio.run(
            run_experiment(
                root,
                directory,
                scenario=args.scenario,
                real=(
                    args.confirm_paid_a or args.confirm_paid_v or args.confirm_paid_a_v
                ),
                paid_confirmation=(
                    "A"
                    if args.confirm_paid_a
                    else "V"
                    if args.confirm_paid_v
                    else "A+V"
                    if args.confirm_paid_a_v
                    else None
                ),
            )
        )
    except Exception:
        print("Experiment not started: check configuration and synthetic fixtures.")
        return 1
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("Artifacts:", directory)
    return (
        0
        if (
            "error" not in summary
            and len(summary["scenarios"]) == len(SCENARIOS[args.scenario])
            and all(s.get("expected_business_match") for s in summary["scenarios"])
        )
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
