document.querySelectorAll('[data-period-picker]').forEach((form) => {
  const slider = form.querySelector('.period-slider');
  const range = form.querySelector('.period-range');
  const field = form.querySelector('[data-period-value]');
  const labels = [...form.querySelectorAll('.period-labels span')];
  const keys = ['today', 'week', 'month', 'all'];
  function update() {
    const index = Number(range.value);
    slider.style.setProperty('--period-index', index);
    field.value = keys[index];
    range.setAttribute('aria-valuetext', labels[index].textContent);
    labels.forEach((label, i) => label.classList.toggle('selected', i === index));
  }
  field.disabled = false;
  slider.hidden = false;
  update();
  range.addEventListener('input', update);
  // Dragging only moves the selection. Releasing submits one local request.
  range.addEventListener('change', () => { update(); form.requestSubmit(); });
  window.addEventListener('pageshow', () => { range.value = range.defaultValue; update(); });
});
