(function(){
  const root = document.getElementById('requestNotifier');
  if(!root) return;

  const style = document.createElement('style');
  style.textContent = `
    .request-notifier{position:relative;flex:none}
    .request-notifier[hidden]{display:none}
    .request-bell{display:inline-flex;align-items:center;gap:7px;min-height:36px;padding:7px 10px;border:1px solid rgba(255,180,92,.38);border-radius:14px;background:rgba(255,180,92,.12);color:#ffd7a3;font-weight:900;cursor:pointer}
    .request-bell[data-empty="true"]{border-color:rgba(180,205,230,.12);background:rgba(255,255,255,.025);color:var(--muted)}
    .request-bell b{display:inline-grid;place-items:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:rgba(255,180,92,.24);color:#ffe2b5;font-size:.78rem}
    .request-tray{position:absolute;right:0;top:calc(100% + 10px);width:min(430px,calc(100vw - 28px));max-height:70vh;overflow:auto;border:1px solid var(--line);border-radius:18px;background:rgba(7,11,16,.98);box-shadow:0 24px 80px rgba(0,0,0,.42);padding:10px;display:none}
    .request-notifier.open .request-tray{display:grid;gap:9px}
    .request-item{display:grid;gap:8px;border:1px solid var(--line);border-radius:14px;background:rgba(255,255,255,.035);padding:11px}
    .request-item strong{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
    .request-meta{display:flex;gap:7px;flex-wrap:wrap;color:var(--muted);font-size:.78rem;font-weight:800}
    .request-estimate{color:var(--muted);font-size:.78rem;line-height:1.25}
    .request-actions{display:flex;gap:8px}
    .request-actions button{min-height:32px;border-radius:999px;padding:6px 10px;border:1px solid var(--line);font-weight:900;cursor:pointer}
    .request-actions button:first-child{background:rgba(101,214,164,.15);border-color:rgba(101,214,164,.36);color:#b8f4d6}
    .request-actions button:last-child{background:rgba(255,107,107,.10);border-color:rgba(255,107,107,.30);color:#ffb8b8}
    .request-actions button:disabled{opacity:.55;cursor:progress}
    @media(max-width:1100px){.request-notifier{order:4}.request-tray{right:auto;left:0}}
    @media(max-width:700px){.request-notifier{display:none}}
  `;
  document.head.appendChild(style);

  const esc = value => String(value == null ? '' : value).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
  const formatBytes = value => {
    const n = Number(value || 0);
    if(!n) return null;
    const units = ['B','KB','MB','GB','TB'];
    let size = n;
    let idx = 0;
    while(size >= 1000 && idx < units.length - 1){ size /= 1000; idx++; }
    return `${size.toFixed(idx < 2 ? 0 : 1)} ${units[idx]}`;
  };

  let items = [];
  let busy = new Set();
  let timer = null;
  let open = false;

  function estimateText(item){
    const estimate = item.estimate || {};
    const size = formatBytes(estimate.estimate_bytes);
    const fulfilled = formatBytes(item.fulfilled_bytes);
    const bits = [];
    if(fulfilled) bits.push(`Fulfilled ${fulfilled}`);
    if(size) bits.push(`Est. ${size}`);
    if(estimate.quality) bits.push(estimate.quality);
    if(estimate.source) bits.push(estimate.source);
    return bits.join(' · ');
  }

  function render(){
    root.hidden = false;
    root.classList.toggle('open', open);
    const count = items.length;
    const rows = items.map(item => {
      const seasonText = item.seasons && item.seasons.length ? `S${item.seasons.join(', S')}` : '';
      const meta = [item.type, seasonText, item.requester && item.requester.name, item.status].filter(Boolean).map(esc).join(' · ');
      const estimate = estimateText(item);
      const disabled = busy.has(String(item.source_id)) ? ' disabled' : '';
      return `<section class="request-item" data-id="${esc(item.source_id)}">
        <div><strong>${esc(item.title)}</strong><div class="request-meta">${meta}</div></div>
        ${estimate ? `<div class="request-estimate">${esc(estimate)}</div>` : ''}
        <div class="request-actions"><button data-action="approve"${disabled}>Approve</button><button data-action="decline"${disabled}>Reject</button></div>
      </section>`;
    }).join('') || '<section class="request-item"><div class="request-meta">No pending Seerr approvals.</div></section>';
    root.innerHTML = `<button class="request-bell" type="button" data-empty="${count ? 'false' : 'true'}" aria-expanded="${open ? 'true' : 'false'}"><span>Requests</span><b>${count}</b></button><div class="request-tray">${rows}</div>`;
  }

  async function load(){
    try{
      const response = await fetch('/api/seerr/pending-requests', {cache:'no-store'});
      if(response.status === 401 || response.status === 403){ root.hidden = true; return; }
      const payload = await response.json();
      items = payload.requests || [];
      render();
    } catch(e){
      items = [];
      render();
    } finally {
      clearTimeout(timer);
      timer = setTimeout(load, document.hidden ? 60000 : 20000);
    }
  }

  async function act(id, action){
    busy.add(String(id));
    render();
    try{
      const response = await fetch(`/api/seerr/requests/${encodeURIComponent(id)}/${action}`, {method:'POST'});
      if(!response.ok) throw new Error(`HTTP ${response.status}`);
      items = items.filter(item => String(item.source_id) !== String(id));
    } catch(e) {
      window.console && window.console.warn && window.console.warn('Request action failed', e);
    } finally {
      busy.delete(String(id));
      render();
      load();
    }
  }

  root.addEventListener('click', event => {
    const bell = event.target.closest('.request-bell');
    if(bell){
      open = !open;
      render();
      return;
    }
    const button = event.target.closest('button[data-action]');
    const row = event.target.closest('.request-item');
    if(button && row && row.dataset.id){
      act(row.dataset.id, button.dataset.action);
    }
  });
  document.addEventListener('click', event => {
    if(open && !root.contains(event.target)){
      open = false;
      render();
    }
  });
  document.addEventListener('visibilitychange', () => { if(!document.hidden) load(); });
  render();
  load();
})();
