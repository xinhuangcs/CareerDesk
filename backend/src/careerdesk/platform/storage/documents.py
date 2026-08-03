"""Document extraction with format validation and PDF/DOCX resource preflight."""

import logging
from pathlib import Path
from zipfile import BadZipFile, ZipFile

logger = logging.getLogger(__name__)

MAX_DOCX_ENTRIES = 2_000
MAX_DOCX_UNCOMPRESSED_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 200

DOCUMENT_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


class _RejectNetworkSession:
    """Deny implicit HTTP access to MarkItDown's local document converters."""

    def request(self, *_args, **_kwargs):
        raise RuntimeError("本地文档解析禁止网络请求")

    def get(self, *args, **kwargs):
        return self.request(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self.request(*args, **kwargs)

def _preflight_docx(path: str) -> None:
    """Inspect ZIP metadata for entry/decompression bombs without extracting."""
    try:
        with ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_DOCX_ENTRIES:
                raise ValueError(f"DOCX 内部文件过多（最多 {MAX_DOCX_ENTRIES} 个）")
            if any(item.flag_bits & 0x1 for item in entries):
                raise ValueError("不支持加密的 DOCX，请先另存为未加密文档")
            uncompressed = sum(item.file_size for item in entries)
            if uncompressed > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError("DOCX 解压后内容过大（最多 32 MB）")
    except BadZipFile as error:
        raise ValueError("DOCX 文件结构无效或已损坏") from error


def _preflight_pdf(path: str) -> None:
    """Walk the PDF page tree before parsing to bound CPU and memory exposure."""
    try:
        from pdfminer.pdfpage import PDFPage

        with Path(path).open("rb") as stream:
            for page_count, _page in enumerate(
                    PDFPage.get_pages(stream, caching=False, check_extractable=False), start=1):
                if page_count > MAX_PDF_PAGES:
                    raise ValueError(f"PDF 页数过多（最多 {MAX_PDF_PAGES} 页）")
    except ValueError:
        raise
    except ImportError as error:
        raise ValueError("解析 PDF 需要完整安装 CareerDesk 后端依赖") from error
    except Exception as error:  # noqa: BLE001 - normalize parser structure failures
        raise ValueError(f"PDF 文件结构无效或已损坏：{error}") from error


def extract_document_text(path: str) -> str:
    """Read a document as text (native txt/md; MarkItDown for PDF/DOCX).

    Raises:
        ValueError: Unsupported suffix, missing dependency, or parsing failure.
    """
    suffix = Path(path).suffix.lower()
    if suffix not in DOCUMENT_SUFFIXES:
        raise ValueError(f"不支持的文档格式：{suffix}（支持 {'/'.join(sorted(DOCUMENT_SUFFIXES))}）")
    if suffix == ".docx":
        _preflight_docx(path)
    elif suffix == ".pdf":
        _preflight_pdf(path)
    try:
        if suffix in {".pdf", ".docx"}:
            # AgentMaker normally constructs a requests.Session. Local files do not need
            # one, so inject a deny-network session to fail closed on future remote access.
            from markitdown import MarkItDown

            text = (
                MarkItDown(
                    requests_session=_RejectNetworkSession(),
                    enable_plugins=False,
                )
                .convert(Path(path))
                .text_content
                or ""
            ).strip()
        else:
            from agentmaker import load_file

            document = load_file(path)
            text = (document.content or "").strip()
    except ImportError as error:
        raise ValueError("解析 PDF/DOCX 需要完整安装 CareerDesk 后端依赖") from error
    except Exception as error:   # noqa: BLE001 - normalize loader failures for users
        logger.error("document parser failed (%s)", type(error).__name__)
        raise ValueError(
            "文档解析失败：无法读取这个文件。请确认文件未损坏、未加密，"
            "并使用完整的最新版 CareerDesk 后重试。"
        ) from error
    if not text:
        raise ValueError("文档解析结果为空（可能是纯扫描图片版 PDF，需要 OCR 后再传）")
    return text
