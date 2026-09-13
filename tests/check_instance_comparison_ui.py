"""Read-only Engine comparison checks using a frozen public-report response."""
from urllib.parse import urlparse, parse_qs
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper.public_read import brief, catalog

URL = 'http://127.0.0.1:8765'
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox','--disable-dev-shm-usage'])
    context = browser.new_context(viewport={'width':1440,'height':1000}, permissions=['clipboard-read','clipboard-write'])
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    public = page.request.get(URL+'/api/public/summary', timeout=30000).json()
    assert len(public['strategies']) > 24
    generation={'catalog':public['strategies'], 'activity':{'instances':len(public['strategies']),'dead':0,'events_24h':0,'entries_24h':0,'open_positions':0}}
    page.route('**/api/public/catalog?*', lambda route: route.fulfill(json=catalog(generation,parse_qs(urlparse(route.request.url).query))))
    page.goto(URL+'/engine', wait_until='domcontentloaded')
    expect(page.locator('.compare-instance').first).to_be_visible(timeout=30000)
    expect(page.locator('#comparison-toggle')).to_be_disabled()
    ids = []
    for index in range(2):
        button = page.locator('.compare-instance').nth(index)
        ids.append(button.get_attribute('data-strategy-id'))
        button.click()
        expect(button).to_have_attribute('aria-pressed','true')
        page.wait_for_timeout(300)
    page.locator('#next-page').click()
    expect(page.locator('#page-info')).to_contain_text('25–48',timeout=30000)
    for index in range(2):
        button = page.locator('.compare-instance').nth(index)
        ids.append(button.get_attribute('data-strategy-id'))
        button.click()
        page.wait_for_timeout(300)
    page.locator('.compare-instance').nth(2).click()
    expect(page.locator('#comparison-note')).to_contain_text('up to four')
    assert len(page.locator('#comparison-selection button').all()) == 4
    page.locator('#comparison-toggle').click()
    expect(page.locator('#instance-comparison')).to_be_visible()
    assert page.locator('#comparison-table thead th').count() == 5
    expect(page.locator('#instance-comparison')).to_contain_text('not a common evaluation window')
    page.locator('#strategy-search').fill('no-strategy-matches-this')
    expect(page.locator('#strategy-cards')).to_have_text('No experiments match these filters.')
    assert page.locator('#comparison-table thead th').count() == 5
    page.locator('#copy-comparison-link').click()
    expect(page.locator('#comparison-note')).to_contain_text('Comparison link copied')
    shared = page.evaluate('navigator.clipboard.readText()')
    assert parse_qs(urlparse(shared).query)['compare'][0].split(',') == ids
    page.goto(shared, wait_until='domcontentloaded')
    expect(page.locator('#comparison-table thead th')).to_have_count(5, timeout=30000)
    page.locator('.comparison-rules summary').click()
    expect(page.locator('#comparison-rules-table')).to_be_visible()
    page.evaluate('localStorage.setItem("pumpwizard.activity-muted","true")')
    page.locator('#instance-comparison').screenshot(path='/tmp/instance-comparison-desktop.png')
    for width in [390,320]:
        page.set_viewport_size({'width':width,'height':900})
        page.wait_for_timeout(250)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), width
        region = page.locator('.comparison-scroll').first
        assert region.evaluate('(el)=>el.scrollWidth > el.clientWidth')
        region.focus()
        page.keyboard.press('ArrowRight')
    page.set_viewport_size({'width':1440,'height':1000})
    page.locator('#comparison-clear').click()
    expect(page.locator('#instance-comparison')).to_be_hidden()
    assert 'compare=' not in page.url
    # Unknown IDs remain visible as unavailable and do not borrow another report.
    page.goto(URL+'/engine?compare='+','.join(ids[:2])+',unknown-instance', wait_until='domcontentloaded')
    expect(page.locator('#comparison-table thead th')).to_have_count(3, timeout=30000)
    expect(page.locator('#comparison-selection')).to_contain_text('unknown-instance · unavailable')
    expect(page.locator('#comparison-context')).to_contain_text('1 selected instance(s) unavailable')
    page.locator('[data-remove="unknown-instance"]').click()
    assert 'unknown-instance' not in page.url
    assert not errors, errors
    browser.close()
    print('Comparison passed: selection cap, pagination/filter persistence, sharing/reload, missing IDs, and mobile scroll.')
