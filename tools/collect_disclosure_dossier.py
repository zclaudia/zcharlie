"""Acquire one exact official report and save bounded, candidate page evidence.

Run inside the application's environment, for example:
  python tools/collect_disclosure_dossier.py 178 2026-03-17 2026-03-19 '福耀玻璃2025年年度报告'
Uses the production disclosure service, immutable receipts/blobs and real import
clock. Never modifies verification, task outcomes, publication gates or budgets.
"""

import argparse
import asyncio
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from investment_agent.config import Settings
from investment_agent.evidence.disclosures import DisclosureService, DiscoveryRequest, FetchRequest
from investment_agent.evidence.documents import (
    OriginalRequest,
    parse_staged,
    save_candidates,
    stage,
)
from investment_agent.storage.database import engine_lifespan, session_scope


async def collect(listing_id: int, start: str, end: str, title: str) -> None:
    async with engine_lifespan(Settings().database_url):
        async with session_scope() as session:
            result = await DisclosureService(session).execute(uuid4(), DiscoveryRequest(
                listing_id=listing_id, start=datetime.fromisoformat(start).replace(tzinfo=UTC),
                as_of=datetime.fromisoformat(end).replace(tzinfo=UTC), limit=20,
            ))
            matches = [item for item in result.get("items", []) if item["title"] == title]
            if len(matches) != 1:
                raise ValueError(f"Expected one exact title; received {result}")
            item = matches[0]
        async with session_scope() as session:
            result = await DisclosureService(session).execute(uuid4(), FetchRequest(
                listing_id=listing_id, item_id=UUID(item["id"]), as_of=datetime.now(UTC),
            ))
            if result.get("status") != "ok":
                raise ValueError(result)
            original = result["original"]
        page, total, text_refs = 1, None, set()
        while total is None or page <= total:
            request = OriginalRequest(listing_id=listing_id, original_id=UUID(original["id"]),
                                      as_of=datetime.now(UTC), page_start=page)
            async with session_scope() as session:
                payload, body = await stage(session, request)
            parsed = await parse_staged(body, payload["mime"], page_start=page, page_count=5)
            if parsed.get("status") != "ok":
                raise ValueError(f"Page window {page}: {parsed}")
            async with session_scope() as session:
                saved = await save_candidates(session, payload, body, parsed, request)
            text_refs.update(ref["id"] for ref in saved["text_refs"])
            total = parsed["page_window"]["total_pages"]
            page = parsed["page_window"]["end"] + 1
            if (page - 1) % 25 == 0 or page > total:
                print(json.dumps({"title": title, "saved_pages": page - 1, "total_pages": total}), flush=True)
        print(json.dumps({"title": title, "original_id": original["id"],
                          "source_url": original["source_url"], "sha256": original["content_hash"],
                          "acquired_at": original["acquired_at"], "page_evidence_count": len(text_refs),
                          "verification": "candidate", "requires_new_message": True}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("listing_id", type=int)
    parser.add_argument("start")
    parser.add_argument("end")
    parser.add_argument("title")
    args = parser.parse_args()
    asyncio.run(collect(args.listing_id, args.start, args.end, args.title))
