(() => {
  const nodes = document.querySelectorAll("time[datetime]");
  if (!nodes.length) return;
  const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
  const dateTimeFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
  nodes.forEach((node) => {
    // A calendar date is not a UTC instant: keep a saved weigh-in on its own day.
    const calendarDate = /^\d{4}-\d{2}-\d{2}$/.test(node.dateTime);
    const value = new Date(calendarDate ? `${node.dateTime}T12:00:00` : node.dateTime);
    if (!Number.isNaN(value.valueOf())) {
      const formatter = calendarDate || node.hasAttribute("data-date-only") ? dateFormat : dateTimeFormat;
      node.textContent = formatter.format(value);
    }
  });
})();
