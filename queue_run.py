#!/usr/bin/env python3
"""Launch a batch of applications at once instead of one at a time.

  python queue_run.py                       # everything in today's out/queue_<date>.md
  python queue_run.py --date 2026-09-20      # a specific day's queue
  python queue_run.py <job_id> <job_id> ...  # an explicit list instead of a queue file
  python queue_run.py --limit 5              # cap how many to open in one batch

For each job: generates the resume/cover letter if missing, then opens the application.
Ashby and Lever reject the automated fill-in browser (flagged as spam in testing), so those
open via --manual: a paste-sheet is printed and the form opens in your default browser for you
to fill by hand. Everything else (Greenhouse, Workable, ...) opens in a controlled browser window,
pre-filled, and stays open for you to review and submit - all launched together, not one-by-one.

When you're done, mark them in one shot:
  python tracker.py mark applied <job_id> <job_id> ...
  python tracker.py mark skip <job_id> ...
"""
import argparse
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import ROOT, db

PY = sys.executable
MANUAL_SOURCES = {"ashby", "lever"}
# These aggregators list a company's posting but not its real application form; apply_assist would
# open the aggregator page itself (often blocked by a bot check) rather than the actual ATS. These
# need the "find the real application page" step done by hand first (see add_job.py --paste).
NEEDS_RESEARCH_SOURCES = {"himalayas", "remoteok", "remotive", "arbeitnow"}


def slug_of(company, title):
    return re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:80]


def ids_from_queue(date: str) -> list[str]:
    path = ROOT / "out" / f"queue_{date}.md"
    if not path.exists():
        raise SystemExit(f"no queue file at {path}")
    ids = re.findall(r"id:\s*`([^`]+)`", path.read_text())
    if not ids:
        raise SystemExit(f"no job ids found in {path}")
    return ids


def main():
    import datetime
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("job_ids", nargs="*", help="explicit job ids; if omitted, reads today's (or --date's) queue file")
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    ap.add_argument("--limit", type=int, default=10)
    a = ap.parse_args()

    ids = a.job_ids or ids_from_queue(a.date)
    ids = ids[: a.limit]
    conn = db()
    print(f"launching {len(ids)} application(s)...\n")

    todo, skipped_status = [], []
    for jid in ids:
        row = conn.execute("SELECT source, company, title, status, salary_min, salary_max FROM jobs WHERE id=?", (jid,)).fetchone()
        if not row:
            print(f"  SKIP {jid}: not found in database")
            continue
        source, company, title, status, lo, hi = row
        if status in ("applied", "interview", "offer", "skip", "rejected"):
            skipped_status.append((jid, company, title, status))
            continue
        if source in NEEDS_RESEARCH_SOURCES:
            print(f"  NEEDS RESEARCH {jid}: {company} - {title} ({source} listing, not a direct ATS link - "
                  f"find the real application page first, see out/{slug_of(company, title)}/ once prepared)")
            continue
        todo.append((jid, source, company, title, lo, hi))

    def prepare(item):
        jid, _source, company, title, _lo, _hi = item
        folder = ROOT / "out" / slug_of(company, title)
        if (folder / "resume.pdf").exists():
            return jid, True, ""
        r = subprocess.run([PY, "tailor.py", jid], cwd=ROOT, capture_output=True, text=True)
        return jid, (r.returncode == 0 and (folder / "resume.pdf").exists()), r.stderr.strip()[-300:]

    need = [i for i in todo if not (ROOT / "out" / slug_of(i[2], i[3]) / "resume.pdf").exists()]
    if need:
        print(f"  preparing {len(need)} resume/cover letter set(s) in parallel ...")
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = {jid: (ok, err) for jid, ok, err in ex.map(prepare, todo)}

    launched, procs = [], []
    for jid, source, company, title, lo, hi in todo:
        ok, err = results[jid]
        if not ok:
            print(f"  FAILED to prepare {jid}: {err}")
            continue
        pay = f"${lo // 1000}-{hi // 1000}K" if lo else "pay not listed"
        mode = "--manual" if source in MANUAL_SOURCES else "(window)"
        print(f"  opening {company} - {title} ({pay}) [{mode}]  id={jid}")
        args = [PY, "apply_assist.py", jid] + (["--manual"] if source in MANUAL_SOURCES else [])
        if procs:
            time.sleep(6)  # let the previous page finish loading before launching the next browser
        procs.append(subprocess.Popen(args, cwd=ROOT))
        launched.append(jid)

    print(f"\n{len(launched)} launched, {len(skipped_status)} already resolved.")
    if skipped_status:
        for jid, company, title, status in skipped_status:
            print(f"  already {status}: {company} - {title}")
    if launched:
        print("\nReview each window, solve any captcha, and click Submit yourself.")
        print("Window jobs mark themselves applied when the confirmation page appears.")
        print("Ashby/Lever (--manual) jobs are not auto-detected; mark those yourself:")
        print(f"  python tracker.py mark applied <job_id> ...   (use 'skip' for any you passed on)")
        for p in procs:
            p.wait()


if __name__ == "__main__":
    main()
