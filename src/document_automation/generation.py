"""Fixed template generation and verification; untrusted data never becomes OOXML."""

import re
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from docx import Document

from document_automation.operations import audit_operation
from document_automation.schemas import (
    AuditEvent,
    GeneratedDocument,
    GeneratedReport,
    ProcessingResult,
    result_fingerprint,
)

TEMPLATE = Path(__file__).parent / "assets/work_request.docx"
DRAFT = "ЧЕРНОВИК — ТРЕБУЕТ ПРОВЕРКИ"
HUMAN = "Документ сформирован автоматически и подлежит проверке человеком"
LABELS = {
    "customer_name": "Заказчик",
    "request_title": "Название заявки",
    "contact_email": "Контактный email",
    "requested_completion_date": "Желаемая дата завершения",
    "work_description": "Описание работ",
    "item_code": "Код позиции",
    "name": "Наименование работ",
    "quantity": "Количество",
    "unit": "Единица измерения",
}


class OperationDenied(Exception):
    def __init__(self, event):
        self.event = event
        super().__init__(event.decision.reason)


def field_entries(result):
    for name in LABELS:
        if hasattr(result.data, name):
            yield name, getattr(result.data, name)
    for index, item in enumerate(result.data.items):
        for name in type(item).model_fields:
            yield f"items[{index}].{name}", getattr(item, name)


def display(result, path, field):
    if any(i.path == path and i.code == "invalid_value" for i in result.issues):
        return "[НЕКОРРЕКТНОЕ ЗНАЧЕНИЕ]"
    if field.state == "found":
        return (
            field.value.strftime("%d.%m.%Y")
            if isinstance(field.value, date)
            else str(field.value)
        )
    return {
        "missing": "Не указан" if path == "contact_email" else "[НЕ УКАЗАНО]",
        "ambiguous": "[ТРЕБУЕТ УТОЧНЕНИЯ]",
        "conflict": "[ПРОТИВОРЕЧИЕ В ИСТОЧНИКАХ]",
    }[field.state]


def _source_text(source, filenames):
    loc = ", ".join(
        f"{key}={value}"
        for key, value in source.location.model_dump().items()
        if value is not None
    )
    return f"{filenames[source.document_id]} ({loc})\n{source.text}"


def _sections(result):
    filenames = {d.document_id: d.filename for d in result.documents}
    issues, sources = [], []
    fields = dict(field_entries(result))
    for issue in result.issues:
        issues.append(f"{issue.path}: {issue.code}")
        if issue.path in fields:
            for c in fields[issue.path].candidates:
                if c.verified:
                    issues.append(
                        "Кандидат (не утверждён): "
                        + c.raw_value
                        + "\n"
                        + _source_text(c.source, filenames)
                    )
                else:
                    issues.append(
                        "Неподтверждённый кандидат исключён из выбора значения."
                    )
    seen_sources = set()
    for path, field in fields.items():
        for candidate in field.candidates:
            key = (path, candidate.source)
            if candidate.verified and key not in seen_sources:
                seen_sources.add(key)
                sources.append(f"{path}\n" + _source_text(candidate.source, filenames))
    return (
        "\n\n".join(issues) or "Автоматическими проверками проблемы не обнаружены",
        "\n\n".join(sources) or "Подтверждённые источники отсутствуют",
    )


def _fingerprint(result):
    return result_fingerprint(result)


def _usable(result, path, field):
    return field.state == "found" and not any(
        issue.path == path and issue.code == "invalid_value" for issue in result.issues
    )


