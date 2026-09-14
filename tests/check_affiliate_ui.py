"""Read-only browser smoke test for the coming-soon community coin page."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
TELEGRAM = 'https://t.me/PumpWizardFun'

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
    expect(page.locator('h1')).to_have_text('Building the lab, together.')
    expect(page.locator('.launch-profile-head')).to_contain_text('Coming soon')
    expect(page.locator('.site-nav a[aria-current="page"]')).to_have_text('$PUMPWIZARD')
    telegram = page.get_by_role('link', name='Join our Telegram')
    expect(telegram).to_be_visible()
    expect(telegram).to_have_attribute('target', '_blank')
    expect(telegram).to_have_attribute('href', TELEGRAM)
    expect(page.locator('#support')).to_contain_text('There is no official coin')
    expect(page.locator('#support')).to_contain_text('Creator rewards will feed the lab.')
    for width in (768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('horizontal overflow', width)
    assert not errors, errors
    page.screenshot(path='/tmp/pumpwizard-coin-mobile.png',full_page=True)
    page.set_viewport_size({'width':1440,'height':1000})
    page.screenshot(path='/tmp/pumpwizard-coin-desktop.png',full_page=True)
    browser.close()
    print('affiliate UI checks passed')
