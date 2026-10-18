(function () {
  const root = document.querySelector('[data-jobs-app]');
  if (!root) return;
  const timezone = root.dataset.timezone || 'UTC';
  function time(value) {
    if (!value) return '—';
    const date = new Date(/Z$|[+-]\d\d:\d\d$/.test(value) ? value : value + 'Z');
    if (Number.isNaN(date.valueOf())) return value;
    return new Intl.DateTimeFormat('en-NZ', { timeZone: timezone, dateStyle: 'short', timeStyle: 'short' }).format(date);
  }
  function set(row, field, value) {
    const element = row.querySelector('[data-job-' + field + ']');
    if (element) element.textContent = value;
  }
  let inFlight = false;
  async function refresh() {
    if (inFlight || document.hidden) return;
    inFlight = true;
    try {
      const response = await fetch('/api/jobs', { cache: 'no-store' });
      if (!response.ok) return;
      const payload = await response.json();
      for (const job of payload.jobs || []) {
        const row = document.getElementById(job.id);
        if (!row || !root.contains(row)) continue;
        const run = job.last_run || {};
        set(row, 'status', job.status.replaceAll('_', ' '));
        set(row, 'last', time(run.started_at || run.queued_at));
        set(row, 'duration', run.duration_seconds == null ? '' : run.duration_seconds + 's');
        set(row, 'summary', (run.summary || {}).label || '');
        set(row, 'error', run.error || '');
        set(row, 'next', job.next_run ? time(job.next_run) : job.setting && !job.enabled ? 'Paused' : '—');
        const button = row.querySelector('[data-job-run]');
        if (button) button.disabled = ['queued', 'running'].includes(job.status);
      }
    } catch (_) { /* Keep the last rendered state during a connection failure. */ }
    finally { inFlight = false; }
  }
  root.addEventListener('toggle', async (event) => {
    const details = event.target;
    if (!details.matches('details[data-job-history]') || !details.open) return;
    const row = details.closest('tr');
    const target = details.querySelector('[data-job-runs]');
    try {
      const response = await fetch('/api/jobs/' + encodeURIComponent(row.id), { cache: 'no-store' });
      if (!response.ok) return;
      const job = await response.json();
      const entries = (job.runs || []).map((run) => {
        const p = document.createElement('p');
        p.textContent = run.status.replaceAll('_', ' ') + ' · ' + time(run.started_at || run.queued_at);
        const small = document.createElement('small');
        small.textContent = (run.trigger || '') + (run.duration_seconds == null ? '' : ' · ' + run.duration_seconds + 's') + (run.error ? ' · ' + run.error : '');
        p.append(small); return p;
      });
      target.replaceChildren(...entries);
      if (!entries.length) target.textContent = 'No runs recorded yet.';
    } catch (_) { /* The server-rendered history remains available. */ }
  }, true);
  setInterval(refresh, 5000);
})();
