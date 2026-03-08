# Spec: GitHub Activity Profiler
**Status:** Draft
**Date:** 2026-03-08
**Author:** Claude

## Problem Statement

Technical hiring committees and open-source program administrators (such as Google Summer of Code) are routinely deceived by candidates who present inflated GitHub portfolios. A candidate may list dozens of merged PRs to well-known projects, but inspection reveals these are documentation tweaks, whitespace fixes, or solutions to manufactured issues that no real user ever filed. Similarly, candidates present personal projects that appear substantial on a README but are either AI-generated ("vibe coded") or thin wrappers with no architectural depth. Human reviewers lack the time — and often the project-specific context — to distinguish a substantive contribution from a performative one. The result is that the signal-to-noise ratio in portfolio evaluation has collapsed, rewarding candidates who game metrics over those who do genuine engineering work.

## Goals

- For any GitHub username, produce a scored contribution report in under 5 minutes covering all public repositories and PRs from the past 3 years
- Classify each external PR into one of: `substantive-code`, `trivial-code`, `docs-only`, `test-only`, `config-only`, or `manufactured` (self-created issue + self-PR with minimal maintainer engagement)
- Detect vibe-coded / AI-generated repositories with >= 80% precision and report the signals that triggered the classification
- Score design discussion quality per PR on a 0–10 scale based on: thread depth, maintainer feedback cycles, technical specificity of comments, and whether reviewer objections were substantively addressed
- Produce a human-readable HTML/Markdown report that an evaluator can read in under 10 minutes and reach a defensible hiring decision
- Expose a JSON output mode for integration into ATS or program management tooling

## Non-Goals

- Private repository analysis — the tool operates on the public GitHub API only
- Real-time or continuous monitoring — this is a point-in-time snapshot tool, not a dashboard
- Ranking candidates against each other — the tool scores individuals in isolation; comparative ranking is the evaluator's job
- Code correctness verification — the tool assesses signals of quality, not whether the code is bug-free
- Resume or LinkedIn cross-referencing — the tool only analyzes GitHub activity

## Background and Context

The "portfolio gaming" problem has become acute since 2022, as LLMs made it trivial to generate plausible-looking code, write passable PR descriptions, and even fabricate convincing GitHub activity. GSoC in particular has seen applicants bulk-submit PRs to popular repositories in the weeks before application deadlines, exploiting the fact that many maintainers merge small PRs quickly without scrutiny. Prior art in this space is limited: `github-profile-summary-cards` and similar tools count activity but do not assess quality. Static analysis tools (Sonarqube, CodeClimate) assess code but require repository access and produce per-file metrics that are not synthesized into a portfolio-level signal. This tool bridges that gap by combining GitHub API metadata analysis, lightweight static analysis on cloned repositories, and LLM-based qualitative assessment of PR discussions and code structure.

## Design

### API / Interface

Primary interface is a CLI:

```bash
# Basic usage — produces HTML report
ghprofile analyze <github-username>

# Full options
ghprofile analyze <github-username> \
  --since 2024-01-01 \           # only analyze activity after this date
  --output report.html \          # default: <username>-report.html
  --format html|markdown|json \  # default: html
  --depth full|quick \           # quick skips repo cloning; full does static analysis
  --focus prs|repos|all \        # default: all
  --llm-model claude-sonnet-4-6  # model used for qualitative analysis

# Output a machine-readable JSON summary (for ATS integration)
ghprofile analyze <github-username> --format json --output profile.json

# List what would be analyzed without running
ghprofile analyze <github-username> --dry-run
```

Environment variables:
```bash
GITHUB_TOKEN=<pat>                         # required; read:user, public_repo scopes
ANTHROPIC_BASE_URL=http://localhost:18082  # relay endpoint (default); override to hit API directly
GHPROFILE_CACHE_DIR=~/.cache/ghprofile     # where cloned repos and API responses are cached
```

LLM calls are routed through the local `agent-relay` server (an Anthropic-compatible proxy that forwards requests through Claude Code). Start it with `agent-relay serve` before running analysis. No separate API key is required when using the relay.

Python library interface (for embedding in other tools):

