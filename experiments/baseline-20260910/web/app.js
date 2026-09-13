/* Token content is rendered only through textContent. No HTML from the feed. */
const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
const pct = n => n == null ? '—' : `${(n*100).toFixed(1)}%`;
const price = n => n == null ? '—' : '$' + Number(n).toPrecision(5);
const stamp = n => n == null ? '—' : new Date(n*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'});
const human = text => String(text ?? '').replaceAll('_',' ');
let latest = null, chartPoints = [];
function set(id, text, sign) { const el=$(id);el.textContent=text;el.classList.remove('positive','negative');if(sign!=null)el.classList.add(sign>=0?'positive':'negative'); }
function node(tag, text, cls) {const el=document.createElement(tag);if(text!=null)el.textContent=text;if(cls)el.className=cls;return el;}
function definition(id, rows) {$(id).replaceChildren(...rows.map(([label,value])=>{const div=node('div');div.append(node('dt',label),node('dd',value));return div;}));}
function tokenCell(item) {const td=node('td');const a=node('a',item.symbol);if(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/.test(item.mint)){a.href='https://pump.fun/coin/'+encodeURIComponent(item.mint);a.target='_blank';a.rel='noopener noreferrer';}td.append(a,node('small',item.mint.slice(0,7)+'…'+item.mint.slice(-5)));return td;}
function cell(text, sign) {return node('td',text,sign==null?'':sign>=0?'positive':'negative');}
function table(id, rows, columns, empty) {if(rows.length)$(id).replaceChildren(...rows);else{const tr=node('tr');const td=node('td',empty,'empty');td.colSpan=columns;tr.append(td);$(id).replaceChildren(tr);}}
function renderTrades() {if(!latest)return;const term=$('search').value.trim().toLowerCase();const data=latest.trades.filter(t=>(t.symbol+' '+t.mint).toLowerCase().includes(term));table('trade-body',data.map(t=>{const tr=node('tr');tr.append(tokenCell(t),cell(stamp(t.entry_at)),cell(money(t.entry_market_cap_usd)),cell(price(t.entry_price_usd)),cell(price(t.exit_price_usd)),cell(money(t.pnl_usd),t.pnl_usd),cell(pct(t.return_fraction),t.return_fraction),cell(`${Math.floor(t.hold_seconds/60)}m ${Math.round(t.hold_seconds%60)}s`),cell(human(t.exit_reason)));return tr;}),9,'No closed trades yet. Entries and exits appear here as the experiment runs.');}
function render(d) {
  latest=d;const demo=d.source==='synthetic_demo';const age=d.last_at==null?Infinity:d.server_at-d.last_at;const stale=age>Math.max(20,d.config.poll_seconds*3);
  set('subtitle',`${demo?'Synthetic demonstration':'Live market observations'} · $10k–$50k entry market cap · ${money(d.config.position_usd)} virtual positions`);
  set('feed-status',demo?'Demo recording':stale?'Collector is not updating':d.source_status==='ok'?'Receiving observations':'Feed degraded');
  $('feed-dot').classList.toggle('stale',!demo&&(stale||d.source_status!=='ok'));
  set('last-update',d.last_at?'Last observation '+stamp(d.last_at):'Waiting for the first collection');
  const impaired=d.positions.filter(p=>p.mark_reason).length;
  let note=demo?'SYNTHETIC DEMO — fabricated prices test the software. These results say nothing about market profitability.':
    'Paper estimates use a sampled public feed, approximate pool quotes, configured fees and execution stress assumptions.';
  if(!demo&&stale)note='COLLECTOR STALE — portfolio values are as of the last observation. New fills are not being recorded.';
  if(impaired)note+=` ${impaired} position(s) have no usable exit quote and are conservatively marked at $0.`;
  if(d.halted)note+=` New entries halted: ${human(d.halt_reason)}. Exit management continues when the collector runs.`;
  set('notice',note);$('notice').classList.toggle('warning',demo||stale||impaired>0||d.halted);
  set('equity',money(d.equity_usd));set('cash',`${money(d.cash_usd)} cash · ${money(d.config.virtual_balance_usd)} starting balance`);
  set('pnl',money(d.total_pnl_usd),d.total_pnl_usd);set('realized',`${money(d.realized_net_pnl_usd)} realized · ${money(d.unrealized_pnl_usd)} unrealized`);
  set('wins',`${d.closed_trades} / ${pct(d.win_rate)}`);set('expectancy',`${money(d.mean_trade_pnl_usd)} mean closed-trade P&L`);
  set('costs',money(d.modeled_fees_usd+d.operating_cost_usd));set('failures',`${d.failed_attempts} modeled failed attempts · includes ${money(d.operating_cost_usd)} overhead`);
  set('drawdown',`Max drawdown ${pct(d.max_drawdown_fraction)}`);set('version',d.config_hash);
  definition('rules',[['Entry range',`${money(d.config.market_cap_min_usd)}–${money(d.config.market_cap_max_usd)}`],['Observation window',`${d.config.observation_seconds}s`],['Net profit / loss triggers',`+${pct(d.config.take_profit_fraction)} / ${pct(d.config.stop_loss_fraction)}`],['Maximum hold',`${d.config.max_hold_seconds/60} minutes`],['Concurrent positions',d.config.max_positions],['Fee / adverse fill per leg',`${(d.config.protocol_fee_bps+d.config.route_fee_bps)/100}% / ${d.config.adverse_fill_bps/100}%`]]);
  set('position-count',d.positions.length);set('pending-count',`${Object.keys(d.pending_entries).length} pending entries`);
  table('open-body',d.positions.map(p=>{const tr=node('tr');tr.append(tokenCell(p),cell(stamp(p.entry_at)),cell(price(p.entry_price_usd)),cell(money(p.cost_usd)),cell(money(p.mark_value_usd)),cell(money(p.unrealized_pnl_usd),p.unrealized_pnl_usd),node('td',human(p.mark_reason||p.exit_reason||p.status),'status'));return tr;}),7,'No positions yet. The strategy waits for eligible observations.');
  renderTrades();
  $('bands').replaceChildren(...d.bands.map(b=>{const el=node('div');el.append(node('label',b.range),node('strong',money(b.pnl_usd),b.pnl_usd>=0?'positive':'negative'),node('small',`${b.trades} closed trades`));return el;}));
  set('concentration',money(d.without_largest_winner_usd),d.without_largest_winner_usd);
  set('source',demo?'DEMO':'Public index');definition('collection',[['Recorded observations',d.observations.toLocaleString()],['Discovered tokens',d.discovered_count.toLocaleString()],['Collection cycles',d.batches.toLocaleString()],['Price source','Indexed bonding curve snapshots'],['Failure stress assumption',pct(d.config.failed_attempt_probability)],['Experiment source',human(d.source)]]);
  $('issues').textContent=d.issues.slice(0,4).join(' · ')||'No errors in the last collection. Coverage is a bounded sample, not every launch.';
  table('candidate-body',d.candidates.map(c=>{const tr=node('tr');tr.append(tokenCell(c),cell(money(c.market_cap_usd)),cell(money(c.real_liquidity_usd)),cell(pct(c.features?.momentum_60s)),cell(pct(c.features?.momentum_15s)),node('td',human(c.reason),'status'),cell(stamp(c.at)));return tr;}),7,'Discovering tokens and building observation windows…');
  drawChart();
}
function drawChart() {
  if(!latest)return;const data=latest.equity_curve,can=$('chart'),box=can.getBoundingClientRect(),ratio=window.devicePixelRatio||1;
  can.width=box.width*ratio;can.height=box.height*ratio;const ctx=can.getContext('2d');ctx.scale(ratio,ratio);const w=box.width,h=box.height,pad=10,right=68;ctx.clearRect(0,0,w,h);chartPoints=[];
  if(!data.length){ctx.fillStyle='#93a2b6';ctx.font='12px system-ui';ctx.fillText('The chart begins with your first observation.',20,h/2);return;}
  const vals=data.map(x=>x.value);let low=Math.min(...vals,latest.config.virtual_balance_usd),high=Math.max(...vals,latest.config.virtual_balance_usd);const margin=Math.max((high-low)*.15,.5);low-=margin;high+=margin;
  const first=data[0].at,last=data.at(-1).at;const x=t=>pad+(t-first)/Math.max(last-first,1)*(w-right-pad);const y=v=>h-15-(v-low)/(high-low)*(h-30);
  ctx.font='10px system-ui';for(let i=0;i<=3;i++){const v=low+(high-low)*i/3,py=y(v);ctx.strokeStyle='#27313e';ctx.beginPath();ctx.moveTo(pad,py);ctx.lineTo(w-right+5,py);ctx.stroke();ctx.fillStyle='#93a2b6';ctx.fillText(money(v),w-right+10,py+3);}
  ctx.setLineDash([3,5]);ctx.strokeStyle='#526375';ctx.beginPath();ctx.moveTo(pad,y(latest.config.virtual_balance_usd));ctx.lineTo(w-right,y(latest.config.virtual_balance_usd));ctx.stroke();ctx.setLineDash([]);
  chartPoints=data.map(v=>({x:x(v.at),y:y(v.value),...v}));const gradient=ctx.createLinearGradient(0,0,0,h);gradient.addColorStop(0,'#7ce3b12b');gradient.addColorStop(1,'#7ce3b100');ctx.fillStyle=gradient;ctx.beginPath();ctx.moveTo(chartPoints[0].x,h);for(const p of chartPoints)ctx.lineTo(p.x,p.y);ctx.lineTo(chartPoints.at(-1).x,h);ctx.closePath();ctx.fill();ctx.strokeStyle='#7ce3b1';ctx.lineWidth=2;ctx.beginPath();chartPoints.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.stroke();set('chart-range',`${stamp(first)} — ${stamp(last)}`);
}
$('chart').addEventListener('mousemove',e=>{if(!chartPoints.length)return;const x=e.offsetX,p=chartPoints.reduce((a,b)=>Math.abs(b.x-x)<Math.abs(a.x-x)?b:a);const tip=$('chart-tooltip');tip.hidden=false;tip.textContent=`${stamp(p.at)} · ${money(p.value)}${p.impaired?' · impaired position':''}`;});
$('chart').addEventListener('mouseleave',()=>{$('chart-tooltip').hidden=true;});
$('search').addEventListener('input',renderTrades);window.addEventListener('resize',drawChart);
async function refresh(){try{const response=await fetch('/api/state',{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);render(await response.json());}catch(error){set('feed-status','Dashboard disconnected');$('feed-dot').classList.add('stale');set('notice','Connection lost. Displayed values are from the last successful update.');$('notice').classList.add('warning');}finally{setTimeout(refresh,3000);}}
refresh();
