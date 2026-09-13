/* Public, descriptive comparison. Selection never changes running instances. */
(() => {
  const validId = value => typeof value === 'string' && /^[a-z0-9-]{1,40}$/.test(value);
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const dollars = value => numeric(value) ? new Intl.NumberFormat('en-US', {style:'currency', currency:'USD'}).format(value) : '—';
  const percent = value => numeric(value) ? (value * 100).toFixed(1) + '%' : '—';
  const count = value => numeric(value) ? String(value) : '—';
  const date = value => numeric(value) && value > 0 ? new Date(value * 1000).toLocaleString() : '—';
  const duration = value => !numeric(value) ? '—' : value >= 86400 ? (value / 86400) + 'd' :
    value >= 3600 ? (value / 3600) + 'h' : value >= 60 ? (value / 60) + 'm' : value + 's';
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  function parseSelection(search) {
    return [...new Set((new URLSearchParams(search).get('compare') || '').split(',').filter(validId))].slice(0, 4);
  }
  function selectionUrl(href, ids) {
    const url = new URL(href);
    const selection = [...new Set(ids.filter(validId))].slice(0, 4);
    if (selection.length) url.searchParams.set('compare', selection.join(','));
    else url.searchParams.delete('compare');
    return url.href;
  }
  function stable(value) {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])]));
    return value;
  }
  function same(values) { return values.every(value => JSON.stringify(stable(value)) === JSON.stringify(stable(values[0]))); }
  const policyKeys = ['risk_enabled', 'ownership_telemetry', 'exits_enabled', 'profit_activation_return',
    'profit_trailing_drawdown', 'confirmation_enabled', 'confirmation_delay_seconds',
    'protection_enabled', 'break_even_activation_return'];
  function buildComparison(strategies) {
    const selected = strategies.slice(0, 4);
    const reports = selected.map(strategy => strategy.report || {});
    const starts = reports.map(report => report.started_at);
    const funding = reports.map(report => report.initial_usd);
    const row = (label, getValue, format = count, colored = false) => {
      const values = reports.map((report, index) => getValue(report, selected[index]));
      return {label, differs: !same(values), cells: values.map(value => ({text:format(value),
        tone:colored && numeric(value) ? (value > 0 ? 'up' : value < 0 ? 'down' : '') : ''}))};
    };
    const rows = [
      row('Started', report => report.started_at, date),
      row('Latest observation', report => report.last_at, date),
      row('Starting balance', report => report.initial_usd, dollars),
      row('Total net P&L · all time', report => report.pnl_usd, dollars, true),
      row('Realized net P&L', report => report.realized_net_pnl_usd, dollars, true),
      row('Open marked P&L', report => report.unrealized_pnl_usd, dollars, true),
      row('Modeled fees', report => report.fees_usd, dollars),
      row('Failed-attempt costs', report => report.failed_fees_usd, dollars),
      row('Closed positions', report => report.closed_count),
      row('Closed wins', report => report.closed_wins),
      row('Closed win rate', report => report.closed_count === 0 ? 'No closed trades' : report.closed_win_rate,
        value => typeof value === 'string' ? value : percent(value)),
      row('Closed profit factor', report => report.closed_count === 0 ? 'No closed trades' :
        numeric(report.closed_profit_factor) ? report.closed_profit_factor : report.closed_gross_profit_usd > 0 ? 'No closed losses' : null,
        value => typeof value === 'string' ? value : numeric(value) ? value.toFixed(2) : '—'),
      row('Maximum drawdown', report => report.max_drawdown, percent),
      row('Total entries / distinct coins', report => [report.total_entries, report.unique_coins],
        values => values.map(count).join(' / ')),
      row('Signal family', report => report.config?.entry_style, value => value || '—'),
      row('Entry market-cap band', report => [report.config?.entry_cap_min, report.config?.entry_cap_max],
        values => values.map(dollars).join(' – ')),
      row('Entry size', report => report.config?.order_usd, dollars),
      row('Observation / maximum hold', report => [report.config?.observation_seconds, report.config?.max_hold_seconds],
        values => values.map(duration).join(' / ')),
      row('Variant', (_, strategy) => strategy.trial?.label || 'Original', value => value)
    ];
    const configs = selected.map(strategy => ({configuration: strategy.report?.config || {},
      trial_policy: Object.fromEntries(policyKeys.filter(key => strategy.trial?.[key] !== undefined).map(key => [key, strategy.trial[key]]))}));
    const differences = [];
    for (const group of ['configuration', 'trial_policy']) {
      const keys = [...new Set(configs.flatMap(config => Object.keys(config[group])))].sort();
      for (const key of keys) {
        const values = configs.map(config => config[group][key]);
        if (!same(values)) differences.push({label:group + '.' + key, cells:values.map(value => ({
          text: value === undefined ? 'Not specified' : JSON.stringify(stable(value)), tone:''}))});
      }
    }
    return {strategies:selected, rows, differences,
      sameStart: selected.length >= 2 && starts.every(value => numeric(value) && value > 0) && same(starts),
      sameFunding: selected.length >= 2 && funding.every(numeric) && same(funding)};
  }
  function create({anchor, onSelectionChanged}) {
    let ids = parseSelection(location.search), strategies = [], opened = ids.length >= 2;
    const tray = document.createElement('section');
    tray.className = 'comparison-tray';
    tray.setAttribute('aria-label', 'Compare strategy instances');
    tray.innerHTML = '<div class="comparison-bar"><div><b>Compare instances</b><span id="comparison-count">Select 2–4 cards</span></div>' +
      '<div class="comparison-actions"><button type="button" id="comparison-toggle" aria-controls="instance-comparison" aria-expanded="false" disabled>Compare</button><button type="button" id="comparison-clear" disabled>Clear</button></div></div>' +
      '<div id="comparison-selection" class="comparison-selection"></div><p id="comparison-note" role="status"></p>';
    const panel = document.createElement('section');
    panel.id = 'instance-comparison';
    panel.className = 'instance-comparison';
    panel.hidden = true;
    panel.innerHTML = '<div class="comparison-heading"><div><p class="eyebrow">Side-by-side evidence</p><h2>Compare selected instances</h2></div>' +
      '<button type="button" id="copy-comparison-link">Copy comparison link</button></div>' +
      '<p id="comparison-context" class="comparison-context"></p>' +
      '<p class="comparison-limit">Each column covers its own all-time run, not a common evaluation window. These are descriptive results, not a controlled test or a ranking. Open marks can change; fees are already included in P&amp;L. Missing values stay unknown.</p>' +
      '<div class="comparison-scroll" tabindex="0" role="region" aria-label="Instance results; scroll horizontally for more columns"><table id="comparison-table"></table></div>' +
      '<details class="comparison-rules"><summary id="comparison-rules-summary">Configuration differences</summary><p>Recorded configuration and version-specific policy fields. Unspecified is not the same as disabled. Signal implementations may require more than these settings.</p>' +
      '<div class="comparison-scroll" tabindex="0" role="region" aria-label="Configuration differences; scroll horizontally for more columns"><table id="comparison-rules-table"></table></div></details>' +
      '<p class="comparison-limit">Win rate needs closed-trade counts and loss sizes for context. Repeated coins and shared market periods are not independent evidence. Open an instance for its full analysis, copy actions and research download.</p>';
    anchor.before(tray, panel);
    const find = id => document.getElementById(id);
    const note = find('comparison-note');
    let chipsMarkup = '';
    function syncUrl() { history.replaceState(null, '', selectionUrl(location.href, ids)); }
    function table(model, rows, label) {
      return '<caption class="comparison-caption">' + escape(label) + '</caption><thead><tr><th scope="col">Metric / rule</th>' +
        model.strategies.map(strategy => '<th scope="col"><a href="/engine/' + encodeURIComponent(strategy.id) + '">' + escape(strategy.label) + ' ↗</a></th>').join('') +
        '</tr></thead><tbody>' + rows.map(row => '<tr><th scope="row">' + escape(row.label) + '</th>' +
          row.cells.map(cell => '<td class="' + cell.tone + '">' + escape(cell.text) + '</td>').join('') + '</tr>').join('') + '</tbody>';
    }
    function render() {
      const available = ids.map(id => strategies.find(strategy => strategy.id === id)).filter(Boolean);
      find('comparison-count').textContent = ids.length ? ids.length + ' / 4 selected · kept across filters' : 'Select 2–4 cards';
      find('comparison-toggle').disabled = available.length < 2;
      find('comparison-toggle').textContent = opened && available.length >= 2 ? 'Hide comparison' : 'Compare (' + available.length + ')';
      find('comparison-toggle').setAttribute('aria-expanded', String(opened && available.length >= 2));
      find('comparison-clear').disabled = !ids.length;
      const markup = ids.map(id => {
        const strategy = strategies.find(item => item.id === id), label = strategy?.label || id + ' · unavailable';
        return '<button type="button" data-remove="' + escape(id) + '" title="Remove ' + escape(label) + '" aria-label="Remove ' + escape(label) + '"><span>' + escape(label) + '</span><i aria-hidden="true">×</i></button>';
      }).join('');
      if (markup !== chipsMarkup) { find('comparison-selection').innerHTML = markup; chipsMarkup = markup; }
      panel.hidden = !(opened && available.length >= 2);
      if (!panel.hidden) {
        const model = buildComparison(available);
        find('comparison-context').textContent = (model.sameStart ? 'Same recorded start' : 'Different or missing start dates') + ' · ' +
          (model.sameFunding ? 'Same starting balance' : 'Different or missing starting balances') + '. ' +
          (available.length < ids.length ? (ids.length - available.length) + ' selected instance(s) unavailable; not shown.' : 'Values refresh with public telemetry.');
        find('comparison-table').innerHTML = table(model, model.rows, 'All-time results and key rules');
        find('comparison-rules-summary').textContent = 'Configuration differences · ' + model.differences.length;
        find('comparison-rules-table').innerHTML = model.differences.length ? table(model, model.differences, 'Only differing recorded fields') : '<caption>No differences in the recorded configuration and policy fields.</caption>';
      }
      onSelectionChanged?.();
    }
    function toggle(id) {
      if (!validId(id)) return;
      if (ids.includes(id)) ids = ids.filter(value => value !== id);
      else if (ids.length < 4) ids.push(id);
      else { note.textContent = 'Select up to four instances. Remove one to add another.'; return; }
      note.textContent = '';
      if (ids.length < 2) opened = false;
      syncUrl(); render();
    }
    find('comparison-selection').addEventListener('click', event => {
      const button = event.target.closest('[data-remove]');
      if (button) { toggle(button.dataset.remove); find('comparison-clear').focus(); }
    });
    find('comparison-toggle').addEventListener('click', () => { opened = !opened; render(); });
    find('comparison-clear').addEventListener('click', () => { ids = []; opened = false; note.textContent = ''; syncUrl(); render(); });
    find('copy-comparison-link').addEventListener('click', async () => {
      try { await window.PumpWizardHandoff.copy(selectionUrl(location.href, ids)); note.textContent = 'Comparison link copied. It opens these instances with live results, not a frozen snapshot.'; }
      catch { note.textContent = 'Could not copy the link. You can copy the page address instead.'; }
    });
    return {has:id => ids.includes(id), selected:() => [...ids], toggle,
      update:items => { strategies = items; render(); },
      restore:() => { ids = parseSelection(location.search); opened = ids.length >= 2; note.textContent = ''; render(); }};
  }
  const api = {parseSelection, selectionUrl, buildComparison, create};
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  if (typeof window !== 'undefined') window.PumpWizardComparison = api;
})();
