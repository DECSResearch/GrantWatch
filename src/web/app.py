"""FastAPI app serving a lightweight grant filter UI."""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from psycopg2.extras import RealDictCursor

from sql_utils import get_connection

app = FastAPI(title="GrantWatch Filters")

INDEX_HTML = """
<!doctype html>
<html lang=\"en\">
  <head>
    <meta charset=\"utf-8\">
    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>GrantWatch Filters</title>
    <style>
      :root {
        color-scheme: light dark;
        font-family: system-ui, sans-serif;
        background-color: #0f172a;
        color: #e2e8f0;
      }
      body {
        margin: 0;
        padding: 2rem 1rem 4rem;
        display: flex;
        justify-content: center;
      }
      .page {
        width: min(960px, 100%);
        display: flex;
        flex-direction: column;
        gap: 1.5rem;
      }
      .filters {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        gap: 1rem;
        background: rgba(15, 23, 42, 0.7);
        padding: 1.5rem;
        border-radius: 16px;
        box-shadow: 0 20px 45px rgba(15, 23, 42, 0.35);
        border: 1px solid rgba(148, 163, 184, 0.2);
      }
      label {
        display: flex;
        flex-direction: column;
        font-size: 0.85rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #94a3b8;
        gap: 0.35rem;
      }
      select, input[type=date] {
        background: rgba(15, 23, 42, 0.92);
        border: 1px solid rgba(148, 163, 184, 0.35);
        color: #e2e8f0;
        border-radius: 12px;
        padding: 0.65rem 0.75rem;
        font-size: 1rem;
        transition: border-color 0.2s ease, transform 0.2s ease;
      }
      select:focus, input[type=date]:focus {
        outline: none;
        border-color: #38bdf8;
        transform: translateY(-1px);
      }
      button {
        grid-column: 1 / -1;
        justify-self: start;
        padding: 0.75rem 1.75rem;
        border-radius: 999px;
        border: none;
        font-size: 0.95rem;
        font-weight: 600;
        letter-spacing: 0.02em;
        background: linear-gradient(135deg, #22d3ee, #0ea5e9);
        color: #0f172a;
        cursor: pointer;
        box-shadow: 0 16px 40px rgba(34, 211, 238, 0.35);
        transition: transform 0.2s ease, box-shadow 0.2s ease;
      }
      button:hover {
        transform: translateY(-1px);
        box-shadow: 0 22px 60px rgba(14, 165, 233, 0.45);
      }
      button:disabled {
        opacity: 0.6;
        cursor: not-allowed;
        transform: none;
        box-shadow: none;
      }
      .results {
        display: grid;
        gap: 1rem;
      }
      .card {
        padding: 1.25rem;
        border-radius: 16px;
        background: rgba(15, 23, 42, 0.8);
        border: 1px solid rgba(148, 163, 184, 0.24);
        box-shadow: 0 16px 40px rgba(15, 23, 42, 0.4);
        display: grid;
        gap: 0.65rem;
      }
      .card h2 {
        margin: 0;
        font-size: 1.25rem;
        color: #f1f5f9;
      }
      .meta {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem 1.25rem;
        font-size: 0.9rem;
        color: #cbd5f5;
      }
      .empty, .error {
        padding: 2rem;
        text-align: center;
        border-radius: 16px;
        background: rgba(15, 23, 42, 0.8);
        border: 1px dashed rgba(148, 163, 184, 0.35);
        color: #94a3b8;
      }
    </style>
  </head>
  <body>
    <main class=\"page\">
      <header>
        <h1>GrantWatch Filter</h1>
        <p>Quickly explore Concept and Full proposals by due date.</p>
      </header>
      <section class=\"filters\">
        <label>
          Stage
          <select id=\"stage\">
            <option value=\"\">All stages</option>
            <option value=\"concept\">Concept</option>
            <option value=\"full\">Full</option>
          </select>
        </label>
        <label>
          Due on or after
          <input type=\"date\" id=\"dueFrom\">
        </label>
        <label>
          Due on or before
          <input type=\"date\" id=\"dueTo\">
        </label>
        <button id=\"submitBtn\">Apply Filters</button>
      </section>
      <section class=\"results\" id=\"results\"></section>
    </main>
    <script>
      const resultsEl = document.getElementById('results');
      const submitBtn = document.getElementById('submitBtn');

      function formatDate(value) {
        if (!value) return 'N/A';
        const date = new Date(value);
        return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
      }

      function renderGrants(grants) {
        if (!grants.length) {
          resultsEl.innerHTML = '<div class="empty">No grants found for the selected filters.</div>';
          return;
        }
        resultsEl.innerHTML = grants.map(grant => `
          <article class="card">
            <h2>${grant.title}</h2>
            <div class="meta">
              <span><strong>Stage:</strong> ${grant.stage}</span>
              <span><strong>Status:</strong> ${grant.opportunity_status}</span>
              <span><strong>Due:</strong> ${formatDate(grant.close_date)}</span>
              <span><strong>Posted:</strong> ${formatDate(grant.post_date)}</span>
            </div>
            <p>${grant.description || 'No description provided.'}</p>
          </article>
        `).join('');
      }

      async function fetchGrants() {
        submitBtn.disabled = true;
        resultsEl.innerHTML = '<div class="empty">Loading results...</div>';
        const stage = document.getElementById('stage').value;
        const dueFrom = document.getElementById('dueFrom').value;
        const dueTo = document.getElementById('dueTo').value;
        const params = new URLSearchParams();
        if (stage) params.set('stage', stage);
        if (dueFrom) params.set('due_from', dueFrom);
        if (dueTo) params.set('due_to', dueTo);

        try {
          const response = await fetch(`/api/grants?${params.toString()}`, { headers: { 'Accept': 'application/json' }});
          if (!response.ok) {
            throw new Error(`Server responded with ${response.status}`);
          }
          const payload = await response.json();
          renderGrants(payload.results || []);
        } catch (error) {
          console.error(error);
          resultsEl.innerHTML = `<div class="error">Failed to load grants. ${error.message}</div>`;
        } finally {
          submitBtn.disabled = false;
        }
      }

      submitBtn.addEventListener('click', fetchGrants);
      window.addEventListener('DOMContentLoaded', fetchGrants);
    </script>
  </body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


@app.get("/api/grants")
def get_grants(
    stage: Optional[str] = Query(default=None),
    due_from: Optional[date] = Query(default=None),
    due_to: Optional[date] = Query(default=None),
):
    conditions = ["opportunity_status = 'Posted'"]
    params: list[object] = []

    if stage and stage not in {"concept", "full"}:
        raise HTTPException(status_code=400, detail="Stage must be 'concept' or 'full'")

    if stage:
        conditions.append("stage = %s")
        params.append(stage)

    if due_from and due_to and due_from > due_to:
        raise HTTPException(status_code=400, detail="due_from cannot be after due_to")

    if due_from:
        conditions.append("close_date >= %s")
        params.append(due_from)

    if due_to:
        conditions.append("close_date <= %s")
        params.append(due_to)

    where_clause = " AND ".join(conditions)
    query = f"""
        SELECT opp_id, title, stage, opportunity_status, post_date, close_date, description
        FROM grants
        WHERE {where_clause}
        ORDER BY close_date NULLS LAST, post_date DESC
        LIMIT 200
    """

    try:
        with get_connection() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Database query failed: {exc}") from exc

    results = []
    for row in rows:
        results.append(
            {
                "opp_id": row.get("opp_id"),
                "title": row.get("title"),
                "stage": row.get("stage"),
                "opportunity_status": row.get("opportunity_status"),
                "post_date": row.get("post_date").isoformat() if row.get("post_date") else None,
                "close_date": row.get("close_date").isoformat() if row.get("close_date") else None,
                "description": row.get("description"),
            }
        )

    return {"results": results}




