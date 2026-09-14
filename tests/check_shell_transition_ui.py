"""Browser check that public routes reveal only the completed shared shell."""
import os

from playwright.sync_api import expect, sync_playwright


URL = os.environ.get('PUMP_TEST_URL', 'http://127.0.0.1:8765').rstrip('/')


with sync_playwright() as playwright:
    browser = playwright.chromium.launch(
        executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'],
    )
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.add_init_script("localStorage.setItem('pumpwizard.welcome.last-shown', String(Date.now()))")
    page.goto(URL + '/?market=raydium-cpmm', wait_until='domcontentloaded')
    assert not page.locator('body').get_attribute('data-shell-pending')
    expect(page.locator('.site-header.market-shell')).to_be_visible()
    expect(page.locator('.brand-name')).to_have_text('PumpWizard Lab')
    assert page.locator('.market-toggle-option small').count() == 0
    assert page.locator('.market-context-panel').count() == 0
    expect(page.locator('.community-cta')).to_contain_text('Visit Community')
    expect(page.locator('.history-export-link')).to_have_attribute('href', '/downloads?market=raydium-cpmm')
    assert page.locator('link[href="/shell-transition.css"]').count() == 1
    assert page.locator('link[href="/market-context.css"]').count() == 1
    assert page.locator('.public-freshness').count() == 0

    with page.expect_navigation(wait_until='domcontentloaded'):
        page.locator('.site-nav').get_by_role('link', name='Lab Research').click()
    assert 'market=raydium-cpmm' in page.url
    assert not page.locator('body').get_attribute('data-shell-pending')
    expect(page.locator('.site-header.market-shell')).to_be_visible()
    expect(page.locator('.market-toggle-option.raydium-cpmm')).to_have_attribute('aria-pressed', 'true')
    assert page.locator('.public-freshness').count() == 0
    browser.close()
    print('shared-shell transition and public freshness checks passed')
