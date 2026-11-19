import { api, Lab, Status } from "../api";
import { C, el, esc, f3, setup } from "../charts";
import { errBox } from "../ui";

const sig = (x: number) => 1 / (1 + Math.exp(-x));
const lg = (p: number) => Math.log(Math.min(1 - 1e-9, Math.max(1e-9, p)) / (1 - Math.min(1 - 1e-9, Math.max(1e-9, p))));

interface M { tp: number; fp: number; fn: number; tn: number; rtp: number; rfp: number; rfn: number; rtn: number; p: number; r: number; f1: number }
function metrics(s: number[], y: number[], w: number[], t: number): M {
  let tp = 0, fp = 0, fn = 0, tn = 0, rtp = 0, rfp = 0, rfn = 0, rtn = 0;
  for (let i = 0; i < s.length; i++) {
    const pr = s[i] >= t;
    if (pr && y[i]) { tp += w[i]; rtp++; } else if (pr) { fp += w[i]; rfp++; } else if (y[i]) { fn += w[i]; rfn++; } else { tn += w[i]; rtn++; }
  }
  const p = tp + fp ? tp / (tp + fp) : 0, r = tp + fn ? tp / (tp + fn) : 0;
  return { tp, fp, fn, tn, rtp, rfp, rfn, rtn, p, r, f1: p + r ? (2 * p * r) / (p + r) : 0 };
}

