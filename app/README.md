# Signal Room — web app for the civil-unrest project

A FastAPI backend that serves your **trained models and saved results**, and a TypeScript + Three.js frontend (pre-built in `frontend/dist`, so Node is not needed to use it).

## Run it (from the project root, `E:\ai\NLP`)

```powershell
pip install fastapi uvicorn
python app/backend/server.py
```

It opens http://127.0.0.1:8000. The first start takes 1–2 minutes (loads the transformers; retrains and caches the TF-IDF model once in `models/farmers_tfidf_app.joblib`). The pills at the top right show when the models and the tweet archive are ready.

## What each page does (all results come from your trained models / result files)

| Page | What it does |
|---|---|
| Analyze a post | Scores one post with every model, shows keyword-rule words, protest actions and states, and a word-by-word occlusion explanation |
| Scan a feed | Paste or upload many posts (optionally with dates): share flagged, score histogram, per-day signal ranked against all 386 archive days |
| Unrest terrain | 3D view of the 386-day archive; click a day to score its tweets live and compare with ACLED |
| Threshold lab | Moves the decision threshold over the 293-tweet gold sample; live precision / recall / F1, PR curve, confusion matrix |
| Evidence | Extraction vs ACLED, surge forecasting with confidence intervals, G-CUT results |

Models used: the newest `models/farmers_transformer_*/best`, the TF-IDF model retrained from `silver_train.csv`, and the ACLED report model. Thresholds for the two tweet models are tuned on the gold sample at startup.

## Develop the frontend (optional)

```powershell
cd app/frontend
npm install
npm run dev      # http://localhost:5173, proxies /api to port 8000
npm run build    # type-checks and rebuilds dist/
```

Options: `python app/backend/server.py --port 8001 --no-browser`.
