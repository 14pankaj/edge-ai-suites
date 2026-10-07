// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Lets the user click (or press Enter/Space on) a row in "Recent Agent Runs"
// to load that run's status/checklist into the "Agent Run" card above,
// without a full page reload. Falls back gracefully — every row still links
// out to /results/<run_id> for the full page, so nothing breaks without JS.

(function () {
  "use strict";

  // Current selection is tracked here (not just via a CSS class) because
  // live-status.js wholesale-replaces #runs-tbody's innerHTML on every poll,
  // wiping any classes on the old <tr> nodes.
  let selectedRunId = null;

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function checklistItemHtml(name, doneDesc, pendingDesc, done, optionalWhenPending) {
    const doneClass = done ? "agent-step-done" : (optionalWhenPending ? "agent-step-optional" : "");
    const statusText = done ? "✓ DONE" : (optionalWhenPending ? "OPTIONAL" : "pending");
    const desc = done ? doneDesc : pendingDesc;
    return `
      <li class="${doneClass}">
        <span class="agent-step-name">${escapeHtml(name)}</span>
        <span class="agent-step-desc">${escapeHtml(desc)}</span>
        <span class="agent-step-status">${escapeHtml(statusText)}</span>
      </li>`;
  }

  function renderAgentRunBody(runId, phase, result) {
    const container = document.getElementById("agent-run-body");
    if (!container) return;

    if (!result) {
      container.innerHTML = `<p class="empty">Run <code>${escapeHtml(runId)}</code> details are temporarily unavailable.</p>`;
      return;
    }

    const status = result.status === "running" ? "running" : (result.error ? "error" : "completed");
    const phaseSuffix = status === "running" && phase ? ` — ${escapeHtml(phase)}` : "";

    container.innerHTML = `
      <p class="run-id-line">
        <span class="status-badge status-${escapeHtml(status)}">${escapeHtml(status)}${phaseSuffix}</span>
        <code class="run-id-cell">${escapeHtml(runId)}</code>
      </p>
      <ul class="agent-checklist">
        ${checklistItemHtml("Policy", "Policy checks completed", "Running policy checks…", !!result.policy)}
        ${checklistItemHtml("Analysis", "Detection results analyzed", "Analyzing detection results…", !!result.analysis)}
        ${checklistItemHtml("Evidence Audit Trail", "Evidence records generated", "Generating evidence records…", !!result.evidence)}
        ${checklistItemHtml("Maintenance Ticket", "Ticket created", "Created only if required", !!result.ticket, true)}
      </ul>
      <p class="status-note"><a href="/results/${encodeURIComponent(runId)}">View full run results &rarr;</a></p>
    `;
  }

  function reapplyHighlight() {
    document.querySelectorAll(".run-row").forEach((row) => {
      row.classList.toggle("run-row-selected", row.getAttribute("data-run-id") === selectedRunId);
    });
  }

  async function loadAndRenderRun(runId) {
    try {
      const res = await fetch(`/api/run/${encodeURIComponent(runId)}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      renderAgentRunBody(data.run_id, data.phase, data.result);
    } catch (err) {
      console.debug("Failed to load run details:", err);
      renderAgentRunBody(runId, null, null);
    }
  }

  async function selectRun(row) {
    const runId = row.getAttribute("data-run-id");
    if (!runId) return;
    selectedRunId = runId;
    reapplyHighlight();
    await loadAndRenderRun(runId);
  }

  // Used by live-status.js to auto-follow a newly-started run (so the card
  // doesn't keep showing a previous, already-completed run's checklist once
  // a new run begins in the background).
  async function selectRunId(runId) {
    if (!runId) return;
    selectedRunId = runId;
    reapplyHighlight();
    await loadAndRenderRun(runId);
  }

  // Called on every live-status.js poll so the checklist keeps advancing
  // (pending -> done) while the selected run is still in progress, instead
  // of staying frozen at whatever it looked like when first selected/loaded.
  function refreshSelected() {
    if (selectedRunId) loadAndRenderRun(selectedRunId);
  }

  function findRow(target) {
    return target && target.closest ? target.closest(".run-row") : null;
  }

  function init() {
    const initialSelected = document.querySelector(".run-row-selected");
    if (initialSelected) selectedRunId = initialSelected.getAttribute("data-run-id");

    // Delegate from the tbody (which persists across live-status.js's
    // innerHTML re-renders) rather than binding each <tr> directly, so
    // clicking/selecting a run keeps working after every poll — not just
    // on rows present at the initial page load.
    const tbody = document.getElementById("runs-tbody");
    if (!tbody) return;
    tbody.addEventListener("click", (event) => {
      const row = findRow(event.target);
      if (row) selectRun(row);
    });
    tbody.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const row = findRow(event.target);
      if (row) {
        event.preventDefault();
        selectRun(row);
      }
    });
  }

  window.RunSelect = {
    getSelectedRunId: () => selectedRunId,
    reapplyHighlight,
    refreshSelected,
    selectRunId,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
