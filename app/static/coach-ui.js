document.querySelectorAll('[data-coach-form]').forEach((coachForm) => {
  const button = coachForm.querySelector('button[type="submit"]');
  const label = coachForm.querySelector('[data-coach-button-label]');
  const status = coachForm.querySelector('[data-coach-status]');
  coachForm.addEventListener('submit', (event) => {
    if (button.disabled) {
      event.preventDefault();
      return;
    }
    button.disabled = true;
    label.textContent = 'Zeus is thinking…';
    status.textContent = 'Big Dawg, Zeus is working on it. Hang tight.';
  });
  window.addEventListener('pageshow', () => {
    button.disabled = false;
    label.textContent = coachForm.dataset.buttonLabel || 'Ask Proffessor Zeus';
    status.textContent = '';
  });
});
