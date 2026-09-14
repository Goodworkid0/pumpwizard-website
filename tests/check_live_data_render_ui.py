"""Read-only regression check for the populated Pump.fun Live Data view."""
import os

from playwright.sync_api import expect, sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'],
    )
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script("localStorage.setItem('pumpwizard.welcome.last-shown', String(Date.now()))")
    page.goto(URL + '/?market=pumpfun', wait_until='domcontentloaded')
    expect(page.locator('#wallet-equity')).not_to_have_text('—', timeout=30000)
    expect(page.locator('#position-count')).not_to_have_text('0 open positions')
    assert page.locator('#allocations .allocation-link').count() > 0
    assert page.locator('#positions .position-row').count() > 0
    assert not errors, errors
    browser.close()
    print('Pump.fun Live Data populated-render check passed')
