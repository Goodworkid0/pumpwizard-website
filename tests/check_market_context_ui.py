"""Browser check for the public market selector and non-live Raydium view."""
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
    expect(page.locator('.market-context-control select')).to_have_value('raydium-cpmm')
    expect(page.locator('.market-context-panel')).to_be_visible()
    expect(page.locator('.market-context-panel')).to_contain_text('Published simulation P&L')
    expect(page.locator('.market-context-panel')).to_contain_text('Not enabled')
    assert page.locator('.portfolio').is_hidden()
    assert not any('/api/public/' in request for request in requests), requests
    assert page.locator('.site-nav a[href*="market=raydium-cpmm"]').count() >= 3
    page.goto(URL + '/engine?market=raydium-cpmm', wait_until='domcontentloaded')
    expect(page.locator('.market-context-panel')).to_be_visible()
    expect(page.locator('.market-context-panel')).to_contain_text('Raydium CPMM')
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
