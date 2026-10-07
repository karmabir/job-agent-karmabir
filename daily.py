#!/usr/bin/env python3
"""Daily pipeline: fetch -> score new -> prepare application packages for the best matches -> write a queue.

  python daily.py --target 40 --min-score 60 --budget 6          # normal
  python daily.py --target 40 --cheap                             # Haiku writes the letters (about 5x cheaper)
  python daily.py --dry-run                                       # show who would be prepared, spend nothing

You then open each item in out/queue_<date>.md, fill the form (Simplify autofill helps), attach the files,
review, and click Submit yourself. Mark it: python tracker.py set <job id> applied
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import ROOT, db

PY = sys.executable
import yaml
EXCLUDE = {x.lower() for x in (yaml.safe_load((ROOT / "config.yaml").read_text()).get("exclude_companies") or [])}


def run(cmd, env=None):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, **(env or {})})


RESEARCH = re.compile(r"research (scientist|engineer)|post-?training|pre-?training|alignment|frontier|research resident", re.I)


def base_title(t):
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", t)).strip().lower()


def candidates(min_score, limit, per_company, include_research=False):
    """Best-scoring postings first; at most per_company applications per company (already-applied ones count),
    no duplicate listings of the same role, and no frontier-lab research roles unless asked."""
    from collections import Counter
    conn = db()
    rows = conn.execute(
        "SELECT id, company, title, location, score, sponsorship, salary_min, salary_max, url, notes FROM jobs "
        "WHERE status='new' AND score >= ? AND sponsorship != 'blocked' "
        "ORDER BY score DESC, CASE sponsorship WHEN 'friendly' THEN 0 ELSE 1 END", (min_score,)).fetchall()
    counts = Counter(r[0] for r in conn.execute("SELECT company FROM jobs WHERE status IN ('applied','interview','offer')"))
    seen = {(r[0], base_title(r[1])) for r in conn.execute(
        "SELECT company, title FROM jobs WHERE status IN ('applied','interview','offer','prepared','skip')")}
    picked = []
    for r in rows:
        key = (r[1], base_title(r[2]))
        if key in seen or counts[r[1]] >= per_company or r[1].lower() in EXCLUDE:
            continue
        if not include_research and RESEARCH.search(r[2]):
            continue
        seen.add(key)
        counts[r[1]] += 1
        picked.append(r)
        if len(picked) >= limit:
            break
    return picked


def slug_of(company, title):
    return re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:80]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", type=int, default=40)
    p.add_argument("--min-score", type=int, default=60)
    p.add_argument("--budget", type=float, default=6.0, help="stop preparing once estimated API spend passes this (USD)")
    p.add_argument("--per-company", type=int, default=99, help="max applications per company in total (default: effectively no cap)")
    p.add_argument("--prepare", action="store_true", help="also generate resume/cover letter now (default: prepared when you start applying)")
    p.add_argument("--include-research", action="store_true", help="also queue frontier-lab research roles")
    p.add_argument("--cheap", action="store_true", help="use Haiku for resume/letter writing")
    p.add_argument("--no-fetch", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()

    spent = 0.0
    if not a.dry_run:
        if not a.no_fetch:
            print("fetching...", run([PY, "fetch_jobs.py", "fetch"]).stdout.strip().splitlines()[-1])
        r = run([PY, "score_jobs.py", "--limit", "200"])
        m = re.search(r"est\. cost=\$([\d.]+)", r.stdout)
        spent += float(m.group(1)) if m else 0
        print("scoring:", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "nothing to score")

    picks = candidates(a.min_score, a.target, a.per_company, a.include_research)
    print(f"{len(picks)} candidates at score >= {a.min_score} (target {a.target})")
    if a.dry_run:
        for r in picks:
            pay = f"${r[6] // 1000}-{r[7] // 1000}K" if r[6] else "pay n/a"
            print(f"  {r[4]:3d} [{r[5]:8s}] {r[1]} - {r[2]} ({pay})")
        return

    env = {"TAILOR_CHEAP": "1"} if a.cheap else {}

    def prep(r):
        res = run([PY, "tailor.py", r[0]], env)
        m = re.search(r"est\. cost=\$([\d.]+)", res.stdout)
        return r, res.returncode == 0 and "wrote" in res.stdout, float(m.group(1)) if m else 0.0, res.stdout

    done, failed = [], []
    if not a.prepare:
        done = [(r, False) for r in picks]
        print("queued without preparing files; resume and cover letter are generated when you start each application")
    else:
        with ThreadPoolExecutor(max_workers=3) as ex:
            for i in range(0, len(picks), 3):
                if spent >= a.budget:
                    print(f"budget ${a.budget:.2f} reached after {len(done)} packages")
                    break
                for r, ok, cost, out in ex.map(prep, picks[i:i + 3]):
                    spent += cost
                    (done if ok else failed).append((r, "WARNING" in out))
                    print(f"  {'ok  ' if ok else 'FAIL'} {r[1]} - {r[2]}  (${cost:.2f})")

    today = datetime.date.today().isoformat()
    q = ROOT / "out" / f"queue_{today}.md"
    lines = [f"# Application queue {today}", "", f"{len(done)} queued, estimated API spend ${spent:.2f}", ""]
    for r, warn in done:
        pay = f"${r[6] // 1000}-{r[7] // 1000}K" if r[6] else "pay not listed"
        n = json.loads(r[9] or "{}")
        lines += [f"- [ ] **{r[1]} - {r[2]}** (score {r[4]}, {pay}, sponsorship: {r[5]})",
                  f"  - apply: {r[8]}", f"  - files: out/{slug_of(r[1], r[2])}/",
                  f"  - id: `{r[0]}`" + ("  **fact-check warning: read notes.txt**" if warn else "")]
        if n.get("red_flags"):
            lines.append(f"  - flags: {'; '.join(n['red_flags'])[:200]}")
    q.write_text("\n".join(lines) + "\n")
    print(f"\nqueue: {q}\nestimated spend today: ${spent:.2f}")


if __name__ == "__main__":
    main()
