# github-roast

[![CI](https://github.com/npow/github-roast/actions/workflows/ci.yml/badge.svg)](https://github.com/npow/github-roast/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Rank and vet OSS contributors in minutes — detects PR farming, analyzes real engagement, and gives you a scored report.

## The problem

Evaluating GitHub profiles for GSoC, hiring, or program admissions takes hours per candidate. Applicants game it: submitting dozens of trivial PRs across unrelated repos in the weeks before a deadline inflates their apparent activity without demonstrating any real skill. Eyeballing PR counts and commit graphs doesn't catch this — and neither do automated tools that look at quantity, not quality.

## Quick start

```bash
# Install
git clone https://github.com/npow/github-roast && cd github-roast && uv sync

# Analyze any GitHub user
github-roast torvalds

# Rank a GSoC cohort
github-roast --repo netflix/metaflow --label gsoc --output report.md
```

## Install

```bash
git clone https://github.com/npow/github-roast
cd github-roast
uv sync
```

Requires the `gh` CLI authenticated with a GitHub account (`gh auth login`) and [agent-relay](https://github.com/npow/claude-relay) running locally.

## Usage

**Analyze any GitHub user** — no repo required:

```bash
github-roast torvalds
github-roast torvalds --format json
```

**Deep-dive with target repo** — adds in-depth PR analysis for a specific repo:

```bash
github-roast npow --repo netflix/metaflow
```

**Bulk ranking** — rank all contributors who opened a PR with a given label:

```bash
github-roast --repo netflix/metaflow --label gsoc --output report.md
```

Output includes a ranked table with merge rate, PRs/week, burst ratio, and trivial-PR rate — the key farming signals — followed by detailed profiles with actual PR discussion excerpts.

**Web UI** — browser-based interface with live progress streaming:

```bash
uv run uvicorn webapp:app --reload
# Visit http://localhost:8000
```

## How it works

For each contributor, github-roast:

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
cd github-roast
uv sync
gh auth login   # if not already authenticated
github-roast youruser
```

## Deploy to Hetzner (`hetzner-recon`)

This repo includes a production deploy workflow: `.github/workflows/deploy-hetzner.yml`.

It deploys on every push to `master` and:

1. SSHes into `hetzner-recon`
2. Rsyncs the repo to `/root/github-roast`
3. Runs `scripts/deploy_remote.sh` to:
   - create/update Python venv and install runtime deps
   - update `.env.production` with CLIProxyAPI runtime (`ANTHROPIC_BASE_URL`)
   - restart a `github-roast` systemd service on `0.0.0.0:8011`
   - patch `/root/recon/Caddyfile` and reload `recon-caddy-1`

### GitHub Actions secrets

Set these repository secrets:

- `HETZNER_HOST` — SSH host (IP or DNS name)
- `HETZNER_USER` — SSH user
- `SSH_PRIVATE_KEY` — private key used by Actions
- `CLIPROXY_API_KEY` — CLIProxyAPI key for runtime LLM calls
- `CLIPROXY_BASE_URL` (optional) — defaults to `http://127.0.0.1:8317`
- `DEPLOY_GH_TOKEN` (optional) — token for `gh` CLI runtime auth on server

### One-time DNS setup

Create an `A` record:

- `gh-roast.deeprecon.app` -> `<hetzner-recon public IPv4>`

### Host prerequisites

On the server, ensure:

- `uv` is installed
- `caddy` is installed and running (for TLS + reverse proxy)
- deploy user has sudo (passwordless for systemd/Caddy reload)

## License

Apache 2.0 — see [LICENSE](LICENSE)
