"""Streamlit client of the local FastAPI service."""

import base64
import os
from pathlib import Path

import httpx
import streamlit as st

from document_automation.generation import display, field_entries
from document_automation.operations import check_operation
from document_automation.schemas import ProcessingResult
from document_automation.ui_text import DEMOS, ISSUES, STATES, field_label, source_label

API = os.environ.get("DAA_API_URL", "http://127.0.0.1:8000").rstrip("/")


def call(method, path, **kwargs):
    response = httpx.request(method, API + path, timeout=120, **kwargs)
    if response.is_error:
        try:
            message = response.json().get("detail", "Ошибка сервиса")
        except ValueError:
            message = "Ошибка сервиса"
        raise RuntimeError(message)
    return response


def main():
    st.set_page_config(page_title="Document Automation Assistant", layout="wide")
    st.title("Сводная заявка на выполнение работ")
    st.write(
        "Загрузите карточку заказчика PDF, описание работ DOCX и перечень работ XLSX. Система проверит данные и подготовит рабочую заявку и отдельный отчёт проверки."
    )
    try:
        health = call("GET", "/health").json()
        st.caption(
            "Режим: "
            + (
                "демонстрационный, без платных вызовов"
                if health["mode"].startswith("demo")
                else "обработка с языковой моделью"
            )
        )
        if health["mode"].startswith("demo"):
            st.info(
                "Демонстрационный режим работает без обращения к языковой модели и рассчитан на утверждённую структуру демонстрационных документов. Произвольные документы могут потребовать ручной проверки."
            )
        st.caption(
            "Реальное подключение OpenAI API ещё не проверено; демонстрация не подтверждает качество его работы."
        )
    except (httpx.HTTPError, RuntimeError):
        st.error(
            "Сервис обработки недоступен. Проверьте запуск приложения по инструкции README."
        )
        return
    files = st.file_uploader(
        "Исходные документы", type=["pdf", "docx", "xlsx"], accept_multiple_files=True
    )
    demo = st.selectbox("Демонстрационный комплект", list(DEMOS), format_func=DEMOS.get)
    st.caption(
        "A — полный комплект; B — пропущена дата; C — даты противоречат друг другу; V — другие заголовки и частичные позиции. При выборе примера используются встроенные файлы. После выбора нажмите «Обработать»."
    )
    if st.button(
        "Обработать", type="primary", disabled=not files and demo == "Свои файлы"
    ):
        st.session_state.pop("document", None)
        st.session_state.pop("report", None)
        st.session_state.pop("result", None)
        try:
            payload = {
                "files": [
                    {
                        "filename": f.name,
                        "content_base64": base64.b64encode(f.getvalue()).decode(),
                    }
                    for f in files
                ]
            }
            if demo != "Свои файлы":
                directory = Path(os.environ.get("DAA_DEMO_DIR", "demo")) / demo
                payload = {
                    "files": [
                        {
                            "filename": p.name,
                            "content_base64": base64.b64encode(p.read_bytes()).decode(),
                        }
                        for p in sorted(directory.glob("*"))
                        if p.suffix in (".pdf", ".docx", ".xlsx")
                    ]
                }
            with st.spinner("Чтение и проверка документов…"):
                st.session_state["result"] = call("POST", "/runs", json=payload).json()
        except (httpx.HTTPError, RuntimeError) as exc:
            st.error(
                "Не удалось обработать документы. Проверьте файлы и доступность сервиса, затем повторите попытку."
            )
            with st.expander("Технические подробности"):
                st.text(str(exc))
    raw = st.session_state.get("result")
    if not raw:
        return
    result = ProcessingResult.model_validate_json(__import__("json").dumps(raw))
    with st.expander("Технические подробности"):
        st.text("ID запуска: " + result.run_id)
        for issue in result.issues:
            st.text(f"{issue.path}: {issue.code} — {issue.message}")
        for event in result.audit:
            if event.error_code:
                st.text(event.stage + ": " + event.error_code)
    if result.processing_status == "failed":
        st.error(
            "Обработка не завершена. Создать документ нельзя. Проверьте, что исходные файлы открываются, и повторите обработку."
        )
        return
    if result.needs_review:
        st.warning("Требуется проверка. DOCX будет явно помечен как черновик.")
        if result.data.items:
            st.info(
                "Частичные позиции сохранены. Пропуски и неподдерживаемые единицы отмечены отдельно; единицы не пересчитываются."
            )
    else:
        st.success(
            "Автоматические проверки пройдены. Итоговый документ всё равно должен проверить сотрудник."
        )
    st.dataframe(
        [
            {
                "Поле": field_label(path),
                "Значение": display(result, path, field),
                "Состояние": STATES[field.state],
            }
            for path, field in field_entries(result)
        ],
        hide_index=True,
        width="stretch",
    )
    if result.issues:
        st.subheader("Проблемы, требующие проверки")
        st.dataframe(
            [
                {
                    "Поле": field_label(i.path),
                    "Проблема": ISSUES[i.code][0],
                    "Что сделать": ISSUES[i.code][1],
                }
                for i in result.issues
            ],
            hide_index=True,
        )
    with st.expander("Источники значений"):
        filenames = {d.document_id: d.filename for d in result.documents}
        for path, field in field_entries(result):
            st.write(field_label(path))
            for candidate in field.candidates:
                st.text(candidate.raw_value)
                if candidate.verified:
                    st.caption(
                        filenames[candidate.source.document_id]
                        + " "
                        + " — "
                        + source_label(candidate.source.location)
                    )
                    st.text(candidate.source.text)
                else:
                    st.warning(
                        "Это значение не подтверждено источником и исключено из выбора итогового значения."
                    )
    decision = check_operation("create_draft", run_id=result.run_id, result=result)
    report_decision = check_operation(
        "create_verification_report", run_id=result.run_id, result=result
    )
    if decision.reason == "no_substantive_content":
        st.error(
            "Создать DOCX нельзя: нет подтверждённых содержательных данных. "
            "Нужны заказчик, название заявки, описание или наименование работы. "
            "Одного email недостаточно. Уточните исходные документы и повторите обработку."
        )
    col1, col2 = st.columns(2)
    if col1.button("Подготовить рабочую заявку", disabled=not decision.allowed):
        st.session_state.pop("document", None)
        try:
            with st.spinner("Создание и проверка рабочей заявки…"):
                updated = call("POST", f"/runs/{result.run_id}/document").json()
                response = call("GET", f"/runs/{result.run_id}/document")
                st.session_state["result"] = updated
                st.session_state["document"] = (
                    updated["generated_document"]["filename"],
                    response.content,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            st.error(
                "Не удалось подготовить рабочую заявку. Проверьте доступность сервиса и повторите попытку."
            )
            with st.expander("Технические подробности ошибки"):
                st.text(str(exc))
    if col2.button("Подготовить отчёт проверки", disabled=not report_decision.allowed):
        st.session_state.pop("report", None)
        try:
            with st.spinner("Создание отчёта проверки…"):
                updated = call(
                    "POST", f"/runs/{result.run_id}/verification-report"
                ).json()
                response = call("GET", f"/runs/{result.run_id}/verification-report")
                st.session_state["result"] = updated
                st.session_state["report"] = (
                    updated["generated_report"]["filename"],
                    response.content,
                )
        except (httpx.HTTPError, RuntimeError) as exc:
            st.error("Не удалось подготовить отчёт проверки.")
            with st.expander("Технические подробности ошибки"):
                st.text(str(exc))
    if "document" in st.session_state:
        filename, data = st.session_state["document"]
        st.download_button(
            "Скачать рабочую заявку",
            data,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    if "report" in st.session_state:
        filename, data = st.session_state["report"]
        st.download_button(
            "Скачать отчёт проверки",
            data,
            file_name=filename,
            mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )


if __name__ == "__main__":
    main()
