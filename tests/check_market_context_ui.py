"""Browser check for the shared market selector and safe Raydium dashboard shell."""
import os

from playwright.sync_api import expect, sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    requests, errors = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL + '/?market=raydium-cpmm', wait_until='domcontentloaded')
    expect(page.locator('.site-topbar')).to_be_visible()
    expect(page.locator('.site-topbar .solana-mark i')).to_have_count(3)
    expect(page.locator('.site-topbar .live-ticker')).to_be_visible()
    expect(page.locator('.site-navigation-row')).to_be_visible()
    expect(page.locator('.market-context-control select')).to_have_value('raydium-cpmm')
    expect(page.locator('.site-navigation-row .market-context-control')).to_be_visible()
    assert page.locator('.site-nav a').filter(has_text='Legal').count() == 0
    expect(page.locator('footer .footer-links a').filter(has_text='Legal & methodology')).to_be_visible()
    expect(page.locator('.market-context-panel')).to_be_visible()
    expect(page.locator('.market-context-panel')).to_contain_text('Awaiting read model')
    expect(page.locator('.portfolio')).to_be_visible()
    expect(page.locator('.portfolio')).to_contain_text('Observed paper ledger')
    expect(page.locator('#positions')).to_contain_text('No observed-ledger positions')
    assert not any('/api/public/' in request for request in requests), requests
    assert page.locator('.site-nav a[href*="market=raydium-cpmm"]').count() >= 3
    assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--mint').trim()") == '#14f195'
    page.goto(URL + '/engine?market=raydium-cpmm', wait_until='domcontentloaded')
    expect(page.locator('.market-context-panel')).to_be_visible()
    expect(page.locator('.market-context-panel')).to_contain_text('Raydium CPMM')
    expect(page.locator('#strategy-cards')).to_contain_text('No Raydium simulation strategies')
    with page.expect_navigation(wait_until='domcontentloaded'):
        page.locator('.market-context-control select').select_option('pumpfun')
    expect(page.locator('html')).to_have_attribute('data-market', 'pumpfun')
    expect(page.locator('.market-context-panel')).to_be_hidden()
    assert 'market=pumpfun' in page.url
    for width in (390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
    print('market context selector and Raydium validation view passed')
