(function () {
  const root = document.getElementById('librariesApp');
  const boot = document.getElementById('librariesBoot');
  if (!root || !boot) return;

  const api = window.MediaOpsApi;
  let state = JSON.parse(boot.textContent || '{}');
  let busyAction = null;

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;',
  }[char]));

  const typeLabel = (item) => (item && item.kind === 'series' ? 'TV' : 'Movie');
  const itemHref = (item) => `/libraries${api.query({ q: item.title, kind: item.kind, source: item.source })}`;

  function actionPayload(item) {
    return {
      kind: item.kind,
      source_index: item.source_index,
      item_id: item.id,
    };
  }

  function hiddenInputs(payload) {
    return Object.entries(payload).map(([name, value]) => (
      `<input type="hidden" name="${esc(name)}" value="${esc(value)}">`
    )).join('');
  }

  function submitForm(path, payload) {
    const form = document.createElement('form');
    form.method = 'post';
    form.action = path;
    form.innerHTML = hiddenInputs({ ...payload, return_to: window.location.pathname + window.location.search });
    document.body.appendChild(form);
    form.submit();
  }

  async function callAction(path, payload, fallbackPath) {
    try {
      await api.post(path, payload);
      await loadData(state.filters, { keepFallback: true });
    } catch (error) {
      if (/404|405|Request failed/.test(error.message)) {
        submitForm(fallbackPath, payload);
        return;
      }
      showNotice(error.message || 'Action failed', 'bad');
    }
  }

  function actionButtons(item, label) {
    if (!state.user || !state.user.is_admin) return '';
    const key = `${item.kind}:${item.source_index}:${item.id}`;
    const disabled = busyAction === key ? ' disabled' : '';
    const monitorLabel = item.monitored ? 'Stop monitoring' : 'Start monitoring';
    return `
      <div class="row-actions">
        <button class="mini ${item.monitored ? 'mini-warn' : ''}" type="button" data-action="monitor" data-key="${esc(key)}"${disabled}>${monitorLabel}</button>
        <button class="mini danger-mini" type="button" data-action="delete" data-key="${esc(key)}"${disabled}>${esc(label || 'Delete')}</button>
      </div>
    `;
  }

  function findItem(key) {
    const pools = [
      ...(state.items || []),
      ...(((state.stats || {}).never_watched) || []),
      ...(((state.stats || {}).stale_large) || []),
      ...(((state.stats || {}).recently_watched) || []),
      ...(((state.stats || {}).movie_top) || []),
      ...(((state.stats || {}).series_top) || []),
      ...((state.selected_detail && state.selected_detail.item) ? [state.selected_detail.item] : []),
    ];
    return pools.find((item) => `${item.kind}:${item.source_index}:${item.id}` === key);
  }

  function mediaCard(item, reason) {
    return `
      <article class="media-decision-card">
        <a class="media-card-main" href="${esc(itemHref(item))}">
          ${item.poster ? `<img src="${esc(item.poster)}" alt="">` : '<i></i>'}
          <span><strong>${esc(item.title)}</strong><small>${typeLabel(item)} · ${esc(item.source)}</small></span>
        </a>
        <div class="media-card-stats">
          <b>${esc(item.size_label)}</b>
          <span>${esc(item.last_watched_label)}</span>
          <span>${esc(item.plays)} plays · ${esc(item.watch_hours)}h</span>
        </div>
        ${reason ? `<p>${esc(reason)}</p>` : ''}
        ${actionButtons(item)}
      </article>
    `;
  }

  function decisionList(items, empty, metric) {
    if (!items || !items.length) return `<p class="muted">${esc(empty)}</p>`;
    return items.map((item) => `
      <a href="${esc(itemHref(item))}">
        <span>${esc(item.title)}</span>
        <b>${esc(metric(item))}</b>
        <small>${esc(item.size_label)} · ${esc(item.last_watched_label)} · ${esc(item.plays)} plays</small>
      </a>
    `).join('');
  }

  function sizeList(items, empty, kind) {
    if (!items || !items.length) return `<p class="muted">${esc(empty)}</p>`;
    return items.map((item) => `
      <a href="${esc(`/libraries${api.query({ q: item.title, kind })}`)}">
        <span>${esc(item.title)}</span>
        <b>${esc(item.size_label)}</b>
        <small>${esc(item.last_watched_label)} · ${esc(item.plays)} plays</small>
      </a>
    `).join('');
  }

  function filterForm(title, compact) {
    const filters = state.filters || {};
    const sources = state.sources || [];
    return `
      <section class="panel ${compact ? 'compact-search-panel' : 'media-manager-panel'}">
        <div class="section-head"><div><p class="eyebrow">${compact ? 'Find another title' : 'Media manager'}</p><h2>${esc(title)}</h2></div></div>
        <form class="filters library-search" data-role="library-search">
          <label>Search<input name="q" value="${esc(filters.q || '')}" placeholder="Title or path"></label>
          <label>Type<select name="kind">
            <option value="all"${filters.kind === 'all' ? ' selected' : ''}>All</option>
            <option value="movie"${filters.kind === 'movie' ? ' selected' : ''}>Movies</option>
            <option value="series"${filters.kind === 'series' ? ' selected' : ''}>TV</option>
          </select></label>
          <label>Source<select name="source">
            <option value="all"${filters.source === 'all' ? ' selected' : ''}>All sources</option>
            ${sources.map((source) => `<option value="${esc(source)}"${filters.source === source ? ' selected' : ''}>${esc(source)}</option>`).join('')}
          </select></label>
          <button class="mini" type="submit">Search</button>
          ${(filters.q || filters.kind !== 'all' || filters.source !== 'all') ? '<button class="mini" type="button" data-action="clear-filters">Clear</button>' : ''}
        </form>
        ${compact ? '' : mediaTable()}
      </section>
    `;
  }

  function mediaTable() {
    const items = state.items || [];
    const rows = items.map((item) => `
      <tr>
        <td><div class="media-title">${item.poster ? `<img src="${esc(item.poster)}" alt="">` : ''}<span><strong>${esc(item.title)}</strong>${item.year ? `<small>${esc(item.year)}</small>` : ''}</span></div></td>
        <td>${typeLabel(item)}</td>
        <td>${esc(item.source)}</td>
        <td>${esc(item.size_label)}</td>
        <td>${esc(item.last_watched_label)}<small>${esc(item.plays)} plays · ${esc(item.watch_hours)}h</small></td>
        <td>${item.monitored ? 'Monitored' : '<span class="muted-cell">Not monitored</span>'}${item.quality ? `<small>${esc(item.quality)}</small>` : ''}</td>
        <td class="path-cell">${esc(item.path || '-')}</td>
        <td>${actionButtons(item)}</td>
      </tr>
    `).join('');
    return `
      <table class="media-table">
        <thead><tr><th>Title</th><th>Type</th><th>Source</th><th>Size</th><th>Watched</th><th>Status</th><th>Path</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8">No media found.</td></tr>'}</tbody>
      </table>
    `;
  }

  function weeklyChart(chart) {
    const data = chart || { points: [], max: 1, half: 0.5, weeks: 26 };
    const points = data.points || [];
    const slot = 860 / (points.length || 1);
    const bars = points.map((point, index) => {
      const x = 78 + index * slot;
      const width = Math.max(slot - 5, 5);
      const height = data.max ? Math.round((point.hours / data.max) * 200) : 0;
      const tick = index % 3 === 0 || index === points.length - 1 ? `<text x="${x}" y="280" class="tick" transform="rotate(32 ${x} 280)">${esc(point.label)}</text>` : '';
      return `<rect x="${x}" y="${250 - height}" width="${width}" height="${height}" rx="3" class="useful-bar"><title>${esc(point.label)} · ${esc(point.hours)}h</title></rect>${tick}`;
    }).join('');
    const top = data.max < 10 ? Number(data.max).toFixed(1) : Math.round(data.max);
    const half = data.half < 10 ? Number(data.half).toFixed(1) : Math.round(data.half);
    return `
      <section class="panel useful-chart-panel">
        <div class="section-head"><div><p class="eyebrow">Demand</p><h2>Weekly watch hours</h2><p class="muted">Hours</p></div><span class="muted">last ${esc(data.weeks)} weeks</span></div>
        <div class="useful-svg-wrap">
          <svg viewBox="0 0 980 320" role="img" aria-label="Weekly watch hours">
            <line x1="70" y1="250" x2="946" y2="250" class="axis"/><line x1="70" y1="150" x2="946" y2="150" class="gridline"/><line x1="70" y1="50" x2="946" y2="50" class="gridline"/>
            <text x="16" y="254" class="scale">0h</text><text x="16" y="154" class="scale">${half}h</text><text x="16" y="54" class="scale">${top}h</text>${bars}
          </svg>
        </div>
      </section>
    `;
  }

  function renderSelected(detail) {
    const item = detail.item;
    const episodesTitle = item.kind === 'series' ? 'Episodes' : 'Sessions';
    return `
      <section class="selected-media-hero">
        ${item.poster ? `<img src="${esc(item.poster)}" alt="">` : '<i></i>'}
        <div>
          <p class="eyebrow">${item.kind === 'series' ? 'TV show' : 'Movie'} · ${esc(item.source)}</p>
          <h1>${esc(item.title)}</h1>
          <p class="muted">${esc(item.size_label)} on disk · ${esc(item.quality || 'No file status')} · ${item.monitored ? 'Monitored' : 'Not monitored'}</p>
          <div class="selected-actions"><a class="mini" href="/libraries">Back to Libraries</a>${actionButtons(item, 'Delete from disk')}</div>
        </div>
      </section>
      <section class="stats library-stats selected-kpis">
        <article><span>Plays</span><b>${esc(detail.plays)}</b><small>${esc(detail.watch_hours)}h watched</small></article>
        <article><span>Last watched</span><b>${esc(detail.last_watched_label)}</b><small>${esc(detail.last_watched_at || 'No Postgres history')}</small></article>
        <article><span>Streamed</span><b>${esc(detail.streamed_bytes_label)}</b><small>${esc(detail.transcodes)} transcodes · ${esc(detail.remote_plays)} remote</small></article>
        <article><span>Audience</span><b>${esc(detail.users)}</b><small>${esc(detail.devices)} devices</small></article>
      </section>
      <section class="grid two selected-detail-grid">
        ${weeklyChart(detail.weekly_chart)}
        <div class="panel selected-facts"><div class="section-head"><div><p class="eyebrow">Storage</p><h2>Managed item</h2></div></div>
          <dl>
            <dt>Type</dt><dd>${item.kind === 'series' ? 'TV show' : 'Movie'}</dd>
            <dt>Size</dt><dd>${esc(item.size_label)}</dd>
            <dt>Source</dt><dd>${esc(item.source)}</dd>
            <dt>Status</dt><dd>${item.monitored ? 'Monitored' : 'Not monitored'}</dd>
            <dt>Files</dt><dd>${esc(item.quality || '-')}</dd>
            ${item.seasons ? `<dt>Seasons</dt><dd>${esc(item.seasons)}</dd>` : ''}
            <dt>Path</dt><dd>${esc(item.path || '-')}</dd>
          </dl>
        </div>
      </section>
      <section class="selected-breakdown">
        ${breakdownPanel('Watchers', 'Users', detail.user_rows, (row) => `<a href="/users/${encodeURIComponent(row.username)}"><span>${esc(row.username)}</span><b>${esc(row.hours)}h</b><small>${esc(row.plays)} plays · ${esc(row.last || 'Never')}</small></a>`, 'No user history for this title yet.')}
        ${breakdownPanel('Players', 'Devices', detail.device_rows, (row) => `<a><span>${esc(row.device)}</span><b>${esc(row.plays)}</b><small>${esc(row.last || 'Never')}</small></a>`, 'No device history for this title yet.')}
        ${breakdownPanel(episodesTitle, 'Recent activity', detail.episode_rows, episodeRow, 'No episode history for this title yet.')}
      </section>
      ${historyPanel(detail.history_rows)}
      ${filterForm('Search library', true)}
    `;
  }

  function episodeRow(row) {
    const prefix = row.parent_media_index || row.media_index ? `S${String(row.parent_media_index || 0).padStart(2, '0')}E${String(row.media_index || 0).padStart(2, '0')} · ` : '';
    return `<a><span>${esc(prefix)}${esc(row.title)}</span><b>${esc(row.plays)}</b><small>${esc(row.hours)}h · ${esc(row.last || 'Never')}</small></a>`;
  }

  function breakdownPanel(eyebrow, title, rows, renderer, empty) {
    return `
      <div class="panel">
        <div class="section-head"><div><p class="eyebrow">${esc(eyebrow)}</p><h2>${esc(title)}</h2></div></div>
        <div class="decision-list">${rows && rows.length ? rows.map(renderer).join('') : `<p class="muted">${esc(empty)}</p>`}</div>
      </div>
    `;
  }

  function historyPanel(rows) {
    const body = (rows || []).map((row) => `
      <tr><td><b>${esc(row.when_date)}</b><small>${esc(row.when_time)}</small></td><td>${esc(row.user)}</td><td class="title-cell">${esc(row.title)}</td><td>${esc(row.watched_minutes)} min</td><td>${esc(row.reach)}</td><td><span class="decision-pill ${row.decision === 'transcode' ? 'danger-pill' : ''}">${esc(row.decision)}</span></td><td>${esc(row.player)}</td></tr>
    `).join('');
    return `
      <section class="panel history-panel">
        <div class="section-head"><div><p class="eyebrow">Postgres watch ledger</p><h2>Watch history</h2></div><span class="muted">${esc((rows || []).length)} shown</span></div>
        ${body ? `<table class="history-table"><thead><tr><th>When</th><th>User</th><th>Title</th><th>Watched</th><th>Reach</th><th>Decision</th><th>Player</th></tr></thead><tbody>${body}</tbody></table>` : '<div class="empty-state"><h2>No watch history found</h2><p class="muted">Postgres has no matching sessions yet.</p><a class="button" href="/settings">Settings</a></div>'}
      </section>
    `;
  }

  function renderDashboard() {
    const stats = state.stats || {};
    const libs = state.libraries || [];
    const errors = state.errors || [];
    return `
      <section class="cinema-hero library-hero"><div><p class="eyebrow">${esc(state.server_label || 'Media server')}</p><h1>Libraries</h1><p class="muted">Storage, watch history, and cleanup from media services.</p></div></section>
      <section class="stats library-stats">
        <article><span>Movies</span><b>${esc(stats.movies_count || 0)}</b><small>${esc(stats.movies_size_label || '0 B')}</small></article>
        <article><span>TV shows</span><b>${esc(stats.series_count || 0)}</b><small>${esc(stats.series_size_label || '0 B')}</small></article>
        <article><span>Total indexed</span><b>${esc(stats.total_size_label || '0 B')}</b><small>Radarr + Sonarr</small></article>
      </section>
      <section class="media-decision-board">
        <div class="panel cleanup-panel hero-cleanup"><div class="section-head"><div><p class="eyebrow">Cleanup candidates</p><h2>Big and unwatched</h2></div></div><div class="decision-grid primary">${(stats.never_watched || []).slice(0, 4).map((item) => mediaCard(item, 'No watches found.')).join('') || '<p class="muted">Nothing obvious here.</p>'}</div></div>
        <div class="panel cleanup-panel"><div class="section-head"><div><p class="eyebrow">Stale storage</p><h2>Large and not watched lately</h2></div></div><div class="decision-list">${decisionList(stats.stale_large, 'No stale large items.', (item) => item.size_label)}</div></div>
        <div class="panel cleanup-panel"><div class="section-head"><div><p class="eyebrow">Recently used</p><h2>Last watched</h2></div></div><div class="decision-list">${decisionList(stats.recently_watched, 'No watch data yet.', (item) => item.last_watched_label)}</div></div>
      </section>
      <section class="grid two library-top-grid compact-storage-grid">
        <div class="panel"><div class="section-head"><div><p class="eyebrow">Largest movies</p><h2>Movies by size</h2></div></div><div class="size-list">${sizeList(stats.movie_top, 'No movie data.', 'movie')}</div></div>
        <div class="panel"><div class="section-head"><div><p class="eyebrow">Largest shows</p><h2>TV by size</h2></div></div><div class="size-list">${sizeList(stats.series_top, 'No TV data.', 'series')}</div></div>
      </section>
      ${libs.length ? `<section class="panel plex-section-panel"><div class="section-head"><div><p class="eyebrow">Plex sections</p><h2>Libraries</h2></div></div><div class="plex-section-row">${libs.map((lib) => `<a href="/libraries/${encodeURIComponent(lib.key)}"><b>${esc(lib.title)}</b><span>${esc(lib.count)} items · ${esc(lib.type)}</span></a>`).join('')}</div></section>` : ''}
      ${filterForm('Search library', false)}
      ${errors.length ? '<section class="panel"><p class="muted">Some sources did not respond.</p></section>' : ''}
    `;
  }

  function showNotice(message, kind) {
    const notice = document.createElement('div');
    notice.className = `panel test-result ${kind || ''}`;
    notice.innerHTML = `<b>${esc(message)}</b>`;
    root.prepend(notice);
    setTimeout(() => notice.remove(), 5000);
  }

  function render() {
    root.innerHTML = state.selected_detail ? renderSelected(state.selected_detail) : renderDashboard();
  }

  function nextFilters(form) {
    const data = new FormData(form);
    return {
      q: String(data.get('q') || ''),
      kind: String(data.get('kind') || 'all'),
      source: String(data.get('source') || 'all'),
    };
  }

  async function loadData(filters, options) {
    const next = {
      q: filters && filters.q ? filters.q : '',
      kind: filters && filters.kind ? filters.kind : 'all',
      source: filters && filters.source ? filters.source : 'all',
    };
    try {
      state = await api.get('/api/libraries', next);
      state.filters = state.filters || next;
      window.history.replaceState(null, '', `/libraries${api.query(next)}`);
      render();
    } catch (error) {
      if (options && options.keepFallback) {
        render();
        return;
      }
      window.location.href = `/libraries${api.query(next)}`;
    }
  }

  root.addEventListener('submit', (event) => {
    const form = event.target.closest('[data-role="library-search"]');
    if (!form) return;
    event.preventDefault();
    loadData(nextFilters(form));
  });

  root.addEventListener('click', async (event) => {
    const button = event.target.closest('button[data-action]');
    if (!button) return;

    if (button.dataset.action === 'clear-filters') {
      await loadData({ q: '', kind: 'all', source: 'all' });
      return;
    }

    const item = findItem(button.dataset.key);
    if (!item) return;
    const key = `${item.kind}:${item.source_index}:${item.id}`;
    busyAction = key;
    render();

    if (button.dataset.action === 'monitor') {
      const monitored = !item.monitored;
      if (window.confirm(`${monitored ? 'Start monitoring' : 'Stop monitoring'} ${item.title}?`)) {
        await callAction('/api/libraries/manage/monitor', { ...actionPayload(item), monitored }, '/libraries/manage/monitor');
      }
    }

    if (button.dataset.action === 'delete') {
      if (window.confirm(`Delete ${item.title} from disk?`)) {
        await callAction('/api/libraries/manage/delete', { ...actionPayload(item), delete_files: true }, '/libraries/manage/delete');
      }
    }

    busyAction = null;
    render();
  });

  render();
  loadData(state.filters, { keepFallback: true });
})();
