// 候选页入口。列表和详情分在独立模块，这里只启动并再导出原来的名字。

import { section } from "./candidate_shared.js";
import {
  loadCandidateList,
  renderCandidateList,
  wireDismissSheet,
  wireNominateForm,
} from "./candidate_list.js";
import { loadCandidateDetail, wireReasonSheet, wireTabs } from "./candidate_detail.js";

export const CANDIDATE_REGIONS = new Set(["candidates-list", "candidate-detail"]);
export { candidatesBucket, normalizeTicker } from "./candidate_list.js";
export { segmentRows } from "./candidate_detail.js";

// ---- 启动 -------------------------------------------------------------

function wireCalibrationBanner() {
  const banner = section("candidates-calibration");
  if (!banner) return;
  const KEY = "zc-cand-calibration-dismissed";
  try {
    if (globalThis.sessionStorage.getItem(KEY) === "1") banner.hidden = true;
  } catch {
    // 存储不可用时照常显示。
  }
  const close = banner.querySelector("[data-dismiss-callout]");
  if (close) {
    close.addEventListener("click", () => {
      banner.hidden = true;
      try {
        globalThis.sessionStorage.setItem(KEY, "1");
      } catch {
        // 只影响下次是否再显示。
      }
    });
  }
}

export function bootCandidates() {
  const list = section("candidates-list");
  if (list) {
    loadCandidateList(list);
    const statusFilter = section("candidates-status-filter");
    if (statusFilter) {
      statusFilter.addEventListener("change", () => loadCandidateList(list));
      for (const button of document.querySelectorAll("[data-status]")) {
        button.addEventListener("click", () => {
          if (statusFilter.value === button.dataset.status) return;
          statusFilter.value = button.dataset.status;
          statusFilter.dispatchEvent(new Event("change"));
        });
      }
    }
    const templateFilter = section("candidates-template-filter");
    if (templateFilter) templateFilter.addEventListener("change", () => renderCandidateList(list));
    const sortSelect = section("candidates-sort");
    if (sortSelect) sortSelect.addEventListener("change", () => renderCandidateList(list));
    wireDismissSheet(list);
    wireCalibrationBanner();
  }
  const nominateForm = section("candidate-nominate-form");
  if (nominateForm) wireNominateForm(nominateForm, list);
  if (section("candidate-detail")) {
    wireTabs();
    wireReasonSheet();
    loadCandidateDetail();
  }
}
