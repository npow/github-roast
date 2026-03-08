"""
Async GitHub Activity Profiler — core analysis logic.
Uses the `gh` CLI for API calls (assumes user is already logged in).

Shared by webapp.py (async routes) and analyze.py (CLI via asyncio.run).
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

LLM_BASE_URL = os.getenv("LLM_BASE_URL", os.getenv("ANTHROPIC_BASE_URL", "http://localhost:18082"))
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("ANTHROPIC_API_KEY", "unused"))
MODEL = os.getenv("LLM_MODEL", "gpt-5")
MAINTAINERS = {
    u.strip().lower()
    for u in os.getenv("MAINTAINERS", "").split(",")
    if u.strip()
}

GH_CACHE_TTL = 6 * 3600
LLM_CACHE_TTL = 24 * 3600


def _openai_base_url() -> str:
    base = LLM_BASE_URL.rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/v1"


def _extract_text_from_openai_message(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "".join(parts)
    return str(content or "")


def _llm_chat_completion(prompt: str, *, max_tokens: int) -> str:
    url = f"{_openai_base_url()}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if LLM_API_KEY:
        headers["Authorization"] = f"Bearer {LLM_API_KEY}"

    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
    }
    with httpx.Client(timeout=180) as client:
        resp = client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {resp.status_code}: {resp.text[:240]}")
        data = resp.json()
    return _extract_text_from_openai_message(
        (((data.get("choices") or [{}])[0]).get("message") or {}).get("content", "")
    )


def _is_retryable(exc: Exception) -> bool:
    """True for transient provider/network errors."""
    msg = str(exc).lower()
    if "http 429" in msg or "http 500" in msg or "http 502" in msg or "http 503" in msg or "http 504" in msg:
        return True
    if "timed out" in msg or "timeout" in msg or "connection reset" in msg:
        return True
    return "overloaded" in msg or "rate_limit" in msg


def _llm_call_with_retry(fn, *args, max_retries: int = 5, base_delay: float = 5.0):
    """Call fn(*args) synchronously, retrying on 503/overloaded with exponential backoff."""
    for attempt in range(max_retries):
        try:
            return fn(*args)
        except Exception as exc:
            if _is_retryable(exc) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            raise


# ── GitHub API helpers ────────────────────────────────────────────────────────

async def _gh_run(args: list[str]) -> str | None:
    """Run a gh command, return stdout or None on failure."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        return None
    return stdout.decode().strip()


async def ensure_gh_auth() -> None:
    """Fail fast if gh CLI auth is unavailable for the current process user."""
    ok = await _gh_run(["gh", "auth", "status"])
    if not ok:
        raise RuntimeError(
            "GitHub CLI auth is unavailable. Run 'gh auth login' for the service user or set GH_TOKEN."
        )


async def gh_async(path: str, db, *, paginate: bool = True) -> dict | list:
    """gh api call with DB-backed caching."""
    cache_key = f"gh:{path}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    cmd = ["gh", "api", "--paginate", path] if paginate else ["gh", "api", path]
    raw = await _gh_run(cmd)
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = json.loads("[" + raw.replace("][", "],[") + "]")
            if isinstance(data, list) and len(data) == 1:
                data = data[0]
        except Exception:
            return {}

    if data:
        await db.cache_set(cache_key, data, ttl=GH_CACHE_TTL)
    return data


async def gh_search_async(query: str, db, *, per_page: int = 100) -> list:
    """gh search via the search/issues API with caching."""
    cache_key = f"ghsearch:{query}:{per_page}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    raw = await _gh_run(["gh", "api", f"search/issues?q={query}&per_page={per_page}"])
    if not raw:
        return []
    items = json.loads(raw).get("items", [])
    if items:
        await db.cache_set(cache_key, items, ttl=GH_CACHE_TTL)
    return items


# ── Data fetchers ─────────────────────────────────────────────────────────────

def parse_date(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def hours_between(a: str | None, b: str | None) -> float | None:
    da, db_ = parse_date(a), parse_date(b)
    if not da or not db_:
        return None
    return abs((db_ - da).total_seconds()) / 3600


def extract_linked_issue(body: str | None) -> int | None:
    if not body:
        return None
    m = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"#(\d+)", body)
    if m:
        return int(m.group(1))
    return None


def classify_file(filename: str) -> str:
    filename = filename.lower()
    if any(filename.endswith(x) for x in [".md", ".rst", ".txt"]):
        return "docs"
    if any(x in filename for x in ["test_", "_test.", "/test/", "/tests/"]):
        return "test"
    if any(filename.endswith(x) for x in [".yml", ".yaml", ".toml", ".cfg", ".ini", ".json"]):
        return "config"
    return "code"


async def get_user_profile(username: str, db) -> dict:
    """Fetch GitHub user profile — account age, followers, bio, public repos count."""
    data = await gh_async(f"users/{username}", db, paginate=False)
    if not isinstance(data, dict):
        return {}
    created = parse_date(data.get("created_at"))
    account_age_days = (datetime.now(timezone.utc) - created).days if created else None
    return {
        "login": data.get("login"),
        "name": data.get("name"),
        "bio": data.get("bio"),
        "company": data.get("company"),
        "location": data.get("location"),
        "blog": data.get("blog") or "",          # personal site / LinkedIn / etc.
        "twitter_username": data.get("twitter_username") or "",
        "public_repos": data.get("public_repos", 0),
        "public_gists": data.get("public_gists", 0),
        "followers": data.get("followers", 0),
        "following": data.get("following", 0),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
        "account_age_days": account_age_days,
        "avatar_url": data.get("avatar_url"),
        "hireable": data.get("hireable"),
    }


async def get_user_repos(username: str, db, *, limit: int = 30) -> list:
    """All non-fork public repos sorted by push date."""
    repos = await gh_async(f"users/{username}/repos?sort=pushed&per_page={limit}", db)
    if not isinstance(repos, list):
        return []
    return [
        {
            "name": r.get("name"),
            "full_name": r.get("full_name"),
            "stars": r.get("stargazers_count", 0),
            "forks": r.get("forks_count", 0),
            "watchers": r.get("watchers_count", 0),
            "description": r.get("description") or "",
            "language": r.get("language"),
            "topics": r.get("topics", []),
            "created_at": r.get("created_at"),
            "pushed_at": r.get("pushed_at"),
            "fork": r.get("fork", False),
            "open_issues": r.get("open_issues_count", 0),
            "has_wiki": r.get("has_wiki"),
            "license": (r.get("license") or {}).get("spdx_id"),
        }
        for r in repos
        if not r.get("fork")
    ]


