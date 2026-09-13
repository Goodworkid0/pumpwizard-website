"""Read-only local UI check; synthetic exhausted bots exist in this browser only."""
import json
import re
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}); errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    url = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
    if os.environ.get('PUMP_TEST_LOCAL_ASSETS'):
        web = Path(__file__).resolve().parents[1] / 'web'
        page.route(url+'/', lambda route: route.fulfill(path=str(web/'index.html'), content_type='text/html'))
        page.route('**/app.js*', lambda route: route.fulfill(path=str(web/'app.js'), content_type='text/javascript'))
    page.goto(url+'/')
    counter = page.locator('#dead-strategies')
    expect(counter).not_to_have_text('—', timeout=30000)
    page.route('**/api/public/summary', lambda route: route.abort())
    expected = page.evaluate('ui.data.strategies.filter(s => isDeadStrategy(s.report)).length')
    expect(counter).to_have_text(str(expected))
    assert page.locator('.metric-grid > .metric').count() == 5
    expect(page.locator('.metric-grid > .metric').last).to_contain_text('Dead strategies')
    expect(counter.locator('..')).to_have_attribute('title', re.compile(r'Current total wallet value below \$50'))
    page.locator('.portfolio').screenshot(path='/tmp/pump-dead-counter-desktop.png')
    for width in (390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        overflow = page.locator('.portfolio').evaluate('''element => ({
          width: element.clientWidth, scroll: element.scrollWidth,
          children: [...element.querySelectorAll('*')].filter(child => child.getBoundingClientRect().right > element.getBoundingClientRect().right)
            .slice(0, 12).map(child => ({tag: child.tagName, id: child.id, className: child.className, width: child.getBoundingClientRect().width}))
        })''')
        assert overflow['scroll'] <= overflow['width'], (width, overflow)
        expect(counter).to_be_visible()
    # The synthetic telemetry is never sent to the service or its ledgers.
    page.evaluate('''() => {
      const data = structuredClone(ui.data);
      data.strategies.forEach(s => Object.assign(s.report, {
        equity_usd: 500, cash_usd: 500, positions: [], pending_count: 0,
        config: {...s.report.config, order_usd: 10}
      }));
      data.strategies[0].report.equity_usd = 0;
      Object.assign(data.strategies[1].report, {equity_usd: 49.99, positions: [{}]});
      Object.assign(data.strategies[2].report, {equity_usd: 49, pending_count: 1});
      data.strategies[2].report.config.order_usd = 50;
      Object.assign(data.strategies[3].report, {equity_usd: 50, cash_usd: 0, positions: [{mark_value_usd: 50}]});
      Object.assign(data.strategies[4].report, {cash_usd: 0, pending_count: 1});
      render(data);
    }''')
    expect(counter).to_have_text('3')
    expect(counter).to_have_class('down')
    for period in ('today', '7d', '30d', 'all'):
        page.locator('#range-'+period).click()
        expect(counter).to_have_text('3')
    if not os.environ.get('PUMP_TEST_LOCAL_ASSETS'):
        page.unroute('**/api/public/summary')
        page.goto(url+'/engine')
        expect(page.locator('#dead-instances')).not_to_have_text('—', timeout=30000)
        expected_engine = page.evaluate('''async () => {
          const data = await (await fetch('/api/public/summary')).json();
          return data.strategies.filter(s => Number.isFinite(s.report.equity_usd) && s.report.equity_usd < 50).length;
        }''')
        expect(page.locator('#dead-instances')).to_have_text(str(expected_engine))
    assert not errors, errors
    print(json.dumps({'home_counter': 'passed', 'live_dead_count': expected,
        'synthetic_dead_count': 3, 'current_count_preserved_across_ranges': True,
        'desktop_and_mobile': 'passed', 'javascript_errors': errors}, indent=2))
    browser.close()
