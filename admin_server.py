"""`/admin` Pizza Dough Instance Record, Scientific Log Lab & RAG Bridge Server.

Run locally:
    python3 admin_server.py --port 8765

Routes:
    GET  /                       -> redirects to /admin
    GET  /admin                  -> interactive Instance & Log Analytics Lab UI
    GET  /api/dashboard          -> instances, known variables, global effects, RAG card matches
    GET  /api/compare?control=..&variant=.. -> dynamic variable comparison & drafted claim
    POST /api/logs               -> compose a new controlled trial log
    PATCH /api/logs/<id>         -> update storyNote, phaseLogs, or Task ORDs on an existing log
    GET  /api/bridge/export      -> export shared JSON bundle for `v0-yeast-development-app`
    POST /api/bridge/import      -> import `DoughLogEntry[]` / `StoredDoughProfile[]` bundle
    POST /api/bridge/cards       -> match RAG insight cards for an app state or variable delta
    POST /api/claims/stage       -> stage an empirical comparison claim into `candidates.jsonl`
    POST /api/claims/verify      -> verify a claim (`internal` corpus or `external` web)
"""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from instance_analytics import (
    LOGS_PATH,
    PROFILES_PATH,
    append_trial_log,
    build_dashboard_payload,
    build_dough_instances,
    compare_trial_instances,
    ensure_store,
    export_app_bundle,
    import_app_bundle,
    load_rag_cards,
    match_cards_for_context,
    stage_empirical_claim,
    update_trial_log,
    verify_claim_bridge,
)


def dispatch_api_request(
    method: str,
    path: str,
    query: Dict[str, list],
    body: Optional[Dict[str, Any]] = None,
    profiles_path: str = PROFILES_PATH,
    logs_path: str = LOGS_PATH,
) -> Tuple[int, Dict[str, Any]]:
    """Pure request dispatcher used by both HTTP handler and unit tests."""
    payload = body or {}

    if method == "GET" and path == "/api/dashboard":
        return 200, build_dashboard_payload(profiles_path=profiles_path, logs_path=logs_path)

    if method == "GET" and path == "/api/compare":
        control_id = (query.get("control") or [""])[0]
        variant_id = (query.get("variant") or [""])[0]
        profiles, logs = ensure_store(profiles_path, logs_path)
        cards = load_rag_cards()
        instances = build_dough_instances(profiles, logs, rag_cards=cards)
        trials_by_id = {t["id"]: t for inst in instances for t in inst["trials"]}
        if control_id not in trials_by_id or variant_id not in trials_by_id:
            return 404, {"error": "control or variant trial ID not found"}
        report = compare_trial_instances(trials_by_id[control_id], trials_by_id[variant_id], rag_cards=cards)
        return 200, report

    if method == "POST" and path == "/api/logs":
        created = append_trial_log(payload, profiles_path=profiles_path, logs_path=logs_path)
        return 201, {
            "created": created,
            "dashboard": build_dashboard_payload(profiles_path=profiles_path, logs_path=logs_path),
        }

    if method in ("PATCH", "POST") and path.startswith("/api/logs/"):
        log_id = path.split("/api/logs/", 1)[1].strip("/")
        updated = update_trial_log(log_id, payload, profiles_path=profiles_path, logs_path=logs_path)
        if updated is None:
            return 404, {"error": f"log {log_id} not found"}
        return 200, {
            "updated": updated,
            "dashboard": build_dashboard_payload(profiles_path=profiles_path, logs_path=logs_path),
        }

    if method == "GET" and path == "/api/bridge/export":
        return 200, export_app_bundle(profiles_path=profiles_path, logs_path=logs_path)

    if method == "POST" and path == "/api/bridge/import":
        stats = import_app_bundle(payload, profiles_path=profiles_path, logs_path=logs_path)
        return 200, {
            "imported": stats,
            "dashboard": build_dashboard_payload(profiles_path=profiles_path, logs_path=logs_path),
        }

    if method == "POST" and path == "/api/bridge/cards":
        style_slug = str(payload.get("styleSlug") or "general")
        rag_vars = payload.get("ragVariables") or ["hydration", "fermentation_time"]
        query_text = str(payload.get("query") or "")
        top_k = int(payload.get("topK") or 4)
        cards = match_cards_for_context(style_slug=style_slug, rag_variables=rag_vars, query_text=query_text, top_k=top_k)
        return 200, {"cards": cards}

    if method == "POST" and path == "/api/claims/stage":
        if not payload.get("claim"):
            return 400, {"error": "claim text required"}
        res = stage_empirical_claim(payload, commit=bool(payload.get("commit", False)))
        return 201, res

    if method == "POST" and path == "/api/claims/verify":
        claim = str(payload.get("claim") or "").strip()
        if not claim:
            return 400, {"error": "claim text required"}
        mode = str(payload.get("evidenceMode") or "internal")
        if mode not in ("internal", "external"):
            mode = "internal"
        return 200, verify_claim_bridge(claim, evidence_mode=mode)

    return 404, {"error": f"unknown route {method} {path}"}


