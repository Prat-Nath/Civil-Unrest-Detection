import { api, Analysis, Status } from "../api";
import { el, esc } from "../charts";
import { busy, errBox, modelBanner, SIGNAL_TEXT, ACTION_LABEL } from "../ui";

const EXAMPLES: [string, string][] = [
  ["Police force", "Police fired tear gas and water cannons at farmers marching towards the Delhi border, several injured. #FarmersProtest"],
  ["Peaceful rally", "Thousands of farmers gathered peacefully for the mahapanchayat in Muzaffarnagar today. Speeches, songs and langar for everyone."],
  ["Riot report", "Mob torched vehicles and clashed with police near the Red Fort; stones were thrown and many policemen were hurt."],
  ["Angry rhetoric", "This government is killing our farmers with these black laws. Shame on you, we will never forgive this betrayal!"],
  ["Announcement", "Bharat Bandh tomorrow: farmers will block highways and rail tracks across Punjab and Haryana from 6 am to 4 pm."],
];

export function analyzer(host: HTMLElement, st: () => Status | null) {
  host.innerHTML = `<h2>Analyze a post</h2><p class="lead">One post in, a full read-out: what each trained model thinks, which words pushed the score, and which protest actions and states it mentions.</p>`;
  const b = modelBanner(st()); if (b) host.appendChild(b);
  const card = el("div", "card"); host.appendChild(card);
  card.innerHTML = `<textarea id="t" rows="4" maxlength="2000" placeholder="Paste a tweet or news line…  (Ctrl+Enter to analyze)"></textarea>
    <div class="row" style="margin-top:12px"><button id="go">Analyze</button><span class="sub">Try:</span><span class="row" id="ex"></span></div>`;
  const ta = card.querySelector("#t") as HTMLTextAreaElement, go = card.querySelector("#go") as HTMLButtonElement;
  const exBox = card.querySelector("#ex") as HTMLElement;
  EXAMPLES.forEach(([l, t]) => { const c = el("button", "chip", l); c.onclick = () => { ta.value = t; run(); }; exBox.appendChild(c); });
  const out = el("div", "", ""); out.style.marginTop = "18px"; host.appendChild(out);
  let explainModel: string | undefined;

  async function run() {
    const text = ta.value.trim(); if (text.length < 3) return;
    busy(go, true, "Scoring…"); out.innerHTML = "";
    try { show(await api.analyze(text, true, explainModel)); } catch (e) { out.appendChild(errBox((e as Error).message)); }
    busy(go, false);
  }
  go.onclick = run;
  ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) run(); });

  function show(a: Analysis) {
    out.innerHTML = ""; out.classList.add("fade");
    const prim = a.models.find((m) => m.id === a.primary);
    const top = el("div", "card glow"); out.appendChild(top);
    top.innerHTML = `<div class="verdict"><span class="badge ${a.signal}">${a.signal.replace("_", " ")}</span>
      <div><div style="font-size:18px;font-weight:600">${SIGNAL_TEXT[a.signal]}</div>
      <div class="sub">Primary model: ${prim?.name} · score ${prim?.score.toFixed(4)} · decision threshold ${prim?.threshold.toFixed(4)}</div></div></div>
      ${a.agreement === "disagree" ? `<div class="note">The two tweet models <b>disagree</b>. Treat this post as uncertain — see the cards below.</div>` : ""}
      ${prim && (prim.score > 0.99 || prim.score < 0.01) ? `<div class="note teal">The transformer's scores sit close to 0 or 1 (it was trained on a clean keyword signal), so a high score is a strong <i>wording</i> match, not a calibrated probability of violence.</div>` : ""}`;

    const g = el("div", "grid g3"); g.style.marginTop = "16px"; out.appendChild(g);
    a.models.forEach((m) => {
      const c = el("div", "card");
      const col = m.verdict === "violent" ? "var(--red)" : "var(--teal)";
      c.innerHTML = `<h3>${m.name}</h3><div class="big" style="color:${col}">${(m.score * 100).toFixed(1)}%</div>
        <div class="gauge"><div class="f" style="width:${m.score * 100}%"></div><div class="t" style="left:${m.threshold * 100}%"></div></div>
        <div class="mrow"><span>verdict <b style="color:${col}">${m.verdict.replace("_", " ")}</b></span><span>thr ${m.threshold.toFixed(3)}</span></div>
        <p class="sub" style="margin:10px 0 0;font-size:12px">${esc(m.note)}${m.kind === "report" ? " <b>Expect low scores on tweets.</b>" : ""}</p>`;
      g.appendChild(c);
    });

    const g2 = el("div", "grid g2"); g2.style.marginTop = "16px"; out.appendChild(g2);
    // keyword rule highlight
    const rc = el("div", "card"); g2.appendChild(rc);
    let html = "", last = 0;
    for (const [s, e, t] of a.rule.spans) { if (s < last) continue; html += esc(a.text.slice(last, s)) + `<span class="hl-${t}">${esc(a.text.slice(s, e))}</span>`; last = e; }
    html += esc(a.text.slice(last));
    rc.innerHTML = `<h3>Keyword rules (the weak-label source)</h3><div class="textflow">${html}</div>
      <div class="legend" style="margin-top:8px"><span><i style="background:var(--red)"></i>strong violence word</span><span><i style="background:var(--amber)"></i>weak / ambiguous word</span></div>
      <p class="sub" style="margin:10px 0 0">The models were trained on labels derived from these rules, then learn wording beyond them.</p>`;
    // protest actions & states
    const ac = el("div", "card"); g2.appendChild(ac);
    ac.innerHTML = `<h3>Protest actions &amp; places mentioned</h3>
      <div class="row">${a.actions.length ? a.actions.map((x) => `<span class="chip amber">${ACTION_LABEL[x] ?? x}</span>`).join("") : `<span class="sub">no protest action keyword found</span>`}</div>
      <div class="row" style="margin-top:10px">${a.states.length ? a.states.map((x) => `<span class="chip blue">${esc(x)}</span>`).join("") : `<span class="sub">no Indian state / capital mentioned</span>`}</div>
      <p class="sub" style="margin:12px 0 0">This is the event-extraction part: a rule-based extractor pulls action types and places from text; on the archive it correlates with ACLED event counts (see Evidence).</p>`;

    // explanation
    if (a.explain) {
      const ex = el("div", "card"); ex.style.marginTop = "16px"; out.appendChild(ex);
      const mx = Math.max(1, ...a.explain.words.map((w) => Math.abs(w.d)));
      const words = a.explain.words.map((w) => {
        const k = Math.min(1, Math.abs(w.d) / mx), c = w.d > 0 ? `255,77,94` : `62,230,196`;
        return `<span title="${w.d > 0 ? "+" : ""}${w.d.toFixed(2)} logit" style="background:rgba(${c},${(k * 0.7).toFixed(2)})">${esc(w.w)}</span>`;
      }).join(" ") + (a.explain.rest.length ? ` <span class="sub">… +${a.explain.rest.length} more words not analysed</span>` : "");
      const opts = a.models.filter((m) => m.kind === "tweet").map((m) => `<option value="${m.id}" ${m.id === a.explain!.model ? "selected" : ""}>${m.name}</option>`).join("");
      ex.innerHTML = `<div class="row" style="justify-content:space-between"><h3 style="margin:0">Which words drove the score — ${esc(a.explain.model_name)}</h3>
        <select id="em" style="width:auto">${opts}</select></div>
        <div class="textflow" style="margin-top:10px">${words}</div>
        <div class="legend" style="margin-top:8px"><span><i style="background:var(--red)"></i>pushes toward violent</span><span><i style="background:var(--teal)"></i>pushes away</span><span>hover a word for its effect</span></div>
        <p class="sub" style="margin:10px 0 0">Method: occlusion. Each word is removed in turn and the change in the model's logit is measured — the model is re-run on the real text, nothing is mocked.</p>`;
      (ex.querySelector("#em") as HTMLSelectElement).onchange = (e) => { explainModel = (e.target as HTMLSelectElement).value; run(); };
    }
  }
}
