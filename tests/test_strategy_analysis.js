const test = require('node:test');
const assert = require('node:assert/strict');
const {analyze, withHistory, researchSnapshot, fetchHistory} = require('../web/strategy-analysis.js');
const historyResponse = (id, rows) => ({ok:true, text:async () => JSON.stringify({strategy_id:id, trades:rows.map((row,i)=>row ? {...row,id:row.id ?? rows.length-i} : row), total:rows.length, snapshot_max_id:rows.length, next_before:null})});
const trade = (mint, pnl_usd, exit_reason = 'stop_loss') => ({mint, pnl_usd, exit_reason});

test('history paging freezes a boundary and rejects duplicate/cycling pages', async () => {
  const previous = global.fetch;
  const first = {strategy_id:'test',total:3,snapshot_max_id:3,next_before:2,trades:[{id:3,...trade('A',1)},{id:2,...trade('B',2)}]};
  const second = {strategy_id:'test',total:3,snapshot_max_id:3,next_before:null,trades:[{id:1,...trade('C',3)}]};
  let calls = 0;
  try {
    global.fetch = async url => {
      const page = calls++ === 0 ? first : second;
      if (calls === 2) { assert.match(url,/snapshot_max_id=3/); assert.match(url,/before=2/); }
      return {ok:true,text:async()=>JSON.stringify(page)};
    };
    assert.deepEqual((await fetchHistory('test')).trades.map(row=>row.id),[3,2,1]);
    for (const bad of [{...second,snapshot_max_id:4},{...second,trades:[{id:2,...trade('B',1)}]},
      {...second,next_before:2}, {...second,total:10001}]) {
      calls = 0;
      global.fetch = async () => ({ok:true,text:async()=>JSON.stringify(calls++===0 ? first : bad)});
      await assert.rejects(fetchHistory('test'));
    }
  } finally { global.fetch = previous; }
});

test('closed attribution, concentration and ledger accounting stay distinct', () => {
  const rows = [trade('A', 10), trade('A', -2), trade('B', -3), trade('C', 5), trade('D', 0)];
  const report = {closed_count: 5, pnl_usd: 20, realized_net_pnl_usd: 12, unrealized_pnl_usd: 8,
    fees_usd: 2, failed_fees_usd: 1, max_drawdown: 0, started_at: 100, last_at: 86500};
  const before = JSON.stringify({rows, report});
  const a = analyze(report, rows);
  assert.equal(a.evidence.complete, true);
  assert.equal(a.evidence.closed_unique_coins, 4);
  assert.equal(a.evidence.observed_span_days, 1);
  assert.equal(a.closed.net_pnl_usd, 10);
  assert.equal(a.closed.mean_trade_usd, 2);
  assert.equal(a.closed.mean_win_usd, 7.5);
  assert.equal(a.closed.mean_loss_usd, 2.5);
  assert.equal(a.closed.win_rate, 0.4);
  assert.equal(a.closed.profit_factor, 3);
  assert.equal(a.closed.breakeven, 1);
  assert.equal(a.closed.without_largest_winner_usd, 0);
  assert.equal(a.closed.top_coin_winning_pnl_share, 2/3);
  assert.equal(a.drivers[0].net_pnl_usd, 8);
  assert.deepEqual(a.loss_sources, [{exit_reason:'stop_loss', losing_trades:2, loss_usd:5}]);
  assert.equal(a.accounting.total_net_pnl_usd, 20);
  assert.equal(a.accounting.realized_net_pnl_usd, 12);
  assert.equal(a.accounting.max_drawdown_fraction, 0);
  assert.equal(a.next_experiment.title, 'Test performance on new coins');
  assert.equal(JSON.stringify({rows, report}), before);
});

test('partial, missing and invalid records cannot become complete attribution', () => {
  for (const rows of [null, [], [trade('A', 1)], [trade('A', 1), null], [trade('A', NaN), trade('B', 1)]]) {
    const a = analyze({closed_count:2}, rows);
    assert.equal(a.evidence.complete, false);
    assert.equal(a.closed, null);
    assert.deepEqual(a.drivers, []);
    assert.equal(a.next_experiment.title, 'Establish a baseline');
  }
  assert.equal(analyze().accounting.total_net_pnl_usd, null);
  assert.equal(analyze({}, []).evidence.complete, false);
});

test('zero trades, zero wins and no losses have different meanings', () => {
  const empty = analyze({closed_count:0}, []);
  assert.equal(empty.closed.win_rate, null);
  assert.equal(empty.next_experiment.title, 'Establish a baseline');
  const losses = analyze({closed_count:2}, [trade('A', -2), trade('B', -4)]);
  assert.equal(losses.closed.win_rate, 0);
  assert.equal(losses.closed.profit_factor, 0);
  assert.equal(losses.closed.without_largest_winner_usd, null);
  assert.equal(losses.next_experiment.title, 'Test entry confirmation');
  const wins = analyze({closed_count:3}, [trade('A', 1), trade('B', 1), trade('C', 1)]);
  assert.equal(wins.closed.win_rate, 1);
  assert.equal(wins.closed.profit_factor, null);
  assert.equal(wins.next_experiment.title, 'Test sensitivity to execution costs');
});