def _clarifications(result):
    """Display-only grouping; the immutable issues and candidates remain intact."""
    from document_automation.ui_text import field_label

    fields = dict(field_entries(result))
    groups = {}
    for issue in result.issues:
        match = re.fullmatch(r"items\[(\d+)\](?:\.(\w+))?", issue.path)
        if match:
            index = int(match[1])
            item = result.data.items[index]
            title = f"Позиция {index + 1}"
            if _usable(result, f"items[{index}].name", item.name):
                title += f" «{item.name.value}»"
            label = LABELS.get(match[2], "Сведения позиции")
        else:
            title, label = "", field_label(issue.path)
        field = fields.get(issue.path)
        raw = (
            list(dict.fromkeys(c.raw_value for c in field.candidates)) if field else []
        )
        if issue.code == "required_missing":
            reason = "не указано"
        elif issue.code == "invalid_value":
            rules = {
                "quantity": "нужно число больше нуля",
                "unit": "допустимы только шт, ч, кв.м, компл.",
                "item_code": "допустимы латинские буквы, цифры и дефис",
                "requested_completion_date": "нужна корректная дата",
                "contact_email": "нужен корректный адрес email",
            }
            name = match[2] if match else issue.path
            reason = "недопустимое значение" + (
                " «" + "», «".join(raw) + "»" if raw else ""
            )
            reason += "; " + rules.get(name, "уточните значение в источнике")
        elif issue.code == "conflicting_values":
            reason = (
                "в источниках разные значения; сверьте источники, значение не выбрано"
            )
        elif issue.code == "unverified_source":
            reason = "кандидат не подтверждён по источнику и контексту"
            if raw:
                reason += " («" + "», «".join(raw) + "»)"
        elif (
            issue.code == "ambiguous_value"
            and field
            and field.ambiguity_reason == "duplicate_item_code"
        ):
            reason = "код повторяется у нескольких позиций; уточните коды"
        else:
            reason = ISSUE_LABELS.get(issue.code, "Уточните исходные данные.")
        text = f"{label}: {reason}"
        entries = groups.setdefault(title, [])
        if text not in entries:
            entries.append(text)
    return [
        f"{title}: " + "; ".join(entries) if title else "; ".join(entries)
        for title, entries in groups.items()
    ]


def _entry_field(doc, result, name, blank):
    field = getattr(result.data, name)
    if _usable(result, name, field):
        doc.add_paragraph(f"{LABELS[name]}: {display(result, name, field)}")
        return
    notes = {
        "missing": "Не указано в источниках — заполнить вручную.",
        "conflict": "Сверьте источники перед ручным заполнением; значение не выбрано.",
        "ambiguous": "Уточните значение перед ручным заполнением.",
        "found": "Значение недопустимо — уточните и заполните вручную.",
    }
    paragraph = doc.add_paragraph(f"{LABELS[name]}: {blank}")
    paragraph.paragraph_format.keep_with_next = True
    doc.add_paragraph(notes[field.state], style="Caption")


def _document_styles(doc, report=False):
    from docx.oxml.ns import qn
    from docx.shared import Pt

    # The packaged default styles include a decorative title border.
    for element in (doc.styles.element, doc.element):
        for border in list(element.iter(qn("w:pBdr"))):
            border.getparent().remove(border)
    if report:
        doc.styles["Normal"].paragraph_format.space_after = Pt(4)
        for name, size in (("Heading 1", 13), ("Heading 2", 11)):
            style = doc.styles[name]
            style.font.size = Pt(size)
            style.paragraph_format.space_before = Pt(8)
            style.paragraph_format.space_after = Pt(3)


