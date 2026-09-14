"""Browser check for the once-per-day PumpWizard Lab introduction."""
import os

from playwright.sync_api import expect, sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
KEY = 'pumpwizard.welcome.last-shown'


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.goto(URL + '/', wait_until='domcontentloaded')
    expect(page.locator('.lab-welcome')).to_be_visible()
    expect(page.locator('.lab-welcome')).to_contain_text('Isolated $500 per strategy')
    expect(page.locator('.lab-welcome')).to_contain_text('no order is signed or submitted')
    expect(page.locator('.lab-welcome')).to_contain_text('stale or missing quotes')
    page.locator('.lab-welcome-confirm').click()
    expect(page.locator('.lab-welcome')).to_be_hidden()
    assert page.evaluate(f"Number(localStorage.getItem('{KEY}')) > 0")
    page.reload(wait_until='domcontentloaded')
    assert page.locator('.lab-welcome').count() == 0
    page.evaluate(f"localStorage.setItem('{KEY}', String(Date.now() - 86400001))")
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('.lab-welcome')).to_be_visible()
    page.keyboard.press('Escape')
    expect(page.locator('.lab-welcome')).to_be_hidden()
    browser.close()
    print('welcome modal first-visit and 24-hour cadence passed')
