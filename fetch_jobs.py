#!/usr/bin/env python3
"""Discover AI/ML/DS job postings from Greenhouse, Lever and aggregator APIs.

Usage:
  python fetch_jobs.py fetch          # pull, filter, store new postings
  python fetch_jobs.py check          # test which company slugs respond
  python fetch_jobs.py list [--all] [--track ml_eng] [--sponsor friendly]
"""
import argparse
import html
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "jobs.db"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text())
COMPANIES = yaml.safe_load((ROOT / "companies.yaml").read_text())
HEADERS = {"User-Agent": CFG["user_agent"]}

US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
US_RE = re.compile(
    r"united states|\busa\b|\bu\.s\.|\bus\b|(?:,\s*|\s)(?:%s)\b|new york|san francisco|seattle|austin|boston|"
    r"chicago|los angeles|denver|atlanta|washington|california|texas|nyc|bay area|palo alto|mountain view"
    % "|".join(US_STATES)
)
NON_US_RE = re.compile(
    r"london|\buk\b|united kingdom|canada|toronto|vancouver|india|bangalore|bengaluru|germany|berlin|munich|"
    r"france|paris|spain|madrid|barcelona|ireland|dublin|netherlands|amsterdam|poland|warsaw|israel|tel aviv|"
    r"singapore|australia|sydney|japan|tokyo|brazil|mexico|europe|emea|apac|latam|dach|portugal|lisbon|"
    r"sweden|stockholm|switzerland|zurich|china|korea|seoul|dubai|uae|philippines|argentina|colombia",
    re.I,
)

SPONSOR_BLOCKED = re.compile(
    r"without (?:the need for )?(?:visa |employer |current or future )?sponsorship|"
    r"(?:not|n't|unable to|cannot|can't) (?:be able to |currently )?(?:offer|provide|sponsor)[^.]{0,40}(?:visa|sponsorship|work authorization)|"
    r"(?:no|not offer(?:ing)?) (?:visa )?sponsorship|will not sponsor|unable to sponsor|"
    r"do(?:es)? not (?:offer|provide) (?:visa )?sponsorship|"
    r"(?:now or )?in the future[^.]{0,30}sponsorship|"
    r"sponsorship (?:is )?not (?:available|offered|provided)|must be (?:a )?(?:u\.?s\.? )?(?:citizen|permanent resident)|"
    r"u\.?s\.? citizens? (?:only|or)|security clearance",
    re.I,
)
SPONSOR_FRIENDLY = re.compile(
    r"visa sponsorship (?:is )?(?:available|provided|offered)|(?:will|can|do|does) sponsor|we sponsor|"
    r"h-?1b (?:transfer|sponsorship)|sponsorship (?:is )?(?:available|provided|offered)|"
    r"(?:provide|offer)s? (?:visa )?sponsorship|open to sponsor|immigration (?:support|assistance)",
    re.I,
)


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def classify_track(title: str) -> str | None:
    t = title.lower()
    for track, kws in CFG["tracks"].items():
        if any(k in t for k in kws):
            return track
    return None


def title_ok(title: str) -> bool:
    t = title.lower()
    if any(x in t for x in CFG["exclude_title"]):
        return False
    if CFG["exclude_senior"] and re.search(r"\b(senior|sr\.?|lead|iii|iv)(?=\W|$)", t):
        return False
    return classify_track(title) is not None


def location_ok(location: str, description: str = "") -> bool:
    """US-only filter. An empty/missing location field used to default to accept, which let at least one
    non-US posting (Paris/France, from Arbeitnow, which has no location metadata for some listings) slip
    through undetected. Scanning the free-text description as a fallback was tried and rejected: company
    "about us" blurbs routinely mention many countries as customers/markets/offices unrelated to where the
    role itself is based (e.g. "we serve customers across Europe and the US" incorrectly reads as a US
    signal), so it produces confident-looking wrong answers rather than admitting uncertainty. Since a
    blank location is rare (only ever seen from Arbeitnow, a EU-centric board) and every reliable source
    (Greenhouse/Lever/Ashby) always populates it, missing location is now treated as a reject, not a pass."""
    if not CFG["us_only"]:
        return True
    loc = (location or "").lower()
    if not loc:
        return False
    if US_RE.search(loc):
        return True
    if "remote" in loc and not NON_US_RE.search(loc):
        return True
    return False


