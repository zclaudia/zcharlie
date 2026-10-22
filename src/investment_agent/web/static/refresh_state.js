// Shared by the API client and pages; no DOM side effects on import.
const guards = new Map();

export function holdRefresh(reason) {
  const token = {};
  guards.set(token, () => reason);
  return () => guards.delete(token);
}

// Return an explanation while unsafe, or an empty string when ready.
export function registerRefreshGuard(key, guard) {
  guards.set(key, guard);
  return () => guards.delete(key);
}

export function refreshBlockers() {
  const reasons = [];
  for (const guard of guards.values()) {
    try { const reason = guard(); if (reason) reasons.push(reason); }
    catch { reasons.push("页面状态尚未确认"); }
  }
  return [...new Set(reasons)];
}
