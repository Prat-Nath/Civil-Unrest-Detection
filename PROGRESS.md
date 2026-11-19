# Project progress: Civil unrest event extraction and prediction

Last updated: 18 Nov 2026 (added the #FarmersProtest tweet archive)

## Status against the proposal

| Deliverable / objective | Status | Where |
|---|---|---|
| Data exploration (ACLED India) | Done | `notebooks/01_data_exploration.ipynb` |
| Preprocessing, baselines, DistilRoBERTa fine-tuning, evaluation with logging | Done and run on ACLED India | `scripts/`, `logs/`, `results/`, `models/` |
| Technical report (methods, models, experiments) | Done (Claude Doc) | shared separately |
| Research paper (Title, Abstract, Intro, Literature Review, Methodology, Results, Discussion and Ethics, Conclusion) | Draft done (Claude Doc), covers the ACLED experiments only | shared separately |
| Social-data pipeline: aggregate, panel, baselines, sequence models, summary | Done and run on the 93.4M-tweet files | `scripts/social_*.py`, `notebooks/02_social_data_analysis.ipynb` |
| Extraction metrics (precision, recall, F1) on curated Twitter text | **Partly**: the #FarmersProtest archive has text but no labels; the ACLED-trained model was run on it (no labels yet, so no P/R/F1). A 300-tweet hand-label sheet is ready | `results/farmers_transfer_*/label_sample.csv`, `scripts/farmers_transfer_eval.py` |
| Prediction metrics and lead-time analysis | Done on the tweet signal (h = 1, 3, 7 days) | `results/social_summary/` |
| Cross-referencing with ACLED ground truth | **Done for the #FarmersProtest tweets** (1.08M tweets, Nov 2020 to Nov 2021, linked to ACLED India by day). Not possible for the G-CUT files (2014/2015/2017 do not overlap ACLED 2018-2025) | `notebooks/03_farmers_protest_analysis.ipynb`, `scripts/farmers_*.py` |
| Add the social-data results to the paper and report | Pending | |
| IEEE-formatted Word and PDF paper | Pending | |
| Presentation slides | Pending | |
| Source code on GitHub | Pending (code is complete locally) | |

## Social tweet data: what was done

- Data: `data/social/tweets_{2014,2015,2017}.json.gz`. Each row has only tweet_id, created_at, country_code, civil_unrest_score and civil_unrest_related (score > 0.5). No text. 93,435,018 tweets, 42 countries; 2016 is missing.
- Task: from a calm country-day, will a surge in flagged tweets occur within the next 1, 3 or 7 days? A surge is z >= 2 against the previous 28 days with at least 20 flagged tweets. Chronological split (train to Feb 2017, validation to Jul 2017, test Jul-Dec 2017).
- Models: base rate, a recent-activity rule, logistic regression, gradient boosting, a small Transformer encoder and a GRU over 28-day windows (3 seeds each).
- Test results (mean over seeds for the sequence models):

| Horizon | Base rate | Best model | PR-AUC (lift over base) | F1 |
|---|---|---|---|---|
| 1 day | 2.5% | GRU | 0.116 (4.7x) | 0.16 |
| 3 days | 6.8% | GRU | 0.170 (2.5x) | 0.25 |
| 7 days | 14.6% | gradient boosting | 0.281 (1.9x) | 0.35 |

  The Transformer was slightly behind the GRU at every horizon (PR-AUC 0.087 / 0.150 / 0.243). Differences among the learned models are small, so no model is clearly best.
- Honest reading: there is a real but weak signal, and it fades as the horizon grows. Labels are classifier-derived, not verified events.

## #FarmersProtest tweets linked to ACLED (built end to end)

- Data: `data/tweets/archive.zip` (1,084,452 tweets with text, 386 days). ACLED India has 5,257 farmer-related events in the window and 15 surge days.
- Pipeline: `scripts/run_farmers_pipeline.py` runs prepare, weak labels, TF-IDF and transformer training, extraction, ACLED-model transfer, forecasting and a summary report (`results/farmers_summary/summary.md`). Commands are in `README.md`.
- Tweet violence detection (gold sample of 293 tweets labelled by Claude, not human-verified): TF-IDF trained on weak labels reaches F1 0.75 (interval 0.46 to 0.94). DistilRoBERTa (2 epochs, GPU run) scores 0.57 at its default threshold (precision 0.40, recall 0.99) because the threshold was tuned on silver data, but 0.81 once the threshold is re-tuned on held-out gold folds (TF-IDF 0.77). The keyword rule scores 0.89 but is optimistic (words were chosen while reading the gold set). The ACLED-trained model does not transfer (weighted precision 0.37, recall 0.09, F1 0.14).
- Extraction: event types (Bharat Bandh, chakka jaam, rail roko, tractor rally, mahapanchayat, blockades, police force, clashes) and places are extracted from tweets and cross-checked against ACLED by day and by state.
- Same-day detection: tweets plus ACLED history reach ROC-AUC 0.66 against 0.51 for ACLED history alone, resting on 9 surge days (interval just above zero).
- Forecasting 1, 3 and 7 days ahead: no skill (ROC-AUC 0.4 to 0.59, intervals include 0.5).
- Details and caveats: `notebooks/03_farmers_protest_analysis.ipynb`.

## Next steps (in order of value)

1. Review and correct `data/farmers_labels/gold_claude.csv`, then re-run the training, transfer-eval and summary steps (see README).
2. Try a tweet-pretrained encoder (`--base-model cardiffnlp/twitter-roberta-base`) and compare it with DistilRoBERTa.
3. Add the social-data and #FarmersProtest sections to the paper and technical report.
4. Convert the paper to IEEE format (Word and PDF), build the slides, push the repository to GitHub.

## Rerun everything (from the project root, in the base conda environment)

```
python scripts/social_aggregate.py --workers 2
python scripts/social_panel.py
python scripts/social_train_baselines.py
python scripts/social_train_sequence.py --arch gru --seed 42
python scripts/social_train_sequence.py --arch gru --seed 43
python scripts/social_train_sequence.py --arch gru --seed 44
python scripts/social_train_sequence.py --arch transformer --seed 42
python scripts/social_train_sequence.py --arch transformer --seed 43
python scripts/social_train_sequence.py --arch transformer --seed 44
python scripts/social_summary.py
```

Farmers' protest tweets (needs `data/tweets/archive.zip`): `python scripts/run_farmers_pipeline.py` (add `--skip-steps transformer,transfer` without PyTorch). Per-script commands are in `README.md`.

`social_aggregate.py` is the slow step (the three files are about 93M lines). The sequence scripts need PyTorch. Logs go to `logs/<script name>/`, results to `results/`, models to `models/`.
