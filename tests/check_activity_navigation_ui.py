"""A navigation must not replay the most recent live-activity toast."""
import json
import os
import time

from playwright.sync_api import sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
feed = {'strategy_count': 1, 'event': {'action': 'fill_entry', 'strategy_label': 'Navigation check',
    'symbol': 'CHECK', 'mint': 'C' * 44, 'at': int(time.time()), 'entry_cap': 24000}}


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.add_init_script("localStorage.setItem('pumpwizard.welcome.last-shown', String(Date.now()))")
    page.route('**/api/public/live-feed', lambda route: route.fulfill(
        status=200, content_type='application/json', body=json.dumps(feed)))
    page.goto(URL + '/', wait_until='domcontentloaded')
    page.wait_for_timeout(250)
    assert page.locator('.activity-toast').count() == 0
    page.goto(URL + '/research?market=raydium-cpmm', wait_until='domcontentloaded')
    page.wait_for_timeout(250)
    assert page.locator('.activity-toast').count() == 0
    browser.close()
    print('activity notifications do not replay on navigation')
