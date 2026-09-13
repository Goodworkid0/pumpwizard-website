"""Read-only UI smoke test for the Research lab page."""
from playwright.sync_api import sync_playwright, expect

URL = 'http://127.0.0.1:8765'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL + '/research', wait_until='domcontentloaded')
    expect(page.locator('h1')).to_contain_text('Understand the strategy')
    expect(page.locator('.constraint-panel')).to_contain_text('Modeled execution')
    expect(page.locator('.execution-conditions')).to_contain_text('Modeled failed attempts')
    expect(page.locator('#tracked-coins')).not_to_have_text('—', timeout=30000)
    expect(page.locator('#outcome-windows progress').first).to_be_visible(timeout=30000)
    assert page.locator('a[href="/engine"]').count() >= 2
    for width in (768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('horizontal overflow', width)
    assert not errors, errors
    browser.close()
    print('research rework UI checks passed')
