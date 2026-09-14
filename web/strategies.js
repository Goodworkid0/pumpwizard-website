if (typeof window === 'undefined' || !window.PumpWizardMarket?.isPreview) {
const $ = id => document.getElementById(id);
const money = n => n == null ? '—' : new Intl.NumberFormat('en-US', {style:'currency', currency:'USD', maximumFractionDigits:2}).format(n);
const signed = n => (n >= 0 ? '+' : '−') + money(Math.abs(n));
const pct = n => n == null ? '—' : (n * 100).toFixed(1) + '%';
const when = t => t ? new Date(t * 1000).toLocaleString() : '—';
const esc = x => String(x ?? '').replace(/[&<>'"]/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', "'":'&#39;', '"':'&quot;'}[c]));
const duration = seconds => seconds >= 86400 ? (seconds / 86400) + 'd' : seconds >= 3600 ? (seconds / 3600) + 'h' : (seconds / 60) + 'm';
const THEORY = {
  momentum:'Repeated positive price and reserve movement inside a defined observation window.',
  breakout:'A positive trend that clears its earlier observation range.',
  pullback_reclaim:'A local pullback that later reclaims from its trough.',
  slow_burn:'Persistent positive movement across both halves of a longer observation window.'
};
let latest = null, page = 0;
let versionFilter = new URLSearchParams(location.search).get('version') || '';
if (!['', 'original', 'trials'].includes(versionFilter)) versionFilter = '';
const versionNav = document.createElement('div');
versionNav.className = 'version-filters';
versionNav.setAttribute('role', 'group');
versionNav.setAttribute('aria-label', 'Strategy versions');
for (const [value, label] of [['', 'All Instances'], ['original', 'Original strategies'], ['trials', 'Versioned comparison pilots']]) {
  const button = document.createElement('button');
  button.type = 'button'; button.dataset.version = value; button.textContent = label;
  button.addEventListener('click', () => {
    versionFilter = value; page = 0;
    const url = new URL(location.href);
    value ? url.searchParams.set('version', value) : url.searchParams.delete('version');
    history.replaceState(null, '', url);
    renderCards();
  });
  versionNav.append(button);
}
document.querySelector('.lab-toolbar').before(versionNav);
const pageSize = 24;
let comparedKey = window.PumpWizardComparison.parseSelection(location.search).join(',');
let catalogController = null, catalogDebounce = null;
const comparison = window.PumpWizardComparison.create({
  anchor: $('strategy-cards'),
  onSelectionChanged: () => {
    document.querySelectorAll('.compare-instance').forEach(button => {
      const selected = comparison.has(button.dataset.strategyId);
      button.setAttribute('aria-pressed', String(selected));
      button.textContent = selected ? '✓ Selected for comparison' : '+ Compare';
    });
    const key = comparison.selected().join(',');
    if (key !== comparedKey) { comparedKey = key; renderCards(); }
  }
});
window.addEventListener('popstate', () => comparison.restore());
function status(report) { return report.experiment_status || (report.total_entries ? 'active' : 'observing'); }
function isDeadInstance(report) {
  return Number.isFinite(report?.equity_usd) && report.equity_usd < 50;
}
function searchable(strategy) {
  const config = strategy.report.config, min = config.entry_cap_min, max = config.entry_cap_max;
  return [strategy.label, strategy.id, strategy.family, strategy.trial?.label, strategy.trial?.parent_id, config.entry_style, min, max,
    Math.round(min / 1000) + 'k', Math.round(max / 1000) + 'k',
    Math.round(min / 1000) + 'k-' + Math.round(max / 1000) + 'k',
    Math.round(min / 1000) + 'k–' + Math.round(max / 1000) + 'k',
    money(min), money(max)].join(' ').toLowerCase();
}
function performance(report, filter) {
  if (!filter) return true;
  if (filter === 'profitable') return report.pnl_usd > 0;
  if (filter === 'unprofitable') return report.pnl_usd < 0;
  if (filter === 'positive_24h') return report.pnl_24h_usd > 0;
  if (filter === 'winning_record') return report.closed_count > 0 && report.closed_win_rate >= .5;
  if (filter === 'losing_record') return report.closed_count > 0 && report.closed_win_rate < .5;
  return report.closed_count === 0;
}
function sortValue(strategy, sort) {
  const report = strategy.report;
  if (sort === 'return_pct') return report.initial_usd ? report.pnl_usd / report.initial_usd : 0;
  return report[sort.replace('_asc','')] || 0;
}
function capFocus(config) {
  if (config.entry_cap_max <= 25000) return 'early';
  if (config.entry_cap_min >= 100000) return 'established';
  return 'growth';
}
function capLabel(config) { return capFocus(config).replace(/^./, c => c.toUpperCase()) + ' cap'; }
function configurationHandoff(strategy) {
  const report = strategy.report || {};
  return JSON.stringify({
    schema:'pumpwizard.strategy-handoff/v1',
    generated_at:new Date().toISOString(),
    mode:'simulation_only',
    strategy:{id:strategy.id,label:strategy.label,family:strategy.family || report.config?.entry_style,description:strategy.description || null},
    configuration:report.config || {},
    versioned_trial:strategy.trial || null,
    public_telemetry:{
      unique_coins:report.unique_coins ?? null, fees_usd:report.fees_usd ?? null, failed_fees_usd:report.failed_fees_usd ?? null,
      closed_profit_factor:report.closed_profit_factor ?? null, trial_metrics:report.trial_metrics || {},started_at:report.started_at || null,last_observation_at:report.last_at || null,model:report.model || null,initial_usd:report.initial_usd,equity_usd:report.equity_usd,cash_usd:report.cash_usd,pnl_usd:report.pnl_usd,realized_net_pnl_usd:report.realized_net_pnl_usd,unrealized_pnl_usd:report.unrealized_pnl_usd,total_entries:report.total_entries,closed_count:report.closed_count,closed_wins:report.closed_wins,closed_win_rate:report.closed_win_rate,max_drawdown:report.max_drawdown ?? null,positions:report.positions || [],recent_trades:report.trades || [],recent_events:report.events || [],current_candidates:report.candidates || []},
    public_endpoints:{full_closed_trade_history:location.origin+'/api/public/strategies/'+encodeURIComponent(strategy.id)+'/trades',strategy_page:location.origin + '/engine/' + encodeURIComponent(strategy.id),telemetry_api:location.origin + '/api/public/strategies/'+encodeURIComponent(strategy.id),execution_history_csv:location.origin + '/api/public/history.csv'},
    limitations:['Public market observations and modeled execution only; no wallet is connected and no order is sent.','Recorded P&L can include open marked positions and is not a claim of future profitability.','Validate data coverage, model assumptions, fees, delays and risk controls independently before using any configuration.']
  }, null, 2);
}
async function fullHandoff(strategy) {
  const data = await window.PumpWizardPublic.request('/api/public/strategies/' + encodeURIComponent(strategy.id));
  const full = data.strategies.find(item => item.id === strategy.id);
  if (!full) throw Error('Instance unavailable');
  return configurationHandoff(full);
}
function card(strategy) {
  const r = strategy.report, c = r.config, node = document.createElement('article');
  const description = strategy.description || (strategy.id.startsWith('exit-study-twin') ? 'Core-frequency entries with a different target and trailing-exit policy.' : THEORY[c.entry_style]) || 'An independent forward-tested agent.';
  node.className = 'lab-card';
  const metrics = [
    ['Wallet / P&L', money(r.equity_usd) + ' <i class="' + (r.pnl_usd > 0 ? 'up' : r.pnl_usd < 0 ? 'down' : '') + '">' + signed(r.pnl_usd) + '</i>'],
    ['Total trades / unique coins', (r.total_entries || 0) + ' / ' + (r.unique_coins || 0)],
    ['Closed performance', pct(r.closed_win_rate) + ' win rate · ' + r.closed_count + ' closed'],
    ['Observe / maximum hold', duration(c.observation_seconds) + ' / ' + duration(c.max_hold_seconds)]
  ];
  if (r.max_drawdown != null) metrics.push(['Maximum drawdown', pct(r.max_drawdown)]);
  node.innerHTML = '<a class="lab-card-link" href="/engine/' + encodeURIComponent(strategy.id) + '"><div class="lab-card-top"><div class="agent-tags"><span>' + esc(c.entry_style.replaceAll('_', ' ')) + '</span><span class="agent-cap-tag">' + capLabel(c) + '</span></div><span class="agent-status">' + esc(status(r).replaceAll('_', ' ')) + '</span></div><h2>' + esc(strategy.label) + '</h2><p>' + esc(description) + '</p><div class="band">' + money(c.entry_cap_min) + ' – ' + money(c.entry_cap_max) + ' market cap</div><dl>' + metrics.map(([k,v]) => '<div><dt>' + esc(k) + '</dt><dd>' + v + '</dd></div>').join('') + '</dl><div class="target-row"><span>Isolated $500 ledger</span><span>View agent →</span></div></a><div class="lab-card-actions"><button type="button" class="copy-card-config" title="Copies public configuration and technical telemetry as JSON">Copy config</button><span class="card-copy-note" aria-live="polite"></span></div>';
  if (strategy.trial) {
    const badge = document.createElement('div'); badge.className = 'trial-badge';
    badge.textContent = strategy.trial.label + ' · matched forward test';
    node.querySelector('h2').before(badge);
    const note = document.createElement('small'); note.className = 'trial-card-note';
    const trial = strategy.trial;
    note.textContent = trial.arm === 'control' ? 'Fresh baseline · same start as every comparison arm' :
      [trial.risk_enabled && 'Verified ownership required',
        trial.ownership_telemetry && 'Ownership telemetry recorded',
        trial.exits_enabled && 'Earlier net-value profit trail',
        trial.confirmation_enabled && ('Second valid signal after ' + trial.confirmation_delay_seconds + 's'),
        trial.protection_enabled && ('Break-even protection after +' + Math.round(trial.break_even_activation_return * 100) + '% net')]
        .filter(Boolean).join(' · ');
    node.querySelector('.target-row').before(note);
  }
  const copy = node.querySelector('.copy-card-config'), note = node.querySelector('.card-copy-note');
  const selectionRow = document.createElement('div');
  selectionRow.className = 'card-comparison-selection';
  const compareButton = document.createElement('button');
  compareButton.type = 'button';
  compareButton.className = 'compare-instance';
  compareButton.dataset.strategyId = strategy.id;
  compareButton.setAttribute('aria-label', 'Compare ' + strategy.label);
  compareButton.setAttribute('aria-pressed', String(comparison.has(strategy.id)));
  compareButton.textContent = comparison.has(strategy.id) ? '✓ Selected for comparison' : '+ Compare';
  compareButton.addEventListener('click', () => comparison.toggle(strategy.id));
  selectionRow.append(compareButton);
  node.prepend(selectionRow);
  const promptButton = document.createElement('button');
  promptButton.type = 'button';
  promptButton.className = 'copy-card-config copy-agent-prompt';
  promptButton.textContent = 'Copy agent prompt';
  promptButton.title = 'Copy strategy rules, evidence and analysis instructions for your agent';
  copy.before(promptButton);
  const downloadButton = document.createElement('button');
  downloadButton.type = 'button';
  downloadButton.className = 'copy-card-config download-research';
  downloadButton.textContent = 'Download research';
  downloadButton.title = 'Save configuration, analysis and complete closed-position history as JSON';
  note.before(downloadButton);
  window.PumpWizardAnalysis.bindDownload(downloadButton, note, () => fullHandoff(strategy));
  promptButton.addEventListener('click', async () => {
    promptButton.disabled = true;
    note.textContent = 'Preparing analysis…';
    try {
      const handoff = await window.PumpWizardAnalysis.withHistory(await fullHandoff(strategy));
      await window.PumpWizardHandoff.copy(window.PumpWizardHandoff.prompt(handoff));
      note.textContent = 'Prompt copied — paste into your agent';
    } catch { note.textContent = 'Could not prepare the prompt. Please try again.'; }
    finally { promptButton.disabled = false; }
  });
  copy.addEventListener('click', async () => {
    copy.disabled = true;
    try { await window.PumpWizardHandoff.copy(await fullHandoff(strategy)); note.textContent = 'Configuration copied'; }
    catch { note.textContent = 'Configuration unavailable. Please retry.'; }
    finally { copy.disabled = false; }
  });
  return node;
}
function renderCards() {
  clearTimeout(catalogDebounce);
  catalogDebounce = setTimeout(refreshCatalog, 180);
}
function renderCatalogCards() {
  if (!latest) return;
  for (const button of versionNav.querySelectorAll('button')) button.setAttribute('aria-pressed', String(button.dataset.version === versionFilter));
  const visible = latest.strategies, total = latest.total;
  page = latest.page - 1;
  $('strategy-cards').replaceChildren(...visible.map(card));
  if (!visible.length) $('strategy-cards').textContent = 'No experiments match these filters.';
  $('page-info').textContent = total ? (page * pageSize + 1) + '–' + Math.min(total, (page + 1) * pageSize) + ' of ' + total + ' experiments' : '0 experiments';
  $('search-summary').textContent = total + ' result' + (total === 1 ? '' : 's');
  $('clear-search').hidden = !$('strategy-search').value;
  $('previous-page').disabled = page === 0;
  $('next-page').disabled = (page + 1) * pageSize >= total;
}
function render(data) {
  latest = data;
  $('active-agents').textContent = data.activity.instances;
  $('activity-24h').textContent = data.activity.events_24h;
  $('entries-24h').textContent = data.activity.entries_24h;
  $('open-positions-total').textContent = data.activity.open_positions;
  const dead = data.activity.dead;
  $('dead-instances').textContent = dead;
  $('dead-instances').className = dead ? 'down' : '';
  const previous = $('family-filter').value;
  $('family-filter').replaceChildren(new Option('All signal families', ''), ...data.families.map(f => new Option(f.replaceAll('_', ' '), f)));
  $('family-filter').value = previous;
  comparison.update([...data.strategies, ...data.comparison]);
  renderCatalogCards();
}
for (const id of ['strategy-search', 'family-filter', 'status-filter', 'performance-filter', 'strategy-sort']) $(id).addEventListener(id === 'strategy-search' ? 'input' : 'change', () => { page = 0; renderCards(); });
$('clear-search').addEventListener('click', () => {
  $('strategy-search').value = '';
  page = 0;
  renderCards();
  $('strategy-search').focus();
});
$('previous-page').onclick = () => { page--; renderCards(); };
$('next-page').onclick = () => { page++; renderCards(); };
async function refreshCatalog() {
  catalogController?.abort();
  const controller = new AbortController(); catalogController = controller;
  const timeout = setTimeout(() => controller.abort(), 15000);
  const params = new URLSearchParams({page:String(page+1), page_size:String(pageSize), q:$('strategy-search').value.trim(),
    family:$('family-filter').value, status:$('status-filter').value, performance:$('performance-filter').value,
    sort:$('strategy-sort').value, version:versionFilter, compare:comparison.selected().join(',')});
  try {
    const data = await window.PumpWizardPublic.request('/api/public/catalog?' + params, {signal:controller.signal});
    if (catalogController === controller) render(data);
  } catch {}
  finally { clearTimeout(timeout); }
}
async function refresh() { await refreshCatalog(); setTimeout(refresh, 10000); }
refresh();
}
