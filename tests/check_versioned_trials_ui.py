"""Read-only browser checks against the activated, locally served trial pilot."""
import json
from playwright.sync_api import sync_playwright, expect

URL = 'http://127.0.0.1:8765'


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}); errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(URL+'/strategies?version=trials')
    expect(page).to_have_url(URL+'/engine?version=trials')
    expect(page.get_by_role('heading', name='Agent-driven strategies on live market data')).to_be_visible()
    method = page.locator('.engine-method')
    expect(method).not_to_have_attribute('open', '')
    method.locator('summary').click()
    expect(method).to_have_attribute('open', '')
    expect(method).to_contain_text('How the live-market simulation works')
    expect(method).to_contain_text('Observe & validate')
    expect(method).to_contain_text('Model the fill')
    expect(method).to_contain_text('not a price prediction')
    expect(method).to_contain_text('no wallet is connected')
    expect(method.get_by_role('link', name='Read the full methodology and limitations →')).to_have_attribute('href', '/legal')
    expect(page.get_by_role('button', name='All Instances', exact=True)).to_be_visible()
    expect(page.locator('#active-agents')).to_have_text('211', timeout=30000)
    expected_dead = page.evaluate("""async () => {
        const data = await (await fetch('/api/public/summary', {cache: 'no-store'})).json();
        return data.strategies.filter(({report}) => {
            return Number.isFinite(report?.equity_usd) && report.equity_usd < 50;
        }).length;
    }""")
    expect(page.locator('#dead-instances')).to_have_text(str(expected_dead), timeout=30000)
    expect(page.locator('.dead-instance-metric')).to_have_attribute('title', 'Current total wallet value below $50 (cash plus marked open positions). Includes all instances, even with open or pending trades.')
    expect(page.locator('#search-summary')).to_have_text('72 results')
    expect(page.get_by_role('button', name='Versioned comparison pilots')).to_have_attribute('aria-pressed', 'true')
    assert page.locator('.trial-badge').count() == 24
    const_card = page.locator('.lab-card').first
    expect(const_card.get_by_role('button', name='Copy config')).to_be_visible()
    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {writeText: value => { window.__copiedCardConfiguration = value; return Promise.resolve(); }}
    })""")
    const_card.get_by_role('button', name='Copy config').click()
    expect(const_card.locator('.card-copy-note')).to_have_text('Configuration copied')
    card_handoff = json.loads(page.evaluate('window.__copiedCardConfiguration'))
    assert card_handoff['schema'] == 'pumpwizard.strategy-handoff/v1'
    assert card_handoff['configuration']['mode'] == 'paper_only'
    assert card_handoff['public_endpoints']['strategy_page'].endswith('/' + card_handoff['strategy']['id'])
    page.locator('#family-filter').select_option('persistent')
    expect(page.locator('#search-summary')).to_have_text('8 results')
    page.locator('#strategy-sort').select_option('closed_win_rate')
    assert page.locator('.lab-card').count() == 8
    page.locator('#strategy-search').fill('V2 ownership telemetry')
    expect(page.locator('#search-summary')).to_have_text('1 result')
    expect(page.locator('.trial-badge')).to_contain_text('V2 ownership telemetry')
    page.locator('#clear-search').click()
    page.get_by_role('button', name='Original strategies', exact=True).click()
    expect(page.locator('#search-summary')).to_have_text('10 results')
    assert page.locator('.trial-badge').count() == 0
    page.locator('#family-filter').select_option('')
    expect(page.locator('#search-summary')).to_have_text('139 results')
    page.get_by_role('button', name='Versioned comparison pilots').click()
    for width in (1440, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('engine overflow', width)
    page.set_viewport_size({'width': 390, 'height': 900})
    method.locator('summary').click()
    expect(method).not_to_have_attribute('open', '')
    method.locator('summary').click()
    expect(method).to_have_attribute('open', '')
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(URL+'/engine/t1-persistent-combined')
    expect(page.locator('#strategy-name')).to_contain_text('V2 combined', timeout=30000)
    expect(page.get_by_role('button', name='Copy configuration')).to_be_visible()
    expect(page.get_by_role('button', name='Copy configuration')).to_have_attribute('title', 'Copies public configuration and technical telemetry as JSON')
    assert page.locator('#strategy-configuration-control').evaluate("node => node.parentElement.matches('.strategy-hero aside')")
    page.evaluate("""() => Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: {writeText: value => { window.__copiedConfiguration = value; return Promise.resolve(); }}
    })""")
    page.get_by_role('button', name='Copy configuration').click()
    expect(page.locator('#configuration-copy-note')).to_have_text('Configuration copied')
    handoff = json.loads(page.evaluate('window.__copiedConfiguration'))
    assert handoff['schema'] == 'pumpwizard.strategy-handoff/v1'
    assert handoff['mode'] == 'simulation_only'
    assert handoff['strategy']['id'] == 't1-persistent-combined'
    assert handoff['configuration']['entry_policy'] == 'simulation_unverified'
    assert handoff['public_endpoints']['telemetry_api'] == URL + '/api/public/strategies/t1-persistent-combined'
    assert 'wallet' not in handoff['configuration']
    expect(page.locator('#version-comparison')).to_be_visible()
    assert page.locator('.version-card').count() == 4
    expect(page.locator('.version-card.current strong')).to_have_text('V2 combined')
    expect(page.locator('#signal-rules')).to_contain_text('Unverified ownership')
    expect(page.locator('#risk-rules')).to_contain_text('V2 net-value profit trail')
    expect(page.locator('#risk-rules')).to_contain_text('V2 ownership telemetry')
    expect(page.locator('#version-comparison')).to_contain_text('Original historical results are not mixed')
    page.screenshot(path='/tmp/pump-trial-comparison-desktop.png', full_page=True)
    for width in (768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('detail overflow', width)
    page.screenshot(path='/tmp/pump-trial-comparison-mobile.png', full_page=True)
    page.locator('.version-card').filter(has=page.get_by_text('V2 control', exact=True)).click()
    expect(page.locator('#strategy-name')).to_contain_text('V2 control', timeout=30000)
    expect(page.locator('#signal-rules')).to_contain_text('Unverified ownership')
    expect(page.locator('#risk-rules')).not_to_contain_text('V2 net-value profit trail')
    page.locator('.version-heading a').click()
    expect(page.locator('#strategy-name')).to_have_text('Persistent leader · $100k–$250k · 5m/7d', timeout=30000)
    assert page.locator('.version-card').count() == 4
    page.goto(URL+'/engine/t2-persistent-combined')
    expect(page.locator('#strategy-name')).to_contain_text('V3 combined', timeout=30000)
    expect(page.locator('#version-comparison')).to_contain_text('V3 entry & exit comparison')
    expect(page.locator('#risk-rules')).to_contain_text('V3 entry confirmation')
    expect(page.locator('#risk-rules')).to_contain_text('V3 break-even protection')
    assert not errors, errors
    print(json.dumps({'browser_checks': 'passed', 'originals': 139, 'trial_instances': 72,
        'viewports': [1440, 768, 390, 320], 'javascript_errors': errors}, indent=2))
    browser.close()
