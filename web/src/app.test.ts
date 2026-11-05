import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ApiError, faucetApi, normalizeAddress, type Claim, type FaucetApi, type Status } from "./api";
import { start, type Turnstile } from "./app";

const ALICE = "zqtest2YVVQP8Qx2W3oSUNvVhJGz6Y1occM4Tzh14mkrYQC3u1GjorYg";
const html = readFileSync(join(process.cwd(), "index.html"), "utf8");

const status: Status = { amount: "10", daily_cap: "400", left_24h: "390", left_24h_atoms: 390_000_000, queue: 1, sent_total: 12, ready: true, empty: false };

function page() {
  document.documentElement.innerHTML = html.replace(/<script[\s\S]*?<\/script>/g, "");
  const timers: (() => void)[] = [];
  const saved = new Map<string, string>();
  let callback: ((token: string) => void) | undefined;
  const turnstile: Turnstile = {
    render: (_el, options) => {
      callback = options.callback as (token: string) => void;
      return "w1";
    },
    reset: vi.fn(),
  };
  const api = {
    status: vi.fn(async () => status),
    claim: vi.fn(async (): Promise<Claim> => ({ id: "a".repeat(32), status: "queued", position: 2, amount: "10", txid: null, error: null })),
    get: vi.fn(async (): Promise<Claim> => ({ id: "a".repeat(32), status: "sent", position: 0, amount: "10", txid: "cd".repeat(32), error: null })),
  } satisfies FaucetApi;
  const run = start(document, {
    api,
    turnstile: () => turnstile,
    storage: { getItem: (k) => saved.get(k) ?? null, setItem: (k, v) => void saved.set(k, v), removeItem: (k) => void saved.delete(k) },
    setTimer: (fn) => timers.push(fn),
  });
  const q = (s: string) => document.querySelector<HTMLElement>(s)!;
  return { api, turnstile, timers, saved, run, q, pass: (t = "token") => callback?.(t) };
}

const tick = () => new Promise((r) => setTimeout(r, 0));

describe("the faucet page", () => {
  it("shows the faucet's figures", async () => {
    const { run, q } = page();
    await run.ready;
    expect(q("[data-left]").textContent).toBe("390 ZKA");
    expect(q("[data-queue]").textContent).toBe("1");
    expect(q("[data-sent]").textContent).toBe("12");
    expect(q("[data-live]").textContent).toBe("Ready");
  });

  it("asks for the check and a short address before sending", async () => {
    const { run, q, pass, api } = page();
    await run.ready;
    const input = q("#address") as HTMLInputElement;
    const submit = q("[data-submit]") as HTMLButtonElement;
    input.value = "zqtest2nope";
    input.dispatchEvent(new Event("input"));
    expect(submit.disabled).toBe(true);
    pass();
    expect(submit.disabled).toBe(false);
    q("[data-form]").dispatchEvent(new Event("submit", { cancelable: true }));
    expect(q("[data-address-error]").textContent).toContain("starts with zqtest2");
    expect(api.claim).not.toHaveBeenCalled();
  });

  it("sends a claim, follows it and shows the payment", async () => {
    const { run, q, pass, api, timers, saved, turnstile } = page();
    await run.ready;
    const input = q("#address") as HTMLInputElement;
    input.value = `zoka:${ALICE}?amount=5`;
    input.dispatchEvent(new Event("input"));
    pass("tok");
    q("[data-form]").dispatchEvent(new Event("submit", { cancelable: true }));
    await tick();
    expect(api.claim).toHaveBeenCalledWith(ALICE, "tok");
    expect(q("[data-result]").textContent).toContain("You are #2 in line");
    expect(saved.get("zoka-faucet-claim")).toBe("a".repeat(32));
    expect(turnstile.reset).toHaveBeenCalled();
    timers.shift()!();
    await tick();
    expect(q("[data-result]").textContent).toContain("10 ZKA are on their way");
    const link = q("[data-result] a") as HTMLAnchorElement;
    expect(link.href).toBe(`https://zokascan.cc/search/${"cd".repeat(32)}`);
    expect(saved.has("zoka-faucet-claim")).toBe(false);
  });

  it("puts the faucet's refusal under the field it is about", async () => {
    const { run, q, pass, api } = page();
    await run.ready;
    api.claim.mockRejectedValueOnce(new ApiError("This wallet already got its ZKA today. Come back in 5 hours.", 429, "address"));
    (q("#address") as HTMLInputElement).value = ALICE;
    pass();
    q("[data-form]").dispatchEvent(new Event("submit", { cancelable: true }));
    await tick();
    await tick();
    expect(q("[data-address-error]").textContent).toContain("Come back in 5 hours");
    expect(q("[data-address-error]").hidden).toBe(false);
  });

  it("links the same networks as zoka.network", () => {
    const { q } = page();
    expect(q('a[aria-label="GitHub"]').getAttribute("href")).toBe("https://github.com/zoka-network");
    expect(q('a[aria-label="X / Twitter"]').getAttribute("href")).toBe("https://x.com/zokanetwork");
    expect(q('a[aria-label="Discord"]').getAttribute("href")).toBe("https://discord.gg/uZRPcNb3m7");
  });
});

describe("the API client", () => {
  it("reads payment links and errors", async () => {
    expect(normalizeAddress(` zoka:${ALICE}?amount=1 `)).toBe(ALICE);
    const fake = vi.fn(async () => new Response(JSON.stringify({ error: "Complete the check first.", field: "token" }), { status: 400 }));
    await expect(faucetApi(fake as unknown as typeof fetch).claim(ALICE, "")).rejects.toMatchObject({ status: 400, field: "token" });
    const [url, init] = fake.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://api.zokascan.cc/faucet/claim");
    expect(init.credentials).toBe("omit");
    const down = vi.fn(async () => {
      throw new TypeError("offline");
    });
    await expect(faucetApi(down as unknown as typeof fetch).status()).rejects.toMatchObject({ status: 0 });
  });
});
