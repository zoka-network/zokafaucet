import "./styles.css";
import { faucetApi } from "./api";
import { start, type Turnstile } from "./app";

start(document, {
  api: faucetApi(),
  turnstile: () => (window as unknown as { turnstile?: Turnstile }).turnstile,
  storage: window.localStorage,
  setTimer: (fn, ms) => window.setTimeout(fn, ms),
});