def sponsorship(description: str) -> str:
    if SPONSOR_BLOCKED.search(description):
        return "blocked"
    if SPONSOR_FRIENDLY.search(description):
        return "friendly"
    return "unclear"


SALARY_RE = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])\b")
YEARS_RE = re.compile(r"(\d{1,2})\s*\+?\s*(?:(?:-|–|to)\s*\d{1,2}\s*\+?\s*)?years?", re.I)


def extract_salary(description: str):
    vals = []
    for m in SALARY_RE.finditer(description):
        raw = m.group(1).replace(",", "").replace(" ", "").lower()
        v = float(raw[:-1]) * 1000 if raw.endswith("k") else float(raw)
        if 40_000 <= v <= 700_000:
            vals.append(int(v))
    return (min(vals), max(vals)) if vals else (None, None)


def extract_years(description: str):
    """Smallest 'N years ... experience' figure in the posting, or None if not stated."""
    found = []
    for m in YEARS_RE.finditer(description):
        window = description[max(0, m.start() - 60): m.end() + 100].lower()
        if "experience" in window and int(m.group(1)) <= 15:
            found.append(int(m.group(1)))
    return min(found) if found else None


def hard_filters_ok(title, salary_max, years_req, location="", description="") -> bool:
    if not title_ok(title):
        return False
    if not location_ok(location, description):
        return False
    if salary_max is not None and salary_max < CFG["min_salary"]:
        return False
    if years_req is not None and years_req > CFG["max_years"]:
        return False
    return True


def get_json(client: httpx.Client, url: str):
    r = client.get(url, timeout=30)
    if r.status_code != 200:
        return None
    return r.json()


def fetch_greenhouse(slug: str):
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if data is None:
        return None
    return [
        dict(
            source="greenhouse", ext_id=str(j["id"]), company=slug, title=j["title"],
            location=(j.get("location") or {}).get("name", ""), url=j["absolute_url"],
            description=strip_html(j.get("content", "")), posted_at=j.get("updated_at", ""),
        )
        for j in data.get("jobs", [])
    ]


def fetch_lever(slug: str):
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if data is None:
        return None
    out = []
    for j in data:
        desc = (j.get("descriptionPlain") or "") + " " + " ".join(
            f"{l.get('text', '')} {strip_html(l.get('content', ''))}" for l in j.get("lists", [])
        ) + " " + (j.get("additionalPlain") or "")
        out.append(dict(
            source="lever", ext_id=j["id"], company=slug, title=j["text"],
            location=(j.get("categories") or {}).get("location", ""), url=j["hostedUrl"],
            description=desc.strip(), posted_at=str(j.get("createdAt", "")),
        ))
    return out


def fetch_remoteok():
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, "https://remoteok.com/api")
    if data is None:
        return None
    return [
        dict(
            source="remoteok", ext_id=str(j["id"]), company=j.get("company", ""), title=j.get("position", ""),
            location=j.get("location") or "Remote", url=j.get("url", ""),
            description=strip_html(j.get("description", "")), posted_at=j.get("date", ""),
        )
        for j in data if isinstance(j, dict) and j.get("id")
    ]


def fetch_arbeitnow():
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, "https://www.arbeitnow.com/api/job-board-api")
    if data is None:
        return None
    return [
        dict(
            source="arbeitnow", ext_id=j["slug"], company=j.get("company_name", ""), title=j.get("title", ""),
            location=("Remote " if j.get("remote") else "") + (j.get("location") or ""), url=j.get("url", ""),
            description=strip_html(j.get("description", "")), posted_at=str(j.get("created_at", "")),
        )
        for j in data.get("data", [])
    ]


