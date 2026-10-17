"""Reviewed FY2024 layouts, limited to exact immutable report bytes.

Different bank tables have different column orders, continued headings and
scope bridges. A reviewed profile binds the entire semantic locator and its
supporting pages; it never treats an arbitrary caller's verified flag as proof.
The registry is code-reviewed data, independent of supplied audit fixtures.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.industry_peers.bank_pdf_text import bank_page_text
from investment_agent.industry_peers.hashing import canonical_json
from investment_agent.industry_peers.originals import normalized

FY2024_PROFILE = "bank-fy2024-reviewed-layout/1"
FINGERPRINT_KEYS = (
    "code",
    "period",
    "metric",
    "status",
    "pdf_page",
    "table_title",
    "period_header",
    "current_column",
    "header",
    "raw_value",
    "raw_unit",
    "parsed_value",
    "accounting_scope",
    "locator_excerpt_sha256",
    "definition_or_footnote",
    "scope_context",
    "scope_chain",
    "period_context",
    "equity_derivation",
    "scope_bridge",
    "supplemental_evidence",
    "actual_header_excerpt",
    "actual_header_page",
    "actual_row_excerpt",
    "definition_page",
)

# (actual PDF SHA-256, metric) -> (audited status, complete semantic fingerprint).
# Filled only after independent source/column/scope review; not loaded from user input.
APPROVED: dict[tuple[str, str], tuple[str, str]] = {
    ("27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c", "equity_parent"): (
        "verified",
        "1fbe6f5e8588ce86be7f0c6753efd561152ae7069adb84b89e9a5509f42d6652",
    ),
    ("27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c", "net_interest_margin"): (
        "verified",
        "8f810da5907f1685aa1f4f6f4608dbbf8e31c081bad74ed84f00044ed05b052b",
    ),
    ("27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c", "npl_ratio"): (
        "verified",
        "e31a34101cf518217b30b486cdfbc9f7ab97a425ba4656851972afd306578264",
    ),
    ("27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c", "provision_coverage"): (
        "verified",
        "c80440409fae5023798066964e7f0805cb27fe77cfb0d4b57fe772bc6715728e",
    ),
    ("27b9c08e81061722ab4166ad23d4f90366a3f075af1422543fa6dcb8c7d1a03c", "roe_weighted_annual"): (
        "verified",
        "524ee198d9c9e0310f30374455967be619c8f03f55e05b002fc1249b34b76d8d",
    ),
    ("32ce58cd83cea6a2c09177381554d87e321e08000791da3eb0e4a5ebc7c42977", "equity_parent"): (
        "verified",
        "88709a04efd2159d38068d579f70e9b2fc383da93216c7cdfa72a151f7248456",
    ),
    ("32ce58cd83cea6a2c09177381554d87e321e08000791da3eb0e4a5ebc7c42977", "net_interest_margin"): (
        "verified",
        "26430ef19163e72771d50d4ce0f9140f7792bf5a1dbaf09ae5f075de6554a247",
    ),
    ("32ce58cd83cea6a2c09177381554d87e321e08000791da3eb0e4a5ebc7c42977", "npl_ratio"): (
        "verified",
        "2167987e9b7e70e3b71956146b28bf9b4a36ea43deb113ac5b0b497859e7967f",
    ),
    ("32ce58cd83cea6a2c09177381554d87e321e08000791da3eb0e4a5ebc7c42977", "provision_coverage"): (
        "verified",
        "716a8206273347823092ebb8faff350fa8e78d5d059212a0769220c89c5ba627",
    ),
    ("32ce58cd83cea6a2c09177381554d87e321e08000791da3eb0e4a5ebc7c42977", "roe_weighted_annual"): (
        "verified",
        "e4e4bb89844ec5cfafdd37c3aab1ca6fee4ff76efbcab1133a8c4002be10f32b",
    ),
    ("377c245794096c5dc60bc70b64bf78add24eb17b838219dfc82fc466991ecce2", "equity_parent"): (
        "verified",
        "dd7777b1b0e57c892070e11ea1277142caa142f1ab2500c4056a317893e8ccd8",
    ),
    ("377c245794096c5dc60bc70b64bf78add24eb17b838219dfc82fc466991ecce2", "net_interest_margin"): (
        "verified",
        "e32e76e4e01e0a5631e9294b51ae7e6c53aa0cc5fdc04cb124682db94f82343c",
    ),
    ("377c245794096c5dc60bc70b64bf78add24eb17b838219dfc82fc466991ecce2", "npl_ratio"): (
        "verified",
        "e6b5308636824d0b49a428d42bd64e1ceba9dd99fd4ee03225e419b8169d0030",
    ),
    ("377c245794096c5dc60bc70b64bf78add24eb17b838219dfc82fc466991ecce2", "provision_coverage"): (
        "verified",
        "2564ad80fb04f274e915753d897de395f72e94b187d447c19d7ec0d0587a772a",
    ),
    ("377c245794096c5dc60bc70b64bf78add24eb17b838219dfc82fc466991ecce2", "roe_weighted_annual"): (
        "verified",
        "4b1520d2626f19459e41d54ba42ea1c516bf7de3a137f57ca126612397ed540a",
    ),
    ("44a8759c94e1ba92f1d96897ca3778592d9a7eb33b53420c19268f0b140d81c3", "equity_parent"): (
        "verified",
        "84c52335dbc382debb23f1835265c9aa2250a0f1096c390aad2431d086184ca1",
    ),
    ("44a8759c94e1ba92f1d96897ca3778592d9a7eb33b53420c19268f0b140d81c3", "net_interest_margin"): (
        "verified",
        "17387018aea5d06a0fd3efa03f371ad26d42f4e3369729cb471625115fd7650b",
    ),
    ("44a8759c94e1ba92f1d96897ca3778592d9a7eb33b53420c19268f0b140d81c3", "npl_ratio"): (
        "verified",
        "d363ff0b4807b30b95647824f110cdcb56df595f2de88bc6d293ae414769e1ed",
    ),
    ("44a8759c94e1ba92f1d96897ca3778592d9a7eb33b53420c19268f0b140d81c3", "provision_coverage"): (
        "verified",
        "6b5e4325aaa00939f4f42ef1f28707415503b75bfbda7d3714ba67369a22e9ad",
    ),
    ("44a8759c94e1ba92f1d96897ca3778592d9a7eb33b53420c19268f0b140d81c3", "roe_weighted_annual"): (
        "verified",
        "c0543d7d1d339b707b6b7df4a25782bdabefd9dfb10b1bafe0922f09ecc5db83",
    ),
    ("4534164c8bbb0f99ab16582589dc43b9d46bfd3e2337ab9690401c8b5c4ea2c8", "equity_parent"): (
        "verified",
        "b32b5f9185614839bf2525fe5dbb6197b5a2cc67740c4a8a9d45eee964b98015",
    ),
    ("4534164c8bbb0f99ab16582589dc43b9d46bfd3e2337ab9690401c8b5c4ea2c8", "net_interest_margin"): (
        "verified",
        "60afd767cff8505362f775e8a0c7cfc52e3ea4cc59d875788c73e21a17660495",
    ),
    ("4534164c8bbb0f99ab16582589dc43b9d46bfd3e2337ab9690401c8b5c4ea2c8", "npl_ratio"): (
        "verified",
        "d850129d25db62525f1b9b58f311e8262b2cd629d0174334c5e65fb2755acf08",
    ),
    ("4534164c8bbb0f99ab16582589dc43b9d46bfd3e2337ab9690401c8b5c4ea2c8", "provision_coverage"): (
        "verified",
        "1acce71c7c37c6c31146e6b9c6c0e60c2fcec7db100f56ffcc6ddd08884ef3f1",
    ),
    ("4534164c8bbb0f99ab16582589dc43b9d46bfd3e2337ab9690401c8b5c4ea2c8", "roe_weighted_annual"): (
        "verified",
        "9e93ee94afcc231b296b42fbd6c09bcb2f68484c57d4c9112d06f8f83927df39",
    ),
    ("77d98b2636f31c0a546728637d99da85d00c4d1783e1372b090a753ad46e14f1", "equity_parent"): (
        "verified",
        "2a2fc100660db7f23531cdafce5cd364d9f71d1cb40418e3ab358822c2ea9555",
    ),
    ("77d98b2636f31c0a546728637d99da85d00c4d1783e1372b090a753ad46e14f1", "net_interest_margin"): (
        "verified",
        "9b91771755ab8dfe3ccb3115fa0b7702a1bb6494b23f98ddfa768b890b5af2d2",
    ),
    ("77d98b2636f31c0a546728637d99da85d00c4d1783e1372b090a753ad46e14f1", "npl_ratio"): (
        "scope_mismatch",
        "63bb912ea0a6f2e9b90ba35a181c8b9b5b5781ce348cefb7c4d327cfec98ae1d",
    ),
    ("77d98b2636f31c0a546728637d99da85d00c4d1783e1372b090a753ad46e14f1", "provision_coverage"): (
        "scope_mismatch",
        "2e1091519f9861c17d7848d34abdd0de1fcf4b4811d272bd1a34aaad6c69cb1a",
    ),
    ("77d98b2636f31c0a546728637d99da85d00c4d1783e1372b090a753ad46e14f1", "roe_weighted_annual"): (
        "verified",
        "873c938850aee9c0f63fa031b8a3189ce50d5fc860c09a0a18509ebe2600517e",
    ),
    ("b14044cfee0301f175f1fd7146dc0b5668e87b07210a73df2a530c61ea4d6156", "equity_parent"): (
        "verified",
        "b5243f74536f450c8688eb088375ee7b198d5a322b054f8cba75a2d6054e3aea",
    ),
    ("b14044cfee0301f175f1fd7146dc0b5668e87b07210a73df2a530c61ea4d6156", "net_interest_margin"): (
        "candidate",
        "2850f2f2f982adc39c71033b3c032806342bae0515a1479632c3e25f415b3b2a",
    ),
    ("b14044cfee0301f175f1fd7146dc0b5668e87b07210a73df2a530c61ea4d6156", "npl_ratio"): (
        "verified",
        "5f121d9b0a359725202faa276c6b23887475cca8876f2a302ea7a207f05f4dcf",
    ),
    ("b14044cfee0301f175f1fd7146dc0b5668e87b07210a73df2a530c61ea4d6156", "provision_coverage"): (
        "verified",
        "6a6a44572c4b2e85b47a257164ac9c64efb9a7f0b90c5599f718beba767a1608",
    ),
    ("b14044cfee0301f175f1fd7146dc0b5668e87b07210a73df2a530c61ea4d6156", "roe_weighted_annual"): (
        "verified",
        "c28eed54f2ddaab81115d7a72c1a757cebb10aace3e17b1a866af7677e47b9c5",
    ),
    ("b7784a8dd0b6b2798683c9db9ea5cb129eabd30f0352f0cce080bb8d06fc17b8", "equity_parent"): (
        "verified",
        "507db6e74b75283643f16a5e3c9c0049a49d51f7e5a9b9653981f2eb55966f29",
    ),
    ("b7784a8dd0b6b2798683c9db9ea5cb129eabd30f0352f0cce080bb8d06fc17b8", "net_interest_margin"): (
        "verified",
        "a3bbdd09ae32d05f94be5ea27b61cf0ca76523d5614ab8af4de3c557ec29f5d7",
    ),
    ("b7784a8dd0b6b2798683c9db9ea5cb129eabd30f0352f0cce080bb8d06fc17b8", "npl_ratio"): (
        "verified",
        "171cac688cd4893f17b33ff2d38c902f5dcbe4e7841edaaf86fd8a514f577db8",
    ),
    ("b7784a8dd0b6b2798683c9db9ea5cb129eabd30f0352f0cce080bb8d06fc17b8", "provision_coverage"): (
        "verified",
        "830a3e3eaa8b68bb6f460bd9dd825580ee0aec0453692bcd1303402237c48981",
    ),
    ("b7784a8dd0b6b2798683c9db9ea5cb129eabd30f0352f0cce080bb8d06fc17b8", "roe_weighted_annual"): (
        "verified",
        "10b62352603282fbde79ad812931694cf550f1e9e222ceca8adf64b34a7ed23a",
    ),
    ("cc5248615b1e259b5985e6156cdc8ed6a0c0aa4a213633858cdd4f5da10e0d98", "equity_parent"): (
        "verified",
        "a2d01a113b93e3c7e25b8294aa2aa2017ddf92d3399736f07d227f7a76baabc5",
    ),
    ("cc5248615b1e259b5985e6156cdc8ed6a0c0aa4a213633858cdd4f5da10e0d98", "net_interest_margin"): (
        "verified",
        "d09f4809c2c2dbdeee589a5ba9952f778ca2c83f8b2fc2bb0620da0e47ff58d3",
    ),
    ("cc5248615b1e259b5985e6156cdc8ed6a0c0aa4a213633858cdd4f5da10e0d98", "npl_ratio"): (
        "verified",
        "2d40cf92b139ec9bcc83c30ed81c472fd872e09b936e28cef5be7b85eab5c0b4",
    ),
    ("cc5248615b1e259b5985e6156cdc8ed6a0c0aa4a213633858cdd4f5da10e0d98", "provision_coverage"): (
        "verified",
        "e99de1247c9a0bf26546156465c7f26b0f977e8c93d1380696fcc9c8ac674346",
    ),
    ("cc5248615b1e259b5985e6156cdc8ed6a0c0aa4a213633858cdd4f5da10e0d98", "roe_weighted_annual"): (
        "verified",
        "b732abd12ec8edefa38fb950dcbc0a5ac79e6d58bd86996af718c36b3dc8b663",
    ),
    ("d1b2dbd50b81a8a52ae77357dd4f03a6887404fae5ed7067f7d18bfd4c141d2d", "equity_parent"): (
        "verified",
        "bb8f0e44eda2dd5c5c22a2c7cf5b3b1f5b752be64fd570c042001d032100b9d4",
    ),
    ("d1b2dbd50b81a8a52ae77357dd4f03a6887404fae5ed7067f7d18bfd4c141d2d", "net_interest_margin"): (
        "verified",
        "424adae41c7081df68a023278b247959e9fa6b523e5addc2106ba357208b1eb0",
    ),
    ("d1b2dbd50b81a8a52ae77357dd4f03a6887404fae5ed7067f7d18bfd4c141d2d", "npl_ratio"): (
        "verified",
        "19fc77daca83b5cfd4b07622a750a87ff621aa0eae13964d067cf1eb85a168bb",
    ),
    ("d1b2dbd50b81a8a52ae77357dd4f03a6887404fae5ed7067f7d18bfd4c141d2d", "provision_coverage"): (
        "verified",
        "fdfb8014d12d3c5c727002d116af2749c4b2d7003151e4dcaa5ac0ad5b466226",
    ),
    ("d1b2dbd50b81a8a52ae77357dd4f03a6887404fae5ed7067f7d18bfd4c141d2d", "roe_weighted_annual"): (
        "verified",
        "71c61f4dd3be21582094fcc4998f486690355df81a630202cbe530c56e7d1e2f",
    ),
}


def record_fingerprint(proof: dict[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json({key: proof.get(key) for key in FINGERPRINT_KEYS}).encode()
    ).hexdigest()


def checked_fy2024_layout(body: bytes, period: date, metric: str, proof: dict[str, Any]) -> None:
    key = (hashlib.sha256(body).hexdigest(), metric)
    expected = APPROVED.get(key)
    if (
        period != date(2024, 12, 31)
        or proof.get("layout_profile") != FY2024_PROFILE
        or proof.get("metric") != metric
        or expected is None
        or proof.get("status") != expected[0]
        or record_fingerprint(proof) != expected[1]
    ):
        raise ValueError("FY2024 source/field locator is not an approved reviewed profile")
    audit = proof.get("audit_record")
    if isinstance(audit, dict) and record_fingerprint(audit) != expected[1]:
        raise ValueError("FY2024 profile differs between proof and audit")
    page_no = proof["pdf_page"]
    excerpt = proof["locator_excerpt"]
    if (
        isinstance(page_no, bool)
        or not isinstance(page_no, int)
        or hashlib.sha256(excerpt.encode()).hexdigest() != proof["locator_excerpt_sha256"]
        or normalized(excerpt) not in normalized(bank_page_text(body, page_no))
    ):
        raise ValueError("FY2024 field excerpt is absent from the actual original page")
    text_parts = [excerpt]
    for key_name, key_page in (
        ("actual_header_excerpt", "actual_header_page"),
        ("actual_row_excerpt", "pdf_page"),
    ):
        if proof.get(key_name):
            part = proof[key_name]
            if normalized(part) not in normalized(
                bank_page_text(body, proof.get(key_page, page_no))
            ):
                raise ValueError("FY2024 actual row/header excerpt is not present on its page")
            text_parts.append(part)
    contexts = list(proof.get("scope_chain") or [])
    for key_name in ("scope_context", "period_context"):
        if isinstance(proof.get(key_name), dict):
            contexts.append(proof[key_name])
    for context in contexts:
        if context.get("source_key"):
            continue  # Its independent original is checked by the async readback below.
        part = context["excerpt"]
        if hashlib.sha256(part.encode()).hexdigest() != context["sha256"] or normalized(
            part
        ) not in normalized(bank_page_text(body, context["page"])):
            raise ValueError("FY2024 scope/period chain differs from actual original bytes")
        text_parts.append(part)
    note = normalized(str(proof["definition_or_footnote"]))
    if proof.get("definition_page"):
        text_parts.append(bank_page_text(body, proof["definition_page"]))
    if not note or not any(note in normalized(part) for part in text_parts):
        raise ValueError("FY2024 definition/footnote is not an original source excerpt")
    raw = Decimal(str(proof["raw_value"]).replace(",", ""))
    parsed = Decimal(str(proof["parsed_value"]))
    factor = {
        "percent": Decimal(".01"),
        "CNY": Decimal(1),
        "CNY_thousand": Decimal(1000),
        "CNY_million": Decimal(1000000),
    }.get(proof["raw_unit"])
    if not raw.is_finite() or not parsed.is_finite() or factor is None or raw * factor != parsed:
        raise ValueError("FY2024 raw and canonical units/values are inconsistent")


async def validate_reviewed_support(
    session: AsyncSession, snapshot: Any, proof: dict[str, Any], boundary: Any
) -> None:
    """Require both owner-scoped originals for the reviewed Ningbo FY2024 bridge."""
    from datetime import UTC, datetime

    from investment_agent.industry_peers.bank_scope_bridges import ningbo_final_source_identity
    from investment_agent.storage.models.disclosure import (
        DisclosureBlob,
        DisclosureItem,
        DisclosureOriginal,
    )

    contexts = list(proof.get("scope_chain") or [])
    if isinstance(proof.get("scope_context"), dict):
        contexts.append(proof["scope_context"])
    external = [context for context in contexts if context.get("source_key")]
    refs = proof.get("supplemental_originals") or []
    audit = proof.get("audit_record", {})
    if refs != audit.get("supplemental_originals", []):
        raise ValueError("FY2024 supplemental references differ between proof and audit")
    if not external:
        if refs:
            raise ValueError("unexpected FY2024 supplemental original")
        return
    if (
        proof.get("code") != "002142.SZ"
        or proof.get("metric") not in {"npl_ratio", "provision_coverage"}
        or len(refs) != 1
        or snapshot.report_period != date(2024, 12, 31)
    ):
        raise ValueError("unsupported FY2024 cross-original scope")
    expected = ningbo_final_source_identity()
    descriptor = refs[0]
    original_ref = descriptor["original_ref"]
    row = await session.get(DisclosureOriginal, original_ref["id"])
    if (
        original_ref.get("kind") != "disclosure_original"
        or original_ref.get("revision") != 1
        or row is None
        or row.owner_id != "local_owner"
        or row.payload.get("listing_id") != snapshot.listing_id
        or row.payload.get("ticker") != "002142.SZ"
        or row.content_hash != expected["pdf_sha256"]
        or any(
            row.payload.get(key) != value
            for key, value in expected.items()
            if key not in {"pdf_sha256", "url"}
        )
        or row.payload.get("source_url") != expected["url"]
        or descriptor.get("pdf_sha256") != row.content_hash
    ):
        raise ValueError("FY2024 supplemental identity/owner/listing/hash mismatch")
    item = await session.get(DisclosureItem, row.item_id)
    blob = await session.get(DisclosureBlob, row.content_hash)
    if (
        item is None
        or item.owner_id != "local_owner"
        or item.listing_id != snapshot.listing_id
        or item.payload.get("source_identity") != row.payload.get("source_identity")
        or blob is None
        or hashlib.sha256(blob.body).hexdigest() != row.content_hash
    ):
        raise ValueError("FY2024 supplemental item/blob binding mismatch")
    checked = datetime.fromisoformat(proof["checked_at"])
    for key_name in ("acquired_at", "available_from"):
        stamp = datetime.fromisoformat(row.payload[key_name])
        if stamp.tzinfo is None or not stamp <= checked <= boundary:
            raise ValueError("FY2024 supplementary evidence was unavailable by cutoff")
    if date.fromisoformat(row.payload["published_on"]) > checked.astimezone(UTC).date():
        raise ValueError("FY2024 supplementary publication is after evidence cutoff")
    for context in external:
        part = context["excerpt"]
        if (
            context["source_key"] != descriptor.get("source_key")
            or hashlib.sha256(part.encode()).hexdigest() != context["sha256"]
            or normalized(part) not in normalized(bank_page_text(blob.body, context["page"]))
        ):
            raise ValueError("FY2024 supplemental page/column context is invalid")
