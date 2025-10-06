"""FastAPI app serving a lightweight grant filter UI."""
from __future__ import annotations

from datetime import date
from typing import Dict, List, Optional
import logging

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, EmailStr
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

load_dotenv()

from doc_checker.config import get_settings
from .document_checker_routes import router as document_checker_router

from sql_utils import add_subscription, available_subscription_fields, get_connection

settings = get_settings()

app = FastAPI(title="GrantWatch Filters")

allow_origins = settings.allowed_origins or ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins if "*" not in allow_origins else ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

app.include_router(document_checker_router)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("grantwatch.web")


class SubscriptionPayload(BaseModel):
    email: EmailStr
    field: str


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
      select, input[type=date], .subscription-form input[type=email] {
        background: rgba(15, 23, 42, 0.92);
        border: 1px solid rgba(148, 163, 184, 0.35);
        color: #e2e8f0;
        border-radius: 12px;
        padding: 0.65rem 0.75rem;
        font-size: 1rem;
        transition: border-color 0.2s ease, transform 0.2s ease;
      }
      select:focus, input[type=date]:focus, .subscription-form input[type=email]:focus {
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
      .subscription {
        background: rgba(15, 23, 42, 0.7);
        padding: 1.5rem;
        border-radius: 16px;
        border: 1px solid rgba(148, 163, 184, 0.2);
        box-shadow: 0 20px 45px rgba(15, 23, 42, 0.35);
        display: grid;
        gap: 1rem;
      }
      .subscription h2 {
        margin: 0;
        font-size: 1.1rem;
        color: #f8fafc;
      }
      .subscription p {
        margin: 0;
        color: #cbd5f5;
        line-height: 1.5;
      }
      .subscription-form {
        display: grid;
        gap: 1rem;
        grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
        align-items: end;
      }
      .subscription-form label {
        display: flex;
        flex-direction: column;
      }
      .subscription-form label span {
        font-size: 0.8rem;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        color: #94a3b8;
        margin-bottom: 0.35rem;
      }
      .subscription-form button {
        background: linear-gradient(135deg, #f97316, #facc15);
        box-shadow: 0 16px 40px rgba(249, 115, 22, 0.35);
      }
      .subscription-form button:hover {
        box-shadow: 0 22px 60px rgba(249, 115, 22, 0.45);
      }
      .feedback {
        font-size: 0.9rem;
        min-height: 1.2rem;
      }
      .feedback.success {
        color: #4ade80;
      }
      .feedback.error {
        color: #fca5a5;
      }
      .feedback.pending {
        color: #cbd5f5;
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

      <section class=\"subscription\">
        <h2>Never miss a grant</h2>
        <p>Join the list and we'll email you when a new opportunity posts in your field.</p>
        <form id=\"subscriptionForm\" class=\"subscription-form\">
          <label>
            <span>Email address</span>
            <input type=\"email\" id=\"subscriptionEmail\" name=\"email\" required placeholder=\"you@example.org\" autocomplete=\"email\">
          </label>
          <label>
            <span>Field of interest</span>
            <select id=\"subscriptionField\" name=\"field\" required>
              <option value=\"\" disabled selected>Select a field</option>
            </select>
          </label>
          <button type=\"submit\" id=\"subscriptionSubmit\">Notify me</button>
        </form>
        <p class=\"feedback\" id=\"subscriptionFeedback\" role=\"status\" aria-live=\"polite\"></p>
      </section>

      <section class=\"results\" id=\"results\"></section>
    </main>
    <script>
      const resultsEl = document.getElementById('results');
      const submitBtn = document.getElementById('submitBtn');

      const subscriptionForm = document.getElementById('subscriptionForm');
      const subscriptionEmail = document.getElementById('subscriptionEmail');
      const subscriptionField = document.getElementById('subscriptionField');
      const subscriptionFeedback = document.getElementById('subscriptionFeedback');
      const subscriptionSubmit = document.getElementById('subscriptionSubmit');

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
          <article class=\"card\">
            <h2>${grant.title}</h2>
            <div class=\"meta\">
              <span><strong>Stage:</strong> ${grant.stage}</span>
              <span><strong>Status:</strong> ${grant.opportunity_status}</span>
              <span><strong>Due:</strong> ${formatDate(grant.close_date)}</span>
              <span><strong>Posted:</strong> ${formatDate(grant.post_date)}</span>
              ${grant.opportunity_category ? `<span><strong>Category:</strong> ${grant.opportunity_category}</span>` : ''}
            </div>
            ${grant.funding_categories ? `<p><strong>Fields:</strong> ${grant.funding_categories}</p>` : ''}
            <p>${grant.description || 'No description provided.'}</p>
          </article>
        `).join('');
      }

      function setSubscriptionFeedback(state, message) {
        if (!subscriptionFeedback) return;
        subscriptionFeedback.className = state ? `feedback ${state}` : 'feedback';
        subscriptionFeedback.textContent = message || '';
      }

      async function loadSubscriptionFields() {
        if (!subscriptionField) return;
        subscriptionField.innerHTML = '<option value="" disabled selected>Loading fields...</option>';
        try {
          const response = await fetch('/api/subscription-fields', { headers: { 'Accept': 'application/json' }});
          let payload = {};
          try {
            payload = await response.json();
          } catch (_) {
            payload = {};
          }
          if (!response.ok) {
            throw new Error(payload.detail || `Server responded with ${response.status}`);
          }
          const options = (payload.fields || []).filter(Boolean);
          const items = options.length ? options : [
            { key: 'concept', label: 'Concept stage' },
            { key: 'full', label: 'Full stage' },
          ];
          subscriptionField.innerHTML = '<option value="" disabled selected>Select a field</option>';
          items.forEach(item => {
            const key = (item.key || '').toLowerCase();
            const label = item.label || item.key || '';
            if (!key) {
              return;
            }
            const option = document.createElement('option');
            option.value = key;
            option.textContent = label;
            subscriptionField.appendChild(option);
          });
          if (!options.length) {
            setSubscriptionFeedback('pending', 'Using default fields until more data is available.');
          } else {
            setSubscriptionFeedback('', '');
          }
        } catch (error) {
          console.error(error);
          subscriptionField.innerHTML = '<option value="" disabled>Fields unavailable</option>';
          setSubscriptionFeedback('error', 'Unable to load subscription fields right now.');
        }
      }

      async function fetchGrants() {
        if (submitBtn) submitBtn.disabled = true;
        resultsEl.innerHTML = '<div class="empty">Loading results...</div>';
        const stageEl = document.getElementById('stage');
        const dueFromEl = document.getElementById('dueFrom');
        const dueToEl = document.getElementById('dueTo');
        const stage = stageEl ? stageEl.value : '';
        const dueFrom = dueFromEl ? dueFromEl.value : '';
        const dueTo = dueToEl ? dueToEl.value : '';
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
          if (submitBtn) submitBtn.disabled = false;
        }
      }

      async function handleSubscription(event) {
        event.preventDefault();
        if (!subscriptionEmail || !subscriptionField) return;
        if (!subscriptionField.value) {
          setSubscriptionFeedback('error', 'Choose a field to follow.');
          return;
        }
        if (!subscriptionEmail.value.trim()) {
          setSubscriptionFeedback('error', 'Add your email so we know where to send alerts.');
          return;
        }
        if (subscriptionSubmit) subscriptionSubmit.disabled = true;
        setSubscriptionFeedback('pending', 'Saving your subscription...');
        try {
          const response = await fetch('/api/subscriptions', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Accept': 'application/json'
            },
            body: JSON.stringify({
              email: subscriptionEmail.value.trim(),
              field: subscriptionField.value
            })
          });
          let payload = {};
          try {
            payload = await response.json();
          } catch (_) {
            payload = {};
          }
          if (!response.ok) {
            throw new Error(payload.detail || 'Unable to save your subscription.');
          }
          const fieldLabel = (payload && payload.field && (payload.field.label || payload.field.key)) || 'your field';
          setSubscriptionFeedback('success', `You're in! We'll email you when new ${fieldLabel} grants post.`);
          if (subscriptionForm) {
            subscriptionForm.reset();
          }
          if (subscriptionField && subscriptionField.options.length > 0) {
            subscriptionField.selectedIndex = 0;
          }
        } catch (error) {
          console.error(error);
          setSubscriptionFeedback('error', error.message || 'Subscription failed. Try again soon.');
        } finally {
          if (subscriptionSubmit) subscriptionSubmit.disabled = false;
        }
      }

      if (subscriptionForm) {
        subscriptionForm.addEventListener('submit', handleSubscription);
      }

      if (submitBtn) {
        submitBtn.addEventListener('click', fetchGrants);
      }

      window.addEventListener('DOMContentLoaded', () => {
        loadSubscriptionFields();
        fetchGrants();
      });
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
        SELECT opp_id,
               title,
               stage,
               opportunity_status,
               opportunity_category,
               funding_categories,
               post_date,
               close_date,
               description
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
        logger.exception("Database query failed when fetching grants")
        raise HTTPException(status_code=500, detail=f"Database query failed: {exc}") from exc

    results = []
    for row in rows:
        results.append(
            {
                "opp_id": row.get("opp_id"),
                "title": row.get("title"),
                "stage": row.get("stage"),
                "opportunity_status": row.get("opportunity_status"),
                "opportunity_category": row.get("opportunity_category"),
                "funding_categories": row.get("funding_categories"),
                "post_date": row.get("post_date").isoformat() if row.get("post_date") else None,
                "close_date": row.get("close_date").isoformat() if row.get("close_date") else None,
                "description": row.get("description"),
            }
        )

    return {"results": results}

@app.get("/api/subscription-fields")
def subscription_fields(limit: int = Query(default=200, ge=1, le=500)) -> Dict[str, List[Dict[str, str]]]:
    try:
        options = available_subscription_fields(limit=limit)
    except Exception as exc:
        logger.exception("Database query failed when loading subscription fields")
        raise HTTPException(status_code=500, detail="Database query failed while loading subscription fields.") from exc
    if not options:
        options = [("concept", "Concept stage"), ("full", "Full stage")]

    fields: List[Dict[str, str]] = []
    seen = set()
    for key, label in options:
        key_lower = (key or "").strip().lower()
        if not key_lower or key_lower in seen:
            continue
        seen.add(key_lower)
        display = (label or "").strip() or key_lower.replace('_', ' ').title()
        fields.append({"key": key_lower, "label": display})

    return {"fields": fields}


@app.post("/api/subscriptions")
def create_subscription(payload: SubscriptionPayload) -> Dict[str, Dict[str, str]]:
    field_key = payload.field.strip().lower()
    if not field_key:
        raise HTTPException(status_code=400, detail="Field selection is required.")

    available = {key.lower(): (label or key) for key, label in available_subscription_fields(limit=500)}
    label = available.get(field_key)
    if not label:
        if field_key in {"concept", "full"}:
            label = "Concept stage" if field_key == "concept" else "Full stage"
        else:
            raise HTTPException(status_code=400, detail="Unknown field selection.")

    try:
        add_subscription(payload.email, field_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Failed to create subscription")
        raise HTTPException(status_code=500, detail="Database query failed while saving the subscription.") from exc

    return {"field": {"key": field_key, "label": label}}