def fetch_ashby(slug: str):
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if data is None:
        return None
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        loc = j.get("location", "") or ""
        if j.get("isRemote"):
            loc = f"Remote {loc}".strip()
        out.append(dict(
            source="ashby", ext_id=j["id"], company=slug, title=j["title"], location=loc,
            url=j.get("jobUrl") or j.get("applyUrl", ""),
            description=(j.get("descriptionPlain") or strip_html(j.get("descriptionHtml") or "")) + " " + comp,
            posted_at=j.get("publishedAt", ""),
        ))
    return out


def fetch_workable(slug: str):
    with httpx.Client(headers=HEADERS) as c:
        data = get_json(c, f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if data is None:
        return None
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc = f"Remote {loc}".strip()
        out.append(dict(
            source="workable", ext_id=j["shortcode"], company=data.get("name", slug), title=j["title"], location=loc,
            url=j.get("url", ""), description=strip_html(j.get("description", "")), posted_at=j.get("published_on", ""),
        ))
    return out


def fetch_himalayas():
    out = []
    with httpx.Client(headers=HEADERS) as c:
        for q in CFG.get("search_queries", []):
            for offset in (0, 20):
                data = get_json(c, f"https://himalayas.app/jobs/api/search?q={quote(q)}&country=US&offset={offset}")
                if not data:
                    break
                for j in data.get("jobs", []):
                    lo, hi = j.get("minSalary"), j.get("maxSalary")
                    if j.get("salaryPeriod") not in (None, "annual", "year"):
                        lo = hi = None
                    restr = j.get("locationRestrictions") or []
                    loc = "Remote " + (", ".join(restr) if restr else "Worldwide")
                    out.append(dict(
                        source="himalayas", ext_id=j["guid"], company=j.get("companyName", ""), title=j.get("title", ""),
                        location=loc, url=j.get("applicationLink") or j["guid"],
                        description=strip_html(j.get("description", "")), posted_at=str(j.get("pubDate", "")),
                        salary_min=int(lo) if lo else None, salary_max=int(hi) if hi else None,
                    ))
                time.sleep(1)
    return out


def fetch_remotive():
    out = []
    with httpx.Client(headers=HEADERS) as c:
        for q in CFG.get("search_queries", []):
            data = get_json(c, f"https://remotive.com/api/remote-jobs?search={quote(q)}")
            for j in (data or {}).get("jobs", []):
                out.append(dict(
                    source="remotive", ext_id=str(j["id"]), company=j.get("company_name", ""), title=j.get("title", ""),
                    location="Remote " + (j.get("candidate_required_location") or ""), url=j.get("url", ""),
                    description=strip_html(j.get("description", "")) + " " + (j.get("salary") or ""),
                    posted_at=j.get("publication_date", ""),
                ))
    return out


def collect_sources():
    tasks = [("greenhouse", s, fetch_greenhouse, s) for s in COMPANIES.get("greenhouse", [])]
    tasks += [("lever", s, fetch_lever, s) for s in COMPANIES.get("lever", [])]
    tasks += [("ashby", s, fetch_ashby, s) for s in COMPANIES.get("ashby", [])]
    tasks += [("workable", s, fetch_workable, s) for s in COMPANIES.get("workable", [])]
    agg = COMPANIES.get("aggregators", {})
    if agg.get("himalayas"):
        tasks.append(("himalayas", "himalayas", fetch_himalayas, None))
    if agg.get("remotive"):
        tasks.append(("remotive", "remotive", fetch_remotive, None))
    if agg.get("remoteok"):
        tasks.append(("remoteok", "remoteok", fetch_remoteok, None))
    if agg.get("arbeitnow"):
        tasks.append(("arbeitnow", "arbeitnow", fetch_arbeitnow, None))
    return tasks


def run_task(task):
    kind, name, fn, arg = task
    try:
        jobs = fn(arg) if arg else fn()
    except Exception as e:  # network/parse errors shouldn't kill the run
        return kind, name, None, str(e)
    return kind, name, jobs, None


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, source TEXT, company TEXT, title TEXT, location TEXT, url TEXT,
            description TEXT, posted_at TEXT, first_seen REAL, track TEXT, sponsorship TEXT,
            status TEXT DEFAULT 'new', score INTEGER, notes TEXT,
            salary_min INTEGER, salary_max INTEGER, years_req INTEGER)"""
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    for col in ("salary_min", "salary_max", "years_req"):
        if col not in cols:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} INTEGER")
    return conn


def refilter(conn):
    """Backfill salary/years for old rows and mark rows failing hard filters (title, location, pay,
    years) as 'filtered'. Also catches stale rows that predate a filter fix, e.g. non-US postings with
    a blank location field that slipped through before location_ok() learned to check the description."""
    rows = conn.execute(
        "SELECT id, title, location, description, salary_min, salary_max FROM jobs WHERE status IN ('new','filtered') "
    ).fetchall()
    for jid, title, location, desc, old_lo, old_hi in rows:
        lo, hi = extract_salary(desc)
        lo, hi = lo or old_lo, hi or old_hi
        yrs = extract_years(desc)
        ok = hard_filters_ok(title, hi, yrs, location, desc)
        conn.execute(
            "UPDATE jobs SET salary_min=?, salary_max=?, years_req=?, status=? WHERE id=?",
            (lo, hi, yrs, "new" if ok else "filtered", jid),
        )
    conn.commit()


def cmd_check(_args):
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run_task, collect_sources()))
    for kind, name, jobs, err in results:
        status = f"ERROR {err}" if err else ("no such board" if jobs is None else f"{len(jobs)} postings")
        print(f"{kind:11s} {name:20s} {status}")


def cmd_fetch(_args):
    conn = db()
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(run_task, collect_sources()))
    seen = kept = new = 0
    for kind, name, jobs, err in results:
        if err or jobs is None:
            print(f"skip {kind}:{name} ({err or 'not found'})", file=sys.stderr)
            continue
        for j in jobs:
            seen += 1
            lo, hi = extract_salary(j["description"])
            lo, hi = j.get("salary_min") or lo, j.get("salary_max") or hi
            yrs = extract_years(j["description"])
            if not hard_filters_ok(j["title"], hi, yrs, j["location"], j["description"]):
                continue
            kept += 1
            jid = f"{j['source']}:{j['ext_id']}"
            cur = conn.execute(
                "INSERT OR IGNORE INTO jobs (id, source, company, title, location, url, description, posted_at,"
                " first_seen, track, sponsorship, salary_min, salary_max, years_req) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (jid, j["source"], j["company"], j["title"], j["location"], j["url"], j["description"],
                 j["posted_at"], time.time(), classify_track(j["title"]), sponsorship(j["description"]), lo, hi, yrs),
            )
            new += cur.rowcount
    conn.commit()
    refilter(conn)
    print(f"scanned {seen} postings, {kept} matched filters, {new} new")


def cmd_list(args):
    conn = db()
    refilter(conn)
    q = "SELECT id, company, title, location, track, sponsorship, status, url, salary_min, salary_max, years_req FROM jobs WHERE 1=1"
    params = []
    if not args.all:
        q += " AND status = 'new'"
    if args.track:
        q += " AND track = ?"
        params.append(args.track)
    if args.sponsor:
        q += " AND sponsorship = ?"
        params.append(args.sponsor)
    elif CFG["hide_blocked_sponsorship"]:
        q += " AND sponsorship != 'blocked'"
    q += " ORDER BY CASE sponsorship WHEN 'friendly' THEN 0 ELSE 1 END, first_seen DESC"
    for r in conn.execute(q, params):
        jid, company, title, loc, track, sp, status, url, lo, hi, yrs = r
        pay = f"${lo // 1000}-{hi // 1000}K" if lo else "pay not listed"
        exp = f"{yrs}+ yrs" if yrs is not None else "yrs not stated"
        print(f"[{sp:8s}] [{track:6s}] {company} - {title} ({loc})\n    {url}\n    {pay} | {exp} | id={jid} status={status}")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch").set_defaults(fn=cmd_fetch)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    ls = sub.add_parser("list")
    ls.add_argument("--all", action="store_true", help="include non-new statuses")
    ls.add_argument("--track", choices=["ai_eng", "ml_eng", "ds"])
    ls.add_argument("--sponsor", choices=["friendly", "unclear", "blocked"])
    ls.set_defaults(fn=cmd_list)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
