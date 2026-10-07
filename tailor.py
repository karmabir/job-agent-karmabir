#!/usr/bin/env python3
"""Generate a tailored one-page resume PDF + cover letter PDF for one job.

Usage: python tailor.py <job_id>

Truthfulness guards:
- The model may only select, reorder and lightly rephrase bullets from profile.yaml.
- Rewritten bullets are rejected if they add a number or a proper noun/tool not in the profile.
- Summary and cover letter go through a separate fact-check pass; unsupported claims trigger a rewrite
  and, if any remain, a WARNING in notes.txt.
Layout guards: every bullet must fit on one line; the resume must fit on one page.
"""
import re
import sys

from pypdf import PdfReader
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from common import FAST_MODEL, ROOT, WRITE_MODEL, ask_json, db, load_profile, profile_text, usage_report

FONT, BOLD, SIZE = "Times-Roman", "Times-Bold", 10
W, H = letter
MARGIN = 40
BULLET_X, TEXT_X = MARGIN + 4, MARGIN + 16
LINE_W = W - 2 * MARGIN
BULLET_W = W - MARGIN - TEXT_X

SYSTEM = """You tailor a resume and cover letter for one job posting using ONLY facts in the candidate profile.
Rules:
- Never invent employers, tools, metrics, dates, responsibilities, collaboration or soft-skill claims.
  Numbers and tool names must come from the profile. If the posting wants something the candidate lacks,
  do not claim it; emphasize what is genuinely relevant instead.
- Resume: choose which bullets to keep, reorder them, and lightly rephrase to mirror the posting's keywords.
  Every bullet must be short enough for ONE line (about 120 characters max); keep the leading action verb.
  Include ALL bullets of ALL roles in the profile, ordered most relevant to this posting first within each role
  (a bullet marked pinned: true in the profile must stay first in its role, text unchanged). Also list ALL
  publications and ALL projects (ids in "projects"), most relevant first.
  A layout engine will trim the least relevant items so the resume fits one page; order matters.
  Order the roles as in the profile (most recent first). Include all five skill categories, reordering skills
  inside each by relevance.
- Summary: 3-4 sentences filling exactly three full lines of a resume, 350-380 characters, last line at least half full. Use only profile facts.
- Cover letter: 4-5 paragraphs, 280-340 words, no greeting or sign-off (added automatically). Follow the
  structure and tone of the candidate's own past letters in STYLE EXAMPLES: opens "I am excited to apply for the
  <role> position at <company>. With <N> years of experience ...", ties to the company's mission or product,
  a paragraph on the current role with metrics, a paragraph on research/relevant background, why this company,
  and a closing thank-you. The example letters are for tone and structure ONLY; their factual claims are NOT
  authoritative. Every claim about the candidate must be supported by the profile.
- Follow the profile's writing_rules exactly.
- Never merge facts from different bullets into one claim (for example, do not attach a dataset size from one
  bullet to a model score from another). State each fact separately or in its own sentence.
- Do not mention visa or work authorization.
Return ONLY JSON:
{"variant": "ds"|"ml_eng"|"ai_eng",
 "summary": "...",
 "experience": [{"id": "<experience id>", "bullets": [{"id": "<bullet id>", "text": "<original or lightly rephrased>"}]}],
 "publications": ["<publication id>", ...],
 "projects": ["<project id>", ...],
 "skills": {"<category>": ["skill", ...]},
 "cover_letter": ["paragraph 1", "paragraph 2", ...],
 "notes": "what you emphasized and any weak spots the candidate should know about"}"""

FACT_SYSTEM = """You are a strict fact-checker. Given a candidate PROFILE and a TEXT (resume summary and cover letter),
list every claim the TEXT makes about the candidate that the PROFILE does not directly support: employers, roles,
tools, numbers, responsibilities, collaboration or soft-skill claims ("worked with stakeholders", "cross-functional"),
years of experience. Ignore statements about the hiring company. Statements in the profile's summary_variants
(e.g. "3+ years of experience") count as supported. Paraphrase, mild generalization and reordering are fine when
the underlying fact is in the profile (e.g. "experimentation frameworks" for A/B and incrementality testing
frameworks). Only flag genuinely NEW facts: tools, employers, numbers, duties or claims absent from, or contradicting, the profile.
Do NOT flag vagueness, loose attribution, generalization or omitted detail when the substance is in the profile.
Understating the candidate's experience is not a problem; only flag overstating or newly combined facts.
Return ONLY JSON: {"unsupported": ["claim", ...]}"""

