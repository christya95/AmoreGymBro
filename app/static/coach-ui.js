/* One normal POST; the server still validates and saves the complete answer. */
(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');

  function paragraph(text, className) {
    const node = document.createElement('p');
    node.className = className;
    node.textContent = text;
    return node;
  }

  function pendingExchange(messages, question) {
    const user = document.createElement('article');
    user.className = 'zeus-bubble zeus-bubble-user is-sending';
    user.setAttribute('aria-label', 'Your question');
    user.append(paragraph('You asked', 'zeus-speaker'), paragraph(question, 'zeus-message-text'));
    const waiting = document.createElement('article');
    waiting.className = 'zeus-bubble zeus-bubble-coach zeus-pending';
    waiting.setAttribute('aria-hidden', 'true'); // Form status announces loading once.
    const spinner = document.createElement('span');
    spinner.className = 'zeus-spinner';
    waiting.append(spinner, paragraph('Zeus is thinking…', 'meta'));
    messages.append(user, waiting);
    user.scrollIntoView({block: 'nearest', behavior: reducedMotion.matches ? 'auto' : 'smooth'});
    return {user, waiting};
  }

  function revealReply(reply) {
    if (!reply || reply.dataset.replyStatus !== 'answered' || reducedMotion.matches) return Promise.resolve();
    reply.classList.add('is-arriving');
    // Full text stays available to screen readers; facts, caveats and approvals
    // remain visible. Only a freshly returned answer gets the brief typing effect.
    const parts = Array.from(reply.querySelectorAll('[data-typewriter]'), (node) => {
      const text = node.textContent;
      const readable = document.createElement('span');
      readable.className = 'zeus-readable';
      readable.textContent = text;
      const visual = document.createElement('span');
      visual.setAttribute('aria-hidden', 'true');
      const letters = typeof Intl.Segmenter === 'function'
        ? Array.from(new Intl.Segmenter(undefined, {granularity: 'grapheme'}).segment(text), (part) => part.segment)
        : Array.from(text);
      node.replaceChildren(readable, visual);
      return {node, text, visual, letters};
    });
    const total = parts.reduce((sum, part) => sum + part.letters.length, 0);
    const duration = Math.min(1800, Math.max(350, total * 5));
    return new Promise((resolve) => {
      const start = performance.now();
      function frame(now) {
        const progress = reducedMotion.matches || document.hidden ? 1 : Math.min(1, (now - start) / duration);
        let count = Math.ceil(total * progress);
        for (const part of parts) {
          part.visual.textContent = part.letters.slice(0, Math.max(0, count)).join('');
          count -= part.letters.length;
        }
        if (progress < 1) requestAnimationFrame(frame);
        else {
          parts.forEach((part) => part.node.replaceChildren(document.createTextNode(part.text)));
          reply.classList.remove('is-arriving');
          resolve();
        }
      }
      requestAnimationFrame(frame);
    });
  }

  document.querySelectorAll('[data-coach-form]').forEach((form) => {
    const button = form.querySelector('button[type="submit"]');
    const label = form.querySelector('[data-coach-button-label]');
    const spinner = form.querySelector('[data-coach-spinner]');
    const status = form.querySelector('[data-coach-status]');
    const question = form.querySelector('[name="question"]');
    const originalLabel = label.textContent;
    let sending = false;

    function busy(value) {
      sending = value;
      button.disabled = value;
      spinner.hidden = !value;
      label.textContent = value ? 'Zeus is thinking…' : originalLabel;
      form.setAttribute('aria-busy', String(value));
      if (question) question.readOnly = value;
    }

    form.addEventListener('submit', async (event) => {
      if (sending) { event.preventDefault(); return; }
      if (!form.checkValidity()) return;
      // Outline generation retains native navigation and the existing research gate.
      if (!question || !window.fetch || !window.DOMParser) {
        busy(true);
        status.textContent = 'Zeus is working on your reply…';
        return;
      }
      event.preventDefault();
      const data = new FormData(form);
      const text = question.value.trim();
      if (!text) { status.textContent = 'Write a question first.'; return; }
      const messages = document.querySelector('.zeus-messages');
      const previousId = messages.querySelector('[data-reply-id]')?.dataset.replyId;
      busy(true);
      question.blur();
      status.textContent = 'Message sent. Zeus is thinking…';
      const pending = pendingExchange(messages, text);
      try {
        // Never retry a POST automatically: a lost response may still incur inference.
        const response = await fetch(form.action, {method: 'POST', body: data, credentials: 'same-origin', cache: 'no-store'});
        if (!response.ok) throw new Error('Request not confirmed');
        const page = new DOMParser().parseFromString(await response.text(), 'text/html');
        const updated = page.querySelector('.zeus-messages');
        const reply = updated?.querySelector('[data-reply-id]');
        const returnedQuestion = updated?.querySelector('.zeus-bubble-user .zeus-message-text')?.textContent.trim();
        if (!reply || reply.dataset.replyId === previousId || returnedQuestion !== text) {
          const notice = page.querySelector('#coach-notice');
          if (notice) document.querySelector('#coach-notice').replaceChildren(...document.importNode(notice, true).childNodes);
          pending.user.remove();
          status.textContent = page.querySelector('#password')
            ? 'Your session ended. Refresh to sign in again.'
            : 'No new reply was confirmed. Your question is still in the box; refresh to check before trying again.';
          return;
        }
        // Import escaped server markup, never model text interpreted as HTML.
        const incoming = document.importNode(updated, true);
        messages.replaceWith(incoming);
        document.querySelector('#coach-notice').replaceChildren();
        question.value = '';
        status.textContent = 'Zeus replied.';
        const answer = incoming.querySelector('[data-reply-id]');
        answer.scrollIntoView({block: 'start', behavior: reducedMotion.matches ? 'auto' : 'smooth'});
        await revealReply(answer);
      } catch {
        pending.user.classList.remove('is-sending');
        pending.user.querySelector('.zeus-speaker').textContent = 'Send not confirmed';
        status.textContent = 'Connection interrupted. Your request may have finished. Refresh to check before sending again.';
      } finally {
        pending.waiting.remove();
        busy(false);
      }
    });
    window.addEventListener('pageshow', (event) => {
      if (event.persisted) { busy(false); status.textContent = ''; }
    });
  });
})();
