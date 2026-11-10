# fomc-stance-coder

English | [简体中文](README.zh-CN.md)

Download FOMC statements and minutes from the Federal Reserve, extract section-level text, and code each paragraph's inflation and labor-market stance with an OpenAI-compatible LLM. Aggregation code turns paragraph judgments into meeting-level variables, so the model never writes the final numbers.

## Pipeline

1. **Fetch.** Crawl the Fed's FOMC index pages for 2008–2025, save each page as raw HTML, and record provenance in `sources.csv`.
2. **Extract.** Pull the document body from each page, mark headings with `## `, assign canonical section types, and write plain text, `sections.csv`, and `manifest.csv`.
3. **Rule baseline.** Split sentences and clauses, match topic and dimension cues with hand-written patterns, and aggregate a net direction share. Units with no resolved direction or conflicting cues stay flagged for review.
4. **LLM coding.** Send paragraphs in batches grouped by meeting and document. The model returns judgments on fixed axes, and every item must quote its evidence verbatim or it is dropped. The script caches answers, so a rerun resumes where the last one stopped.
5. **Aggregate.** Turn paragraph directions into meeting × document × topic × dimension variables: `net`, high-confidence `net`, `thin`, mixed counts, explicit-neutral counts, and state + change composites.

The rule baseline runs without a model. Both coding layers read statement bodies and the minutes section that reports participants' views.

## Coding scheme

Topics:

| Code | Meaning |
|---|---|
| `inf` | inflation |
| `lab` | labor market |

Dimensions:

| Code | Meaning |
|---|---|
| `state` | current level against the 2 percent goal or full employment |
| `change` | recent direction of movement |
| `outlook` | the Committee's stated expectation about the future |

Direction is `-1`, `0`, or `1`. The prompt tells the model to skip sentiment and good/bad news, to keep policy intentions out of economic judgments, and to keep inflation expectations out of the Committee's outlook.

## Repository layout

```
fomc-stance-coder/
├── _fetch/fetch_fomc_text.py     # download raw FOMC pages
├── _extract/extract_text.py      # HTML to text, sections, manifest
├── _code/code_sentences.py       # rule-based sentence coding
├── _code/llm_code.py             # LLM paragraph coding
├── _code/llm_aggregate.py        # meeting-level variables
├── _code/profiles.example.json   # copy to profiles.json
├── requirements.txt
├── README.md
└── README.zh-CN.md
```

Generated data stays out of git. The scripts create `FOMC statement/`, `FOMC minutes/`, `extracted/`, and `coded/` at the repository root.

## Requirements

Python 3.9 or newer.

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## Quick start

Download and extract:

```bash
python _fetch/fetch_fomc_text.py
python _extract/extract_text.py
python _code/code_sentences.py   # optional rule baseline
```

Configure a model:

```bash
cp _code/profiles.example.json _code/profiles.json
export LLM_API_KEY=your-key      # Windows PowerShell: $env:LLM_API_KEY="your-key"
python _code/llm_code.py --list-profiles
python _code/llm_code.py --ping
```

Code a sample:

```bash
python _code/llm_code.py --pilot 12 --workers 2
```

Code a fixed set of meetings. The CSV needs a `conference_date` column in `YYYY-MM-DD` form:

```bash
python _code/llm_code.py --event-dates dates.csv --workers 8
```

Code the full archive:

```bash
python _code/llm_code.py --all --workers 8
```

Aggregate:

```bash
python _code/llm_aggregate.py --items coded/llm/<tag>/items.jsonl
```

The script writes `variables.csv`, `paragraph_scores.csv`, and `composites.csv` next to `items.jsonl`. It refuses to overwrite existing output files.

## Configuration

`_code/profiles.json` holds named profiles. Each profile sets `base_url`, `model`, `key` (a slot name), `workers`, `timeout`, and optional `rpm`, `think`, `effort`, and `think_off`.

Key lookup order:

1. `--api-key`
2. `LLM_API_KEY` environment variable
3. `_code/keys.json`, keyed by the slot name in the profile
4. `_code/api_key.txt`

`keys.json` looks like this:

```json
{"deepseek": "sk-...", "openrouter": "sk-or-..."}
```

A local server works through the same interface. Point `base_url` at `http://127.0.0.1:<port>/v1` and leave the key empty.

Keep `profiles.json`, `keys.json`, and `api_key.txt` out of git. `.gitignore` already lists them.

Useful flags:

| Flag | Effect |
|---|---|
| `--pilot N` | evenly spaced meetings as a sample |
| `--event-dates file.csv` | restrict to dates in the CSV |
| `--all` | code the full 2008–2025 archive |
| `--only-doc statement\|minutes` | code one document type |
| `--limit N` | cap the number of requests |
| `--workers N` | concurrent requests |
| `--rpm N` | global requests-per-minute cap |
| `--reparse` | rebuild `items.jsonl` from cached answers |
| `--tag name` | set the run folder name |

## Outputs

| Path | Content |
|---|---|
| `FOMC statement/`, `FOMC minutes/` | raw HTML and `sources.csv` |
| `extracted/` | section text, `sections.csv`, `manifest.csv` |
| `coded/*.csv` | rule-layer sentence scores and meeting variables |
| `coded/llm/<tag>/items.jsonl` | retained paragraph judgments |
| `coded/llm/<tag>/processed.jsonl` | paragraphs already coded |
| `coded/llm/<tag>/failures.csv` | failed paragraph IDs |
| `coded/llm/<tag>/variables.csv` | aggregated meeting-level variables |
| `coded/llm/cache/` | raw model answers |

## Checks in the LLM layer

- Evidence must be a contiguous quote of at most 15 words. An ellipsis is allowed when the quoted fragments appear in order.
- The evidence or its surrounding window must contain a topic word.
- Asset-price phrases do not count as inflation.
- Inflation expectations do not count as the Committee's outlook.
- Aggregation runs in code. Model output never becomes the final variable.
- A failed batch retries without thinking, then falls back to one paragraph per request. Failures land in `failures.csv`.
- A watchdog stops a run after 15 minutes without a result. Rerunning fills the gap.

## Data source and limitations

- Source: public FOMC pages on `federalreserve.gov`, covering 2008–2025.
- The fetch script stores raw HTML unchanged. The extractor repairs text on pages that arrived with the wrong encoding.
- Minutes from 2008–2009 carry no section headings, so the participants' views section cannot be located automatically. Both coding layers skip those documents.
- The 2020-03-03 meeting has no separate minutes.
- The 2011-04-27 minutes have a heading that wraps across lines. The extractor handles that boundary.
- The rule baseline has limited coverage. Units with no resolved direction or conflicting cues need manual review.
- Coding results depend on the model and the prompt version. Spot-check against the source text before using them in analysis.

FOMC statements and minutes are public Federal Reserve publications. Follow the Fed's site terms when redistributing them.

## License

MIT. See [LICENSE](LICENSE).

This is a research tool. It is not affiliated with the Federal Reserve.
