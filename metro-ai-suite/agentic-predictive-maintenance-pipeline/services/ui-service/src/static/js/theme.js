// Copyright (C) 2026 Intel Corporation
// SPDX-License-Identifier: Apache-2.0
//
// Light/dark theme toggle. Dark is the default theme (matching the
// ui-ux-builder skill's Stack C palette); the choice is persisted in
// localStorage and applied to <html data-theme="..."> before first paint via
// the inline snippet in each page's <head> (see applyStoredTheme below).

(function () {
  const STORAGE_KEY = "apm-theme";

  function applyStoredTheme() {
    const stored = localStorage.getItem(STORAGE_KEY);
    const theme = stored === "light" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", theme);
  }

  function toggleTheme() {
    const current = document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
    const next = current === "light" ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem(STORAGE_KEY, next);
  }

  // Applied immediately on script load (this file is loaded synchronously in
  // <head>, before body paint) so the page never flashes the wrong theme.
  applyStoredTheme();

  window.addEventListener("DOMContentLoaded", function () {
    const btn = document.getElementById("themeToggle");
    if (btn) btn.addEventListener("click", toggleTheme);
  });
})();