async def get_repo_readme(full_name: str, db) -> str:
    """Fetch decoded README text for a repo (truncated to 3000 chars)."""
    cache_key = f"readme:{full_name}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    data = await gh_async(f"repos/{full_name}/readme", db, paginate=False)
    if not isinstance(data, dict):
        return ""

    import base64
    content = data.get("content", "")
    encoding = data.get("encoding", "")
    if encoding == "base64":
        try:
            text = base64.b64decode(content.replace("\n", "")).decode("utf-8", errors="replace")
        except Exception:
            text = ""
    else:
        text = content

    truncated = text[:3000]
    await db.cache_set(cache_key, truncated, ttl=GH_CACHE_TTL)
    return truncated


async def get_repo_file_tree(full_name: str, db) -> list[str]:
    """Get top-level file listing for a repo."""
    data = await gh_async(f"repos/{full_name}/contents", db, paginate=False)
    if not isinstance(data, list):
        return []
    return [f.get("path", "") for f in data if isinstance(f, dict)]


async def get_repo_commit_count(full_name: str, db) -> int | None:
    """Estimate commit count from contributors endpoint."""
    cache_key = f"commitcount:{full_name}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    # /contributors gives per-user commit counts; sum them
    data = await gh_async(f"repos/{full_name}/contributors?per_page=100&anon=true", db)
    if not isinstance(data, list):
        return None
    total = sum(c.get("contributions", 0) for c in data)
    await db.cache_set(cache_key, total, ttl=GH_CACHE_TTL)
    return total


async def get_repo_languages(full_name: str, db) -> dict:
    """Bytes per language for a repo."""
    data = await gh_async(f"repos/{full_name}/languages", db, paginate=False)
    return data if isinstance(data, dict) else {}


async def get_top_repos_deep(username: str, repos: list, db, *, top_n: int = 4) -> list:
    """
    For the top N repos (by stars, or most recently pushed if no stars),
    fetch README, file tree, languages, and commit count.
    """
    # Pick top by stars, fallback to recent
    ranked = sorted(repos, key=lambda r: (r["stars"], r["pushed_at"] or ""), reverse=True)[:top_n]

    async def enrich(repo: dict) -> dict:
        full_name = repo.get("full_name") or f"{username}/{repo['name']}"
        readme, tree, languages, commits = await asyncio.gather(
            get_repo_readme(full_name, db),
            get_repo_file_tree(full_name, db),
            get_repo_languages(full_name, db),
            get_repo_commit_count(full_name, db),
        )
        return {
            **repo,
            "readme_excerpt": readme[:2000],
            "top_files": tree[:30],
            "languages": languages,
            "total_commits": commits,
        }

    return await asyncio.gather(*[enrich(r) for r in ranked])


async def get_user_events(username: str, db) -> list:
    """
    Recent public events — the most honest signal of actual activity.
    Returns up to 300 events (GitHub's max, ~90 days).
    """
    cache_key = f"ghevents:{username}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    # Events API doesn't support --paginate cleanly; fetch 3 pages in parallel
    pages = await asyncio.gather(*[
        _gh_run(["gh", "api", f"users/{username}/events/public?per_page=100&page={p}"])
        for p in range(1, 4)
    ])
    events = []
    for raw in pages:
        if not raw:
            break
        page_events = json.loads(raw)
        if not page_events:
            break
        events.extend(page_events)

    if events:
        await db.cache_set(cache_key, events, ttl=GH_CACHE_TTL)
    return events


def summarize_events(events: list) -> dict:
    """
    Condense raw events into countable signals for LLM context.
    Returns per-repo activity and overall type distribution.
    """
    type_counts: dict[str, int] = {}
    repos_touched: dict[str, dict] = {}

    for e in events:
        etype = e.get("type", "")
        repo = e.get("repo", {}).get("name", "")
        type_counts[etype] = type_counts.get(etype, 0) + 1

        if repo:
            if repo not in repos_touched:
                repos_touched[repo] = {"types": set(), "count": 0}
            repos_touched[repo]["types"].add(etype)
            repos_touched[repo]["count"] += 1

    # Top repos by activity
    top_repos = sorted(repos_touched.items(), key=lambda x: x[1]["count"], reverse=True)[:15]
    top_repos_summary = [
        {"repo": r, "events": d["count"], "types": sorted(d["types"])}
        for r, d in top_repos
    ]

    # Commits breakdown
    push_events = [e for e in events if e.get("type") == "PushEvent"]
    total_commits = sum(len(e.get("payload", {}).get("commits", [])) for e in push_events)
    commit_repos = {e.get("repo", {}).get("name") for e in push_events}

    # PR activity
    pr_events = [e for e in events if e.get("type") == "PullRequestEvent"]
    prs_opened = [e for e in pr_events if e.get("payload", {}).get("action") == "opened"]
    prs_merged = [e for e in pr_events if e.get("payload", {}).get("pull_request", {}).get("merged")]

    # Issue activity
    issue_events = [e for e in events if e.get("type") == "IssuesEvent"]
    issues_opened = [e for e in issue_events if e.get("payload", {}).get("action") == "opened"]

    # Comment activity
    comment_events = [e for e in events if e.get("type") in ("IssueCommentEvent", "PullRequestReviewCommentEvent", "PullRequestReviewEvent")]

    # Date range of events
    dates = [e.get("created_at") for e in events if e.get("created_at")]
    oldest = min(dates) if dates else None
    newest = max(dates) if dates else None

    return {
        "total_events": len(events),
        "date_range": {"oldest": oldest, "newest": newest},
        "type_distribution": type_counts,
        "total_commits": total_commits,
        "repos_with_commits": len(commit_repos),
        "prs_opened": len(prs_opened),
        "prs_merged": len(prs_merged),
        "issues_opened": len(issues_opened),
        "comments_and_reviews": len(comment_events),
        "top_repos": top_repos_summary,
    }


