"""Code FOMC paragraphs with a local OpenAI-compatible chat endpoint.

Each paragraph gets one request; the model returns judgments on a fixed axis, and every
item must quote its own evidence verbatim or it is dropped. Aggregation is done in code,
so the model never controls the final variables.

Usage:
  python llm_code.py --dry-run                       # print one prompt, no network
  python llm_code.py --pilot 12 --workers 2          # 12 meetings first
  python llm_code.py --all --workers 3
Endpoint/model come from --base-url and --model (or env LLM_BASE_URL / LLM_MODEL).
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "extracted"
OUT = ROOT / "coded" / "llm"
CACHE = OUT / "cache"
SCOPE = {"statement_body", "participants"}
# Participant headings are repaired in extraction; policy sections are not a substitute.
SCOPE_BY_DATE = {}
BATCH = {"statement": 7, "minutes": 10}
PROMPT_VERSION = "axis-v3"
HERE = Path(__file__).resolve().parent
PROFILES = HERE / "profiles.json"
KEYS = HERE / "keys.json"
KEY_FILE = HERE / "api_key.txt"          # fallback for the old single-key setup


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def pick_profile(name):
    cfg = read_json(PROFILES)
    table = cfg.get("profiles") or {}
    name = name or cfg.get("default") or "local"
    if name not in table:
        raise SystemExit(f"unknown profile '{name}'; available: {', '.join(table) or 'none'}")
    return name, table[name]


def resolve_key(explicit, prof):
    """Order: --api-key, LLM_API_KEY, keys.json[profile.key], legacy api_key.txt."""
    if explicit:
        return explicit.strip()
    if os.environ.get("LLM_API_KEY"):
        return os.environ["LLM_API_KEY"].strip()
    slot = prof.get("key")
    if slot:
        # a profile that names a slot never falls back to another platform's key
        return (read_json(KEYS).get(slot) or "").strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text(encoding="utf-8").strip()
    return ""

SYSTEM = """You label Federal Reserve communication. Output ONLY JSON, no prose, no code fences.

For each paragraph decide whether it contains an economic judgment about inflation or the labor market, and code it on two fixed axes. Never code sentiment or good/bad news; code only the direction on these axes.

Axis definitions
- inflation (topic "inf"), dimension "state": +1 inflation above the 2 percent goal or described as high/elevated/persistent; 0 explicitly near target or balanced; -1 below target or described as low/subdued.
- inflation, dimension "change": +1 inflation pressure rising, accelerating, or readings above expectations; 0 explicitly little changed; -1 easing, declining, moderating, or below expectations.
- labor (topic "lab"), dimension "state": +1 labor market tight, strong, solid, unemployment low relative to full employment; 0 explicitly balanced or in line with maximum employment; -1 slack, weak, unemployment elevated.
- labor, dimension "change": +1 strengthening, tightening, solid job gains; 0 explicitly stable; -1 softening, cooling, weakening, rising unemployment.
- dimension "outlook": only when the paragraph states an expectation about the future; same +/- meaning as above.

Rules
1. One paragraph may produce several items (state and change can point opposite ways). Return an empty list when the paragraph is procedural, a roster, a vote, SEP methodology, or makes no economic judgment.
2. "evidence" must copy a contiguous fragment of the paragraph, at most 15 words, exactly as written.
3. Do not guess from outside knowledge of the economy; code only what this paragraph says.
4. direction must be -1, 0 or 1. Use 0 only for an explicit neutral statement, never for "unclear".
5. A statement of the Committee's goal, mandate, or commitment is not a judgment about the economy. "Ensure that inflation returns to 2 percent" is a policy intention: return no item for it.
6. Minutes report speech. Code the judgment being reported, not the reporting frame ("participants noted that ..." is coded by what follows).
7. If a sentence concedes progress but asserts a level ("although inflation has eased, it remains elevated"), return both a change item and a state item, not one net value.
8. Inflation expectations belong to households, markets and firms, not to the Committee's own forecast. "Expectations remained anchored" is not an outlook item: return nothing for it. Code "outlook" only when the Committee states what it expects will happen.

