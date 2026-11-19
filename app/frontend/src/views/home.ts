import { api } from "../api";
import { el } from "../charts";
import { createTerrain } from "../terrain";
import type { Status } from "../api";

export function home(host: HTMLElement, _st: () => Status | null) {
  host.innerHTML = `
  <section class="hero">
    <div class="eyebrow">Civil unrest · event extraction &amp; signal detection · #FarmersProtest 2020–21</div>
    <h1>Read the feed.<br><em>Find the unrest.</em></h1>
    <p class="lead">Models trained on a million protest tweets and cross-checked against real ACLED event records. Type a post, scan a feed, or fly through 386 days of the protest and see what the models picked up — and where they fail.</p>
  </section>
  <div class="stage" id="stage"><div class="hint">drag to orbit · scroll to zoom · white beams = ACLED surge days</div></div>
  <div class="grid g4" id="kpis" style="margin:18px 0"></div>
  <div class="grid g3" id="cards"></div>
  <div class="grid g2" style="margin-top:16px">
    <div class="card"><h3>What the three lanes show</h3>
      <p class="sub" style="margin:0"><b style="color:var(--teal)">Tweets</b> — daily #FarmersProtest volume. <b style="color:var(--red)">Model signal</b> — the average violence score the trained tweet model gives that day's tweets. <b style="color:var(--amber)">ACLED events</b> — farmer-related events logged by the independent ACLED dataset that day. Height is scaled per lane.</p></div>
    <div class="card"><h3>What to trust</h3>
      <p class="sub" style="margin:0">The tweet models were trained on <i>weak</i> labels (keyword rules) and checked on a 293-tweet sample labelled by one annotator. They separate violent from non-violent protest talk well, but they also fire on angry rhetoric. Forecasting beyond the same day shows no skill. Every page shows these limits next to the numbers.</p></div>
  </div>`;
  const cards = host.querySelector("#cards") as HTMLElement;
  [
    ["01", "Analyze a post", "#/analyze", "Paste any post. Get each model's verdict, the words that drove it, the protest actions and states it mentions."],
    ["02", "Scan a feed", "#/batch", "Paste or upload many posts (with dates). Get the share flagged, a score histogram and a day-by-day signal compared with the archive."],
    ["03", "Unrest terrain", "#/explore", "Click any day of the 3D terrain: the live-scored tweets, top posts, actions, states and what ACLED recorded."],
    ["04", "Threshold lab", "#/lab", "Drag the decision threshold and watch precision, recall and F1 move on the labelled gold sample."],
    ["05", "Evidence", "#/evidence", "Extraction vs ACLED, surge forecasting with confidence intervals, and the cross-country G-CUT results."],
  ].forEach(([n, t, h, d]) => { const a = el("a", "card", `<div class="n">${n}</div><h2>${t}</h2><p class="sub" style="margin:0">${d}</p>`); a.href = h; cards.appendChild(a); });

  let dispose = () => {};
  let dead = false;
  api.timeline().then((tl) => {
    if (dead) return;
    const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
    const k = host.querySelector("#kpis") as HTMLElement;
    [[sum(tl.tweets).toLocaleString(), "tweets in the archive", "var(--teal)"], [String(tl.dates.length), "days covered", "var(--text)"],
     [sum(tl.acled).toLocaleString(), "ACLED farmer events", "var(--amber)"], [String(tl.surge.filter(Boolean).length), "surge days", "var(--red)"]]
      .forEach(([v, l, c]) => k.appendChild(el("div", "card kpi", `<div class="big" style="color:${c}">${v}</div><div class="l">${l}</div>`)));
    const stage = host.querySelector("#stage") as HTMLElement;
    const t = createTerrain(stage, tl, { auto: true });
    if (!t.ok) stage.insertAdjacentHTML("afterbegin", `<div class="empty" style="margin:40px">WebGL is not available in this browser — the 3D view is disabled. The other pages work normally.</div>`);
    t.onPick((i) => { location.hash = "#/explore?d=" + tl.dates[i]; });
    dispose = t.dispose;
  }).catch((e: Error) => { (host.querySelector("#stage") as HTMLElement).insertAdjacentHTML("afterbegin", `<div class="empty" style="margin:40px">Backend not reachable: ${e.message}</div>`); });
  return () => { dead = true; dispose(); };
}