async def get_user_all_prs(username: str, db, *, limit: int = 50) -> list:
    """All PRs by user across all public repos (open + merged + closed)."""
    items = await gh_search_async(
        f"author:{username}+is:pr+is:public", db, per_page=limit
    )
    result = []
    for i in items[:limit]:
        repo = i.get("repository_url", "").split("/repos/")[-1]
        result.append({
            "repo": repo,
            "number": i.get("number"),
            "title": i.get("title", ""),
            "state": i.get("state", ""),
            "created_at": i.get("created_at"),
            "closed_at": i.get("closed_at"),
            "pull_request": i.get("pull_request", {}),
        })
    return result


async def get_user_issues_filed(username: str, db, *, limit: int = 30) -> list:
    """Issues opened by user across public repos."""
    items = await gh_search_async(
        f"author:{username}+is:issue+is:public", db, per_page=limit
    )
    return [
        {
            "repo": i.get("repository_url", "").split("/repos/")[-1],
            "number": i.get("number"),
            "title": i.get("title", ""),
            "state": i.get("state", ""),
            "created_at": i.get("created_at"),
            "comments": i.get("comments", 0),
        }
        for i in items[:limit]
    ]


async def get_pr_sample_stats(all_prs: list, db) -> list:
    """
    Sample up to 12 cross-repo PRs (one per unique repo) and fetch actual
    diff stats + discussion threads so the LLM can read real PR content.
    """
    seen_repos: dict[str, dict] = {}
    for pr in all_prs:
        repo = pr.get("repo", "")
        if repo and repo not in seen_repos:
            seen_repos[repo] = pr
        if len(seen_repos) >= 12:
            break

    async def fetch_one(pr: dict) -> dict:
        repo = pr["repo"]
        num = pr["number"]
        cache_key = f"prstats2:{repo}:{num}"
        cached = await db.cache_get(cache_key)
        if cached is not None:
            return cached

        pr_data, comments, reviews = await asyncio.gather(
            gh_async(f"repos/{repo}/pulls/{num}", db, paginate=False),
            gh_async(f"repos/{repo}/issues/{num}/comments", db),
            gh_async(f"repos/{repo}/pulls/{num}/reviews", db),
        )

        if not isinstance(pr_data, dict):
            return {"repo": repo, "number": num, "title": pr.get("title", ""), "error": "not found",
                    "total_lines": 0, "changed_files": 0, "merged": False, "reviewer_comments": 0}

        author_login = (pr_data.get("user") or {}).get("login", "")

        # Build readable thread excerpt
        thread_parts = []
        if pr_data.get("body"):
            thread_parts.append(f"PR body: {pr_data['body'][:500]}")
        if isinstance(comments, list):
            for c in sorted(comments, key=lambda x: x.get("created_at", ""))[:6]:
                user = (c.get("user") or {}).get("login", "?")
                body = c.get("body", "").strip()[:250]
                if body:
                    thread_parts.append(f"[{user}]: {body}")
        if isinstance(reviews, list):
            for r in reviews[:4]:
                user = (r.get("user") or {}).get("login", "?")
                state = r.get("state", "")
                body = (r.get("body") or "").strip()[:150]
                thread_parts.append(f"[{user} review - {state}]: {body}" if body else f"[{user} review - {state}]")

        reviewer_comments = sum(
            1 for c in (comments if isinstance(comments, list) else [])
            if (c.get("user") or {}).get("login", "") != author_login
        )

        result = {
            "repo": repo,
            "number": num,
            "title": pr_data.get("title", pr.get("title", "")),
            "url": pr_data.get("html_url", ""),
            "merged": bool(pr_data.get("merged_at")),
            "state": pr_data.get("state", pr.get("state", "")),
            "additions": pr_data.get("additions", 0),
            "deletions": pr_data.get("deletions", 0),
            "changed_files": pr_data.get("changed_files", 0),
            "total_lines": pr_data.get("additions", 0) + pr_data.get("deletions", 0),
            "reviewer_comments": reviewer_comments,
            "review_states": [r.get("state") for r in (reviews if isinstance(reviews, list) else [])],
            "thread": "\n".join(thread_parts)[:2000],
        }
        await db.cache_set(cache_key, result, ttl=GH_CACHE_TTL)
        return result

    return list(await asyncio.gather(*[fetch_one(pr) for pr in seen_repos.values()]))


def compute_activity_signals(all_prs: list, event_summary: dict, pr_sample_stats: list) -> dict:
    """
    Derive explicit farming/quality signals from raw data.
    These are presented to the LLM up-front to prevent it from being fooled
    by high raw PR counts or long account ages.
    """
    from datetime import timedelta

    total_prs = len(all_prs)
    merged_prs = [p for p in all_prs if (p.get("pull_request") or {}).get("merged_at")]
    closed_not_merged = [
        p for p in all_prs
        if p.get("state") == "closed" and not (p.get("pull_request") or {}).get("merged_at")
    ]
    open_prs = [p for p in all_prs if p.get("state") == "open"]

    merge_rate = len(merged_prs) / total_prs if total_prs > 0 else 0

    # PR velocity over visible history
    pr_dates = sorted([p["created_at"] for p in all_prs if p.get("created_at")])
    if len(pr_dates) >= 2:
        oldest = datetime.fromisoformat(pr_dates[0].replace("Z", "+00:00"))
        newest = datetime.fromisoformat(pr_dates[-1].replace("Z", "+00:00"))
        weeks = max((newest - oldest).days / 7, 1)
        prs_per_week = total_prs / weeks
    else:
        prs_per_week = 0.0

    # Burst ratio: what fraction of all PRs happened in the last 90 days
    now = datetime.now(timezone.utc)
    cutoff_90d = now - timedelta(days=90)
    recent_prs = [
        p for p in all_prs
        if p.get("created_at") and
        datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")) > cutoff_90d
    ]
    burst_ratio = len(recent_prs) / total_prs if total_prs > 0 else 0

    # Org/repo diversity
    orgs = {
        p["repo"].split("/")[0]
        for p in all_prs if p.get("repo") and "/" in p.get("repo", "")
    }
    repos = {p["repo"] for p in all_prs if p.get("repo")}

    # PR size stats from sample
    sizes = [s.get("total_lines", 0) for s in pr_sample_stats if not s.get("error")]
    avg_lines = sum(sizes) / len(sizes) if sizes else 0
    trivial_count = sum(1 for s in pr_sample_stats if not s.get("error") and s.get("total_lines", 999) < 10)
    trivial_rate = trivial_count / len(pr_sample_stats) if pr_sample_stats else 0

    files = [s.get("changed_files", 0) for s in pr_sample_stats if not s.get("error")]
    avg_files = sum(files) / len(files) if files else 0

    rev_comments = [s.get("reviewer_comments", 0) for s in pr_sample_stats if not s.get("error")]
    avg_reviewer_comments = sum(rev_comments) / len(rev_comments) if rev_comments else 0

    return {
        "total_prs": total_prs,
        "merged_prs": len(merged_prs),
        "closed_not_merged": len(closed_not_merged),
        "open_prs": len(open_prs),
        "merge_rate_pct": round(merge_rate * 100, 1),
        "prs_per_week": round(prs_per_week, 2),
        "unique_orgs": len(orgs),
        "unique_repos": len(repos),
        "recent_90d_prs": len(recent_prs),
        "burst_ratio_pct": round(burst_ratio * 100, 1),
        "sample_size": len(pr_sample_stats),
        "avg_lines_changed": round(avg_lines, 1),
        "avg_files_changed": round(avg_files, 1),
        "trivial_pr_rate_pct": round(trivial_rate * 100, 1),
        "avg_reviewer_comments": round(avg_reviewer_comments, 1),
    }


