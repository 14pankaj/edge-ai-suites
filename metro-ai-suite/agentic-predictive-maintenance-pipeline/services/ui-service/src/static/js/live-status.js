// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Polls /api/status and patches the dashboard in place so detection counts,
// run history, and the current run phase (detecting -> reasoning -> completed)
// stay current without a full page reload.

const POLL_INTERVAL_MS = 3000;

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function renderDetectionsRows(byClass) {
  if (!byClass || byClass.length === 0) return "";
  return byClass
    .map(
      (cls) => `
      <tr>
        <td><span class="label-badge">${escapeHtml(cls.label)}</span></td>
        <td>${cls.count}</td>
        <td>${Number(cls.avg_confidence).toFixed(3)}</td>
        <td>${Number(cls.max_confidence).toFixed(3)}</td>
      </tr>`
    )
    .join("");
}

function statusBadgeHtml(run) {
  const suffix = run.status === "running" && run.phase ? ` — ${escapeHtml(run.phase)}` : "";
  return `<span class="status-badge status-${escapeHtml(run.status)}">${escapeHtml(run.status)}${suffix}</span>`;
}

function runActionHtml(run, activeRun) {
  const runPath = encodeURIComponent(run.run_id);
  let links;
  if (run.status === "completed") {
    links = `<a href="/results/${runPath}">View Results</a><span aria-hidden="true"> · </span>`
      + `<a href="/chat?run_id=${runPath}">Ask about run</a>`;
  } else if (run.status === "running") {
    links = `<a href="/results/${runPath}">Waiting…</a>`;
  } else {
    links = `<a href="/results/${runPath}">View Error</a>`;
  }
  const action = window.__runAgainAction || "/run";
  const disabled = activeRun ? "disabled" : "";
  return `${links}<span aria-hidden="true"> · </span>`
    + `<form method="POST" action="${escapeHtml(action)}" class="run-again-form">`
    + `<button type="submit" class="btn-link run-again-btn" ${disabled} onclick="event.stopPropagation();">&#8635; Run again</button>`
    + `</form>`;
}

function renderRunsRows(runs, activeRun, selectedRunId) {
  if (!runs || runs.length === 0) return "";
  // Chronological order (oldest -> newest) is preserved as given — existing
  // rows stay top-aligned/fixed in place and a new run always appends at the
  // bottom, instead of the whole list reshuffling every time a run starts.
  return runs
    .map((run) => {
      const selectedClass = run.run_id === selectedRunId ? " run-row-selected" : "";
      return `
      <tr class="run-row${selectedClass}" data-run-id="${escapeHtml(run.run_id)}" tabindex="0" role="button"
        aria-label="Select run ${escapeHtml(run.run_id)} to show its details in the Agent Run card">
        <td class="run-id-cell"><code title="${escapeHtml(run.run_id)}">${escapeHtml(run.run_id)}</code></td>
        <td>${statusBadgeHtml(run)}</td>
        <td><div class="run-actions">${runActionHtml(run, activeRun)}</div></td>
      </tr>`;
    })
    .join("");
}

function phaseHintText(activeRun) {
  if (!activeRun) return "Ready — click to run inference + agent analysis.";
  if (activeRun.phase === "detecting") return "Running DL Streamer inference over the video…";
  if (activeRun.phase === "reasoning") return "Detection complete — agents are analyzing the results…";
  return "Run in progress…";
}

async function pollStatus() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();

    const detTotal = document.getElementById("stat-detections");
    const completed = document.getElementById("stat-runs-completed");
    const running = document.getElementById("stat-runs-running");
    if (detTotal) detTotal.textContent = data.total_detections;
    if (completed) completed.textContent = data.runs_completed;
    if (running) running.textContent = data.runs_running;

    const detTbody = document.getElementById("detections-tbody");
    const detTable = document.getElementById("detections-table");
    const detEmpty = document.getElementById("detections-empty");
    if (detTbody) {
      detTbody.innerHTML = renderDetectionsRows(data.by_class);
      if (data.by_class && data.by_class.length > 0) {
        if (detTable) detTable.style.display = "";
        if (detEmpty) detEmpty.style.display = "none";
      }
    }

    const runsTbody = document.getElementById("runs-tbody");
    const runsTable = document.getElementById("runs-table");
    const runsEmpty = document.getElementById("runs-empty");
    if (runsTbody) {
      const selectedRunId = window.RunSelect ? window.RunSelect.getSelectedRunId() : null;
      runsTbody.innerHTML = renderRunsRows(data.recent_runs, data.active_run, selectedRunId);
      if (data.recent_runs && data.recent_runs.length > 0) {
        if (runsTable) runsTable.style.display = "";
        if (runsEmpty) runsEmpty.style.display = "none";
      }
      // Rows were just replaced wholesale — re-apply the selection highlight
      // (click/keyboard handling itself survives via event delegation).
      if (window.RunSelect) window.RunSelect.reapplyHighlight();
    }

    // Keep the "Agent Run" card live instead of freezing at whatever it
    // showed when first rendered/clicked: auto-follow a newly-started run
    // (so a fresh run doesn't leave a stale, already-completed checklist on
    // screen), and otherwise keep re-fetching the currently selected run so
    // its checklist advances pending -> done as the run progresses.
    if (window.RunSelect) {
      const currentSelected = window.RunSelect.getSelectedRunId();
      if (data.active_run && data.active_run.run_id !== currentSelected) {
        window.RunSelect.selectRunId(data.active_run.run_id);
      } else {
        window.RunSelect.refreshSelected();
      }
    }

    const runBtn = document.getElementById("run-pipeline-btn");
    if (runBtn) {
      runBtn.disabled = !!data.active_run;
      runBtn.textContent = data.active_run ? "▶ Running…" : "▶ Run Inspection";
    }

    // Re-enable the Device/Video config fields once the run finishes — the
    // fieldset is disabled server-side only for the initial page render
    // (based on active_run at load time), so without this it would stay
    // grayed out forever after a run completes since this page never reloads.
    const configFieldset = document.getElementById("pipeline-config-fieldset");
    if (configFieldset) configFieldset.disabled = !!data.active_run;

    const phaseHint = document.getElementById("run-phase-hint");
    if (phaseHint) phaseHint.textContent = phaseHintText(data.active_run);
  } catch (err) {
    // Network hiccup — keep the last known state, next poll will retry.
    console.debug("Live status poll failed:", err);
  }
}

pollStatus();
setInterval(pollStatus, POLL_INTERVAL_MS);