```python
from ghprofile import Profiler, ReportFormat

profiler = Profiler(github_token="...", llm_base_url="http://localhost:18082")

profile = await profiler.analyze(
    username="octocat",
    since="2024-01-01",
    depth="full",
)

# Access structured data
for pr in profile.external_prs:
    print(pr.repo, pr.classification, pr.discussion_score)

for repo in profile.owned_repos:
    print(repo.name, repo.vibe_code_score, repo.vibe_code_signals)

# Render report
report = profile.render(format=ReportFormat.HTML)
```

### Data Model

```python
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime

class PRClassification(Enum):
    SUBSTANTIVE_CODE = "substantive-code"   # real logic change, non-trivial
    TRIVIAL_CODE = "trivial-code"           # code change but minimal complexity
    DOCS_ONLY = "docs-only"                 # pure documentation
    TEST_ONLY = "test-only"                 # tests with no production code change
    CONFIG_ONLY = "config-only"             # CI, deps, build config only
    MANUFACTURED = "manufactured"           # self-created issue, quick rubber-stamp

@dataclass
class PRAnalysis:
    url: str
    repo: str                           # owner/repo
    repo_stars: int                     # proxy for project significance
    repo_monthly_active_contributors: int
    title: str
    merged_at: datetime
    lines_added: int
    lines_removed: int
    files_changed: int
    code_lines_changed: int             # excluding docs, tests, generated
    doc_lines_changed: int
    test_lines_changed: int
    linked_issue_id: int | None
    issue_reporter_is_author: bool      # red flag: candidate filed the issue themselves
    issue_age_days: int | None          # how old was the issue before the PR? new = suspicious
    issue_comment_count: int | None     # engagement before PR
    time_to_merge_hours: float          # raw signal; LLM interprets in context of repo norms
    repo_median_merge_hours: float | None  # computed from repo PR history if available; context for LLM
    review_cycles: int                  # number of review request → push → re-review iterations
    reviewer_comment_count: int
    author_response_count: int          # how many times author replied to reviewers
    reviewer_approved_without_comment: bool  # raw signal passed to LLM
    # All raw signals above are passed as context to a single LLM call that produces:
    discussion_score: float             # 0.0–10.0, LLM-assessed
    classification: PRClassification   # LLM-assessed; no hardcoded thresholds applied
    classification_rationale: str       # human-readable explanation citing specific signals
    significance_score: float           # 0.0–10.0 overall

@dataclass
class VibeCodeSignal:
    signal: str          # e.g. "all_code_in_initial_commit"
    description: str     # human-readable explanation
    severity: str        # "low" | "medium" | "high"

@dataclass
class RepoAnalysis:
    name: str                           # owner/repo
    url: str
    stars: int
    forks: int
    created_at: datetime
    commit_count: int
    contributor_count: int
    languages: dict[str, int]           # language → bytes
    has_tests: bool
    test_coverage_estimate: float | None  # None if cannot be determined
    has_ci: bool
    readme_word_count: int
    readme_is_generic: bool             # boilerplate detection
    initial_commit_code_fraction: float # fraction of total code in first commit
    commit_message_quality_score: float # 0.0–10.0, LLM-assessed
    code_quality_score: float           # 0.0–10.0, LLM-assessed (sampled files)
    architecture_depth_score: float     # 0.0–10.0; shallow = thin wrapper
    vibe_code_score: float              # 0.0–10.0; higher = more likely AI-generated
    vibe_code_signals: list[VibeCodeSignal]
    engineering_rigor_summary: str      # LLM narrative, 2-3 sentences

@dataclass
class ContributorProfile:
    username: str
    analyzed_at: datetime
    account_age_days: int
    pr_burst_detected: bool             # many PRs filed in short window before deadline
    pr_burst_window: str | None         # e.g. "2024-02-01 to 2024-02-28"

    # External contributions (PRs to repos the candidate does not own)
    external_prs: list[PRAnalysis]
    pr_classification_counts: dict[str, int]
    substantive_pr_count: int           # convenience accessor
    manufactured_pr_count: int

    # Owned repositories
    owned_repos: list[RepoAnalysis]
    vibe_coded_repo_count: int          # repos with vibe_code_score >= 7.0

    # Aggregate scores
    contribution_quality_score: float   # 0.0–10.0
    portfolio_authenticity_score: float # 0.0–10.0; penalizes gaming signals
    overall_score: float                # 0.0–10.0 weighted composite

    # Narrative
    executive_summary: str              # LLM-written, 4-6 sentences
    red_flags: list[str]                # specific concerns for evaluator
    strengths: list[str]                # genuine positive signals
```

