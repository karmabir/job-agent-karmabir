# job-agent

A personal job-search pipeline: find roles, score them against a profile, tailor a resume and cover letter,
pre-fill the application form, and track everything. It never clicks Submit; a human reviews and submits.

## Pipeline

| Step | Script | What it does |
|---|---|---|
| Fetch | `fetch_jobs.py` | Pulls postings from Greenhouse, Lever, Ashby, Workable and aggregators listed in `companies.yaml`, then applies the filters in `config.yaml` |
| Score | `score_jobs.py` | Scores new postings against your profile with Claude |
| Daily run | `daily.py` | Fetch, score and write a queue of the best matches |
| Tailor | `tailor.py` | Builds a one-page resume and cover letter from `profile.yaml`, with a fact-check pass so nothing is invented |
| Fill | `apply_assist.py` | Opens the form in a browser (Playwright), fills it from your profile, and leaves consents and commitments for you |
| Batch | `queue_run.py` | Launches a whole day's queue |
| Track | `tracker.py` | Keeps status per job (`new`, `applied`, `skip`) in SQLite and exports a CSV |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && playwright install chromium
cp .env.example .env                 # add ANTHROPIC_API_KEY
cp profile.example.yaml profile.yaml # fill in your own facts
```

## Usage

```bash
python daily.py --target 10 --min-score 60 --budget 6
python apply_assist.py <job id>
python tracker.py mark applied <job id>
python tracker.py summary
```

## Privacy

`profile.yaml`, `.env`, `data/` (job database) and `out/` (generated resumes) are gitignored and stay on your machine.
