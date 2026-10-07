#!/usr/bin/env python3
"""Score unscored postings against profile.yaml.

Usage: python score_jobs.py [--limit 50] [--include-blocked]
"""
import argparse
import json
from concurrent.futures import ThreadPoolExecutor

from common import FAST_MODEL, ask_json, db, profile_text, usage_report

SYSTEM = """You screen job postings for one candidate. Be honest and calibrated; do not flatter.
The candidate's work-authorization needs, education and experience level are in PROFILE
(work_authorization, experience, education). Return ONLY JSON:
{"score": 0-100 overall fit,
 "track": "ai_eng" | "ml_eng" | "ds",
 "reason": "one or two sentences",
 "matches": ["top 3 candidate strengths relevant to this role"],
 "gaps": ["real gaps versus the requirements"],
 "red_flags": ["e.g. requires PhD, 5+ years required, clearance, non-US location"]}
Hard requirements: the candidate only wants roles paying $100K or more and requiring at most 3 years of
experience. If the posting lists pay whose top is under $100K, or requires more than 3 years, score <= 30 and
flag it. Mention in red_flags when pay is not listed. Score 80+ only if the role is realistic for ~3 years experience and the core requirements match.
The candidate needs an H-1B transfer, which is routine and not a disqualifier by itself - never lower the
score or add a red flag for this alone unless the posting itself explicitly states it will not sponsor visas.
Do not classify or comment on sponsorship availability; that is determined separately from the posting's
own text, not from your judgment."""


def score_one(row):
    jid, company, title, location, description = row
    user = (
        f"CANDIDATE PROFILE:\n{profile_text()}\n\nPOSTING:\n{company} - {title} ({location})\n"
        f"{description[:7000]}"
    )
    try:
        return jid, ask_json(FAST_MODEL, SYSTEM, user, max_tokens=600)
    except Exception as e:
        return jid, {"error": str(e)}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--include-blocked", action="store_true")
    args = p.parse_args()

    conn = db()
    q = "SELECT id, company, title, location, description FROM jobs WHERE score IS NULL AND status = 'new'"
    if not args.include_blocked:
        q += " AND sponsorship != 'blocked'"
    q += " ORDER BY CASE sponsorship WHEN 'friendly' THEN 0 ELSE 1 END, first_seen DESC LIMIT ?"
    rows = conn.execute(q, (args.limit,)).fetchall()
    print(f"scoring {len(rows)} postings...")

    with ThreadPoolExecutor(max_workers=4) as ex:
        for jid, res in ex.map(score_one, rows):
            if "error" in res:
                print(f"  fail {jid}: {res['error']}")
                continue
            # sponsorship is NOT set here: it's determined once, reliably, from the posting's own text
            # at fetch time (fetch_jobs.py's sponsorship()); the model was inconsistent about it and
            # defaulted to "blocked" for postings that never even mentioned sponsorship.
            conn.execute(
                "UPDATE jobs SET score=?, track=?, notes=? WHERE id=?",
                (int(res["score"]), res.get("track"), json.dumps(res), jid),
            )
    conn.commit()
    print(usage_report())


if __name__ == "__main__":
    main()
