# Civil Unrest Event Extraction and Prediction from Social Media

Course project (Bennett University, NLP). Goal: detect civil-unrest events in social-media text and predict unrest surges, evaluated
against an event database (ACLED). This repository holds three experiments that share one set of utilities (`scripts/common.py`).

| Part | Data | Task | Status |
|---|---|---|---|
| A. ACLED text | ACLED India 2018-2025 event notes | classify violent vs non-violent events (TF-IDF, DistilRoBERTa) | done |
| B. G-CUT signals | 93M tweets (2014, 2015, 2017), scores only, no text | forecast surges in unrest-related tweet volume | done |
| C. #FarmersProtest | 1.08M tweets with text (Nov 2020 - Nov 2021) linked to ACLED by day | tweet violence detection, event/place extraction vs ACLED, surge prediction | done (this README's main pipeline) |

Progress and findings: `PROGRESS.md`. Per-part notebooks: `notebooks/01_data_exploration.ipynb`, `02_social_data_analysis.ipynb`,
`03_farmers_protest_analysis.ipynb`. Every script writes a timestamped log to `logs/<script name>/`, results to `results/`, models to `models/`.

## Folder layout

```
data/
  ACLED Data_*.csv            ACLED India export
  tweets/archive.zip          #FarmersProtest tweets (tweets.csv, users.csv)
  social/*.json.gz            G-CUT style tweet scores (2014, 2015, 2017)
  processed/, social_processed/, farmers_processed/, farmers_labels/   generated
scripts/                      all code (run from the project root)
notebooks/  logs/  models/  results/
```

## Environment

Use the base conda environment (Python 3.10+, pandas, scikit-learn, matplotlib, scipy). The transformer steps also need PyTorch and
`transformers` (a GPU makes them much faster; the CPU works for tests). Run every command from the project root (`E:\ai\NLP`).

## Part C: the #FarmersProtest pipeline (main commands)

Everything in one command (needs `data/tweets/archive.zip` and the ACLED csv):

```
python scripts/run_farmers_pipeline.py
```

Useful variants:

```
python scripts/run_farmers_pipeline.py --skip-steps transformer,transfer        # no PyTorch / no GPU
python scripts/run_farmers_pipeline.py --steps label,tfidf,extract,forecast,summary
python scripts/run_farmers_pipeline.py --epochs 3 --base-model cardiffnlp/twitter-roberta-base   # tweet-pretrained model
python scripts/run_farmers_pipeline.py --max-train 4000 --max-eval 2000 --epochs 1 --skip-steps prepare   # quick test
```

Or run the steps yourself, in this order:

```
python scripts/farmers_prepare.py                    # daily tweet table, ACLED daily table, cleaned text (about 2 min)
python scripts/farmers_label_weak.py                 # weak labels (train/val/test by date) + rule score on the gold sample
python scripts/farmers_train_tweets.py --model tfidf --score-daily
python scripts/farmers_train_tweets.py --model transformer --epochs 2 --cue-dropout 0.5 --score-daily     # GPU recommended
python scripts/farmers_extract.py                    # event types and places vs ACLED (about 2 min)
python scripts/farmers_transfer.py --model-dir models/<your ACLED distilroberta run>     # ACLED-trained model on tweets
python scripts/farmers_transfer_eval.py --run results/farmers_transfer_<runid> --labels data/farmers_labels/gold_claude.csv
python scripts/farmers_forecast.py --signals results/farmers_tweets_<runid>/daily_signals_transformer.csv
python scripts/farmers_gold_threshold.py             # re-tune each classifier's threshold on gold folds (cross-validated)
python scripts/farmers_summary.py                    # -> results/farmers_summary/summary.md
```

What each step does:

