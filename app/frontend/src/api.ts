export interface ModelInfo { id: string; name: string; kind: "tweet" | "report"; threshold: number; threshold_note: string; note: string }
export interface Status { models: ModelInfo[]; errors: string[]; device: string; load_seconds: number | null; models_loading: boolean; archive_loaded: boolean; archive_loading: boolean; gold_n: number | null; event_types: string[]; states: string[] }
export interface ModelScore extends ModelInfo { score: number; logit: number; verdict: "violent" | "not_violent" }
export interface Analysis {
  text: string; models: ModelScore[]; primary: string; signal: "violent" | "protest_topic" | "none";
  rule: { strong: string[]; weak: string[]; spans: [number, number, "strong" | "weak"][] };
  actions: string[]; states: string[]; agreement?: "agree" | "disagree";
  explain?: { model: string; model_name: string; words: { w: string; d: number }[]; rest: string[]; base_logit: number };
}
export interface BatchRow { i: number; text: string; score: number | null; verdict: string; signal: string; actions: string[]; states: string[]; words: string[] }
export interface Batch {
  rows: BatchRow[];
  summary: { n: number; scored: number; violent: number; protest_topic: number; mean_score: number | null; model: string; model_name: string; threshold: number; hist: number[] };
  daily: { date: string; posts: number; mean: number; share_violent: number; percentile: number | null; noisy: boolean }[] | null;
}
export interface Timeline { dates: string[]; tweets: number[]; kw_violence: number[]; acled: number[]; acled_violent: number[]; mean_p: (number | null)[]; share_flag: (number | null)[]; surge: boolean[]; signal_model: string | null }
export interface DayInfo {
  date: string; tweets_total: number; scored: number; model: string; mean_score: number | null; share_violent: number | null; threshold: number; hist: number[];
  top: { text: string; score: number }[]; actions: Record<string, number>; states: { state: string; tweets: number }[]; percentile: number | null;
  acled: { farmer_events: number; farmer_violent: number; farmer_riots: number } | null;
}
export interface Lab {
  n: number; labels: number[]; weights: number[]; strata: string[]; note: string;
  models: Record<string, { name: string; scores: number[]; default_threshold: number }>;
  cv: { folds: number; repeats: number; models: Record<string, { precision: number; recall: number; f1: number; mean_threshold: number; f1_sd_over_repeats: number }> } | null;
}
export interface Evidence {
  extraction?: { event_types: Record<string, { tweets_mentioning: number; acled_events_matching: number; spearman_daily: number; precision_at_10_days: number }>; places: Record<string, number | string | boolean | null> };
  states?: { state: string; tweet_mentions: number; acled_farmer_events: number }[];
  forecast?: Record<string, { positives: number; models: Record<string, { roc_auc: number; pr_auc_lift: number }>; ci: Record<string, number[]> }>;
  surge_days?: { date: string; acled_farmer: number; n_tweets: number }[];
  social?: { model: string; horizon: number; f1_mean: number; pr_auc_mean: number; pr_auc_lift_over_prevalence_mean: number; roc_auc_mean: number; prevalence: number; seeds: number }[];
  transfer?: { tweets_scored: number; "share_ge_0.5": number };
}

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, init);
  if (!r.ok) {
    let msg = r.statusText;
    try { const b = await r.json(); msg = b.detail ?? msg; } catch { /* ignore */ }
    throw new Error(String(msg));
  }
  return r.json() as Promise<T>;
}
const post = (b: unknown): RequestInit => ({ method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(b) });

export const api = {
  status: () => j<Status>("/api/status"),
  analyze: (text: string, explain = true, explain_model?: string) => j<Analysis>("/api/analyze", post({ text, explain, explain_model })),
  batch: (texts: string[], dates?: (string | null)[], model?: string) => j<Batch>("/api/batch", post({ texts, dates, model })),
  timeline: () => j<Timeline>("/api/timeline"),
  day: (d: string, n = 250) => j<DayInfo>(`/api/day/${d}?n=${n}`),
  lab: () => j<Lab>("/api/lab"),
  evidence: () => j<Evidence>("/api/evidence"),
};