def _render(result):
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(1.7)
    section.bottom_margin = Cm(1.7)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    normal = doc.styles["Normal"]
    normal.font.name = "Arial"
    normal.font.size = Pt(10)
    _document_styles(doc)
    doc.add_heading("Заявка на выполнение работ", 0)
    status = doc.add_paragraph(DRAFT if result.needs_review else HUMAN)
    status.runs[0].bold = True
    if result.needs_review:
        status.runs[0].font.color.rgb = RGBColor(170, 35, 35)
    data = result.data
    for name, label in (
        ("customer_name", "Заказчик"),
        ("contact_email", "Контактный email"),
    ):
        field = getattr(data, name)
        if field.state == "found" and not any(
            i.path == name and i.code == "invalid_value" for i in result.issues
        ):
            doc.add_paragraph(f"{label}: {field.value}")
    _entry_field(doc, result, "request_title", "________________________________")
    _entry_field(doc, result, "requested_completion_date", "__.__.____")
    if data.work_description.state == "found":
        doc.add_heading("Описание работ", level=1)
        doc.add_paragraph(str(data.work_description.value))
    valid_items = []
    for index, item in enumerate(data.items):

        def acceptable(field_name, field):
            return field.state == "found" and not any(
                issue.path == f"items[{index}].{field_name}"
                and issue.code in {"invalid_value", "unverified_source"}
                for issue in result.issues
            )

        if acceptable("name", item.name):
            valid_items.append((index, item))
    if valid_items:
        doc.add_heading("Перечень работ", level=1)
        include_code = any(item.item_code.state == "found" for _, item in valid_items)
        headers = (["Код позиции"] if include_code else []) + [
            "Наименование",
            "Количество",
            "Единица измерения",
        ]
        table = doc.add_table(rows=1, cols=len(headers))
        table.style = "Table Grid"
        for cell, value in zip(table.rows[0].cells, headers):
            cell.text = value
        for index, item in valid_items:
            cells = table.add_row().cells
            values = []
            if include_code:
                values.append(
                    str(item.item_code.value)
                    if item.item_code.state == "found"
                    and acceptable("item_code", item.item_code)
                    else ""
                )
            values.extend(
                [
                    str(item.name.value),
                    str(item.quantity.value)
                    if acceptable("quantity", item.quantity)
                    else "",
                    str(item.unit.value) if acceptable("unit", item.unit) else "",
                ]
            )
            for cell, value in zip(cells, values):
                cell.text = value
        _table_layout(table, (2, 8, 3, 4) if include_code else (10, 3, 4))
    clarification = _clarifications(result)
    if result.needs_review or clarification:
        doc.add_heading("Требует уточнения", level=1)
        if clarification:
            for line in clarification:
                doc.add_paragraph(
                    line, style="List Bullet"
                ).paragraph_format.keep_together = True
        else:
            doc.add_paragraph(
                "Проверьте исходные сведения перед использованием заявки."
            )
    doc.sections[0].header.paragraphs[0].text = DRAFT if result.needs_review else ""
    doc.core_properties.identifier = result.run_id
    doc.core_properties.subject = _fingerprint(result)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def _content(doc):
    return (
        [p.text for p in doc.paragraphs],
        [[[c.text for c in row.cells] for row in t.rows] for t in doc.tables],
        [p.text for s in doc.sections for p in s.header.paragraphs],
    )


def verify_document(result, data):
    if (
        result.processing_status != "completed"
        or result.generated_document is None
        or not data
        or len(data) > 10 * 1024 * 1024
    ):
        return False
    meta = result.generated_document
    if (
        meta.run_id != result.run_id
        or meta.sha256 != sha256(data).hexdigest()
        or meta.template_sha256 != sha256(TEMPLATE.read_bytes()).hexdigest()
    ):
        return False
    try:
        doc = Document(BytesIO(data))
        expected = Document(BytesIO(_render(result)))
        return (
            doc.core_properties.identifier == result.run_id
            and doc.core_properties.subject == _fingerprint(result)
            and _content(doc) == _content(expected)
        )
    except (ValueError, KeyError, OSError, Exception):
        # Verification is a boundary: malformed ZIP/XML must never authorize export.
        return False


def generate_document(result):
    decision, event = audit_operation(
        "create_draft", run_id=result.run_id, result=result
    )
    if not decision.allowed:
        raise OperationDenied(event)
    data = _render(result)
    meta = GeneratedDocument(
        run_id=result.run_id,
        filename="work_request_needs_review.docx"
        if result.needs_review
        else "work_request.docx",
        sha256=sha256(data).hexdigest(),
        template_sha256=sha256(TEMPLATE.read_bytes()).hexdigest(),
        needs_review=result.needs_review,
    )
    generated = AuditEvent(
        run_id=result.run_id,
        timestamp=datetime.now(timezone.utc),
        stage="generation",
        event="generated",
        message="DOCX generated and verified",
    )
    updated = ProcessingResult.model_validate(
        {
            **result.model_dump(),
            "generated_document": meta,
            "audit": result.audit + (event, generated),
        }
    )
    if not verify_document(updated, data):
        raise ValueError("Generated document did not pass verification")
    return updated, data