def extract_commit_messages(events: list) -> list[str]:
    """Pull distinct commit messages from PushEvent payloads."""
    seen: set[str] = set()
    messages: list[str] = []
    for e in events:
        if e.get("type") == "PushEvent":
            for commit in e.get("payload", {}).get("commits", []):
                msg = commit.get("message", "").strip().split("\n")[0][:200]
                if msg and msg not in seen:
                    seen.add(msg)
                    messages.append(msg)
    return messages[:60]


# ── PR details (for repo-specific analysis) ────────────────────────────────────

async def get_pr_details(pr_number: int, repo: str, db) -> dict:
    pr, files, reviews, comments, review_comments = await asyncio.gather(
        gh_async(f"repos/{repo}/pulls/{pr_number}", db, paginate=False),
        gh_async(f"repos/{repo}/pulls/{pr_number}/files", db),
        gh_async(f"repos/{repo}/pulls/{pr_number}/reviews", db),
        gh_async(f"repos/{repo}/issues/{pr_number}/comments", db),
        gh_async(f"repos/{repo}/pulls/{pr_number}/comments", db),
    )

    if not pr or not isinstance(pr, dict):
        return {}

    stats = {"code": 0, "docs": 0, "test": 0, "config": 0}
    if isinstance(files, list):
        for f in files:
            ftype = classify_file(f.get("filename", ""))
            stats[ftype] += f.get("additions", 0) + f.get("deletions", 0)

    review_list = reviews if isinstance(reviews, list) else []
    reviewer_usernames = {r["user"]["login"] for r in review_list if r.get("user")}
    reviewer_usernames.discard(pr.get("user", {}).get("login", ""))
    approved_without_comment = any(
        r.get("state") == "APPROVED" and not r.get("body", "").strip()
        for r in review_list
    )
    review_cycles = sum(1 for r in review_list if r.get("state") == "CHANGES_REQUESTED")

    all_comments = (comments if isinstance(comments, list) else []) + \
                   (review_comments if isinstance(review_comments, list) else [])
    author_login = pr.get("user", {}).get("login", "")
    reviewer_comment_count = sum(1 for c in all_comments if c.get("user", {}).get("login") != author_login)
    author_response_count = sum(1 for c in all_comments if c.get("user", {}).get("login") == author_login)

    issue_number = extract_linked_issue(pr.get("body"))
    issue_data: dict = {}
    issue_reporter_is_author = False
    issue_age_days = None
    issue_comment_count = None

    if issue_number:
        issue_data = await gh_async(f"repos/{repo}/issues/{issue_number}", db, paginate=False)
        if isinstance(issue_data, dict):
            issue_reporter_is_author = issue_data.get("user", {}).get("login") == author_login
            created = parse_date(issue_data.get("created_at"))
            pr_created = parse_date(pr.get("created_at"))
            if created and pr_created:
                issue_age_days = (pr_created - created).days
            issue_comment_count = issue_data.get("comments")

    time_to_merge = hours_between(pr.get("created_at"), pr.get("merged_at"))

    thread_parts = []
    if pr.get("body"):
        thread_parts.append(f"PR Description: {pr['body'][:800]}")
    for c in sorted(all_comments, key=lambda x: x.get("created_at", "")):
        user = c.get("user", {}).get("login", "unknown")
        body = c.get("body", "").strip()[:300]
        if body:
            thread_parts.append(f"[{user}]: {body}")
    for r in review_list:
        user = r.get("user", {}).get("login", "unknown")
        state = r.get("state", "")
        body = r.get("body", "").strip()[:200]
        thread_parts.append(f"[{user} - {state}]: {body}" if body else f"[{user} - {state}]")

    return {
        "number": pr_number,
        "title": pr.get("title", ""),
        "state": pr.get("state", ""),
        "merged": pr.get("merged_at") is not None,
        "created_at": pr.get("created_at"),
        "merged_at": pr.get("merged_at"),
        "time_to_merge_hours": time_to_merge,
        "stats": stats,
        "total_lines": sum(stats.values()),
        "files_changed": len(files) if isinstance(files, list) else 0,
        "review_cycles": review_cycles,
        "reviewer_comment_count": reviewer_comment_count,
        "author_response_count": author_response_count,
        "approved_without_comment": approved_without_comment,
        "reviewer_count": len(reviewer_usernames),
        "issue_number": issue_number,
        "issue_reporter_is_author": issue_reporter_is_author,
        "issue_age_days": issue_age_days,
        "issue_comment_count": issue_comment_count,
        "issue_title": issue_data.get("title") if issue_data else None,
        "thread": "\n".join(thread_parts)[:4000],
        "url": pr.get("html_url", ""),
    }


