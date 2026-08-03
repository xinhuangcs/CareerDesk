"""Real document-extraction fixtures used by resume adaptation.

The adaptation workflow consumes ``resumes.content_text`` exactly as the local
document reader produced it.  These fixtures therefore test the production
reader instead of pretending that any non-empty string proves visual fidelity.
"""

from __future__ import annotations

from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from careerdesk.orchestration.application_prep.adaptation import (
    assess_resume_extraction,
    exact_text_segments,
)
from careerdesk.platform.storage.documents import extract_document_text


def _pdf_literal(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _text_stream(items: list[tuple[int, int, str]]) -> bytes:
    commands = [
        f"BT /F1 11 Tf 1 0 0 1 {x} {y} Tm ({_pdf_literal(text)}) Tj ET"
        for x, y, text in items
    ]
    return "\n".join(commands).encode("ascii")


def _pdf_document(pages: list[list[tuple[int, int, str]]]) -> bytes:
    """Build a tiny valid PDF without adding a test-only PDF writer dependency."""

    page_object_numbers = [4 + index * 2 for index in range(len(pages))]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            f"<< /Type /Pages /Kids [{' '.join(f'{number} 0 R' for number in page_object_numbers)}] "
            f"/Count {len(pages)} >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for index, items in enumerate(pages):
        page_number = page_object_numbers[index]
        content_number = page_number + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        stream = _text_stream(items)
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )

    payload = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode("ascii"))
        payload.extend(body)
        payload.extend(b"\nendobj\n")
    xref_offset = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    payload.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        payload.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    payload.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(payload)


def _docx_document() -> bytes:
    content_types = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""
    document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:v="urn:schemas-microsoft-com:vml">
 <w:body>
  <w:p><w:r><w:t>PROFILE START</w:t></w:r></w:p>
  <w:tbl>
   <w:tr><w:tc><w:p><w:r><w:t>TABLE ROLE</w:t></w:r></w:p></w:tc>
   <w:tc><w:p><w:r><w:t>TABLE IMPACT 42%</w:t></w:r></w:p></w:tc></w:tr>
  </w:tbl>
  <w:p><w:r><w:pict><v:shape><v:textbox><w:txbxContent>
   <w:p><w:r><w:t>TEXTBOX CERTIFICATE</w:t></w:r></w:p>
  </w:txbxContent></v:textbox></v:shape></w:pict></w:r></w:p>
  <w:p><w:r><w:t>混合语言 Python 数据平台</w:t></w:r></w:p>
  <w:sectPr/>
 </w:body>
</w:document>""".encode("utf-8")
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def test_single_and_two_column_pdf_fixtures_preserve_extracted_content_order(tmp_path):
    single = tmp_path / "single-column.pdf"
    single.write_bytes(_pdf_document([[
        (72, 740, "SUMMARY FIRST"),
        (72, 710, "EXPERIENCE SECOND"),
        (72, 680, "EDUCATION THIRD"),
    ]]))
    single_text = extract_document_text(str(single))
    assert single_text.index("SUMMARY FIRST") < single_text.index("EXPERIENCE SECOND")
    assert single_text.index("EXPERIENCE SECOND") < single_text.index("EDUCATION THIRD")

    columns = tmp_path / "two-column.pdf"
    columns.write_bytes(_pdf_document([[
        (72, 740, "LEFT SKILLS"),
        (72, 710, "LEFT LANGUAGES"),
        (330, 740, "RIGHT EXPERIENCE"),
        (330, 710, "RIGHT IMPACT"),
    ]]))
    column_text = extract_document_text(str(columns))
    assert column_text.index("LEFT SKILLS") < column_text.index("LEFT LANGUAGES")
    assert column_text.index("RIGHT EXPERIENCE") < column_text.index("RIGHT IMPACT")
    assert all(
        marker in column_text
        for marker in ("LEFT SKILLS", "LEFT LANGUAGES", "RIGHT EXPERIENCE", "RIGHT IMPACT")
    )


def test_repeated_pdf_headers_and_footers_are_not_silently_deleted(tmp_path):
    path = tmp_path / "repeated-header-footer.pdf"
    path.write_bytes(_pdf_document([
        [(72, 760, "RESUME HEADER"), (72, 700, "PAGE ONE ROLE"), (72, 40, "PRIVATE FOOTER")],
        [(72, 760, "RESUME HEADER"), (72, 700, "PAGE TWO ROLE"), (72, 40, "PRIVATE FOOTER")],
    ]))
    text = extract_document_text(str(path))
    assert text.count("RESUME HEADER") == 2
    assert text.count("PRIVATE FOOTER") == 2
    assert text.index("PAGE ONE ROLE") < text.index("PAGE TWO ROLE")
    assert "".join(segment.text for segment in exact_text_segments(text, namespace="R")) == text


def test_scanned_pdf_fixture_fails_closed_instead_of_claiming_text_analysis(tmp_path):
    path = tmp_path / "scanned.pdf"
    path.write_bytes(_pdf_document([[]]))
    with pytest.raises(ValueError, match="扫描图片版 PDF"):
        extract_document_text(str(path))
    receipt = assess_resume_extraction("", source_suffix=".pdf")
    assert receipt.status == "reupload_required"
    assert "scanned_pdf_without_text_layer" in receipt.reason_codes


def test_docx_table_textbox_and_non_latin_fixture_crosses_the_real_reader(tmp_path):
    path = tmp_path / "structured.docx"
    path.write_bytes(_docx_document())
    text = extract_document_text(str(path))
    assert text.index("PROFILE START") < text.index("TABLE ROLE")
    assert "TABLE IMPACT 42%" in text
    assert "TEXTBOX CERTIFICATE" in text
    assert "混合语言 Python 数据平台" in text
    assert assess_resume_extraction(text, source_suffix=".docx").usable
    assert "".join(segment.text for segment in exact_text_segments(text, namespace="R")) == text
