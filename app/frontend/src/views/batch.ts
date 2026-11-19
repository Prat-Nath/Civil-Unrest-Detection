import { api, Batch, Status } from "../api";
import { C, el, esc, histChart, lineChart, pct } from "../charts";
import { busy, errBox, modelBanner, ACTION_LABEL } from "../ui";

const SAMPLE = `2021-01-26|Farmers clash with police at ITO, tear gas fired, tractors overturned. Several injured.
2021-01-26|Mob attacked barricades and vandalised vehicles near Red Fort, police lathicharge continues
2021-01-26|Tractor rally began peacefully from Singhu border, thousands joined with flags
2021-01-27|Farmer unions condemn violence and appeal for calm after yesterday's incidents
2021-01-27|Heavy police deployment across Delhi borders, internet suspended in Singhu
2021-02-06|Chakka jaam today: farmers block highways across Punjab and Haryana, traffic diverted
2021-02-06|Peaceful chakka jaam, no incidents reported, langar served to stranded commuters
2021-02-06|Government must repeal the farm laws, our farmers are being betrayed`;

function parseCSV(s: string): string[][] {
  const rows: string[][] = []; let row: string[] = [], f = "", q = false;
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (q) { if (c === '"') { if (s[i + 1] === '"') { f += '"'; i++; } else q = false; } else f += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(f); f = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && s[i + 1] === "\n") i++; row.push(f); f = ""; if (row.some((x) => x.trim())) rows.push(row); row = []; }
    else f += c;
  }
  row.push(f); if (row.some((x) => x.trim())) rows.push(row);
  return rows;
}

