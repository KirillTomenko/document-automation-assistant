"""Application-owned request identity and lifecycle, outside business snapshots."""

import json
import re
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from document_automation.contracts import ContractModel, Sha256
from document_automation.schemas import ProcessingResult

RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


def key_hash(key):
    if key is None:
        return None
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        raise ValueError("Invalid idempotency key")
    return sha256(key.encode("ascii")).hexdigest()


def content_fingerprint(files):
    # A multiset: repeated identical uploads remain significant, order does not.
    identity = sorted(
        (name, len(data), sha256(data).hexdigest()) for name, data in files
    )
    return sha256(
        json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def request_fingerprint(content_sha256, retry_of=None):
    return sha256(
        json.dumps(
            ["runs-request-v1", content_sha256, retry_of], separators=(",", ":")
        ).encode()
    ).hexdigest()


def streamlit_action(content_sha256, previous=None, retry_of=None):
    if retry_of is None and previous and previous["content_sha256"] == content_sha256:
        return previous
    fingerprint = request_fingerprint(content_sha256, retry_of)
    # Stable across Streamlit reruns/restarts. An explicit retry includes its parent.
    return dict(
        content_sha256=content_sha256, key="daa-ui-v1:" + fingerprint, retry_of=retry_of
    )


class RunRequest(ContractModel):
    format_version: Literal[1] = 1
    run_id: RunId
    key_sha256: Sha256 | None
    content_sha256: Sha256
    request_sha256: Sha256
    contract_version: Literal["1.0", "2.0"]
    retry_of: RunId | None = None
    created_at: AwareDatetime
    state: Literal["registered", "processing", "finished", "interrupted"] = "registered"


class PendingRun(ContractModel):
    run_id: RunId
    operation_status: Literal["registered", "processing"]
    code: Literal["run_in_progress"] = "run_in_progress"
    detail: str = "Обработка ещё выполняется. Повтор с тем же ключом не отправляет новый запрос к LLM."


class Submission(ContractModel):
    status_code: Literal[200, 201, 202]
    replayed: bool
    body: ProcessingResult | PendingRun


class OperationStatus(ContractModel):
    run_id: RunId
    state: Literal["registered", "processing", "finished", "interrupted"]
    contract_version: Literal["1.0", "2.0"]
    retry_of: RunId | None = None
    can_retry: bool
    retry_block_reason: str | None = None
