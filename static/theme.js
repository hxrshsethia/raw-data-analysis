"use strict";

(() => {
  const system = matchMedia("(prefers-color-scheme: dark)");
  let preference;
  try { preference = localStorage.getItem("data-explorer-theme"); }
  catch { /* System preference remains available when storage is restricted. */ }
  if (!["light", "dark"].includes(preference)) preference = null;

  function apply() {
    const theme = preference || (system.matches ? "dark" : "light");
    document.documentElement.dataset.theme = theme;
    const control = document.getElementById("theme-toggle");
    if (!control) return;
    control.textContent = "Dark mode";
    control.setAttribute("aria-pressed", String(theme === "dark"));
    control.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
  }

  apply();
  system.addEventListener("change", () => { if (!preference) apply(); });
  document.addEventListener("DOMContentLoaded", () => {
    apply();
    document.getElementById("theme-toggle").addEventListener("click", () => {
      preference = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      try { localStorage.setItem("data-explorer-theme", preference); }
      catch { /* The choice still applies for the current page. */ }
      apply();
    });
  });
})();
