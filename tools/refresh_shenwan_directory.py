"""Refresh ``config/shenwan_2021.yaml`` (Shenwan 2021 code -> name directory, P1-A9).

Run it about monthly, or when ``industry_sync`` logs ``shenwan_codes_unresolved``:

1. ``stock_industry_clf_hist_sw`` (AkShare) -> every stock's current code;
2. Shenwan's official code table ``SwClassCode_2021.xls`` (swsresearch.com,
   one request) -> code, name, level, parent;
3. reports current 2021-era codes missing from the table.

Without ``--write`` it only prints the diff against the committed file
(added / removed / renamed codes). With ``--write`` it rewrites the file;
review and commit it yourself (nothing is committed automatically). If a
level-1/2 code appears or changes, update ``config/industry_groups.yaml``
(the coverage tests fail until it is mapped) and bump its version.

``--cross-check`` additionally rebuilds the directory the slow way planned in
p1-plan 2.2 (legulegu ``sw_index_*_info`` + one ``sw_index_third_cons`` per
level-3 index) and compares names; it also checks the exchange-suffix rule
of ``to_canonical_ticker`` against legulegu's suffixed constituent tickers.
legulegu throttles bursts (HTTP 429, then 504 for minutes): use ``--cache
DIR`` so an interrupted run resumes, and ``--limit N`` for a sample.

``--fixtures`` rewrites the offline test fixtures under ``tests/fixtures/shenwan/``
(history rows of the CSI 300 fixture members plus edge cases, the official
code table, and - with ``--cross-check`` - the legulegu info tables and two
level-3 constituent lists).

Usage::

    .venv/bin/python tools/refresh_shenwan_directory.py [--write] [--fixtures]
        [--cross-check [--limit N] [--cache DIR]]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from investment_agent.providers.akshare_shenwan import (  # noqa: E402
    AkshareShenwanBackend,
    AkshareShenwanClient,
    InfoLevel,
    RawTable,
    ShenwanHistory,
    parse_index_info,
    to_canonical_ticker,
)
from investment_agent.providers.shenwan_directory import (  # noqa: E402
    DEFAULT_SHENWAN_DIRECTORY_PATH,
    SW2021_CUTOVER,
    ShenwanDirectory,
    dump_shenwan_directory,
    load_shenwan_directory,
)

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "shenwan"
CSI300_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "factors" / "csi300_industry_2026-10-05.json"
EXTRA_SYMBOLS = (
    "920002",  # Beijing (920 prefix)
    "832317",  # Beijing (legacy 8xxxxx code)
    "000003",  # delisted, pre-2021 code only
    "000004",  # seven changes
    "000005",
)
FIXTURE_CONS_INDEXES = ("850111.SI", "850339.SI")  # 种子, 其他化学制品
CONS_FIXTURE_COLUMNS = ("序号", "股票代码", "股票简称", "纳入时间", "申万3级")


class RecordingBackend:
    """Real backend that keeps every raw table (fixtures) and caches legulegu answers."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        self.real = AkshareShenwanBackend()
        self.cache_dir = cache_dir
        self.history: RawTable | None = None
        self.class_codes: RawTable | None = None
        self.info: dict[int, RawTable] = {}
        self.cons: dict[str, RawTable] = {}

    def industry_history(self) -> RawTable:
        self.history = self.real.industry_history()
        return self.history

    def class_code_table(self) -> RawTable:
        self.class_codes = self.real.class_code_table()
        return self.class_codes

    def index_info(self, level: InfoLevel) -> RawTable:
        self.info[level] = self._cached(f"info-{level}", lambda: self.real.index_info(level))
        return self.info[level]

    def third_constituents(self, index_code: str) -> RawTable:
        table = self._cached(index_code, lambda: self.real.third_constituents(index_code))
        self.cons[index_code] = table
        return table

    def _cached(self, key: str, fetch: Callable[[], RawTable]) -> RawTable:
        cached = self.cache_dir / f"{key}.json" if self.cache_dir else None
        if cached is not None and cached.is_file():
            payload = json.loads(cached.read_text(encoding="utf-8"))
            return RawTable(tuple(payload["columns"]), tuple(payload["rows"]))
        table = fetch()
        if cached is not None:
            cached.parent.mkdir(parents=True, exist_ok=True)
            cached.write_text(
                json.dumps(_table_json(table), ensure_ascii=False, default=str), encoding="utf-8"
            )
        return table


def _table_json(table: RawTable, rows: list[Any] | None = None) -> dict[str, Any]:
    return {"columns": list(table.columns), "rows": rows if rows is not None else list(table.rows)}


def _csi300_symbols() -> list[str]:
    payload = json.loads(CSI300_FIXTURE.read_text(encoding="utf-8"))
    return sorted(member["code"].split(".")[1] for member in payload["members"])


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8"
    )