SHORTEN_SYSTEM = """Shorten the text to at most the given number of characters without changing its meaning.
Keep every number. Add no new facts, tools or claims. Return ONLY JSON: {"text": "..."}"""


def numbers(s: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*\d|\d", s))


def grounded(candidate: str, source: str, profile_lower: str) -> bool:
    if not numbers(candidate) <= numbers(source):
        return False
    words = [m.group(0) for m in re.finditer(r"\b[A-Z][A-Za-z0-9+#]*\b", candidate)
             if m.start() > 0 and not re.search(r"[.!?]\s+$", candidate[:m.start()])]
    return all(w.lower() in profile_lower for w in words)


def width(text: str, font=FONT) -> float:
    return stringWidth(text, font, SIZE)


LINK_RGB = (0.06, 0.27, 0.67)


def pieces_width(pieces, size) -> float:
    return sum(stringWidth(t, f, size) for t, f, _ in pieces)


def draw_pieces(c, x, y, pieces, size):
    for text, font, url in pieces:
        w = stringWidth(text, font, size)
        c.setFont(font, size)
        if url:
            c.setFillColorRGB(*LINK_RGB)
            c.setStrokeColorRGB(*LINK_RGB)
            c.drawString(x, y, text)
            c.setLineWidth(0.4)
            c.line(x, y - 1, x + w, y - 1)
            c.linkURL(url, (x, y - 2, x + w, y + size), relative=0)
            c.setFillColorRGB(0, 0, 0)
            c.setStrokeColorRGB(0, 0, 0)
        else:
            c.drawString(x, y, text)
        x += w


def link_pieces(links):
    if not links:
        return []
    out = [(" (", FONT, None)]
    for i, l in enumerate(links):
        if i:
            out.append((" | ", FONT, None))
        out.append((l["label"], FONT, l["url"]))
    out.append((")", FONT, None))
    return out


def title_pieces(title, links, tech=None):
    """Bold title (+ italic tech) + linked labels, shrunk to fit one line; returns (pieces, size)."""
    tech = list(tech or [])
    for size in (10, 9.6, 9.2, 8.8, 8.4):
        while True:
            pcs = [(title, BOLD, None)]
            if tech:
                pcs.append((" - " + ", ".join(tech), "Times-Italic", None))
            pcs += link_pieces(links)
            if pieces_width(pcs, size) <= LINE_W or not tech or size < 10:
                break
            tech.pop()
        if pieces_width(pcs, size) <= LINE_W:
            return pcs, size
    while pieces_width(pcs, 8.4) > LINE_W and len(title) > 10:
        title = title[:-4].rstrip() + "..."
        pcs[0] = (title, BOLD, None)
    return pcs, 8.4


def shorten(text: str, max_chars: int) -> str:
    try:
        return ask_json(FAST_MODEL, SHORTEN_SYSTEM, f"max_chars={max_chars}\n{text}", max_tokens=400)["text"].strip()
    except Exception:
        return text


def one_line(proposed: str, original: str, profile_lower: str) -> str | None:
    """Return text guaranteed grounded and fitting on one line, else None."""
    cand = proposed.strip() if grounded(proposed, original, profile_lower) else original
    for _ in range(3):
        if width(cand) <= BULLET_W:
            return cand
        target = int(len(cand) * BULLET_W / width(cand)) - 4
        short = shorten(cand, target)
        cand = short if grounded(short, original, profile_lower) else cand
    if width(original) <= BULLET_W:
        return original
    return None


def build_items(profile: dict, plan: dict, summary_lines: list[str]):
    """Resolve the plan into drawable items, all grounded and one-line."""
    plower = profile_text().lower()
    pinned = {b["id"] for e in profile["experience"] for b in e["bullets"] if b.get("pinned")}
    items = [("name", profile["person"]["name"])]
    p = profile["person"]
    contact = [(" | ".join([p["email"], p["phone"]]), FONT, None)]
    for label, key in (("LinkedIn", "linkedin"), ("GitHub", "github")):
        if p.get(key):
            contact += [(" | ", FONT, None), (label, FONT, p[key])]
    contact.append((" | " + p["location"], FONT, None))
    items.append(("contact", contact))
    items.append(("rule", None))
    items.append(("heading", "PROFESSIONAL SUMMARY"))
    items += [("para", ln) for ln in summary_lines]

    items.append(("heading", "WORK EXPERIENCE"))
    exp_by_id = {e["id"]: e for e in profile["experience"]}
    originals = {b["id"]: b["text"].strip() for e in profile["experience"] for b in e["bullets"]}
    # Role ORDER is fixed by profile.yaml (already reverse-chronological); the model only
    # chooses which bullets to keep per role, never the order of roles themselves.
    profile_order = {e["id"]: i for i, e in enumerate(profile["experience"])}
    exp_plan = sorted((item for item in plan["experience"] if item["id"] in profile_order),
                       key=lambda item: profile_order[item["id"]])
    for item in exp_plan:
        e = exp_by_id.get(item["id"])
        if not e:
            continue
        proposed = {b["id"]: b["text"] for b in item["bullets"] if b["id"] in originals}
        order = [b["id"] for b in item["bullets"] if b["id"] in originals]
        pinned_here = [b["id"] for b in e["bullets"] if b.get("pinned")]
        if pinned_here:
            PINNED_COMPANIES.add(e["company"])
            order = pinned_here + [i for i in order if i not in pinned_here]
        bullets = []
        for bid in dict.fromkeys(order):
            keep_verbatim = bid in pinned and width(originals[bid]) <= BULLET_W
            text = originals[bid] if keep_verbatim else one_line(
                proposed.get(bid, originals[bid]), originals[bid], plower)
            if text:
                bullets.append(text)
        if bullets:
            items.append(("role", (f"{e['company']} - {e['title']}", e["location"], e["dates"])))
            items += [("bullet", b) for b in bullets]

    projs = {x["id"]: x for x in profile.get("projects", [])}
    order = [i for i in plan.get("projects", []) if i in projs] or list(projs)
    order += [i for i in projs if i not in order]
    pubs = {x["id"]: x for x in profile["publications"]}
    chosen = [pubs[i] for i in plan.get("publications", []) if i in pubs][:4]
    if order or chosen:
        items.append(("heading", "PROJECTS"))
    if order:
        for pid in order:
            pr = projs[pid]
            bl = [t for t in (one_line(b, b, plower) for b in pr["bullets"]) if t]
            if bl:
                items.append(("projtitle", (pr["name"], pr["tech"], pr.get("links") or [])))
                items += [("bullet", b) for b in bl]

    if chosen:
        for pub in chosen:
            pub_bullets = [t for t in (one_line(b, b, plower) for b in pub["bullets"]) if t]
            if pub_bullets:
                items.append(("pubtitle", (pub.get("short_title") or pub["title"], pub.get("links") or [])))
                items += [("bullet", b) for b in pub_bullets]

    items.append(("heading", "TECHNICAL SKILLS"))
    allowed = {s for cat in profile["skills"].values() for s in cat}
    for cat, skills in plan["skills"].items():
        kept = [s for s in skills if s in allowed]
        label = {"llm_ai": "LLM & AI"}.get(cat, cat.replace("_", " ").title()) + ": "
        while kept and width(label, BOLD) + width(", ".join(kept)) > LINE_W:
            kept.pop()
        if kept:
            items.append(("skill", (label, ", ".join(kept))))

    items.append(("heading", "EDUCATION"))
    for ed in profile["education"]:
        items.append(("edu", (f"{ed['school']} - {ed['degree']} (GPA: {ed['gpa']})", ed["dates"])))
        if ed.get("notes"):
            items.append(("edunote", ed["notes"].replace("; ", " | ")))
    return items


GAP = {"name": 20, "contact": 0, "rule": 8, "heading": 9, "role": 3}


def item_height(kind: str, lead: float) -> float:
    return lead + GAP.get(kind, 0) if kind != "rule" else GAP["rule"]


def total_height(items, lead):
    return 2 * MARGIN * 0.75 + sum(item_height(k, lead) for k, _ in items)


PINNED_COMPANIES = set()
LEADS = (14.0, 13.6, 13.2, 12.8, 12.4, 12.0)


def _cleanup(items):
    """Remove role/publication headers left without bullets, and empty section headings."""
    out = []
    for i, (k, v) in enumerate(items):
        nxt = items[i + 1][0] if i + 1 < len(items) else None
        if k in ("role", "pubtitle", "projtitle") and nxt != "bullet":
            continue
        out.append((k, v))
    res = []
    for i, (k, v) in enumerate(out):
        nxt = out[i + 1][0] if i + 1 < len(out) else None
        if k == "heading" and v == "PROJECTS" and nxt not in ("pubtitle", "projtitle"):
            continue
        res.append((k, v))
    return res


def _drop_least_important(items):
    """Trim priority (least important first): the LEAST relevant publication's bullets, then the
    LEAST relevant project's bullets, then the OLDEST role's bullets, working forward toward the
    most recent role - which is protected until everything else is exhausted. Relevance/recency
    order is the order the items already appear in (the model ranks publications and projects most
    relevant first; roles are in profile order, most recent first). Within any group, the deepest
    (last) bullet goes first. The first bullet of the most recent role is never dropped."""
    role_seq = pub_seq = proj_seq = -1
    group = None  # (kind, start_index, seq_or_None)
    pos = 0
    cands = []
    for i, (k, v) in enumerate(items):
        if k == "role":
            role_seq += 1
            group, pos = ("role", i, role_seq), 0
        elif k == "pubtitle":
            pub_seq += 1
            group, pos = (k, i, pub_seq), 0
        elif k == "projtitle":
            proj_seq += 1
            group, pos = (k, i, proj_seq), 0
        elif k == "bullet":
            is_protected = group[0] == "role" and group[2] == 0 and pos == 0
            if not is_protected:
                tier = {"pubtitle": 3, "projtitle": 2, "role": 1}[group[0]]
                urgency = tier * 1000 + group[2] * 10 + pos
                cands.append((urgency, i))
            pos += 1
    _, idx = max(cands)
    return _cleanup(items[:idx] + items[idx + 1:])


def trim_to_page(items):
    """Drop least important bullets until the content fits at leading >= 12, then use the roomiest leading that fits."""
    while total_height(items, LEADS[-1]) > H:
        items = _drop_least_important(items)
    for lead in LEADS:
        if total_height(items, lead) <= H:
            return items, lead
    return items, LEADS[-1]


def render_resume(items, lead, path):
    c = canvas.Canvas(str(path), pagesize=letter)
    c.setTitle("Resume")
    y = H - MARGIN * 0.75
    for kind, val in items:
        y -= lead if kind != "rule" else 0
        if kind == "name":
            c.setFont(BOLD, 16)
            c.drawCentredString(W / 2, y - 3, val)
            y -= GAP["name"] - 6
        elif kind == "contact":
            draw_pieces(c, (W - pieces_width(val, SIZE)) / 2, y, val, SIZE)
        elif kind == "rule":
            y -= 5
            c.setLineWidth(0.6)
            c.line(MARGIN, y, W - MARGIN, y)
            y -= GAP["rule"] - 5
        elif kind == "heading":
            y -= GAP["heading"] - 1
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, y, val)
        elif kind == "para":
            c.setFont(FONT, SIZE)
            c.drawString(MARGIN, y, val)
        elif kind == "role":
            left, loc, dates = val
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, y, left)
            c.setFont(FONT, SIZE)
            c.drawString(MARGIN + width(left, BOLD), y, f" | {loc}")
            c.drawRightString(W - MARGIN, y, dates)
        elif kind == "bullet":
            c.setFont(FONT, SIZE)
            c.drawString(BULLET_X, y, "-")
            c.drawString(TEXT_X, y, val)
        elif kind in ("pubtitle", "projtitle"):
            pcs, size = title_pieces(*val) if kind == "pubtitle" else title_pieces(val[0], val[2], val[1])
            draw_pieces(c, MARGIN, y, pcs, size)
        elif kind == "skill":
            label, rest = val
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, y, label)
            c.setFont(FONT, SIZE)
            c.drawString(MARGIN + width(label, BOLD), y, rest)
        elif kind == "edunote":
            c.setFont("Times-Italic", SIZE)
            c.drawString(MARGIN, y, val)
        elif kind == "edu":
            left, dates = val
            c.setFont(BOLD, SIZE)
            c.drawString(MARGIN, y, left)
            c.setFont(FONT, SIZE)
            c.drawRightString(W - MARGIN, y, dates)
    c.showPage()
    c.save()
    return y


