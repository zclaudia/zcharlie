"""Original-row selection cannot drift to another year or lose an inline sign."""

import pytest
from tools.annual_review_inline import inline_row_html

BODY = b'''<html><table><tr><td>CFO</td><td><ix:nonfraction name="us-gaap:CFO"
contextref="FY2024" unitref="usd" scale="6" id="old">123</ix:nonfraction></td></tr>
<tr><td>CFO</td><td><ix:nonfraction name="us-gaap:CFO" contextref="FY2025"
unitref="usd" scale="6" sign="-" id="new">1,234</ix:nonfraction></td></tr></table></html>'''


def _review():
    return {
        "value": "1,234",
        "inline_xbrl": {
            "name": "us-gaap:CFO", "context_ref": "FY2025", "unit_ref": "usd",
            "scale": "6", "sign": "-", "id": "new",
        },
    }


def test_original_row_uses_context_identity_and_retains_signed_markup():
    row = inline_row_html(BODY, _review())
    assert 'id="new"' in row and 'sign="-"' in row
    assert 'id="old"' not in row
    assert row.encode() in BODY


@pytest.mark.parametrize("field,value", [("context_ref", "FY2024"), ("scale", "3"), ("sign", None)])
def test_conflicting_review_identity_never_selects_a_convenient_row(field, value):
    review = _review()
    review["inline_xbrl"][field] = value
    with pytest.raises(ValueError, match="0 original table rows"):
        inline_row_html(BODY, review)
