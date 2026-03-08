#!/usr/bin/env python3
"""
CLI entry point for GitHub Activity Profiler.
Shares all logic with the webapp via analyzer.py.

Usage:
  # Bulk: analyze all contributors with a label in a repo
  python analyze.py --repo netflix/metaflow --label gsoc

  # Single user
  python analyze.py --user npow --repo netflix/metaflow

  # Output formats
  python analyze.py --repo netflix/metaflow --label gsoc --format markdown
  python analyze.py --repo netflix/metaflow --label gsoc --format json
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from analyzer import analyze_bulk, analyze_single_user
from db import Database

DB_PATH = Path("gh_profiler.db")


async def run_bulk(repo: str, label: str, db: Database, verbose: bool) -> list:
    q: asyncio.Queue = asyncio.Queue()

    async def printer():
        while True:
            event = await q.get()
            if event.get("type") == "progress" and verbose:
                print(event["message"], flush=True)
            elif event.get("type") == "result":
                break

    task = asyncio.create_task(printer())
    results = await analyze_bulk(repo=repo, label=label, db=db, progress_queue=q)
    await task
    return results


async def run_single(username: str, repo: str, db: Database, verbose: bool) -> dict:
    q: asyncio.Queue = asyncio.Queue()

    async def printer():
        while True:
            event = await q.get()
            if event.get("type") == "progress" and verbose:
                print(event["message"], flush=True)
            elif event.get("type") == "result":
                break

    task = asyncio.create_task(printer())
    result = await analyze_single_user(username=username, repo=repo, db=db, progress_queue=q)
    await task
    return result


def render_markdown(results: list) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    sorted_results = sorted(results, key=lambda x: x.get("overall", {}).get("overall_score", 0), reverse=True)

    lines = [
        "# GitHub Activity Analysis",
        f"Generated: {now}  |  {len(results)} contributors",
        "",
        "## Rankings",
        "",
        "| Rank | User | Score | Rec | Merge% | PRs | PRs/wk | Trivial% | Events | Commits |",
        "|------|------|-------|-----|--------|-----|--------|----------|--------|---------|",
    ]

    for i, r in enumerate(sorted_results, 1):
        o = r.get("overall", {})
        ev = r.get("event_summary", {})
        sig = r.get("activity_signals", {})
        lines.append(
            f"| {i} | [{r['username']}](https://github.com/{r['username']}) "
            f"| {o.get('overall_score', 0):.1f} "
            f"| {o.get('gsoc_recommendation', '?')} "
            f"| {sig.get('merge_rate_pct', '?')}% "
            f"| {sig.get('total_prs', len(r.get('all_prs', [])))} "
            f"| {sig.get('prs_per_week', '?')} "
            f"| {sig.get('trivial_pr_rate_pct', '?')}% "
            f"| {ev.get('total_events', 0)} "
            f"| {ev.get('total_commits', 0)} |"
        )

    lines += ["", "---", "", "## Detailed Profiles", ""]

    for r in sorted_results:
        u = r["username"]
        o = r.get("overall", {})
        p = r.get("profile", {})
        ev = r.get("event_summary", {})
        sig = r.get("activity_signals", {})

        lines += [
            f"### [{u}](https://github.com/{u})",
            f"**Score:** {o.get('overall_score', 0):.1f}/10  |  "
            f"**Recommendation:** {o.get('gsoc_recommendation', '?')}  |  "
            f"**Activity:** {o.get('activity_level', '?')}",
            "",
            f"Account age: {p.get('account_age_days', '?')} days  |  "
            f"Followers: {p.get('followers', '?')}  |  "
            f"Public repos: {p.get('public_repos', '?')}",
            "",
        ]

        if sig:
            lines += [
                f"**Farming signals:** "
                f"Merge rate: {sig.get('merge_rate_pct', '?')}% ({sig.get('merged_prs', '?')}/{sig.get('total_prs', '?')} PRs)  |  "
                f"PRs/week: {sig.get('prs_per_week', '?')}  |  "
                f"90d burst: {sig.get('burst_ratio_pct', '?')}%  |  "
                f"Trivial: {sig.get('trivial_pr_rate_pct', '?')}%  |  "
                f"Orgs: {sig.get('unique_orgs', '?')}",
                "",
            ]

        if p.get("bio"):
            lines += [f"*{p['bio']}*", ""]

        lines += [
            f"**Activity (last ~90 days):** {ev.get('total_events', 0)} events, "
            f"{ev.get('total_commits', 0)} commits across {ev.get('repos_with_commits', 0)} repos, "
            f"{ev.get('prs_opened', 0)} PRs opened, {ev.get('comments_and_reviews', 0)} review/comment events",
            "",
        ]

        top_repos = ev.get("top_repos", [])[:5]
        if top_repos:
            lines.append("**Most active repos (recent events):**")
            for tr in top_repos:
                lines.append(f"- [{tr['repo']}](https://github.com/{tr['repo']}): {tr['events']} events")
            lines.append("")

        lines.append(o.get("executive_summary", ""))
        lines.append("")

        if o.get("red_flags"):
            lines.append("**Red flags:**")
            for f in o["red_flags"]:
                lines.append(f"- {f}")
            lines.append("")

        if o.get("strengths"):
            lines.append("**Strengths:**")
            for s in o["strengths"]:
                lines.append(f"- {s}")
            lines.append("")

        own_repos = r.get("own_repos", [])
        if own_repos:
            lines += ["**Own repos:**", ""]
            lines.append("| Repo | Lang | ⭐ | Description |")
            lines.append("|------|------|----|-------------|")
            for repo in own_repos[:10]:
                lines.append(
                    f"| [{repo['name']}](https://github.com/{u}/{repo['name']}) "
                    f"| {repo['language'] or '?'} "
                    f"| {repo['stars']} "
                    f"| {(repo['description'] or '')[:80]} |"
                )
            lines.append("")

        target_prs = r.get("target_repo_pr_analyses", [])
        if target_prs:
            lines += ["**Target repo PRs:**", ""]
            lines.append("| PR | Classification | Discussion | Rationale |")
            lines.append("|----|----------------|------------|-----------|")
            for pr in target_prs:
                title_link = f"[{pr['title'][:60]}]({pr['url']})"
                lines.append(
                    f"| {title_link} | {pr['classification']} "
                    f"| {pr['discussion_score']:.1f}/10 "
                    f"| {pr['rationale'][:120]} |"
                )
            lines.append("")

        all_prs = r.get("all_prs", [])
        if all_prs:
            lines.append(f"**All public PRs ({len(all_prs)} found):**")
            for pr in all_prs[:20]:
                merged = pr.get("pull_request", {}).get("merged_at")
                state = "merged" if merged else pr.get("state", "?")
                lines.append(f"- [{pr['repo']}] #{pr['number']} \"{pr['title']}\" ({state})")
            if len(all_prs) > 20:
                lines.append(f"  ... and {len(all_prs) - 20} more")
            lines.append("")

        lines += ["---", ""]

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        prog="github-roast",
        description="Rank and vet GitHub contributors — detects PR farming, analyzes real engagement.",
    )
    parser.add_argument("user", nargs="?", help="GitHub username to analyze (positional)")
    parser.add_argument("--user", dest="user_flag", help="GitHub username to analyze")
    parser.add_argument("--repo", default=None, help="Target repo (owner/repo) for in-depth PR analysis")
    parser.add_argument("--label", help="Analyze all contributors with this label in --repo (bulk mode)")
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument("--output", help="Output file (default: stdout)")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output")
    args = parser.parse_args()

    username = args.user or args.user_flag

    if not args.label and not username:
        parser.error("Provide a username (github-roast <user>) or --label with --repo for bulk mode")
    if args.label and not args.repo:
        parser.error("--label requires --repo")

    db = Database(DB_PATH)
    db.init()

    verbose = not args.quiet

    if username:
        results = [asyncio.run(run_single(username, args.repo, db, verbose))]
    else:
        results = asyncio.run(run_bulk(args.repo, args.label, db, verbose))

    if args.format == "json":
        output = json.dumps(results, indent=2, default=str)
    else:
        output = render_markdown(results)

    if args.output:
        Path(args.output).write_text(output)
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(output)


if __name__ == "__main__":
    main()
