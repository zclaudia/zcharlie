import { load } from "@tauri-apps/plugin-store";

export type Pairing = {
  gatewayUrl: string;
  backendId: string;
  deviceToken: string;
};

const STORE_PATH = "zcharlie-mobile.json";
const PAIRING_KEY = "gateway-pairing";
let storePromise: ReturnType<typeof load> | undefined;

async function getStore() {
  storePromise ??= load(STORE_PATH, { autoSave: false });
  return storePromise;
}

export async function readPairing(): Promise<Pairing | null> {
  const store = await getStore();
  return (await store.get<Pairing>(PAIRING_KEY)) ?? null;
}

export async function savePairing(pairing: Pairing): Promise<void> {
  const store = await getStore();
  await store.set(PAIRING_KEY, pairing);
  await store.save();
}

export async function removePairing(): Promise<void> {
  const store = await getStore();
  await store.delete(PAIRING_KEY);
  await store.save();
}
