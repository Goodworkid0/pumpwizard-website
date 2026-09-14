"""Browser check for the explicit localhost-only populated market UI preview."""
import os

from playwright.sync_api import expect, sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.add_init_script("localStorage.setItem('pumpwizard.welcome.last-shown', String(Date.now()))")
    page.goto(URL + '/?market=pumpfun&local_demo=1', wait_until='domcontentloaded')
    expect(page.locator('.local-demo-notice')).to_contain_text('Synthetic localhost UI preview')
    expect(page.locator('#wallet-equity')).to_have_text('$1,069.80')
    expect(page.locator('#positions')).to_contain_text('PUMP-DEMO')
    page.goto(URL + '/?market=raydium-cpmm&local_demo=1', wait_until='domcontentloaded')
    expect(page.locator('.local-demo-notice')).to_be_visible()
    assert page.locator('.market-context-panel').count() == 0
    expect(page.locator('#wallet-equity')).to_contain_text('demo USDC')
    expect(page.locator('#positions')).to_contain_text('RAY-DEMO')
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    browser.close()
    print('local Pump.fun and Raydium populated demo views passed')
