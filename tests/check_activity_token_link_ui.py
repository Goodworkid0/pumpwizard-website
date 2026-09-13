"""Read-only check that the header activity token is inspectable on Solscan."""
from playwright.sync_api import sync_playwright, expect

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.goto('http://127.0.0.1:8765/', wait_until='domcontentloaded')
    link = page.locator('.live-event a.live-token-link')
    expect(link).to_be_visible(timeout=30000)
    expect(link).to_have_attribute('href', __import__('re').compile(r'^https://solscan\.io/token/.+'))
    expect(link).to_have_attribute('target', '_blank')
    browser.close()
    print('activity token link UI check passed')
