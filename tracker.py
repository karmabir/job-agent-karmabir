#!/usr/bin/env python3
"""Track application status. You do the actual submitting.

  python tracker.py top [--min 60] [--track ml_eng]   ranked scored postings
  python tracker.py set <job_id> <status> [note]      statuses: new shortlisted prepared applied interview offer rejected skip
  python tracker.py mark <status> <job_id> [job_id...]  set several jobs to one status in one call
  python tracker.py open <job_id>                     open posting URL + prepared folder
  python tracker.py summary                           counts by status
  python tracker.py export                            write data/applications.csv
"""
import argparse
import csv
import json
import re
import subprocess

from common import ROOT, db

STATUSES = ["new", "shortlisted", "prepared", "applied", "interview", "offer", "rejected", "skip"]


def cmd_top(a):
    q = "SELECT id, company, title, location, track, sponsorship, score, notes, status, salary_min, salary_max, years_req FROM jobs WHERE score >= ?"
    params = [a.min]
    if a.track:
        q += " AND track=?"
        params.append(a.track)
    q += " AND status NOT IN ('skip','rejected','filtered') ORDER BY score DESC LIMIT ?"
    params.append(a.limit)
    for jid, company, title, loc, track, sp, score, notes, status, lo, hi, yrs in db().execute(q, params):
        n = json.loads(notes or "{}")
        print(f"{score:3d} [{track}] [{sp}] {company} - {title} ({loc}) <{status}>")
        pay = f"${lo // 1000}-{hi // 1000}K" if lo else "pay not listed"
        exp = f"{yrs}+ yrs" if yrs is not None else "yrs not stated"
        print(f"     {pay} | {exp}")
        print(f"     {n.get('reason', '')}")
        if n.get("red_flags"):
            print(f"     flags: {'; '.join(n['red_flags'])}")
        print(f"     id={jid}")


def cmd_set(a):
    if a.status not in STATUSES:
        raise SystemExit(f"status must be one of {STATUSES}")
    conn = db()
    cur = conn.execute("UPDATE jobs SET status=? WHERE id=?", (a.status, a.job_id))
    if cur.rowcount == 0:
        raise SystemExit(f"no job {a.job_id}")
    conn.commit()
    print(f"{a.job_id} -> {a.status}")


def cmd_mark(a):
    if a.status not in STATUSES:
        raise SystemExit(f"status must be one of {STATUSES}")
    conn = db()
    ok, missing = [], []
    for jid in a.job_ids:
        cur = conn.execute("UPDATE jobs SET status=? WHERE id=?", (a.status, jid))
        (ok if cur.rowcount else missing).append(jid)
    conn.commit()
    print(f"{len(ok)} -> {a.status}: {', '.join(ok) if ok else '(none)'}")
    if missing:
        print(f"not found, skipped: {', '.join(missing)}")


def cmd_open(a):
    row = db().execute("SELECT company, title, url FROM jobs WHERE id=?", (a.job_id,)).fetchone()
    if not row:
        raise SystemExit(f"no job {a.job_id}")
    company, title, url = row
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:80]
    folder = ROOT / "out" / slug
    subprocess.run(["open", url])
    if folder.exists():
        subprocess.run(["open", str(folder)])
    else:
        print(f"no prepared folder yet; run: python tailor.py {a.job_id}")


def cmd_summary(_a):
    for status, n in db().execute("SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY 2 DESC"):
        print(f"{status:12s} {n}")


def cmd_export(_a):
    path = ROOT / "data" / "applications.csv"
    rows = db().execute(
        "SELECT company, title, location, track, sponsorship, score, status, url FROM jobs "
        "WHERE status NOT IN ('new') OR score IS NOT NULL ORDER BY score DESC"
    ).fetchall()
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "title", "location", "track", "sponsorship", "score", "status", "url"])
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} rows)")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("top")
    t.add_argument("--min", type=int, default=60)
    t.add_argument("--limit", type=int, default=25)
    t.add_argument("--track", choices=["ai_eng", "ml_eng", "ds"])
    t.set_defaults(fn=cmd_top)
    s = sub.add_parser("set")
    s.add_argument("job_id")
    s.add_argument("status")
    s.set_defaults(fn=cmd_set)
    m = sub.add_parser("mark")
    m.add_argument("status")
    m.add_argument("job_ids", nargs="+")
    m.set_defaults(fn=cmd_mark)
    o = sub.add_parser("open")
    o.add_argument("job_id")
    o.set_defaults(fn=cmd_open)
    sub.add_parser("summary").set_defaults(fn=cmd_summary)
    sub.add_parser("export").set_defaults(fn=cmd_export)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