ADMIN_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Admin · Pizza Dough Instance Record & Scientific Log Lab</title>
<style>
  :root {
    --paper: #f7f3eb;
    --card: #fdfbf7;
    --ink: #1e1b18;
    --muted: #686158;
    --line: #dcd4c6;
    --line-strong: #bdb19d;
    --amber: #b45309;
    --amber-soft: #fef3c7;
    --green: #15803d;
    --green-soft: #dcfce7;
    --red: #b91c1c;
    --red-soft: #fee2e2;
    --blue: #1d4ed8;
    --blue-soft: #dbeafe;
    --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    --serif: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: var(--sans);
    line-height: 1.45;
  }
  .shell {
    max-width: 1320px;
    margin: 0 auto;
    padding: 22px 24px 56px;
  }
  .topbar {
    display: flex;
    flex-wrap: wrap;
    justify-content: space-between;
    align-items: flex-end;
    gap: 16px;
    padding-bottom: 16px;
    border-bottom: 2px solid var(--ink);
    margin-bottom: 20px;
  }
  .eyebrow {
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--amber);
    font-weight: 700;
  }
  h1 {
    font-family: var(--serif);
    font-size: 28px;
    margin: 3px 0 2px;
    font-weight: 700;
    letter-spacing: -0.01em;
  }
  .subtitle {
    margin: 0;
    color: var(--muted);
    font-size: 14px;
    font-style: italic;
  }
  .btn-row {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
  }
  button, .btn {
    font-family: var(--sans);
    font-size: 12.5px;
    font-weight: 600;
    padding: 8px 13px;
    border-radius: 8px;
    border: 1px solid var(--line-strong);
    background: var(--card);
    color: var(--ink);
    cursor: pointer;
    transition: all 0.14s ease;
  }
  button:hover { border-color: var(--ink); background: #f2ece1; }
  button.primary {
    background: var(--ink);
    color: #fff;
    border-color: var(--ink);
  }
  button.primary:hover { background: #35302b; }
  button.amber {
    background: var(--amber);
    color: #fff;
    border-color: var(--amber);
  }
  button.amber:hover { filter: brightness(0.94); }

  .card {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 18px;
    box-shadow: 0 1px 2px rgba(30,27,24,0.04);
  }
  .section-label {
    font-family: var(--mono);
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.09em;
    color: var(--muted);
    margin-bottom: 10px;
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  /*Instance tabs*/
  .instance-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 18px;
  }
  .inst-tab {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 14px;
    border-radius: 999px;
    border: 1px solid var(--line-strong);
    background: var(--card);
    cursor: pointer;
    font-size: 13px;
  }
  .inst-tab.active {
    background: var(--ink);
    color: #fff;
    border-color: var(--ink);
  }
  .inst-tab .code {
    font-family: var(--mono);
    font-size: 11.5px;
    opacity: 0.82;
  }
  .pill {
    display: inline-flex;
    align-items: center;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: 11px;
    font-family: var(--mono);
    font-weight: 600;
    border: 1px solid var(--line);
    background: #f3ede2;
  }
  .pill.good { background: var(--green-soft); color: var(--green); border-color: #86efac; }
  .pill.bad { background: var(--red-soft); color: var(--red); border-color: #fca5a5; }
  .pill.amber { background: var(--amber-soft); color: var(--amber); border-color: #fcd34d; }
  .pill.blue { background: var(--blue-soft); color: var(--blue); border-color: #93c5fd; }

  /* TOP BAND: Phase-Wise Logging + Object Referencing */
  .top-band {
    display: grid;
    grid-template-columns: 1.25fr 0.95fr;
    gap: 18px;
    margin-bottom: 18px;
  }
  @media (max-width: 960px) {
    .top-band, .mid-band, .bottom-band { grid-template-columns: 1fr !important; }
  }
  .phase-stack {
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .phase-row {
    display: grid;
    grid-template-columns: 62px 1fr;
    gap: 14px;
    align-items: center;
    padding: 12px 14px;
    border: 1px solid var(--line);
    border-radius: 12px;
    background: #faf6ee;
  }
  .vessel-icon {
    width: 54px;
    height: 54px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .pct-bar {
    height: 7px;
    border-radius: 999px;
    background: #e6dfd2;
    overflow: hidden;
    margin: 6px 0;
  }
  .pct-fill {
    height: 100%;
    border-radius: 999px;
    background: linear-gradient(90deg, #d97706, #15803d);
    transition: width 0.25s ease;
  }
  .obj-bowl-wrap {
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    align-items: center;
    text-align: center;
    position: relative;
  }
  .obj-ref-tag {
    font-family: var(--mono);
    font-size: 12px;
    padding: 5px 11px;
    border: 1.5px dashed var(--amber);
    background: var(--amber-soft);
    color: #92400e;
    border-radius: 8px;
  }

  /* Composer Drawer */
  .composer {
    display: none;
    margin-bottom: 18px;
    border: 2px solid var(--ink);
  }
  .composer.open { display: block; }
  .form-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(185px, 1fr));
    gap: 12px;
    margin-bottom: 12px;
  }
  label.field {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 12px;
    font-weight: 600;
  }
  label.field input, label.field select, label.field textarea {
    padding: 7px 9px;
    border: 1px solid var(--line-strong);
    border-radius: 7px;
    background: #fff;
    font-family: var(--sans);
    font-size: 13px;
  }

  /* MIDDLE BAND: Variable Rail + Journey + Storybank */
  .mid-band {
    display: grid;
    grid-template-columns: 225px 1fr;
    gap: 18px;
    margin-bottom: 18px;
  }
  .var-rail {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .var-node {
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 10px;
    border-radius: 9px;
    border: 1px solid transparent;
    cursor: pointer;
    font-size: 12.5px;
    text-align: left;
    background: transparent;
  }
  .var-node:hover { background: #f1ebe0; }
  .var-node.active {
    background: var(--ink);
    color: #fff;
  }
  .var-dot {
    width: 11px;
    height: 11px;
    border-radius: 50%;
    border: 2px solid currentColor;
    flex-shrink: 0;
  }
  .var-node.active .var-dot { background: #fbbf24; border-color: #fbbf24; }

  .journey-chain {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 14px;
    background: #faf6ee;
    border: 1px solid var(--line);
    border-radius: 12px;
    margin: 12px 0;
  }
  .ball-step {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 8px 12px;
    border-radius: 12px;
    border: 1.5px solid var(--line-strong);
    background: #fff;
    cursor: pointer;
    min-width: 96px;
  }
  .ball-step.active {
    border-color: var(--ink);
    box-shadow: 0 0 0 2px var(--ink);
  }
  .ball-circle {
    border-radius: 50%;
    border: 2px solid var(--ink);
    background: radial-gradient(circle at 35% 30%, #fffbeb, #fde68a 68%, #d97706 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--mono);
    font-size: 11px;
    font-weight: 700;
  }
  .chain-arrow {
    font-family: var(--mono);
    color: var(--muted);
    font-size: 18px;
    font-weight: 700;
  }

  .story-split {
    display: grid;
    grid-template-columns: 1.15fr 0.85fr;
    gap: 14px;
    margin-top: 14px;
  }
  @media (max-width: 860px) {
    .story-split { grid-template-columns: 1fr; }
  }
  .story-item {
    padding: 10px 12px;
    border-left: 3px solid var(--amber);
    background: #faf6ee;
    border-radius: 0 9px 9px 0;
    margin-bottom: 9px;
    font-size: 13px;
  }
  .rag-card {
    padding: 10px 12px;
    border: 1px solid #cbd5e1;
    background: #f8fafc;
    border-radius: 10px;
    margin-bottom: 8px;
    font-size: 12.5px;
  }

  /* BOTTOM BAND: Task ORDs + BALL Circle + Dynamic Comparison */
  .bottom-band {
    display: grid;
    grid-template-columns: 0.9fr 0.95fr 1.35fr;
    gap: 18px;
  }
  .ord-item {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    padding: 8px 9px;
    border-bottom: 1px dashed var(--line);
    font-size: 12.5px;
    cursor: pointer;
  }
  .ord-item.done { color: var(--muted); text-decoration: line-through; }

  .ball-hub {
    display: flex;
    flex-direction: column;
    align-items: center;
  }
  .ball-emblem {
    width: 176px;
    height: 176px;
    border-radius: 50%;
    border: 2.5px solid var(--ink);
    background: radial-gradient(circle at 35% 30%, #fffef9, #fef3c7 62%, #f59e0b 100%);
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    text-align: center;
    padding: 14px;
    margin: 6px 0 12px;
    box-shadow: inset 0 -8px 18px rgba(180,83,9,0.14);
  }
  .ing-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
  }
  .ing-table td {
    padding: 5px 4px;
    border-bottom: 1px solid var(--line);
  }

  .delta-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 12px;
    margin-top: 8px;
  }
  .delta-table th, .delta-table td {
    padding: 6px 7px;
    border-bottom: 1px solid var(--line);
    text-align: left;
  }
  .delta-table tr.changed {
    background: #fef9c3;
    font-weight: 600;
  }
  .dot-matrix {
    display: grid;
    grid-template-columns: repeat(6, 7px);
    gap: 4px;
  }
  .dot-matrix span {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--line-strong);
  }
  .dot-matrix span.hot { background: var(--amber); }
  .dot-matrix span.lock { background: var(--green); }
  .toast {
    position: fixed;
    bottom: 20px;
    right: 20px;
    padding: 11px 16px;
    border-radius: 10px;
    background: var(--ink);
    color: #fff;
    font-size: 13px;
    display: none;
    z-index: 50;
    max-width: 440px;
    box-shadow: 0 8px 24px rgba(0,0,0,0.2);
  }
</style>
</head>
<body>
<div class="shell">
  <header class="topbar">
    <div>
      <div class="eyebrow">/admin · Empirical Fermentation Lab &amp; RAG Bridge</div>
      <h1>A Pizza Dough Instance Record &amp; Log</h1>
      <p class="subtitle">Phase-wise logging: Toward more comprehensive % logs, variable isolation &amp; verified RAG feedback</p>
    </div>
    <div class="btn-row">
      <button onclick="toggleComposer()" class="primary" id="composerBtn">+ Compose Controlled Trial</button>
      <button onclick="exportBridgeBundle()">Export Bridge JSON</button>
      <button onclick="document.getElementById('importFile').click()">Import App Logs</button>
      <input type="file" id="importFile" accept=".json" style="display:none" onchange="importBridgeFile(event)" />
    </div>
  </header>

  <!-- Controlled Trial Composer -->
  <section class="card composer" id="composerDrawer">
    <div class="section-label">
      <span>Scientific Controlled Trial Composer (locks controls from baseline trial)</span>
      <span id="composerIsolationBadge" class="pill good">Ready · 0 variables shifted</span>
    </div>
    <div class="form-grid">
      <label class="field">Instance / Profile
        <select id="cmpProfile" onchange="prefillComposerFromInstance()"></select>
      </label>
      <label class="field">Trial Title
        <input id="cmpTitle" placeholder="e.g. T5 · Isolate 66% Hydration" />
      </label>
      <label class="field">Object Reference (`[object ref]`)
        <input id="cmpObjRef" placeholder="#5678 · Ball E1" />
      </label>
      <label class="field">Outcome Rating
        <select id="cmpOutcome">
          <option value="good">Worked (Good convergence)</option>
          <option value="bad">Didn't work (Regressed)</option>
          <option value="">Unrated / In progress</option>
        </select>
      </label>
    </div>
    <div class="form-grid">
      <label class="field">Hydration (%)
        <input type="number" step="0.5" id="cmpHydration" oninput="recalcComposerDiff()" />
      </label>
      <label class="field">Flour Base
        <select id="cmpFlour" onchange="recalcComposerDiff()">
          <option value="00">Tipo 00</option>
          <option value="bread">Bread Flour</option>
          <option value="all-purpose">All-Purpose</option>
          <option value="whole-wheat">Whole Wheat</option>
        </select>
      </label>
      <label class="field">Rest Temp (°F)
        <input type="number" id="cmpRestTemp" oninput="recalcComposerDiff()" />
      </label>
      <label class="field">Bulk Ferment Temp (°F)
        <input type="number" id="cmpFermentTemp" oninput="recalcComposerDiff()" />
      </label>
      <label class="field">Cold Retard Phase
        <select id="cmpFridge" onchange="recalcComposerDiff()">
          <option value="none">None (Direct / Ambient)</option>
          <option value="ferment">Cold Bulk (38°F)</option>
          <option value="proof">Cold Proof (38°F)</option>
        </select>
      </label>
      <label class="field">Preferment Flour (%)
        <input type="number" id="cmpPreferment" oninput="recalcComposerDiff()" />
      </label>
      <label class="field">Observed Rise (%)
        <input type="number" id="cmpRise" value="75" />
      </label>
      <label class="field">Elapsed Clock (min)
        <input type="number" id="cmpClock" oninput="recalcComposerDiff()" />
      </label>
    </div>
    <div class="form-grid">
      <label class="field">Mixing Phase Feedback (□)
        <input id="cmpMixNote" placeholder="Hydration uptake, Mix = 0 feel..." />
      </label>
      <label class="field">Resting Phase Feedback (▭)
        <input id="cmpRestNote" placeholder="Autolyse / windowpane / coil fold feel..." />
      </label>
      <label class="field">Fermentation Phase Feedback (⏢)
        <input id="cmpFermNote" placeholder="Bubble dispersion, poke test, peel transfer..." />
      </label>
    </div>
    <div class="form-grid">
      <label class="field">Hypothesis (1 variable under test)
        <input id="cmpHypothesis" placeholder="Holding bulk °F & flour fixed, +2% hydration increases rim airiness..." />
      </label>
      <label class="field">Result &amp; Storybank Note
        <input id="cmpResult" placeholder="Clean stretch, blistered cornicione..." />
      </label>
    </div>
    <div class="btn-row" style="justify-content:flex-end">
      <button onclick="toggleComposer()">Cancel</button>
      <button class="amber" onclick="submitComposerTrial()">Record Controlled Trial</button>
    </div>
  </section>

  <!-- Instance Selector Pills -->
  <div class="instance-tabs" id="instanceTabs"></div>

  <!-- TOP SECTION: Phase-Wise % Logging & Object Referencing -->
  <section class="top-band">
    <div class="card">
      <div class="section-label">
        <span>Phase-Wise % Logging · Mixing (□) → Resting (▭) → Fermentation (⏢)</span>
        <span id="activeTrialBadge" class="pill">Trial —</span>
      </div>
      <div class="phase-stack" id="phaseStack"></div>
    </div>

    <div class="card obj-bowl-wrap">
      <div class="section-label" style="width:100%">
        <span>Physical Vessel &amp; Object Referencing</span>
        <span class="pill amber">[object referencing]</span>
      </div>
      <div id="bowlSvgWrap" style="width:100%;max-width:300px;margin:4px auto;"></div>
      <div style="display:flex;flex-direction:column;gap:6px;align-items:center;width:100%">
        <div class="obj-ref-tag" id="objRefLabel">#5678 · Ball T1</div>
        <div id="objRefCaption" style="font-size:12.5px;color:var(--muted);max-width:340px;"></div>
      </div>
    </div>
  </section>

  <!-- MIDDLE SECTION: Left Variable Scrubber Rail + Instance Journey + Storybank + RAG Cards -->
  <section class="mid-band">
    <aside class="card">
      <div class="section-label">
        <span>Known Variables</span>
        <span>
          <button style="padding:2px 7px;font-size:11px" onclick="scrubVariable(-1)" title="Previous variable">↑</button>
          <button style="padding:2px 7px;font-size:11px" onclick="scrubVariable(1)" title="Next variable">↓</button>
        </span>
      </div>
      <div class="var-rail" id="variableRail"></div>
    </aside>

    <div class="card">
      <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:flex-start;">
        <div>
          <div class="eyebrow" id="journeyEyebrow">dough id #5678</div>
          <h2 id="journeyTitle" style="margin:2px 0;font-family:var(--serif);font-size:22px;"></h2>
          <div id="desiredOutcomeText" style="font-size:13px;color:var(--muted);"></div>
        </div>
        <div id="journeyKpis" style="display:flex;gap:8px;flex-wrap:wrap;"></div>
      </div>

      <!-- Dough-ball evolution chain o -> o -> o -> o -->
      <div class="journey-chain" id="journeyChain"></div>

      <!-- Convergence + Scrubbed Variable Trajectory SVG -->
      <div id="trajectoryChartWrap" style="margin:10px 0 6px;"></div>

      <!-- Storybank + Matched RAG Insight Cards -->
      <div class="story-split">
        <div>
          <div class="section-label">
            <span>Storybank · Dough-wise Stories &amp; Narratives</span>
          </div>
          <div id="storybankList"></div>
          <div style="display:flex;gap:6px;margin-top:8px;">
            <input id="storyAppendInput" style="flex:1;padding:7px 9px;border:1px solid var(--line-strong);border-radius:7px;font-size:12.5px;" placeholder="Update narrative note on active trial..." />
            <button onclick="saveStoryNote()">Save Note</button>
          </div>
        </div>
        <div>
          <div class="section-label">
            <span>Matched RAG Insight Cards (`cards.jsonl`)</span>
            <span id="ragCountBadge" class="pill blue">0 cards</span>
          </div>
          <div id="instanceRagCards"></div>
        </div>
      </div>
    </div>
  </section>

  <!-- BOTTOM SECTION: [Task ORDs] + BALL Composition Circle + Dynamic Instance Comparison -->
  <section class="bottom-band">
    <div class="card">
      <div class="section-label">
        <span>[Task ORDs] · Ordered Protocol</span>
        <span id="ordDoneBadge" class="pill">0/0</span>
      </div>
      <p style="margin:0 0 8px;font-size:12px;color:var(--muted);">Hold process order constant across trials; click any checkpoint to toggle completion.</p>
      <div id="taskOrdList"></div>
    </div>

    <div class="card ball-hub">
      <div class="section-label" style="width:100%">
        <span>BALL · Composition &amp; Stoichiometry</span>
        <span id="ballCountBadge" class="pill">3 balls</span>
      </div>
      <div class="ball-emblem" id="ballEmblem"></div>
      <table class="ing-table" id="ingTable"></table>
      <div id="stoichLine" style="margin-top:10px;font-size:11.5px;color:var(--muted);font-family:var(--mono);text-align:center;"></div>
    </div>

    <div class="card">
      <div class="section-label">
        <span>Dynamic Instance Comparison &amp; Variable Isolation</span>
        <div class="dot-matrix" id="dotMatrix"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px;">
        <label class="field">Control (Baseline A)
          <select id="cmpControlSelect" onchange="runComparison()"></select>
        </label>
        <label class="field">Variant (Test B)
          <select id="cmpVariantSelect" onchange="runComparison()"></select>
        </label>
      </div>
      <div id="comparisonBody"></div>
    </div>
  </section>
</div>

<div class="toast" id="toast"></div>

<script>
let STATE = {
  dashboard: null,
  activeInstanceIdx: 0,
  activeTrialIdx: 0,
  activeVarIdx: 0,
  comparison: null,
};

function showToast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.display = 'block';
  clearTimeout(window._toastTimer);
  window._toastTimer = setTimeout(() => { el.style.display = 'none'; }, 4200);
}

async function boot() {
  const res = await fetch('/api/dashboard');
  STATE.dashboard = await res.json();
  STATE.activeInstanceIdx = 0;
  const inst = currentInstance();
  STATE.activeTrialIdx = inst && inst.trials.length ? inst.trials.length - 1 : 0;
  populateComposerProfiles();
  renderAll();
  populateComparisonSelectors(true);
  await runComparison();
}

function currentInstance() {
  if (!STATE.dashboard || !STATE.dashboard.instances.length) return null;
  return STATE.dashboard.instances[STATE.activeInstanceIdx] || STATE.dashboard.instances[0];
}

function currentTrial() {
  const inst = currentInstance();
  if (!inst || !inst.trials.length) return null;
  return inst.trials[STATE.activeTrialIdx] || inst.trials[inst.trials.length - 1];
}

function vesselSvg(shape) {
  if (shape === 'square') {
    return `<svg width="46" height="46" viewBox="0 0 46 46">
      <rect x="7" y="7" width="32" height="32" rx="4" fill="#fef3c7" stroke="#1e1b18" stroke-width="2.2"/>
      <path d="M13 26 Q23 18 33 26" fill="none" stroke="#b45309" stroke-width="2"/>
    </svg>`;
  }
  if (shape === 'rectangle') {
    return `<svg width="46" height="46" viewBox="0 0 46 46">
      <rect x="4" y="13" width="38" height="22" rx="4" fill="#fde68a" stroke="#1e1b18" stroke-width="2.2"/>
      <ellipse cx="23" cy="25" rx="12" ry="6" fill="#fffbeb" stroke="#b45309" stroke-width="1.6"/>
    </svg>`;
  }
  return `<svg width="46" height="46" viewBox="0 0 46 46">
    <polygon points="6,11 40,11 34,37 12,37" fill="#dcfce7" stroke="#1e1b18" stroke-width="2.2"/>
    <path d="M13 24 Q23 12 33 24" fill="#fef3c7" stroke="#15803d" stroke-width="1.8"/>
  </svg>`;
}

function renderBowlSvg(trial) {
  const rise = trial ? Number(trial.log.rise || 65) : 65;
  const hydr = trial ? Number(trial.variables.hydrationPct || 64) : 64;
  const domeLift = Math.max(24, Math.min(82, rise * 0.78));
  const topY = 122 - domeLift;
  const bubbles = Math.max(4, Math.round(rise / 11));
  let bubbleCircles = '';
  for (let i = 0; i < bubbles; i++) {
    const cx = 88 + ((i * 29) % 104);
    const cy = 112 - ((i * 17) % Math.max(18, Math.round(domeLift * 0.65)));
    const r = 2.2 + (i % 3) * 1.4;
    bubbleCircles += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="rgba(180,83,9,0.22)" />`;
  }
  return `<svg viewBox="0 0 280 175" width="100%">
    <circle cx="140" cy="90" r="78" fill="#faf5eb" stroke="#dcd4c6" stroke-dasharray="4 4" stroke-width="1.5"/>
    <!-- Dough dome reacting to rise % -->
    <path d="M 68 118 Q 140 ${topY} 212 118 Z" fill="#fde68a" stroke="#b45309" stroke-width="2"/>
    ${bubbleCircles}
    <!-- Bowl rim & cross section -->
    <path d="M 54 112 L 74 152 Q 140 162 206 152 L 226 112" fill="rgba(255,255,255,0.62)" stroke="#1e1b18" stroke-width="2.6"/>
    <ellipse cx="140" cy="112" rx="86" ry="11" fill="none" stroke="#1e1b18" stroke-width="2.2"/>
    <!-- Object reference leader arrow -->
    <path d="M 182 ${Math.max(46, topY + 24)} L 236 34" stroke="#b45309" stroke-width="1.8" marker-end="url(#arrow)"/>
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#b45309"/>
      </marker>
    </defs>
    <text x="140" y="24" text-anchor="middle" font-size="11" font-family="monospace" fill="#686158">rise ${rise}% · hydr ${hydr}%</text>
  </svg>`;
}

function renderTrajectorySvg(inst, activeVar) {
  const trials = inst.trials;
  if (!trials.length) return '';
  const w = 680, h = 148, padX = 44, padY = 22;
  const stepX = trials.length > 1 ? (w - padX * 2) / (trials.length - 1) : 0;

  const convPts = trials.map((t, i) => {
    const x = padX + (trials.length > 1 ? i * stepX : (w - padX * 2) / 2);
    const y = h - padY - ((t.convergenceScore / 100) * (h - padY * 2));
    return { x, y, t };
  });

  const pathD = convPts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${p.x.toFixed(1)} ${p.y.toFixed(1)}`).join(' ');
  const targetY = h - padY - (0.85 * (h - padY * 2));

  let nodesSvg = convPts.map((p, idx) => {
    const active = idx === STATE.activeTrialIdx;
    const varVal = p.t.variables[activeVar.key];
    const varTxt = varVal == null ? '—' : `${varVal}${activeVar.unit || ''}`;
    const color = p.t.outcomePositive === true ? '#15803d' : (p.t.outcomePositive === false ? '#b91c1c' : '#b45309');
    return `<g style="cursor:pointer" onclick="selectTrial(${idx})">
      <circle cx="${p.x}" cy="${p.y}" r="${active ? 8 : 5.5}" fill="${color}" stroke="#fff" stroke-width="2"/>
      <text x="${p.x}" y="${p.y - 11}" text-anchor="middle" font-size="10.5" font-family="monospace" fill="#1e1b18">T${p.t.trialNumber}: ${p.t.convergenceScore}</text>
      <text x="${p.x}" y="${h - 5}" text-anchor="middle" font-size="10" font-family="monospace" fill="#686158">${activeVar.shortLabel}: ${varTxt}</text>
    </g>`;
  }).join('');

  return `<svg viewBox="0 0 ${w} ${h}" width="100%" style="background:#fff;border:1px solid var(--line);border-radius:10px;">
    <line x1="${padX}" y1="${targetY}" x2="${w - padX}" y2="${targetY}" stroke="#15803d" stroke-dasharray="5 4" stroke-width="1.2"/>
    <text x="${w - padX}" y="${targetY - 4}" text-anchor="end" font-size="10" font-family="monospace" fill="#15803d">Desired outcome threshold (85)</text>
    <path d="${pathD}" fill="none" stroke="#1e1b18" stroke-width="2.4"/>
    ${nodesSvg}
  </svg>`;
}

function renderAll() {
  const inst = currentInstance();
  if (!inst) return;
  const trial = currentTrial();
  const vars = STATE.dashboard.knownVariables;
  const activeVar = vars[STATE.activeVarIdx] || vars[0];

  // Instance tabs
  document.getElementById('instanceTabs').innerHTML = STATE.dashboard.instances.map((it, idx) => {
    const active = idx === STATE.activeInstanceIdx ? 'active' : '';
    const delta = it.improvementDelta != null ? (it.improvementDelta >= 0 ? `+${it.improvementDelta}` : `${it.improvementDelta}`) : '—';
    return `<button class="inst-tab ${active}" onclick="selectInstance(${idx})">
      <span class="code">${it.shortCode}</span>
      <span>${it.name}</span>
      <span class="pill ${it.improvementDelta > 0 ? 'good' : ''}">${it.trials.length}T · Δ${delta}</span>
    </button>`;
  }).join('');

  // Top Phase Stack
  if (trial) {
    document.getElementById('activeTrialBadge').textContent = `${inst.shortCode} · T${trial.trialNumber} (${trial.convergenceScore}/100)`;
    const order = ['mixing', 'resting', 'fermentation'];
    document.getElementById('phaseStack').innerHTML = order.map(k => {
      const p = trial.phases[k];
      const pct = Math.max(0, Math.min(100, Number(p.completionPct || 0)));
      return `<div class="phase-row">
        <div class="vessel-icon">${vesselSvg(p.shapeHint)}</div>
        <div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:13px;font-weight:700;">
            <span>${p.label}</span>
            <span class="pill">${p.metricBadge} · ${pct}%</span>
          </div>
          <div class="pct-bar"><div class="pct-fill" style="width:${pct}%"></div></div>
          <div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;color:var(--muted);">
            <span>${p.feedback}</span>
            <span style="font-family:var(--mono);white-space:nowrap;">[${p.objectRef}]</span>
          </div>
        </div>
      </div>`;
    }).join('');
    document.getElementById('bowlSvgWrap').innerHTML = renderBowlSvg(trial);
    document.getElementById('objRefLabel').textContent = trial.objectRef;
    document.getElementById('objRefCaption').textContent = `${trial.log.title} — ${trial.log.result}`;
  }

  // Left Variable Rail
  document.getElementById('variableRail').innerHTML = vars.map((v, idx) => {
    const active = idx === STATE.activeVarIdx ? 'active' : '';
    const val = trial ? trial.variables[v.key] : null;
    const disp = val == null ? '—' : `${val}${v.unit || ''}`;
    return `<button class="var-node ${active}" onclick="selectVariable(${idx})">
      <span class="var-dot"></span>
      <span style="flex:1">${v.shortLabel}</span>
      <span style="font-family:var(--mono);font-size:11px;opacity:0.85">${disp}</span>
    </button>`;
  }).join('');

  // Middle Journey
  document.getElementById('journeyEyebrow').textContent = `dough id ${inst.shortCode} · ${inst.styleSlug}`;
  document.getElementById('journeyTitle').textContent = `"${inst.name}"`;
  document.getElementById('desiredOutcomeText').textContent = `Desired Outcome: ${inst.desiredOutcome}`;

  const impPill = inst.improvementDelta != null
    ? `<span class="pill ${inst.improvementDelta >= 0 ? 'good' : 'bad'}">${inst.improvementDelta >= 0 ? '+' : ''}${inst.improvementDelta} pts vs T1</span>`
    : '';
  document.getElementById('journeyKpis').innerHTML = `
    <span class="pill amber">Convergence ${inst.convergenceScore ?? '—'}/100</span>
    ${impPill}
    <span class="pill">${inst.successRate ?? 0}% win rate</span>
  `;

  document.getElementById('journeyChain').innerHTML = inst.trials.map((t, idx) => {
    const active = idx === STATE.activeTrialIdx ? 'active' : '';
    const sz = Math.round(34 + Math.min(20, (Number(t.log.rise || 55) - 45) * 0.35));
    const badgeCls = t.outcomePositive === true ? 'good' : (t.outcomePositive === false ? 'bad' : '');
    const arrow = idx < inst.trials.length - 1 ? `<span class="chain-arrow">→</span>` : '';
    return `<div class="ball-step ${active}" onclick="selectTrial(${idx})">
      <div class="ball-circle" style="width:${sz}px;height:${sz}px;">T${t.trialNumber}</div>
      <div style="font-size:11.5px;font-weight:700;">${t.variables.hydrationPct}% · ${t.log.rise}%</div>
      <span class="pill ${badgeCls}">${t.convergenceScore} pts</span>
    </div>${arrow}`;
  }).join('');

  document.getElementById('trajectoryChartWrap').innerHTML = renderTrajectorySvg(inst, activeVar);

  // Storybank
  document.getElementById('storybankList').innerHTML = inst.storybank.map(s => {
    const cls = s.outcomePositive === true ? 'good' : (s.outcomePositive === false ? 'bad' : '');
    return `<div class="story-item">
      <div style="display:flex;justify-content:space-between;gap:8px;font-weight:700;margin-bottom:3px;">
        <span>${s.headline}</span>
        <span class="pill ${cls}">${s.convergenceScore}/100</span>
      </div>
      <div>${s.narrative}</div>
      ${s.hypothesis ? `<div style="font-size:11.5px;color:var(--muted);margin-top:3px;"><em>Hypothesis:</em> ${s.hypothesis}</div>` : ''}
    </div>`;
  }).join('');

  // Matched RAG Cards
  document.getElementById('ragCountBadge').textContent = `${inst.matchedCards.length} matched / ${STATE.dashboard.ragCardCount} corpus`;
  document.getElementById('instanceRagCards').innerHTML = inst.matchedCards.map(c => {
    const sw = (c.tags || []).includes('sourcewise_supported') ? `<span class="pill good">sourcewise_supported</span>` : '';
    return `<div class="rag-card">
      <div style="display:flex;justify-content:space-between;gap:6px;margin-bottom:4px;">
        <code style="font-size:11px;color:var(--blue);font-weight:700;">${c.id}</code>
        <span>${sw} <span class="pill">conf ${c.confidence}</span></span>
      </div>
      <div>${c.claim}</div>
      <div style="margin-top:4px;font-size:11px;color:var(--muted);font-family:var(--mono);">
        vars: ${(c.variables || []).join(', ')}
      </div>
    </div>`;
  }).join('');

  // Bottom Left: Task ORDs
  const ords = (trial && trial.log.experimentMeta && trial.log.experimentMeta.taskOrders) || inst.taskOrders;
  const doneCnt = ords.filter(o => o.completed).length;
  document.getElementById('ordDoneBadge').textContent = `${doneCnt}/${ords.length} complete`;
  document.getElementById('taskOrdList').innerHTML = ords.map((o, idx) => `
    <div class="ord-item ${o.completed ? 'done' : ''}" onclick="toggleTaskOrd(${idx})">
      <input type="checkbox" ${o.completed ? 'checked' : ''} style="pointer-events:none;margin-top:3px;" />
      <div style="flex:1">
        <strong>#${o.order}</strong> · ${o.label}
        <div style="font-family:var(--mono);font-size:11px;color:var(--muted);">t = ${o.targetMin >= 0 ? '+' : ''}${o.targetMin}m · ${o.phase}</div>
      </div>
    </div>
  `).join('');

  // Bottom Center: BALL Composition
  const comp = inst.ballComposition;
  document.getElementById('ballCountBadge').textContent = `${comp.ballCount250g} × 250g balls (${comp.totalDoughGrams}g)`;
  document.getElementById('ballEmblem').innerHTML = `
    <div style="font-family:var(--mono);font-size:11px;text-transform:uppercase;color:#92400e;">BALL Composition</div>
    <div style="font-size:26px;font-weight:800;font-family:var(--serif);">${comp.hydrationPct}%</div>
    <div style="font-size:12px;font-weight:600;">${comp.flourLine}</div>
    <div style="font-size:11px;color:#78350f;margin-top:3px;">Yeast ${comp.yeastBakerPct}% · Salt ${comp.saltBakerPct}%</div>
  `;
  document.getElementById('ingTable').innerHTML = comp.ingredients.map(ing => `
    <tr>
      <td><strong>${ing.name}</strong></td>
      <td style="font-family:var(--mono);text-align:right;">${ing.amount} ${ing.unit}</td>
      <td style="font-family:var(--mono);text-align:right;color:var(--muted);">${ing.bakerPercent}%</td>
    </tr>
  `).join('');
  const st = comp.sampleAtLatestRise;
  document.getElementById('stoichLine').textContent =
    `Stoichiometry: ${st.sugarConsumedG}g sugar → ${st.co2ProducedG}g CO₂ (${st.co2HeadspacePpm} ppm) + ${st.ethanolProducedG}g EtOH`;
}

function selectInstance(idx) {
  STATE.activeInstanceIdx = idx;
  const inst = currentInstance();
  STATE.activeTrialIdx = inst && inst.trials.length ? inst.trials.length - 1 : 0;
  renderAll();
  populateComparisonSelectors(true);
  runComparison();
}

function selectTrial(idx) {
  STATE.activeTrialIdx = idx;
  renderAll();
}

function selectVariable(idx) {
  STATE.activeVarIdx = idx;
  renderAll();
}

function scrubVariable(dir) {
  const n = STATE.dashboard.knownVariables.length;
  STATE.activeVarIdx = (STATE.activeVarIdx + dir + n) % n;
  renderAll();
}

function populateComparisonSelectors(resetToActiveInstance) {
  const allTrials = [];
  STATE.dashboard.instances.forEach(inst => {
    inst.trials.forEach(t => allTrials.push(t));
  });
  const opts = allTrials.map(t => `<option value="${t.id}">${t.shortCode} T${t.trialNumber} · ${t.log.title} (${t.convergenceScore}pts)</option>`).join('');
  const cSel = document.getElementById('cmpControlSelect');
  const vSel = document.getElementById('cmpVariantSelect');
  cSel.innerHTML = opts;
  vSel.innerHTML = opts;

  if (resetToActiveInstance) {
    const inst = currentInstance();
    if (inst && inst.trials.length >= 2) {
      const latest = inst.trials[inst.trials.length - 1];
      const explicitCtrl = latest.log.experimentMeta && latest.log.experimentMeta.controlLogId;
      const hasCtrl = explicitCtrl && inst.trials.some(t => t.id === explicitCtrl);
      cSel.value = hasCtrl ? explicitCtrl : inst.trials[inst.trials.length - 2].id;
      vSel.value = latest.id;
    } else if (allTrials.length >= 2) {
      cSel.value = allTrials[0].id;
      vSel.value = allTrials[1].id;
    }
  }
}

async function runComparison() {
  const cId = document.getElementById('cmpControlSelect').value;
  const vId = document.getElementById('cmpVariantSelect').value;
  if (!cId || !vId) return;
  const res = await fetch(`/api/compare?control=${encodeURIComponent(cId)}&variant=${encodeURIComponent(vId)}`);
  const cmp = await res.json();
  STATE.comparison = cmp;
  renderComparison(cmp);
}

function renderComparison(cmp) {
  if (!cmp || cmp.error) return;
  const dots = cmp.deltas.slice(0, 12).map(d => `<span class="${d.changed ? 'hot' : 'lock'}" title="${d.definition.label}: ${d.deltaFormatted}"></span>`).join('');
  document.getElementById('dotMatrix').innerHTML = dots;

  const rigorCls = cmp.scientificRigor === 'single-variable' ? 'good'
    : (cmp.scientificRigor === 'confounded' ? 'bad' : 'amber');

  const changedRows = cmp.isolatedVariables.map(d => `
    <tr class="changed">
      <td>Δ ${d.definition.label}</td>
      <td style="font-family:var(--mono)">${d.controlFormatted}</td>
      <td style="font-family:var(--mono)">${d.variantFormatted}</td>
      <td style="font-family:var(--mono)">${d.deltaFormatted}</td>
    </tr>
  `).join('');

  const heldNames = cmp.heldConstant.map(d => `${d.definition.shortLabel} (${d.controlFormatted})`).join(' · ');

  const inst = currentInstance();
  const effectsHtml = (inst && inst.variableEffects || []).slice(0, 3).map(ef => `
    <div style="font-size:11.5px;padding:5px 8px;background:#faf6ee;border-radius:6px;margin-top:4px;">
      <strong>${ef.label}</strong> (${ef.signalStrength}% signal): ${ef.directionHint}
    </div>
  `).join('');

  const claimBox = cmp.draftedClaim ? `
    <div style="margin-top:12px;padding:10px 12px;border:1.5px dashed var(--blue);background:#eff6ff;border-radius:10px;">
      <div style="font-family:var(--mono);font-size:11px;color:var(--blue);font-weight:700;margin-bottom:4px;">
        RAG BRIDGE · EMPIRICAL CLAIM READY FOR CANDIDATES.JSONL
      </div>
      <div style="font-size:12.5px;margin-bottom:8px;">"${cmp.draftedClaim.claim}"</div>
      <div class="btn-row">
        <button class="primary" onclick="stageDraftedClaim()">Stage → candidates.jsonl</button>
        <button onclick="verifyDraftedClaim('internal')">Verify (Internal Corpus)</button>
        <button onclick="verifyDraftedClaim('external')">Verify (External Web)</button>
      </div>
      <div id="verifyResultBox" style="margin-top:8px;font-size:12px;"></div>
    </div>
  ` : `<div style="margin-top:10px;font-size:12px;color:var(--muted);">
    <em>Tip:</em> Isolate 1–2 variables (avoid confounding) to unlock one-click empirical RAG claim minting.
  </div>`;

  document.getElementById('comparisonBody').innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px;">
      <span class="pill ${rigorCls}">${cmp.rigorBadge}</span>
      <span class="pill">Convergence ${cmp.convergenceDelta >= 0 ? '+' : ''}${cmp.convergenceDelta} pts · Rise ${cmp.riseDelta >= 0 ? '+' : ''}${cmp.riseDelta}%</span>
    </div>
    <div style="font-size:12.5px;margin-bottom:6px;"><strong>Verdict:</strong> ${cmp.verdictSummary}</div>
    <div style="font-size:12px;color:var(--muted);margin-bottom:8px;">${cmp.rigorExplanation}</div>
    ${changedRows ? `<table class="delta-table">
      <thead><tr><th>Isolated Variable</th><th>Control</th><th>Variant</th><th>Delta</th></tr></thead>
      <tbody>${changedRows}</tbody>
    </table>` : ''}
    <div style="margin-top:8px;font-size:11.5px;color:var(--green);">
      <strong>✓ Held Constant (${cmp.heldConstant.length}):</strong> ${heldNames || 'None'}
    </div>
    ${effectsHtml ? `<div style="margin-top:10px;"><div class="eyebrow">What Works vs What Doesn't (${inst.shortCode})</div>${effectsHtml}</div>` : ''}
    ${claimBox}
  `;
}

async function stageDraftedClaim() {
  if (!STATE.comparison || !STATE.comparison.draftedClaim) return;
  const res = await fetch('/api/claims/stage', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(STATE.comparison.draftedClaim),
  });
  const data = await res.json();
  if (data.card) {
    showToast(`Staged ${data.card.id} (conf ${data.card.confidence}) to ${data.stagedPath}`);
  }
}

async function verifyDraftedClaim(mode) {
  if (!STATE.comparison || !STATE.comparison.draftedClaim) return;
  const box = document.getElementById('verifyResultBox');
  box.innerHTML = `Running <code>${mode}</code> truth evaluation...`;
  const res = await fetch('/api/claims/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ claim: STATE.comparison.draftedClaim.claim, evidenceMode: mode }),
  });
  const out = await res.json();
  const srcCount = (out.sources || []).length;
  box.innerHTML = `<strong>Verdict (${out.evidence_mode}):</strong> <code>${out.verdict}</code> (conf ${out.confidence}) — ${out.reasoning} <br/><span style="font-family:var(--mono);font-size:11px;">sources[] attached: ${srcCount}</span>`;
}

function toggleComposer() {
  const el = document.getElementById('composerDrawer');
  el.classList.toggle('open');
  if (el.classList.contains('open')) prefillComposerFromInstance();
}

function populateComposerProfiles() {
  const sel = document.getElementById('cmpProfile');
  sel.innerHTML = STATE.dashboard.profiles.map(p => `<option value="${p.id}">${p.shortCode || ''} ${p.name}</option>`).join('');
}

function prefillComposerFromInstance() {
  const inst = currentInstance();
  if (!inst) return;
  if (inst.profileId) document.getElementById('cmpProfile').value = inst.profileId;
  const base = inst.latestTrial ? inst.latestTrial.variables : {
    hydrationPct: inst.baselineConfig.hydrationTarget || 64,
    flourTypeKey: inst.baselineConfig.selectedFlour || '00',
    phaseTempFRest: 70,
    phaseTempFFerment: 68,
    fridgePhase: inst.baselineConfig.fridgePhase || 'none',
    prefermentShare: inst.baselineConfig.prefermentFlourPercent || 0,
    timelineTimeMin: inst.baselineConfig.maxTime || 420,
  };
  window._composerBase = base;
  const nextT = (inst.trials.length || 0) + 1;
  document.getElementById('cmpTitle').value = `T${nextT} · Single-variable trial`;
  document.getElementById('cmpObjRef').value = `${inst.shortCode} · Ball T${nextT}`;
  document.getElementById('cmpHydration').value = base.hydrationPct;
  document.getElementById('cmpFlour').value = String(base.flourTypeKey).startsWith('blend:') ? 'bread' : base.flourTypeKey;
  document.getElementById('cmpRestTemp').value = base.phaseTempFRest;
  document.getElementById('cmpFermentTemp').value = base.phaseTempFFerment;
  document.getElementById('cmpFridge').value = base.fridgePhase;
  document.getElementById('cmpPreferment').value = base.prefermentShare;
  document.getElementById('cmpClock').value = base.timelineTimeMin || 420;
  recalcComposerDiff();
}

function recalcComposerDiff() {
  const b = window._composerBase || {};
  const changed = [];
  if (Math.abs(Number(document.getElementById('cmpHydration').value) - Number(b.hydrationPct || 64)) > 0.4) changed.push('hydrationPct');
  if (Math.abs(Number(document.getElementById('cmpRestTemp').value) - Number(b.phaseTempFRest || 70)) > 1) changed.push('phaseTempFRest');
  if (Math.abs(Number(document.getElementById('cmpFermentTemp').value) - Number(b.phaseTempFFerment || 68)) > 1) changed.push('phaseTempFFerment');
  if (document.getElementById('cmpFridge').value !== (b.fridgePhase || 'none')) changed.push('fridgePhase');
  if (Math.abs(Number(document.getElementById('cmpPreferment').value) - Number(b.prefermentShare || 0)) > 1) changed.push('prefermentShare');
  if (Math.abs(Number(document.getElementById('cmpClock').value) - Number(b.timelineTimeMin || 420)) > 10) changed.push('timelineTimeMin');

  window._composerIsolated = changed;
  const badge = document.getElementById('composerIsolationBadge');
  if (changed.length === 0) {
    badge.className = 'pill';
    badge.textContent = 'Pure replicate (0 variables changed)';
  } else if (changed.length === 1) {
    badge.className = 'pill good';
    badge.textContent = `✓ Scientific 1-variable isolation: ${changed[0]}`;
  } else {
    badge.className = 'pill bad';
    badge.textContent = `⚠ Confounded (${changed.length} variables shifted: ${changed.join(', ')})`;
  }
}

async function submitComposerTrial() {
  const inst = currentInstance();
  const hydr = Number(document.getElementById('cmpHydration').value);
  const restT = Number(document.getElementById('cmpRestTemp').value);
  const fermT = Number(document.getElementById('cmpFermentTemp').value);
  const rise = Number(document.getElementById('cmpRise').value);
  const clock = Number(document.getElementById('cmpClock').value);
  const pref = Number(document.getElementById('cmpPreferment').value);
  const objRef = document.getElementById('cmpObjRef').value;

  const payload = {
    profileId: document.getElementById('cmpProfile').value,
    title: document.getElementById('cmpTitle').value,
    hydration: hydr,
    flour: document.getElementById('cmpFlour').value,
    fridgePhase: document.getElementById('cmpFridge').value,
    prefermentType: pref > 0 ? 'poolish' : 'none',
    prefermentFlourPercent: pref,
    phaseTempF: { rest: restT, ferment: fermT, proof: fermT + 2 },
    rise: rise,
    timelineTimeMin: clock,
    maxTime: clock,
    outcomeRating: document.getElementById('cmpOutcome').value || null,
    result: document.getElementById('cmpResult').value || 'Controlled trial recorded',
    phaseLogs: {
      mixing: { phase: 'mixing', completionPct: hydr, tempF: restT, durationMin: 15, feedback: document.getElementById('cmpMixNote').value, objectRef: objRef },
      resting: { phase: 'resting', completionPct: 80, tempF: restT, durationMin: 30, feedback: document.getElementById('cmpRestNote').value, objectRef: objRef },
      fermentation: { phase: 'fermentation', completionPct: rise, tempF: fermT, durationMin: Math.max(30, clock - 45), feedback: document.getElementById('cmpFermNote').value, objectRef: objRef },
    },
    experimentMeta: {
      desiredOutcome: inst ? inst.desiredOutcome : undefined,
      hypothesis: document.getElementById('cmpHypothesis').value,
      isolatedVariables: window._composerIsolated || [],
      controlLogId: inst && inst.latestTrial ? inst.latestTrial.id : undefined,
      objectRef: objRef,
      storyNote: document.getElementById('cmpResult').value,
    },
  };

  const res = await fetch('/api/logs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  STATE.dashboard = data.dashboard;
  const updatedInst = currentInstance();
  STATE.activeTrialIdx = updatedInst.trials.length - 1;
  document.getElementById('composerDrawer').classList.remove('open');
  renderAll();
  populateComparisonSelectors(true);
  await runComparison();
  showToast(`Saved ${data.created.title}`);
}

async function saveStoryNote() {
  const trial = currentTrial();
  const txt = document.getElementById('storyAppendInput').value.trim();
  if (!trial || !txt) return;
  const res = await fetch(`/api/logs/${encodeURIComponent(trial.id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ experimentMeta: { storyNote: txt } }),
  });
  const data = await res.json();
  STATE.dashboard = data.dashboard;
  document.getElementById('storyAppendInput').value = '';
  renderAll();
  showToast('Updated Storybank narrative');
}

async function toggleTaskOrd(ordIdx) {
  const inst = currentInstance();
  const trial = currentTrial();
  if (!inst || !trial) return;
  const existing = ((trial.log.experimentMeta && trial.log.experimentMeta.taskOrders) || inst.taskOrders).map(o => ({...o}));
  existing[ordIdx].completed = !existing[ordIdx].completed;
  const res = await fetch(`/api/logs/${encodeURIComponent(trial.id)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ experimentMeta: { taskOrders: existing } }),
  });
  const data = await res.json();
  STATE.dashboard = data.dashboard;
  renderAll();
}

function exportBridgeBundle() {
  window.location.href = '/api/bridge/export';
}

async function importBridgeFile(ev) {
  const file = ev.target.files[0];
  if (!file) return;
  const text = await file.text();
  const json = JSON.parse(text);
  const payload = Array.isArray(json) ? { logs: json } : json;
  const res = await fetch('/api/bridge/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  STATE.dashboard = data.dashboard;
  renderAll();
  populateComparisonSelectors(true);
  await runComparison();
  showToast(`Imported ${data.imported.logsMerged} logs & ${data.imported.profilesMerged} profiles`);
}

boot();
</script>
</body>
</html>
"""


class AdminRequestHandler(BaseHTTPRequestHandler):
    profiles_path = PROFILES_PATH
    logs_path = LOGS_PATH

    def _send_cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(raw)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/admin")
            self.end_headers()
            return
        if parsed.path == "/admin":
            raw = ADMIN_HTML.encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return
        status, out = dispatch_api_request(
            "GET",
            parsed.path,
            parse_qs(parsed.query),
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self._send_json(status, out)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        status, out = dispatch_api_request(
            "POST",
            parsed.path,
            parse_qs(parsed.query),
            body=body,
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self._send_json(status, out)

    def do_PATCH(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid JSON body"})
            return
        status, out = dispatch_api_request(
            "PATCH",
            parsed.path,
            parse_qs(parsed.query),
            body=body,
            profiles_path=self.profiles_path,
            logs_path=self.logs_path,
        )
        self._send_json(status, out)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RAG /admin Instance & Log Analytics server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Bind port (default: 8765)")
    args = parser.parse_args()

    ensure_store()
    server = ReusableThreadingHTTPServer((args.host, args.port), AdminRequestHandler)
    print(f"Dough Instance Lab & RAG Bridge running at http://{args.host}:{args.port}/admin", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down /admin server.")
        server.server_close()


if __name__ == "__main__":
    main()
