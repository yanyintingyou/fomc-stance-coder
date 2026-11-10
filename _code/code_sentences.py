"""Split in-scope FOMC text into sentence units and code five fields.

Scope: statement body and the minutes section holding participants' views.
This is a rule layer, not a finished measure: every row keeps the matched cue and a
status flag, so unresolved or contradictory units can be reviewed instead of scored.
A cue only counts when a topic word comes before it in the same unit, which blocks
reading "economic activity will expand" as an employment signal.
"""
import csv
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extracted"
OUT = ROOT / "coded"
SCOPE = {"statement_body", "participants"}

ABBREV = ["U.S.", "Mr.", "Ms.", "Mrs.", "Dr.", "St.", "No.", "vs.", "etc.", "e.g.", "i.e.",
          "Inc.", "Corp.", "Jr.", "Sr.", "a.m.", "p.m.", "Ph.D.", "Jan.", "Feb.", "Mar.",
          "Apr.", "Jun.", "Jul.", "Aug.", "Sep.", "Sept.", "Oct.", "Nov.", "Dec."]

INFL = r"(inflation|disinflation|deflation|\bcpi\b|\bpce\b|price index|price pressures?|core prices|consumer prices)"
LAB = r"(unemployment|payrolls?|employment|labou?r market|jobs?|job gains|hiring|layoffs?|vacancies|job openings|participation rate|wages?|wage growth|labor force|labour force)"

OUTLOOK_CUE = r"(\bexpected\b|\bexpects?\b|\banticipated\b|\bprojected\b|\bforecast\b|\boutlook\b|\blikely\b|\bwill\b|\bwould\b|\bcould\b|going forward|next year|coming quarters|longer run)"
CHANGE_CUE = r"(\bhas\b|\bhave\b|\bhad\b|over the (past|coming)|in recent months|\bsince\b|\brecently\b|continued to|began to|picked up|\bedged\b)"
STATE_CUE = r"(\bremains?\b|\bremained\b|\bis\b|\bare\b|\bwas\b|\bwere\b|\bstands?\b|\bcurrently\b|\bcontinues? to be\b)"
DIM_CUE = {"state": STATE_CUE, "change": CHANGE_CUE, "outlook": OUTLOOK_CUE}

DIRECTION = {
    "inf": {
        "state": [(1, r"\belevated\b|\bhigh\b|above (the )?(committee'?s )?(longer-run )?(goal|objective|target|2 percent|two percent)|well above|too high|\bpersistent\b|\bsticky\b"),
                  (-1, r"below (the )?(goal|objective|target|2 percent|two percent)|\bsubdued\b|\bsoft\b"),
                  (0, r"(near|close to|around|roughly at) (2 percent|two percent|the (goal|objective|target))|(consistent|in line) with [^.]{0,15}(goal|objective|target)")],
        "change": [(1, r"(has|have|had) (risen|increased|accelerated|picked up|moved up|edged up)|pick(ed)? up|higher than (expected|anticipated)|reaccelerat\w*"),
                   (-1, r"(has|have|had) (eased|declined|fallen|moderated|slowed|come down|decreased|retreated)|\bdisinflation\b|lower than (expected|anticipated)|continued to (ease|moderate|decline|slow)"),
                   (0, r"(little|roughly|broadly) (changed|unchanged)|(has|have) (been )?(steady|stable)|held steady")],
        "outlook": [(1, r"(expected|anticipated|projected|likely|would|could|will) [^.]{0,40}(rise|pick up|increase|higher)|\bupside risk|tilted to the upside"),
                    (-1, r"(expected|anticipated|projected|likely|would|could|will) [^.]{0,40}(decline|fall|ease|lower|slow|moderate)|\bdownside risk|tilted to the downside"),
                    (0, r"(roughly )?balanced")],
    },
    "lab": {
        "state": [(1, r"\btight\b|\bstrong\b|\bsolid\b|\brobust\b|\bhealthy\b|at a low level|low (level of )?unemployment|at or above|\bfull employment\b"),
                  (-1, r"\bslack\b|\bweak\b|lower than (the )?(goal|objective|maximum)|elevated unemployment|high unemployment|considerable slack|\bshortfall\b"),
                  (0, r"(roughly )?(in line with|consistent with) (the committee'?s )?(maximum employment|goal)|near (maximum employment|full employment)")],
        "change": [(1, r"(has|have|had) (strengthened|tightened|improved|increased)|continued to (expand|strengthen|tighten)|solid (gains|growth)|job gains"),
                   (-1, r"(has|have|had) (softened|cooled|weakened|eased|slowed|declined|moderated)|continued to (soften|cool|weaken|moderate)|weakening|rising unemployment|job losses|reducing payrolls|layoffs"),
                   (0, r"stabilized|(little|roughly|broadly) (changed|unchanged)|\bsteady\b|held steady")],
        "outlook": [(1, r"(expected|anticipated|projected|likely|would|could|will) [^.]{0,40}(improve|strengthen|rise|increase|solid|expand)"),
                    (-1, r"(expected|anticipated|projected|likely|would|could|will) [^.]{0,40}(weaken|soften|decline|slow|higher unemployment)"),
                    (0, r"(roughly )?balanced")],
    },
}

