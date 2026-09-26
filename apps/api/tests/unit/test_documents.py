from pathlib import Path

import pytest

from app.domain.errors import DocumentError, ErrorCode
from app.infrastructure.documents.pymupdf_processor import PyMuPDFDocumentProcessor
from tests.conftest import make_pdf


def processor(**kw: float) -> PyMuPDFDocumentProcessor:
    args = {"max_pages": 5, "max_chars": 10_000, "timeout_s": 20}
    args.update(kw)
    return PyMuPDFDocumentProcessor(**args)  # type: ignore[arg-type]


async def extract(tmp_path: Path, data: bytes, **kw: float):  # type: ignore[no-untyped-def]
    pdf = tmp_path / "document.pdf"
    pdf.write_bytes(data)
    return await processor(**kw).extract(pdf, tmp_path)


async def test_extracts_text_and_leaves_no_file(tmp_path: Path) -> None:
    doc = await extract(tmp_path, make_pdf("Quarterly synthetic report", pages=2))
    assert doc.page_count == 2
    assert "Quarterly synthetic report" in doc.text
    assert not doc.truncated
    assert not (tmp_path / "extracted.txt").exists()


async def test_rejects_too_many_pages(tmp_path: Path) -> None:
    with pytest.raises(DocumentError) as err:
        await extract(tmp_path, make_pdf(pages=6))
    assert err.value.code is ErrorCode.DOCUMENT_TOO_MANY_PAGES


async def test_rejects_non_pdf_signature(tmp_path: Path) -> None:
    with pytest.raises(DocumentError) as err:
        await extract(tmp_path, b"MZ\x90\x00 definitely an executable")
    assert err.value.code is ErrorCode.DOCUMENT_INVALID


async def test_rejects_corrupt_pdf(tmp_path: Path) -> None:
    with pytest.raises(DocumentError) as err:
        await extract(tmp_path, b"%PDF-1.7\n" + b"\x00garbage" * 100)
    assert err.value.code in (ErrorCode.DOCUMENT_INVALID, ErrorCode.DOCUMENT_EMPTY)


async def test_rejects_pdf_without_text(tmp_path: Path) -> None:
    import pymupdf

    d = pymupdf.open()
    d.new_page()
    with pytest.raises(DocumentError) as err:
        await extract(tmp_path, d.tobytes())
    assert err.value.code is ErrorCode.DOCUMENT_EMPTY


async def test_truncates_to_max_chars(tmp_path: Path) -> None:
    doc = await extract(tmp_path, make_pdf("x" * 200, pages=5), max_chars=300)
    assert doc.truncated
    assert len(doc.text) <= 300


async def test_extraction_timeout_kills_child(tmp_path: Path) -> None:
    with pytest.raises(DocumentError) as err:
        await extract(tmp_path, make_pdf(pages=3), timeout_s=0.001)
    assert err.value.code is ErrorCode.DOCUMENT_EXTRACTION_TIMEOUT