async def get_repo_median_merge_hours(repo: str, db) -> float | None:
    cache_key = f"median:{repo}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached

    prs = await gh_async(f"repos/{repo}/pulls?state=closed&per_page=50", db)
    if not isinstance(prs, list):
        return None
    times = []
    for pr in prs:
        if pr.get("merged_at"):
            h = hours_between(pr.get("created_at"), pr.get("merged_at"))
            if h:
                times.append(h)
    if not times:
        return None
    times.sort()
    median = times[len(times) // 2]
    await db.cache_set(cache_key, median, ttl=GH_CACHE_TTL)
    return median


# ── LLM analysis ─────────────────────────────────────────────────────────────

def _llm_analyze_pr_sync(pr_detail: dict, repo_median_hours: float | None) -> dict:
    stats = pr_detail["stats"]
    median_info = f"{repo_median_hours:.0f}h" if repo_median_hours else "unknown"
    merge_info = (
        f"{pr_detail['time_to_merge_hours']:.1f}h"
        if pr_detail.get("time_to_merge_hours")
        else "not merged"
    )

    prompt = f"""You are evaluating a GitHub PR contribution.

PR: {pr_detail['title']}
URL: {pr_detail['url']}
State: {'MERGED' if pr_detail['merged'] else pr_detail['state'].upper()}

Diff breakdown (lines changed):
- Code: {stats['code']}
- Tests: {stats['test']}
- Docs: {stats['docs']}
- Config: {stats['config']}
- Files changed: {pr_detail['files_changed']}

Review activity:
- Time to merge: {merge_info} (repo median: {median_info})
- Review cycles (changes requested): {pr_detail['review_cycles']}
- Reviewer comments: {pr_detail['reviewer_comment_count']}
- Author responses to reviewers: {pr_detail['author_response_count']}
- Approved without comment: {pr_detail['approved_without_comment']}

Linked issue #{pr_detail['issue_number'] or 'none'}:
- Issue title: {pr_detail['issue_title'] or 'N/A'}
- Issue filed by PR author: {pr_detail['issue_reporter_is_author']}
- Issue age when PR opened: {f"{pr_detail['issue_age_days']} days" if pr_detail.get('issue_age_days') is not None else 'unknown'}
- Prior comments on issue: {pr_detail['issue_comment_count'] if pr_detail.get('issue_comment_count') is not None else 'unknown'}

Review thread:
{pr_detail['thread'] or '(no discussion)'}

Return JSON:
{{
  "discussion_score": <float 0-10>,
  "classification": <one of: "substantive-code", "trivial-code", "docs-only", "test-only", "config-only", "manufactured">,
  "classification_rationale": <2-3 sentence explanation>
}}
Return only JSON, no markdown fences."""

    raw = _llm_chat_completion(prompt, max_tokens=512)
    text = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


async def llm_analyze_pr(pr_detail: dict, repo_median_hours: float | None, db) -> dict:
    cache_key = f"llmpr:{pr_detail['number']}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, _llm_call_with_retry, _llm_analyze_pr_sync, pr_detail, repo_median_hours
        )
        await db.cache_set(cache_key, result, ttl=LLM_CACHE_TTL)
        return result
    except Exception as e:
        return {"discussion_score": 0, "classification": "unknown", "classification_rationale": f"Error: {e}"}


