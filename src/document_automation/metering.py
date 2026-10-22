"""Durable attempt observation; does not decide retries or mutate business results."""

import asyncio
import re
from datetime import datetime, timezone
from decimal import Decimal

from pydantic import ValidationError

from document_automation.llm_usage import (
    AttemptRecord,
    RunMeter,
    checked_usage,
    estimated_cost,
)


def run_meter(run_id, extractor):
    if getattr(extractor, "is_llm", None) is False:
        return RunMeter(run_id=run_id, mode="offline", data_origin="offline")
    if getattr(extractor, "is_llm", None) is True:
        return RunMeter(
            run_id=run_id,
            mode="llm",
            provider=extractor.provider,
            model=extractor.model,
            data_origin=extractor.data_origin,
        )
    return RunMeter(run_id=run_id, mode="untracked", data_origin="untracked")


def safe_code(code):
    return (
        code
        if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code)
        else "extraction_error"
    )


def automatic_attempts(extractor, limits):
    # Only an explicitly offline implementation may repeat automatically. A new
    # paid attempt is an explicit new run; an unknown extractor is conservative.
    return limits.attempts if getattr(extractor, "is_llm", None) is False else 1


class AttemptJournal:
    def __init__(self, store, meter, extractor, tariffs):
        self.store, self.meter, self.extractor = store, meter, extractor
        self.tariff = tariffs.find(meter.provider, meter.model)
        self.current = None

    def begin(self, number):
        self.extractor.reset_observation()
        self.current = AttemptRecord(
            run_id=self.meter.run_id,
            provider=self.meter.provider,
            model=self.meter.model,
            attempt_no=number,
            started_at=datetime.now(timezone.utc),
            tariff=self.tariff,
        )
        self.store.start_attempt(self.current)

    def finish(self, error):
        state, usage = checked_usage(self.extractor.usage)
        request_state = self.extractor.request_state
        amount = (
            Decimal(0)
            if request_state == "not_sent"
            else estimated_cost(usage, self.tariff)
        )
        record = AttemptRecord.model_validate(
            {
                **self.current.model_dump(),
                "finished_at": datetime.now(timezone.utc),
                "status": "succeeded" if error is None else "failed",
                "error_code": safe_code(error) if error else None,
                "request_state": request_state,
                "http_status": getattr(self.extractor, "http_status", None),
                "usage_status": state,
                "input_tokens": usage.input_tokens if usage else None,
                "output_tokens": usage.output_tokens if usage else None,
                "cached_tokens": usage.cached_tokens if usage else None,
                "estimated_cost": amount,
            }
        )
        self.store.finish_attempt(record)

    def reject_last(self, code):
        if self.current:
            self.store.reject_attempt_output(
                self.meter.run_id, self.current.attempt_no, safe_code(code)
            )


async def extract_attempt(extractor, payload, limits, schema, journal, number):
    from document_automation.extraction import ExtractionError

    if journal:
        journal.begin(number)
    error = "extraction_internal_error"
    try:
        async with asyncio.timeout(limits.timeout_seconds):
            output = await extractor.extract(payload, limits)
        if len(output.encode()) > limits.max_output_bytes:
            raise ExtractionError("extraction_output_limit")
        result = schema.model_validate_json(output)
        error = None
        return result
    except ValidationError:
        error = "extraction_invalid_output"
        raise
    except TimeoutError:
        error = "extraction_timeout"
        raise
    except ExtractionError as exc:
        error = safe_code(exc.code)
        raise
    except asyncio.CancelledError:
        error = "attempt_cancelled"
        raise
    finally:
        if journal:
            journal.finish(error)