export function batch(host: HTMLElement, st: () => Status | null) {
  host.innerHTML = `<h2>Scan a feed</h2><p class="lead">Paste posts (one per line, optionally <span class="mono">date|text</span>) or upload a CSV with a text column and a date column. You get the share of posts flagged violent, the score distribution, and — with dates — each day's signal ranked against all 386 archive days.</p>`;
  const b = modelBanner(st()); if (b) host.appendChild(b);
  const card = el("div", "card"); host.appendChild(card);
  card.innerHTML = `<textarea id="t" rows="7" placeholder="2021-01-26|Police fired tear gas at farmers near the border…&#10;one post per line"></textarea>
    <div class="row" style="margin-top:12px"><button id="go">Scan</button><button class="ghost" id="smp">Load sample</button>
    <label class="btn ghost" style="cursor:pointer">Upload CSV<input id="f" type="file" accept=".csv,.txt" hidden></label><span class="sub" id="info"></span></div>`;
  const ta = card.querySelector("#t") as HTMLTextAreaElement, go = card.querySelector("#go") as HTMLButtonElement, info = card.querySelector("#info") as HTMLElement;
  const out = el("div"); out.style.marginTop = "18px"; host.appendChild(out);
  (card.querySelector("#smp") as HTMLButtonElement).onclick = () => { ta.value = SAMPLE; };
  (card.querySelector("#f") as HTMLInputElement).onchange = async (e) => {
    const file = (e.target as HTMLInputElement).files?.[0]; if (!file) return;
    const rows = parseCSV(await file.text()); if (rows.length < 2) { info.textContent = "file looks empty"; return; }
    const head = rows[0].map((h) => h.trim().toLowerCase());
    let ti = head.findIndex((h) => ["text", "tweet", "content", "post", "body", "message", "clean"].includes(h));
    if (ti < 0) { const len = head.map((_, c) => rows.slice(1, 50).reduce((s, r) => s + (r[c]?.length ?? 0), 0)); ti = len.indexOf(Math.max(...len)); }
    const di = head.findIndex((h) => ["date", "created_at", "timestamp", "time", "datetime", "day"].includes(h));
    ta.value = rows.slice(1).map((r) => (di >= 0 && r[di] ? r[di].trim() + "|" : "") + (r[ti] ?? "").replace(/\s+/g, " ").trim()).filter((l) => l.length > 3).join("\n");
    info.textContent = `${file.name}: text column "${rows[0][ti]}"${di >= 0 ? `, date column "${rows[0][di]}"` : ", no date column found"}; ${Math.min(rows.length - 1, 5000)} rows used`;
  };

  go.onclick = async () => {
    const lines = ta.value.split("\n").map((l) => l.trim()).filter(Boolean).slice(0, 5000);
    if (!lines.length) return;
    const texts: string[] = [], dates: (string | null)[] = []; let anyDate = false;
    for (const l of lines) {
      const m = l.match(/^(\d{4}-\d{2}-\d{2}[^|]{0,20})\|(.*)$/);
      if (m) { dates.push(m[1]); texts.push(m[2]); anyDate = true; } else { dates.push(null); texts.push(l); }
    }
    busy(go, true, `Scoring ${texts.length}…`); out.innerHTML = "";
    try { render(await api.batch(texts, anyDate ? dates : undefined), texts, dates); } catch (e) { out.appendChild(errBox((e as Error).message)); }
    busy(go, false);
  };

  function render(r: Batch, _t: string[], _d: (string | null)[]) {
    out.innerHTML = ""; out.classList.add("fade");
    const s = r.summary, rate = s.scored ? s.violent / s.scored : 0;
    const k = el("div", "grid g4"); out.appendChild(k);
    [[String(s.scored), "posts scored", "var(--text)"], [String(s.violent), "flagged violent", "var(--red)"], [pct(rate), "share flagged", "var(--red)"], [String(s.protest_topic), "protest topic, not flagged", "var(--amber)"]]
      .forEach(([v, l, c]) => k.appendChild(el("div", "card kpi", `<div class="big" style="color:${c}">${v}</div><div class="l">${l}</div>`)));
    const g = el("div", "grid g2"); g.style.marginTop = "16px"; out.appendChild(g);
    const hc = el("div", "card", `<h3>Score distribution — ${esc(s.model_name)}</h3><canvas class="chart"></canvas><p class="sub" style="margin:8px 0 0">Red bars are above the decision threshold (${s.threshold.toFixed(3)}). For reference, about 7% of the archive's real tweets are violent.</p>`); g.appendChild(hc);
    requestAnimationFrame(() => histChart(hc.querySelector("canvas") as HTMLCanvasElement, 190, s.hist, s.threshold));
    const dc = el("div", "card"); g.appendChild(dc);
    if (r.daily && r.daily.length) {
      dc.innerHTML = `<h3>Daily signal vs the archive</h3><canvas class="chart"></canvas>`;
      requestAnimationFrame(() => lineChart(dc.querySelector("canvas") as HTMLCanvasElement, 190, [{ name: "mean score", color: C.red, y: r.daily!.map((d) => d.mean), fill: true }],
        { xLabels: r.daily!.map((d) => d.date), yMax: 1, hlines: [{ y: s.threshold, color: "#ffffff88", label: "threshold" }] }));
      const hot = r.daily.filter((d) => (d.percentile ?? 0) >= 90 && !d.noisy);
      dc.insertAdjacentHTML("beforeend", hot.length
        ? `<div class="note"><b>${hot.map((d) => d.date).join(", ")}</b> scored higher than ≥90% of archive days — an unusually violent-sounding day for this topic.</div>`
        : `<div class="note teal">No day with ≥30 posts stands out against the archive. Days with fewer than 30 posts are marked noisy.</div>`);
    } else dc.innerHTML = `<h3>Daily signal</h3><div class="empty">Add dates (date|text per line, or a CSV date column) to see day-by-day signals compared with the archive.</div>`;

    if (r.daily && r.daily.length) {
      const t = el("div", "card"); t.style.marginTop = "16px"; out.appendChild(t);
      t.innerHTML = `<h3>Days</h3><div class="scroll"><table><tr><th>date</th><th class="num">posts</th><th class="num">mean score</th><th class="num">share violent</th><th class="num">percentile vs archive</th><th></th></tr>` +
        r.daily.map((d) => `<tr><td>${d.date}</td><td class="num">${d.posts}</td><td class="num">${d.mean.toFixed(3)}</td><td class="num">${pct(d.share_violent, 0)}</td><td class="num">${d.percentile ?? "–"}</td><td>${d.noisy ? `<span class="chip">few posts — noisy</span>` : ""}</td></tr>`).join("") + `</table></div>`;
    }

    const tc = el("div", "card"); tc.style.marginTop = "16px"; out.appendChild(tc);
    tc.innerHTML = `<div class="row" style="justify-content:space-between"><h3 style="margin:0">Posts, highest score first</h3>
      <div class="row"><select id="flt" style="width:auto"><option value="all">all</option><option value="violent">flagged violent</option><option value="protest_topic">protest topic</option></select>
      <button class="ghost" id="dl">Download CSV</button></div></div><div class="scroll" id="tb" style="margin-top:10px"></div>`;
    const draw = () => {
      const f = (tc.querySelector("#flt") as HTMLSelectElement).value;
      const rows = r.rows.filter((x) => x.score != null && (f === "all" || x.signal === f)).sort((a, b) => (b.score as number) - (a.score as number)).slice(0, 200);
      (tc.querySelector("#tb") as HTMLElement).innerHTML = `<table><tr><th class="num">score</th><th>signal</th><th>post</th><th>actions / states</th></tr>` + rows.map((x) =>
        `<tr><td class="num" style="color:${x.signal === "violent" ? "var(--red)" : "var(--text)"}">${(x.score as number).toFixed(3)}</td><td><span class="chip ${x.signal === "violent" ? "red" : x.signal === "protest_topic" ? "amber" : ""}">${x.signal.replace("_", " ")}</span></td>` +
        `<td>${esc(x.text)}</td><td>${x.actions.map((a) => `<span class="chip amber">${ACTION_LABEL[a] ?? a}</span>`).join(" ")} ${x.states.map((a) => `<span class="chip blue">${esc(a)}</span>`).join(" ")}</td></tr>`).join("") + `</table>`;
    };
    (tc.querySelector("#flt") as HTMLSelectElement).onchange = draw; draw();
    (tc.querySelector("#dl") as HTMLButtonElement).onclick = () => {
      const q = (v: string) => `"${v.replace(/"/g, '""')}"`;
      const csv = "index,score,verdict,signal,actions,states,text\n" + r.rows.map((x) => [x.i, x.score ?? "", x.verdict, x.signal, q(x.actions.join(";")), q(x.states.join(";")), q(x.text)].join(",")).join("\n");
      const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "signal_room_scan.csv"; a.click();
    };
  }
}
