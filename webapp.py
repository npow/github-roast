"""
GitHub Activity Profiler — FastAPI Web Application
Uses the `gh` CLI for all GitHub API calls (no OAuth needed).
"""

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator
from urllib.parse import quote_plus

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from analyzer import analyze_bulk, analyze_single_user
from db import db

app = FastAPI(title="GitHub Roast")


@app.on_event("startup")
def startup():
    db.init()


# ── SSE fan-out ───────────────────────────────────────────────────────────────

_sse_queues: dict[str, list[asyncio.Queue]] = {}


def get_queue(job_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _sse_queues.setdefault(job_id, []).append(q)
    return q


async def broadcast(job_id: str, event: dict):
    for q in _sse_queues.get(job_id, []):
        await q.put(event)


async def run_job(job_id: str, job_type: str, input_data: dict):
    progress_queue: asyncio.Queue = asyncio.Queue()
    await db.update_job(job_id, "running")

    async def forwarder():
        while True:
            event = await progress_queue.get()
            await broadcast(job_id, event)
            if event.get("type") == "result":
                break

    forwarder_task = asyncio.create_task(forwarder())
    try:
        if job_type == "single":
            result = await analyze_single_user(
                username=input_data["username"],
                repo=input_data["repo"],
                db=db,
                progress_queue=progress_queue,
            )
        else:
            result = await analyze_bulk(
                repo=input_data["repo"],
                label=input_data["label"],
                db=db,
                progress_queue=progress_queue,
            )
        await db.update_job(job_id, "done", result=result)
        await broadcast(job_id, {"type": "done"})
    except Exception as e:
        await db.update_job(job_id, "error", error=str(e))
        await broadcast(job_id, {"type": "error", "message": str(e)})
    finally:
        await forwarder_task


# ── HTML helpers ──────────────────────────────────────────────────────────────

TAILWIND = '<script src="https://cdn.tailwindcss.com"></script>'

PAGE = """<!DOCTYPE html>
<html lang="en" class="h-full">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — GitHub Roast</title>
{tailwind}
<style>
  :root {{
    color-scheme: light dark;
  }}
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; }}
  [data-theme="dark"] body {{ background:#030712; color:#f3f4f6; }}
  [data-theme="light"] body {{ background:#f3f4f6; color:#0f172a; }}
  .badge-strong-yes  {{ background:#16a34a;color:#fff; }}
  .badge-yes         {{ background:#4ade80;color:#14532d; }}
  .badge-maybe       {{ background:#facc15;color:#713f12; }}
  .badge-no          {{ background:#f97316;color:#fff; }}
  .badge-strong-no   {{ background:#dc2626;color:#fff; }}
  .badge             {{ padding:2px 10px;border-radius:9999px;font-size:.75rem;font-weight:600;display:inline-block; }}
  .log-line          {{ font-family:ui-monospace,monospace;font-size:.8rem;color:#a3e635;padding:1px 0; }}
  @keyframes pdot    {{ 0%,100%{{opacity:1}}50%{{opacity:.3}} }}
  .pulse-dot         {{ animation:pdot 1.2s ease-in-out infinite;display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;margin-right:6px; }}
  [data-theme="light"] .bg-gray-950 {{ background:#f3f4f6 !important; }}
  [data-theme="light"] .bg-gray-900,
  [data-theme="light"] .bg-gray-900\\/50,
  [data-theme="light"] .bg-gray-900\\/60,
  [data-theme="light"] .bg-gray-900\\/80,
  [data-theme="light"] .bg-gray-800,
  [data-theme="light"] .bg-gray-800\\/40,
  [data-theme="light"] .bg-gray-800\\/50 {{ background:#ffffff !important; }}
  [data-theme="light"] nav {{ background:#ffffff !important; }}
  [data-theme="light"] .border-gray-800,
  [data-theme="light"] .border-gray-700,
  [data-theme="light"] .border-gray-600 {{ border-color:#cbd5e1 !important; }}
  [data-theme="light"] .text-gray-100,
  [data-theme="light"] .text-gray-300 {{ color:#0f172a !important; }}
  [data-theme="light"] .text-gray-400,
  [data-theme="light"] .text-gray-500 {{ color:#475569 !important; }}
  [data-theme="light"] .text-blue-300,
  [data-theme="light"] .text-blue-400 {{ color:#1d4ed8 !important; }}
  [data-theme="light"] .text-red-400 {{ color:#b91c1c !important; }}
  [data-theme="light"] .text-green-400 {{ color:#166534 !important; }}
  [data-theme="light"] .bg-gray-700 {{ background:#e2e8f0 !important; color:#0f172a !important; }}
  [data-theme="light"] .bg-blue-900\\/30 {{ background:#dbeafe !important; }}
  [data-theme="light"] input,
  [data-theme="light"] select,
  [data-theme="light"] textarea {{
    color:#111827 !important;
    background:#fff !important;
  }}
  [data-theme="light"] a {{ color:#1d4ed8; }}
  [data-theme="light"] a:hover {{ color:#1e40af; }}
  [data-theme="light"] a.text-white,
  [data-theme="light"] a.text-white:hover {{ color:#ffffff !important; }}
  [data-theme="light"] .log-line {{ color:#166534; }}
</style>
<script>
  (function() {{
    const saved = localStorage.getItem("theme") || "system";
    const resolved = saved === "system"
      ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
      : saved;
    document.documentElement.setAttribute("data-theme", resolved);
  }})();
</script>
</head>
<body class="h-full text-gray-100">
<nav class="border-b border-gray-800 bg-gray-900/50">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
    <a href="/" class="font-bold text-lg">GitHub Roast</a>
    <div class="flex items-center gap-2 text-xs">
      <span class="text-gray-400">Theme</span>
      <select id="theme-select" class="bg-gray-800 border border-gray-600 rounded px-2 py-1">
        <option value="system">System</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
    </div>
  </div>
</nav>
<main class="max-w-7xl mx-auto px-4 py-8">
{body}
</main>
<script>
  (function() {{
    const sel = document.getElementById("theme-select");
    if (!sel) return;
    sel.value = localStorage.getItem("theme") || "system";
    sel.addEventListener("change", function() {{
      const val = sel.value;
      localStorage.setItem("theme", val);
      const resolved = val === "system"
        ? (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")
        : val;
      document.documentElement.setAttribute("data-theme", resolved);
    }});
  }})();
</script>
</body>
</html>"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, tailwind=TAILWIND, body=body))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    recent = await db.list_recent_jobs(limit=12)
    recent_items = []
    for job in recent:
        kind = "Single" if job["type"] == "single" else "Cohort"
        if job["type"] == "single":
            username = (job["input"].get("username", "") or "unknown").strip()
            repo = (job["input"].get("repo", "") or "").strip()
            href = f"/{username}"
            if repo:
                href += f"?repo={quote_plus(repo)}"
            target = username
        else:
            job_id = job["id"]
            href = f"/job/{job_id}"
            target = f"{job['input'].get('repo', '')} [{job['input'].get('label', '')}]"
        recent_items.append(
            f"<a href='{href}' class='block px-3 py-2 rounded border border-gray-700 hover:bg-gray-800/40'>"
            f"<div class='text-sm font-semibold'>{target}</div>"
            f"<div class='text-xs text-gray-400'>{kind} · {job['status']} · {job['created_at'][:19]}Z</div>"
            "</a>"
        )
    recent_html = "".join(recent_items) if recent_items else "<p class='text-sm text-gray-500'>No roasts yet.</p>"

    body = """
    <div class="text-center mb-10">
      <h1 class="text-4xl font-extrabold tracking-tight mb-3">GitHub Roast</h1>
      <p class="text-gray-400 text-lg max-w-2xl mx-auto">
        Roast GitHub profiles with signal-based scoring: contribution quality, authenticity, and discussion depth.
      </p>
    </div>

    <div class="bg-gray-900 border border-gray-700 rounded-2xl p-6 mb-6">
      <div class="flex gap-2 mb-4">
        <button id="tab-single" class="px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-semibold">Single Roast</button>
        <button id="tab-bulk" class="px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 text-sm font-semibold">Cohort Rank</button>
      </div>

      <div id="panel-single">
        <p class="text-gray-400 text-sm mb-4">Analyze one profile. Repository is optional.</p>
        <form action="/analyze/single" method="post" class="space-y-3">
          <div>
            <label class="block text-xs text-gray-400 mb-1">GitHub Username</label>
            <input name="username" type="text" value="torvalds" required
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <div>
            <label class="block text-xs text-gray-400 mb-1">Repository (Optional)</label>
            <input name="repo" type="text" value="" placeholder="owner/repo"
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <button class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 rounded-lg text-sm transition">
            Roast Profile
          </button>
        </form>
      </div>

      <div id="panel-bulk" class="hidden">
        <p class="text-gray-400 text-sm mb-4">Rank contributors from a labeled PR cohort.</p>
        <form action="/analyze/bulk" method="post" class="space-y-3">
          <div>
            <label class="block text-xs text-gray-400 mb-1">Repository</label>
            <input name="repo" type="text" placeholder="owner/repo" required
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <div>
            <label class="block text-xs text-gray-400 mb-1">Label</label>
            <input name="label" type="text" value="gsoc" placeholder="e.g. gsoc"
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <button class="w-full bg-purple-600 hover:bg-purple-700 text-white font-semibold py-2 rounded-lg text-sm transition">
            Rank Cohort
          </button>
        </form>
      </div>
    </div>

    <div class="bg-gray-900 border border-gray-700 rounded-2xl p-6">
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-lg font-bold">Recent Roasts</h2>
        <a href="/api/jobs/recent" class="text-xs text-blue-400 hover:underline">JSON</a>
      </div>
      <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-2">
        __RECENT_JOBS__
      </div>
    </div>

    <script>
      (function() {
        const singleBtn = document.getElementById('tab-single');
        const bulkBtn = document.getElementById('tab-bulk');
        const singlePanel = document.getElementById('panel-single');
        const bulkPanel = document.getElementById('panel-bulk');

        function setTab(kind) {
          const single = kind === 'single';
          singlePanel.classList.toggle('hidden', !single);
          bulkPanel.classList.toggle('hidden', single);
          singleBtn.className = single
            ? 'px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-semibold'
            : 'px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 text-sm font-semibold';
          bulkBtn.className = single
            ? 'px-4 py-2 rounded-lg bg-gray-800 border border-gray-600 text-sm font-semibold'
            : 'px-4 py-2 rounded-lg bg-blue-600 text-white text-sm font-semibold';
        }
        singleBtn.addEventListener('click', function(){ setTab('single'); });
        bulkBtn.addEventListener('click', function(){ setTab('bulk'); });
      })();
    </script>
    """
    return page("Home", body.replace("__RECENT_JOBS__", recent_html))


@app.post("/analyze/single")
async def analyze_single(
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    repo: str = Form(""),
):
    input_data = {"username": username.strip(), "repo": repo.strip()}
    job_id = await db.create_job("single", input_data)
    background_tasks.add_task(run_job, job_id, "single", input_data)
    share_path = f"/{input_data['username']}"
    if input_data["repo"]:
        share_path += f"?repo={quote_plus(input_data['repo'])}"
    return RedirectResponse(share_path, status_code=303)


@app.post("/analyze/bulk")
async def analyze_bulk_route(
    background_tasks: BackgroundTasks,
    repo: str = Form(""),
    label: str = Form("gsoc"),
):
    input_data = {"repo": repo.strip(), "label": label.strip()}
    job_id = await db.create_job("bulk", input_data)
    background_tasks.add_task(run_job, job_id, "bulk", input_data)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.get("/job/{job_id}", response_class=HTMLResponse)
async def job_page(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        return page("Not Found", "<p class='text-gray-400'>Job not found.</p>")

    status = job["status"]
    job_type = job["type"]
    inp = job["input"]

    if job_type == "single":
        title_str = f"Analysis: {inp.get('username', '')}"
        share_path = f"/{inp.get('username', '').strip()}"
        if inp.get("repo", "").strip():
            share_path += f"?repo={quote_plus(inp.get('repo', '').strip())}"
        share_cta = f"""
        <button id="share-copy-btn" type="button" class="text-xs text-white bg-blue-700 hover:bg-blue-600 px-3 py-1 rounded transition">Copy share link</button>
        """
    else:
        title_str = f"Bulk Rank: {inp.get('repo', '')} [{inp.get('label', '')}]"
        share_cta = ""

    icon = {"queued": "⏳", "running": "🔄", "done": "✅", "error": "❌"}.get(status, "?")

    body = f"""
    <div class="mb-6 flex items-center justify-between gap-3 flex-wrap">
      <div class="flex items-center gap-3">
      <span class="text-2xl">{icon}</span>
      <div>
        <h1 class="text-2xl font-bold">{title_str}</h1>
        <p class="text-gray-400 text-sm">Status: {status}</p>
      </div>
      </div>
      <div class="flex items-center gap-2">
        {share_cta}
        <a href="/" class="text-xs bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded transition">Back home</a>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
        <div class="flex items-center gap-2 mb-3">
          <span id="pulse" class="pulse-dot" {'style="display:none"' if status in ("done","error") else ""}></span>
          <h2 class="font-semibold text-sm text-gray-300">Live Progress</h2>
        </div>
        <div id="phase-progress" class="mb-3 space-y-2 text-xs">
          <div>
            <div class="flex justify-between text-gray-400 mb-1">
              <span>PR fetch</span><span id="fetch-count">0/0</span>
            </div>
            <div class="h-1.5 bg-gray-700 rounded-full"><div id="fetch-bar" class="h-1.5 bg-blue-500 rounded-full" style="width:0%"></div></div>
          </div>
          <div>
            <div class="flex justify-between text-gray-400 mb-1">
              <span>LLM analysis</span><span id="llm-count">0/0</span>
            </div>
            <div class="h-1.5 bg-gray-700 rounded-full"><div id="llm-bar" class="h-1.5 bg-purple-500 rounded-full" style="width:0%"></div></div>
          </div>
        </div>
        <div id="phase-note" class="mb-3 text-xs text-gray-500 hidden"></div>
        <div id="log" class="h-80 overflow-y-auto bg-gray-950 rounded-lg p-3 space-y-0.5">
          {'<div class="log-line text-gray-500">Waiting...</div>' if status == "queued" else ""}
        </div>
      </div>

      <div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
        <h2 class="font-semibold text-sm text-gray-300 mb-3">Summary</h2>
        <div id="summary-content">
          <p class="text-gray-500 text-sm">Results will appear here...</p>
        </div>
      </div>
    </div>

    <div id="full-results" class="mt-8"></div>

    <script>
    (function(){{
      const jobId = {json.dumps(job_id)};
      const jobType = {json.dumps(job_type)};
      const currentStatus = {json.dumps(status)};
      const hasTargetRepo = {json.dumps(bool(inp.get("repo", "").strip()))};
      const sharePath = {json.dumps(share_path if job_type == "single" else "")};

      const log = document.getElementById('log');
      const summary = document.getElementById('summary-content');
      const fullResults = document.getElementById('full-results');
      const pulse = document.getElementById('pulse');
      const fetchBar = document.getElementById('fetch-bar');
      const llmBar = document.getElementById('llm-bar');
      const fetchCount = document.getElementById('fetch-count');
      const llmCount = document.getElementById('llm-count');
      const phaseProgress = document.getElementById('phase-progress');
      const phaseNote = document.getElementById('phase-note');
      const phaseState = {{ fetchDone: 0, fetchTotal: 0, llmDone: 0, llmTotal: 0 }};

      function addLog(msg) {{
        const d = document.createElement('div');
        d.className = 'log-line';
        d.textContent = '▶ ' + msg;
        log.appendChild(d);
        log.scrollTop = log.scrollHeight;
      }}

      function stopPulse() {{ if (pulse) pulse.style.display = 'none'; }}
      function renderPhase() {{
        const fp = phaseState.fetchTotal > 0 ? Math.min(100, Math.round((phaseState.fetchDone / phaseState.fetchTotal) * 100)) : 0;
        const lp = phaseState.llmTotal > 0 ? Math.min(100, Math.round((phaseState.llmDone / phaseState.llmTotal) * 100)) : 0;
        fetchBar.style.width = fp + '%';
        llmBar.style.width = lp + '%';
        fetchCount.textContent = `${{phaseState.fetchDone}}/${{phaseState.fetchTotal}}`;
        llmCount.textContent = `${{phaseState.llmDone}}/${{phaseState.llmTotal}}`;

        const hasDetailedPhases = phaseState.fetchTotal > 0 || phaseState.llmTotal > 0;
        if (hasDetailedPhases) {{
          phaseProgress.classList.remove('hidden');
          phaseNote.classList.add('hidden');
          return;
        }}

        phaseProgress.classList.add('hidden');
        phaseNote.classList.remove('hidden');
        if (!hasTargetRepo) {{
          phaseNote.textContent = 'Detailed PR phase is only shown when a target repo is specified.';
        }} else if (currentStatus === 'done') {{
          phaseNote.textContent = 'No target-repo PRs found for this user.';
        }} else {{
          phaseNote.textContent = 'Waiting to discover target-repo PRs...';
        }}
      }}

      function badge(rec) {{
        const cls = {{'strong yes':'badge-strong-yes','yes':'badge-yes','maybe':'badge-maybe','no':'badge-no','strong no':'badge-strong-no'}}[rec]||'badge-maybe';
        return `<span class="badge ${{cls}}">${{rec}}</span>`;
      }}

      function escHtml(s) {{
        return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');
      }}

      function linkifyRefText(text) {{
        let out = escHtml(text);
        const refRe = /(^|[\\s(\\[])([A-Za-z][A-Za-z0-9-]{0,38}\\/[A-Za-z0-9._-]+)(?:\\s+#(\\d+))?/g;
        out = out.replace(refRe, (m, prefix, repo, pr) => {{
          if (pr) {{
            return `${{prefix}}<a href="https://github.com/${{repo}}/pull/${{pr}}" target="_blank" class="hover:underline text-blue-300">${{repo}} #${{pr}}</a>`;
          }}
          return `${{prefix}}<a href="https://github.com/${{repo}}" target="_blank" class="hover:underline text-blue-300">${{repo}}</a>`;
        }});
        return out;
      }}

      function clfBadge(clf) {{
        const m = {{'substantive-code':'bg-blue-600 text-white','trivial-code':'bg-blue-900 text-blue-200','docs-only':'bg-gray-600 text-gray-200','test-only':'bg-purple-700 text-white','config-only':'bg-gray-700 text-gray-300','manufactured':'bg-red-700 text-red-100','unknown':'bg-gray-800 text-gray-400'}};
        return `<span class="badge ${{m[clf]||'bg-gray-800 text-gray-400'}}">${{clf}}</span>`;
      }}

      function prStatus(p) {{
        if (p && p.merged) return 'merged';
        const st = String((p && p.state) || '').toLowerCase();
        if (st === 'closed') return 'closed-not-merged';
        if (st === 'open') return 'open';
        if (p && p.pull_request && p.pull_request.merged_at) return 'merged';
        return st || 'unknown';
      }}

      function prStatusBadge(p) {{
        const s = prStatus(p);
        if (s === 'merged') return '<span class="badge bg-green-700 text-white">merged</span>';
        if (s === 'closed-not-merged') return '<span class="badge bg-amber-700 text-white">closed (not merged)</span>';
        if (s === 'open') return '<span class="badge bg-blue-700 text-white">open</span>';
        return `<span class="badge bg-gray-700 text-gray-200">${{escHtml(s)}}</span>`;
      }}

      function bar(label, val) {{
        const pct = Math.min(100, Math.round((val/10)*100));
        const c = pct>=70?'bg-green-500':pct>=40?'bg-yellow-500':'bg-red-500';
        return `<div class="mb-2"><div class="flex justify-between text-xs mb-0.5"><span class="text-gray-400">${{label}}</span><span class="font-mono">${{val.toFixed(1)}}/10</span></div><div class="h-1.5 bg-gray-700 rounded-full"><div class="${{c}} h-1.5 rounded-full" style="width:${{pct}}%"></div></div></div>`;
      }}

      function renderSingleSummary(data) {{
        const o = data.overall||{{}};
        if ((o.executive_summary||'').startsWith('Error:')) {{
          summary.innerHTML = `
            <div class="rounded-lg border border-red-700 bg-red-950/40 p-3">
              <div class="font-semibold text-red-300 mb-1">Analysis failed</div>
              <p class="text-sm text-red-200">${{o.executive_summary}}</p>
              <p class="text-xs text-gray-400 mt-2">Check GitHub CLI auth on the server (gh auth status).</p>
            </div>
          `;
          return;
        }}
        summary.innerHTML = `
          <div class="flex items-center gap-4 mb-3">
            <div class="text-4xl font-black">${{(o.overall_score||0).toFixed(1)}}</div>
            <div><div class="font-bold">${{data.username}}</div>${{badge(o.recommendation||'?')}}</div>
          </div>
          ${{bar('Contribution Quality',o.contribution_quality||0)}}
          ${{bar('Portfolio Authenticity',o.portfolio_authenticity||0)}}
          ${{bar('Discussion Depth',o.discussion_depth||0)}}
          ${{bar('Repo Quality',o.repo_quality||0)}}
          <p class="text-sm text-gray-300 mt-2">${{o.executive_summary||''}}</p>
        `;
      }}

      function renderSingleFull(data) {{
        const o = data.overall||{{}};
        const prs = data.pr_analyses||[];
        const allPrs = data.all_prs||[];
        const repos = data.own_repos||[];
        const flags = (o.red_flags||[]).map(f=>`<li class="text-sm text-gray-300 bg-gray-800 rounded px-3 py-2">${{linkifyRefText(f)}}</li>`).join('');
        const strengths = (o.strengths||[]).map(s=>`<li class="text-sm text-gray-300 bg-gray-800 rounded px-3 py-2">${{linkifyRefText(s)}}</li>`).join('');
        const prRows = prs.map(p=>`<tr class="border-b border-gray-800 hover:bg-gray-800/40">
          <td class="py-2 px-3"><a href="${{p.url}}" target="_blank" class="text-blue-400 hover:underline text-sm">${{(p.title||'').slice(0,70)}}</a></td>
          <td class="py-2 px-3">${{prStatusBadge(p)}}</td>
          <td class="py-2 px-3">${{clfBadge(p.classification)}}</td>
          <td class="py-2 px-3 text-center font-mono text-sm">${{p.discussion_score.toFixed(1)}}</td>
          <td class="py-2 px-3 text-xs text-gray-400">${{(p.rationale||'').slice(0,120)}}</td>
        </tr>`).join('');
        const allPrRows = allPrs.slice(0, 20).map(p=>{{
          const href = `https://github.com/${{p.repo}}/pull/${{p.number}}`;
          return `<tr class="border-b border-gray-800 hover:bg-gray-800/40">
            <td class="py-2 px-3"><a href="${{href}}" target="_blank" class="text-blue-400 hover:underline text-sm">${{(p.title||'').slice(0,90)}}</a></td>
            <td class="py-2 px-3 text-xs text-gray-400">${{p.repo || ''}}</td>
            <td class="py-2 px-3 text-xs text-gray-400">#${{p.number || ''}}</td>
            <td class="py-2 px-3 text-xs text-gray-400">${{prStatusBadge(p)}}</td>
          </tr>`;
        }}).join('');
        function sortRepos(items, mode) {{
          const arr = [...items];
          if (mode === 'pushed') return arr.sort((a,b)=>Date.parse(b.pushed_at||0)-Date.parse(a.pushed_at||0));
          if (mode === 'name') return arr.sort((a,b)=>(a.name||'').localeCompare(b.name||''));
          return arr.sort((a,b)=>(b.stars||0)-(a.stars||0));
        }}
        function repoCardsHtml(items) {{
          return items.map(r=>`<div class="bg-gray-800 rounded-lg p-3">
            <div class="font-mono text-sm font-bold text-blue-300">${{r.name}}</div>
            <div class="text-xs text-gray-400 mt-1">${{r.language||'?'}} · ⭐${{r.stars||0}}</div>
            <div class="text-xs text-gray-500 mt-1 truncate">${{r.description||''}}</div>
          </div>`).join('');
        }}

        fullResults.innerHTML = `
          <div class="bg-gray-900 border border-gray-700 rounded-xl p-6 mb-6">
            <h2 class="text-xl font-bold mb-1"><a href="https://github.com/${{data.username}}" target="_blank" class="hover:text-blue-400">${{data.username}}</a></h2>
            <p class="text-gray-300 text-sm mb-4">${{o.executive_summary||''}}</p>
            <div class="grid sm:grid-cols-2 gap-x-6">
              ${{bar('Contribution Quality',o.contribution_quality||0)}}
              ${{bar('Portfolio Authenticity',o.portfolio_authenticity||0)}}
              ${{bar('Discussion Depth',o.discussion_depth||0)}}
              ${{bar('Repo Quality',o.repo_quality||0)}}
            </div>
            ${{flags?'<h3 class="text-sm font-semibold mt-4">Red Flags</h3><ul class="mt-2 space-y-2">'+flags+'</ul>':''}}
            ${{strengths?'<h3 class="text-sm font-semibold mt-3">Strengths</h3><ul class="mt-2 space-y-2">'+strengths+'</ul>':''}}
          </div>
          ${{prs.length?`<div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden mb-6">
            <div class="px-4 py-3 border-b border-gray-700 font-semibold">PRs Analyzed</div>
            <table class="w-full text-sm"><thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-2 px-3">Title</th><th class="py-2 px-3">Status</th><th class="py-2 px-3">Type</th><th class="py-2 px-3 text-center">Discussion</th><th class="py-2 px-3">Rationale</th>
            </tr></thead><tbody>${{prRows}}</tbody></table></div>`:''}}
          ${{allPrRows?`<div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden mb-6">
            <div class="px-4 py-3 border-b border-gray-700 font-semibold">Recent Public PRs</div>
            <table class="w-full text-sm"><thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-2 px-3">Title</th><th class="py-2 px-3">Repo</th><th class="py-2 px-3">#</th><th class="py-2 px-3">State</th>
            </tr></thead><tbody>${{allPrRows}}</tbody></table></div>`:''}}
          ${{repos.length?`<div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
            <div class="flex items-center justify-between gap-3 mb-3">
              <h3 class="font-semibold">Own Repos</h3>
              <label class="text-xs text-gray-400">Sort
                <select id="repo-sort" class="ml-1 bg-gray-800 border border-gray-600 rounded px-2 py-1">
                  <option value="stars">Stars</option>
                  <option value="pushed">Recently Pushed</option>
                  <option value="name">Name</option>
                </select>
              </label>
            </div>
            <div id="repo-grid" class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${{repoCardsHtml(sortRepos(repos,'stars'))}}</div></div>`:''}}
        `;
        const repoSort = document.getElementById('repo-sort');
        const repoGrid = document.getElementById('repo-grid');
        if (repoSort && repoGrid) {{
          repoSort.addEventListener('change', function() {{
            repoGrid.innerHTML = repoCardsHtml(sortRepos(repos, repoSort.value));
          }});
        }}
      }}

      function renderBulkPartial(data) {{
        const o = data.overall||{{}};
        let partial = document.getElementById('bulk-partial');
        if (!partial) {{
          summary.innerHTML = '<p class="text-sm text-gray-400 mb-2">Building ranking...</p><div id="bulk-partial"></div>';
          partial = document.getElementById('bulk-partial');
        }}
        const row = document.createElement('div');
        row.className = 'flex items-center gap-3 py-1 border-b border-gray-800 text-sm';
        row.innerHTML = `<span class="font-mono text-blue-300">${{data.username}}</span><span class="ml-auto font-bold">${{(o.overall_score||0).toFixed(1)}}</span>${{badge(o.recommendation||'?')}}`;
        partial.appendChild(row);
      }}

      function renderBulkFull(results) {{
        const sorted = [...results].sort((a,b)=>(b.overall?.overall_score||0)-(a.overall?.overall_score||0));
        let filterRec = '', filterSearch = '';

        function buildTable() {{
          const filtered = sorted.filter(r=>{{
            const o = r.overall||{{}};
            if (filterRec && o.recommendation!==filterRec) return false;
            if (filterSearch && !r.username.toLowerCase().includes(filterSearch.toLowerCase())) return false;
            return true;
          }});
          return `<table class="w-full text-sm">
            <thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-2 px-3">Rank</th><th class="py-2 px-3">User</th><th class="py-2 px-3">Score</th>
              <th class="py-2 px-3">Recommendation</th><th class="py-2 px-3">PRs</th><th class="py-2 px-3">Top Flags</th>
            </tr></thead>
            <tbody>${{filtered.map((r,i)=>{{
              const o=r.overall||{{}};
              const flags=(o.red_flags||[]).slice(0,2).join(', ')||'—';
              return `<tr class="border-b border-gray-800 hover:bg-gray-800/50 cursor-pointer" onclick="showPanel(${{JSON.stringify(r)}})">
                <td class="py-2 px-3 text-gray-400">${{i+1}}</td>
                <td class="py-2 px-3 font-mono text-blue-300"><a href="https://github.com/${{r.username}}" target="_blank" class="hover:underline">${{r.username}}</a></td>
                <td class="py-2 px-3 font-bold font-mono">${{(o.overall_score||0).toFixed(1)}}</td>
                <td class="py-2 px-3">${{badge(o.recommendation||'?')}}</td>
                <td class="py-2 px-3 text-sm text-gray-400">${{(r.pr_analyses||[]).length}}</td>
                <td class="py-2 px-3 text-xs text-gray-500 max-w-xs truncate">${{flags}}</td>
              </tr>`;
            }}).join('')}}</tbody>
          </table>`;
        }}

        const recs = [...new Set(sorted.map(r=>r.overall?.recommendation||'').filter(Boolean))];
        const chips = recs.map(r=>`<button onclick="setRec('${{r}}')" data-rec="${{r}}" class="rec-chip px-3 py-1 rounded-full text-xs border border-gray-600 hover:border-blue-400 transition">${{r}}</button>`).join('');

        fullResults.innerHTML = `
          <div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden">
            <div class="px-4 py-3 border-b border-gray-700 flex flex-wrap gap-3 items-center">
              <span class="font-semibold">${{sorted.length}} Contributors</span>
              <div class="flex gap-2 flex-wrap">${{chips}}</div>
              <input id="search-inp" type="text" placeholder="Search..." oninput="setSearch(this.value)"
                class="ml-auto bg-gray-800 border border-gray-600 rounded px-2 py-1 text-sm w-36 focus:outline-none">
              <a href="/api/results/${{jobId}}" target="_blank" class="text-xs bg-gray-700 hover:bg-gray-600 px-3 py-1 rounded transition">Export JSON</a>
            </div>
            <div id="tbl-wrap" class="overflow-x-auto">${{buildTable()}}</div>
          </div>
          <div id="side-panel" class="fixed top-0 right-0 h-full w-full max-w-2xl bg-gray-900 border-l border-gray-700 shadow-2xl transform translate-x-full transition-transform duration-300 overflow-y-auto z-50 p-6">
            <button onclick="closePanel()" class="absolute top-4 right-4 text-gray-400 hover:text-white text-2xl">&times;</button>
            <div id="panel-body"></div>
          </div>
          <div id="overlay" class="fixed inset-0 bg-black/50 z-40 hidden" onclick="closePanel()"></div>
        `;

        window.setRec = function(r) {{
          filterRec = filterRec===r?'':r;
          document.querySelectorAll('.rec-chip').forEach(c=>{{
            c.classList.toggle('border-blue-400',c.dataset.rec===filterRec);
            c.classList.toggle('bg-blue-900/30',c.dataset.rec===filterRec);
          }});
          document.getElementById('tbl-wrap').innerHTML = buildTable();
        }};
        window.setSearch = function(v) {{ filterSearch=v; document.getElementById('tbl-wrap').innerHTML=buildTable(); }};
        window.showPanel = function(data) {{
          renderSingleSummary(data);  // reuse for panel
          // render inline in panel instead
          const o=data.overall||{{}};
          const prs=data.pr_analyses||[];
          const flags=(o.red_flags||[]).map(f=>`<li class="text-sm text-gray-300 bg-gray-800 rounded px-3 py-2">${{linkifyRefText(f)}}</li>`).join('');
          const strengths=(o.strengths||[]).map(s=>`<li class="text-sm text-gray-300 bg-gray-800 rounded px-3 py-2">${{linkifyRefText(s)}}</li>`).join('');
          const prRows=prs.map(p=>`<tr class="border-b border-gray-800">
            <td class="py-1.5 px-2"><a href="${{p.url}}" target="_blank" class="text-blue-400 hover:underline text-xs">${{p.title.slice(0,60)}}</a></td>
            <td class="py-1.5 px-2">${{prStatusBadge(p)}}</td>
            <td class="py-1.5 px-2">${{clfBadge(p.classification)}}</td>
            <td class="py-1.5 px-2 font-mono text-xs text-center">${{p.discussion_score.toFixed(1)}}</td>
          </tr>`).join('');
          document.getElementById('panel-body').innerHTML = `
            <h2 class="text-xl font-bold mb-1 mr-8"><a href="https://github.com/${{data.username}}" target="_blank" class="hover:text-blue-400">${{data.username}}</a></h2>
            <div class="flex items-center gap-3 mb-4"><span class="text-3xl font-black">${{(o.overall_score||0).toFixed(1)}}</span>${{badge(o.recommendation||'?')}}</div>
            ${{bar('Contribution Quality',o.contribution_quality||0)}}
            ${{bar('Portfolio Authenticity',o.portfolio_authenticity||0)}}
            ${{bar('Discussion Depth',o.discussion_depth||0)}}
            ${{bar('Repo Quality',o.repo_quality||0)}}
            <p class="text-sm text-gray-300 mt-3 mb-3">${{o.executive_summary||''}}</p>
            ${{flags?'<h3 class="text-sm font-semibold mt-4 mb-2">Red Flags</h3><ul class="space-y-2 mb-3">'+flags+'</ul>':''}}
            ${{strengths?'<h3 class="text-sm font-semibold mt-3 mb-2">Strengths</h3><ul class="space-y-2 mb-3">'+strengths+'</ul>':''}}
            ${{prs.length?`<table class="w-full text-sm mt-2"><thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-1.5 px-2">PR</th><th class="py-1.5 px-2">Status</th><th class="py-1.5 px-2">Type</th><th class="py-1.5 px-2">Score</th>
            </tr></thead><tbody>${{prRows}}</tbody></table>`:''}}
          `;
          document.getElementById('side-panel').classList.remove('translate-x-full');
          document.getElementById('overlay').classList.remove('hidden');
        }};
        window.closePanel = function() {{
          document.getElementById('side-panel').classList.add('translate-x-full');
          document.getElementById('overlay').classList.add('hidden');
        }};
      }}

      const shareBtn = document.getElementById('share-copy-btn');
      if (shareBtn && sharePath) {{
        shareBtn.addEventListener('click', async function() {{
          const shareUrl = window.location.origin + sharePath;
          try {{
            await navigator.clipboard.writeText(shareUrl);
          }} catch (_) {{
            const ta = document.createElement('textarea');
            ta.value = shareUrl;
            ta.style.position = 'fixed';
            ta.style.opacity = '0';
            document.body.appendChild(ta);
            ta.select();
            document.execCommand('copy');
            document.body.removeChild(ta);
          }}
          const prev = shareBtn.textContent;
          shareBtn.textContent = 'Copied';
          setTimeout(() => {{ shareBtn.textContent = prev; }}, 1200);
        }});
      }}

      renderPhase();

      if (currentStatus==='done'||currentStatus==='error') {{
        fetch('/api/results/'+jobId).then(r=>r.json()).then(data=>{{
          stopPulse();
          if (data.status==='done') {{
            if (jobType==='single') {{ renderSingleSummary(data.result); renderSingleFull(data.result); }}
            else renderBulkFull(data.result);
          }}
        }});
      }} else {{
        const es = new EventSource('/stream/'+jobId);
        es.addEventListener('message', function(e) {{
          const evt = JSON.parse(e.data);
          if (evt.type==='progress') addLog(evt.message);
          else if (evt.type==='phase_totals') {{
            if (typeof evt.fetch_total === 'number') phaseState.fetchTotal = evt.fetch_total;
            if (typeof evt.llm_total === 'number') phaseState.llmTotal = evt.llm_total;
            renderPhase();
          }}
          else if (evt.type==='sub_progress') {{
            if (evt.phase==='fetch_prs') phaseState.fetchDone += 1;
            if (evt.phase==='llm_prs') phaseState.llmDone += 1;
            renderPhase();
          }}
          else if (evt.type==='partial_result') renderBulkPartial(evt.data);
          else if (evt.type==='result') {{
            if (jobType==='single') {{ renderSingleSummary(evt.data); renderSingleFull(evt.data); }}
            else renderBulkFull(evt.data);
          }} else if (evt.type==='done') {{
            stopPulse(); addLog('✅ Complete!'); es.close();
          }} else if (evt.type==='error') {{
            stopPulse(); addLog('❌ Error: '+evt.message); es.close();
          }}
        }});
        es.onerror = function() {{ addLog('Connection lost.'); es.close(); }};
      }}
    }})();
    </script>
    """
    return page(title_str, body)


@app.get("/stream/{job_id}")
async def stream(job_id: str, request: Request):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404)

    async def events() -> AsyncIterator[str]:
        if job["status"] == "done" and job["result"]:
            yield f"data: {json.dumps({'type': 'result', 'data': job['result']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"
            return
        if job["status"] == "error":
            yield f"data: {json.dumps({'type': 'error', 'message': job.get('error', 'Unknown')})}\n\n"
            return

        q = get_queue(job_id)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"
                    if event.get("type") in ("done", "error"):
                        break
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            qs = _sse_queues.get(job_id, [])
            if q in qs:
                qs.remove(q)

    return StreamingResponse(events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


@app.get("/api/results/{job_id}")
async def api_results(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404)
    return JSONResponse({
        "job_id": job_id,
        "type": job["type"],
        "status": job["status"],
        "input": job["input"],
        "result": job["result"],
        "created_at": job["created_at"],
    })


@app.get("/api/jobs/recent")
async def api_recent_jobs(limit: int = 20):
    jobs = await db.list_recent_jobs(limit=max(1, min(limit, 100)))
    return JSONResponse({"jobs": jobs})


@app.get("/{username}", include_in_schema=False)
async def share_profile(
    username: str,
    background_tasks: BackgroundTasks,
    repo: str = "",
):
    reserved = {"analyze", "job", "stream", "api", "docs", "openapi.json", "redoc"}
    if username in reserved:
        raise HTTPException(status_code=404)
    if not re.fullmatch(r"[A-Za-z0-9-]{1,39}", username):
        raise HTTPException(status_code=404)

    clean_repo = repo.strip()
    existing = await db.find_latest_single_job(username=username, repo=clean_repo)
    if existing:
        status = existing.get("status")
        result = existing.get("result") or {}
        exec_summary = ((result.get("overall") or {}).get("executive_summary") or "").strip()
        failed_done = status == "done" and exec_summary.startswith("Error:")
        updated_at = existing.get("updated_at") or existing.get("created_at")
        is_fresh = False
        if updated_at:
            try:
                ts = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
                is_fresh = ts >= (datetime.now(timezone.utc) - timedelta(hours=24))
            except Exception:
                is_fresh = False
        if status in {"queued", "running"} or (status == "done" and not failed_done and is_fresh):
            return await job_page(existing["id"])

    input_data = {"username": username.strip(), "repo": clean_repo}
    job_id = await db.create_job("single", input_data)
    background_tasks.add_task(run_job, job_id, "single", input_data)
    return await job_page(job_id)