### Workflow / Sequence

```
Phase 1 — Data Collection (GitHub API)
  1. Fetch user profile metadata (account age, bio, follower count)
  2. Fetch all public repositories owned by the user
  3. For each owned repo: fetch commit history, contributors, languages, issues
  4. Search for all PRs authored by user across GitHub (search API)
  5. For each external PR: fetch PR metadata, diff stats, review thread, linked issue
  6. Detect PR burst: flag if > 5 PRs filed to external repos within any 30-day window
     that coincides with a known GSoC/hiring deadline (or if date range is suspicious)
  7. Cache all API responses to GHPROFILE_CACHE_DIR (TTL: 24h)

Phase 2 — Static Analysis (requires --depth full)
  8. Clone each owned repo (shallow clone, last 500 commits)
  9. Run language-appropriate linter (pylint/eslint/golint) on sampled files
  10. Detect test presence and estimate coverage via coverage config / badge scraping
  11. Compute initial-commit code fraction (git log --follow)
  12. Analyze commit message corpus for quality signals

Phase 3 — LLM Analysis
  13. For each external PR: single LLM call combining discussion quality scoring and classification.
      Inputs: PR title, description, diff stat breakdown (code/docs/test/config line counts),
      full review thread, issue metadata (reporter, age, prior comment count), time_to_merge_hours,
      repo_median_merge_hours (if available), review_cycles, reviewer_approved_without_comment.
      LLM returns structured JSON: { discussion_score, classification, classification_rationale }.
      No hardcoded thresholds — the LLM reasons about whether the merge was fast *for this repo*,
      whether filing your own issue is suspicious *given the issue's history*, etc.
  15. For each owned repo: send sampled source files (up to 20KB) to LLM for
      code quality, architecture depth, and vibe-code assessment
  16. Generate executive summary and red flags list

Phase 4 — Scoring
  17. Compute per-PR significance_score as weighted function of:
      classification weight × repo_stars_normalized × discussion_score × (1 - rubber_stamp_penalty)
  18. Compute contribution_quality_score as mean significance_score across substantive PRs only
  19. Compute portfolio_authenticity_score; deduct for: manufactured PRs, burst patterns,
      vibe-coded repos, rubber-stamp approvals
  20. Compute overall_score as: 0.4 × contribution_quality + 0.3 × authenticity + 0.3 × repo_quality

Phase 5 — Report Generation
  21. Render HTML report with: executive summary, score breakdown, PR table with
      classifications, per-repo cards with vibe-code signals, red flags callout
  22. Write JSON summary if --format json requested
```

### Key Design Decisions

| Decision | Options Considered | Chosen | Rationale |
|---|---|---|---|
| LLM vs rule-only for PR classification | Pure heuristics, LLM-only, hybrid | Hybrid: heuristics for structure, LLM for discussion quality | Heuristics are fast and auditable; LLM adds judgment on nuanced text. Pure LLM is expensive and slow for high-volume PRs |
| GitHub API vs scraping | GitHub REST API, GraphQL API, scraping | REST for metadata, GraphQL for PR threads | REST is simpler; GraphQL is necessary for PR review thread structure which REST exposes poorly |
| Repository cloning | Full clone, shallow clone, no clone | Shallow clone (last 500 commits, --depth full only) | Full clone is too slow for large repos; shallow gives enough history for commit pattern analysis |
| Vibe-code detection approach | LLM review of code, static signals only, hybrid | Hybrid: structural signals (commit pattern, initial-commit fraction) + LLM code review | Structural signals are fast and not fooled by polished prose; LLM catches semantic emptiness |
| PR burst detection | Absolute count threshold, relative to account age, deadline-relative | Deadline-relative when deadlines known, else 30-day rolling window | Deadline-relative is more accurate for GSoC specifically; rolling window handles unknown deadlines |
| Scoring model | Single score, multi-dimensional, pass/fail | Multi-dimensional (4 subscores + composite) | Single score loses too much information; multi-dimensional lets evaluators weight by their priorities |
| Manufactured PR detection | Hardcoded threshold rules, relative/percentile thresholds, LLM holistic judgment | LLM holistic judgment with all raw signals as context | Hardcoded thresholds (e.g. "merged in < 4h") don't generalize across repos with different norms. Percentile-based relative thresholds require enough repo PR history to be meaningful. LLM can reason about *why* a fast merge is suspicious given the specific issue history and review thread — no threshold needed |

