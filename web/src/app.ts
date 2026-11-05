// The page: the faucet's figures, the form with the Turnstile check, and the
// request followed until its payment leaves.
import { ApiError, EXPLORER, SHORT_ADDRESS, SITE_KEY, normalizeAddress, type Claim, type FaucetApi, type Status } from "./api";

export interface Turnstile {
  render(element: HTMLElement, options: Record<string, unknown>): string;
  reset(widget?: string): void;
}

export interface Deps {
  api: FaucetApi;
  turnstile: () => Turnstile | undefined;
  storage: Pick<Storage, "getItem" | "setItem" | "removeItem">;
  setTimer: (fn: () => void, ms: number) => unknown;
}

const SAVED = "zoka-faucet-claim";
const POLL_MS = 8_000;

function $(root: ParentNode, selector: string): HTMLElement {
  const found = root.querySelector<HTMLElement>(selector);
  if (!found) throw new Error(`missing ${selector}`);
  return found;
}

function show(element: HTMLElement, text: string | null): void {
  element.textContent = text ?? "";
  element.hidden = !text;
}

export function start(root: Document, deps: Deps): { ready: Promise<void> } {
  const form = $(root, "[data-form]") as HTMLFormElement;
  const input = $(root, "#address") as HTMLInputElement;
  const submit = $(root, "[data-submit]") as HTMLButtonElement;
  const addressError = $(root, "[data-address-error]");
  const tokenError = $(root, "[data-token-error]");
  const formError = $(root, "[data-form-error]");
  const result = $(root, "[data-result]");
  const live = $(root, "[data-live]");
  const dot = $(root, "[data-dot]");
  let token = "";
  let widget: string | undefined;
  let busy = false;
  let status: Status | null = null;

  const refreshButton = () => {
    submit.disabled = busy || !token || !input.value.trim() || status?.empty === true;
  };

  const paintStatus = (s: Status) => {
    status = s;
    root.querySelectorAll("[data-amount]").forEach((el) => (el.textContent = s.amount));
    root.querySelectorAll("[data-cap]").forEach((el) => (el.textContent = s.daily_cap));
    $(root, "[data-left]").textContent = `${s.left_24h} ZKA`;
    $(root, "[data-queue]").textContent = String(s.queue);
    $(root, "[data-sent]").textContent = s.sent_total.toLocaleString("en");
    if (s.empty) {
      live.textContent = "Out of ZKA for now";
      dot.className = "dot off";
    } else if (s.left_24h_atoms <= 0) {
      live.textContent = "Done for today";
      dot.className = "dot off";
    } else {
      live.textContent = s.ready ? "Ready" : "Reading the chain";
      dot.className = s.ready ? "dot" : "dot busy";
    }
    refreshButton();
  };

  const loadStatus = () =>
    deps.api.status().then(paintStatus, () => {
      live.textContent = "Cannot reach the faucet";
      dot.className = "dot off";
    });

  const paintClaim = (claim: Claim) => {
    result.hidden = false;
    result.className = `result ${claim.status}`;
    result.replaceChildren();
    const title = root.createElement("p");
    title.className = "result-title";
    const detail = root.createElement("p");
    detail.className = "hint";
    if (claim.status === "queued") {
      title.textContent = claim.position > 1 ? `You are #${claim.position} in line` : "You are next";
      detail.textContent = "Payments leave one per block, about 2 minutes each. You can close this page: the ZKA still arrive.";
    } else if (claim.status === "sending") {
      title.textContent = `Proving and sending your ${claim.amount} ZKA…`;
      detail.textContent = "A STARK proof is being made for your payment.";
    } else if (claim.status === "sent") {
      title.textContent = `${claim.amount} ZKA are on their way`;
      detail.textContent = "They show in your wallet once the next block is mined, usually within a few minutes.";
    } else {
      title.textContent = "The payment was not sent";
      detail.textContent = claim.error ?? "Try again tomorrow.";
    }
    result.append(title, detail);
    if (claim.txid) {
      const id = root.createElement("p");
      id.className = "mono";
      id.textContent = claim.txid;
      const link = root.createElement("a");
      link.className = "link";
      link.href = `${EXPLORER}/search/${claim.txid}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "View on ZokaScan ↗";
      result.append(id, link);
    }
  };

  const follow = (id: string) => {
    deps.api.get(id).then(
      (claim) => {
        paintClaim(claim);
        if (claim.status === "queued" || claim.status === "sending") {
          deps.setTimer(() => follow(id), POLL_MS);
        } else {
          deps.storage.removeItem(SAVED);
          void loadStatus();
        }
      },
      (error: ApiError) => {
        if (error.status === 404) deps.storage.removeItem(SAVED);
        else deps.setTimer(() => follow(id), POLL_MS * 2);
      },
    );
  };

  const renderCheck = (tries = 0) => {
    const ts = deps.turnstile();
    if (!ts) {
      if (tries < 150) deps.setTimer(() => renderCheck(tries + 1), 100);
      else show(tokenError, "The check could not load. Allow challenges.cloudflare.com and reload the page.");
      return;
    }
    widget = ts.render($(root, "[data-turnstile]"), {
      sitekey: SITE_KEY,
      action: "claim",
      theme: "dark",
      callback: (value: string) => {
        token = value;
        show(tokenError, null);
        refreshButton();
      },
      "expired-callback": () => {
        token = "";
        refreshButton();
      },
      "error-callback": () => {
        token = "";
        show(tokenError, "The check could not finish. Reload the page and try again.");
        refreshButton();
      },
    });
  };

  input.addEventListener("input", () => {
    show(addressError, null);
    show(formError, null);
    refreshButton();
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const address = normalizeAddress(input.value);
    input.value = address;
    show(addressError, null);
    show(tokenError, null);
    show(formError, null);
    if (!SHORT_ADDRESS.test(address)) {
      show(addressError, "Paste a Zoka Quantic Testnet short address: it starts with zqtest2.");
      return;
    }
    busy = true;
    refreshButton();
    deps.api.claim(address, token).then(
      (claim) => {
        deps.storage.setItem(SAVED, claim.id);
        paintClaim(claim);
        deps.setTimer(() => follow(claim.id), POLL_MS);
        void loadStatus();
      },
      (error: ApiError) => {
        const target = error.field === "address" ? addressError : error.field === "token" ? tokenError : formError;
        show(target, error.message);
      },
    ).finally(() => {
      busy = false;
      token = "";
      deps.turnstile()?.reset(widget);
      refreshButton();
    });
  });

  const saved = deps.storage.getItem(SAVED);
  if (saved && /^[0-9a-f]{32}$/.test(saved)) follow(saved);
  renderCheck();
  return { ready: loadStatus() };
}
