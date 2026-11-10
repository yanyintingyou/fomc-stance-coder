"""Aggregate unique paragraph directions, then equally weight valid paragraphs.

A paragraph is valid only when it has at least one retained item. Explicit
neutrality means directions == {0}; mixed means both -1 and +1 are present.
n_items counts retained input entries, including repeated directions, for audit
only. thin (<3 paragraphs) is descriptive, never an exclusion or a regression
precision weight. Composite count weights are not regression precision weights.
Existing output files are never overwritten.
"""
import argparse
import csv
import json
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY_FIELDS = ["doc_type", "meeting_date", "topic", "dimension"]
VARIABLE_FIELDS = KEY_FIELDS + [
    "n_pos", "n_neg", "n_neutral", "n_units", "net", "net_highconf", "thin",
    "n_mixed", "n_explicit_neutral", "n_items", "n_units_highconf",
]
PARAGRAPH_FIELDS = KEY_FIELDS + [
    "pid", "directions", "score", "mixed", "explicit_neutral", "n_items",
    "directions_highconf", "score_highconf", "mixed_highconf",
    "explicit_neutral_highconf", "n_items_highconf",
]
COMPOSITE_FIELDS = ["doc_type", "meeting_date", "topic", "state", "change",
                    "n_state", "n_change", "simple", "weighted", "outlook", "n_outlook"]


def selected_pids(items, pid_file=None):
    """Use current llm_code scope, restricted to dates represented in items.

    An optional UTF-8 file with one PID per line replaces the current scope.
    Importing llm_code does not run its CLI or resolve/read credentials.
    """
    if pid_file is not None:
        return {line.strip() for line in Path(pid_file).read_text(
            encoding="utf-8-sig").splitlines() if line.strip()}
    import llm_code

    dates = {r["meeting_date"] for r in items}
    return {p["pid"] for p in llm_code.paragraphs() if p["date"] in dates}


def direction_stats(directions):
    if not directions:
        return "", "", ""
    return (sum(directions) / len(directions),
            int(-1 in directions and 1 in directions), int(directions == {0}))


def aggregate(items, allowed_pids):
    """Return variables, paragraph audit rows and composites without file I/O."""
    groups = {}
    for r in items:
        if r["pid"] not in allowed_pids:
            continue
        direction = r["direction"]
        if type(direction) is not int or direction not in (-1, 0, 1):
            raise ValueError(f"Invalid direction for {r['pid']}: {direction!r}")
        key = tuple(r[f] for f in KEY_FIELDS) + (r["pid"],)
        g = groups.setdefault(key, {"directions": set(), "high": set(),
                                    "n_items": 0, "n_high": 0})
        g["directions"].add(direction)
        g["n_items"] += 1
        # Filter entries before deduplicating: a low-confidence repeat cannot
        # hide a later high-confidence item with the same direction.
        if r.get("confidence") == "high":
            g["high"].add(direction)
            g["n_high"] += 1

    paragraphs = []
    meetings = defaultdict(list)
    for key, g in sorted(groups.items()):
        score, mixed, neutral = direction_stats(g["directions"])
        hi_score, hi_mixed, hi_neutral = direction_stats(g["high"])
        row = dict(zip(KEY_FIELDS, key[:4]))
        row.update({
            "pid": key[4], "directions": json.dumps(sorted(g["directions"])),
            "score": score, "mixed": mixed, "explicit_neutral": neutral,
            "n_items": g["n_items"],
            "directions_highconf": json.dumps(sorted(g["high"])),
            "score_highconf": hi_score, "mixed_highconf": hi_mixed,
            "explicit_neutral_highconf": hi_neutral, "n_items_highconf": g["n_high"],
        })
        paragraphs.append(row)
        meetings[key[:4]].append(row)

    variables = []
    for key, rows in sorted(meetings.items()):
        scores = [r["score"] for r in rows]
        high = [r["score_highconf"] for r in rows if r["score_highconf"] != ""]
        row = dict(zip(KEY_FIELDS, key))
        row.update({
            "n_pos": sum(s > 0 for s in scores),
            "n_neg": sum(s < 0 for s in scores),
            "n_neutral": sum(s == 0 for s in scores),
            "n_units": len(scores), "net": sum(scores) / len(scores),
            "net_highconf": sum(high) / len(high) if high else "",
            "thin": int(len(scores) < 3),
            "n_mixed": sum(r["mixed"] for r in rows),
            "n_explicit_neutral": sum(r["explicit_neutral"] for r in rows),
            "n_items": sum(r["n_items"] for r in rows),
            "n_units_highconf": len(high),
        })
        variables.append(row)
    return variables, paragraphs, composite_rows(variables)


def composite_rows(variables):
    groups = defaultdict(dict)
    for row in variables:
        key = tuple(row[f] for f in KEY_FIELDS[:3])
        groups[key][row["dimension"]] = row
    composites = []
    for key, dims in sorted(groups.items()):
        row = dict(zip(KEY_FIELDS[:3], key))
        for dim in ("state", "change", "outlook"):
            value = dims.get(dim)
            row[dim] = value["net"] if value else ""
            row[f"n_{dim}"] = value["n_units"] if value else 0
        if row["n_state"] and row["n_change"]:
            row["simple"] = (row["state"] + row["change"]) / 2
            row["weighted"] = (
                row["n_state"] * row["state"] + row["n_change"] * row["change"]
            ) / (row["n_state"] + row["n_change"])
        else:
            row["simple"] = row["weighted"] = ""
        composites.append(row)
    return composites


def write_outputs(outputs):
    """Reserve every destination exclusively before writing any CSV content."""
    paths = [Path(path).resolve() for path, _, _ in outputs]
    if len(set(paths)) != len(paths):
        raise ValueError("Output paths must be distinct")
    for path in paths:
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    with ExitStack() as stack:
        handles = [stack.enter_context(path.open("x", newline="", encoding="utf-8-sig"))
                   for path in paths]
        for fh, (_, fields, rows) in zip(handles, outputs):
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--items", required=True, help="items.jsonl produced by llm_code.py")
    ap.add_argument("--out", default="", help="Variables CSV (must not exist)")
    ap.add_argument("--paragraph-out", default="", help="Paragraph CSV (must not exist)")
    ap.add_argument("--composites-out", default="", help="Composites CSV (must not exist)")
    ap.add_argument("--pid-file", default="", help="Explicit scope: UTF-8 file, one PID per line")
    args = ap.parse_args()
    src = Path(args.items)
    out = Path(args.out) if args.out else src.parent / "variables.csv"
    paragraph_out = Path(args.paragraph_out) if args.paragraph_out else out.parent / "paragraph_scores.csv"
    composites_out = Path(args.composites_out) if args.composites_out else out.parent / "composites.csv"
    with src.open(encoding="utf-8-sig") as fh:
        items = [json.loads(line) for line in fh if line.strip()]
    allowed = selected_pids(items, args.pid_file or None)
    variables, paragraphs, composites = aggregate(items, allowed)
    write_outputs([
        (out, VARIABLE_FIELDS, variables),
        (paragraph_out, PARAGRAPH_FIELDS, paragraphs),
        (composites_out, COMPOSITE_FIELDS, composites),
    ])
    retained = sum(r["n_items"] for r in paragraphs)
    print(f"items={len(items)} retained={retained} excluded={len(items) - retained} "
          f"paragraph_scores={len(paragraphs)} variables={len(variables)} "
          f"meetings={len({r['meeting_date'] for r in variables})} -> {out}")


if __name__ == "__main__":
    main()
