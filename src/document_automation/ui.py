"""Streamlit client of the local FastAPI service."""

import base64
import os
from pathlib import Path

import httpx
import streamlit as st

from document_automation.generation import display, field_entries
from document_automation.operations import check_operation
from document_automation.run_requests import content_fingerprint, streamlit_action
from document_automation.schemas import ProcessingResult
from document_automation.ui_business import show_acceptance, show_metrics
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


def send_action(payload, action):
    # Persist before sending, including on a client timeout. Never rotate a key
    # merely because no HTTP response was received.
    st.session_state["submission"] = action
    st.session_state["submission_payload"] = payload
    response = call(
        "POST",
        "/runs",
        json={**payload, "retry_of": action["retry_of"]},
        headers={"Idempotency-Key": action["key"]},
    )
    body = response.json()
    if response.status_code == 202:
        st.session_state["pending_run"] = body["run_id"]
    else:
        st.session_state.pop("pending_run", None)
        st.session_state["result"] = body
        st.session_state.pop("document", None)
        st.session_state.pop("report", None)


def main():
    st.set_page_config(page_title="Document Automation Assistant", layout="wide")
    st.title("Сводная заявка на выполнение работ")
    st.write(
        "Загрузите документы PDF, DOCX и XLSX с данными заказчика, описанием и перечнем работ. Форматы можно сочетать; один файл может содержать несколько групп данных. Система поддерживает подписанные поля, простые таблицы Word/Excel и текстовые PDF-таблицы с явными разделителями."
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
                fingerprint = content_fingerprint(
                    [
                        (f["filename"], base64.b64decode(f["content_base64"]))
                        for f in payload["files"]
                    ]
                )
                action = streamlit_action(
                    fingerprint, st.session_state.get("submission")
                )
                send_action(payload, action)
        except (httpx.HTTPError, RuntimeError) as exc:
            st.error(
                "Ответ сервиса не получен или запрос отклонён. Повтор кнопки «Обработать» использует прежний ключ и проверяет тот же запуск."
            )
            with st.expander("Технические подробности"):
                st.text(str(exc))
    # Read after submission so the same render includes the newly saved run.
    show_metrics(call)
    pending = st.session_state.get("pending_run")
    if pending:
        st.info(
            "Обработка ещё выполняется. Повторная отправка этого действия не создаёт новый LLM-вызов."
        )
        if st.button("Проверить состояние"):
            try:
                response = call("GET", f"/runs/{pending}")
                if response.status_code == 200:
                    st.session_state["result"] = response.json()
                    st.session_state.pop("pending_run", None)
            except (httpx.HTTPError, RuntimeError):
                st.error("Не удалось проверить состояние. Ключ запроса сохранён.")
        if st.session_state.get("pending_run"):
            return
    raw = st.session_state.get("result")
    if not raw:
        return
    result = ProcessingResult.model_validate_json(__import__("json").dumps(raw))
    with st.expander("Использование LLM API"):
        try:
            usage = call("GET", f"/runs/{result.run_id}/usage").json()
            if usage["mode"] == "offline":
                st.write(
                    "LLM API: 0 вызовов, расходы на LLM API — 0. Общая себестоимость обработки не измерялась."
                )
            elif usage["mode"] in ("untracked", "legacy_untracked"):
                st.write(
                    "Постоянный учёт для этого запуска отсутствует. Расходы неизвестны."
                )
            else:
                st.write(f"Провайдер: {usage['provider']}; модель: {usage['model']}")
                st.write(
                    f"Попыток: {usage['attempts']}; повторных обращений: {usage['retry_calls']}"
                )
                st.write(
                    f"Подтверждённые токены: вход {usage['confirmed_input_tokens']}, выход {usage['confirmed_output_tokens']}"
                )
                if usage["confirmed_cached_tokens"] is not None:
                    st.write(
                        f"Подтверждённые cached tokens: {usage['confirmed_cached_tokens']}"
                    )
                amounts = "; ".join(
                    f"{amount} {currency}"
                    for currency, amount in usage["known_estimated_costs"].items()
                )
                st.write(
                    "Известная расчётная сумма: "
                    + (amounts or "нет оценённых расходов")
                )
                if usage["has_unknown_cost"]:
                    st.write(
                        f"Есть неизвестные расходы: {usage['unknown_cost_attempts']} попыток. Известная сумма не является полной."
                    )
                st.caption(
                    "Расчёт по настроенному тарифу, не подтверждённое списание провайдера."
                )
                if usage["data_origin"] == "simulated":
                    st.caption(
                        "Симулированные данные mock HTTP, без реального обращения к провайдеру."
                    )
        except (httpx.HTTPError, RuntimeError):
            st.write("Сведения об использовании LLM API сейчас недоступны.")
    with st.expander("Технические подробности"):
        st.text("ID запуска: " + result.run_id)
        for issue in result.issues:
            st.text(f"{issue.path}: {issue.code} — {issue.message}")
        for event in result.audit:
            if event.error_code:
                st.text(event.stage + ": " + event.error_code)
    if result.processing_status == "failed":
        st.error(
            "Обработка не завершена. Создать документ нельзя. Повтор того же действия вернёт этот запуск."
        )
        try:
            operation = call("GET", f"/runs/{result.run_id}/operation").json()
            can_retry = (
                operation["can_retry"] and "submission_payload" in st.session_state
            )
            if operation["retry_block_reason"] == "retry_outcome_unknown":
                st.info(
                    "Исход предыдущего вызова провайдера неизвестен. Новая отправка заблокирована до выяснения его состояния."
                )
            st.caption(
                "Явный новый запуск может создать новый платный LLM-вызов. Это не бесплатный повтор запроса."
            )
            if st.button("Создать новый запуск после отказа", disabled=not can_retry):
                payload = st.session_state["submission_payload"]
                fingerprint = content_fingerprint(
                    [
                        (f["filename"], base64.b64decode(f["content_base64"]))
                        for f in payload["files"]
                    ]
                )
                action = streamlit_action(fingerprint, retry_of=result.run_id)
                try:
                    send_action(payload, action)
                    st.rerun()
                except (httpx.HTTPError, RuntimeError):
                    st.error(
                        "Новый запуск не подтверждён. Повтор кнопки «Обработать» использует сохранённый ключ."
                    )
        except (httpx.HTTPError, RuntimeError):
            st.info(
                "Состояние предыдущей попытки проверить не удалось. Новая отправка недоступна."
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
    try:
        artifact_states = call("GET", f"/runs/{result.run_id}/artifacts").json()
        for kind, route, title, permitted, cache_key in (
            ("document", "document", "рабочую заявку", decision.allowed, "document"),
            (
                "report",
                "verification-report",
                "отчёт проверки",
                report_decision.allowed,
                "report",
            ),
        ):
            state = artifact_states[kind]
            if state["state"] in ("recovery_required", "damaged"):
                if not state["verified"]:
                    st.session_state.pop(cache_key, None)
                st.warning(
                    f"Нужно восстановить {title}: обнаружена незавершённая запись или повреждение файла."
                )
                st.caption(
                    "Это явное действие сохранит проблемные файлы отдельно и подготовит DOCX из сохранённого результата. Обращений к LLM не будет."
                )
                if st.button(f"Восстановить {title}", disabled=not permitted):
                    try:
                        updated = call(
                            "POST", f"/runs/{result.run_id}/{route}/recover"
                        ).json()
                        st.session_state["result"] = updated
                        st.session_state.pop(cache_key, None)
                        st.rerun()
                    except (httpx.HTTPError, RuntimeError) as exc:
                        st.error(
                            "Восстановление не завершено. Ранее проверенные документы сохранены."
                        )
                        with st.expander("Технические подробности восстановления"):
                            st.text(str(exc))
    except (httpx.HTTPError, RuntimeError):
        st.info(
            "Не удалось проверить состояние файлов DOCX. Доступность скачивания проверяет сервер."
        )
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
    show_acceptance(call, result)


if __name__ == "__main__":
    main()
