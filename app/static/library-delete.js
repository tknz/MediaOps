document.querySelector('[data-library-delete]')?.addEventListener('submit', (event) => {
  const form = event.currentTarget;
  if (form.dataset.submitted) { event.preventDefault(); return; }
  form.dataset.submitted = 'true';
  form.querySelector('button').disabled = true;
  form.querySelector('[data-delete-status]').textContent = 'Deleting… Wait for confirmation before trying again.';
});