| Script | Purpose | Main outputs |
|---|---|---|
| `farmers_prepare.py` | read the zip, build daily tweet features and daily ACLED counts (farmer-related, violent, riots, surge days) | `data/farmers_processed/` |
| `farmers_label_weak.py` | rule-based weak labels: 1 = strong physical-violence term, 0 = no violence term; chronological split | `data/farmers_labels/silver_*.csv` |
| `farmers_train_tweets.py` | TF-IDF or DistilRoBERTa on the weak labels; evaluated on silver val/test and on the gold sample; optional daily signals | `results/farmers_tweets_*`, `models/farmers_*` |
| `farmers_extract.py` | event-type and place extraction from tweets, cross-checked with ACLED by day and by state | `results/farmers_extract_*` |
| `farmers_transfer.py`, `farmers_transfer_eval.py` | score tweets with the ACLED-trained model; precision/recall on the gold sample | `results/farmers_transfer_*` |
| `farmers_forecast.py` | same-day detection and 1/3/7-day forecasts of ACLED surges; tweet and classifier features vs ACLED history | `results/farmers_forecast_*` |
| `farmers_gold_threshold.py` | threshold tuned on silver data does not suit the real tweet mix; re-tune on gold folds and score the held-out part | `results/farmers_gold_threshold/` |
| `farmers_summary.py` | one Markdown report from the latest runs | `results/farmers_summary/summary.md` |

### The gold sample (please review it)

`data/farmers_labels/gold_claude.csv` has 293 tweets (stratified by the ACLED-model score) labelled by Claude as one annotator:
`label = 1` if the tweet describes, reports or incites a specific violent act or clash (beating, attack, killing, tear gas, riot),
`0` otherwise (including general political rhetoric). **It has not been checked by a human.** Open the file, correct the `label`
column where you disagree, then re-run `farmers_label_weak.py`, `farmers_train_tweets.py`, `farmers_transfer_eval.py` and
`farmers_summary.py` so every number uses your labels. The `weight` column undoes the stratified sampling; leave it as is.
For a stronger evaluation, label more tweets (a fresh sheet can be drawn with `farmers_transfer.py`) or have a second annotator.

### Test on unseen data (your own labels)

The gold sample was also used to re-tune thresholds, so for a clean accuracy check label a fresh sample the models have never seen:

```
python scripts/farmers_make_unseen.py            # draws 300 fresh tweets from Oct-Nov 2021 -> data/farmers_unseen/unseen_to_label.csv
# open unseen_to_label.csv, fill the `label` column (1 = violent, 0 = not, blank = skip), save as CSV
python scripts/farmers_eval_unseen.py            # scores rule, TF-IDF and the latest DistilRoBERTa -> results/farmers_unseen_<runid>/summary.md
```

Thresholds are fixed before your labels are read; metrics are weighted to the whole tweet population with 95% intervals. Use `--skip-transformer` without PyTorch.

### How to read the results

- The weak labels come from keyword rules. Scores on `silver_*` measure agreement with the rules; only the gold sample says how well a model does on real tweets.
- The rule's own gold score is optimistic because its word list was written while reading the gold sample.
- Surges are days when ACLED farmer-related events spike (z >= 2 against the previous 28 days, at least 15 events); only 15 such days exist, so forecast intervals are wide.
- ACLED "farmer-related" and the keyword/place lists are heuristics (see `scripts/farmers_common.py` and `farmers_prepare.py`).

## Part A: ACLED text pipeline

```
python scripts/preprocess.py
python scripts/train_baseline.py                       # TF-IDF + logistic regression (binary)
python scripts/train_baseline.py --task 3class
python scripts/train_transformer.py                    # DistilRoBERTa, 3 epochs
python scripts/train_transformer.py --mask-cues        # leakage experiment (hide label-revealing words)
python scripts/evaluate.py --model-dir models/<run_name> --tune-threshold
```

## Part B: G-CUT tweet-signal pipeline

```
python scripts/social_aggregate.py --workers 2         # slow: streams 93M tweets
python scripts/social_panel.py
python scripts/social_train_baselines.py
python scripts/social_train_sequence.py --arch gru --seed 42
python scripts/social_train_sequence.py --arch transformer --seed 42
python scripts/social_summary.py
```
