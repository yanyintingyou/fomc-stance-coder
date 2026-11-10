"""Extract statement / minutes body text from the saved FOMC HTML pages.

Writes cleaned plain text (paragraph breaks kept, section headings marked with '## ')
plus a section index and a per-document manifest. Raw pages are never modified.
"""
import csv
import re
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
SRC = {"statement": ROOT / "FOMC statement", "minutes": ROOT / "FOMC minutes"}
OUT = ROOT / "extracted"
NOISE = {"share", "print", "skip to main content", "back to home", "chapters"}


def load(path):
    text = path.read_bytes().decode("utf-8", "ignore")
    if "\u00e2\u20ac" in text or "\u00c2" in text:  # page decoded as latin-1 upstream
        try:
            text = text.encode("latin-1").decode("utf-8")
        except UnicodeError:
            pass
    return text


def tidy(text):
    text = text.replace("\u00a0", " ").replace("\u200b", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def blocks(html):
    """Flatten the content container to lines; older pages keep body text as bare nodes."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "head"]):
        tag.decompose()
    root = soup.find(id="article") or soup.find(id="leftText") or soup.find(id="contentContainer") or soup.body
    # A trailing <br> inside a heading must not split its boundary markers.
    # Normalize heading whitespace before flattening the surrounding body.
    for st in root.find_all("strong"):
        st.string = tidy(st.get_text(" "))
    for br in root.find_all("br"):
        br.replace_with("\n")
    for el in root.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        el.append("\n")
    for st in root.find_all("strong"):
        st.insert_before("\x01")
        st.insert_after("\x02")
    out = []
    for raw in root.get_text("").split("\n"):
        line = tidy(raw)
        if not line or line.lower() in NOISE or set(line) <= {"_", " "}:
            continue
        head = ""
        if line.startswith("\x01") and "\x02" in line:
            head, line = line[1:].split("\x02", 1)
            head, line = tidy(head), tidy(line)
        head = head.replace("\x01", "").replace("\x02", "")
        line = line.replace("\x01", "").replace("\x02", "")
        if not line and not head:
            continue
        out.append((head, line))
    return out


def trim(items, start_pat, after=False):
    """Keep the document body only: from the release/opening line to the page footer."""
    begin = next((i for i, (_, t) in enumerate(items) if re.search(start_pat, t, re.I)), 0)
    if after:
        begin += 1
    body = []
    for head, text in items[begin:]:
        if re.match(r"^(return to top|back to top|last update|stay connected|frequently asked questions|related content)", text, re.I):
            break
        body.append((head, text))
    return body


def is_release(text):
    return re.match(r"for release at", text, re.I) is not None


def split_statement(items):
    start = next((i for i, (_, t) in enumerate(items) if is_release(t)), None)
    release = items[start][1] if start is not None else ""
    body, voting = [], []
    for head, text in items[start + 1 if start is not None else 0:]:
        if re.match(r"voting (for|against)", text, re.I):
            voting.append((head, text))
        elif re.match(r"for media inquiries|implementation note", text, re.I):
            break
        elif not voting:
            body.append((head, text))
        else:
            voting.append((head, text))
    return release, body, voting


def split_minutes(items):
    sections, order = [], []
    for head, text in items:
        if head:
            sections.append([head, []])
        if not sections:
            sections.append(["Opening", []])
        if text:
            sections[-1][1].append(text)
    if len(sections) == 1:  # older minutes carry no headings
        sections[0][0] = "Body"
    return sections


def render(title, sections):
    lines = [f"# {title}", ""]
    for head, paras in sections:
        lines += [f"## {head}", ""]
        for p in paras:
            lines += [p, ""]
    return "\n".join(lines).rstrip() + "\n"


def section_type(head):
    """Canonical label so downstream coding can filter sections without matching wording."""
    h = head.lower()
    if ("participant" in h and "view" in h
            and "current" in h and "condition" in h and "outlook" in h):
        return "participants"
    if "staff review of the economic" in h or "staff review of economic" in h:
        return "staff_econ"
    if "staff review of the financial" in h:
        return "staff_fin"
    if "staff economic outlook" in h or "staff forecast" in h:
        return "staff_outlook"
    if "committee policy action" in h:
        return "policy_action"
    if "voting" in h or "votes for" in h or "votes against" in h or "notation vote" in h or "absent and not" in h:
        return "voting"
    if "developments in financial" in h or "money market" in h:
        return "markets"
    if "statement" == h:
        return "statement_body"
    if any(k in h for k in ("opening", "present", "attendance", "secretary", "organizational")):
        return "front"
    if re.fullmatch(r"([A-Z][a-z]+\.? ){1,3}[A-Z][a-z]+", head or ""):
        return "signature"
    return "other"


def main():
    for kind, folder in SRC.items():
        docs = OUT / kind
        (docs / "text").mkdir(parents=True, exist_ok=True)
        manifest, index = [], []
        for path in sorted(folder.glob("*.html")):
            date = path.name.split("_")[0]
            items = blocks(load(path))
            if kind == "statement":
                items = trim(items, r"^(for release at|for immediate release)", after=True)
                release, body, voting = split_statement(items)
                sections = [["Statement", [t for _, t in body]]]
                if voting:
                    sections.append(["Voting", [t for _, t in voting]])
                title, release_line = f"STATEMENT {date}", release
            else:
                items = trim(items, r"^(a (joint )?meeting of the Federal Open Market Committee|the Federal Open Market Committee met)")
                sections = split_minutes(items)
                title, release_line = f"MINUTES {date}", ""
            text = render(title, sections)
            (docs / "text" / f"{date}_{kind}.txt").write_text(text, encoding="utf-8")
            for i, (head, paras) in enumerate(sections, 1):
                index.append({
                    "meeting_date": date,
                    "section_order": i,
                    "section_type": section_type(head),
                    "heading": head,
                    "n_words": sum(len(p.split()) for p in paras),
                    "n_paragraphs": len(paras),
                })
            manifest.append({
                "meeting_date": date,
                "doc_type": kind,
                "release_line": release_line,
                "n_sections": len(sections),
                "n_words": len(text.split()),
                "has_headings": int(any(h not in ("Statement", "Body", "Opening") for h, _ in sections)),
            })
        with (docs / "sections.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(index[0]))
            w.writeheader()
            w.writerows(index)
        with (docs / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(manifest[0]))
            w.writeheader()
            w.writerows(manifest)
        no_head = [m["meeting_date"] for m in manifest if not m["has_headings"]]
        print(f"{kind}: {len(manifest)} docs, no-heading {len(no_head)} {no_head[:6]}", flush=True)


if __name__ == "__main__":
    main()
