"""Isolated, bounded text extraction. Source text is data, never instructions.

PDF line locators deliberately do not claim geometrical table reconstruction.
Only HTML tables with ordinary cells are supported. OCR is not configured.
"""

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

VERSION = "original-text.v1"
MAX_INPUT = 4 * 1024 * 1024
MAX_TEXT = 200_000
MAX_FRAGMENTS = 500
MAX_PAGES = 100
TIMEOUT = 8


def fragment(locator: str, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "locator": locator,
        "text": text,
        "content_hash": hashlib.sha256(text.encode()).hexdigest(),
        **extra,
    }


class SafeHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[dict[str, Any]] = []
        self.parts: list[str] = []
        self.hidden = 0
        self.depth = 0
        self.nodes = 0
        self.table = 0
        self.in_table = False
        self.row = 0
        self.cells: list[str] = []
        self.cell: list[str] | None = None
        self.complex_table = False
        self.in_row = False
        self.warnings: list[str] = []

    def paragraph(self) -> None:
        text = " ".join("".join(self.parts).split())
        self.parts = []
        if text:
            self.fragments.append(fragment(f"paragraph:{len(self.fragments) + 1}", text))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.nodes += 1
        self.depth += tag not in {"br", "hr", "img", "meta", "link", "input"}
        if self.nodes > 10000 or self.depth > 64:
            raise ValueError("html_structure_limit")
        if any(
            k == "hidden"
            or k == "aria-hidden"
            and v == "true"
            or k == "style"
            and v is not None
            and (
                "display:none" in v.replace(" ", "").lower()
                or "visibility:hidden" in v.replace(" ", "").lower()
            )
            for k, v in attrs
        ):
            raise ValueError("hidden_html_layout_needs_data")
        if tag in {"script", "style", "iframe", "object", "template", "svg", "noscript"}:
            self.hidden += 1
        if self.hidden:
            return
        if tag == "table":
            self.paragraph()
            if self.in_table:
                raise ValueError("nested_table_needs_data")
            else:
                self.table += 1
                self.row = 0
                self.complex_table = False
            self.in_table = True
        elif tag == "tr" and self.in_table:
            if self.in_row:
                raise ValueError("malformed_table_needs_data")
            self.in_row = True
            self.cells = []
            self.row += 1
        elif tag in {"td", "th"} and self.in_table:
            if not self.in_row or self.cell is not None:
                raise ValueError("malformed_table_needs_data")
            if any(k in {"rowspan", "colspan"} and v not in {None, "1"} for k, v in attrs):
                self.complex_table = True
            self.cell = []
        elif tag in {"p", "div", "h1", "h2", "h3", "br", "li"} and not self.in_table:
            self.paragraph()

    def handle_endtag(self, tag: str) -> None:
        self.depth = max(0, self.depth - 1)
        if tag in {"script", "style", "iframe", "object", "template", "svg", "noscript"}:
            self.hidden = max(0, self.hidden - 1)
            return
        if self.hidden:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cells.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.in_table:
            if not self.in_row or self.cell is not None:
                raise ValueError("malformed_table_needs_data")
            self.in_row = False
            if self.row > 200 or len(self.cells) > 16:
                raise ValueError("table_size_limit")
            self.fragments.append(
                fragment(
                    f"table:{self.table}/row:{self.row}",
                    " | ".join(self.cells),
                    cells=self.cells,
                    table=self.table,
                    row=self.row,
                )
            )
        elif tag == "table":
            if self.in_row or self.cell is not None:
                raise ValueError("malformed_table_needs_data")
            if self.complex_table:
                self.warnings.append("complex_table_needs_data")
                for item in self.fragments:
                    if item.get("table") == self.table:
                        item["unsupported"] = True
            self.in_table = False
        elif tag in {"p", "div", "h1", "h2", "h3", "li"} and not self.in_table:
            self.paragraph()

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.cell is not None:
            self.cell.append(data)
        elif not self.in_table:
            self.parts.append(data)