def render_cover_letter(paragraphs, name, path):
    style = ParagraphStyle("body", fontName=FONT, fontSize=11, leading=14.5, alignment=TA_JUSTIFY, spaceAfter=9)
    doc = SimpleDocTemplate(str(path), pagesize=letter, leftMargin=72, rightMargin=72, topMargin=72, bottomMargin=72,
                            title="Cover Letter", author=name)
    flow = [Paragraph("Dear Hiring Team,", style), Spacer(1, 4)]
    flow += [Paragraph(p.replace("&", "&amp;"), style) for p in paragraphs]
    flow += [Spacer(1, 6), Paragraph("Sincerely,<br/>" + name, style)]
    doc.build(flow)


def generate(job, feedback: str = "") -> dict:
    company, title, location, url, description = job
    examples = (ROOT / "style" / "cover_letter_examples.txt").read_text()
    user = (
        f"CANDIDATE PROFILE (YAML):\n{profile_text()}\n\nSTYLE EXAMPLES (tone/structure only):\n{examples}\n\n"
        f"POSTING:\n{company} - {title} ({location})\n{url}\n{description[:9000]}"
    )
    if feedback:
        user += f"\n\nYour previous draft made claims the profile does not support. Remove or rewrite them:\n{feedback}"
    return ask_json(WRITE_MODEL, SYSTEM, user, max_tokens=6000, retries=3)


