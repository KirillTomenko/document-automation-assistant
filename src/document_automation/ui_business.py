"""Compact operator decisions and all-time metrics in the existing Streamlit app."""

from decimal import Decimal

import httpx
import streamlit as st


def _share(value):
    return "Недостаточно данных" if value is None else f"{Decimal(value):.1%}"


def show_metrics(call):
    with st.expander("Качество и экономика — всё время"):
        try:
            m = call("GET", "/business-metrics").json()
            st.write(
                f"Запусков: {m['total_runs']}; completed: {m['completed_runs']} ({_share(m['completed_share'])}); "
                f"failed: {m['failed_runs']}; незавершённых: {m['unfinished_runs']}; needs_review: {m['needs_review_runs']}"
            )
            st.write(
                f"Успешно обработанных входных файлов: {m['successful_inputs']} из {m['registered_inputs']} "
                f"({_share(m['successful_input_share'])}). Файл считается успешным только в completed-запуске."
            )
            if m["runs_without_registered_inputs"]:
                st.caption(
                    f"Запусков без зарегистрированных файлов: {m['runs_without_registered_inputs']}. Их файлы не входят в знаменатель."
                )
            st.write(
                f"LLM-попыток: {m['llm_attempts']}; повторных обращений: {m['retry_calls']}"
            )
            costs = "; ".join(
                f"{amount} {currency}"
                for currency, amount in m["known_estimated_costs"].items()
            )
            st.write(
                "Известная расчётная стоимость LLM API: "
                + (
                    costs
                    or (
                        "0 (только LLM API)"
                        if not m["llm_attempts"] and not m["untracked_runs"]
                        else "нет оценённых сумм; это не подтверждение нулевых расходов"
                    )
                )
            )
            st.write(
                f"Попыток с неизвестной стоимостью: {m['unknown_cost_attempts']}; запусков без полного учёта: {m['untracked_runs']}"
            )
            st.write(f"Принято оператором: {m['accepted_results']}")
            average = m["average_cost_per_accepted"]
            if average is None:
                reasons = {
                    "no_accepted_results": "Нет принятых результатов.",
                    "unknown_cost": "Есть неизвестные расходы; известная часть не является полной суммой.",
                    "multiple_currencies": "Несколько валют; конвертация не выполняется.",
                }
                st.write(
                    "Средняя стоимость принятого результата: Недостаточно данных. "
                    + reasons[m["average_unavailable_reason"]]
                )
            else:
                st.write(
                    f"Средняя стоимость принятого результата: {average} {m['average_currency'] or '(только LLM API, без валютных расходов)'}"
                )
            if m["simulated_runs"]:
                st.warning(
                    f"Сводка содержит симулированные mock-данные: {m['simulated_runs']} запусков. Это демонстрационные показатели."
                )
            for tariff in m["tariffs"]:
                st.caption(
                    f"Тариф {tariff['provider']}/{tariff['model']}, {tariff['version']}, {tariff['currency']}, на {tariff['as_of']}: {tariff['source']}. Цены за миллион токенов."
                )
            st.caption(
                "Числитель средней — расходы всех запусков, включая failed; знаменатель — принятые и не отозванные результаты. Completed, needs_review и принятие — разные признаки. Расчёт API по тарифу не является подтверждённым списанием или полной себестоимостью."
            )
        except (httpx.HTTPError, RuntimeError):
            st.info(
                "Сводные показатели сейчас недоступны. Значения не заменены нулями."
            )


def show_acceptance(call, result):
    st.subheader("Принятие результата оператором")
    st.caption(
        "Подтверждение дальнейшего использования результата. Не является юридическим утверждением или согласованием заказчиком; needs_review и исходные данные сохраняются."
    )
    try:
        status = call("GET", f"/runs/{result.run_id}/acceptance").json()
        if status["accepted"]:
            st.success("Проверено и принято для дальнейшей работы")
            if not status["eligible"]:
                st.warning(
                    "Историческая отметка сохранена, но текущая доступность обоих DOCX не подтверждена. Проверьте и восстановите файлы."
                )
            if st.button("Отозвать принятие результата", key=f"revoke_{result.run_id}"):
                call("POST", f"/runs/{result.run_id}/acceptance/revoke")
                st.rerun()
        else:
            reviewed = False
            if result.needs_review:
                reviewed = st.checkbox(
                    "Замечания needs_review рассмотрены",
                    key=f"reviewed_{result.run_id}",
                )
            if not status["eligible"]:
                st.info(
                    "Сначала завершите обработку, сформируйте и проверьте рабочую заявку и отчёт проверки."
                )
            if st.button(
                "Проверено и принято для дальнейшей работы",
                disabled=not status["eligible"] or result.needs_review and not reviewed,
                key=f"accept_{result.run_id}",
            ):
                call(
                    "POST",
                    f"/runs/{result.run_id}/acceptance",
                    json={"remarks_reviewed": reviewed},
                )
                st.rerun()
        with st.expander("История принятия и отзыва"):
            for event in status["history"]:
                st.text(
                    f"{event['timestamp']} — {'Принято' if event['action'] == 'accepted' else 'Принятие отозвано'}; замечания рассмотрены: {event['remarks_reviewed']}"
                )
    except (httpx.HTTPError, RuntimeError):
        st.error(
            "Не удалось проверить или сохранить отметку. Повтор того же действия не создаёт дубликат."
        )
