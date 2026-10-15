"""Locate a reviewed inline-XBRL fact's original table row without network IO."""

from html.parser import HTMLParser
from typing import Any


def inline_row_html(body: bytes, reviewed: dict[str, Any]) -> str:
    """Return original HTML for the exact reviewed tag/context/unit/value row.

    This helper does not calculate or interpret a financial metric. It checks
    reviewer-supplied inline identity against the original source before the
    manifest builder converts that source row into a visible-text citation.
    """
    expected = reviewed.get("inline_xbrl")
    if not isinstance(expected, dict):
        raise ValueError("reviewed row has no inline XBRL identity")
    source = body.decode("utf-8")
    offsets = [0]
    for index, char in enumerate(source):
        if char == "\n":
            offsets.append(index + 1)

    def numeric(value: str) -> str:
        return value.replace(",", "").replace(" ", "").strip("()\t\r\n+-")

    class Parser(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.rows: list[int] = []
            self.active: tuple[int, list[str]] | None = None
            self.matched: set[int] = set()
            self.results: list[str] = []

        def source_offset(self) -> int:
            line, column = self.getpos()
            return offsets[line - 1] + column

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            if tag == "tr":
                self.rows.append(self.source_offset())
            if tag != "ix:nonfraction" or not self.rows:
                return
            attributes = dict(attrs)
            match = (
                attributes.get("name") == expected.get("name")
                and attributes.get("contextref") == expected.get("context_ref")
                and attributes.get("unitref") == expected.get("unit_ref")
                and (not expected.get("id") or attributes.get("id") == expected["id"])
                and attributes.get("scale", "0") == str(expected.get("scale", "0"))
                and attributes.get("sign") == expected.get("sign")
            )
            if match:
                self.active = (self.rows[-1], [])

        def handle_data(self, data: str) -> None:
            if self.active:
                self.active[1].append(data)

        def handle_endtag(self, tag: str) -> None:
            if tag == "ix:nonfraction" and self.active:
                start, values = self.active
                if numeric("".join(values)) == numeric(str(reviewed.get("value", ""))):
                    self.matched.add(start)
                self.active = None
            if tag == "tr" and self.rows:
                start = self.rows.pop()
                if start in self.matched:
                    end = source.find(">", self.source_offset()) + 1
                    self.results.append(source[start:end])

    parser = Parser()
    parser.feed(source)
    parser.close()
    candidates = list(dict.fromkeys(parser.results))
    if len(candidates) != 1:
        raise ValueError(f"reviewed inline fact resolves to {len(candidates)} original table rows")
    return candidates[0]
