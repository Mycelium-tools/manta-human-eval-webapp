# MANTA Scenario Review — Deployment Guide

## Project structure

```
manta-review/
├── backend/
│   ├── main.py           # FastAPI app
│   └── requirements.txt
└── frontend/
    └── index.html        # Self-contained review form
```

---

## Option A: Deploy on a single server (recommended)

### 1. Set up the backend

```bash
cd backend
pip install -r requirements.txt
```

Set environment variables for email sending (Gmail shown; any SMTP works):

```bash
export SMTP_HOST=smtp.gmail.com
export SMTP_PORT=587
export SMTP_USER=your.address@gmail.com
export SMTP_PASS=your_app_password   # Gmail: create at myaccount.google.com/apppasswords
```

> **Gmail note:** You need to enable 2FA and generate an App Password at
> https://myaccount.google.com/apppasswords — do not use your regular Gmail password.

Start the server:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 2. Serve the frontend

Update `API_URL` in `frontend/index.html` if your backend is on a different host:

```js
// near bottom of the <script> block:
const API_URL = "https://your-server.com";   // or leave "" for same-origin
```

Then serve the frontend from the same server (nginx, caddy, or even Python):

```bash
cd frontend
python3 -m http.server 80
```

Or configure nginx to serve `frontend/` as the root and proxy `/submit` and
`/scenarios` to `localhost:8000`.

---

## Option B: Render.com (free tier, easiest cloud deploy)

1. Push this repo to GitHub.
2. Go to https://render.com → New → Web Service → connect your repo.
3. Set:
   - **Root directory**: `backend`
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `uvicorn main:app --host 0.0.0.0 --port $PORT`
4. Add environment variables (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`) in the Render dashboard.
5. For the frontend, create a second Render service as a **Static Site** pointing to `frontend/`.
6. Update `API_URL` in `index.html` to point to your backend Render URL.

---

## Option C: Railway.app

Similar to Render. One service for backend, deploy `frontend/index.html`
via Railway's static hosting or any CDN (Netlify, Vercel, Cloudflare Pages).

---

## Sharing with reviewers

Once deployed, send reviewers the URL to `index.html`.
Each submission triggers an email to **Allenlu0007@gmail.com** with:
- Summary stats (pass / minor / major / remove counts)
- Full table of all 40 responses with scores, flags, and notes

---

## Computing inter-rater agreement

Once you have 2+ reviewers, run Fleiss' kappa on the `verdict` column:

```python
import pandas as pd
from statsmodels.stats.inter_rater import fleiss_kappa, aggregate_raters

# Load responses from emails or export to CSV first
# Then:
ratings = pd.DataFrame(...)   # rows = scenarios, cols = reviewers, values = verdict
table, _ = aggregate_raters(ratings.values)
kappa = fleiss_kappa(table)
print(f"Fleiss' kappa: {kappa:.3f}")
# Target: κ > 0.6
```