PROCEDURAL = r"(seeks? to achieve|maximum employment and (price stability|inflation at the rate)|voted|voting|approved by notation vote|directs? the desk|authorize and direct|target range for the federal funds rate at|decided to (maintain|raise|lower)|reaffirms? its intention|implement(s|ing)? monetary policy)"


def load_text(path):
    sections, current = [], ("Opening", [])
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            if current[1]:
                sections.append(current)
            current = (line[3:].strip(), [])
        elif line.strip() and not line.startswith("# "):
            current[1].append(line.strip())
    if current[1]:
        sections.append(current)
    return sections


def split_sentences(text):
    for ab in ABBREV:
        text = text.replace(ab, ab.replace(".", "\u00b7"))
    text = re.sub(r"(\d)\.(\d)", "\\1\u00b7\\2", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"\u201c])", text)
    return [p.replace("\u00b7", ".").strip() for p in parts if p.strip()]


def split_clauses(sentence):
    """Contrast and semicolon clauses carry opposite directions; keep them apart."""
    out = []
    for chunk in re.split(r";\s+", sentence):
        m = re.match(r"^(.*?),?\s+(although|though|while|whereas|but)\s+(.*)$", chunk, re.I)
        if m and len(m.group(1).split()) > 3:
            out.append((m.group(1), "main"))
            out.append((m.group(3), "concession"))
        else:
            out.append((chunk, "plain"))
    return [(c.strip(), role) for c, role in out if c.strip()]


def topics(unit):
    found = []
    if re.search(INFL, unit, re.I):
        found.append("inf")
    if re.search(LAB, unit, re.I):
        found.append("lab")
    return found


def direction(unit, topic, dim):
    """A cue counts only if a topic word precedes it, so topic and direction stay linked."""
    pattern = INFL if topic == "inf" else LAB
    topic_pos = [m.end() for m in re.finditer(pattern, unit, re.I)]
    hits = []
    for sign, pat in DIRECTION.get(topic, {}).get(dim, []):
        for m in re.finditer(pat, unit, re.I):
            if any(p <= m.start() + 40 for p in topic_pos):
                hits.append((sign, m.group(0)[:60]))
    if not hits:
        return None, ""
    if len({s for s, _ in hits}) > 1:
        return None, "conflict:" + "|".join(sorted({h[1] for h in hits}))[:80]
    sign, cue = hits[0]
    negated = re.search(r"(not|no longer|less|without)\s+(\w+\s+){0,2}" + re.escape(cue[:12]), unit, re.I)
    if negated:
        sign, cue = -sign, negated.group(0)[:60]
    return sign, cue