def _llm_analyze_full_profile_sync(
    username: str,
    profile: dict,
    own_repos: list,
    deep_repos: list,
    all_prs: list,
    issues_filed: list,
    event_summary: dict,
    target_repo_pr_analyses: list,
    activity_signals: dict,
    pr_samples: list,
    commit_messages: list,
) -> dict:
    """
    Holistic LLM assessment using the full GitHub activity picture.
    """
    def _clip(value: Any, n: int) -> str:
        return str(value or "")[:n]

    def _date10(value: Any) -> str:
        return _clip(value or "?", 10)

    sig = activity_signals

    # ── Farming signals warnings ─────────────────────────────────────────────
    farming_warnings = []
    if sig.get("merge_rate_pct", 100) < 20 and sig.get("total_prs", 0) >= 5:
        farming_warnings.append(
            f"CRITICAL: Only {sig['merge_rate_pct']}% of PRs merged — strong indicator of PR spam."
        )
    if sig.get("trivial_pr_rate_pct", 0) > 40:
        farming_warnings.append(
            f"CRITICAL: {sig['trivial_pr_rate_pct']}% of sampled PRs are trivial (<10 lines) — copy-paste spam."
        )
    if sig.get("burst_ratio_pct", 0) > 70 and sig.get("total_prs", 0) >= 5:
        farming_warnings.append(
            f"WARNING: {sig['burst_ratio_pct']}% of all PRs in last 90 days — suspicious burst activity."
        )
    if sig.get("prs_per_week", 0) > 4:
        farming_warnings.append(
            f"WARNING: {sig['prs_per_week']:.1f} PRs/week is unsustainably high for real contributions."
        )
    if sig.get("avg_reviewer_comments", 10) < 1 and sig.get("total_prs", 0) >= 5:
        farming_warnings.append(
            f"WARNING: avg {sig['avg_reviewer_comments']:.1f} reviewer comments per PR — maintainers not engaging."
        )
    farming_warnings_str = "\n".join(f"  ⚠️  {w}" for w in farming_warnings) or "  None detected."

    # ── Sampled PR threads ───────────────────────────────────────────────────
    sample_sections = []
    for s in pr_samples[:12]:
        if s.get("error"):
            continue
        review_states = ", ".join(s.get("review_states", [])) or "no reviews"
        merged_str = "MERGED" if s.get("merged") else s.get("state", "?").upper()
        sample_sections.append(
            f"  [{s['repo']}] #{s['number']} \"{_clip(s.get('title'), 80)}\"\n"
            f"  Status: {merged_str} | +{s.get('additions',0)}/-{s.get('deletions',0)} lines, {s.get('changed_files',0)} files | reviews: {review_states}\n"
            f"  Discussion:\n    {(s.get('thread') or '(no discussion)').replace(chr(10), chr(10)+'    ')}"
        )
    pr_samples_str = "\n\n".join(sample_sections) or "(no samples)"

    # ── Commit messages ──────────────────────────────────────────────────────
    commit_msgs_str = "\n".join(f"  - {m}" for m in commit_messages[:30]) or "  (none)"

    # ── Own repos ────────────────────────────────────────────────────────────
    repo_lines = "\n".join(
        f"- {r['name']} ({r['language'] or '?'}): ⭐{r['stars']} forks:{r['forks']} | {_clip(r.get('description') or 'no description', 80)}"
        + (f" | topics: {','.join(r['topics'][:5])}" if r.get('topics') else "")
        for r in own_repos[:15]
    ) or "(none)"

    # ── Deep repo analysis ───────────────────────────────────────────────────
    deep_repo_sections = []
    for r in deep_repos:
        langs = ", ".join(f"{k}:{v//1000}k" for k, v in sorted((r.get("languages") or {}).items(), key=lambda x: -x[1])[:5])
        files_str = ", ".join(r.get("top_files", [])[:20])
        readme = (r.get("readme_excerpt") or "")[:1500]
        deep_repo_sections.append(
            f"  REPO: {r['name']} ⭐{r['stars']} | commits:{r.get('total_commits','?')} | langs:{langs or '?'}\n"
            f"  Files: {files_str or '(empty)'}\n"
            f"  README excerpt:\n{readme[:800] or '(no readme)'}"
        )
    deep_repos_str = "\n\n".join(deep_repo_sections) or "(none)"

    # ── All PRs list ─────────────────────────────────────────────────────────
    pr_lines = "\n".join(
        f"- [{p['repo']}] #{p['number']} \"{p['title']}\" ({p['state']})"
        for p in all_prs[:30]
    ) or "(none found)"

    # ── Issues filed ─────────────────────────────────────────────────────────
    issue_lines = "\n".join(
        f"- [{i['repo']}] #{i['number']} \"{i['title']}\" ({i['state']}, {i['comments']} comments)"
        for i in issues_filed[:20]
    ) or "(none found)"

    # ── Target repo PR analyses ───────────────────────────────────────────────
    target_pr_lines = "\n".join(
        f"- PR #{p['number']} '{p['title']}': {p['classification']} | discussion: {p['discussion_score']}/10 | {p['rationale']}"
        for p in target_repo_pr_analyses
    ) or "(none)"

    # ── Event summary ────────────────────────────────────────────────────────
    ev = event_summary
    top_repos_str = "\n".join(
        f"  - {r['repo']}: {r['events']} events ({', '.join(r['types'])})"
        for r in ev.get("top_repos", [])[:10]
    )

    # ── Social / identity signals ────────────────────────────────────────────
    social_parts = []
    if profile.get("blog"):
        social_parts.append(f"Website/LinkedIn: {profile['blog']}")
    if profile.get("twitter_username"):
        social_parts.append(f"Twitter: @{profile['twitter_username']}")
    if profile.get("company"):
        social_parts.append(f"Company: {profile['company']}")
    if profile.get("location"):
        social_parts.append(f"Location: {profile['location']}")
    social_str = " | ".join(social_parts) or "none listed"

    prompt = f"""You are doing a deep, evidence-based GitHub assessment of '{username}'.
Read ALL sections carefully, especially the farming signals and actual PR discussions.

══════════════════════════════════════════════════════════════════════
COMPUTED FARMING SIGNALS — READ FIRST, WEIGHT HEAVILY IN SCORING
══════════════════════════════════════════════════════════════════════
PR statistics (all public PRs found):
  Total PRs: {sig.get('total_prs', 0)} | Merged: {sig.get('merged_prs', 0)} ({sig.get('merge_rate_pct', 0)}%) | Closed-not-merged: {sig.get('closed_not_merged', 0)} | Open: {sig.get('open_prs', 0)}
  PRs per week (over visible history): {sig.get('prs_per_week', 0):.2f}
  90-day burst: {sig.get('recent_90d_prs', 0)} of {sig.get('total_prs', 0)} PRs in last 90 days ({sig.get('burst_ratio_pct', 0)}%)
  Unique orgs targeted: {sig.get('unique_orgs', 0)} | Unique repos: {sig.get('unique_repos', 0)}

PR quality (sampled {sig.get('sample_size', 0)} PRs, one per repo):
  Avg PR size: {sig.get('avg_lines_changed', 0)} lines, {sig.get('avg_files_changed', 0)} files
  Trivial PRs (<10 lines): {sig.get('trivial_pr_rate_pct', 0)}%
  Avg reviewer engagement: {sig.get('avg_reviewer_comments', 0)} reviewer comments per PR

Automated warnings:
{farming_warnings_str}

══════════════════════════════════════════════════════════════════════
SAMPLED CROSS-REPO PR DISCUSSIONS (actual content — read carefully)
══════════════════════════════════════════════════════════════════════
{pr_samples_str}

══════════════════════════════════════════════════════════════════════
COMMIT MESSAGES (sample from push events — assess quality/authenticity)
══════════════════════════════════════════════════════════════════════
{commit_msgs_str}

══════════════════════════════════════════════════════════════════════
ACCOUNT & IDENTITY
══════════════════════════════════════════════════════════════════════
- Account age: {profile.get('account_age_days', '?')} days (created {(profile.get('created_at') or '')[:10]})
- Public repos: {profile.get('public_repos', '?')} | Gists: {profile.get('public_gists', '?')}
- Followers: {profile.get('followers', '?')} | Following: {profile.get('following', '?')}
- Bio: {profile.get('bio') or 'none'}
- Social/identity: {social_str}

══════════════════════════════════════════════════════════════════════
RECENT ACTIVITY (last ~90 days of public events)
══════════════════════════════════════════════════════════════════════
- Total events: {ev.get('total_events', 0)} (date range: {_date10((ev.get('date_range') or {}).get('oldest'))} to {_date10((ev.get('date_range') or {}).get('newest'))})
- Commits: {ev.get('total_commits', 0)} across {ev.get('repos_with_commits', 0)} repos
- PRs opened: {ev.get('prs_opened', 0)} | PRs with merged activity: {ev.get('prs_merged', 0)}
- Issues opened: {ev.get('issues_opened', 0)} | Review/comment events: {ev.get('comments_and_reviews', 0)}
- Most active repos:
{top_repos_str or '  (none)'}

══════════════════════════════════════════════════════════════════════
OWN REPOS
══════════════════════════════════════════════════════════════════════
{repo_lines}

DEEP REPO ANALYSIS (top repos — actual README + file tree):
{deep_repos_str}

══════════════════════════════════════════════════════════════════════
ALL PUBLIC PRs (list view)
══════════════════════════════════════════════════════════════════════
{pr_lines}

ISSUES FILED:
{issue_lines}

TARGET REPO PR ANALYSES (in-depth, with actual discussion):
{target_pr_lines}

══════════════════════════════════════════════════════════════════════
ASSESSMENT INSTRUCTIONS
══════════════════════════════════════════════════════════════════════
Produce a thorough, evidence-based assessment. Use the actual PR discussion threads above.

Key things to evaluate:
1. FARMING vs genuine: Low merge rate + trivial diffs + no reviewer engagement = PR farming. Weight this VERY heavily.
2. Discussion quality: In the sampled PRs, does the author engage with maintainer feedback? Do maintainers respond at all?
3. Commit message quality: Are they descriptive and thoughtful, or generic "fix", "update", "Initial commit"?
4. Real projects: Do READMEs describe working software? File trees match the claim? Tests, CI, real docs?
5. Skill depth: Languages/frameworks across repos — superficial wrappers or real implementations?
6. Burst activity: Sudden spike of PRs in a short window is a major red flag.
7. Self-issued PRs: Self-filed issues with immediate same-day PRs = manufactured contributions.

Scoring guidance:
- 0% merge rate with many PRs → overall_score ≤ 3, recommendation "no" or "strong no"
- <20% merge rate → significant penalty, likely 3-5 range
- Genuine OSS engagement with merged PRs and real discussions → can score 6-9
- Account age alone does NOT compensate for farming signals

Return JSON:
{{
  "overall_score": <float 0-10>,
  "contribution_quality": <float 0-10>,
  "portfolio_authenticity": <float 0-10>,
  "discussion_depth": <float 0-10>,
  "repo_quality": <float 0-10>,
  "activity_level": <"very active" | "active" | "moderate" | "low" | "very low">,
  "executive_summary": <5-6 sentences: honest assessment citing specific repos/PRs/merge rate>,
  "red_flags": [<specific concerns with concrete evidence — cite PR numbers, repos, percentages>],
  "strengths": [<genuine positives with concrete evidence>],
  "recommendation": <"strong yes" | "yes" | "maybe" | "no" | "strong no">
}}

Be direct and skeptical. A high PR count with a 0% merge rate is worse than 3 merged PRs. Return only JSON."""

    raw = _llm_chat_completion(prompt, max_tokens=1000)
    text = re.sub(r"^```(?:json)?\n?", "", raw.strip())
    text = re.sub(r"\n?```$", "", text)
    return json.loads(text)


