"""Read bank PDF text with the declared Adobe-GB1 CID character mapping.

Some official reports omit ToUnicode for Identity-H fonts. Their extracted
characters are CIDs, not Unicode. Apply Adobe's standard mapping only to those
fonts, in memory; the original bytes and evidence hash remain untouched.
"""

from functools import lru_cache
from importlib.resources import files
from io import BytesIO


@lru_cache(maxsize=1)
def _gb1_cmap() -> bytes:
    return files("investment_agent.industry_peers").joinpath("data/Adobe-GB1-UCS2").read_bytes()


@lru_cache(maxsize=16)
def _report_bytes(body: bytes) -> bytes:
    # ORM reads return fresh equal byte strings. Intern them by actual content
    # so each cached page does not retain another multi-megabyte report copy.
    return body


def bank_page_text(body: bytes, page: int, *, layout: bool = False) -> str:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("bank original PDF page is outside the report")
    return _extract_page(_report_bytes(body), page, layout=layout)


@lru_cache(maxsize=128, typed=True)
def _extract_page(body: bytes, page: int, *, layout: bool = False) -> str:
    from pypdf import PdfReader
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    reader = PdfReader(BytesIO(body))
    if isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= len(reader.pages):
        raise ValueError("bank original PDF page is outside the report")
    target = reader.pages[page - 1]
    visited: set[int] = set()

    def map_resources(resources: DictionaryObject) -> None:
        if id(resources) in visited:
            return
        visited.add(id(resources))
        fonts = resources.get("/Font", DictionaryObject()).get_object()
        for ref in fonts.values():
            font = ref.get_object()
            if font.get("/Encoding") != "/Identity-H" or "/ToUnicode" in font:
                continue
            descendants = font.get("/DescendantFonts", ArrayObject()).get_object()
            if len(descendants) != 1:
                continue
            info = descendants[0].get_object().get("/CIDSystemInfo", DictionaryObject()).get_object()
            if str(info.get("/Registry")) != "Adobe" or str(info.get("/Ordering")) != "GB1":
                continue
            cmap = DecodedStreamObject()
            cmap.set_data(_gb1_cmap())
            font[NameObject("/ToUnicode")] = cmap
        for ref in resources.get("/XObject", DictionaryObject()).get_object().values():
            form = ref.get_object()
            if form.get("/Subtype") == "/Form":
                map_resources(form.get("/Resources", DictionaryObject()).get_object())

    map_resources(target.get("/Resources", DictionaryObject()).get_object())
    return target.extract_text(extraction_mode="layout" if layout else "plain")
