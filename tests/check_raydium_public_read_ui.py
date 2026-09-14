"""Read-only smoke check for the isolated Raydium Live Data projection."""
from playwright.sync_api import expect, sync_playwright


URL = 'http://127.0.0.1:8765'
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
                                         args=['--no-sandbox'])
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL + '/?market=raydium-cpmm', wait_until='domcontentloaded')
    expect(page.locator('#wallet-equity')).to_have_text('500 USDC', timeout=30000)
    expect(page.locator('#hero-capital')).to_have_text('500 USDC')
    expect(page.locator('#count')).to_contain_text('1 strategies')
    expect(page.locator('#strategies-list')).to_contain_text('NEAR / USDC observed trend')
    page.goto(URL + '/engine?market=raydium-cpmm', wait_until='domcontentloaded')
    expect(page.locator('#strategy-cards')).to_contain_text('NEAR / USDC observed trend', timeout=30000)
    expect(page.locator('#active-agents')).to_have_text('1')
    expect(page.locator('#strategy-cards')).to_contain_text('500 USDC')
    assert not errors, errors
    browser.close()
print('Raydium public-read populated-render check passed')