## Failure Modes

| Failure | Probability | Impact | Mitigation |
|---|---|---|---|
| GitHub API rate limit exhausted (5000 req/hr for authenticated) | High for active users | High — analysis halted | Prioritize fetching, cache aggressively, implement request budgeting; expose --since flag to narrow scope |
| LLM refuses to evaluate code (safety filter false positive) | Low | Medium — scores missing for affected repos | Catch refusals, fall back to structural signals only, mark score as `partial` in report |
| PR search API missing results (GitHub search is not exhaustive) | Medium | Medium — undercount of contributions | Note coverage gap in report; supplement with user's event feed for recent activity |
| Repo too large to clone within time budget | Medium for prolific users | Low — skip static analysis for that repo | Set 5-minute clone timeout; skip to Phase 3 with API-only data; flag as `partial` |
| False positive vibe-code classification (legitimate project flagged) | Medium | High — reputational harm to candidate | Report signals, not a verdict; require 3+ independent signals for "likely AI-generated" label; always show rationale |
| False negative: sophisticated portfolio gamer not detected | Medium | Medium — tool provides false confidence | Document limitations explicitly in report footer; tool is signal, not verdict |
| LLM hallucination in executive summary | Low | High — incorrect claims about candidate | Ground summary in structured data fields; instruct LLM to cite specific PRs/repos; human review required |
| GitHub token with insufficient scope | High (user error) | High — no data collected | Validate token scopes at startup; print clear error with required scopes |

## Success Metrics

- Full analysis of a user with 50 external PRs and 10 owned repos completes in under 5 minutes at `--depth full`
- PR classification achieves >= 85% agreement with expert human reviewers on a 200-PR labeled test set
- Vibe-code detection achieves >= 80% precision and >= 70% recall on a labeled set of 50 repositories (25 human-authored, 25 AI-generated)
- Report is rated "useful for hiring decision" by >= 8 out of 10 evaluators in user testing
- False positive rate for "manufactured PR" classification <= 5% on a labeled test set
- JSON output schema remains stable across minor versions (breaking changes require major version bump)

## Open Questions

1. Should the tool include a labeled ground-truth dataset for evaluating its own classifier accuracy, or rely on users to validate? — Owner: TBD, Deadline: before v1 release
2. Known GSoC application deadlines could improve burst detection precision — should these be hardcoded, fetched from a config URL, or user-supplied via `--deadline` flag? — Owner: TBD, Deadline: before v1 release
3. Should the tool support GitLab and Gitea, or GitHub only for v1? — Owner: TBD, Deadline: before v1 scoping
4. For the HTML report, should red flags be shown to the candidate (transparency) or only to the evaluator (evaluator-only mode)? — Owner: TBD, Deadline: before UX design

## Appendix

### Vibe-Code Detection Signals (full list)

Structural signals (fast, no LLM):
- `initial_commit_fraction_high`: > 80% of total lines of code present in the first commit (typical of "generate and push" pattern)
- `commit_message_generic`: > 60% of commit messages match patterns like "initial commit", "update", "fix", "add files", "wip"
- `single_contributor`: only one contributor across full history (no collaborators, no accepted PRs from others)
- `no_issues_ever`: zero issues ever filed by anyone other than the author (no external users encountered the project)
- `readme_boilerplate`: README structure matches common AI-generated template (Features / Installation / Usage / Contributing / License with generic prose)
- `no_tests`: no test files detected in any directory
- `burst_created`: repository created within 2 weeks of application deadline
- `dependency_heavy_thin_wrapper`: < 200 lines of original logic but declares > 10 dependencies

LLM-assessed signals:
- `code_reads_better_than_it_works`: fluent variable names, good formatting, but structural issues (missing error handling, hardcoded values, dead code paths)
- `architecture_mismatch`: README describes sophisticated architecture; actual code is much simpler
- `uniform_style_no_evolution`: entire codebase has identical formatting and idiom — no variation indicating multiple sessions or authors
- `no_design_evidence`: no comments explaining *why* decisions were made, only *what* the code does

