const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const predicates = [['app.js', 'isDeadStrategy'], ['strategies.js', 'isDeadInstance']].map(([file, name]) => {
  const source = fs.readFileSync(path.join(__dirname, '../web', file), 'utf8');
  const helper = source.match(new RegExp('function ' + name + '\\(report\\) \\{[\\s\\S]*?\\n\\}'));
  assert.ok(helper);
  return vm.runInNewContext(helper[0] + '; ' + name);
});
test('below $50 counts regardless of open trades, pending orders, cash or order size', () => {
  for (const dead of predicates) {
    for (const equity_usd of [-10, 0, 10, 49, 49.999, 50, 50.01, 500]) {
      assert.equal(dead({equity_usd}), equity_usd < 50);
      assert.equal(dead({equity_usd, cash_usd: 0, positions: [{}], pending_count: 1, config: {order_usd: 1}}), equity_usd < 50);
    }
  }
});
test('invalid or missing balances are not treated as dead', () => {
  for (const dead of predicates) {
    for (const value of [undefined, null, {}, ...[null, undefined, NaN, Infinity, -Infinity, '49', false].map(equity_usd => ({equity_usd}))]) {
      assert.equal(dead(value), false);
    }
  }
});
