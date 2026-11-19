import { api, DayInfo, Timeline, Status } from "../api";
import { C, el, esc, f3, histChart, hideTip, lineChart, pairBars, pct, showTip } from "../charts";
import { ACTION_LABEL, errBox } from "../ui";
import { createTerrain } from "../terrain";

export function explorer(host: HTMLElement, _st: () => Status | null) {
  host.innerHTML = `<h2>Unrest terrain</h2><p class="lead">Every day of the #FarmersProtest archive as a 3D landscape. Click a column (or any chart) to open that day: the tweets are scored live by the trained model, and ACLED's own record of the day sits beside it.</p>
  <div class="stage" id="stage"><div class="hint">drag · scroll · click a column to inspect the day</div></div>
  <div class="row" style="margin:12px 0" id="surge"><span class="sub">Jump to a surge day:</span></div>
  <div class="grid g3" id="mini"></div><div id="day" style="margin-top:18px"></div>`;
  let dispose = () => {}, dead = false;
  const dayBox = host.querySelector("#day") as HTMLElement;
  api.timeline().then((tl) => {
    if (dead) return;
    const stage = host.querySelector("#stage") as HTMLElement;
    const t = createTerrain(stage, tl); dispose = t.dispose;
    if (!t.ok) stage.insertAdjacentHTML("afterbegin", `<div class="empty" style="margin:40px">WebGL unavailable — use the charts below instead.</div>`);
    const mini = host.querySelector("#mini") as HTMLElement;
    let sel = -1; const redraw: (() => void)[] = [];
    const pick = (i: number) => { sel = i; t.select(i); redraw.forEach((f) => f()); loadDay(tl, i); };
    const mk = (title: string, color: string, y: (number | null)[], yMax?: number) => {
      const c = el("div", "card", `<h3>${title}</h3><canvas class="chart"></canvas>`); mini.appendChild(c);
      const cv = c.querySelector("canvas") as HTMLCanvasElement;
      const draw = () => lineChart(cv, 120, [{ name: title, color, y, fill: true }], { xLabels: tl.dates, markers: tl.surge.map((s, i) => (s ? i : -1)).filter((i) => i >= 0), markerColor: "#ffffff", onPick: pick, selected: sel, yMax });
      requestAnimationFrame(draw); redraw.push(draw);
    };
    mk("tweets per day", C.teal, tl.tweets);
    mk("model mean violence score", C.red, tl.mean_p, 1);
    mk("ACLED farmer events", C.amber, tl.acled);
    t.onHover((i, x, y) => showTip(x, y, `<b>${tl.dates[i]}</b>${tl.surge[i] ? " · <span style='color:#fff'>surge day</span>" : ""}<br><span style="color:${C.teal}">■</span> ${tl.tweets[i].toLocaleString()} tweets<br><span style="color:${C.red}">■</span> signal ${f3(tl.mean_p[i])}<br><span style="color:${C.amber}">■</span> ${tl.acled[i]} ACLED events`), hideTip);
    t.onPick(pick);
    const sg = host.querySelector("#surge") as HTMLElement;
    tl.dates.forEach((d, i) => { if (!tl.surge[i]) return; const b = el("button", "chip", d); b.onclick = () => { pick(i); window.scrollTo({ top: dayBox.offsetTop - 80, behavior: "smooth" }); }; sg.appendChild(b); });
    const q = new URLSearchParams(location.hash.split("?")[1] ?? "").get("d"); const qi = q ? tl.dates.indexOf(q) : -1;
    if (qi >= 0) pick(qi);
  }).catch((e: Error) => host.querySelector("#stage")!.insertAdjacentHTML("afterbegin", `<div class="empty" style="margin:40px">${esc(e.message)}</div>`));

  async function loadDay(tl: Timeline, i: number) {
    const d = tl.dates[i];
    dayBox.innerHTML = `<div class="card"><span class="spin l"></span> scoring tweets from ${d}…</div>`;
    try { renderDay(await api.day(d, 250), tl.surge[i]); } catch (e) { dayBox.innerHTML = ""; dayBox.appendChild(errBox((e as Error).message + " (if the archive is still loading, try again in a few seconds)")); }
  }
  function renderDay(r: DayInfo, surge: boolean) {
    dayBox.innerHTML = ""; dayBox.classList.add("fade");
    const k = el("div", "grid g4"); dayBox.appendChild(k);
    k.appendChild(el("div", "card kpi", `<div class="big">${r.date}</div><div class="l">${surge ? "ACLED surge day" : "regular day"}</div>`));
    k.appendChild(el("div", "card kpi", `<div class="big" style="color:var(--teal)">${r.tweets_total.toLocaleString()}</div><div class="l">tweets that day (${r.scored} scored live)</div>`));
    k.appendChild(el("div", "card kpi", `<div class="big" style="color:var(--red)">${pct(r.share_violent, 0)}</div><div class="l">of sample flagged violent · ${esc(r.model)}</div>`));
    k.appendChild(el("div", "card kpi", `<div class="big" style="color:var(--amber)">${r.acled ? r.acled.farmer_events : "–"}</div><div class="l">ACLED farmer events${r.acled ? ` (${r.acled.farmer_violent} violent, ${r.acled.farmer_riots} riots)` : ""}</div>`));
    const g = el("div", "grid g2"); g.style.marginTop = "16px"; dayBox.appendChild(g);
    const hc = el("div", "card", `<h3>Score distribution of the day's tweets</h3><canvas class="chart"></canvas>
      <p class="sub" style="margin:8px 0 0">Mean score ${f3(r.mean_score)} — higher than <b>${r.percentile ?? "–"}%</b> of the 386 archive days.</p>`); g.appendChild(hc);
    requestAnimationFrame(() => histChart(hc.querySelector("canvas") as HTMLCanvasElement, 170, r.hist, r.threshold));
    const ac = el("div", "card", `<h3>Protest actions mentioned (all ${r.tweets_total.toLocaleString()} tweets)</h3><div class="row">${Object.entries(r.actions).filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]).map(([a, v]) => `<span class="chip amber">${ACTION_LABEL[a] ?? a} · ${v.toLocaleString()}</span>`).join("") || `<span class="sub">none</span>`}</div>
      <h3 style="margin-top:16px">States mentioned</h3><div class="row">${r.states.map((s) => `<span class="chip blue">${esc(s.state)} · ${s.tweets.toLocaleString()}</span>`).join("") || `<span class="sub">none</span>`}</div>`); g.appendChild(ac);
    const tp = el("div", "card"); tp.style.marginTop = "16px"; dayBox.appendChild(tp);
    tp.innerHTML = `<h3>Highest-scoring tweets in the sample (handles and links removed)</h3>` + r.top.map((x) => `<p class="quote">${esc(x.text)}<small>score ${x.score.toFixed(4)}</small></p>`).join("") +
      `<p class="sub">High score = violent wording, not proof that violence happened. Compare with the ACLED counts above.</p>`;
    void pairBars;
  }
  return () => { dead = true; dispose(); hideTip(); };
}