### Scoring Formula Details

```
significance_score(pr) =
    classification_weight(pr.classification)          # substantive=1.0, trivial=0.3, docs=0.1, manufactured=0.0
    × log10(max(pr.repo_stars, 10)) / log10(100000)  # normalized 0–1 by stars
    × (pr.discussion_score / 10.0)                   # rubber-stamp quality already captured here by LLM

contribution_quality_score =
    mean(significance_score(pr) for pr in prs if pr.classification != MANUFACTURED) × 10

portfolio_authenticity_score =
    10.0
    - 2.0 × (manufactured_pr_count / max(total_pr_count, 1))    # penalize manufactured ratio
    - 1.5 × (1 if pr_burst_detected else 0)                      # penalize burst pattern
    - 1.5 × (vibe_coded_repo_count / max(total_repo_count, 1)) × 10  # penalize vibe-coded ratio
    (clamped to [0, 10])

repo_quality_score =
    mean(
        0.4 × repo.code_quality_score
        + 0.3 × repo.architecture_depth_score
        + 0.3 × (10 - repo.vibe_code_score)
        for repo in owned_repos if repo.commit_count > 5
    )

overall_score =
    0.4 × contribution_quality_score
    + 0.3 × portfolio_authenticity_score
    + 0.3 × repo_quality_score
```

### LLM Prompt Strategy

**Per-PR unified call** — one LLM call produces both `discussion_score` and `classification`. Combining them is cheaper and gives the model the full context it needs for both judgments simultaneously.

Inputs:
- PR title and description
- Diff stat breakdown: `{code: N, docs: N, tests: N, config: N}` lines changed
- Full review thread (comments, inline comments, approval events) up to 8KB; truncated with summary if longer
- Issue metadata: reporter username (same as PR author?), issue created date, comment count, issue body summary
- `time_to_merge_hours` and `repo_median_merge_hours` (null if unavailable)
- `review_cycles`, `reviewer_approved_without_comment`

Output schema (LLM returns structured JSON):
```json
{
  "discussion_score": 7.5,
  "classification": "substantive-code",
  "classification_rationale": "Author implemented a non-trivial cache invalidation fix (47 lines of logic across 3 files). The linked issue was 4 months old with 12 upvotes. Reviewer requested changes twice; author engaged with both rounds substantively. Merge time of 6 days is consistent with the repo's typical review cadence."
}
```

`discussion_score` rubric (0–10):
- 0–2: No discussion, immediate approval, or only superficial comments ("LGTM", "nice!")
- 3–4: Some reviewer comments but author made no substantive response, or discussion is procedural only
- 5–6: Real back-and-forth but shallow; reviewer flagged issues, author addressed them mechanically
- 7–8: Technical discussion with evidence of understanding; author demonstrated knowledge of tradeoffs
- 9–10: Exemplary: author proactively raised tradeoffs, incorporated feedback thoughtfully, discussion visible to future readers

`classification` guidance given to LLM (not hard rules — illustrative examples):
- `manufactured`: consider if author filed the issue themselves, the issue was very new with no community engagement, the PR was merged quickly with no substantive review, *and* the change itself is small. Any one of these alone is not sufficient.
- `substantive-code`: non-trivial logic change, ideally addressing a real user-reported problem with evidence of thought and iteration
- `trivial-code`: real code change but mechanical (rename, single-line fix, obvious missing null check with no design dimension)
- Rationale must cite the specific signals that drove the classification

Code Quality prompt inputs:
- Up to 20KB of sampled source files (largest files by line count, excluding generated/vendor)
- Repo language breakdown
- Commit count and contributor count (context)

Scoring rubric (0–10) for vibe-code:
- 0–2: Clear human-authored with idiomatic style evolution, meaningful comments, obvious debugging history
- 3–5: Competent code but uniform style, limited comments explaining decisions
- 6–8: Multiple vibe-code signals: overly clean, generic naming, no error handling, structure doesn't match complexity
- 9–10: Strong AI-generation indicators: identical patterns throughout, boilerplate sections, README-to-code mismatch