async def llm_analyze_full_profile(
    username: str,
    profile: dict,
    own_repos: list,
    deep_repos: list,
    all_prs: list,
    issues_filed: list,
    event_summary: dict,
    target_repo_pr_analyses: list,
    db,
    activity_signals: dict | None = None,
    pr_samples: list | None = None,
    commit_messages: list | None = None,
) -> dict:
    cache_key = f"llmprofile4:{username}"
    cached = await db.cache_get(cache_key)
    if cached is not None:
        return cached
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            _llm_call_with_retry,
            _llm_analyze_full_profile_sync,
            username, profile, own_repos, deep_repos, all_prs, issues_filed,
            event_summary, target_repo_pr_analyses,
            activity_signals or {}, pr_samples or [], commit_messages or [],
        )
        await db.cache_set(cache_key, result, ttl=LLM_CACHE_TTL)
        return result
    except Exception as e:
        return {
            "overall_score": 0, "activity_level": "unknown",
            "executive_summary": f"Error: {e}",
            "red_flags": [], "strengths": [], "recommendation": "no",
        }


# ── High-level analysis entry points ─────────────────────────────────────────

async def analyze_single_user(
    username: str,
    repo: str | None,
    db,
    progress_queue: asyncio.Queue,
) -> dict:
    """
    Full profile analysis of a single user.
    Fetches activity across all of GitHub, plus detailed PR analysis in `repo`.
    Emits progress messages to queue.
    """
    async def emit(msg: str):
        await progress_queue.put({"type": "progress", "message": msg})

    await emit("Checking GitHub CLI auth...")
    await ensure_gh_auth()

    await emit(f"Fetching GitHub profile for {username}...")
    profile = await get_user_profile(username, db)

    await emit(f"Fetching public events (last ~90 days)...")
    events = await get_user_events(username, db)
    event_summary = summarize_events(events)
    await emit(f"  {event_summary['total_events']} events, {event_summary['total_commits']} commits across {event_summary['repos_with_commits']} repos")

    await emit(f"Fetching all public repos...")
    own_repos = await get_user_repos(username, db)
    await emit(f"  {len(own_repos)} non-fork repos found")

    await emit(f"Deep-diving top repos + fetching all PRs/issues in parallel...")
    (deep_repos, (all_prs, issues_filed)) = await asyncio.gather(
        get_top_repos_deep(username, own_repos, db),
        asyncio.gather(get_user_all_prs(username, db), get_user_issues_filed(username, db)),
    )
    await emit(f"  {len(deep_repos)} repos deep-dived, {len(all_prs)} PRs, {len(issues_filed)} issues")

    await emit(f"Sampling cross-repo PR discussions...")
    pr_samples = await get_pr_sample_stats(all_prs, db)
    commit_messages = extract_commit_messages(events)
    activity_signals = compute_activity_signals(all_prs, event_summary, pr_samples)
    await emit(
        f"  Signals: merge_rate={activity_signals['merge_rate_pct']}%, "
        f"prs/wk={activity_signals['prs_per_week']}, "
        f"trivial_rate={activity_signals['trivial_pr_rate_pct']}%, "
        f"orgs={activity_signals['unique_orgs']}"
    )

    # Detailed analysis of PRs in the target repo (optional)
    target_pr_analyses = []
    if repo:
        await emit(f"Fetching PRs in {repo}...")
        repo_items = await gh_search_async(f"author:{username}+is:pr+repo:{repo}", db)
        pr_numbers = [i["number"] for i in repo_items]
        await emit(f"  {len(pr_numbers)} PR(s) in {repo}")

        await emit("Fetching repo merge time baseline...")
        repo_median = await get_repo_median_merge_hours(repo, db)

        await emit(f"Fetching {len(pr_numbers)} PR(s) in parallel...")
        await progress_queue.put({"type": "phase_totals", "fetch_total": len(pr_numbers), "llm_total": 0})

        async def _fetch_pr_with_progress(n):
            result = await get_pr_details(n, repo, db)
            await progress_queue.put({"type": "sub_progress", "phase": "fetch_prs"})
            return result

        pr_details = [
            d for d in await asyncio.gather(*[_fetch_pr_with_progress(n) for n in pr_numbers])
            if d
        ]
        await progress_queue.put({"type": "phase_totals", "fetch_total": len(pr_numbers), "llm_total": len(pr_details)})

        await emit(f"LLM: analyzing {len(pr_details)} PR(s) in parallel...")

        async def _llm_pr_with_progress(d):
            result = await llm_analyze_pr(d, repo_median, db)
            await progress_queue.put({"type": "sub_progress", "phase": "llm_prs"})
            return result

        llm_results = await asyncio.gather(*[_llm_pr_with_progress(d) for d in pr_details])
        for detail, llm_result in zip(pr_details, llm_results):
            target_pr_analyses.append({
                "number": detail["number"],
                "title": detail["title"],
                "url": detail["url"],
                "classification": llm_result.get("classification", "unknown"),
                "discussion_score": llm_result.get("discussion_score", 0),
                "rationale": llm_result.get("classification_rationale", ""),
            })

    await emit(f"LLM: full profile assessment for {username}...")
    overall = await llm_analyze_full_profile(
        username, profile, own_repos, deep_repos, all_prs, issues_filed, event_summary, target_pr_analyses, db,
        activity_signals=activity_signals, pr_samples=pr_samples, commit_messages=commit_messages,
    )

    result = {
        "username": username,
        "profile": profile,
        "event_summary": event_summary,
        "activity_signals": activity_signals,
        "own_repos": own_repos,
        "deep_repos": deep_repos,
        "all_prs": all_prs,
        "issues_filed": issues_filed,
        "target_repo_pr_analyses": target_pr_analyses,
        "pr_analyses": target_pr_analyses,
        "overall": overall,
    }
    await progress_queue.put({"type": "result", "data": result})
    await emit(f"Done analyzing {username}!")
    return result


