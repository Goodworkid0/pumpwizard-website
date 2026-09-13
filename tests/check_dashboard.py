"""Optional browser check against a running dashboard; never places real orders."""
import json
import copy
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

url = sys.argv[1] if len(sys.argv) > 1 else 'http://127.0.0.1:8765'
out = Path(__file__).resolve().parents[1] / 'exports'
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                args=['--no-sandbox', '--disable-dev-shm-usage'], timeout=12000)
    page = browser.new_page(viewport={'width': 1440, 'height': 1100})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(url, wait_until='domcontentloaded', timeout=15000)
    expect(page.locator('#equity')).not_to_have_text('—', timeout=10000)
    assert page.locator('.paper-emblem').inner_text().strip() == 'PAPER MODE'
    assert page.locator('#brief-copy p').count() >= 2

    def response_for(action, **params):
        from urllib.parse import parse_qs, urlparse
        def matches(response):
            parsed = urlparse(response.url)
            q = parse_qs(parsed.query, keep_blank_values=True)
            return parsed.path == '/api/dashboard' and all(q.get(k) == [v] for k, v in params.items())
        with page.expect_response(matches, timeout=10000) as pending:
            action()
        result = pending.value.json()
        page.wait_for_timeout(100)
        return result

    for period in ['today', '7d', '30d', 'live']:
        data = response_for(lambda: page.locator(f'[data-period="{period}"]').click(), period=period)
        assert data['range']['id'] == period
        assert page.locator(f'[data-period="{period}"]').get_attribute('aria-selected') == 'true'
        assert page.locator('#pnl-period').inner_text() == data['range']['label'].upper()
    for outcome, sign in [('winners', 1), ('losers', -1), ('all', 0)]:
        data = response_for(lambda: page.locator(f'[data-outcome="{outcome}"]').click(), outcome=outcome)
        assert not sign or all(t['pnl_usd'] * sign > 0 for t in data['journal']['rows'])
        assert page.locator('#trade-body tr[data-trade-id]').count() == len(data['journal']['rows'])
    for sort, key, reverse in [('best', 'pnl_usd', True), ('worst', 'pnl_usd', False), ('newest', 'exit_at', True)]:
        data = response_for(lambda: page.locator('#trade-sort').select_option(sort), sort=sort)
        values = [t[key] for t in data['journal']['rows']]
        assert values == sorted(values, reverse=reverse)
    data = response_for(lambda: page.locator('#trade-search').fill('no-such-token-test-zyx'), search='no-such-token-test-zyx')
    assert data['journal']['total'] == 0
    assert 'No trades match' in page.locator('#trade-body').inner_text()
    data = response_for(lambda: page.locator('#trade-search').fill(''), search='')
    if data.get('trading_status'):
        expect(page.locator('#trading-health')).to_be_visible()
        expect(page.locator('#trading-health-title')).to_have_text(data['trading_status']['title'])
        expect(page.locator('#last-trade-age')).to_contain_text('Last trade:')
        if data['trading_status']['warning']:
            assert 'entry_blocker' in data['brief']['fact_ids']
    if data.get('account'):
        expect(page.locator('#account')).to_be_visible()
        expect(page.locator('#account-check')).to_have_text('BALANCE VERIFIED')
        assert page.locator('.allocation-card').count()==2
        sheet=data['account']['balance_sheet']
        assert abs(sheet['equity_usd']-sheet['original_capital_usd']-sheet['realized_net_pnl_usd']-sheet['unrealized_pnl_usd'])<1e-6
        if data['account']['statement']['pages']>1:
            data=response_for(lambda:page.locator('#statement-next').click(),ledger_page='2')
            expect(page.locator('#statement-page')).to_contain_text('2 /')
            data=response_for(lambda:page.locator('#statement-prev').click(),ledger_page='1')
        data=response_for(lambda:page.locator('#statement-action').select_option('funding'),ledger_action='funding')
        assert all(r['action'] in ('virtual_funding','allocation_transfer') for r in data['account']['statement']['rows'])
        assert page.locator('#statement-body tr[data-action="allocation_transfer"]').count()==1
        expect(page.locator('#statement-body')).to_contain_text('Account total unchanged')
        data=response_for(lambda:page.locator('#statement-strategy').select_option('ladder'),ledger_strategy='ladder')
        assert all(r['strategy']=='ladder' for r in data['account']['statement']['rows'])
        data=response_for(lambda:page.locator('#statement-search').fill('no-such-ledger-token'),ledger_search='no-such-ledger-token')
        assert data['account']['statement']['total']==0
        data=response_for(lambda:page.locator('#statement-search').fill(''),ledger_search='')
        data=response_for(lambda:page.locator('#statement-strategy').select_option('all'),ledger_strategy='all')
        data=response_for(lambda:page.locator('#statement-action').select_option('all'),ledger_action='all')
        page.locator('#account').screenshot(path=str(out/'account-balance-desktop.png'))
        assert 'cash_after_usd' in page.request.get(url+'/api/account.csv').text().splitlines()[0]
    if data['journal']['rows']:
        page.locator('#trade-body button').first.click()
        assert page.locator('#trade-dialog').is_visible()
        assert 'Net proceeds' in page.locator('#detail-fields').inner_text()
        page.locator('#close-dialog').click()
        assert not page.locator('#trade-dialog').is_visible()
    opened = page.locator('.position-card').evaluate_all('(rows) => rows.map(r => Number(r.dataset.entryAt))')
    assert opened == sorted(opened, reverse=True)
    page.locator('[data-chart="pnl"]').click()
    assert 'Net portfolio movement' in page.locator('#chart-legend').inner_text()
    if data.get('account'):
        assert page.evaluate('chartPoints.map(p=>p.value)')==[p['period_pnl'] for p in data['equity_curve']]
    page.locator('[data-chart="equity"]').click()
    win_arc = page.locator('#donut-win').evaluate('(n) => parseFloat(getComputedStyle(n).strokeDasharray)')
    assert (win_arc > 0) == (data['performance']['wins'] > 0)
    page.locator('#watchlist summary').click()
    assert f"{data['config']['observation_seconds']}s momentum" in page.locator('#long-window-heading').inner_text()
    page.locator('#watchlist summary').click()
    if data.get('brain') and data['brain'].get('coins'):
        page.locator('#brain-body button').first.click()
        assert page.locator('#coin-dialog').is_visible()
        assert 'Ownership evidence' in page.locator('#coin-detail-content').inner_text()
        assert 'First observations above milestones' in page.locator('#coin-detail-content').inner_text()
        page.locator('#close-coin').click()
        symbol = data['brain']['coins'][0]['symbol']
        page.locator('#brain-search').fill('missing-memory-coin-zyx')
        expect(page.locator('#brain-body')).to_contain_text('No coins match')
        page.locator('#brain-search').fill('')
        page.locator('[data-brain-filter="blocked"]').click()
        assert all(x == 'Avoid' for x in page.locator('#brain-body td:nth-child(5) .thesis-tag').all_inner_texts())
        page.locator('[data-brain-filter="all"]').click()
        page.locator('#coin-brain').screenshot(path=str(out/'coin-memory-desktop.png'))
    if data.get('brain',{}).get('runner',{}).get('version')=='early-ladder-v1':
        expect(page.locator('#ladder-steps')).to_contain_text('Sell 40.0%')
        expect(page.locator('#ladder-steps')).to_contain_text('$60,000.00')
        assert page.locator('a[href="/api/ladder.csv"]').count()==1
        assert 'remaining position' in page.locator('#ladder-rules').inner_text()
    page.evaluate('window.scrollTo(0, 0)')
    page.screenshot(path=str(out / 'dashboard-desktop.png'), full_page=True)
    for width in [768, 390, 320]:
        page.set_viewport_size({'width': width, 'height': 844})
        page.wait_for_timeout(100)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Overflow at {width}px'
        if width == 390:
            page.screenshot(path=str(out / 'dashboard-mobile.png'), full_page=True)
    result = {'page_errors': errors, 'equity': page.locator('#equity').inner_text(),
                      'feed': page.locator('#feed-label').inner_text(),
                      'brief': page.locator('#brief-label').inner_text(),
                      'closed_trades': data['journal']['total'],
                      'checks': 'periods, outcomes, sorting, search, details, funding-neutral chart, unified balance, statement filters/pages/export, watchlist, 1440/768/390/320px'}

    # Browser-only fixtures exercise populated positions and multiple journal pages.
    # They intercept reads in a separate tab; no fixture is written to the collector.
    fixture = copy.deepcopy(data)
    fixture['source'] = 'synthetic_demo'
    if fixture.get('trading_status'):
        fixture['trading_status'].update(status='ownership_unavailable',title='Entries waiting for ownership data',warning=True,
            message='Market prices are updating, but the ownership connection is unavailable.')
    fixture['positions'] = []
    for i, pnl in enumerate([4, -3, -20]):
        fixture['positions'].append({'mint': chr(65+i)*44, 'symbol': f'UI TEST {i+1}',
            'entry_at': data['server_at']-(i+1)*60, 'entry_price_usd': 0.00002,
            'exit_price_usd': (20+pnl)/1000000 if i<2 else None,
            'cost_usd': 20, 'mark_value_usd': 20+pnl, 'quantity': 1000000,
            'unrealized_pnl_usd': pnl, 'return_fraction': pnl/20,
            'mark_reason': 'missing_quote' if i==2 else None,
            'status': 'exit_pending' if i==2 else 'open'})
    fixture['portfolio'].update(open_count=3, impaired_count=1, exposure_usd=60,
                                cash_usd=940, equity_usd=981, unrealized_pnl_usd=-19)
    if fixture.get('brain',{}).get('runner',{}).get('version')=='early-ladder-v1':
        r=fixture['brain']['runner']
        r.update(cash_usd=101.2,equity_usd=118.2,pnl_usd=18.2,realized_net_pnl_usd=7.2,
                 unrealized_pnl_usd=11,sale_count=1,closed_count=0,pending_count=0)
        r['positions']=[{'mint':'A'*44,'symbol':'LADDER UI TEST','quantity':600000,'initial_quantity':1000000,
            'remaining_fraction':.6,'cost_usd':10,'remaining_cost_usd':6,'proceeds_usd':11.2,
            'mark_value_usd':17,'total_pnl_usd':18.2,'entry_at':data['server_at']-3600,
            'entry_cap':11000,'last_cap':32000,'entry_price_usd':.00001,'next_target':60000,
            'sold_stages':[0],'status':'open','mark_reason':None}]
        r['trades']=[{'id':1,'mint':'A'*44,'symbol':'LADDER UI TEST','entry_at':data['server_at']-3600,
            'exit_at':data['server_at']-20,'entry_cap':11000,'exit_cap':32000,'entry_price_usd':.00001,
            'exit_price_usd':.000028,'initial_fraction':.4,'cost_usd':4,'proceeds_usd':11.2,
            'pnl_usd':7.2,'exit_reason':'market_cap_target'}]
    fixture_rows = []
    for i in range(27):
        fixture_rows.append({'id': i+1, 'mint': 'A'*44, 'symbol': f'UI ROW {i+1}',
            'entry_at': data['server_at']-i*60-120, 'exit_at': data['server_at']-i*60,
            'entry_price_usd': 0.00002, 'exit_price_usd': 0.000021,
            'cost_usd': 20, 'proceeds_usd': 21, 'pnl_usd': 1, 'return_fraction': 0.05,
            'entry_market_cap_usd': 20000, 'fees_usd': 0.5,
            'hold_seconds': 120, 'exit_reason': 'time_limit'})
    def fixture_response(route):
        from urllib.parse import urlparse, parse_qs
        query = parse_qs(urlparse(route.request.url).query)
        number = int(query.get('page', ['1'])[0])
        response = copy.deepcopy(fixture)
        response['journal'].update(rows=fixture_rows[(number-1)*12:number*12],
                                    total=27, pages=3, page=number)
        route.fulfill(json=response)
    fixture_page = browser.new_page(viewport={'width': 1440, 'height': 1100})
    fixture_page.on('pageerror', lambda error: errors.append(str(error)))
    fixture_page.route('**/api/dashboard?*', fixture_response)
    fixture_page.goto(url, wait_until='domcontentloaded', timeout=10000)
    fixture_page.wait_for_selector('.position-card')
    assert fixture_page.locator('.position-card').count() == 3
    if fixture.get('trading_status'):
        expect(fixture_page.locator('#engine-state')).to_have_text('Awaiting ownership')
        expect(fixture_page.locator('#trading-health')).to_contain_text('ownership connection is unavailable')
    assert fixture_page.locator('.position-card.impaired').count() == 1
    assert 'Exit pending' in fixture_page.locator('.position-card').last.inner_text()
    assert 'Unavailable' in fixture_page.locator('.position-card').last.inner_text()
    if fixture.get('brain',{}).get('runner',{}).get('version')=='early-ladder-v1':
        expect(fixture_page.locator('.ladder-position')).to_contain_text('60.0% of original tokens still held')
        expect(fixture_page.locator('.ladder-position')).to_contain_text('+$18.20')
        expect(fixture_page.locator('.ladder-position')).to_contain_text('Sold 40.0%')
        expect(fixture_page.locator('#ladder-sale-body')).to_contain_text('+$7.20')
        expect(fixture_page.locator('#runner-closed')).to_have_text('0')
        fixture_page.locator('.ladder-position').screenshot(path=str(out/'ladder-position-example.png'))
    fixture_page.locator('#next-page').click()
    expect(fixture_page.locator('#page-number')).to_have_text('2 / 3')
    assert fixture_page.locator('#trade-body tr').count() == 12
    assert fixture_page.locator('#trade-body tr').first.get_attribute('data-trade-id') == '13'
    fixture_page.locator('#next-page').click()
    expect(fixture_page.locator('#page-number')).to_have_text('3 / 3')
    assert fixture_page.locator('#trade-body tr').count() == 3
    assert fixture_page.locator('#next-page').is_disabled()
    fixture_page.locator('#prev-page').click()
    expect(fixture_page.locator('#page-number')).to_have_text('2 / 3')
    fixture_page.set_viewport_size({'width': 390, 'height': 844})
    assert fixture_page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    result['fixture_checks'] = 'populated positions, impaired quote, journal page navigation'
    fixture_page.close()
    print(json.dumps(result))
    assert not errors, errors
    browser.close()