test('handoff fetch validates identity, retains timestamps and flags snapshot races', async () => {
  const originalFetch = global.fetch;
  const handoff = JSON.stringify({strategy:{id:'test'}, public_telemetry:{closed_count:1, started_at:10, last_observation_at:86410}});
  try {
    global.fetch = async () => historyResponse('test', [trade('A', 2)]);
    const a = JSON.parse(await withHistory(handoff)).strategy_analysis;
    assert.equal(a.evidence.complete, true);
    assert.equal(a.as_of, 86410);
    assert.equal(a.evidence.observed_span_days, 1);
    global.fetch = async () => historyResponse('other', []);
    await assert.rejects(withHistory(handoff), /Invalid.*history/);
    global.fetch = async () => historyResponse('test', []);
    assert.equal(JSON.parse(await withHistory(handoff)).strategy_analysis.evidence.complete, false);
    global.fetch = async () => ({ok:false});
    await assert.rejects(withHistory(handoff), /History unavailable/);
  } finally { global.fetch = originalFetch; }
});

test('research snapshot embeds every closed record without conflating partial sales', async () => {
  const originalFetch = global.fetch;
  const rows = Array.from({length:125}, (_, n) => ({...trade('mint-'+n, n % 2 ? -1 : 2),
    id:125-n, entry_at:100+n, exit_at:200+n, cost_usd:10, symbol:'coin'}));
  const data = {schema:'pumpwizard.strategy-handoff/v1', generated_at:'2026-09-12T00:00:00.000Z',
    mode:'simulation_only', strategy:{id:'test', label:'../../unsafe label'}, configuration:{order_usd:10},
    versioned_trial:{generation:'V3'}, public_telemetry:{closed_count:125, last_observation_at:500,
      recent_trades:[{pnl_usd:99, action:'fill_partial_exit'}]}};
  try {
    global.fetch = async (url, options) => {
      assert.equal(url, '/api/public/strategies/test/trades?limit=200');
      assert.equal(options.cache, 'no-store');
      return historyResponse('test', rows);
    };
    const result = await researchSnapshot(JSON.stringify(data));
    const snapshot = JSON.parse(result.text);
    assert.match(result.filename, /^pumpwizard-test-[\dTZ-]+\.json$/);
    assert.equal(snapshot.schema, 'pumpwizard.research-snapshot/v1');
    assert.deepEqual(snapshot.closed_positions, rows);
    assert.deepEqual(snapshot.configuration, data.configuration);
    assert.deepEqual(snapshot.versioned_trial, data.versioned_trial);
    assert.equal(snapshot.strategy_analysis.closed.net_pnl_usd, 64);
    assert.equal(snapshot.evidence_manifest.closed_positions_count, 125);
    assert.equal(snapshot.evidence_manifest.telemetry_captured_at, data.generated_at);
    assert.equal(snapshot.strategy_analysis.as_of, 500);
    assert.match(snapshot.evidence_manifest.coverage.decisions, /not the complete/);
    assert.match(snapshot.evidence_manifest.coverage.market_inputs, /not included/);
    assert.equal(snapshot.public_telemetry.recent_trades.length, 1);
    assert.equal(data.schema, 'pumpwizard.strategy-handoff/v1');
  } finally { global.fetch = originalFetch; }
});

test('research download refuses incomplete, invalid or cross-instance history', async () => {
  const originalFetch = global.fetch;
  const data = {strategy:{id:'test'}, public_telemetry:{closed_count:1}};
  try {
    for (const trades of [[], [null], [trade('', 1)], [trade('A', null)], [trade('A', 1), trade('B', 2)]]) {
      global.fetch = async () => historyResponse('test', trades);
      await assert.rejects(researchSnapshot(JSON.stringify(data)), /incomplete|Invalid history page/);
    }
    global.fetch = async () => historyResponse('other', [trade('A', 1)]);
    await assert.rejects(researchSnapshot(JSON.stringify(data)), /Invalid.*history/);
    global.fetch = async () => { throw Error('offline'); };
    await assert.rejects(researchSnapshot(JSON.stringify(data)), /offline/);
    await assert.rejects(researchSnapshot(JSON.stringify({strategy:{id:'../other'}})), /Invalid strategy/);
    global.fetch = async () => historyResponse('test', []);
    data.public_telemetry.closed_count = 0;
    const empty = JSON.parse((await researchSnapshot(JSON.stringify(data))).text);
    assert.deepEqual(empty.closed_positions, []);
    assert.equal(empty.strategy_analysis.evidence.status, 'Awaiting closed trades');
  } finally { global.fetch = originalFetch; }
});