Output schema
{"items":[{"topic":"inf|lab","dimension":"state|change|outlook","direction":-1,"evidence":"...","confidence":"high|medium|low"}]}"""

FEWSHOT = [
    ('Inflation remains somewhat elevated.', [{"topic": "inf", "dimension": "state", "direction": 1, "evidence": "Inflation remains somewhat elevated", "confidence": "high"}]),
    ('Although inflation had eased substantially, it remained somewhat elevated.', [{"topic": "inf", "dimension": "change", "direction": -1, "evidence": "inflation had eased substantially", "confidence": "high"}, {"topic": "inf", "dimension": "state", "direction": 1, "evidence": "it remained somewhat elevated", "confidence": "high"}]),
    ('The Committee voted to maintain the target range at 4-1/4 to 4-1/2 percent.', []),
    ('The Committee will continue to monitor incoming information and is strongly committed to returning inflation to its 2 percent objective.', []),
    ('Longer-run inflation expectations remained well anchored.', []),
    ('Inflation is expected to moderate in coming quarters.', [{"topic": "inf", "dimension": "outlook", "direction": -1, "evidence": "Inflation is expected to moderate", "confidence": "high"}]),
]


def paragraphs():
    for kind in ("statement", "minutes"):
        index = {}
        with (SRC / kind / "sections.csv").open(encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                index[(row["meeting_date"], row["section_order"])] = row["section_type"]
        for path in sorted((SRC / kind / "text").glob("*.txt")):
            date = path.name.split("_")[0]
            order, buf = 0, []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.startswith("## "):
                    if buf:
                        yield from emit(kind, date, index, order, buf)
                    order, buf = order + 1, []
                elif line.strip() and not line.startswith("# "):
                    buf.append(line.strip())
            if buf:
                yield from emit(kind, date, index, order, buf)


def emit(kind, date, index, order, paras):
    sec = index.get((date, str(order)))
    if sec not in SCOPE_BY_DATE.get(date, SCOPE):
        return
    for i, text in enumerate(paras, 1):
        yield {"pid": f"{date}|{kind}|{order}|{i}", "date": date, "doc": kind,
               "section_type": sec, "text": text}


def build_messages(batch):
    body = json.dumps([{"id": p["pid"], "text": p["text"]} for p in batch], ensure_ascii=False)
    messages = [{"role": "system", "content": SYSTEM}]
    for text, items in FEWSHOT:
        messages.append({"role": "user", "content": text})
        messages.append({"role": "assistant", "content": json.dumps({"items": items}, ensure_ascii=False)})
    messages.append({"role": "user", "content": "Paragraphs:\n" + body +
                     '\n\nReturn {"results":[{"id":"<paragraph id>","items":[...]}]} covering every id.'})
    return messages


def norm(text):
    return re.sub(r"\s+", " ", text).strip().lower()


PROXY = {
    # evidence must mention the topic; these are the words allowed to carry it
    "inf": r"inflation|cpi|pce|price|wage|cost|compensat|salary",
    "lab": r"unemploy|payroll|employ|labou?r|job|wage|hiring|participat|hours|workweek|slack|utilization",
}
# "price" alone would let asset-market sentences pass as inflation
NON_INFL_PRICE = r"(equity|stock|share|house|home|housing|asset|bond|commodit(?:y|ies)|oil)\s+prices?"
CORE_INFL = r"inflation|cpi|pce|wage|cost|compensat|salary"


def evidence_ok(evidence, text_n, topic, dim=""):
    """Verbatim check, tolerating a model that elides the middle of a long quote with '...'.

    The topic word may sit in the quoted clause or in the text around it: minutes routinely
    write "they slowed in recent months" and name the subject a sentence earlier.
    """
    ev = norm(evidence)
    if not ev:
        return False
    anchor = ev
    if "..." in ev or "…" in ev:
        parts = [p for p in (norm(x) for x in re.split(r"\.\.\.|…", ev)) if len(p) > 3]
        if not parts:
            return False
        positions = [text_n.find(p) for p in parts]
        if any(i < 0 for i in positions) or positions != sorted(positions):
            return False
        anchor = parts[0]
    elif ev not in text_n:
        return False
    if re.search(PROXY[topic], ev) is None:
        start = text_n.find(anchor)
        window = text_n[max(0, start - 250): start + len(anchor) + 150]
        if re.search(PROXY[topic], window) is None:
            return False
    # an asset-price phrase is not inflation unless some other inflation word is present
    if topic == "inf" and re.search(NON_INFL_PRICE, ev) and not re.search(CORE_INFL, ev):
        return False
    # expectations describe households/markets, not the Committee's own forecast
    if topic == "inf" and "expectation" in ev and not re.search(r"expected to|projected to|\bwill\b|\bwould\b", ev):
        return False
    return True


def parse(content, batch):
    data = extract_json(content)
    got = {}
    for block in data.get("results") or []:
        if isinstance(block, dict):
            got[block.get("id")] = block.get("items") or []
    rows, dropped = [], 0
    for p in batch:
        text_n = norm(p["text"])
        for item in got.get(p["pid"], []) or []:
            ev = norm(str(item.get("evidence", "")))
            topic = item.get("topic")
            dim = item.get("dimension")
            sign = item.get("direction")
            ok = (topic in ("inf", "lab") and dim in ("state", "change", "outlook")
                  and sign in (-1, 0, 1) and evidence_ok(item.get("evidence", ""), text_n, topic, dim))
            if not ok:
                dropped += 1
                continue
            rows.append({"pid": p["pid"], "meeting_date": p["date"], "doc_type": p["doc"],
                         "topic": topic, "dimension": dim, "direction": sign,
                         "evidence": item.get("evidence", ""), "confidence": item.get("confidence", "")})
    missing = [p["pid"] for p in batch if p["pid"] not in got]
    return rows, dropped, missing


def call(base_url, model, messages, api_key="", timeout=1800, think=True, max_tokens=0,
         json_mode=False, temperature=0.0, effort="", think_off=None):
    """Return (content, meta). Only `content` is ever parsed; reasoning is measured, not used.

    Thinking is kept on because it improves the axis judgments, but it is billed against
    max_tokens, so a cap must cover reasoning plus the answer. Measured: 5 minutes
    paragraphs -> 6,308 completion tokens of which ~5,300 were reasoning.
    """
    url = base_url.rstrip("/") + "/chat/completions"
    # MiMo and DeepSeek ignore temperature while thinking; kept for local runs
    payload = {"model": model, "messages": messages, "temperature": temperature}
    if max_tokens:
        payload["max_tokens"] = max_tokens
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if not think:
        payload["max_tokens"] = max_tokens or 1200
        # vendor switches only make sense on the local Qwen server; cloud APIs reject them
        if re.search(r"(127\.0\.0\.1|localhost)", base_url):
            payload["reasoning_effort"] = "none"
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        elif think_off:
            # DeepSeek and MiMo both switch off with thinking.type=disabled
            payload.update(think_off)
    elif effort:
        # DeepSeek accepts low/high/max (default high); MiMo has no effort knob at all
        payload["reasoning_effort"] = effort
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    # cloud gateways throttle with 429 and transient 5xx; retry here so a burst is not
    # mistaken for a bad batch and silently downgraded to a non-thinking call
    for attempt in range(4):
        r = requests.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code == 429 or r.status_code >= 500:
            if attempt == 3:
                r.raise_for_status()
            time.sleep(5 * (2 ** attempt))
            continue
        r.raise_for_status()
        break
    data = r.json()
    choice = data["choices"][0]
    msg = choice["message"]
    meta = {"finish_reason": choice.get("finish_reason"),
            "reasoning_chars": len(msg.get("reasoning_content") or msg.get("reasoning") or ""),
            "usage": data.get("usage") or {}}
    return (msg.get("content") or ""), meta


def extract_json(content):
    """Models may prepend reasoning; take the outermost object."""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json object")
    return json.loads(content[start:end + 1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="", help="name in profiles.json (default: its 'default' entry)")
    ap.add_argument("--list-profiles", action="store_true")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--ping", action="store_true")
    ap.add_argument("--pilot", type=int, default=0, help="use N evenly spaced meetings")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--workers", type=int, default=0, help="0 takes the profile value")
    ap.add_argument("--limit", type=int, default=0, help="cap number of requests")
    ap.add_argument("--tag", default="", help="output prefix; defaults to prompt version + think flag")
    ap.add_argument("--event-dates", default="",
                    help="CSV with conference_date; code only those meetings")
    ap.add_argument("--batch-statement", type=int, default=BATCH["statement"])
    ap.add_argument("--batch-minutes", type=int, default=BATCH["minutes"])
    ap.add_argument("--think", dest="think", action="store_true", default=None,
                    help="keep native reasoning on; only the JSON answer is parsed (default)")
    ap.add_argument("--no-think", dest="think", action="store_false")
    ap.add_argument("--max-tokens", type=int, default=0, help="0 leaves the cap to the provider")
    ap.add_argument("--timeout", type=int, default=0,
                    help="seconds per request; 0 takes the profile value")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--effort", default="", help="reasoning effort: low/high/max (DeepSeek only)")
    ap.add_argument("--rpm", type=float, default=0, help="global requests per minute cap (0 = none)")
    ap.add_argument("--only-doc", default="", choices=["", "statement", "minutes"])
    ap.add_argument("--json-mode", action="store_true",
                    help="send response_format=json_object (not supported by every server)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reparse", action="store_true",
                    help="rebuild the items file from cached answers under the current rules")
    ap.add_argument("--reparse-minutes", type=int, default=0,
                    help="only caches written in the last N minutes (0 = all)")
    args = ap.parse_args()

    if args.list_profiles:
        cfg = read_json(PROFILES)
        for name, prof in (cfg.get("profiles") or {}).items():
            mark = "*" if name == (cfg.get("default") or "local") else " "
            print(f"{mark} {name:12s} {prof.get('model',''):22s} {prof.get('base_url','')}")
        return

    # a profile carries endpoint, model, key slot and the concurrency/timeout that suit it
    name, prof = pick_profile(args.profile)
    args.profile = name
    args.base_url = args.base_url or os.environ.get("LLM_BASE_URL") or prof.get("base_url", "")
    args.model = args.model or os.environ.get("LLM_MODEL") or prof.get("model", "")
    args.think = prof.get("think", True) if args.think is None else args.think
    args.effort = args.effort or prof.get("effort", "")
    args.rpm = args.rpm or prof.get("rpm", 0)
    args.think_off = prof.get("think_off") or {}
    args.workers = args.workers or prof.get("workers", 1)
    args.timeout = args.timeout or prof.get("timeout", 1800)
    args.api_key = resolve_key(args.api_key, prof)
    print(f"[profile {name}] {args.model} @ {args.base_url} | think={args.think} "
          f"effort={args.effort or 'default'} workers={args.workers} "
          f"key={'set' if args.api_key else 'MISSING'}", flush=True)
    size = {"statement": args.batch_statement, "minutes": args.batch_minutes}

    def make_batches(items):
        """Group by (meeting, document) first, then slice, so no batch mixes two documents."""
        grouped = {}
        for p in items:
            grouped.setdefault((p["date"], p["doc"]), []).append(p)
        out = []
        for (_, doc), g in grouped.items():
            n = size[doc]
            out += [g[i:i + n] for i in range(0, len(g), n)]
        return out

    if args.ping:
        try:
            out, meta = call(args.base_url, args.model, [{"role": "user", "content": "reply with ok"}],
                             args.api_key, timeout=600, think=args.think)
            print("OK:", (out or "")[:200], "| finish", meta["finish_reason"])
        except Exception as exc:
            print("FAILED:", type(exc).__name__, str(exc)[:300])
        return

    # without a scope flag the parser would happily code the whole 2008-2025 archive
    if not args.event_dates and not args.pilot and not args.all:
        raise SystemExit(
            "refusing to code the whole archive by default: pass --event-dates <csv> to "
            "restrict meetings, --pilot N for a sample, or --all for the full archive")

    paras = list(paragraphs())
    dates = sorted({p["date"] for p in paras})
    if args.event_dates:
        with open(args.event_dates, encoding="utf-8-sig") as fh:
            keep = {r["conference_date"][:10] for r in csv.DictReader(fh)}
        paras = [p for p in paras if p["date"] in keep]
        dates = sorted({p["date"] for p in paras})
        print(f"restricted to {len(keep)} meetings -> {len(paras)} paragraphs", flush=True)
    if args.only_doc:
        paras = [p for p in paras if p["doc"] == args.only_doc]
    if args.pilot:
        step = max(1, len(dates) // args.pilot)
        keep = set(dates[::step][: args.pilot])
        paras = [p for p in paras if p["date"] in keep]
    if args.dry_run:
        batch = make_batches(paras)[0]
        print(json.dumps(build_messages(batch), ensure_ascii=False, indent=1)[:2000])
        print(f"\nparagraphs={len(paras)} batches={len(make_batches(paras))} dates={len(dates)}")
        return

    OUT.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    tag = args.tag or "{}-{}-{}{}".format(
        PROMPT_VERSION, re.sub(r"[^A-Za-z0-9._-]+", "-", args.model),
        "think" if args.think else "plain",
        "-" + re.sub(r"[^A-Za-z0-9._-]+", "-", args.effort) if (args.effort and args.think) else "")
    # one folder per model, so runs never overwrite or blend into each other
    run_dir = OUT / tag
    run_dir.mkdir(parents=True, exist_ok=True)
    items_path = run_dir / "items.jsonl"
    fail_path = run_dir / "failures.csv"
    done_path = run_dir / "processed.jsonl"
    done, seen_rows = set(), set()
    # paragraphs that produced no item still count as processed; items alone would re-run them
    for path in (done_path, items_path):
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:   # last line cut short by a hard kill
                        continue
                    if "pid" in row:
                        done.add(row["pid"])
                    if "topic" in row:
                        seen_rows.add((row["pid"], row["topic"], row["dimension"],
                                       row["direction"], row["evidence"]))
    all_paras = list(paras)
    paras = [p for p in paras if p["pid"] not in done]
    batches = make_batches(paras)

    if args.reparse:
        lookup = {p["pid"]: p for p in all_paras}
        cutoff = time.time() - args.reparse_minutes * 60 if args.reparse_minutes else 0
        seen, out_rows = set(), []
        skipped_tag = 0
        for cache_file in sorted(CACHE.glob("*.json")):
            if cutoff and cache_file.stat().st_mtime < cutoff:
                continue
            try:
                saved = json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not (isinstance(saved, dict) and saved.get("pids")):
                continue
            batch = [lookup[x] for x in saved["pids"] if x in lookup]
            if not batch:
                continue
            # caches are shared across models: accept only this tag, or (for old files without
            # a tag) the one whose hash matches this exact batch under this tag
            saved_tag = saved.get("tag")
            if saved_tag and saved_tag != tag:
                skipped_tag += 1
                continue
            if not saved_tag:
                expect = hashlib.sha1((tag + "".join(p["text"] for p in batch)).encode()).hexdigest()[:16]
                if len(batch) != len(saved["pids"]) or expect != cache_file.stem:
                    skipped_tag += 1
                    continue
            try:
                rows, _, _ = parse(saved["content"], batch)
            except Exception:
                continue
            for r in rows:
                key = (r["pid"], r["topic"], r["dimension"], r["direction"], r["evidence"])
                if key in seen:
                    continue
                seen.add(key)
                r["model"] = args.model
                out_rows.append(r)
        with items_path.open("w", encoding="utf-8") as fh:
            for r in out_rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"reparsed items={len(out_rows)} skipped_other_tags={skipped_tag} -> {items_path}")
        return
    if args.limit:
        batches = batches[: args.limit]

    def run(batch, think):
        """One request. Cache stores the answer and its cost only, never the reasoning text."""
        key = hashlib.sha1((tag + "".join(p["text"] for p in batch)).encode()).hexdigest()[:16]
        cache_file = CACHE / f"{key}.json"
        if cache_file.exists():
            saved = json.loads(cache_file.read_text(encoding="utf-8"))
            content, meta = saved["content"], saved
        else:
            throttle()
            content, meta = call(args.base_url, args.model, build_messages(batch),
                                 args.api_key, timeout=args.timeout, think=think,
                                 temperature=args.temperature, effort=args.effort,
                                 think_off=args.think_off,
                                 max_tokens=args.max_tokens, json_mode=args.json_mode)
            cache_file.write_text(json.dumps({"content": content, "pids": [p["pid"] for p in batch],
                                              "tag": tag, "model": args.model, **meta},
                                             ensure_ascii=False), encoding="utf-8")
            time.sleep(0.05)
        if not content.strip():
            raise ValueError("empty content")
        rows, dropped, missing = parse(content, batch)
        if missing:
            raise ValueError(f"missing_ids:{len(missing)}")
        return rows, dropped, meta

    def work(batch):
        # thinking is the default; a truncated or malformed batch is retried without it
        attempts = [(batch, True), (batch, False)] if args.think else [(batch, False)]
        for this_batch, think in attempts:
            try:
                rows, dropped, meta = run(this_batch, think)
                return finish(batch, rows, meta, dropped, [p["pid"] for p in batch])
            except Exception as exc:
                reason = type(exc).__name__
        with lock, fail_path.open("a", encoding="utf-8") as fh:
            fh.write(f"{batch[0]['pid']},batch_{reason}\n")
        rows, ok = [], []
        for p in batch:
            try:
                part, _, meta = run([p], False)
                rows += part
                ok.append(p["pid"])
            except Exception as exc2:
                with lock, fail_path.open("a", encoding="utf-8") as fh:
                    fh.write(f"{p['pid']},{type(exc2).__name__}\n")
        return finish(batch, rows, {}, 0, ok)

    totals = {"rows": 0, "dropped": 0, "reasoning": 0, "completion": 0}
    # free tiers cap requests per minute, not concurrency: space the calls globally
    rate_lock, next_slot = threading.Lock(), [0.0]
    # a proxy can hold a socket open past the client timeout; if nothing lands for 15
    # minutes the run is wedged, so stop loudly instead of sitting idle
    heartbeat = [time.time()]

    def watchdog():
        while True:
            time.sleep(30)
            idle = time.time() - heartbeat[0]
            if idle > 900:
                print(f"WATCHDOG: no result for {int(idle)}s, exiting; re-run to fill the gap",
                      flush=True)
                os._exit(3)

    def throttle():
        if not args.rpm:
            return
        with rate_lock:
            now = time.time()
            wait = next_slot[0] - now
            next_slot[0] = max(now, next_slot[0]) + 60.0 / args.rpm
        if wait > 0:
            time.sleep(wait)

    def finish(batch, rows, meta, dropped, ok_pids):
        with lock:
            written = 0
            with items_path.open("a", encoding="utf-8") as fh:
                for r in rows:
                    key = (r["pid"], r["topic"], r["dimension"], r["direction"], r["evidence"])
                    if key in seen_rows:      # a crash between the two appends must not duplicate
                        continue
                    seen_rows.add(key)
                    r["model"] = args.model
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                    written += 1
            with done_path.open("a", encoding="utf-8") as fh:
                for pid in ok_pids:
                    fh.write(json.dumps({"pid": pid}) + "\n")
            totals["rows"] += written
            heartbeat[0] = time.time()
            totals["dropped"] += dropped
            totals["reasoning"] += meta.get("reasoning_chars", 0)
            totals["completion"] += (meta.get("usage") or {}).get("completion_tokens", 0)
        return written

    threading.Thread(target=watchdog, daemon=True).start()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for n, _ in enumerate(pool.map(work, batches), 1):
            print(f"{n}/{len(batches)} batches | items {totals['rows']} | "
                  f"reasoning {totals['reasoning']//1000}k chars", flush=True)
    print(f"DONE think={args.think} batches={len(batches)} items={totals['rows']} "
          f"dropped={totals['dropped']} completion_tokens={totals['completion']}")


if __name__ == "__main__":
    main()
