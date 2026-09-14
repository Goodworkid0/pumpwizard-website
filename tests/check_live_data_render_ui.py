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
    expect(page.locator('#range-today')).to_have_attribute('aria-selected', 'true')
    expect(page.locator('#position-count')).not_to_have_text('0 open positions')
    assert page.locator('#allocations .allocation-link').count() > 0
    expect(page.locator('#allocations .allocation-link').first).to_contain_text('Today P&L')
    assert page.locator('#positions .position-row').count() > 0
    page.locator('#range-all').click()
    expect(page.locator('#allocations .allocation-link').first).to_contain_text('Today P&L')
    page.goto(URL + '/engine?market=pumpfun', wait_until='domcontentloaded')
    expect(page.locator('#strategy-sort')).to_have_value('pnl_24h_usd', timeout=30000)
    expect(page.locator('#strategy-cards .lab-card').first).to_contain_text('Wallet / Today P&L')
    assert not errors, errors
    browser.close()
    print('Pump.fun Live Data populated-render check passed')