ISSUE_LABELS = {
    "required_missing": "Не хватает обязательных данных.",
    "ambiguous_value": "Нельзя однозначно определить значение.",
    "conflicting_values": "В исходных документах есть расхождения.",
    "invalid_value": "Значение не соответствует допустимому формату.",
    "unverified_source": "Значение не удалось подтвердить по источнику и контексту.",
    "unsupported_content": "Часть структуры документа не поддерживается.",
    "missing_document": "Не хватает документа нужного типа.",
    "unknown_document": "Назначение документа не подтверждено.",
    "duplicate_document": "Обнаружены повторные документы.",
}


def _source_key(source):
    return (source.document_id, source.location.model_dump_json(), source.text)


def _evidence_registry(result, fields):
    verified, claimed = {}, {}

    def add(source, trusted=True):
        if source is None:
            return
        registry = verified if trusted else claimed
        entry = registry.setdefault(_source_key(source), [source, []])
        if source.fragment_id not in entry[1]:
            entry[1].append(source.fragment_id)

    for _, field in fields:
        for candidate in field.candidates:
            add(candidate.source, candidate.verified)
    for classification in result.classifications:
        for source in classification.evidence:
            add(source)
    for issue in result.issues:
        for source in issue.sources:
            add(source)

    def reference(source, trusted=True):
        if source is None:
            return "Источник не заявлен"
        registry = verified if trusted else claimed
        number = list(registry).index(_source_key(source)) + 1
        return ("Источник " if trusted else "Неподтверждённая ссылка ") + str(number)

    return verified, claimed, reference


def _table_layout(table, widths):
    from docx.oxml import OxmlElement
    from docx.shared import Cm

    table.autofit = False
    for column, width in zip(table.columns, widths):
        column.width = Cm(width)
    table.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    for row in table.rows:
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
        for cell, width in zip(row.cells, widths):
            cell.width = Cm(width)