def extract(body: bytes, mime: str, *, page_start: int | None = None, page_count: int = 5) -> dict[str, Any]:
    warnings: list[str] = []
    if page_start is not None and (not 1 <= page_start <= 2000 or not 1 <= page_count <= 5):
        raise ValueError("invalid_pdf_page_window")
    if not body or len(body) > MAX_INPUT:
        raise ValueError("original_parser_size_limit")
    if mime == "text/html":
        parser = SafeHTML()
        parser.feed(body.decode("utf-8-sig", errors="strict"))
        parser.close()
        parser.paragraph()
        fragments, warnings = parser.fragments, parser.warnings
        if parser.in_table or parser.cell is not None:
            raise ValueError("malformed_table_needs_data")
    elif mime == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise ValueError("pdf_parser_not_installed") from exc
        reader = PdfReader(io.BytesIO(body), strict=True)
        if reader.is_encrypted:
            raise ValueError("encrypted_pdf_unsupported")
        if page_start is None and len(reader.pages) > MAX_PAGES:
            raise ValueError("pdf_page_limit")
        fragments = []
        start = page_start or 1
        end = min(len(reader.pages), start + page_count - 1) if page_start else len(reader.pages)
        if start > len(reader.pages):
            raise ValueError("pdf_page_out_of_range")
        for number in range(start, end + 1):
            page = reader.pages[number - 1]
            contents = page.get_contents()
            if contents is not None and len(contents.get_data()) > 2 * 1024 * 1024:
                raise ValueError("pdf_stream_limit")
            text = page.extract_text() or ""
            if not text.strip():
                warnings.append(f"page:{number}:ocr_needed_not_configured")
            # Image-backed pages may contain an untrusted OCR layer. Keep text
            # available for readback, but never auto-verify its extracted numbers.
            image_backed = bool(page.get("/Resources", {}).get("/XObject"))
            for line, value in enumerate(text.splitlines(), 1):
                if value.strip():
                    fragments.append(
                        fragment(
                            f"page:{number}/line:{line}", value.strip(), unsupported=image_backed
                        )
                    )
            if image_backed and text.strip():
                warnings.append(f"page:{number}:image_text_layer_candidate_only")
            if len(fragments) > MAX_FRAGMENTS or sum(len(f["text"]) for f in fragments) > MAX_TEXT:
                raise ValueError("extracted_text_limit")
    else:
        raise ValueError("original_mime_unsupported")
    if len(fragments) > MAX_FRAGMENTS or sum(len(f["text"]) for f in fragments) > MAX_TEXT:
        raise ValueError("extracted_text_limit")
    return {
        "status": "ok" if fragments else "needs_data",
        "parser_version": VERSION,
        "fragments": fragments,
        "warnings": warnings,
        "ocr": "unsupported_not_configured",
        **({"page_window": {"start": start, "end": end, "total_pages": len(reader.pages)}} if mime == "application/pdf" and page_start else {}),
        "untrusted_source_data": True,
    }


def parse_original(body: bytes, mime: str, *, page_start: int | None = None, page_count: int = 5) -> dict[str, Any]:
    """No DB transaction is held by this function; caller must stage bytes first."""
    if not body or len(body) > MAX_INPUT:
        return {"status": "needs_data", "reason": "original_parser_size_limit", "fragments": []}
    try:
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as target:
            source.write(body)
            source.seek(0)
            child = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), mime, str(page_start or 0), str(page_count)],
                stdin=source,
                stdout=target,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + TIMEOUT
            try:
                while child.poll() is None:
                    if time.monotonic() > deadline:
                        raise ValueError("parser_timeout")
                    if sys.platform == "darwin":
                        # Darwin rejects a small RLIMIT_AS because of reserved
                        # virtual address space. Bound real RSS instead; this
                        # watchdog can overshoot by one 50ms polling interval.
                        usage = subprocess.run(
                            ["/bin/ps", "-o", "rss=", "-p", str(child.pid)],
                            capture_output=True,
                            timeout=1,
                            check=False,
                        )
                        if usage.stdout.strip() and int(usage.stdout) > 512 * 1024:
                            raise ValueError("parser_rss_limit")
                    time.sleep(0.05)
                if child.returncode:
                    raise ValueError("parser_resource_or_process_failure")
                target.seek(0)
                raw = target.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("parser_output_limit")
                material: dict[str, Any] = json.loads(raw)
                return material
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        return {
            "status": "needs_data",
            "reason": str(exc) if isinstance(exc, ValueError) and str(exc).startswith("parser_") else "parser_timeout_or_failure",
            "fragments": [],
            "ocr": "unsupported_not_configured",
        }


if __name__ == "__main__":
    # Set limits before importing the optional parser or decompressing streams.
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (6, 6))
        if sys.platform != "darwin":
            resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, 32))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        output = extract(sys.stdin.buffer.read(MAX_INPUT + 1), sys.argv[1],
                         page_start=(int(sys.argv[2]) or None) if len(sys.argv) > 2 else None,
                         page_count=int(sys.argv[3]) if len(sys.argv) > 3 else 5)
    except Exception as exc:
        reason = (
            "html_encoding_unsupported"
            if isinstance(exc, UnicodeError)
            else str(exc)
            if isinstance(exc, ValueError)
            else "original_parse_failed"
        )
        output = {
            "status": "needs_data",
            "reason": reason[:100],
            "fragments": [],
            "ocr": "unsupported_not_configured",
        }
    sys.stdout.write(json.dumps(output, ensure_ascii=True))
