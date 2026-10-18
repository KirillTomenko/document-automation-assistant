from typing import get_args

from document_automation.contracts import (
    DocxLocation,
    IssueCode,
    PdfLocation,
    XlsxLocation,
)
from document_automation.ui_text import ISSUES, STATES, field_label, source_label


def test_all_issues_have_explanation_and_action():
    assert set(ISSUES) == set(get_args(IssueCode))
    assert all(problem and action for problem, action in ISSUES.values())
    assert STATES["ambiguous"] == "Неоднозначно"
    assert STATES["conflict"] == "Противоречие в источниках"


def test_labels_preserve_coordinates_and_human_numbering():
    assert field_label("items[1].unit") == "Позиция 2 — Единица измерения"
    assert field_label("items[0]") == "Позиция 1"
    assert source_label(PdfLocation(page=1)) == "PDF, страница 1"
    assert (
        source_label(XlsxLocation(sheet="Работы", cell="C2"))
        == "Excel, лист Работы, ячейка C2"
    )
    assert source_label(DocxLocation(paragraph=3)) == "Word, абзац 3"
    assert (
        source_label(DocxLocation(section="Работы", table=2, row=3, column=4))
        == "Word, раздел Работы, таблица 2, строка 3, столбец 4"
    )
