// 页面数据装配：按 data-region 声明加载数据并渲染。
// 页面只消费版本化 API 结构，不在前端解释 score 语义或复算分数。
// 今日概览、运行中心、单股详情分在 overview.js / runs_page.js / stock_page.js。
// 证据导航留在 stock_page.js：它和单股页头共用同一批渲染函数。

import { loadRegion } from "./api.js";
import { CANDIDATE_REGIONS, bootCandidates } from "./candidates.js";
import { TEMPLATE_REGIONS, bootTemplates } from "./templates.js";
import { HEALTH_REGIONS, bootHealth } from "./health.js";
import { bootAgentControls } from "./agent_controls.js";
import { bootAgentViews } from "./agent_views.js";
import { SETTINGS_REGIONS, bootSettings } from "./settings.js";
import { table } from "./dom.js";
import { DATA_SOURCE_REGIONS, bootDataSources } from "./data-sources.js";
import { INDUSTRY_GROUP_REGIONS, bootIndustryGroups } from "./industry-groups.js";
import { bootAgent } from "./agent.js";
import { bootAgentPortfolio } from "./agent_portfolio.js";
import { bootFinancials } from "./financials.js";
import { WATCHLIST_REGIONS, bootWatchlist } from "./watchlist.js";
import { PORTFOLIO_REGIONS, bootPortfolio } from "./portfolio.js";
import { bootAccountFit } from "./account_fit.js";
import { bootCandidateRanking } from "./candidate_ranking.js";
import { bootSupplementalRules } from "./supplemental_rules.js";
import { bootSupplementalItems } from "./supplemental_items.js";
import { loadDashboard } from "./overview.js";
import { loadRuns, loadRunsPage, setRunsFilter, wireRunsPage } from "./runs_page.js";
import {
  loadStockChanges,
  loadStockDetail,
  loadStockHistory,
  loadStockSpark,
  wireStockPage,
  wireStockRunForm,
} from "./stock_page.js";

// ---- 启动 -------------------------------------------------------------

function refreshOverview() {
  const region = document.querySelector("#dashboard-today");
  if (region) loadRegion(region, { render: loadDashboard });
}

function boot() {
  for (const region of document.querySelectorAll(".data-region[data-endpoint]")) {
    const id = region.id;
    if (CANDIDATE_REGIONS.has(id) || TEMPLATE_REGIONS.has(id) || HEALTH_REGIONS.has(id) || SETTINGS_REGIONS.has(id)) continue;
    if (DATA_SOURCE_REGIONS.has(id)) continue;
    if (INDUSTRY_GROUP_REGIONS.has(id)) continue;
    if (WATCHLIST_REGIONS.has(id)) continue;
    if (PORTFOLIO_REGIONS.has(id)) continue;
    if (id === "dashboard-today") loadRegion(region, { render: loadDashboard });
    else if (id === "runs-list") {
      const wanted = new URLSearchParams(globalThis.location.search).get("status") || "";
      const known = ["queued", "running", "success", "partial", "failed"].includes(wanted) ? wanted : "";
      setRunsFilter(known, { reload: false });
      loadRunsPage(region, 0, { status: known });
    }
    else if (id === "stock-history")
      loadRegion(region, { render: (node, data) => loadStockHistory(node, data).then((latest) => {
        const detailRegion = document.querySelector("#stock-detail");
        if (detailRegion) loadStockDetail(detailRegion, latest);
      }) });
    else loadRegion(region);
  }

  const stockRunForm = document.querySelector("#stock-run-form");
  if (stockRunForm) wireStockRunForm(stockRunForm);

  wireRunsPage();
  document.querySelector("#overview-refresh")?.addEventListener("click", refreshOverview);
  document.querySelector("#overview-refresh-m")?.addEventListener("click", refreshOverview);
  wireStockPage();
  loadStockSpark();

  bootWatchlist();
  bootPortfolio();
  bootAgentControls();
  bootAgentViews();
  bootAgentPortfolio();
  bootAgent();
  bootCandidates();
  bootAccountFit();
  bootCandidateRanking();
  bootTemplates();
  bootHealth();
  bootSettings();
  bootDataSources();
  bootIndustryGroups();
  bootFinancials();
  bootSupplementalRules();
  bootSupplementalItems();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}

export { loadDashboard, loadRuns, loadStockChanges, loadStockDetail, loadStockHistory, table };
