import "./styles.css";
import { api, Status } from "./api";
import { el } from "./charts";
import { startBackground } from "./terrain";
import { home } from "./views/home";
import { analyzer } from "./views/analyzer";
import { batch } from "./views/batch";
import { explorer } from "./views/explorer";
import { lab } from "./views/lab";
import { evidence } from "./views/evidence";

type View = (host: HTMLElement, st: () => Status | null) => void | (() => void);
const ROUTES: { path: string; label: string; view: View }[] = [
  { path: "/", label: "Overview", view: home },
  { path: "/analyze", label: "Analyze a post", view: analyzer },
  { path: "/batch", label: "Scan a feed", view: batch },
  { path: "/explore", label: "Unrest terrain", view: explorer },
  { path: "/lab", label: "Threshold lab", view: lab },
  { path: "/evidence", label: "Evidence", view: evidence },
];

let status: Status | null = null;
let cleanup: (() => void) | void;
const nav = document.getElementById("nav") as HTMLElement;
const host = document.getElementById("view") as HTMLElement;
ROUTES.forEach((r) => { const a = el("a", "", r.label); a.href = "#" + r.path; a.dataset.p = r.path; nav.appendChild(a); });

function render() {
  const p = (location.hash.replace(/^#/, "").split("?")[0]) || "/";
  const r = ROUTES.find((x) => x.path === p) ?? ROUTES[0];
  nav.querySelectorAll("a").forEach((a) => a.classList.toggle("on", (a as HTMLElement).dataset.p === r.path));
  if (cleanup) cleanup();
  host.innerHTML = "";
  const box = el("div", "fade"); host.appendChild(box);
  cleanup = r.view(box, () => status);
  window.scrollTo(0, 0);
}
addEventListener("hashchange", render);

const sEl = document.getElementById("status") as HTMLElement;
async function poll() {
  try {
    status = await api.status();
    const loading = status.models_loading;
    const names = status.models.map((m) => m.name).join(", ") || "none";
    sEl.innerHTML = loading
      ? `<span class="pill warn">loading models…</span>`
      : `<span class="pill ${status.models.length ? "ok" : "bad"}" title="${names}">${status.models.length} models · ${status.device}</span>` +
        `<span class="pill ${status.archive_loaded ? "ok" : "warn"}">${status.archive_loaded ? "archive ready" : "archive loading…"}</span>`;
    setTimeout(poll, loading || !status.archive_loaded ? 2000 : 15000);
  } catch {
    sEl.innerHTML = `<span class="pill bad">backend offline</span>`;
    setTimeout(poll, 4000);
  }
}
poll();
startBackground(document.getElementById("bg") as HTMLCanvasElement);
render();
