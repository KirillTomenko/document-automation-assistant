"""Only invented content and the existing synthetic PDF; never reads private inputs."""

from pathlib import Path

from docx import Document
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]


def build(directory):
    directory.mkdir(parents=True, exist_ok=True)
    # Optional build-only dependency; committed fixtures need no ReportLab at runtime.
    import os

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font = os.environ.get(
        "DAA_FIXTURE_FONT",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    )
    pdfmetrics.registerFont(TTFont("Fixture", font))
    pdf = canvas.Canvas(str(directory / "customer.pdf"))
    pdf.setAuthor("Synthetic regression fixture")
    pdf.setFont("Fixture", 12)
    for index, line in enumerate(
        (
            "Карточка заказчика",
            "Наименование: ООО «Демо Офис»",
            "Email: office@example.com",
        )
    ):
        pdf.drawString(50, 780 - index * 24, line)
    pdf.save()
    doc = Document()
    doc.core_properties.author = "Synthetic regression fixture"
    doc.add_paragraph("Наименование объекта: Учебный офис\nРеквизиты: синтетические")
    doc.add_paragraph(
        "Этапы работ:\nСобрать учебный стол.\nУстановить учебную полку.\n"
        "Стоимость: 1000\nУсловия оплаты: не извлекать"
    )
    doc.save(directory / "description.docx")
    book = Workbook()
    sheet = book.active
    sheet.title = "Sheet1"
    sheet.append(["Синтетический перечень"])
    sheet.append(["№", "Ед. изм.", "Цена", "Наименование работы", "Кол-во", "Сумма"])
    sheet.append([1, "шт.", 100, "Сборка учебного стола", 2, 200])
    sheet.append([2, "м2", 100, "Установка учебной полки", None, 100])
    sheet.append([3, None, None, "Дополнительная учебная работа", 1, None])
    sheet.append([None, None, None, "**ИТОГО:**", None, 300])
    book.properties.creator = "Synthetic regression fixture"
    book.save(directory / "items.xlsx")
    book.close()


if __name__ == "__main__":
    build(ROOT / "demo/V")
