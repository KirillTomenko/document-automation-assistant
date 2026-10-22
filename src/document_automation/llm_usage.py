"""Provider-independent, privacy-minimal usage and estimated price contracts."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_serializer,
    model_validator,
)

from document_automation.contracts import ContractModel

Identifier = Annotated[str, Field(min_length=1, max_length=200)]
Nonnegative = Annotated[Decimal, Field(ge=0)]


class Tariff(ContractModel):
    provider: Identifier
    model: Identifier
    version: Identifier
    currency: Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
    source: Annotated[str, Field(min_length=1, max_length=500)]
    as_of: date
    input_per_million: Nonnegative
    output_per_million: Nonnegative
    cached_input_per_million: Nonnegative | None = None


class TariffCatalog(ContractModel):
    tariffs: tuple[Tariff, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_pairs(self):
        pairs = [(t.provider, t.model) for t in self.tariffs]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Only one active tariff per provider/model")
        return self

    def find(self, provider, model):
        return next(
            (t for t in self.tariffs if (t.provider, t.model) == (provider, model)),
            None,
        )


class CachedUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    cached_tokens: int | None = Field(default=None, ge=0)


class ConfirmedUsage(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    input_tokens_details: CachedUsage | None = None

    @model_validator(mode="after")
    def consistent(self):
        if (
            self.total_tokens is not None
            and self.total_tokens != self.input_tokens + self.output_tokens
        ):
            raise ValueError("Inconsistent usage total")
        if self.cached_tokens is not None and self.cached_tokens > self.input_tokens:
            raise ValueError("Cached tokens exceed input")
        return self

    @property
    def cached_tokens(self):
        return (
            self.input_tokens_details.cached_tokens
            if self.input_tokens_details
            else None
        )


def checked_usage(raw):
    if raw is None:
        return "missing", None
    try:
        return "valid", ConfirmedUsage.model_validate(raw)
    except ValidationError:
        return "invalid", None


def estimated_cost(usage, tariff):
    if usage is None or tariff is None:
        return None
    cached_price = tariff.cached_input_per_million
    cached = usage.cached_tokens
    if cached_price is None:
        # This configured tariff explicitly applies its input price to all input.
        inputs = usage.input_tokens * tariff.input_per_million
    elif (
        cached is None
        and usage.input_tokens
        and cached_price != tariff.input_per_million
    ):
        return None
    else:
        cached = cached or 0
        inputs = (
            usage.input_tokens - cached
        ) * tariff.input_per_million + cached * cached_price
    return (inputs + usage.output_tokens * tariff.output_per_million) / Decimal(
        1_000_000
    )


class RunMeter(ContractModel):
    format_version: Literal[1] = 1
    run_id: Identifier
    mode: Literal["offline", "llm", "untracked"]
    provider: Identifier | None = None
    model: Identifier | None = None
    data_origin: Literal["offline", "provider_response", "simulated", "untracked"]

    @model_validator(mode="after")
    def identity(self):
        if self.mode == "llm" and (self.provider is None or self.model is None):
            raise ValueError("LLM metering requires provider/model")
        return self


class AttemptRecord(ContractModel):
    format_version: Literal[1] = 1
    run_id: Identifier
    provider: Identifier
    model: Identifier
    attempt_no: int = Field(ge=1)
    started_at: AwareDatetime
    finished_at: AwareDatetime | None = None
    status: Literal["started", "succeeded", "failed", "interrupted"] = "started"
    error_code: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")] | None = None
    request_state: Literal[
        "unknown", "not_sent", "possibly_sent", "response_received"
    ] = "unknown"
    http_status: int | None = Field(default=None, ge=100, le=599)
    usage_status: Literal["missing", "invalid", "valid"] = "missing"
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    tariff: Tariff | None = None
    estimated_cost: Nonnegative | None = None

    @model_serializer(mode="wrap")
    def compatible_observation(self, handler):
        data = handler(self)
        if self.http_status is None:
            data.pop("http_status", None)
        return data

    @property
    def outcome_unknown(self):
        if self.request_state == "not_sent":
            return False
        if self.status in ("started", "interrupted") or self.request_state in (
            "unknown",
            "possibly_sent",
        ):
            return True
        if self.error_code in (
            "extraction_timeout",
            "attempt_cancelled",
            "extraction_internal_error",
            "extraction_output_limit",
            "openai_incomplete",
        ):
            return True
        if self.http_status == 408 or (
            self.http_status is not None and self.http_status >= 500
        ):
            return True
        return self.error_code == "openai_unavailable" and self.http_status in (
            None,
            200,
        )

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "started") != (self.finished_at is None):
            raise ValueError("Terminal attempt requires finish time")
        if self.usage_status == "valid":
            if self.input_tokens is None or self.output_tokens is None:
                raise ValueError("Valid usage requires confirmed counts")
            if (
                self.cached_tokens is not None
                and self.cached_tokens > self.input_tokens
            ):
                raise ValueError("Invalid cached count")
        elif any(
            v is not None
            for v in (self.input_tokens, self.output_tokens, self.cached_tokens)
        ):
            raise ValueError("Unknown usage cannot contain confirmed tokens")
        if self.tariff and (self.tariff.provider, self.tariff.model) != (
            self.provider,
            self.model,
        ):
            raise ValueError("Tariff identity mismatch")
        usage = None
        if self.usage_status == "valid":
            usage = ConfirmedUsage(
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                input_tokens_details=CachedUsage(cached_tokens=self.cached_tokens),
            )
        expected = (
            Decimal(0)
            if self.request_state == "not_sent"
            else estimated_cost(usage, self.tariff)
        )
        if self.estimated_cost != expected:
            raise ValueError("Estimate disagrees with confirmed usage and tariff")
        if self.status == "started" and self.usage_status != "missing":
            raise ValueError("Started attempt cannot contain completed usage")
        return self


class UsageSummary(ContractModel):
    run_id: Identifier
    mode: Literal["offline", "llm", "untracked", "legacy_untracked"]
    provider: Identifier | None = None
    model: Identifier | None = None
    data_origin: Literal["offline", "provider_response", "simulated", "untracked"] = (
        "untracked"
    )
    attempts: int | None = None
    retry_calls: int | None = None
    response_received_calls: int | None = None
    uncertain_calls: int | None = None
    confirmed_input_tokens: int | None = None
    confirmed_output_tokens: int | None = None
    confirmed_cached_tokens: int | None = None
    missing_usage_attempts: int = 0
    invalid_usage_attempts: int = 0
    unknown_cost_attempts: int = 0
    unknown_outcome_attempts: int = 0
    known_estimated_costs: dict[str, Decimal] = Field(default_factory=dict)
    estimated_llm_api_cost: Decimal | None = None
    currency: str | None = None
    has_unknown_cost: bool = True
    tariff_versions: tuple[str, ...] = ()


def summarize_usage(run_id, meter, attempts):
    if meter is None or meter.mode == "untracked":
        return UsageSummary(
            run_id=run_id, mode="legacy_untracked" if meter is None else "untracked"
        )
    known, versions = {}, set()
    for record in attempts:
        if record.tariff:
            versions.add(record.tariff.version)
        if record.estimated_cost is not None and record.tariff:
            currency = record.tariff.currency
            known[currency] = known.get(currency, Decimal(0)) + record.estimated_cost
    unknown = sum(a.estimated_cost is None for a in attempts)
    total = None if unknown or len(known) > 1 else sum(known.values(), Decimal(0))
    cached = [a.cached_tokens for a in attempts if a.cached_tokens is not None]
    return UsageSummary(
        run_id=run_id,
        mode=meter.mode,
        provider=meter.provider,
        model=meter.model,
        data_origin=meter.data_origin,
        attempts=len(attempts),
        retry_calls=sum(
            a.attempt_no > 1 and a.request_state != "not_sent" for a in attempts
        ),
        response_received_calls=sum(
            a.request_state == "response_received" for a in attempts
        ),
        uncertain_calls=sum(
            a.request_state in ("unknown", "possibly_sent") for a in attempts
        ),
        confirmed_input_tokens=sum(a.input_tokens or 0 for a in attempts),
        confirmed_output_tokens=sum(a.output_tokens or 0 for a in attempts),
        confirmed_cached_tokens=sum(cached) if cached else None,
        missing_usage_attempts=sum(a.usage_status == "missing" for a in attempts),
        invalid_usage_attempts=sum(a.usage_status == "invalid" for a in attempts),
        unknown_cost_attempts=unknown,
        unknown_outcome_attempts=sum(a.outcome_unknown for a in attempts),
        known_estimated_costs=known,
        estimated_llm_api_cost=total,
        currency=next(iter(known)) if len(known) == 1 else None,
        has_unknown_cost=bool(unknown),
        tariff_versions=tuple(sorted(versions)),
    )
