"""Read-only browser smoke test for the launched community coin page."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
COIN = 'https://pump.fun/coin/B6tt2AMXTs3Fv8FaJd2eNut676YnaoKXzeSEf7Xqpump'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    if os.environ.get('PUMP_TEST_LOCAL_ASSETS'):
        page.route('**/affiliate', lambda route: route.fulfill(path=str(ROOT/'web/affiliate.html'),content_type='text/html'))
        page.route('**/affiliate.css', lambda route: route.fulfill(path=str(ROOT/'web/affiliate.css'),content_type='text/css'))
    page.goto(URL + '/affiliate', wait_until='networkidle')
    expect(page.locator('h1')).to_have_text('Support the PumpWizard lab.')
    expect(page.locator('.launch-profile-head')).to_contain_text('Live on Pump.fun')
    expect(page.locator('main')).not_to_contain_text('Pre-launch')
    expect(page.locator('main')).not_to_contain_text('Launch setup')
    expect(page.locator('.site-nav a[href="/affiliate"]')).to_have_attribute('aria-current', 'page')
    create = page.get_by_role('link', name='Buy or trade on Pump.fun')
    expect(create).to_be_visible()
    expect(create).to_have_attribute('target', '_blank')
    expect(create).to_have_attribute('href', COIN)
    expect(page.locator('.launch-links a').first).to_have_attribute('href', COIN)
    expect(page.locator('.coin-mint code')).to_have_text(COIN.rsplit('/',1)[1])
    expect(page.locator('.affiliate-note')).to_contain_text('risk of losing your entire purchase')
    expect(page.locator('#goal')).to_contain_text('Turn public simulations')
    for width in (768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('horizontal overflow', width)
    assert not errors, errors
    page.screenshot(path='/tmp/pumpwizard-coin-mobile.png',full_page=True)
    page.set_viewport_size({'width':1440,'height':1000})
    page.screenshot(path='/tmp/pumpwizard-coin-desktop.png',full_page=True)
    browser.close()
    print('affiliate UI checks passed')
