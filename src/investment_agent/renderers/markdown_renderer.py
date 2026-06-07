"""Markdown renderer for Discovery + Attribution reports."""

from datetime import date

from investment_agent.domain.protocols import RenderedContent


class MarkdownRenderer:
    async def render_discovery(
        self,
        run_id: int,
        consensus_picks: list[dict[str, object]],
        divergent_picks: list[dict[str, object]],
        overlooked_picks: list[dict[str, object]],
        scores_by_listing: dict[int, list[dict[str, object]]],
    ) -> RenderedContent:
        """Render the Discovery report. Pick entries are expected to be pre-enriched with `ticker`.

        scores_by_listing values must be score dicts with keys: strategy_code, score, confidence,
        bull_thesis, bear_thesis, ticker (optional, for header).
        """
        lines: list[str] = [
            f"# Discovery Report — Run {run_id}",
            "",
            f"_Generated: {date.today().isoformat()}_",
            "",
            "## 共识股 (Consensus)",
            "",
        ]
        if consensus_picks:
            lines.append("| Ticker | Avg Score | Supporting Strategies |")
            lines.append("|---|---|---|")
            for p in consensus_picks:
                ticker = p.get("ticker") or p.get("listing_id")
                avg = p.get("avg_score", "-")
                supporting = p.get("supporting", [])
                support_str = (
                    ", ".join(str(x) for x in supporting)
                    if isinstance(supporting, list)
                    else str(supporting)
                )
                lines.append(f"| {ticker} | {avg} | {support_str} |")
        else:
            lines.append("_No consensus picks today._")
        lines.append("")

        lines.extend(["## 强分歧股 (Divergent)", ""])
        if divergent_picks:
            lines.append("| Ticker | Spread | Bullish | Bearish |")
            lines.append("|---|---|---|---|")
            for p in divergent_picks:
                ticker = p.get("ticker") or p.get("listing_id")
                spread = p.get("spread", "-")
                bulls = p.get("bull", [])
                bears = p.get("bear", [])
                bull_str = (
                    ", ".join(str(x) for x in bulls)
                    if isinstance(bulls, list)
                    else str(bulls)
                )
                bear_str = (
                    ", ".join(str(x) for x in bears)
                    if isinstance(bears, list)
                    else str(bears)
                )
                lines.append(f"| {ticker} | {spread} | {bull_str} | {bear_str} |")
        else:
            lines.append("_No divergent picks today._")
        lines.append("")

        lines.extend(["## 被忽略的优质股 (Overlooked)", ""])
        if overlooked_picks:
            lines.append("| Ticker | Champion Strategy | Champion Score |")
            lines.append("|---|---|---|")
            for p in overlooked_picks:
                ticker = p.get("ticker") or p.get("listing_id")
                champ = p.get("champion", "-")
                cscore = p.get("champion_score", "-")
                lines.append(f"| {ticker} | {champ} | {cscore} |")
        else:
            lines.append("_No overlooked picks today._")
        lines.append("")

        lines.extend(["---", "", "## 详细评分（按策略）", ""])
        for lid, score_dicts in scores_by_listing.items():
            header_ticker = next(
                (sd.get("ticker") for sd in score_dicts if sd.get("ticker")), None
            )
            header = f"### {header_ticker or lid}"
            lines.append(header)
            for sd in score_dicts:
                conf = sd.get("confidence", 0.0)
                conf_f = float(conf) if not isinstance(conf, float) else conf  # type: ignore[arg-type]
                lines.append(
                    f"- **{sd.get('strategy_code')}**: {sd.get('score')}/100 "
                    f"(conf {conf_f:.2f})"
                )
                bull = str(sd.get("bull_thesis") or "")[:200]
                bear = str(sd.get("bear_thesis") or "")[:200]
                if bull:
                    lines.append(f"  - Bull: {bull}")
                if bear:
                    lines.append(f"  - Bear: {bear}")
            lines.append("")

        body = "\n".join(lines).rstrip() + "\n"
        return RenderedContent(
            format="markdown",
            body=body,
            metadata={"run_id": run_id},
        )

    async def render_attribution(
        self, run_id: int, payload: dict[str, object]
    ) -> RenderedContent:
        """Render an attribution report. Payload keys:
          - ticker, company_name, as_of (str)
          - beta (dict of BetaDecomposition)
          - result (dict of AttributionResult)
        """
        ticker = payload.get("ticker", "?")
        company = payload.get("company_name", "")
        as_of = payload.get("as_of", "")
        raw_beta = payload.get("beta")
        raw_result = payload.get("result")
        beta: dict[str, object] = raw_beta if isinstance(raw_beta, dict) else {}
        result: dict[str, object] = raw_result if isinstance(raw_result, dict) else {}

        def _pct(v: object) -> str:
            try:
                return f"{float(v) * 100:.2f}%"  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return "-"

        lines: list[str] = [
            f"# Attribution: {ticker} — {as_of}",
            "",
            f"_Run {run_id} · {company}_",
            "",
            "## TL;DR",
            "",
            str(result.get("summary", "_no summary_")),
            "",
            f"_Confidence: {result.get('confidence', '-')}_",
            "",
            "## β Decomposition",
            "",
            f"- Stock return: **{_pct(beta.get('stock_return_pct'))}**",
            f"- Market return: {_pct(beta.get('market_return_pct'))} "
            f"(β_market = {beta.get('beta_market', '-')})",
        ]
        if beta.get("beta_sector") is not None:
            lines.append(
                f"- Sector return: {_pct(beta.get('sector_return_pct'))} "
                f"(β_sector = {beta.get('beta_sector', '-')})"
            )
        lines.append(
            f"- Explained by market: {_pct(beta.get('explained_by_market_pct'))}"
        )
        if beta.get("explained_by_sector_pct") is not None:
            lines.append(
                f"- Explained by sector: {_pct(beta.get('explained_by_sector_pct'))}"
            )
        lines.extend(
            [
                f"- **Idiosyncratic: {_pct(beta.get('idiosyncratic_pct'))}**",
                f"- R² = {beta.get('r_squared', '-')} · "
                f"window = {beta.get('window_days', '-')} days",
                "",
            ]
        )

        events = result.get("triggering_events") or []
        if isinstance(events, list) and events:
            lines.extend(["## Triggering Events", ""])
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                line = (
                    f"- **[{ev.get('event_type', 'event')}]** "
                    f"{ev.get('headline', '')}"
                )
                if ev.get("source"):
                    line += f"  ({ev['source']})"
                lines.append(line)
                if ev.get("impact_estimate"):
                    lines.append(f"  - Impact: {ev['impact_estimate']}")
                if ev.get("url"):
                    lines.append(f"  - {ev['url']}")
            lines.append("")
        else:
            lines.extend(
                [
                    "## Triggering Events",
                    "",
                    "_No specific triggering events identified._",
                    "",
                ]
            )

        if result.get("valuation_delta"):
            lines.extend(
                [
                    "## Valuation Delta",
                    "",
                    str(result["valuation_delta"]),
                    "",
                ]
            )
        if result.get("cross_market_context"):
            lines.extend(
                [
                    "## Cross-Market Context",
                    "",
                    str(result["cross_market_context"]),
                    "",
                ]
            )

        body = "\n".join(lines).rstrip() + "\n"
        return RenderedContent(
            format="markdown", body=body, metadata={"run_id": run_id}
        )
