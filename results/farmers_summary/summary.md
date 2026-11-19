# #FarmersProtest results summary

Gold labels: 300 tweets labelled by Claude (single annotator, not human-verified); metrics are weighted to estimate the whole tweet population and carry wide intervals.

## Data
- 1,084,452 tweets, 2020-11-01 to 2021-11-21 (386 days); ACLED India events in window: 19,679, farmer-related: 5,257

## Tweet-level violence detection (gold sample, weighted)

| model | precision | recall | F1 | F1 95% CI | PR-AUC | silver-test F1 (agreement with rules) | F1 with threshold tuned on gold (5-fold CV) |
|---|---|---|---|---|---|---|---|
| rule (keywords) | 0.9384 | 0.8379 | 0.8853 | [0.67, 0.98] | 0.797 | - | - |
| tfidf | 0.6735 | 0.8459 | 0.7499 | [0.46, 0.94] | 0.8859 | 0.7867 | 0.77 |
| transformer | 0.4047 | 0.9893 | 0.5744 | [0.32, 0.76] | 0.8758 | 0.9482 | 0.81 |
| ACLED-trained DistilRoBERTa (no retraining) | 0.398 | 0.094 | 0.153 |  |  | - | - |

The rule list was written while reading the gold sample, so the rule's own row is optimistic. The silver-test column measures agreement with the rules, not the truth. The threshold of each model is tuned on silver validation data (no ambiguous tweets, about 25% positive), which is not like the real tweet mix (about 7% violent), so precision on the gold sample is low and recall high; the last column re-tunes the threshold on part of the gold sample and scores the rest.

## Event and place extraction vs ACLED

| event type | tweets | ACLED events naming it | Spearman (daily) | precision@10 busiest days | chance |
|---|---|---|---|---|---|
| bharat_bandh | 9,714 | 423 | 0.225 | 0.5 | 0.021 |
| chakka_jaam | 6,500 | 230 | 0.182 | 0.2 | 0.01 |
| rail_roko | 2,296 | 157 | 0.212 | 0.4 | 0.171 |
| tractor_rally | 27,276 | 31 | 0.173 | 0.4 | 0.054 |
| mahapanchayat | 5,534 | 37 | 0.34 | 0.4 | 0.078 |
| road_blockade | 795 | 269 | 0.142 | 0.3 | 0.07 |
| police_force | 7,457 | 71 | 0.356 | 0.5 | 0.08 |
| clash | 18,384 | 358 | 0.322 | 0.5 | 0.233 |
| hunger_strike_dharna | 5,549 | 238 | 0.107 | 0.7 | 0.342 |

Places: Spearman between tweet mentions and ACLED farmer events is 0.343 per state-day and 0.496 per state-month. On 52 busy days (20+ ACLED farmer events) the most-mentioned state matches ACLED's top state 10% of the time (always guessing Delhi: 4%); ACLED's top state is among the tweets' top 3 in 52% of them.

## Predicting ACLED farmer-protest surges (walk-forward, ROC-AUC with 95% interval; 0.5 = chance)

| task | surges | persistence | acled | tweets | acled_tweets | model_signals | acled_tweets_model |
|---|---|---|---|---|---|---|---|
| same day | 9 | 0.55 [0.34, 0.74] | 0.51 [0.24, 0.76] | 0.63 [0.43, 0.91] | 0.66 [0.44, 0.94] | 0.50 [0.22, 0.79] | 0.63 [0.34, 0.90] |
| next 1 day | 8 | 0.52 [0.29, 0.72] | 0.59 [0.36, 0.77] | 0.51 [0.25, 0.81] | 0.55 [0.33, 0.82] | 0.56 [0.37, 0.78] | 0.52 [0.31, 0.77] |
| next 3 days | 23 | 0.50 [0.33, 0.68] | 0.54 [0.37, 0.67] | 0.43 [0.19, 0.70] | 0.51 [0.31, 0.72] | 0.50 [0.33, 0.69] | 0.51 [0.28, 0.74] |
| next 7 days | 50 | 0.50 [0.34, 0.64] | 0.51 [0.36, 0.66] | 0.41 [0.22, 0.61] | 0.54 [0.37, 0.70] | 0.48 [0.33, 0.63] | 0.55 [0.37, 0.72] |

Difference in ROC-AUC, (ACLED history + tweets) minus (ACLED history only), 95% interval: same day [0.006, 0.284]; 1 day [-0.264, 0.259]; 3 days [-0.243, 0.213]; 7 days [-0.21, 0.24]