def check_facts(plan: dict) -> list[str]:
    text = plan["summary"] + "\n\n" + "\n\n".join(plan["cover_letter"])
    res = ask_json(FAST_MODEL, FACT_SYSTEM, f"PROFILE:\n{profile_text()}\n\nTEXT:\n{text}", max_tokens=800)
    return res.get("unsupported", [])


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    jid = sys.argv[1]
    conn = db()
    row = conn.execute("SELECT company, title, location, url, description FROM jobs WHERE id=?", (jid,)).fetchone()
    if not row:
        raise SystemExit(f"no job {jid}")
    company, title = row[0], row[1]

    plan = generate(row)
    unsupported = check_facts(plan)
    for _ in range(3):
        if not unsupported:
            break
        plan = generate(row, "\n".join(f"- {u}" for u in unsupported))
        unsupported = check_facts(plan)

    profile = load_profile()
    plower = profile_text().lower()
    variant = re.sub(r"\s+", " ", profile["summary_variants"].get(plan.get("variant", "ds"), profile["summary_variants"]["ds"]).strip())
    summary = re.sub(r"\s+", " ", plan["summary"].strip())
    lines = simpleSplit(summary, FONT, SIZE, LINE_W)
    if len(lines) > 3:
        summary = shorten(summary, 3 * int(LINE_W / width("n")) - 20)
        lines = simpleSplit(summary, FONT, SIZE, LINE_W)
    full_three = len(lines) == 3 and width(lines[-1]) >= 150
    if not (grounded(summary, profile_text(), plower) and full_three):
        summary = variant
        lines = simpleSplit(summary, FONT, SIZE, LINE_W)

    items, lead = trim_to_page(build_items(profile, plan, lines))
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{title}".lower()).strip("-")[:80]
    out = ROOT / "out" / slug
    out.mkdir(parents=True, exist_ok=True)
    for old in ("resume.docx",):
        (out / old).unlink(missing_ok=True)
    last_y = render_resume(items, lead, out / "resume.pdf")
    pages = len(PdfReader(str(out / "resume.pdf")).pages)
    if pages != 1 or last_y < 24:
        raise SystemExit(f"resume does not fit one page (pages={pages}, bottom y={last_y:.0f}); layout guard failed")

    name = profile["person"]["name"]
    render_cover_letter(plan["cover_letter"], name, out / "cover_letter.pdf")
    (out / "cover_letter.txt").write_text("Dear Hiring Team,\n\n" + "\n\n".join(plan["cover_letter"]) + f"\n\nSincerely,\n{name}\n")
    notes = f"{title} @ {company}\n{row[3]}\n\nvariant: {plan.get('variant')}\n{plan.get('notes', '')}\n"
    if unsupported:
        notes += "\nWARNING - claims the fact-check could not tie to profile.yaml (review before sending):\n"
        notes += "\n".join(f"- {u}" for u in unsupported) + "\n"
    (out / "notes.txt").write_text(notes)
    conn.execute("UPDATE jobs SET status='prepared' WHERE id=? AND status IN ('new','shortlisted')", (jid,))
    conn.commit()
    print(f"wrote {out} (resume: {pages} page, {len(plan['cover_letter'])} letter paragraphs)")
    if unsupported:
        print("WARNING unsupported claims:\n" + "\n".join(f"- {u}" for u in unsupported))
    print(usage_report())


if __name__ == "__main__":
    main()