def code(doc_type, date, sec_type, para_id, sent_id, sentence):
    rows = []

    def row(clause_id, role, unit, judg, tp, dim, sign, cue, status):
        rows.append([doc_type, date, sec_type, para_id, sent_id, clause_id, role, unit,
                     judg, tp, dim, "" if sign is None else sign, cue, status])

    for clause_id, (unit, role) in enumerate(split_clauses(sentence), 1):
        tp = topics(unit)
        if not tp:
            row(clause_id, role, unit, 0, "", "", None, "", "no_topic")
            continue
        if re.search(PROCEDURAL, unit, re.I) and not re.search(r"(inflation|unemployment|labou?r market)", unit[:40], re.I):
            row(clause_id, role, unit, 0, "|".join(tp), "", None, "", "procedural")
            continue
        emitted = False
        for topic in tp:
            for dim in ("state", "change", "outlook"):
                if not re.search(DIM_CUE[dim], unit, re.I):
                    continue
                sign, cue = direction(unit, topic, dim)
                if sign is None and not cue:
                    continue
                status = "conflict" if cue.startswith("conflict") else "ok"
                row(clause_id, role, unit, 1, topic, dim, sign, cue, status)
                emitted = True
        if not emitted:
            row(clause_id, role, unit, 1, "|".join(tp), "", None, "", "unresolved")
    return rows


HEADER = ["doc_type", "meeting_date", "section_type", "para_id", "sent_id", "clause_id", "clause_role",
          "unit", "is_judgment", "topic", "dimension", "direction", "cue", "status"]


def main():
    OUT.mkdir(exist_ok=True)
    for kind in ("statement", "minutes"):
        sec_index = {}
        with (SRC / kind / "sections.csv").open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                sec_index[(r["meeting_date"], r["section_order"])] = r["section_type"]
        rows = []
        for path in sorted((SRC / kind / "text").glob("*.txt")):
            date = path.name.split("_")[0]
            for order, (heading, paras) in enumerate(load_text(path), 1):
                sec_type = sec_index.get((date, str(order)))
                if sec_type not in SCOPE:
                    continue
                for p_id, para in enumerate(paras, 1):
                    for s_id, sent in enumerate(split_sentences(para), 1):
                        rows += code(kind, date, sec_type, p_id, s_id, sent)
        with (OUT / f"{kind}_sentences.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(HEADER)
            w.writerows(rows)
        st = Counter(r[13] for r in rows)
        judg = sum(1 for r in rows if r[8] == 1)
        print(f"{kind}: units={len(rows)} judgment={judg} status={dict(st)}", flush=True)
    aggregate()


def aggregate():
    """Meeting x document x topic x dimension -> net direction share for regression."""
    agg = defaultdict(lambda: [0, 0, 0])
    for kind in ("statement", "minutes"):
        with (OUT / f"{kind}_sentences.csv").open(encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                if r["is_judgment"] != "1" or r["clause_role"] == "concession":
                    continue
                if r["direction"] == "" or r["dimension"] not in ("state", "change"):
                    continue
                key = (kind, r["meeting_date"], r["topic"], r["dimension"])
                agg[key][{"1": 0, "-1": 1, "0": 2}[r["direction"]]] += 1
    out = []
    for (kind, date, topic, dim), (pos, neg, zero) in sorted(agg.items()):
        total = pos + neg + zero
        out.append({
            "doc_type": kind, "meeting_date": date, "topic": topic, "dimension": dim,
            "n_pos": pos, "n_neg": neg, "n_neutral": zero, "n_units": total,
            "net": round((pos - neg) / total, 4), "thin": int(total < 3),
        })
    with (OUT / "meeting_variables.csv").open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=list(out[0]))
        w.writeheader()
        w.writerows(out)
    print(f"meeting_variables rows={len(out)} meetings={len({r['meeting_date'] for r in out})}", flush=True)


if __name__ == "__main__":
    main()
