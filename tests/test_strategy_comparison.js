const test = require('node:test');
const assert = require('node:assert/strict');
const {parseSelection, selectionUrl, buildComparison} = require('../web/strategy-comparison.js');

test('selection links deduplicate, validate and cap four ids without losing other filters', () => {
  assert.deepEqual(parseSelection('?compare=a,a,b,../bad,c,d,e'), ['a','b','c','d']);
  assert.deepEqual(parseSelection('?compare=%3Cscript%3E,'+'x'.repeat(41)), []);
  assert.deepEqual(parseSelection(''), []);
  const url = new URL(selectionUrl('https://example.com/engine?version=trials#test', ['b','a','b']));
  assert.equal(url.searchParams.get('compare'), 'b,a');
  assert.equal(url.searchParams.get('version'), 'trials');
  assert.equal(url.hash, '#test');
  assert.equal(new URL(selectionUrl(url.href, [])).searchParams.has('compare'), false);
});
const strategy = (id, changes = {}) => ({id, label:id, report:{started_at:100, last_at:200,
  initial_usd:500, closed_count:0, config:{order_usd:10}, ...changes}});

test('same start/funding require known values; different dates are not normalized away', () => {
  assert.equal(buildComparison([strategy('a'),strategy('b')]).sameStart, true);
  const model = buildComparison([strategy('a'),strategy('b', {started_at:101, initial_usd:1000})]);
  assert.equal(model.sameStart, false);
  assert.equal(model.sameFunding, false);
  assert.equal(buildComparison([strategy('a',{started_at:null}),strategy('b',{started_at:null})]).sameStart, false);
  assert.equal(buildComparison([strategy('a',{initial_usd:null}),strategy('b',{initial_usd:null})]).sameFunding, false);
  assert.equal(buildComparison([strategy('a')]).sameStart, false);
  assert.equal(model.rows.find(row=>row.label==='Started').differs, true);
});

test('no evidence is distinct from zero success and zero losses', () => {
  const model = buildComparison([strategy('empty'), strategy('losses',{closed_count:3,closed_wins:0,closed_win_rate:0,closed_profit_factor:0,max_drawdown:0}),
    strategy('wins',{closed_count:1,closed_win_rate:1,closed_profit_factor:null,closed_gross_profit_usd:4}), strategy('unknown',{closed_count:2})]);
  assert.deepEqual(model.rows.find(row=>row.label==='Closed win rate').cells.map(cell=>cell.text), ['No closed trades','0.0%','100.0%','—']);
  assert.deepEqual(model.rows.find(row=>row.label==='Closed profit factor').cells.map(cell=>cell.text), ['No closed trades','0.00','No closed losses','—']);
  assert.equal(model.rows.find(row=>row.label==='Maximum drawdown').cells[1].text, '0.0%');
  assert.equal(model.rows.find(row=>row.label==='Total net P&L · all time').cells[0].text, '—');
});

test('configuration differences preserve missing vs false, nested targets and trial policies', () => {
  const a = strategy('a',{config:{order_usd:10, targets:[{fraction:0.5, cap:100}], flag:false}});
  const b = strategy('b',{config:{order_usd:10, targets:[{cap:100, fraction:0.5}]}});
  b.trial = {id:'irrelevant', label:'Different label', exits_enabled:true, profit_activation_return:0.2};
  const input = JSON.stringify([a,b]);
  const model = buildComparison([a,b]);
  assert.deepEqual(model.differences.map(row=>row.label), ['configuration.flag','trial_policy.exits_enabled','trial_policy.profit_activation_return']);
  assert.deepEqual(model.differences[0].cells.map(cell=>cell.text), ['false','Not specified']);
  assert.equal(JSON.stringify([a,b]), input);
  b.report.config.targets[0].cap = 200;
  assert.ok(buildComparison([a,b]).differences.find(row=>row.label==='configuration.targets'));
});