export function lab(host: HTMLElement, _st: () => Status | null) {
  host.innerHTML = `<h2>Threshold lab</h2><p class="lead">The gold sample is 293 real #FarmersProtest tweets that were labelled violent or not. Slide the decision threshold and see precision, recall and F1 change for each model. Numbers are recomputed live from the model's scores on those tweets and weighted to undo the stratified sampling.</p><div id="c"></div>`;
  const box = host.querySelector("#c") as HTMLElement;
  box.innerHTML = `<div class="card"><span class="spin l"></span> scoring the gold sample…</div>`;
  let dead = false;
  api.lab().then((d) => { if (!dead) build(d); }).catch((e: Error) => { box.innerHTML = ""; box.appendChild(errBox(e.message)); });

  function build(d: Lab) {
    box.innerHTML = "";
    const ids = Object.keys(d.models); let cur = ids.includes("tweet_transformer") ? "tweet_transformer" : ids[0];
    let sv = lg(d.models[cur].default_threshold);
    const top = el("div", "card"); box.appendChild(top);
    top.innerHTML = `<div class="row" style="justify-content:space-between"><div class="row"><span class="sub">Model</span><select id="m" style="width:auto">${ids.map((k) => `<option value="${k}" ${k === cur ? "selected" : ""}>${esc(d.models[k].name)}</option>`).join("")}</select></div>
      <div class="row"><button class="ghost" id="def">default threshold</button><button class="ghost" id="best">best F1 on this sample</button></div></div>
      <div style="margin-top:14px"><div class="mrow"><span>threshold</span><b id="tv"></b></div><input class="slider" id="sl" type="range" min="-9" max="9" step="0.02"><div class="mrow"><span>flags almost everything</span><span>flags almost nothing</span></div></div>`;
    const kp = el("div", "grid g4"); kp.style.marginTop = "16px"; box.appendChild(kp);
    const g = el("div", "grid g2"); g.style.marginTop = "16px"; box.appendChild(g);
    const pc = el("div", "card", `<h3>Precision–recall curve</h3><canvas class="chart"></canvas>`), fc = el("div", "card", `<h3>F1 as the threshold moves (logit scale)</h3><canvas class="chart"></canvas>`);
    g.appendChild(pc); g.appendChild(fc);
    const cmx = el("div", "card"); cmx.style.marginTop = "16px"; box.appendChild(cmx);
    const note = el("div"); box.appendChild(note);
    const sl = top.querySelector("#sl") as HTMLInputElement, tv = top.querySelector("#tv") as HTMLElement;

    const bestT = () => {
      const s = d.models[cur].scores; const u = [...new Set(s)].sort((a, b) => a - b); let bt = u[0], bf = -1;
      for (let i = 0; i < u.length; i++) { const t = i ? (u[i - 1] + u[i]) / 2 : u[0]; const f = metrics(s, d.labels, d.weights, t).f1; if (f > bf) { bf = f; bt = t; } }
      return bt;
    };
    function update() {
      const m = d.models[cur], t = sig(sv), r = metrics(m.scores, d.labels, d.weights, t);
      sl.value = String(sv); tv.textContent = t.toFixed(5);
      kp.innerHTML = "";
      [["precision", r.p, C.teal], ["recall", r.r, C.amber], ["F1", r.f1, C.text]].forEach(([l, v, c]) => kp.appendChild(el("div", "card kpi", `<div class="big" style="color:${c}">${((v as number) * 100).toFixed(1)}%</div><div class="l">${l} (weighted)</div>`)));
      kp.appendChild(el("div", "card kpi", `<div class="big">${r.rtp + r.rfp}</div><div class="l">tweets flagged of ${d.n}</div>`));
      cmx.innerHTML = `<h3>Confusion matrix — ${esc(m.name)}</h3><table style="max-width:560px"><tr><th></th><th class="num">labelled violent</th><th class="num">labelled not violent</th></tr>
        <tr><td>model flags it</td><td class="num" style="color:var(--teal)">${r.rtp} <span class="sub">(weighted ${r.tp.toFixed(0)})</span></td><td class="num" style="color:var(--red)">${r.rfp} <span class="sub">(${r.fp.toFixed(0)})</span></td></tr>
        <tr><td>model does not flag it</td><td class="num" style="color:var(--red)">${r.rfn} <span class="sub">(${r.fn.toFixed(0)})</span></td><td class="num" style="color:var(--teal)">${r.rtn} <span class="sub">(${r.tn.toFixed(0)})</span></td></tr></table>
        <p class="sub" style="margin:8px 0 0">Raw counts on the 293 tweets, with weighted counts in brackets. The weights re-scale the stratified sample to the real tweet mix (about 7% violent).</p>`;
      drawCurves(r);
      const cv = d.cv?.models[cur === "tweet_transformer" ? "transformer" : cur === "tweet_tfidf" ? "tfidf" : ""];
      note.innerHTML = (cv ? `<div class="note teal"><b>Honest estimate:</b> if the threshold is chosen on part of the gold sample and tested on the rest (${d.cv!.folds}-fold, ${d.cv!.repeats} repeats), F1 is <b>${(cv.f1 * 100).toFixed(1)}%</b> (precision ${(cv.precision * 100).toFixed(1)}%, recall ${(cv.recall * 100).toFixed(1)}%). The "best F1 on this sample" button is optimistic because it picks the threshold on the same tweets it is scored on.</div>` : "") +
        (cur === "acled_report" ? `<div class="note">This model was trained on ACLED report-style text, not tweets. Its poor recall here is the expected result — it shows why a tweet-specific model was needed.</div>` : "") +
        (cur === "rule" ? `<div class="note">The keyword rule has only two possible scores, so moving the threshold between 0 and 1 changes nothing.</div>` : "") +
        `<div class="note">${esc(d.note)}</div>`;
    }
    function drawCurves(cur0: M) {
      const m = d.models[cur], u = [...new Set(m.scores)].sort((a, b) => a - b);
      const pts = u.map((v, i) => ({ t: i ? (u[i - 1] + v) / 2 : v - 1e-6, ...metrics(m.scores, d.labels, d.weights, i ? (u[i - 1] + v) / 2 : v - 1e-6) }));
      { // PR
        const { ctx, w, h } = setup(pc.querySelector("canvas") as HTMLCanvasElement, 250), L = 38, T = 8, B = 28, R = 10, pw = w - L - R, ph = h - T - B;
        ctx.font = "10.5px 'JetBrains Mono',monospace"; ctx.fillStyle = C.muted; ctx.strokeStyle = C.line;
        for (let k = 0; k <= 4; k++) { const y = T + ph - (k / 4) * ph; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.textAlign = "right"; ctx.fillText((k / 4).toFixed(2), L - 5, y + 3); ctx.textAlign = "center"; ctx.fillText((k / 4).toFixed(2), L + (k / 4) * pw, h - 12); }
        ctx.textAlign = "left"; ctx.fillText("recall →", L, h - 1);
        const sorted = [...pts].sort((a, b) => a.r - b.r || b.p - a.p);
        ctx.strokeStyle = C.teal; ctx.lineWidth = 2; ctx.beginPath(); sorted.forEach((p, i) => { const x = L + p.r * pw, y = T + ph - p.p * ph; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); }); ctx.stroke();
        ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(L + cur0.r * pw, T + ph - cur0.p * ph, 6, 0, 7); ctx.fill();
      }
      { // F1 vs threshold (logit axis)
        const { ctx, w, h } = setup(fc.querySelector("canvas") as HTMLCanvasElement, 250), L = 38, T = 8, B = 28, R = 10, pw = w - L - R, ph = h - T - B, X = (s: number) => L + ((s + 9) / 18) * pw;
        ctx.font = "10.5px 'JetBrains Mono',monospace"; ctx.fillStyle = C.muted; ctx.strokeStyle = C.line;
        for (let k = 0; k <= 4; k++) { const y = T + ph - (k / 4) * ph; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke(); ctx.textAlign = "right"; ctx.fillText((k / 4).toFixed(2), L - 5, y + 3); }
        ctx.textAlign = "center"; [-8, -4, 0, 4, 8].forEach((s) => ctx.fillText(sig(s).toFixed(s === 0 ? 1 : 4).replace(/0+$/, ""), X(s), h - 12));
        const xs: number[] = [], fs: number[] = [];
        for (let s = -9; s <= 9.001; s += 0.06) { xs.push(s); fs.push(metrics(m.scores, d.labels, d.weights, sig(s)).f1); }
        ctx.strokeStyle = C.amber; ctx.lineWidth = 2; ctx.beginPath(); xs.forEach((s, i) => { const y = T + ph - fs[i] * ph; i ? ctx.lineTo(X(s), y) : ctx.moveTo(X(s), y); }); ctx.stroke();
        const dt = lg(m.default_threshold); ctx.strokeStyle = "#ffffff55"; ctx.setLineDash([4, 3]); ctx.beginPath(); ctx.moveTo(X(dt), T); ctx.lineTo(X(dt), T + ph); ctx.stroke(); ctx.setLineDash([]);
        ctx.fillStyle = "#ffffff99"; ctx.textAlign = "left"; ctx.fillText("default", X(dt) + 4, T + 10);
        ctx.strokeStyle = "#fff"; ctx.lineWidth = 2; ctx.beginPath(); ctx.moveTo(X(sv), T); ctx.lineTo(X(sv), T + ph); ctx.stroke();
        ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(X(sv), T + ph - cur0.f1 * ph, 5, 0, 7); ctx.fill();
        ctx.fillStyle = C.muted; ctx.textAlign = "left"; ctx.fillText("threshold (logit spacing) →", L, h - 1);
      }
      void f3;
    }
    (top.querySelector("#m") as HTMLSelectElement).onchange = (e) => { cur = (e.target as HTMLSelectElement).value; sv = lg(d.models[cur].default_threshold); update(); };
    sl.oninput = () => { sv = parseFloat(sl.value); update(); };
    (top.querySelector("#def") as HTMLButtonElement).onclick = () => { sv = lg(d.models[cur].default_threshold); update(); };
    (top.querySelector("#best") as HTMLButtonElement).onclick = () => { sv = Math.max(-9, Math.min(9, lg(bestT()))); update(); };
    update();
  }
  return () => { dead = true; };
}
