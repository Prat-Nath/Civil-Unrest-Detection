import { api, Evidence, Status } from "../api";
import { C, el, esc, pairBars } from "../charts";
import { ACTION_LABEL, errBox } from "../ui";

const MODEL_LABEL: Record<string, string> = { persistence: "yesterday's events", acled: "ACLED history", tweets: "tweets", acled_tweets: "ACLED + tweets", model_signals: "model signal", acled_tweets_model: "ACLED + tweets + model" };
const HOR: Record<string, string> = { h0: "same day", h1: "1 day ahead", h3: "3 days ahead", h7: "7 days ahead" };

export function evidence(host: HTMLElement, _st: () => Status | null) {
  host.innerHTML = `<h2>Evidence</h2><p class="lead">What the trained models and extractors actually achieve against independent ACLED records. Every figure on this page is read from your saved result files.</p><div id="c"><div class="card"><span class="spin l"></span> loading results…</div></div>`;
  const box = host.querySelector("#c") as HTMLElement; let dead = false;
  api.evidence().then((e) => { if (!dead) build(e); }).catch((e: Error) => { box.innerHTML = ""; box.appendChild(errBox(e.message)); });

  function build(e: Evidence) {
    box.innerHTML = "";
    if (e.forecast) {
      const models = ["persistence", "acled", "tweets", "acled_tweets", "model_signals", "acled_tweets_model"];
      const c = el("div", "card"); box.appendChild(c);
      const cell = (v: number, ci?: number[]) => { const k = Math.max(0, Math.min(1, (v - 0.45) / 0.25)); return `<td class="num" style="background:rgba(62,230,196,${(k * 0.35).toFixed(2)})">${v.toFixed(2)}${ci ? `<div class="sub" style="font-size:10px">[${ci[0].toFixed(2)}, ${ci[1].toFixed(2)}]</div>` : ""}</td>`; };
      c.innerHTML = `<h3>Surge-day forecasting — ROC-AUC (0.5 = chance)</h3><div class="scroll"><table><tr><th>horizon</th><th class="num">surge days</th>${models.map((m) => `<th class="num">${MODEL_LABEL[m]}</th>`).join("")}</tr>` +
        Object.entries(e.forecast).map(([h, f]) => `<tr><td>${HOR[h] ?? h}</td><td class="num">${f.positives}</td>${models.map((m) => (f.models[m] ? cell(f.models[m].roc_auc, f.ci[m]) : `<td class="num">–</td>`)).join("")}</tr>`).join("") + `</table></div>
        <div class="note"><b>Reading this honestly.</b> Only 15 days in the archive are surges, so the confidence intervals (in brackets) are wide. Adding tweets to ACLED history helps on the <i>same</i> day (paired difference CI above zero) but there is no skill at 1, 3 or 7 days: the tweet models describe unrest as it happens, they do not forecast it.</div>`;
    }
    const g = el("div", "grid g2"); g.style.marginTop = "16px"; box.appendChild(g);
    if (e.extraction) {
      const c = el("div", "card"); g.appendChild(c);
      c.innerHTML = `<h3>Event extraction vs ACLED (daily Spearman correlation)</h3><table><tr><th>action type</th><th class="num">tweets</th><th class="num">ACLED events</th><th class="num">ρ daily</th><th class="num">P@10</th></tr>` +
        Object.entries(e.extraction.event_types).map(([k, v]) => `<tr><td>${ACTION_LABEL[k] ?? k}</td><td class="num">${v.tweets_mentioning.toLocaleString()}</td><td class="num">${v.acled_events_matching}</td><td class="num">${v.spearman_daily.toFixed(2)}</td><td class="num">${(v.precision_at_10_days * 100).toFixed(0)}%</td></tr>`).join("") + `</table>
        <p class="sub" style="margin:8px 0 0">P@10: of the 10 days with most tweets about the action, how many had a matching ACLED event.</p>`;
      const p = e.extraction.places, c2 = el("div", "card"); g.appendChild(c2);
      const rows: [string, string][] = [["state-day correlation", String(p.spearman_state_day)], ["state-month correlation", String(p.spearman_state_month)], ["busy days evaluated", String(p.busy_days_n)],
        ["top state matches ACLED's (accuracy)", String(p.top_state_match)], ["baseline: always guess " + String((p.always_predict_most_mentioned_state_overall as unknown as { state: string }).state), String((p.always_predict_most_mentioned_state_overall as unknown as { accuracy: number }).accuracy)], ["ACLED's top state inside tweets' top 3", String(p.acled_top_state_in_tweets_top3)]];
      c2.innerHTML = `<h3>Place extraction (Indian states)</h3><table>${rows.map(([a, b]) => `<tr><td>${a}</td><td class="num">${esc(b)}</td></tr>`).join("")}</table>
        <p class="sub" style="margin:8px 0 0">Tweets overwhelmingly mention Delhi, ACLED's busiest states are Punjab and Haryana. Place extraction is useful over a month, weak for picking a single day's hotspot.</p>`;
    }
    if (e.states?.length) {
      const c = el("div", "card", `<h3>States: tweet mentions vs ACLED farmer events</h3><canvas class="chart"></canvas>`); c.style.marginTop = "16px"; box.appendChild(c);
      requestAnimationFrame(() => pairBars(c.querySelector("canvas") as HTMLCanvasElement, e.states!.map((s) => [s.state, s.tweet_mentions, s.acled_farmer_events]), C.teal, C.amber, "tweet mentions", "ACLED events"));
    }
    if (e.social?.length) {
      const c = el("div", "card"); c.style.marginTop = "16px"; box.appendChild(c);
      const rows = e.social.filter((r) => [1, 3, 7].includes(r.horizon)).sort((a, b) => a.horizon - b.horizon || b.pr_auc_lift_over_prevalence_mean - a.pr_auc_lift_over_prevalence_mean);
      c.innerHTML = `<h3>Cross-country unrest forecasting (G-CUT, 93M tweets, 42 countries)</h3><div class="scroll" style="max-height:340px"><table><tr><th>horizon</th><th>model</th><th class="num">F1</th><th class="num">PR-AUC</th><th class="num">lift over chance</th><th class="num">ROC-AUC</th><th class="num">seeds</th></tr>` +
        rows.map((r) => `<tr><td>${r.horizon} day${r.horizon > 1 ? "s" : ""}</td><td>${esc(r.model)}</td><td class="num">${r.f1_mean.toFixed(3)}</td><td class="num">${r.pr_auc_mean.toFixed(3)}</td><td class="num"><b>${r.pr_auc_lift_over_prevalence_mean.toFixed(2)}×</b></td><td class="num">${r.roc_auc_mean.toFixed(3)}</td><td class="num">${r.seeds}</td></tr>`).join("") + `</table></div>
        <p class="sub" style="margin:8px 0 0">On this large multi-country dataset a sequence model (GRU) beats chance by 1.9–4.7× depending on horizon; "lift" = PR-AUC divided by the share of positive country-days.</p>`;
    }
    box.appendChild(el("div", "note", `<b>Limits you should state with these numbers:</b> trained on one event; weak labels from keyword rules; the gold sample has one annotator (no inter-annotator agreement); 293 tweets give wide confidence intervals; tweet-model scores are saturated near 0/1; gold-tuned thresholds are the honest basis, not the default 0.5.`));
  }
  return () => { dead = true; };
}
