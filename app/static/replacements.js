(() => {
  const form = document.querySelector('[data-replacements]');
  if (!form) { if(document.querySelector('[data-replacement-refresh]')) setTimeout(() => { if(!document.querySelector('details[open]')) window.location.reload(); },15000); return; }
  if(form.dataset.bound) return;
  form.dataset.bound = 'true';
  const choices = [...form.querySelectorAll('input[name="selection"]')];
  const all = form.querySelector('[data-select-replacements]');
  function update() {
    const checked = choices.filter(c => c.checked);
    form.querySelector('[data-replacement-count]').textContent = `${checked.length} selected · ${(checked.reduce((n,c) => n + Number(c.dataset.saving),0)/1e9).toFixed(1)} GB reduction`;
    all.checked = choices.length > 0 && checked.length === choices.length;
    all.indeterminate = checked.length > 0 && checked.length < choices.length;
  }
  all.addEventListener('change', () => { choices.forEach(c => c.checked = all.checked); update(); });
  choices.forEach(c => c.addEventListener('change', update));
  form.addEventListener('submit', e => { if (!choices.some(c => c.checked)) { e.preventDefault(); form.querySelector('[data-replacement-count]').textContent = 'Select at least one replacement.'; } });
  update();
  if (document.querySelector('[data-replacement-refresh]')) setTimeout(() => { if (!choices.some(c => c.checked)) window.location.reload(); },15000);
})();
