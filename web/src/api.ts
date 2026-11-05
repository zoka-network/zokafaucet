// The faucet's API, behind the tunnel of api.zokascan.cc (server/faucet.py).

export const API = "https://api.zokascan.cc/faucet";
export const SITE_KEY = "0x4AAAAAAF1SHgqAf0NwUhnv";
export const EXPLORER = "https://zokascan.cc";

export interface Status {
  amount: string;
  daily_cap: string;
  left_24h: string;
  left_24h_atoms: number;
  queue: number;
  sent_total: number;
  ready: boolean;
  empty: boolean;
}

export interface Claim {
  id: string;
  status: "queued" | "sending" | "sent" | "failed";
  position: number;
  amount: string;
  txid: string | null;
  error: string | null;
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly field?: "address" | "token",
  ) {
    super(message);
  }
}

type Fetch = typeof fetch;

async function call<T>(fetchFn: Fetch, path: string, init: RequestInit = {}): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 20_000);
  let response: Response;
  try {
    response = await fetchFn(`${API}${path}`, {
      ...init,
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      signal: controller.signal,
    });
  } catch {
    throw new ApiError("The faucet cannot be reached right now. Try again in a few minutes.", 0);
  } finally {
    clearTimeout(timer);
  }
  let body: Record<string, unknown> = {};
  try {
    body = (await response.json()) as Record<string, unknown>;
  } catch {
    // An empty or non-JSON reply: the status says enough.
  }
  if (!response.ok) {
    const field = body.field === "address" || body.field === "token" ? body.field : undefined;
    const message = typeof body.error === "string" ? body.error : "The faucet could not answer. Try again in a few minutes.";
    throw new ApiError(message, response.status, field);
  }
  return body as T;
}

export function faucetApi(fetchFn: Fetch = (...a) => fetch(...a)) {
  return {
    status: () => call<Status>(fetchFn, "/status"),
    claim: (address: string, token: string) =>
      call<Claim>(fetchFn, "/claim", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ address, token }),
      }),
    get: (id: string) => call<Claim>(fetchFn, `/claims/${encodeURIComponent(id)}`),
  };
}

export type FaucetApi = ReturnType<typeof faucetApi>;

/** A short address from what was pasted: also a zoka: payment link. */
export function normalizeAddress(text: string): string {
  let value = text.trim();
  if (value.toLowerCase().startsWith("zoka:")) value = value.slice(5);
  const query = value.indexOf("?");
  if (query >= 0) value = value.slice(0, query);
  return value.replace(/\s+/g, "");
}

export const SHORT_ADDRESS = /^zqtest2[1-9A-HJ-NP-Za-km-z]{30,80}$/;