async def analyze_bulk(
    repo: str,
    label: str,
    db,
    progress_queue: asyncio.Queue,
) -> list:
    """
    Analyze all contributors with a given label in a repo.
    Full GitHub profile analysis for each.
    """
    async def emit(msg: str):
        await progress_queue.put({"type": "progress", "message": msg})

    await emit("Checking GitHub CLI auth...")
    await ensure_gh_auth()

    await emit(f"Fetching PRs with label '{label}' in {repo}...")
    items = await gh_search_async(f"repo:{repo}+label:{label}+is:pr", db)

    by_user: dict[str, list] = {}
    for item in items:
        user = item.get("user", {}).get("login", "")
        if user and user.lower() not in MAINTAINERS and "[bot]" not in user:
            by_user.setdefault(user, []).append(item["number"])

    await emit(f"Found {len(by_user)} contributors: {', '.join(sorted(by_user.keys()))}")
    await emit("Fetching repo merge time baseline...")
    repo_median = await get_repo_median_merge_hours(repo, db)

    sem = asyncio.Semaphore(4)  # max 4 users analyzed concurrently

    async def analyze_user(username: str, pr_numbers: list) -> dict:
        async with sem:
            await emit(f"[{username}] Starting full profile analysis...")

            await emit(f"[{username}] Fetching profile + events + repos + all PRs...")
            profile, events, own_repos, all_prs, issues_filed = await asyncio.gather(
                get_user_profile(username, db),
                get_user_events(username, db),
                get_user_repos(username, db),
                get_user_all_prs(username, db),
                get_user_issues_filed(username, db),
            )
            event_summary = summarize_events(events)
            await emit(f"[{username}] {event_summary['total_events']} events, {event_summary['total_commits']} commits, {len(own_repos)} repos, {len(all_prs)} PRs total")

            deep_repos, pr_samples = await asyncio.gather(
                get_top_repos_deep(username, own_repos, db),
                get_pr_sample_stats(all_prs, db),
            )
            commit_messages = extract_commit_messages(events)
            activity_signals = compute_activity_signals(all_prs, event_summary, pr_samples)
            await emit(
                f"[{username}] Signals: merge_rate={activity_signals['merge_rate_pct']}%, "
                f"prs/wk={activity_signals['prs_per_week']}, "
                f"trivial={activity_signals['trivial_pr_rate_pct']}%, "
                f"orgs={activity_signals['unique_orgs']}"
            )

            await emit(f"[{username}] Fetching {len(pr_numbers)} target-repo PR(s) in parallel...")
            pr_details = [
                d for d in await asyncio.gather(*[get_pr_details(n, repo, db) for n in pr_numbers])
                if d
            ]

            target_pr_analyses = []
            if pr_details:
                await emit(f"[{username}] LLM: analyzing {len(pr_details)} PR(s) in parallel...")
                llm_results = await asyncio.gather(*[llm_analyze_pr(d, repo_median, db) for d in pr_details])
                for detail, llm_result in zip(pr_details, llm_results):
                    target_pr_analyses.append({
                        "number": detail["number"],
                        "title": detail["title"],
                        "url": detail["url"],
                        "classification": llm_result.get("classification", "unknown"),
                        "discussion_score": llm_result.get("discussion_score", 0),
                        "rationale": llm_result.get("classification_rationale", ""),
                    })

            await emit(f"[{username}] LLM: full profile assessment...")
            overall = await llm_analyze_full_profile(
                username, profile, own_repos, deep_repos, all_prs, issues_filed, event_summary, target_pr_analyses, db,
                activity_signals=activity_signals, pr_samples=pr_samples, commit_messages=commit_messages,
            )

            user_result = {
                "username": username,
                "profile": profile,
                "event_summary": event_summary,
                "activity_signals": activity_signals,
                "own_repos": own_repos,
                "deep_repos": deep_repos,
                "all_prs": all_prs,
                "issues_filed": issues_filed,
                "target_repo_pr_analyses": target_pr_analyses,
                "pr_analyses": target_pr_analyses,
                "overall": overall,
            }
            await progress_queue.put({"type": "partial_result", "data": user_result})
            await emit(f"[{username}] Done!")
            return user_result

    all_results = list(await asyncio.gather(*[
        analyze_user(username, pr_numbers)
        for username, pr_numbers in by_user.items()
    ]))

    await progress_queue.put({"type": "result", "data": all_results})
    await emit("All done!")
    return all_results
