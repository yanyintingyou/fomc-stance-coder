"""Download FOMC statements and minutes (HTML, 2008-2025) from federalreserve.gov.

Raw pages are stored unchanged in ../FOMC statement and ../FOMC minutes,
named by the policy decision date. A sources.csv in each folder records provenance.
"""
import csv
import re
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://www.federalreserve.gov"
ROOT = Path(__file__).resolve().parent.parent
OUT = {"statement": ROOT / "FOMC statement", "minutes": ROOT / "FOMC minutes"}
YEARS = range(2008, 2026)
PAUSE = 0.2
HEADERS = {"User-Agent": "Mozilla/5.0 (local research script)"}

STMT_RE = re.compile(r"monetary/?(\d{8})([a-z]?)\.htm$")
MIN_RE = re.compile(r"fomc(?:minutes)?(\d{8})\.htm$")
RELEASED_RE = re.compile(r"Released\s+([A-Z][a-z]{2,8}\.?\s+\d{1,2},\s+\d{4})")


def get(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
    except requests.RequestException:
        return None
    time.sleep(PAUSE)
    return r.text if r.status_code == 200 else None


def block_text(a, maxlen=300):
    node = a
    for _ in range(3):
        node = node.parent
        if node is None:
            break
        text = node.get_text(" ", strip=True)
        if len(text) <= maxlen:
            return text
    return ""


def index_urls():
    urls = [f"{BASE}/monetarypolicy/fomchistorical{y}.htm" for y in range(2008, 2021)]
    urls.append(f"{BASE}/monetarypolicy/fomccalendars.htm")
    return urls


def collect():
    stmt, mins, released = {}, {}, {}
    for url in index_urls():
        html = get(url)
        print(f"index {url} -> {'ok' if html else 'FAILED'}", flush=True)
        if not html:
            continue
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = MIN_RE.search(href)
            if m:
                date = m.group(1)
                if int(date[:4]) in YEARS and date not in mins:
                    mins[date] = BASE + href
                    hit = RELEASED_RE.search(block_text(a))
                    if hit:
                        raw = hit.group(1).replace(".", "")
                        for fmt in ("%B %d, %Y", "%b %d, %Y"):
                            try:
                                released[date] = datetime.strptime(raw, fmt).date().isoformat()
                                break
                            except ValueError:
                                continue
                continue
            m = STMT_RE.search(href)
            if not m or int(m.group(1)[:4]) not in YEARS:
                continue
            text = a.get_text(" ", strip=True)
            if not (text == "Statement" or block_text(a, 200).startswith("Statement:")):
                continue
            stmt.setdefault(m.group(1), BASE + href)
    return stmt, mins, released


def save(kind, date, url, release_date):
    folder = OUT[kind]
    folder.mkdir(parents=True, exist_ok=True)
    name = f"{date[:4]}-{date[4:6]}-{date[6:]}_{kind}.html"
    path = folder / name
    marker = "Minutes of the Federal Open Market Committee" if kind == "minutes" else "Federal Open Market Committee"
    if path.exists():
        html = path.read_text(encoding="utf-8", errors="ignore")
        status = "ok" if marker in html else "check_failed"
    else:
        html = get(url)
        if html is None:
            status = "failed"
        else:
            path.write_text(html, encoding="utf-8")
            status = "ok" if marker in html else "check_failed"
    return {
        "meeting_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        # statement release id (monetaryYYYYMMDD) is the release day; minutes date comes from page text
        "release_date": release_date or (f"{date[:4]}-{date[4:6]}-{date[6:]}" if kind == "statement" else ""),
        "release_time": "",
        "timezone": "",
        "source_url": url,
        "local_file": name,
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "status": status,
    }


def main():
    stmt, mins, released = collect()
    print(f"discovered statements={len(stmt)} minutes={len(mins)}", flush=True)
    for kind, items in (("statement", stmt), ("minutes", mins)):
        rows = [save(kind, d, items[d], released.get(d, "") if kind == "minutes" else "") for d in sorted(items)]
        with (OUT[kind] / "sources.csv").open("w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        bad = [r["local_file"] for r in rows if r["status"] != "ok"]
        print(f"{kind}: rows={len(rows)} not_ok={len(bad)} {bad[:10]}", flush=True)
    only_stmt = sorted(set(stmt) - set(mins))
    only_min = sorted(set(mins) - set(stmt))
    print(f"statement-only={only_stmt}", flush=True)
    print(f"minutes-only={only_min}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
