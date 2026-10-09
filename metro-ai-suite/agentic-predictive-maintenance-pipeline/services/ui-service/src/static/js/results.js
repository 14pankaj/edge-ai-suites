// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Progressive-disclosure "View full report" toggle for the long-form
// agent-generated text blocks on the Run Results page (Policy/Analysis/
// Evidence/Ticket cards). Each clamp starts collapsed to a glanceable
// height; clicking the toggle expands it in place.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll(".report-toggle").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var clamp = document.getElementById(btn.dataset.target);
      if (!clamp) return;
      // First click reveals a tile card's hidden full-text block; after
      // that it behaves as a normal clamp/expand toggle.
      clamp.classList.remove("is-hidden");
      var expanded = clamp.classList.toggle("is-expanded");
      btn.textContent = expanded ? "Show less ▴" : "View full report ▾";
    });
  });
});
