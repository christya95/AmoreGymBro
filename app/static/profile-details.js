(() => {
  // Open the edit panel before following a link to it or a missing field.
  const reveal = (hash) => {
    const target = document.getElementById(hash.slice(1));
    if (!target) return;
    const panel = target.closest("details");
    if (panel) panel.open = true;
    if (target.matches("input, select, textarea")) target.focus({ preventScroll: true });
  };
  document.querySelectorAll('a[href^="#"]').forEach((link) => {
    link.addEventListener("click", () => reveal(link.hash));
  });
  reveal(window.location.hash);
})();