def _report_render(result, audit_snapshot=None):
    """Create the audit artifact directly from the immutable result snapshot."""
    from docx.shared import Cm, Pt

    from document_automation.ui_text import STATES, field_label, source_label

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(1.8)
    section.bottom_margin = Cm(1.8)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    doc.styles["Normal"].font.name = "Arial"
    doc.styles["Normal"].font.size = Pt(9)
    _document_styles(doc, report=True)
    fields = list(field_entries(result)) if result.data else []
    verified, claimed, reference = _evidence_registry(result, fields)
    doc.add_heading("Отчёт проверки исходных данных", 0)
    doc.add_paragraph(f"Идентификатор обработки: {result.run_id}")
    status_label = {
        "completed": "Обработка завершена",
        "failed": "Обработка завершилась ошибкой",
    }[result.processing_status]
    doc.add_paragraph(
        f"Статус: {status_label}; проверка человеком: {'требуется' if result.needs_review else 'не отмечена'}"
    )
    doc.add_paragraph(
        "Проверка подтверждает происхождение значений и соответствие правилам обработки. "
        "Она не устанавливает истинность сведений в исходных документах."
    )
    doc.add_heading("Входные документы", level=1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Shading Accent 1"
    for cell, value in zip(
        table.rows[0].cells, ("Файл", "Назначение", "Статус", "Доказательства")
    ):
        cell.text = value
    class_names = {}
    for classification in result.classifications:
        class_names.setdefault(classification.document_id, []).append(classification)
    document_types = {
        "customer_card": "Карточка заказчика",
        "work_description": "Описание работ",
        "work_items": "Перечень работ",
        "unknown": "Назначение не определено",
    }
    for item in result.documents:
        classifications = class_names.get(item.document_id, [])
        row = table.add_row().cells
        row[0].text = item.filename
        row[1].text = (
            "; ".join(
                document_types.get(c.document_type, "Назначение не определено")
                for c in classifications
            )
            if classifications
            else "Не определено"
        )
        row[2].text = (
            "Подтверждено фрагментами"
            if classifications and all(c.evidence for c in classifications)
            else "Не подтверждено"
        )
        row[3].text = (
            ", ".join(
                dict.fromkeys(reference(s) for c in classifications for s in c.evidence)
            )
            if classifications
            else "Нет"
        )
    _table_layout(table, (4, 4, 5, 4))

    doc.add_heading("Найденные значения и состояния", level=1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Light Shading Accent 1"
    for cell, value in zip(
        table.rows[0].cells, ("Поле", "Значение", "Состояние", "Источники")
    ):
        cell.text = value
    for path, field in fields:
        row = table.add_row().cells
        row[0].text = field_label(path)
        row[1].text = str(field.value) if field.state == "found" else "Не подставлено"
        invalid = any(
            issue.path == path and issue.code == "invalid_value"
            for issue in result.issues
        )
        row[2].text = (
            "Найдено, но значение недопустимо" if invalid else STATES[field.state]
        )
        row[3].text = (
            ", ".join(
                dict.fromkeys(reference(c.source, c.verified) for c in field.candidates)
            )
            or "Нет кандидатов"
        )
    _table_layout(table, (4.5, 5, 3.5, 4))

    doc.add_heading("Проблемы и требуемые действия", level=1)
    if not result.issues:
        doc.add_paragraph("По правилам обработки замечаний не обнаружено.")
    for issue in result.issues:
        doc.add_heading(field_label(issue.path), level=2)
        doc.add_paragraph(ISSUE_LABELS.get(issue.code, "Требуется ручная проверка."))
        if issue.sources:
            doc.add_paragraph(
                "Доказательства: "
                + ", ".join(dict.fromkeys(reference(s) for s in issue.sources))
            )

    filenames = {d.document_id: d.filename for d in result.documents}
    doc.add_heading("Кандидаты и результаты проверки", level=1)
    for path, field in fields:
        if not field.candidates:
            continue
        doc.add_heading(field_label(path), level=2)
        for candidate in field.candidates:
            doc.add_paragraph(
                f"Кандидат: {candidate.raw_value} — "
                f"{'подтверждён правилами' if candidate.verified else 'отклонён, не использован'}"
            ).paragraph_format.keep_with_next = True
            interpretations = (
                "; ".join(str(v) for v in candidate.interpretations)
                or "Нет допустимого преобразования"
            )
            normalized = doc.add_paragraph(
                "После нормализации: "
                + interpretations
                + "; "
                + reference(candidate.source, candidate.verified)
            )
            normalized.paragraph_format.keep_together = True
            normalized.paragraph_format.keep_with_next = not candidate.verified or any(
                i.path == path and i.code == "invalid_value" for i in result.issues
            )
            if candidate.verified and any(
                i.path == path and i.code == "invalid_value" for i in result.issues
            ):
                doc.add_paragraph(
                    "Значение недопустимо и не подставляется в заявку; требуется уточнение."
                )
            if not candidate.verified:
                doc.add_paragraph(
                    "Причина: источник, координаты или контекст не прошли независимую проверку."
                )
    doc.add_heading("Источники значений и фрагменты", level=1)
    for trusted, registry in ((True, verified), (False, claimed)):
        if not trusted and registry:
            doc.add_heading(
                "Неподтверждённые ссылки — не являются доказательствами", level=2
            )
        for source, _ in registry.values():
            doc.add_heading(reference(source, trusted), level=2)
            name = filenames.get(source.document_id, "Источник не зарегистрирован")
            doc.add_paragraph(
                f"{name}; {source_label(source.location)}"
            ).paragraph_format.keep_with_next = True
            doc.add_paragraph(source.text).paragraph_format.keep_together = True
    doc.add_heading("Что требует участия человека", level=1)
    if result.needs_review:
        doc.add_paragraph(
            "Сверьте перечисленные проблемы и исправьте исходные данные перед использованием заявки."
        )
    else:
        doc.add_paragraph(
            "Рекомендуется проверить сформированную заявку перед использованием."
        )
    doc.add_heading("Технические подробности", level=1)
    technical_start = len(doc.paragraphs)
    doc.add_paragraph(f"run_id: {result.run_id}")
    for path, field in fields:
        if field.ambiguity_reason:
            doc.add_paragraph(f"{path}: ambiguity_reason={field.ambiguity_reason}")
    for trusted, registry in ((True, verified), (False, claimed)):
        for source, fragment_ids in registry.values():
            doc.add_paragraph(
                reference(source, trusted)
                + ": "
                + source.document_id
                + "; fragment_id: "
                + ", ".join(fragment_ids)
            )
    for issue in result.issues:
        doc.add_paragraph(f"{issue.path} — {issue.code}: {issue.message}")
    for event in audit_snapshot if audit_snapshot is not None else result.audit:
        details = (
            f"{event.timestamp.isoformat()} | {event.stage} | {event.event} | "
            f"{event.error_code or ''} | {event.message}"
        )
        if event.model:
            details += "\nМодель: " + event.model
        if event.decision:
            details += (
                f"\nРазрешение: {event.decision.operation}; allowed={event.decision.allowed}; "
                f"reason={event.decision.reason}; resource_id={event.decision.resource_id}; "
                f"draft_required={event.decision.draft_required}"
            )
        doc.add_paragraph(details)
    for paragraph in doc.paragraphs[technical_start:]:
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.keep_together = True
        for run in paragraph.runs:
            run.font.size = Pt(8)
    doc.core_properties.identifier = result.run_id
    doc.core_properties.subject = result_fingerprint(result)
    output = BytesIO()
    doc.save(output)
    return output.getvalue()


def verify_verification_report(result, data):
    if (
        result.processing_status != "completed"
        or not result.generated_report
        or not data
        or len(data) > 10 * 1024 * 1024
    ):
        return False
    meta = result.generated_report
    if (
        meta.run_id != result.run_id
        or meta.result_sha256 != result_fingerprint(result)
        or meta.sha256 != sha256(data).hexdigest()
    ):
        return False
    try:
        doc = Document(BytesIO(data))
        expected = Document(BytesIO(_report_render(result, meta.audit_snapshot)))
        return (
            doc.core_properties.identifier == result.run_id
            and doc.core_properties.subject == result_fingerprint(result)
            and _content(doc) == _content(expected)
        )
    except Exception:
        return False


def generate_verification_report(result):
    decision, event = audit_operation(
        "create_verification_report", run_id=result.run_id, result=result
    )
    if not decision.allowed:
        raise OperationDenied(event)
    generated = AuditEvent(
        run_id=result.run_id,
        timestamp=datetime.now(timezone.utc),
        stage="generation",
        event="generated",
        message="Verification report generated",
    )
    audit_snapshot = result.audit + (event, generated)
    render_result = ProcessingResult.model_validate(
        {**result.model_dump(), "audit": audit_snapshot}
    )
    data = _report_render(render_result, audit_snapshot)
    meta = GeneratedReport(
        run_id=result.run_id,
        sha256=sha256(data).hexdigest(),
        result_sha256=result_fingerprint(result),
        audit_snapshot=audit_snapshot,
    )
    updated = ProcessingResult.model_validate(
        {
            **result.model_dump(),
            "generated_report": meta,
            "audit": audit_snapshot,
        }
    )
    if not verify_verification_report(updated, data):
        raise ValueError("Verification report did not pass verification")
    return updated, data
