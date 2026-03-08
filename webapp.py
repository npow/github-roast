"""
GitHub Activity Profiler — FastAPI Web Application
Uses the `gh` CLI for all GitHub API calls (no OAuth needed).
"""

import asyncio
import json
from typing import AsyncIterator

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from analyzer import analyze_bulk, analyze_single_user
from db import db

app = FastAPI(title="GitHub Portfolio Intelligence")


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
<html lang="en" class="h-full bg-gray-950">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} — GitHub Portfolio Intelligence</title>
{tailwind}
<style>
  body {{ font-family: ui-sans-serif, system-ui, sans-serif; }}
  .badge-strong-yes  {{ background:#16a34a;color:#fff; }}
  .badge-yes         {{ background:#4ade80;color:#14532d; }}
  .badge-maybe       {{ background:#facc15;color:#713f12; }}
  .badge-no          {{ background:#f97316;color:#fff; }}
  .badge-strong-no   {{ background:#dc2626;color:#fff; }}
  .badge             {{ padding:2px 10px;border-radius:9999px;font-size:.75rem;font-weight:600;display:inline-block; }}
  .log-line          {{ font-family:ui-monospace,monospace;font-size:.8rem;color:#a3e635;padding:1px 0; }}
  @keyframes pdot    {{ 0%,100%{{opacity:1}}50%{{opacity:.3}} }}
  .pulse-dot         {{ animation:pdot 1.2s ease-in-out infinite;display:inline-block;width:8px;height:8px;border-radius:50%;background:#4ade80;margin-right:6px; }}
</style>
</head>
<body class="h-full text-gray-100">
<nav class="border-b border-gray-800 bg-gray-900/50">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center">
    <a href="/" class="font-bold text-lg">🔍 GitHub Portfolio Intelligence</a>
  </div>
</nav>
<main class="max-w-7xl mx-auto px-4 py-8">
{body}
</main>
</body>
</html>"""


def page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(PAGE.format(title=title, tailwind=TAILWIND, body=body))


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index():
    body = """
    <div class="text-center mb-10">
      <h1 class="text-4xl font-extrabold tracking-tight mb-3">GitHub Portfolio Intelligence</h1>
      <p class="text-gray-400 text-lg max-w-2xl mx-auto">
        LLM-powered analysis of GitHub contributions — evaluate PR quality, authenticity, and discussion depth.
      </p>
    </div>

    <div class="grid md:grid-cols-2 gap-6">
      <div class="bg-gray-900 border border-gray-700 rounded-2xl p-6">
        <div class="text-2xl mb-2">👤</div>
        <h2 class="text-xl font-bold mb-1">Single Profile</h2>
        <p class="text-gray-400 text-sm mb-4">Analyze one user's contributions to a repo.</p>
        <form action="/analyze/single" method="post" class="space-y-3">
          <div>
            <label class="block text-xs text-gray-400 mb-1">GitHub Username</label>
            <input name="username" type="text" placeholder="e.g. octocat" required
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <div>
            <label class="block text-xs text-gray-400 mb-1">Repository</label>
            <input name="repo" type="text" value="netflix/metaflow" placeholder="owner/repo"
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <button class="w-full bg-blue-600 hover:bg-blue-700 text-white font-semibold py-2 rounded-lg text-sm transition">
            🔍 Analyze
          </button>
        </form>
      </div>

      <div class="bg-gray-900 border border-gray-700 rounded-2xl p-6">
        <div class="text-2xl mb-2">🏆</div>
        <h2 class="text-xl font-bold mb-1">Rank a Cohort</h2>
        <p class="text-gray-400 text-sm mb-4">Fetch all PRs with a label and rank contributors.</p>
        <form action="/analyze/bulk" method="post" class="space-y-3">
          <div>
            <label class="block text-xs text-gray-400 mb-1">Repository</label>
            <input name="repo" type="text" value="netflix/metaflow" placeholder="owner/repo"
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <div>
            <label class="block text-xs text-gray-400 mb-1">Label</label>
            <input name="label" type="text" value="gsoc" placeholder="e.g. gsoc"
              class="w-full bg-gray-800 border border-gray-600 rounded-lg px-3 py-2 text-sm focus:outline-none focus:border-blue-500">
          </div>
          <button class="w-full bg-purple-600 hover:bg-purple-700 text-white font-semibold py-2 rounded-lg text-sm transition">
            🏆 Fetch &amp; Rank
          </button>
        </form>
      </div>
    </div>
    """
    return page("Home", body)


@app.post("/analyze/single")
async def analyze_single(
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    repo: str = Form("netflix/metaflow"),
):
    input_data = {"username": username.strip(), "repo": repo.strip()}
    job_id = await db.create_job("single", input_data)
    background_tasks.add_task(run_job, job_id, "single", input_data)
    return RedirectResponse(f"/job/{job_id}", status_code=303)


@app.post("/analyze/bulk")
async def analyze_bulk_route(
    background_tasks: BackgroundTasks,
    repo: str = Form("netflix/metaflow"),
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
    else:
        title_str = f"Bulk Rank: {inp.get('repo', '')} [{inp.get('label', '')}]"

    icon = {"queued": "⏳", "running": "🔄", "done": "✅", "error": "❌"}.get(status, "?")

    body = f"""
    <div class="mb-6 flex items-center gap-3">
      <span class="text-2xl">{icon}</span>
      <div>
        <h1 class="text-2xl font-bold">{title_str}</h1>
        <p class="text-gray-400 text-sm">Status: {status}</p>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
      <div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
        <div class="flex items-center gap-2 mb-3">
          <span id="pulse" class="pulse-dot" {'style="display:none"' if status in ("done","error") else ""}></span>
          <h2 class="font-semibold text-sm text-gray-300">Live Progress</h2>
        </div>
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

      const log = document.getElementById('log');
      const summary = document.getElementById('summary-content');
      const fullResults = document.getElementById('full-results');
      const pulse = document.getElementById('pulse');

      function addLog(msg) {{
        const d = document.createElement('div');
        d.className = 'log-line';
        d.textContent = '▶ ' + msg;
        log.appendChild(d);
        log.scrollTop = log.scrollHeight;
      }}

      function stopPulse() {{ if (pulse) pulse.style.display = 'none'; }}

      function badge(rec) {{
        const cls = {{'strong yes':'badge-strong-yes','yes':'badge-yes','maybe':'badge-maybe','no':'badge-no','strong no':'badge-strong-no'}}[rec]||'badge-maybe';
        return `<span class="badge ${{cls}}">${{rec}}</span>`;
      }}

      function clfBadge(clf) {{
        const m = {{'substantive-code':'bg-blue-600 text-white','trivial-code':'bg-blue-900 text-blue-200','docs-only':'bg-gray-600 text-gray-200','test-only':'bg-purple-700 text-white','config-only':'bg-gray-700 text-gray-300','manufactured':'bg-red-700 text-red-100','unknown':'bg-gray-800 text-gray-400'}};
        return `<span class="badge ${{m[clf]||'bg-gray-800 text-gray-400'}}">${{clf}}</span>`;
      }}

      function bar(label, val) {{
        const pct = Math.min(100, Math.round((val/10)*100));
        const c = pct>=70?'bg-green-500':pct>=40?'bg-yellow-500':'bg-red-500';
        return `<div class="mb-2"><div class="flex justify-between text-xs mb-0.5"><span class="text-gray-400">${{label}}</span><span class="font-mono">${{val.toFixed(1)}}/10</span></div><div class="h-1.5 bg-gray-700 rounded-full"><div class="${{c}} h-1.5 rounded-full" style="width:${{pct}}%"></div></div></div>`;
      }}

      function renderSingleSummary(data) {{
        const o = data.overall||{{}};
        summary.innerHTML = `
          <div class="flex items-center gap-4 mb-3">
            <div class="text-4xl font-black">${{(o.overall_score||0).toFixed(1)}}</div>
            <div><div class="font-bold">${{data.username}}</div>${{badge(o.gsoc_recommendation||'?')}}</div>
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
        const repos = data.own_repos||[];
        const flags = (o.red_flags||[]).map(f=>`<span class="badge bg-red-900 text-red-200">${{f}}</span>`).join(' ');
        const strengths = (o.strengths||[]).map(s=>`<span class="badge bg-green-900 text-green-200">${{s}}</span>`).join(' ');
        const prRows = prs.map(p=>`<tr class="border-b border-gray-800 hover:bg-gray-800/40">
          <td class="py-2 px-3"><a href="${{p.url}}" target="_blank" class="text-blue-400 hover:underline text-sm">${{p.title.slice(0,70)}}</a></td>
          <td class="py-2 px-3">${{clfBadge(p.classification)}}</td>
          <td class="py-2 px-3 text-center font-mono text-sm">${{p.discussion_score.toFixed(1)}}</td>
          <td class="py-2 px-3 text-xs text-gray-400">${{p.rationale.slice(0,120)}}</td>
        </tr>`).join('');
        const repoCards = repos.map(r=>`<div class="bg-gray-800 rounded-lg p-3">
          <div class="font-mono text-sm font-bold text-blue-300">${{r.name}}</div>
          <div class="text-xs text-gray-400 mt-1">${{r.language||'?'}} · ⭐${{r.stars}}</div>
          <div class="text-xs text-gray-500 mt-1 truncate">${{r.description||''}}</div>
        </div>`).join('');

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
            ${{flags?'<h3 class="text-sm font-semibold mt-4 text-red-400">Red Flags</h3><div class="flex flex-wrap gap-2 mt-1">'+flags+'</div>':''}}
            ${{strengths?'<h3 class="text-sm font-semibold mt-3 text-green-400">Strengths</h3><div class="flex flex-wrap gap-2 mt-1">'+strengths+'</div>':''}}
          </div>
          ${{prs.length?`<div class="bg-gray-900 border border-gray-700 rounded-xl overflow-hidden mb-6">
            <div class="px-4 py-3 border-b border-gray-700 font-semibold">PRs Analyzed</div>
            <table class="w-full text-sm"><thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-2 px-3">Title</th><th class="py-2 px-3">Type</th><th class="py-2 px-3 text-center">Discussion</th><th class="py-2 px-3">Rationale</th>
            </tr></thead><tbody>${{prRows}}</tbody></table></div>`:''}}
          ${{repos.length?`<div class="bg-gray-900 border border-gray-700 rounded-xl p-4">
            <h3 class="font-semibold mb-3">Own Repos</h3>
            <div class="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">${{repoCards}}</div></div>`:''}}
        `;
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
        row.innerHTML = `<span class="font-mono text-blue-300">${{data.username}}</span><span class="ml-auto font-bold">${{(o.overall_score||0).toFixed(1)}}</span>${{badge(o.gsoc_recommendation||'?')}}`;
        partial.appendChild(row);
      }}

      function renderBulkFull(results) {{
        const sorted = [...results].sort((a,b)=>(b.overall?.overall_score||0)-(a.overall?.overall_score||0));
        let filterRec = '', filterSearch = '';

        function buildTable() {{
          const filtered = sorted.filter(r=>{{
            const o = r.overall||{{}};
            if (filterRec && o.gsoc_recommendation!==filterRec) return false;
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
                <td class="py-2 px-3">${{badge(o.gsoc_recommendation||'?')}}</td>
                <td class="py-2 px-3 text-sm text-gray-400">${{(r.pr_analyses||[]).length}}</td>
                <td class="py-2 px-3 text-xs text-gray-500 max-w-xs truncate">${{flags}}</td>
              </tr>`;
            }}).join('')}}</tbody>
          </table>`;
        }}

        const recs = [...new Set(sorted.map(r=>r.overall?.gsoc_recommendation||'').filter(Boolean))];
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
          const flags=(o.red_flags||[]).map(f=>`<span class="badge bg-red-900 text-red-200">${{f}}</span>`).join(' ');
          const strengths=(o.strengths||[]).map(s=>`<span class="badge bg-green-900 text-green-200">${{s}}</span>`).join(' ');
          const prRows=prs.map(p=>`<tr class="border-b border-gray-800">
            <td class="py-1.5 px-2"><a href="${{p.url}}" target="_blank" class="text-blue-400 hover:underline text-xs">${{p.title.slice(0,60)}}</a></td>
            <td class="py-1.5 px-2">${{clfBadge(p.classification)}}</td>
            <td class="py-1.5 px-2 font-mono text-xs text-center">${{p.discussion_score.toFixed(1)}}</td>
          </tr>`).join('');
          document.getElementById('panel-body').innerHTML = `
            <h2 class="text-xl font-bold mb-1 mr-8"><a href="https://github.com/${{data.username}}" target="_blank" class="hover:text-blue-400">${{data.username}}</a></h2>
            <div class="flex items-center gap-3 mb-4"><span class="text-3xl font-black">${{(o.overall_score||0).toFixed(1)}}</span>${{badge(o.gsoc_recommendation||'?')}}</div>
            ${{bar('Contribution Quality',o.contribution_quality||0)}}
            ${{bar('Portfolio Authenticity',o.portfolio_authenticity||0)}}
            ${{bar('Discussion Depth',o.discussion_depth||0)}}
            ${{bar('Repo Quality',o.repo_quality||0)}}
            <p class="text-sm text-gray-300 mt-3 mb-3">${{o.executive_summary||''}}</p>
            ${{flags?'<div class="mb-2">'+flags+'</div>':''}}
            ${{strengths?'<div class="mb-3">'+strengths+'</div>':''}}
            ${{prs.length?`<table class="w-full text-sm mt-2"><thead><tr class="text-left text-xs text-gray-400 border-b border-gray-700">
              <th class="py-1.5 px-2">PR</th><th class="py-1.5 px-2">Type</th><th class="py-1.5 px-2">Score</th>
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
