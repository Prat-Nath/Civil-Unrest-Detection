import { el } from "./charts";
import type { Status } from "./api";

export function busy(btn: HTMLButtonElement, on: boolean, label?: string) {
  btn.disabled = on;
  if (on) { btn.dataset.l = btn.innerHTML; btn.innerHTML = `<span class="spin"></span>${label ?? "Working…"}`; }
  else if (btn.dataset.l) btn.innerHTML = btn.dataset.l;
}
export function errBox(msg: string): HTMLElement {
  const e = el("div", "err");
  const soon = /loading/i.test(msg);
  e.textContent = (soon ? "⏳ " : "⚠ ") + msg;
  return e;
}
export function modelBanner(st: Status | null): HTMLElement | null {
  if (!st) return null;
  if (st.models_loading) return el("div", "note", "<b>Models are loading.</b> The first start takes a minute or two (it retrains the TF-IDF model once, then caches it). Buttons will work as soon as the status pill turns green.");
  if (!st.models.length) return el("div", "note", "<b>No model found.</b> Run the training pipeline first (see README), then restart the server. " + st.errors.join(" "));
  return null;
}
export const SIGNAL_TEXT: Record<string, string> = {
  violent: "Flagged as violent content by the primary model.",
  protest_topic: "Not flagged as violent, but it mentions protest actions or places — peaceful or contested protest talk.",
  none: "No violence flag and no protest action detected.",
};
export const ACTION_LABEL: Record<string, string> = {
  bharat_bandh: "Bharat Bandh", chakka_jaam: "Chakka Jaam", rail_roko: "Rail Roko", tractor_rally: "Tractor rally", mahapanchayat: "Mahapanchayat",
  road_blockade: "Road blockade", police_force: "Police force", clash: "Clash", hunger_strike_dharna: "Hunger strike / dharna",
};
