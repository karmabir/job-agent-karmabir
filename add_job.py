#!/usr/bin/env python3
"""Add a job you found anywhere (LinkedIn, Indeed, Glassdoor, a company site) to the tracker.

  python add_job.py <url>                                   # Greenhouse/Lever/Ashby/other company pages are fetched
  python add_job.py <url> --paste --company X --title Y     # paste the description on stdin (Ctrl-D to finish)
  python add_job.py <url> --text-file jd.txt --company X --title Y
  add --score to score it right away (about $0.006)

LinkedIn, Indeed and Glassdoor pages are never fetched (their terms forbid automated access and they
block bots); for those you paste the text yourself. Then run: python tailor.py <printed job id>
"""
import argparse
import hashlib
import re
import sys
import time
from urllib.parse import urlparse

import httpx

from fetch_jobs import (
    CFG, HEADERS, classify_track, extract_salary, extract_years, hard_filters_ok, sponsorship, strip_html,
)
from common import db

NO_FETCH = ("linkedin.com", "indeed.com", "glassdoor.com")


def fetch_known(url: str):
    """Return (company, title, location, description) for Greenhouse/Lever/Ashby URLs, else None."""
    u = urlparse(url)
    parts = [p for p in u.path.split("/") if p]
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        if "greenhouse.io" in u.netloc and len(parts) >= 3 and parts[1] == "jobs":
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[2]}")
            if r.status_code == 200:
                j = r.json()
                return parts[0], j["title"], (j.get("location") or {}).get("name", ""), strip_html(j.get("content", ""))
        if u.netloc == "jobs.lever.co" and len(parts) >= 2:
            r = c.get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}")
            if r.status_code == 200:
                j = r.json()
                return parts[0], j["text"], (j.get("categories") or {}).get("location", ""), j.get("descriptionPlain", "")
        if u.netloc == "jobs.ashbyhq.com" and len(parts) >= 2:
            r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}?includeCompensation=true")
            if r.status_code == 200:
                for j in r.json().get("jobs", []):
                    if j["id"] == parts[1]:
                        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
                        return parts[0], j["title"], j.get("location", ""), (j.get("descriptionPlain") or "") + " " + comp
    return None


def fetch_generic(url: str):
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        r = c.get(url)
    if r.status_code != 200:
        raise SystemExit(f"could not fetch page (HTTP {r.status_code}); use --paste instead")
    title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S | re.I)
    return urlparse(url).netloc, strip_html(title.group(1)) if title else "", "", strip_html(r.text)[:15000]


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("url")
    p.add_argument("--paste", action="store_true", help="read the job description from stdin")
    p.add_argument("--text-file")
    p.add_argument("--company")
    p.add_argument("--title")
    p.add_argument("--location", default="")
    p.add_argument("--score", action="store_true")
    a = p.parse_args()

    host = urlparse(a.url).netloc.lower()
    blocked = any(h in host for h in NO_FETCH)
    company = title = location = desc = None

    if a.paste:
        desc = sys.stdin.read()
    elif a.text_file:
        desc = open(a.text_file).read()
    elif blocked:
        raise SystemExit(f"{host} is never fetched automatically. Paste the description: add --paste "
                         "(then paste and press Ctrl-D) or --text-file jd.txt, plus --company and --title.")
    else:
        known = fetch_known(a.url) or fetch_generic(a.url)
        company, title, location, desc = known

    company = a.company or company
    title = a.title or title
    location = a.location or location or ""
    if not (company and title and desc and desc.strip()):
        raise SystemExit("need a company, a title and a description (use --company, --title, and --paste/--text-file)")

    lo, hi = extract_salary(desc)
    yrs = extract_years(desc)
    jid = "manual:" + hashlib.sha1((a.url or desc).encode()).hexdigest()[:12]
    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO jobs (id, source, company, title, location, url, description, posted_at, first_seen,"
        " track, sponsorship, status, salary_min, salary_max, years_req) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (jid, "manual", company, title, location, a.url, desc, "", time.time(), classify_track(title) or "ml_eng",
         sponsorship(desc), "new", lo, hi, yrs),
    )
    conn.commit()
    pay = f"${lo // 1000}-{hi // 1000}K" if lo else "pay not listed"
    print(f"added {jid}: {company} - {title} | {pay} | {yrs}+ yrs" if yrs is not None else
          f"added {jid}: {company} - {title} | {pay} | yrs not stated")
    if not hard_filters_ok(title, hi, yrs):
        print("NOTE: this fails your filters (pay under $100K, more than 3 years required, or a senior title). Added anyway.")
    if a.score:
        from score_jobs import score_one
        import json
        from common import usage_report
        _, res = score_one((jid, company, title, location, desc))
        if "error" in res:
            print("scoring failed:", res["error"])
        else:
            conn.execute("UPDATE jobs SET score=?, track=?, sponsorship=?, notes=? WHERE id=?",
                         (int(res["score"]), res.get("track"), res.get("sponsorship"), json.dumps(res), jid))
            conn.commit()
            print(f"score {res['score']}: {res.get('reason', '')}\nflags: {'; '.join(res.get('red_flags', []))}\n{usage_report()}")
    print(f"next: python tailor.py {jid}")


if __name__ == "__main__":
    main()
