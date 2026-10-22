"""All-time local metrics from one SQLite read transaction, without document text."""

from contextlib import contextmanager
from decimal import Decimal
from typing import Literal

from document_automation.contracts import ContractModel
from document_automation.llm_usage import Tariff
from document_automation.storage import ResultStore, StorageIntegrityError


class BusinessMetrics(ContractModel):
    period: Literal["all_time"] = "all_time"
    total_runs: int
    completed_runs: int
    completed_share: Decimal | None
    failed_runs: int
    unfinished_runs: int
    needs_review_runs: int
    registered_inputs: int
    successful_inputs: int
    successful_input_share: Decimal | None
    runs_without_registered_inputs: int
    llm_attempts: int
    retry_calls: int
    unknown_cost_attempts: int
    untracked_runs: int
    simulated_runs: int
    known_estimated_costs: dict[str, Decimal]
    tariffs: tuple[Tariff, ...]
    accepted_results: int
    average_cost_per_accepted: Decimal | None
    average_currency: str | None
    average_unavailable_reason: str | None


class _SnapshotReader(ResultStore):
    def __init__(self, db):
        self.db = db

    @contextmanager
    def connection(self):
        yield self.db


def summarize_business(store):
    with store.connection() as db:
        db.execute("BEGIN")
        reader = _SnapshotReader(db)
        rows = db.execute(
            "SELECT run_id,snapshot IS NOT NULL FROM runs ORDER BY run_id"
        ).fetchall()
        total = len(rows)
        completed = failed = review = accepted = success = attempts = retries = 0
        unknown = untracked = simulated = inputs = without_inputs = 0
        known, tariff_records = {}, {}
        for run_id, saved in rows:
            count = db.execute(
                "SELECT count(*) FROM inputs WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            inputs += count
            without_inputs += count == 0
            request = reader.get_request(run_id)
            meter = reader.get_meter(run_id)
            records = reader.get_attempts(run_id)
            if records and (
                meter is None
                or meter.mode != "llm"
                or any(
                    (a.provider, a.model) != (meter.provider, meter.model)
                    for a in records
                )
            ):
                raise StorageIntegrityError("Inconsistent metrics ledger")
            untracked += meter is None or meter.mode == "untracked"
            simulated += bool(meter and meter.data_origin == "simulated")
            attempts += len(records)
            retries += sum(
                a.request_state != "not_sent"
                and (a.attempt_no > 1 or bool(request and request.retry_of))
                for a in records
            )
            unknown += sum(a.estimated_cost is None for a in records)
            for record in records:
                if record.tariff:
                    tariff_records[record.tariff.model_dump_json()] = record.tariff
                if record.estimated_cost is not None and record.tariff:
                    currency = record.tariff.currency
                    known[currency] = (
                        known.get(currency, Decimal(0)) + record.estimated_cost
                    )
            if not saved:
                continue
            result = reader.get(run_id)
            events = reader.acceptance_history(result)
            accepted += bool(events and events[-1].action == "accepted")
            review += result.needs_review
            failed += result.processing_status == "failed"
            if result.processing_status == "completed":
                completed += 1
                # A file succeeds only as part of a completed workflow. Count
                # uploads, including identical copies, rather than deduplicated docs.
                hashes = {d.sha256 for d in result.documents}
                registered = db.execute(
                    "SELECT sha256 FROM inputs WHERE run_id=?", (run_id,)
                ).fetchall()
                if any(h not in hashes for (h,) in registered):
                    raise StorageIntegrityError(
                        "Completed input missing result evidence"
                    )
                success += count
        reason = (
            "no_accepted_results"
            if not accepted
            else "unknown_cost"
            if unknown or untracked
            else "multiple_currencies"
            if len(known) > 1
            else None
        )
        return BusinessMetrics(
            total_runs=total,
            completed_runs=completed,
            completed_share=Decimal(completed) / total if total else None,
            failed_runs=failed,
            unfinished_runs=total - completed - failed,
            needs_review_runs=review,
            registered_inputs=inputs,
            successful_inputs=success,
            successful_input_share=Decimal(success) / inputs if inputs else None,
            runs_without_registered_inputs=without_inputs,
            llm_attempts=attempts,
            retry_calls=retries,
            unknown_cost_attempts=unknown,
            untracked_runs=untracked,
            simulated_runs=simulated,
            known_estimated_costs=dict(sorted(known.items())),
            tariffs=tuple(tariff_records[key] for key in sorted(tariff_records)),
            accepted_results=accepted,
            average_cost_per_accepted=None
            if reason
            else sum(known.values(), Decimal(0)) / accepted,
            average_currency=next(iter(known))
            if not reason and len(known) == 1
            else None,
            average_unavailable_reason=reason,
        )
