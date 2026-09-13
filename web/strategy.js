const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',maximumFractionDigits:2}).format(n);
const signed = n => (n >= 0 ? '+' : '−') + money(Math.abs(n));
const pct = n => n == null ? '—' : (n * 100).toFixed(1) + '%';
const signedPct = n => n == null ? '—' : (n >= 0 ? '+' : '−') + (Math.abs(n) * 100).toFixed(1) + '%';
const when = t => t ? new Date(t * 1000).toLocaleString() : '—';
const dateTime = t => t ? new Intl.DateTimeFormat(undefined,{year:'numeric',month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(t * 1000)) : '—';
const duration = seconds => !Number.isFinite(seconds) || seconds < 0 ? '—' : seconds < 3600 ? Math.floor(seconds / 60) + 'm' : seconds < 86400 ? Math.floor(seconds / 3600) + 'h ' + Math.floor(seconds % 3600 / 60) + 'm' : Math.floor(seconds / 86400) + 'd ' + Math.floor(seconds % 86400 / 3600) + 'h';
const capMove = (from,to) => from && to ? (to / from - 1) : null;
const readable = value => String(value || '—').replaceAll('_',' ');
const tokenUrl = mint => 'https://solscan.io/token/' + encodeURIComponent(mint || '');
const tokenLink = (mint, label, className = 'chain-link') => '<a class="' + className + '" href="' + tokenUrl(mint) + '" target="_blank" rel="noreferrer">' + label + ' ↗</a>';
const esc = x => String(x ?? '').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const id = (() => {
  const parts = location.pathname.split('/').filter(Boolean);
  const candidate = parts[0] === 'engine' ? parts[1] : new URLSearchParams(location.search).get('id');
  return /^[a-z0-9-]{1,40}$/.test(candidate || '') ? candidate : null;
})();
let fullTradeHistory = null, fullTradeHistoryCount = null, tradeHistoryLoading = false;
let analysisStrategy = null;
let historyPages = [], historyPageIndex = 0, analysisLoading = false;
const EXPLAINERS = {
  momentum: {title:'Follow early momentum',text:'This strategy enters only after repeated reserve-implied price changes, positive price growth, and real SOL reserve growth inside its observation window. It is a continuation test, not a prediction of a coin’s future value.'},
  breakout: {title:'Enter confirmed range breaks',text:'This strategy waits for an established observation range, then requires the latest quote to clear the earlier range high by a configured margin while broader price and reserve conditions remain positive.'},
  pullback_reclaim: {title:'Buy a recovered pullback',text:'This strategy looks for a meaningful local pullback after a prior peak, then waits for the price to reclaim from the local low. It tests whether recovered trends continue more reliably than first spikes.'},
  slow_burn: {title:'Back persistent survivors',text:'This strategy watches for a longer, steadier rise. It requires positive progress in both halves of its configured observation window, aiming to distinguish persistent activity from a single burst.'}
};
if(!document.querySelector('link[href="/agent-live.css?v=2"]')){const stylesheet=document.createElement('link');stylesheet.rel='stylesheet';stylesheet.href='/agent-live.css?v=2';document.head.append(stylesheet)}
if(!document.querySelector('link[href="/agent-share.css"]')){const stylesheet=document.createElement('link');stylesheet.rel='stylesheet';stylesheet.href='/agent-share.css';document.head.append(stylesheet)}
if(!document.querySelector('link[href="/agent-hero.css"]')){const stylesheet=document.createElement('link');stylesheet.rel='stylesheet';stylesheet.href='/agent-hero.css';document.head.append(stylesheet)}
const shortWhen=t=>{const seconds=Math.max(0,Math.floor(Date.now()/1000-(t||0)));return seconds<60?'just now':seconds<3600?Math.floor(seconds/60)+'m ago':seconds<86400?Math.floor(seconds/3600)+'h ago':Math.floor(seconds/86400)+'d ago'};
function agentLiveView(strategy){
  const report=strategy.report,candidates=report.candidates||[],events=report.events||[],ready=candidates.filter(item=>item.reason==='Entry scheduled').length;
  let panel=document.getElementById('agent-live');
  if(!panel){panel=document.createElement('section');panel.id='agent-live';panel.className='agent-live';document.querySelector('.detail-metrics').after(panel)}
  const state=report.pending_count?'An entry is being evaluated against available virtual cash and risk limits.':ready?'A candidate is qualifying for a simulated entry.':candidates.length?'The agent is screening current in-band candidates for its configured signal.':'The agent is observing the market for a new in-band candidate.';
  panel.innerHTML='<div class="agent-live-head"><p class="eyebrow">Live agent</p></div><div class="agent-live-grid"><article class="agent-state"><h3>Current attention</h3><p>'+esc(state)+'</p><div class="agent-counts"><div><b>'+candidates.length+'</b><span>Screened now</span></div><div><b>'+ready+'</b><span>Entry signals</span></div><div><b>'+(report.pending_count||0)+'</b><span>Pending</span></div></div><div class="agent-focus" id="agent-focus"></div></article><article class="agent-decisions"><p class="eyebrow">Decision trail</p><h3>Latest agent observations</h3><div class="agent-decisions-list" id="agent-decisions"></div></article></div>';
  const focus=panel.querySelector('#agent-focus');
  focus.innerHTML=candidates.length?candidates.slice(0,3).map(item=>{const name=esc(item.symbol||item.mint?.slice(0,8)||'Unknown coin'),link=item.mint?'<a class="agent-token-link" href="'+tokenUrl(item.mint)+'" target="_blank" rel="noreferrer">'+name+' ↗</a>':name;return '<div class="agent-focus-item"><div><b>'+link+'</b><small>'+money(item.market_cap_usd)+' market cap</small></div><span>'+esc(item.reason||'Under review')+'</span></div>'}).join(''):'<div class="agent-focus-item"><div><b>No in-band coin is held in attention</b><small>The agent continues to sample market observations.</small></div></div>';
  const decisions=panel.querySelector('#agent-decisions'),labels={signal_entry:'Entry signal spotted',fill_entry:'Position opened',fill_exit:'Position closed',fill_partial_exit:'Partial exit recorded'};
  decisions.innerHTML=events.length?events.slice(0,5).map(event=>{const payload=event.data&&typeof event.data==='object'?event.data:{},action=event.action||'observation',coin=payload.symbol||event.mint?.slice(0,8)||'Market observation',coinLink=event.mint?'<a class="agent-token-link" href="'+tokenUrl(event.mint)+'" target="_blank" rel="noreferrer">'+esc(coin)+' ↗</a>':esc(coin),detail=String(payload.reason||payload.status||payload.mark_reason||'Recorded from the strategy ledger');return '<div class="agent-decision '+esc(action)+'"><span class="agent-decision-mark">'+(action==='fill_entry'?'↗':action.includes('exit')?'✓':'◌')+'</span><div><b>'+esc(labels[action]||action.replaceAll('_',' '))+' · '+coinLink+'</b><small>'+esc(detail.replaceAll('_',' '))+'</small></div><time>'+shortWhen(event.at)+'</time></div>'}).join(''):'<div class="agent-decision"><span class="agent-decision-mark">◌</span><div><b>Awaiting first agent decision</b><small>Observations will appear here as this cohort evaluates coins.</small></div></div>';
}
function configurationHandoff(strategy){
  const report=strategy.report||{};
  return JSON.stringify({
    schema:'pumpwizard.strategy-handoff/v1',
    generated_at:new Date().toISOString(),
    mode:'simulation_only',
    strategy:{id:strategy.id,label:strategy.label,family:strategy.family||report.config?.entry_style,description:strategy.description||null},
    configuration:report.config||{},
    versioned_trial:strategy.trial||null,
    strategy_analysis:window.PumpWizardAnalysis.analyze(report, fullTradeHistory),
    public_telemetry:{
      unique_coins:report.unique_coins ?? null, fees_usd:report.fees_usd ?? null, failed_fees_usd:report.failed_fees_usd ?? null,
      closed_profit_factor:report.closed_profit_factor ?? null, trial_metrics:report.trial_metrics || {},
      started_at:report.started_at||null,last_observation_at:report.last_at||null,model:report.model||null,
      initial_usd:report.initial_usd,equity_usd:report.equity_usd,cash_usd:report.cash_usd,pnl_usd:report.pnl_usd,
      realized_net_pnl_usd:report.realized_net_pnl_usd,unrealized_pnl_usd:report.unrealized_pnl_usd,
      total_entries:report.total_entries,closed_count:report.closed_count,closed_wins:report.closed_wins,
      closed_win_rate:report.closed_win_rate,max_drawdown:report.max_drawdown??null,
      positions:report.positions||[],recent_trades:report.trades||[],recent_events:report.events||[],
      current_candidates:report.candidates||[]
    },
    public_endpoints:{full_closed_trade_history:location.origin+'/api/public/strategies/'+encodeURIComponent(strategy.id)+'/trades',strategy_page:location.href,telemetry_api:location.origin+'/api/public/strategies/'+encodeURIComponent(strategy.id),execution_history_csv:location.origin+'/api/public/history.csv'},
    limitations:[
      'Public market observations and modeled execution only; no wallet is connected and no order is sent.',
      'Recorded P&L can include open marked positions and is not a claim of future profitability.',
      'Validate data coverage, model assumptions, fees, delays and risk controls independently before using any configuration.'
    ]
  },null,2);
}
function agentShare(strategy){
  let controls=document.getElementById('agent-share');
  if(!controls){
    controls=document.createElement('div');
    controls.id='agent-share';
    controls.className='agent-share';
    controls.innerHTML='<button type="button" id="copy-agent-link">Copy page</button><a href="#trade-history">Trade history</a><span class="agent-share-note" id="agent-share-note"></span>';
    $('strategy-summary').after(controls);
    const note=$('agent-share-note');
    const copyText=async(text,message)=>{try{await navigator.clipboard.writeText(text)}catch{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();document.execCommand('copy');input.remove()}note.textContent=message;setTimeout(()=>{if(note.textContent===message)note.textContent=''},2400)};
    $('copy-agent-link').addEventListener('click',()=>copyText(location.href,'Link copied'));
  }
  controls._strategy=strategy;
}
function configurationControl(strategy){
  let controls=document.getElementById('strategy-configuration-control');
  if(!controls){
    controls=document.createElement('div');
    controls.id='strategy-configuration-control';
    controls.className='strategy-configuration-control';
    controls.innerHTML='<button type="button" id="copy-agent-config" title="Copies public configuration and technical telemetry as JSON">Copy configuration</button><span id="configuration-copy-note"></span>';
    $('started-at').after(controls);
    const note=$('configuration-copy-note');
    const copy=async()=>{const text=configurationHandoff(controls._strategy);try{await navigator.clipboard.writeText(text)}catch{const input=document.createElement('textarea');input.value=text;document.body.append(input);input.select();document.execCommand('copy');input.remove()}note.textContent='Configuration copied';setTimeout(()=>{if(note.textContent==='Configuration copied')note.textContent=''},2400)};
    $('copy-agent-config').addEventListener('click',copy);
    const promptButton = document.createElement('button');
    promptButton.type = 'button';
    promptButton.id = 'copy-agent-prompt';
    promptButton.textContent = 'Copy agent prompt';
    promptButton.title = 'Copy strategy rules, evidence and analysis instructions for your agent';
    $('copy-agent-config').before(promptButton);
    const downloadButton = document.createElement('button');
    downloadButton.type = 'button';
    downloadButton.id = 'download-research';
    downloadButton.textContent = 'Download research';
    downloadButton.title = 'Save configuration, analysis and complete closed-position history as JSON';
    note.setAttribute('aria-live', 'polite');
    note.before(downloadButton);
    const more = document.createElement('details');
    more.className = 'strategy-export-options';
    more.innerHTML = '<summary>More export options</summary>';
    note.before(more);
    more.append($('copy-agent-config'), downloadButton);
    const replay = document.createElement('div');
    replay.className = 'replay-package-options';
    replay.textContent = 'Checking verified replay archives…';
    more.append(replay);
    fetch('/api/public/packages?instance=' + encodeURIComponent(strategy.id))
      .then(response => { if (!response.ok) throw new Error(); return response.json(); })
      .then(data => {
        replay.replaceChildren();
        const latest = data.packages.sort((a,b) => b.observed_through-a.observed_through)[0];
        if (!latest) { replay.textContent = 'A verified replay archive has not been published for this instance yet.'; return; }
        const link = document.createElement('a');
        link.href = latest.url;
        link.textContent = 'Download verified replay · ' + Math.ceil(latest.bytes / 1024 / 1024) + ' MB';
        const note = document.createElement('small');
        note.textContent = 'Frozen history through ' + dateTime(latest.observed_through) + '. Includes source, inputs and separate future labels—not live trading instructions.';
        replay.append(link, note);
      }).catch(() => { replay.textContent = 'Replay archives are temporarily unavailable.'; });
    window.PumpWizardAnalysis.bindDownload(downloadButton, note, () => configurationHandoff(controls._strategy));
    promptButton.addEventListener('click', async () => {
      promptButton.disabled = true;
      note.textContent = 'Preparing analysis…';
      try {
        const handoff = await window.PumpWizardAnalysis.withHistory(configurationHandoff(controls._strategy));
        await window.PumpWizardHandoff.copy(window.PumpWizardHandoff.prompt(handoff));
        note.textContent = 'Prompt copied — paste into your agent';
      } catch { note.textContent = 'Could not prepare the prompt. Please try again.'; }
      finally { promptButton.disabled = false; }
    });
  }
  controls._strategy=strategy;
}
function empty(host,columns,message) {
  $(host).innerHTML = '<tr><td colspan="' + columns + '" class="empty">' + esc(message) + '</td></tr>';
}
function rules(host,entries) {
  $(host).replaceChildren(...entries.map(([label,value]) => {
    const item = document.createElement('div');
    item.innerHTML = '<dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd>';
    return item;
  }));
}
function openRow(position) {
  const tr = document.createElement('tr');
  const move = capMove(position.entry_cap,position.current_cap), pnlClass = position.pnl_usd >= 0 ? 'up' : 'down';
  const returnPct = position.cost_usd ? position.pnl_usd / position.cost_usd : null;
  tr.innerHTML = '<td class="ledger-asset"><a class="ledger-token" href="' + tokenUrl(position.mint) + '" target="_blank" rel="noreferrer"><b>' + esc(position.symbol) + ' ↗</b><small title="' + esc(position.mint) + '">' + esc(position.mint.slice(0,8)) + '…</small></a></td>' +
    '<td>' + tokenLink(position.mint,'<b>' + dateTime(position.entry_at) + '</b>','ledger-time') + '<small>Held ' + duration(Date.now() / 1000 - position.entry_at) + '</small></td>' +
    '<td><b>' + money(position.cost_usd) + ' stake</b><small>Marked ' + money(position.mark_value_usd) + '</small></td>' +
    '<td><b>' + money(position.entry_cap) + ' → ' + money(position.current_cap) + '</b><small class="' + (move >= 0 ? 'up' : 'down') + '">' + (move == null ? 'Awaiting mark' : signedPct(move) + ' market-cap move') + '</small></td>' +
    '<td><b class="' + pnlClass + '">' + signed(position.pnl_usd) + '</b><small class="' + pnlClass + '">' + (returnPct == null ? 'Return pending' : signedPct(returnPct) + ' return') + '</small></td>' +
    '<td><b>' + money(position.next_target) + '</b><small>Next target</small></td>' +
    '<td><span class="ledger-status">' + esc(readable(position.status)) + '</span><small>' + esc(readable(position.ownership_status)) + ' ownership</small>' + tokenLink(position.mint,'Inspect token activity') + '</td>';
  return tr;
}
function closedRow(position) {
  const tr = document.createElement('tr');
  const move = capMove(position.entry_cap,position.exit_cap), pnlClass = position.pnl_usd >= 0 ? 'up' : 'down';
  const returnPct = position.cost_usd ? position.pnl_usd / position.cost_usd : null;
  tr.innerHTML = '<td class="ledger-asset"><a class="ledger-token" href="' + tokenUrl(position.mint) + '" target="_blank" rel="noreferrer"><b>' + esc(position.symbol) + ' ↗</b><small title="' + esc(position.mint) + '">' + esc(position.mint.slice(0,8)) + '…</small></a></td>' +
    '<td>' + tokenLink(position.mint,'<b>Entered ' + dateTime(position.entry_at) + '</b>','ledger-time') + '<small>Closed ' + dateTime(position.exit_at) + ' · held ' + duration(position.exit_at - position.entry_at) + '</small></td>' +
    '<td><b>' + money(position.entry_cap) + ' → ' + money(position.exit_cap) + '</b><small class="' + (move >= 0 ? 'up' : 'down') + '">' + (move == null ? 'Market-cap move unavailable' : signedPct(move) + ' market-cap move') + '</small></td>' +
    '<td><b>' + money(position.cost_usd) + ' → ' + money(position.proceeds_usd) + '</b><small>Stake → proceeds</small></td>' +
    '<td><b>' + esc(readable(position.exit_reason)) + '</b><small>Exit fee ' + money(position.exit_fees_usd) + '</small>' + tokenLink(position.mint,'Inspect token activity') + '</td>' +
    '<td><b class="' + pnlClass + '">' + signed(position.pnl_usd) + '</b><small class="' + pnlClass + '">' + (returnPct == null ? 'Return unavailable' : signedPct(returnPct) + ' return') + '</small></td>';
  return tr;
}
function renderStrategyAnalysis(strategy, failed = false) {
  const a = window.PumpWizardAnalysis.analyze(strategy.report, fullTradeHistory);
  let panel = $('strategy-analysis');
  if (!panel) {
    panel = document.createElement('section');
    panel.id = 'strategy-analysis';
    panel.className = 'strategy-analysis';
    panel.setAttribute('aria-labelledby', 'analysis-title');
    document.querySelector('.explain-grid').before(panel);
  }
  const metric = (label, value) => '<div><dt>' + esc(label) + '</dt><dd>' + esc(value) + '</dd></div>';
  const accounting = a.accounting, closed = a.closed;
  const full = a.evidence.complete;
  const evidence = full ? a.evidence.closed_reported + ' closed positions · ' + a.evidence.closed_unique_coins + ' distinct coins'
    : a.evidence.closed_received + ' records available / ' + (a.evidence.closed_reported ?? 'unknown') + ' reported';
  const winners = a.drivers.filter(row => row.net_pnl_usd > 0).slice(0, 3);
  const loserCoins = [...a.drivers].filter(row => row.net_pnl_usd < 0).sort((a,b) => a.net_pnl_usd - b.net_pnl_usd).slice(0, 3);
  const coinRows = rows => rows.map(row => '<li><a href="' + tokenUrl(row.mint) + '" target="_blank" rel="noopener noreferrer">' + esc(row.symbol) + ' ↗</a><span class="' + (row.net_pnl_usd >= 0 ? 'up' : 'down') + '">' + signed(row.net_pnl_usd) + '</span><small>' + row.trades + ' closed trades</small></li>').join('');
  const expanded = panel.querySelector('.analysis-details')?.open || false;
  panel.innerHTML = '<div class="analysis-heading"><h2 id="analysis-title">Strategy analysis</h2><span class="analysis-state" role="status">' + esc(failed ? 'History unavailable · try again' : a.evidence.status) + '</span></div>' +
    '<dl class="analysis-summary">' + metric('Realized net', money(accounting.realized_net_pnl_usd)) + metric('Open marked P&L', money(accounting.unrealized_pnl_usd)) + metric('Modeled fees', money(accounting.fees_usd)) + metric('Max drawdown', pct(accounting.max_drawdown_fraction)) + '</dl>' +
    '<p class="analysis-scope">' + (full ? esc(evidence) + '. All-time closed-trade evidence.' : 'Full trade attribution is not loaded. Ledger totals are shown above.') + '</p>' +
    '<details class="analysis-details"' + (expanded ? ' open' : '') + '><summary>Evidence &amp; next experiment</summary>' +
    '<p class="analysis-scope">' + esc(evidence) + (a.evidence.observed_span_days == null ? '' : ' · ' + a.evidence.observed_span_days.toFixed(1) + ' days between first and latest observation') + '. All-time instance data.</p>' +
    '<div class="analysis-grid"><article><h3>Where P&amp;L comes from</h3><dl>' +
    metric('Total net', money(accounting.total_net_pnl_usd)) + metric('Realized net', money(accounting.realized_net_pnl_usd)) + metric('Open marked P&L', money(accounting.unrealized_pnl_usd)) +
    metric('Modeled fees', money(accounting.fees_usd)) + metric('Failed-attempt costs', money(accounting.failed_fees_usd)) + metric('Maximum drawdown', pct(accounting.max_drawdown_fraction)) +
    '</dl><p>' + esc(accounting.note) + '</p></article><article><h3>Closed-trade evidence</h3><dl>' +
    metric('Mean result per trade', money(closed?.mean_trade_usd)) + metric('Average winning trade', money(closed?.mean_win_usd)) + metric('Average losing trade', money(closed?.mean_loss_usd)) +
    metric('Win rate', pct(closed?.win_rate)) + metric('Profit factor', closed?.profit_factor == null ? (closed?.gross_profit_usd > 0 ? 'No closed losses' : '—') : closed.profit_factor.toFixed(2)) +
    metric('Without largest winner', money(closed?.without_largest_winner_usd)) +
    '</dl><p>' + esc(a.evidence.note) + '</p></article><article><h3>Coin contributions</h3>' +
    (full ? '<p>Net P&amp;L from fully closed trades, grouped by coin.</p><ul class="analysis-coins">' + coinRows(winners) + coinRows(loserCoins) + '</ul>' +
      (!winners.length && !loserCoins.length ? '<p>No nonzero closed results yet.</p>' : '')
      : '<p>Waiting for complete history before attributing results to coins.</p>') + '</article></div>' +
    '<div class="analysis-findings"><h3>Recorded observations</h3><ul>' + a.observations.map(text => '<li>' + esc(text) + '</li>').join('') + '</ul>' +
    (a.loss_sources.length ? '<dl class="analysis-losses">' + a.loss_sources.slice(0, 3).map(row => metric(readable(row.exit_reason) + ' · ' + row.losing_trades + ' losses', money(-row.loss_usd))).join('') + '</dl>' : '') + '</div>' +
    '<div class="analysis-experiment"><p class="eyebrow">Next experiment · proposal</p><h3>' + esc(a.next_experiment.title) + '</h3><p>' + esc(a.next_experiment.hypothesis) + '</p><p>' + esc(a.next_experiment.proposal) + '</p><small>' + esc(a.next_experiment.evaluation) + '</small></div>' +
    '<p class="analysis-footer">Fixed-rule analysis, not a prediction. Fees are already included in P&amp;L. <a href="#trade-history">Inspect trade history →</a></p>' +
    (!full ? '<button class="analysis-load" type="button"' + (analysisLoading ? ' disabled' : '') + '>' + (analysisLoading ? 'Loading complete evidence…' : 'Load full closed-trade analysis') + '</button><p class="analysis-scope">On-demand attribution: up to 10,000 closed records / 20 MB.</p>' : '') + '</details>';
  panel.querySelector('.analysis-load')?.addEventListener('click', async () => {
    analysisLoading = true; renderStrategyAnalysis(analysisStrategy);
    try {
      const history = await window.PumpWizardAnalysis.fetchHistory(id);
      fullTradeHistory = history.trades; fullTradeHistoryCount = history.total;
      analysisLoading = false; renderStrategyAnalysis(analysisStrategy);
    } catch { analysisLoading = false; renderStrategyAnalysis(analysisStrategy, true); }
  });
}
function renderTradeHistory() {
  const data = historyPages[historyPageIndex];
  let controls = $('history-pagination');
  if (!controls) {
    controls = document.createElement('div'); controls.id = 'history-pagination'; controls.className = 'history-pagination';
    $('closed-positions').closest('.table-wrap').after(controls);
    controls.innerHTML = '<button type="button" id="history-previous">Previous</button><button type="button" id="history-next">Next</button><button type="button" id="history-refresh">Refresh history</button>';
    $('history-previous').onclick = () => { historyPageIndex--; renderTradeHistory(); };
    $('history-next').onclick = () => loadHistoryPage(false, true);
    $('history-refresh').onclick = () => loadHistoryPage(true);
  }
  $('history-previous').disabled = tradeHistoryLoading || historyPageIndex === 0;
  $('history-next').disabled = tradeHistoryLoading || !data?.next_before;
  $('history-refresh').disabled = tradeHistoryLoading;
  if (!data) { $('closed-count').textContent = 'Loading history…'; return; }
  $('closed-count').textContent = data.total + ' closed · showing ' + (data.total ? historyPageIndex*25+1 : 0) + '–' + (historyPageIndex*25+data.trades.length) +
    (analysisStrategy?.report.closed_count !== data.total ? ' · refresh for newer records' : '');
  data.trades.length ? $('closed-positions').replaceChildren(...data.trades.map(closedRow)) : empty('closed-positions',6,'No closed positions yet.');
}
async function loadHistoryPage(reset = false, next = false) {
  if (tradeHistoryLoading) return;
  if (next && historyPages[historyPageIndex+1]) { historyPageIndex++; renderTradeHistory(); return; }
  if (reset) { historyPages = []; historyPageIndex = 0; }
  tradeHistoryLoading = true;
  renderTradeHistory();
  try {
    const query = new URLSearchParams({limit:'25'});
    if (next) { query.set('snapshot_max_id', historyPages[0].snapshot_max_id); query.set('before', historyPages[historyPageIndex].next_before); }
    const data = await window.PumpWizardPublic.request('/api/public/strategies/' + encodeURIComponent(id) + '/trades?' + query);
    if (data.strategy_id !== id || !Array.isArray(data.trades)) throw Error();
    historyPages.push(data); historyPageIndex = historyPages.length-1;
  } catch { $('closed-count').textContent = 'History unavailable · use Refresh history to retry'; }
  finally { tradeHistoryLoading = false; if (historyPages.length) renderTradeHistory(); else $('history-refresh').disabled = false; }
}
function versionComparison(strategy, strategies) {
  const trial = strategy.trial || strategies.find(s => s.trial?.parent_id === strategy.id)?.trial;
  if (!trial) return;
  const arms = trial.generation === 'V3' ? ['control', 'confirmation', 'protection', 'combined'] : ['control', 'risk', 'exits', 'combined'];
  const siblings = strategies.filter(s => s.trial?.trial_id === trial.trial_id && s.trial.group === trial.group)
    .sort((a,b) => arms.indexOf(a.trial.arm) - arms.indexOf(b.trial.arm));
  const control = siblings.find(s => s.trial.arm === 'control');
  const matched = control?.report.started_at != null && siblings.length === 4 &&
    siblings.every(s => s.report.started_at === control.report.started_at && s.report.last_at === control.report.last_at);
  let panel = $('version-comparison');
  if (!panel) {
    panel = document.createElement('section'); panel.id = 'version-comparison'; panel.className = 'version-comparison';
    document.querySelector('.detail-metrics').after(panel);
  }
  const start = matched ? 'Matched start: ' + when(control.report.started_at) : 'Waiting for synchronized observations';
  const comparisonTitle = trial.comparison_title || 'V1 / V2 comparison';
  const policyFor = definition => {
    if (definition.arm === 'control') return 'Original rules, fresh ledger';
    return [definition.risk_enabled && 'Fresh ownership clearance',
      definition.ownership_telemetry && 'Ownership telemetry only',
      definition.exits_enabled && ('Trail arms at +' + Math.round(definition.profit_activation_return*100) + '% net; ' + Math.round(definition.profit_trailing_drawdown*100) + '% giveback'),
      definition.confirmation_enabled && ('Second valid signal after ' + definition.confirmation_delay_seconds + 's'),
      definition.protection_enabled && ('Break-even protection after +' + Math.round(definition.break_even_activation_return*100) + '% net')]
      .filter(Boolean).join(' · ');
  };
  const evidenceFor = (definition, checks) => {
    const evidence = [];
    if (definition.risk_enabled) evidence.push((checks.ownership_rejections || 0) + ' ownership-rejected checks');
    if (definition.ownership_telemetry) evidence.push((checks.ownership_observations || 0) + ' ownership observations');
    if (definition.confirmation_enabled) evidence.push((checks.confirmation_waits || 0) + ' confirmation waits');
    if (definition.protection_enabled) evidence.push((checks.protection_armed || 0) + ' protection arms');
    return evidence.join(' · ') || 'Original entry-risk policy';
  };
  panel.innerHTML = '<div class="version-heading"><h2>' + esc(comparisonTitle) + '</h2><a href="/engine/' + encodeURIComponent(trial.parent_id) + '">Original strategy ↗</a></div>' +
    '<p>Four independent $500 simulations. ' + esc(start) + '. Original historical results are not mixed into this comparison.</p>' +
    '<div class="version-grid">' + siblings.map(s => {
      const r = s.report, d = s.trial, checks = r.trial_metrics || {};
      const factor = r.closed_profit_factor == null ? (r.closed_gross_profit_usd > 0 ? 'No closed losses' : 'No closed evidence') : r.closed_profit_factor.toFixed(2);
      const policy = policyFor(d);
      return '<a class="version-card' + (s.id === strategy.id ? ' current' : '') + '" href="/engine/' + encodeURIComponent(s.id) + '"' + (s.id === strategy.id ? ' aria-current="page"' : '') + '>' +
        '<strong>' + esc(d.label) + '</strong><small>' + esc(policy) + '</small>' +
        '<b class="version-pnl ' + (r.pnl_usd > 0 ? 'up' : r.pnl_usd < 0 ? 'down' : '') + '">' + signed(r.pnl_usd) + '</b>' +
        '<small>' + (d.arm === 'control' ? 'Control baseline' : matched ? signed(r.pnl_usd-control.report.pnl_usd) + ' vs V1 control' : 'Comparison pending') + '</small>' +
        '<dl><div><dt>Realized net</dt><dd>' + signed(r.realized_net_pnl_usd) + '</dd></div>' +
        '<div><dt>Entries / closed</dt><dd>' + r.total_entries + ' / ' + r.closed_count + '</dd></div>' +
        '<div><dt>Closed win rate</dt><dd>' + pct(r.closed_win_rate ?? 0) + '</dd></div>' +
        '<div><dt>Max drawdown</dt><dd>' + pct(r.max_drawdown) + '</dd></div>' +
        '<div><dt>Closed profit factor</dt><dd>' + esc(factor) + '</dd></div></dl>' +
        '<small>' + esc(evidenceFor(d, checks)) + '</small>' +
        '<span class="version-evidence">' + (r.closed_count ? r.closed_count + ' closed trades · unproven experiment' : 'No closed trades · collecting evidence') + '</span></a>';
    }).join('') + '</div><p class="version-footnote">Net P&amp;L includes open marks and modeled costs. Policy-check counts are repeated checks, not unique coins. No trades does not mean success; an exit-protection request cannot guarantee its later modeled fill.</p>';
}
function render(data) {
  const strategy = data.strategies.find(item => item.id === id);
  if (!strategy) {
    $('strategy-name').textContent = 'Strategy not found';
    $('strategy-summary').textContent = 'This strategy may not exist or may no longer be active.';
    document.body.classList.remove('strategy-loading');
    document.querySelector('.detail-main').setAttribute('aria-busy','false');
    return;
  }
  document.body.classList.remove('strategy-loading');
  document.querySelector('.detail-main').setAttribute('aria-busy','false');
  const report = strategy.report, config = report.config, style = config.entry_style || 'momentum';
  analysisStrategy = strategy;
  const explainer = id === 'exit-study-twin'
    ? {title:'Test the exit, not the entry',text:'This cohort intentionally shares the core-frequency momentum entry rule. Its different target ladder and tighter trailing drawdown isolate whether exit discipline improves outcomes after a valid entry.'}
    : strategy.description ? {title:style.replaceAll('-', ' '),text:strategy.description} : EXPLAINERS[style] || EXPLAINERS.momentum;
  const closed = (data.closed_positions || []).filter(item => item.strategy_id === id);
  const open = (data.open_positions || []).filter(item => item.strategy_id === id);
  const wins = report.closed_wins || 0;
  document.title = strategy.label + ' · PumpWizard.fun';
  $('strategy-name').textContent = strategy.label;
  $('strategy-summary').textContent = explainer.text;
  $('started-at').textContent = when(report.started_at);
  const lastAt=$('last-at');
  if(lastAt){lastAt.previousElementSibling?.remove();lastAt.remove()}
  $('equity').textContent = money(report.equity_usd);
  $('pnl').textContent = signed(report.pnl_usd);
  $('pnl').className = report.pnl_usd >= 0 ? 'up' : 'down';
  $('entries').textContent = report.total_entries || 0;
  $('win-rate').textContent = pct(report.closed_win_rate);
  $('win-note').textContent = report.closed_count ? wins + ' wins / ' + report.closed_count + ' closed' : 'No closed trades yet';
  agentLiveView(strategy);
  configurationControl(strategy);
  agentShare(strategy);
  versionComparison(strategy, data.strategies);
  document.querySelector('.detail-status')?.remove();
  $('thesis-title').textContent = explainer.title;
  $('thesis').textContent = explainer.text;
  rules('signal-rules',[
    ['Entry style',style.replaceAll('_',' ')],
    ['Market-cap band',money(config.entry_cap_min)+' – '+money(config.entry_cap_max)],
    ['Observation window',config.observation_seconds+' seconds'],
    ['Minimum growth',(config.minimum_growth_fraction*100).toFixed(1)+'%'],
    ['Liquidity screen',strategy.description ? 'Family-specific signal + modeled entry-cost check' : money(config.minimum_reserve_growth_usd)+' reserve growth'],
    ['Entry policy',config.entry_policy === 'strict' ? 'Fresh, matching ownership clearance required' : 'Unverified ownership']
  ]);
  rules('risk-rules',[
    ['Entry size',money(config.order_usd)],
    ['Maximum positions',config.max_positions],
    ['Stop loss',(config.stop_loss_fraction*100).toFixed(0)+'%'],
    ['Original trailing drawdown',(config.trailing_drawdown_fraction*100).toFixed(0)+'% after first target'],
    ...(strategy.trial?.exits_enabled ? [['V2 net-value profit trail','Arms at +'+Math.round(strategy.trial.profit_activation_return*100)+'% net return; exits after '+Math.round(strategy.trial.profit_trailing_drawdown*100)+'% giveback']] : []),
    ...(strategy.trial?.ownership_telemetry ? [['V2 ownership telemetry','Recorded for research only; it never blocks a simulated entry or exit']] : []),
    ...(strategy.trial?.confirmation_enabled ? [['V3 entry confirmation','Requires a second valid signal after '+strategy.trial.confirmation_delay_seconds+' seconds']] : []),
    ...(strategy.trial?.protection_enabled ? [['V3 break-even protection','Arms after +'+Math.round(strategy.trial.break_even_activation_return*100)+'% net return; requests exit if net value returns to cost']] : []),
    ['Maximum hold',Math.round(config.max_hold_seconds/86400)+' days'],
    ['Profit targets',config.targets.map(target=>money(target.market_cap_usd)).join(' · ')]
  ]);
  $('open-count').textContent = open.length+' position'+(open.length===1?'':'s');
  open.length ? $('open-positions').replaceChildren(...open.map(openRow)) : empty('open-positions',7,'No open positions.');
  if (!historyPages.length) loadHistoryPage();
  else renderTradeHistory();
  renderStrategyAnalysis(strategy);
  const history=$('closed-positions').closest('section');
  history.id='trade-history';
  history.querySelector('h2').textContent='Trade history';
}
async function refresh() {
  try {
    if (!id) throw Error();
    render(await window.PumpWizardPublic.request('/api/public/strategies/' + encodeURIComponent(id)));
  } catch {
    $('strategy-name').textContent = 'Strategy unavailable';
    $('strategy-summary').textContent = 'The strategy page could not load its public telemetry.';
    document.body.classList.remove('strategy-loading');
    document.querySelector('.detail-main').setAttribute('aria-busy','false');
  } finally {
    setTimeout(refresh,5000);
  }
}
refresh();
