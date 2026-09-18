# AGENTS.md

This file provides guidance to the AI agent when working with code in this repository.

## What this repo is

The distributable (sanitized) version of the `job-application-copilot` skill — a Chinese-language job-application CRM. `SKILL.md` (frontmatter + body) **is the product**; scripts under `scripts/` support it. All user-facing content, filenames, CSV columns, and comments are Chinese — keep them in Chinese when editing.

## Non-obvious constraints

- `references/` is mentioned throughout SKILL.md but **does not exist in this repo** (removed during sanitization). Do not try to read or recreate files referenced from it.
- Real user data lives outside the repo in `~/.workbuddy/career-facts/` (Windows: `%USERPROFILE%\.workbuddy\career-facts\`). That fact library is **user-maintained; read-only for the skill** — never write to it without explicit user confirmation of the formatted change.
- Never commit real resumes, JD pools, or anything with name/contact info. Only虚构 examples belong here (`resumes/示例_*.md`, `jd-pool/examples/`).
- **Evidence rule (red line):** no strategy/match/market claim from model memory alone — every conclusion must carry a URL, an IMA-cited summary, or a career-facts ID (E-xx/P-xx/M-xx). Numbers without source + caliber (口径) never enter resumes or metrics.
- In `scripts/match_jd.py`, `load_corpus()` excludes `tags.md` via `FACTS_EXCLUDE` — **do not remove that exclusion** (tags.md's first column mirrors keyword category names and would make every category trivially match).
- **Never add fields to the `MATCHMETA` line**, and never write apply state into jd-pool files — a 2026-09-14 incident: adding a trailing field broke tail-anchored regexes in downstream scripts and nearly archived the entire 115-job pool. Application state lives in `pipeline.csv`; `jobId`/`jobKind` live in the shortlist csv.
- 猎聘 MCP token lives only in `~/.workbuddy/mcp.json` (`x-user-token` header), read at runtime by `scripts/liepin_cli.py`. **Never print it, never copy it into the repo, never commit capture files** (`--raw-out` outputs contain real job data — keep them in temp dirs).
- `apply_shortlist.py` performs **external, irreversible actions** (real job applications via official `user-apply-job`). Default is dry-run; `--confirm` requires explicit user go-ahead. Never run `--confirm` autonomously.

## Build / test / run

- Python 3, stdlib only — no requirements.txt, no pip install.
- Regression tests (17 groups, **no pytest**):
  `python scripts/tests/test_p1_regression.py`
- Key script invocations (from repo root):
  - `python scripts/find_jobs.py [--round 占坑,练手] [--dry-run]` (job-hunting pipeline: 猎聘 search → 4-layer dedupe → round filter → 15-job shortlist; never applies)
  - `python scripts/apply_shortlist.py --list jd-pool/短名单_YYYYMMDD.csv [--confirm]` (batch apply; dry-run by default, `--max 15`)
  - `python scripts/liepin_cli.py search --job-name X [--raw-out capture.json] | apply --job-id N --job-kind K --dry-run | resume | probe`
  - `python scripts/match_jd.py <jd-file> --company X --title Y --save --source 主动搜索 --purpose <轮次>`
  - `python scripts/rank_pool.py` (regenerates `jd-pool/RANKED_LIST.md`; `--no-link-check` to skip URL probing)
  - `python scripts/ats_check.py <resume.html> --jd <jd-file>`
  - `python evals/check_citations.py {check|stmt|summary} ...` (see `evals/README.md`)
  - `python evals/check_scorecard.py [--log run.txt] [--record]` (pipeline scorecard: 8 binary items, additive scoring; see `evals/scorecard.md`)
- Scripts resolve paths relative to their own location (`Path(__file__).parent.parent`), so they operate on whatever repo dir they sit in. Real jd-pool tier dirs (`jd-pool/green|yellow|red/`) exist only in installed copies.

## Data/file quirks

- `pipeline.csv` starts with a UTF-8 BOM — read as `utf-8-sig`, but **append as plain `utf-8`** (utf-8-sig in append mode re-emits a BOM mid-file and corrupts the first column; same rule for `jd-pool/shown_log.csv` and shortlist csvs).
- Each JD file's first line is a machine-readable `<!-- MATCHMETA tier=... score=... ... -->` comment; `rank_pool.py` parses only that format.
- Commit messages are Conventional Commits in Chinese or English (`feat:`/`chore:`), single-line.
