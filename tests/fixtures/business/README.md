# Synthetic customer card

`customer.pdf` is a one-page text PDF with the exact customer-card content from
DEMO_CONTRACT.md scenario A. It contains only synthetic data and office@example.com.
The Cyrillic font is embedded as a subset; the space glyph's ToUnicode mapping
explicitly maps to U+0020, matching the authored text. No images, scripts, forms,
attachments or external links are needed.

Created once with PyMuPDF and checked with pypdf and a rendered page. Generation
tools are not test dependencies: the regression test reads these committed bytes
using the existing production PDF parser. DOCX and XLSX are generated in pytest's
temporary directory, parsed normally and combined with this PDF for scenario A.
