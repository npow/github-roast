# gh-roast

[![CI](https://github.com/npow/github-roast/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/github-roast/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Rank and vet OSS contributors in minutes — detects PR farming, analyzes real engagement, and gives you a scored report.

## The problem

Evaluating GitHub profiles for GSoC, hiring, or program admissions takes hours per candidate. Applicants game it: submitting dozens of trivial PRs across unrelated repos in the weeks before a deadline inflates their apparent activity without demonstrating any real skill. Eyeballing PR counts and commit graphs doesn't catch this — and neither do automated tools that look at quantity, not quality.

## Quick start

```bash
# Requires: gh CLI logged in, ANTHROPIC_BASE_URL set
uv sync

# Analyze a cohort by label
python analyze.py --repo netflix/metaflow --label gsoc --output report.md

# Analyze a single contributor
python analyze.py --repo netflix/metaflow --user someuser
```

## Install

```bash
# Clone and install deps with uv
git clone https://github.com/npow/github-roast
cd gh-roast
uv sync

# Set your Anthropic relay (or use the API directly)
export ANTHROPIC_BASE_URL=http://localhost:18082
```

Requires the `gh` CLI authenticated with a GitHub account (`gh auth login`).

## Usage

**Bulk ranking** — analyze all contributors who opened a PR with a given label:

```bash
python analyze.py --repo netflix/metaflow --label gsoc --format markdown --output report.md
```

Output includes a ranked table with merge rate, PRs/week, burst ratio, and trivial-PR rate — the key farming signals — followed by detailed profiles with actual PR discussion excerpts.

**Single user** — deep-dive a specific contributor:

```bash
python analyze.py --repo netflix/metaflow --user npow --format json
```

**Web UI** — run the FastAPI app for a browser-based interface with live progress streaming:

```bash
uv run uvicorn webapp:app --reload
# Visit http://localhost:8000
```

## How it works

For each contributor, gh-roast:

1. Fetches the last 90 days of public GitHub events (commits, PRs, reviews, comments)
2. Pulls their top repos and reads actual README content, file trees, and language stats
3. Samples up to 12 cross-repo PRs (one per org) and fetches the actual discussion threads
4. Computes farming signals: merge rate, PRs/week, 90-day burst ratio, trivial-PR rate, reviewer engagement
5. Runs a per-PR LLM classification on target-repo PRs (substantive vs. manufactured)
6. Generates a holistic LLM assessment that leads with the computed signals — preventing the LLM from being fooled by high PR counts or long account age

Results are cached in SQLite (6h for GitHub API calls, 24h for LLM results) so re-runs are fast.

## Configuration

| Env var | Default | Description |
|---------|---------|-------------|
| `ANTHROPIC_BASE_URL` | `http://localhost:18082` | Anthropic API endpoint |

The `gh` CLI handles GitHub auth — no tokens to manage.

## Development

```bash
git clone https://github.com/npow/github-roast
cd gh-roast
uv sync
gh auth login   # if not already authenticated
python analyze.py --user youruser --repo owner/repo
```

## License

Apache 2.0 — see [LICENSE](LICENSE)
