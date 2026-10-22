// Stable v1 contract. Old APKs have no discovery command: try only the known
// legacy commands, disabling each missing command once per document.
const LEGACY = new Set(["proxy_request", "stream_task_events", "move_task_to_back"]);
const unavailable = new Set();
let discovery;

export function missingNativeCommand(error) {
  return /(?:command\s+.*(?:not found|unknown)|unknown\s+command|command not found)/i.test(String(error));
}

export function disableNativeCommand(command) { unavailable.add(command); }

export function nativeCapabilities() {
  if (!discovery) discovery = (async () => {
    const invoke = globalThis.__TAURI_INTERNALS__?.invoke;
    if (!invoke) return { bridge_version: 0, capabilities: [], legacy: false };
    let timer;
    try {
      const result = await Promise.race([
        invoke("bridge_capabilities"),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("bridge_timeout")), 1500); }),
      ]);
      if (!result || !Number.isInteger(result.bridge_version) || !Array.isArray(result.capabilities)) {
        return { bridge_version: 0, capabilities: [], legacy: false };
      }
      return { ...result, legacy: false };
    } catch (error) {
      return { bridge_version: 0, capabilities: [], legacy: missingNativeCommand(error) };
    } finally { clearTimeout(timer); }
  })();
  return discovery;
}

export async function supportsNative(command) {
  if (unavailable.has(command)) return false;
  const info = await nativeCapabilities();
  return info.legacy ? LEGACY.has(command) : info.bridge_version === 1 && info.capabilities.includes(command);
}
