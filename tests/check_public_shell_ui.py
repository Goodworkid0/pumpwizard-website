"""Read-only visual-shell consistency check across public routes."""
import os
from playwright.sync_api import sync_playwright

URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')
ROUTES = ('/', '/engine', '/research', '/downloads', '/affiliate', '/socials', '/contact', '/legal')

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    for route in ROUTES:
        page.goto(URL + route, wait_until='domcontentloaded')
        page.wait_for_function("() => Boolean(document.querySelector('link[href=\"/page-shell.css\"]')?.sheet)")
        width = page.locator('main').evaluate('element => Math.round(element.getBoundingClientRect().width)')
        assert width == 1280, (route, width)
        page.set_viewport_size({'width': 390, 'height': 900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('horizontal overflow', route)
        page.set_viewport_size({'width': 1440, 'height': 900})
    browser.close()
    print('public shell consistency checks passed')
