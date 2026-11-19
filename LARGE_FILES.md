# Files that are not in this repository

GitHub rejects any single file larger than 100 MiB, so these files stay on the author's machine. Everything else in the project folder is committed.

| Path | Size | What it is | How to get it back |
|---|---|---|---|
| `data/social/tweets_2014.json.gz` | ~621 MB | G-CUT raw tweet-score data, 2014 | download the G-CUT dataset again and place it here |
| `data/social/tweets_2015.json.gz` | ~791 MB | G-CUT raw data, 2015 | same |
| `data/social/tweets_2017.json.gz` | ~679 MB | G-CUT raw data, 2017 | same |
| `data/tweets/archive.zip` | ~331 MB | the #FarmersProtest tweet archive (Kaggle download) | download it again and place it here |
| `models/*/best/model.safetensors` (5 files) | ~328 MB each | fine-tuned DistilRoBERTa weights (ACLED report model, its masked variant, tweet transformers) | retrain with the pipeline in `README.md` (the configs, tokenizers and metrics of each run are in the repo) |

The processed data, labels, results, logs, scripts, notebooks and the web app (`app/`) are all included. Run the steps in `README.md` to rebuild the excluded files and models.
