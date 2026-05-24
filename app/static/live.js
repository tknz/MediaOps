(function(){
  const page=document.querySelector('[data-live-page]');
  if(!page) return;

  const sessionsGrid=document.querySelector('[data-live-sessions]');
  const downloadsGrid=document.querySelector('[data-live-downloads]');
  const bandwidth=document.querySelector('[data-live-bandwidth]');
  const summary=document.querySelector('[data-live-summary]');
  const bars=document.querySelector('[data-live-bars]');
  const remaining=document.querySelector('[data-live-remaining]');
  const sync=document.querySelector('[data-live-sync]');
  const isAdmin=page.dataset.admin==='true';
  let timer=null;
  let inFlight=null;
  let lastSignature='';

  const text=value=>value == null ? '' : String(value);
  const esc=value=>text(value).replace(/[&<>"']/g, char=>({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[char]));
  const enc=value=>encodeURIComponent(text(value));
  const number=value=>Number.isFinite(Number(value)) ? Number(value) : 0;
  const pct=value=>Math.max(0, Math.min(100, number(value)));
  const gb=value=>value ? `${(number(value)/1000000000).toFixed(2)} GB` : '—';
  const transcodeLabel=count=>`${count} transcode${count===1 ? '' : 's'}`;

  function signature(payload){
    const sessions=(payload.sessions||[]).map(s=>[
      s.session_key,s.session_id,s.user,s.title,s.grandparent_title,s.state,s.view_offset,
      s.bandwidth,s.part_decision,s.transcode_decision,s.remote_public_address,s.machine_identifier
    ].join('|'));
    const downloads=(payload.downloads||[]).map(d=>[
      d.item_key,d.source,d.title,d.status,d.progress,d.message,d.timeleft
    ].join('|'));
    return sessions.concat(downloads).join('~');
  }

  function isEditing(){
    const active=document.activeElement;
    if(active && page.contains(active) && ['INPUT','TEXTAREA','SELECT'].includes(active.tagName)){
      return true;
    }
    if(page.querySelector('details.moderation-menu[open]')) return true;
    return [...page.querySelectorAll('form[data-live-terminate] input[name="reason"]')]
      .some(input=>input.value.trim().length>0);
  }

  function setSyncStatus(text, kind){
    if(!sync) return;
    sync.textContent=text;
    sync.classList.toggle('paused', kind==='paused');
    sync.classList.toggle('bad', kind==='bad');
  }

  function renderStats(payload){
    const stats=payload.stats||{};
    const ops=payload.ops||{};
    const mbps=number(stats.total_mbps);
    const active=Number(stats.active_sessions||0);
    const paused=Number(stats.paused_sessions||0);
    const transcodes=Number(stats.transcodes||0);
    const downloads=Number(ops.active_downloads||0);
    if(bandwidth) bandwidth.textContent=`${mbps.toFixed(1)} Mb/s`;
    if(summary) summary.textContent=`${active} streaming · ${paused} paused · ${transcodeLabel(transcodes)} · ${downloads} downloading`;
    if(remaining) remaining.textContent=`${ops.remaining_label||'0 B'} remaining`;
    if(bars){
      bars.innerHTML=(stats.bars||[]).map((h, index)=>
        `<i style="height:${pct(h)}%; animation-delay:-${index*0.19}s"></i>`
      ).join('');
    }
  }

  function streamTitle(s){
    return s.grandparent_title || s.title || 'Untitled';
  }

  function subhead(s){
    if(!s.grandparent_title) return '';
    return `<p class="subhead">${esc(s.parent_title||'')} · ${esc(s.title||'')}</p>`;
  }

  function isLocal(s){
    return s.local==='1' && s.secure==='1' && s.relayed==='0';
  }

  function stopForm(s){
    if(!isAdmin || !s.session_id) return '';
    return `<form method="post" action="/live/terminate" data-live-terminate><input type="hidden" name="session_id" value="${esc(s.session_id)}"><input name="reason" placeholder="Message if stopping" autocomplete="off"><button class="stop-button" type="submit">Stop</button></form>`;
  }

  function banOptions(s){
    if(!isAdmin) return '';
    const userPath=enc(s.user);
    const ip=s.remote_public_address||'';
    const ipLabel=s.isp||s.org||s.ptr||ip;
    const device=s.machine_identifier||'';
    const deviceLabel=s.product||s.player||s.device||device;
    return `<details class="moderation-menu"><summary>Ban options for ${esc(s.user)}</summary><div>
      ${ip ? `<form method="post" action="/users/${userPath}/blocks"><input type="hidden" name="block_type" value="ip"><input type="hidden" name="value" value="${esc(ip)}"><input type="hidden" name="label" value="${esc(ipLabel)}"><input type="hidden" name="message" value="The IP address you are connecting from is banned. Please contact your server administrator."><input type="hidden" name="return_to" value="/users/${userPath}?tab=bans"><button class="quiet-warn" type="submit">Ban this IP for ${esc(s.user)}</button></form>` : ''}
      ${device ? `<form method="post" action="/users/${userPath}/blocks"><input type="hidden" name="block_type" value="device"><input type="hidden" name="value" value="${esc(device)}"><input type="hidden" name="label" value="${esc(deviceLabel)}"><input type="hidden" name="message" value="The device you are connecting from is banned. Please contact your server administrator."><input type="hidden" name="return_to" value="/users/${userPath}?tab=bans"><button class="quiet-warn" type="submit">Ban this device</button></form>` : ''}
      <a class="quiet-link" href="/users/${userPath}?tab=bans">Open ${esc(s.user)} bans</a>
    </div></details>`;
  }

  function sessionCard(s){
    const progress=s.duration ? pct(number(s.view_offset)/number(s.duration)*100) : 0;
    const local=isLocal(s);
    const actions=isAdmin ? `<div class="stream-actions calm-actions">${stopForm(s)}${banOptions(s)}</div>` : '';
    const art=s.thumb ? ` style="background-image:url('/plex-image?path=${enc(s.thumb)}')"` : '';
    return `<article class="stream-card">
      <div class="art"${art}></div>
      <div class="stream-body">
        <p class="eyebrow">${esc(s.user)} · ${esc(s.state)}</p>
        <h2>${esc(streamTitle(s))}</h2>
        ${subhead(s)}
        <div class="progress"><span style="width:${progress}%"></span></div>
        <ul class="facts">
          <li><b>Client:</b> ${esc(s.product)} on ${esc(s.device)}</li>
          <li><b>Device ID:</b> ${esc(s.machine_identifier||'—')}</li>
          <li><b>Reach:</b> ${local ? 'Local' : 'Remote'}</li>
          ${local ? `<li><b>LAN IP:</b> ${esc(s.player_address||'—')}</li>` : ''}
          <li><b>Public IP:</b> ${esc(s.remote_public_address||'—')}</li>
          <li><b>PTR:</b> ${esc(s.ptr||'—')}</li>
          <li><b>ISP:</b> ${esc(s.isp||s.org||'Private network')}</li>
          <li><b>Bandwidth:</b> ${s.state==='paused' ? 'Paused - not counted' : `${esc(s.bandwidth)} kbps`}</li>
          <li><b>Decision:</b> ${esc(s.part_decision)} / ${esc(s.transcode_decision)}</li>
          <li><b>Media:</b> ${esc(s.resolution)} ${esc(s.video_codec)} · ${esc(s.container)}</li>
        </ul>
        ${actions}
        <details><summary>More</summary><dl>
          <dt>Library</dt><dd>${esc(s.library)}</dd>
          <dt>Client</dt><dd>${esc(s.product)} ${esc(s.version)} · ${esc(s.platform)} ${esc(s.platform_version)}</dd>
          <dt>Local IP</dt><dd>${esc(s.player_address||'—')}</dd>
          <dt>Public IP</dt><dd>${esc(s.remote_public_address||'—')}</dd>
          <dt>Security</dt><dd>local=${esc(s.local)} secure=${esc(s.secure)} relayed=${esc(s.relayed)}</dd>
          <dt>Audio</dt><dd>${esc(s.audio_stream_title)}</dd>
          <dt>File</dt><dd>${esc(s.file)}</dd>
          <dt>Size</dt><dd>${esc(gb(s.file_size))}</dd>
          <dt>Session</dt><dd>${esc(s.session_key)} / ${esc(s.session_id)}</dd>
        </dl></details>
      </div>
    </article>`;
  }

  function downloadCard(item){
    return `<article class="ops-card">
      <div><p class="eyebrow">${esc(item.source)} · ${esc(item.status)}</p><h3>${esc(item.title)}</h3>${item.message ? `<small>${esc(item.message)}</small>` : ''}</div>
      <div class="progress"><span style="width:${pct(item.progress)}%"></span></div>
      <p class="ops-meta">${esc(item.quality||'—')} · ${esc(item.indexer||'—')} · ${esc(item.timeleft||'—')}${item.size_gb ? ` · ${esc(item.size_gb)} GB` : ''}</p>
    </article>`;
  }

  function renderPayload(payload){
    renderStats(payload);
    if(isEditing()){
      setSyncStatus('Live updates paused while editing', 'paused');
      return;
    }
    const nextSignature=signature(payload);
    if(nextSignature===lastSignature){
      setSyncStatus('Live updates active', 'ok');
      return;
    }
    lastSignature=nextSignature;
    if(sessionsGrid){
      const sessions=payload.sessions||[];
      sessionsGrid.innerHTML=sessions.length ? sessions.map(sessionCard).join('') : '<p>Nothing playing right now.</p>';
    }
    if(downloadsGrid){
      const downloads=payload.downloads||[];
      downloadsGrid.innerHTML=downloads.length ? downloads.map(downloadCard).join('') : '<p class="muted">No active downloads or processing jobs.</p>';
    }
    setSyncStatus('Live updates active', 'ok');
  }

  function schedule(delay){
    clearTimeout(timer);
    timer=setTimeout(tick, delay);
  }

  async function tick(){
    clearTimeout(timer);
    if(inFlight) inFlight.abort();
    const controller=new AbortController();
    inFlight=controller;
    try{
      const response=await fetch('/api/live', {cache:'no-store', signal:controller.signal});
      if(!response.ok) throw new Error(`Live refresh failed: ${response.status}`);
      renderPayload(await response.json());
    } catch(error){
      if(!controller.signal.aborted) setSyncStatus('Live updates unavailable', 'bad');
    } finally {
      if(inFlight===controller){
        inFlight=null;
        schedule(document.hidden ? 30000 : 10000);
      }
    }
  }

  document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
  page.addEventListener('focusout', ()=>setTimeout(tick, 150));
  page.addEventListener('toggle', event=>{ if(event.target.matches('details')) setTimeout(tick, 150); }, true);
  window.addEventListener('online', tick);
  tick();
})();
