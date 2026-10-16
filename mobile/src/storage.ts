import { load } from "@tauri-apps/plugin-store";
import { deleteItem, getItem, setItem } from "tauri-plugin-secure-keystore";

export type Pairing = {
  gatewayUrl: string;
  backendId: string;
  deviceToken: string;
};

// 凭证经 tauri-plugin-secure-keystore 加密落盘（Android Keystore AES-256-GCM）。
// 旧版把明文 JSON 存在 plugin-store 里；升级后的首次读取做一次性迁移，迁完删明文键。
const LEGACY_STORE_PATH = "zcharlie-mobile.json";
const PAIRING_KEY = "gateway-pairing";
let legacyStorePromise: ReturnType<typeof load> | undefined;

async function getLegacyStore() {
  legacyStorePromise ??= load(LEGACY_STORE_PATH, { autoSave: false });
  return legacyStorePromise;
}

function parsePairing(raw: string): Pairing | null {
  try {
    const pairing = JSON.parse(raw) as Pairing;
    return pairing.gatewayUrl && pairing.backendId && pairing.deviceToken ? pairing : null;
  } catch {
    return null;
  }
}

async function purgeLegacyPairing(): Promise<void> {
  const store = await getLegacyStore();
  if ((await store.get(PAIRING_KEY)) === null) return;
  await store.delete(PAIRING_KEY);
  await store.save();
}

export async function readPairing(): Promise<Pairing | null> {
  const stored = await getItem(PAIRING_KEY).catch(() => null);
  if (stored !== null) return parsePairing(stored);
  // 一次性迁移：明文 Store 里还有旧凭证就搬进加密存储，再删明文。
  const store = await getLegacyStore();
  const pairing = (await store.get<Pairing>(PAIRING_KEY)) ?? null;
  if (!pairing) return null;
  await setItem(PAIRING_KEY, JSON.stringify(pairing));
  await store.delete(PAIRING_KEY);
  await store.save();
  return pairing;
}

export async function savePairing(pairing: Pairing): Promise<void> {
  await setItem(PAIRING_KEY, JSON.stringify(pairing));
  await purgeLegacyPairing();
}

export async function removePairing(): Promise<void> {
  await deleteItem(PAIRING_KEY).catch(() => undefined);
  await purgeLegacyPairing();
}
