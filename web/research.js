if (typeof window === 'undefined' || !window.PumpWizardMarket?.isPreview) {
const $=id=>document.getElementById(id),number=n=>new Intl.NumberFormat('en-US').format(n||0),money=n=>n==null?'—':new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:0}).format(n),when=t=>t?new Date(t*1000).toLocaleString():'—',esc=x=>String(x??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function windowLabel(seconds){return seconds>=86400?(seconds/86400)+'d':seconds>=3600?(seconds/3600)+'h':(seconds/60)+'m'}
const tokenUrl=mint=>'https://solscan.io/token/'+encodeURIComponent(mint||'');
let evidenceMarket='pumpfun',recentEvidenceEvents=[];
function renderEvidenceEvents(){
  const host=$('research-events');
  if(evidenceMarket==='raydium'){
    host.innerHTML='<tr><td colspan="5" class="empty">Raydium CPMM evidence is not published yet. Its ledger will remain separate from Pump.fun records.</td></tr>';
    return;
  }
  const events=recentEvidenceEvents;
  host.replaceChildren(...(events.length?events.map(event=>{
    const tr=document.createElement('tr'),strategyId=String(event.strategy_id||''),mint=String(event.mint||''),strategyUrl='/engine/'+encodeURIComponent(strategyId),token=mint?'<a class="research-token" href="'+tokenUrl(mint)+'" target="_blank" rel="noreferrer" title="'+esc(mint)+'">'+esc(mint.slice(0,8))+'… <span aria-hidden="true">↗</span></a>':'—';
    tr.className='research-event-row';tr.tabIndex=0;tr.setAttribute('role','link');tr.setAttribute('aria-label','Open strategy '+strategyId);
    tr.innerHTML='<td><time datetime="'+new Date((event.at||0)*1000).toISOString()+'">'+when(event.at)+'</time></td><td><a class="research-instance" href="'+strategyUrl+'">'+esc(strategyId)+' <span aria-hidden="true">→</span></a></td><td><span class="event-kind">'+esc(String(event.kind||'recorded').replaceAll('_',' '))+'</span></td><td>'+token+'</td><td>'+money(event.reference_cap)+'</td>';
    const open=target=>{if(!(target instanceof Element)||!target.closest('a'))location.assign(strategyUrl)};
    tr.addEventListener('click',event=>open(event.target));tr.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();location.assign(strategyUrl)}});return tr;
  }):[Object.assign(document.createElement('tr'),{innerHTML:'<td colspan="5" class="empty">No modeled fills recorded yet.</td>'})]));
}
function setEvidenceMarket(market){
  evidenceMarket=market==='raydium'?'raydium':'pumpfun';
  document.querySelectorAll('[data-evidence-market]').forEach(button=>{const active=button.dataset.evidenceMarket===evidenceMarket;button.classList.toggle('selected',active);button.setAttribute('aria-pressed',String(active));});
  $('events-title').textContent=evidenceMarket==='raydium'?'Raydium CPMM lifecycle markers':'Latest Pump.fun lifecycle markers';
  $('evidence-ledger-note').textContent=evidenceMarket==='raydium'?'Separate Raydium CPMM record · coming soon':'Open a row for its strategy · Token opens Solscan';
  renderEvidenceEvents();
}
function render(data){
  const r=data.research||{};
  $('tracked-coins').textContent=number(r.tracked_coins);$('minute-bars').textContent=number(r.minute_bars);$('lifecycle-events').textContent=number(r.lifecycle_events);$('outcome-coverage').textContent=number(r.outcomes_completed)+' / '+number(r.outcomes_scheduled);$('due-outcomes').textContent=number(r.outcome_mints_due);$('quality-summary').textContent=number(r.outcomes_late)+' late · '+number(r.outcomes_missed)+' missed so far';
  const windows=r.outcome_windows||[];
  $('outcome-windows').replaceChildren(...(windows.length?windows.map(item=>{const row=document.createElement('div');row.innerHTML='<b>'+windowLabel(item.window_seconds)+'</b><progress aria-label="'+windowLabel(item.window_seconds)+' on-time coverage" value="'+item.completed+'" max="'+Math.max(1,item.scheduled)+'"></progress><small>'+number(item.completed)+' / '+number(item.scheduled)+'<br>'+number(item.late)+' late · '+number(item.missed)+' missed</small>';return row}):[Object.assign(document.createElement('p'),{className:'empty',textContent:'Outcome windows will appear after the first fill.'})]));
  recentEvidenceEvents=r.recent_events||[];renderEvidenceEvents();
}
document.querySelectorAll('[data-evidence-market]').forEach(button=>button.addEventListener('click',()=>setEvidenceMarket(button.dataset.evidenceMarket)));
async function refresh(){try{render(await window.PumpWizardPublic.request('/api/public/research'))}catch{}finally{setTimeout(refresh,5000)}}refresh();
fetch('/api/public/packages').then(response => { if (!response.ok) throw new Error(); return response.json(); }).then(data => {
  const host = document.getElementById('replay-package-list');
  if (!data.packages.length) { host.replaceChildren(); return; }
  host.replaceChildren(...data.packages.map(item => {
    const row = document.createElement('p'), link = document.createElement('a'), details = document.createElement('small');
    link.href = item.url; link.textContent = item.instance + ' · Download replay (' + Math.ceil(item.bytes / 1024 / 1024) + ' MB)';
    details.textContent = ' · Frozen through ' + when(item.observed_through) + ' · SHA-256: ' + item.sha256;
    row.append(link, details); return row;
  }));
}).catch(() => { document.getElementById('replay-package-list').textContent = 'Replay archives are temporarily unavailable.'; });
}
