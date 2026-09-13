"""Read-only browser coverage for the public research JSON export."""
import json
from pathlib import Path
import sys
from urllib.parse import urlparse,parse_qs
from playwright.sync_api import sync_playwright, expect
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper.public_read import brief,catalog

URL = 'http://127.0.0.1:8765'

def snapshot_download(page, button):
    with page.expect_download(timeout=30000) as event:
        button.click()
    download = event.value
    assert download.failure() is None
    assert download.suggested_filename.startswith('pumpwizard-')
    with open(download.path()) as exported:
        data = json.load(exported)
    assert data['schema'] == 'pumpwizard.research-snapshot/v1'
    rows = data['closed_positions']
    assert len(rows) == data['public_telemetry']['closed_count']
    assert data['strategy_analysis']['evidence']['complete']
    assert abs(sum(row['pnl_usd'] for row in rows) - data['strategy_analysis']['closed']['net_pnl_usd']) < 0.001
    assert data['evidence_manifest']['closed_positions_count'] == len(rows)
    return data

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width':1440, 'height':1000}, accept_downloads=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    public = page.request.get(URL+'/api/public/summary', timeout=30000).json()
    strategy = max(public['strategies'], key=lambda s: s['report']['closed_count'])
    strategy = page.request.get(URL+'/api/public/strategies/'+strategy['id'], timeout=30000).json()['strategies'][0]
    # Freeze public reads to keep polling/new closes from making this UI test flaky.
    endpoint = URL+'/api/public/strategies/'+strategy['id']+'/trades'
    history = page.request.get(endpoint+'?limit=200', timeout=30000).json()
    cursor = history['next_before']
    while cursor:
        extra=page.request.get(endpoint+'?limit=200&snapshot_max_id='+str(history['snapshot_max_id'])+'&before='+str(cursor), timeout=30000).json()
        history['trades'].extend(extra['trades']); cursor=extra['next_before']
    strategy['report']['closed_count'] = len(history['trades'])
    generation={'catalog':public['strategies'], 'activity':{'instances':len(public['strategies']),'dead':0,'events_24h':0,'entries_24h':0,'open_positions':0}}
    page.route('**/api/public/catalog?*', lambda route: route.fulfill(json=catalog(generation,parse_qs(urlparse(route.request.url).query))))
    page.route('**/api/public/strategies/'+strategy['id'], lambda route: route.fulfill(json={'strategies':[strategy],'open_positions':[],'closed_positions':[]}))
    history_url = '**/api/public/strategies/'+strategy['id']+'/trades?*'
    def history_page(route):
        query=parse_qs(urlparse(route.request.url).query); limit=int(query.get('limit',['100'])[0]); before=int(query.get('before',[str(2**63-1)])[0])
        rows=[row for row in history['trades'] if row['id']<before]
        route.fulfill(json={**history,'trades':rows[:limit],'next_before':rows[limit-1]['id'] if len(rows)>limit else None})
    page.route(history_url, history_page)
    page.goto(URL+'/engine/'+strategy['id'], wait_until='domcontentloaded')
    page.locator('.strategy-export-options > summary').click()
    button = page.locator('#download-research')
    expect(button).to_be_visible(timeout=30000)
    data = snapshot_download(page, button)
    assert data['strategy']['id'] == strategy['id']
    assert data['closed_positions'] == history['trades']
    expect(page.locator('#configuration-copy-note')).to_have_text('Research download started')
    for width in [390, 320]:
        page.set_viewport_size({'width':width,'height':900})
        page.wait_for_timeout(250)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), page.evaluate('({width:innerWidth, overflowing:[...document.querySelectorAll("body *")].filter(e=>e.getBoundingClientRect().right>innerWidth+1).map(e=>[e.tagName,e.className,e.id,Math.round(e.getBoundingClientRect().right)]).slice(0,20)})')
    page.unroute(history_url)
    page.route(history_url, lambda route: route.fulfill(status=503, body='unavailable'))
    downloads = []
    page.on('download', lambda download: downloads.append(download))
    button.click()
    expect(page.locator('#configuration-copy-note')).to_contain_text('Download unavailable', timeout=30000)
    expect(button).to_be_enabled()
    assert not downloads
    page.unroute(history_url)
    page.route(history_url, history_page)
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(URL+'/engine', wait_until='domcontentloaded')
    # Select the same instance after the all-time P&L sort.
    page.locator('#strategy-search').fill(strategy['id'])
    card = page.locator('.lab-card').filter(has=page.locator('a[href="/engine/'+strategy['id']+'"]'))
    expect(card).to_be_visible(timeout=30000)
    snapshot_download(page, card.locator('.download-research'))
    for width in [390, 320]:
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    assert not errors, errors
    browser.close()
    print('Research downloads passed: instance/card, complete ledger, failure state, and mobile widths.')