def _write_fixtures(backend: RecordingBackend, captured_on: date) -> None:
    assert backend.history is not None and backend.class_codes is not None
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    wanted = set(_csi300_symbols()) | set(EXTRA_SYMBOLS)
    for code in FIXTURE_CONS_INDEXES:  # constituents of the cross-check fixture indexes
        for row in backend.cons[code].rows if code in backend.cons else ():
            wanted.add(str(row["股票代码"]).split(".")[0])
    rows = [row for row in backend.history.rows if str(row["symbol"]) in wanted]
    _dump(
        FIXTURE_DIR / "history.json",
        {
            "source": "akshare 1.19.1 stock_industry_clf_hist_sw (swsresearch.com)",
            "captured_on": captured_on.isoformat(),
            "note": "rows of the CSI 300 fixture members (2026-10-05), "
            + ", ".join(EXTRA_SYMBOLS)
            + " and the constituents of "
            + ", ".join(FIXTURE_CONS_INDEXES),
            "full_rows": len(backend.history.rows),
            **_table_json(backend.history, rows),
        },
    )
    _dump(
        FIXTURE_DIR / "class_code.json",
        {
            "source": "swsresearch.com SwClassCode_2021.xls",
            "captured_on": captured_on.isoformat(),
            **_table_json(backend.class_codes),
        },
    )
    if backend.info:
        _dump(
            FIXTURE_DIR / "index_info.json",
            {
                "source": "akshare 1.19.1 sw_index_first/second/third_info (legulegu.com)",
                "captured_on": captured_on.isoformat(),
                **{f"level{level}": _table_json(table) for level, table in backend.info.items()},
            },
        )
    cons = {
        code: _table_json(
            RawTable(
                CONS_FIXTURE_COLUMNS,
                tuple(
                    {key: row.get(key) for key in CONS_FIXTURE_COLUMNS}
                    for row in backend.cons[code].rows
                ),
            )
        )
        for code in FIXTURE_CONS_INDEXES
        if code in backend.cons
    }
    if cons:
        _dump(
            FIXTURE_DIR / "constituents.json",
            {
                "source": "akshare 1.19.1 sw_index_third_cons (legulegu.com)",
                "captured_on": captured_on.isoformat(),
                "indexes": cons,
            },
        )
    print(f"fixtures written to {FIXTURE_DIR} ({len(rows)} history rows)")


def _unnamed_current_codes(history: ShenwanHistory, directory: ShenwanDirectory) -> list[str]:
    """Codes assigned on / after 2021-07-30 that the directory cannot name.

    Older open-ended codes belong to stocks delisted before the 2021 standard
    (91 stocks on 2026-10-06) and are expected to be unnamed.
    """

    codes = set()
    for changes in history.changes.values():
        for change in changes:
            if change.start_date >= SW2021_CUTOVER and directory.get(change.code) is None:
                codes.add(change.code)
    return sorted(codes)


async def _cross_check(
    client: AkshareShenwanClient,
    backend: RecordingBackend,
    history: ShenwanHistory,
    official: ShenwanDirectory,
    limit: int | None,
) -> None:
    index_codes = None
    if limit is not None:
        third = parse_index_info(await asyncio.to_thread(backend.index_info, 3), 3)
        chosen = [info.index_code for info in third[:limit]]
        index_codes = sorted(set(chosen) | set(FIXTURE_CONS_INDEXES))

    def progress(position: int, total: int, code: str) -> None:
        if position % 25 == 0 or position == total:
            print(f"  constituents {position}/{total} ({code})", flush=True)

    reverse, lookups, constituents = await client.reverse_lookup_directory(
        history, index_codes=index_codes, progress=progress
    )
    total = sum(len(tickers) for tickers in constituents.values())
    mismatched = [
        ticker
        for tickers in constituents.values()
        for ticker in tickers
        if to_canonical_ticker(ticker.split(".")[0]) != ticker
    ]
    print(
        f"exchange rule: {total - len(mismatched)}/{total} constituent tickers agree "
        f"{mismatched[:10]}"
    )
    diff = official.diff(reverse)
    resolved = [lookup for lookup in lookups if lookup.code]
    print(
        f"cross-check: {len(resolved)}/{len(lookups)} level-3 indexes resolved; "
        f"codes unknown to the official table: {list(diff.added)}; "
        f"name mismatches: {list(diff.renamed)}"
    )


async def main(args: argparse.Namespace) -> int:
    backend = RecordingBackend(Path(args.cache) if args.cache else None)
    # legulegu.com answers HTTP 429 to bursts: pace the constituent calls
    client = AkshareShenwanClient(
        backend, max_attempts=6, retry_backoff_seconds=15.0, constituent_pause_seconds=1.5
    )
    history = await client.fetch_industry_history()
    print(f"history: {len(history.changes)} tickers, skipped {list(history.skipped_symbols)}")
    directory = await client.fetch_code_directory()
    print(
        f"directory: L1 {len(directory.codes(1))}, L2 {len(directory.codes(2))}, "
        f"L3 {len(directory.codes(3))}; codes assigned since 2021-07-30 without a name: "
        f"{_unnamed_current_codes(history, directory)}"
    )
    if args.cross_check:
        await _cross_check(client, backend, history, directory, args.limit)
    _finish(args, directory, backend)
    return 0


def _finish(args: argparse.Namespace, directory: ShenwanDirectory, backend: RecordingBackend) -> None:
    target = Path(args.path)
    if target.is_file():
        diff = load_shenwan_directory(target).diff(directory)
        print(f"diff vs {target}: added {list(diff.added)}, removed {list(diff.removed)}")
        for code, old, new in diff.renamed:
            print(f"  renamed {code}: {old} -> {new}")
    if args.write:
        target.write_text(dump_shenwan_directory(directory), encoding="utf-8")
        print(f"wrote {target} - review and commit it")
    if args.fixtures:
        assert directory.captured_on is not None
        _write_fixtures(backend, directory.captured_on)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="rewrite the directory file")
    parser.add_argument("--fixtures", action="store_true", help="rewrite tests/fixtures/shenwan")
    parser.add_argument("--cross-check", action="store_true", help="compare with legulegu")
    parser.add_argument("--limit", type=int, default=None, help="cross-check only N indexes")
    parser.add_argument("--cache", default=None, help="cache dir for legulegu answers")
    parser.add_argument("--path", default=str(DEFAULT_SHENWAN_DIRECTORY_PATH))
    sys.exit(asyncio.run(main(parser.parse_args())))
