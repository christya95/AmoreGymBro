/* Run before styles paint; a saved choice overrides the device appearance. */
(() => {
  const storageKey = "amoregymbro-theme";
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  const root = document.documentElement;
  let button;

  function savedTheme() {
    try {
      const value = localStorage.getItem(storageKey);
      return value === "light" || value === "dark" ? value : null;
    } catch {
      return null; // Storage can be unavailable in restricted browsing contexts.
    }
  }

  let preference = savedTheme();

  function applyTheme() {
    const theme = preference || (system.matches ? "dark" : "light");
    root.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]').content =
      theme === "dark" ? "#111722" : "#f5f5f8";
    if (button) {
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    }
  }

  applyTheme();
  document.addEventListener("DOMContentLoaded", () => {
    button = document.querySelector(".theme-toggle");
    if (!button) return;
    applyTheme();
    button.hidden = false;
    button.addEventListener("click", () => {
      preference = root.dataset.theme === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(storageKey, preference);
      } catch {
        // The toggle still works for this page when persistence is blocked.
      }
      applyTheme();
    });
  });
  system.addEventListener("change", applyTheme);
  window.addEventListener("storage", (event) => {
    if (event.key === storageKey || event.key === null) {
      preference = savedTheme();
      applyTheme();
    }
  });
})();
