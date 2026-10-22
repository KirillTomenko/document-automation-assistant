"""Local staged publication without replacement; no shared filesystem/SQL transaction."""

import os
import stat
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, model_validator

from document_automation.contracts import ContractModel, Sha256
from document_automation.generation import verify_document, verify_verification_report
from document_automation.run_requests import RunId
from document_automation.schemas import GeneratedDocument, GeneratedReport

ArtifactKind = Literal["document", "report"]
FIELDS = {"document": "generated_document", "report": "generated_report"}
NAMES = {
    "document": ("work_request.docx", "work_request_needs_review.docx"),
    "report": ("verification_report.docx",),
}


class ArtifactProblem(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class ArtifactWrite(ContractModel):
    format_version: Literal[1] = 1
    operation_id: RunId
    run_id: RunId
    kind: ArtifactKind
    filename: Literal[
        "work_request.docx",
        "work_request_needs_review.docx",
        "verification_report.docx",
    ]
    result_sha256: Sha256
    expected_sha256: Sha256
    previous_metadata: GeneratedDocument | GeneratedReport | None = None
    created_at: AwareDatetime
    state: Literal["prepared", "committed", "abandoned"] = "prepared"
    finished_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def consistency(self):
        if self.filename not in NAMES[self.kind]:
            raise ValueError("Artifact kind/name mismatch")
        if self.previous_metadata is not None and (
            self.previous_metadata.run_id != self.run_id
            or self.previous_metadata.filename not in NAMES[self.kind]
        ):
            raise ValueError("Foreign previous artifact")
        if (self.state == "prepared") != (self.finished_at is None):
            raise ValueError("Terminal write requires finish time")
        return self


class ArtifactStatus(ContractModel):
    kind: ArtifactKind
    state: Literal["verified", "not_generated", "recovery_required", "damaged"]
    verified: bool
    pending_operations: int
    code: str | None = None


def verifies(result, kind, data):
    return (
        verify_document(result, data)
        if kind == "document"
        else verify_verification_report(result, data)
    )


class ArtifactFiles:
    def __init__(self, root, run_id):
        self.root = Path(root).resolve()
        self.directory = self.root / run_id
        self._directory(self.directory)

    def _directory(self, path):
        # Reject links/junctions even when they resolve inside data. Never follow
        # an operator-created path while publishing or isolating an artifact.
        if (
            path.is_symlink()
            or (hasattr(path, "is_junction") and path.is_junction())
            or path.resolve() != path.absolute()
            or not path.resolve().is_relative_to(self.root)
            or not path.is_dir()
        ):
            raise ArtifactProblem(
                "artifact_path_unsafe",
                "Небезопасный путь артефакта. Операция остановлена.",
            )

    def path(self, name):
        if Path(name).name != name or name.startswith(".."):
            raise ArtifactProblem("artifact_path_unsafe", "Недопустимое имя артефакта.")
        self._directory(self.directory)
        path = self.directory / name
        if (
            path.is_symlink()
            or (hasattr(path, "is_junction") and path.is_junction())
            or (path.exists() and not stat.S_ISREG(path.lstat().st_mode))
        ):
            raise ArtifactProblem(
                "artifact_path_unsafe", "Артефакт должен быть обычным локальным файлом."
            )
        return path

    def read(self, name):
        path = self.path(name)
        if not path.exists() or path.stat().st_size > 10 * 1024 * 1024:
            return b""
        return path.read_bytes()

    def stage_name(self, write):
        return f".{write.kind}-{write.operation_id}.tmp"

    def write_stage(self, write, data):
        path = self.path(self.stage_name(write))
        with path.open("xb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())

    def publish(self, write):
        source = self.path(self.stage_name(write))
        target = self.path(write.filename)
        # An atomic new directory entry on the same filesystem. Unlike replace
        # or POSIX rename, link refuses to overwrite an existing target.
        os.link(source, target)
        source.unlink()

    def leftovers(self, kind, writes):
        names = set(NAMES[kind])
        names.update(str(Path(n).with_suffix(".tmp")) for n in NAMES[kind])
        names.update(self.stage_name(w) for w in writes)
        # Detect abandoned application staging files even if a legacy write had
        # no journal. Unknown project/user files are never swept up.
        for p in self.directory.glob(f".{kind}-*.tmp"):
            if len(p.name) == len(f".{kind}-") + 32 + 4:
                identifier = p.name[len(f".{kind}-") : -4]
                if all(c in "0123456789abcdef" for c in identifier):
                    names.add(p.name)
        return [name for name in sorted(names) if self.path(name).exists()]

    def isolate(self, name, operation_id):
        source = self.path(name)
        if not source.exists():
            return
        directory = self.directory / ".artifact-recovery"
        directory.mkdir(exist_ok=True)
        self._directory(directory)
        target = directory / f"{operation_id}-{name}"
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ArtifactProblem(
                "artifact_path_unsafe", "Небезопасный путь изоляции артефакта."
            )
        if target.exists():
            # A crash between link and unlink is safe to repeat only if both
            # names still refer to the very same file, not merely equal bytes.
            if not source.samefile(target):
                raise ArtifactProblem(
                    "artifact_recovery_conflict",
                    "Конфликт файлов изоляции; требуется ручная проверка.",
                )
        else:
            os.link(source, target)
        source.unlink()

    def validate(self, result, kind, filename, expected_sha256):
        data = self.read(filename)
        if sha256(data).hexdigest() != expected_sha256 or not verifies(
            result, kind, data
        ):
            raise ArtifactProblem(
                "artifact_verification_failed",
                "DOCX не прошёл проверку содержимого и SHA-256. Нужна явная процедура восстановления.",
            )
        return data
