(() => {
  document.querySelectorAll('[data-garmin-sync-form]').forEach((form) => {
    let sending = false;
    const button = form.querySelector('button[type="submit"]');
    const label = form.querySelector('[data-garmin-sync-label]');
    const status = form.querySelector('[data-garmin-sync-status]');
    const originallyDisabled = button ? button.disabled : false;
    const originalLabel = label ? label.textContent : '';
    window.addEventListener('pageshow', (event) => {
      if (!event.persisted) return;
      sending = false;
      form.removeAttribute('aria-busy');
      if (button) button.disabled = originallyDisabled;
      if (label) label.textContent = originalLabel;
      if (status) status.textContent = '';
    });
    form.addEventListener('submit', (event) => {
      if (sending) { event.preventDefault(); return; }
      if (!form.checkValidity()) return;
      sending = true;
      form.setAttribute('aria-busy', 'true');
      if (button) button.disabled = true;
      if (label) label.textContent = 'Syncing Garmin…';
      if (status) status.textContent = 'Checking saved Garmin readings. This may take a moment.';
    });
  });
})();
