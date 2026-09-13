"""Browser integration for bounded public reads and existing copy/download flows."""
import json
import os
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, expect

URL=os.environ.get('PUMP_TEST_URL','http://127.0.0.1:8765').rstrip('/')
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    context=browser.new_context(viewport={'width':1440,'height':1000},permissions=['clipboard-read','clipboard-write'],accept_downloads=True)
    page=context.new_page(); errors=[]; paths=[]
    if os.environ.get('PUMP_TEST_LOCAL_ASSETS'):
        web=Path(__file__).resolve().parents[1]/'web'
        def local_asset(route):
            name=Path(urlparse(route.request.url).path).name
            file=web/name
            if file.is_file() and file.suffix in ('.js','.css'):
                route.fulfill(path=str(file),content_type='text/javascript' if file.suffix=='.js' else 'text/css')
            else: route.continue_()
        page.route('**/*',local_asset)
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:paths.append(urlparse(request.url).path))
    page.goto(URL+'/engine',wait_until='domcontentloaded')
    try: expect(page.locator('.lab-card')).to_have_count(24,timeout=30000)
    except AssertionError:
        print('Engine diagnostics:', errors, page.locator('main').inner_text()[:1600],flush=True)
        raise
    first=page.locator('.compare-instance').first.get_attribute('data-strategy-id')
    page.locator('.compare-instance').first.click()
    page.locator('#next-page').click()
    expect(page.locator('#page-info')).to_contain_text('25–48',timeout=30000)
    second=page.locator('.compare-instance').first.get_attribute('data-strategy-id')
    assert first!=second
    page.locator('.compare-instance').first.click()
    expect(page.locator('#comparison-toggle')).to_be_enabled(timeout=30000)
    page.locator('#comparison-toggle').click()
    expect(page.locator('#comparison-table thead th')).to_have_count(3,timeout=30000)
    page.locator('#strategy-search').fill(first)
    expect(page.locator('#page-info')).to_contain_text('1–',timeout=30000)
    expect(page.locator('#comparison-table thead th')).to_have_count(3)
    page.locator('#copy-comparison-link').click()
    expect(page.locator('#comparison-note')).to_contain_text('link copied')
    page.goto(URL+'/engine/early-frequency',wait_until='domcontentloaded')
    expect(page.locator('#closed-count')).to_contain_text('showing 1–25',timeout=30000)
    assert page.locator('#closed-positions tr').count()==25
    first_token=page.locator('#closed-positions tr').first.inner_text()
    page.locator('#history-next').click()
    expect(page.locator('#closed-count')).to_contain_text('showing 26–50',timeout=30000)
    assert page.locator('#closed-positions tr').first.inner_text()!=first_token
    page.locator('#history-previous').click()
    expect(page.locator('#closed-count')).to_contain_text('showing 1–25')
    expect(page.locator('#copy-agent-prompt')).to_be_visible()
    expect(page.locator('#copy-agent-config')).to_be_hidden()
    expect(page.locator('#download-research')).to_be_hidden()
    expect(page.locator('.analysis-grid')).to_be_hidden()
    expect(page.locator('.analysis-summary > div')).to_have_count(4)
    assert page.locator('#strategy-analysis').bounding_box()['height'] < 320
    page.locator('.analysis-details > summary').focus()
    page.keyboard.press('Enter')
    expect(page.locator('.analysis-grid')).to_be_visible()
    page.locator('.analysis-load').click()
    expect(page.locator('.analysis-state')).to_have_text('Descriptive evidence',timeout=30000)
    expect(page.locator('.analysis-details')).to_have_attribute('open','')
    page.locator('#copy-agent-prompt').click()
    expect(page.locator('#configuration-copy-note')).to_contain_text('Prompt copied',timeout=30000)
    text=page.evaluate('navigator.clipboard.readText()')
    handoff=json.loads(text.split('BEGIN PUMPWIZARD SNAPSHOT\n')[1].split('\nEND PUMPWIZARD SNAPSHOT')[0])
    assert handoff['strategy_analysis']['evidence']['complete']
    assert handoff['public_endpoints']['telemetry_api'] == URL+'/api/public/strategies/early-frequency'
    assert page.request.get(handoff['public_endpoints']['telemetry_api']).ok
    page.locator('.strategy-export-options > summary').click()
    page.locator('#copy-agent-config').click()
    expect(page.locator('#configuration-copy-note')).to_have_text('Configuration copied')
    assert json.loads(page.evaluate('navigator.clipboard.readText()'))['strategy']['id']=='early-frequency'
    with page.expect_download(timeout=30000) as event:page.locator('#download-research').click()
    with open(event.value.path()) as f:download=json.load(f)
    assert len(download['closed_positions'])==download['public_telemetry']['closed_count']
    assert len({row['id'] for row in download['closed_positions']})==len(download['closed_positions'])
    for width in [390,320]:
        page.set_viewport_size({'width':width,'height':900});page.wait_for_timeout(250)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.goto(URL+'/',wait_until='domcontentloaded')
    expect(page.locator('#hero-strategy-count')).not_to_have_text('—',timeout=30000)
    expect(page.locator('#strategies-list .strategy')).to_have_count(4,timeout=30000)
    page.goto(URL+'/research',wait_until='domcontentloaded')
    expect(page.locator('#tracked-coins')).not_to_have_text('—',timeout=30000)
    page.goto(URL+'/contact',wait_until='domcontentloaded')
    community=page.request.get(URL+'/api/public/community')
    assert community.ok
    for key, selector in [('x_url','#x-link'),('telegram_url','#telegram-link')]:
        url=community.json().get(key)
        if url: expect(page.locator(selector)).to_have_attribute('href',url,timeout=30000)
    for endpoint in ['/api/public','/api/state','/api/acp/community','/acp']:
        assert page.request.get(URL+endpoint).status==404
    # Delayed publications must be visibly distinguished from fresh ones.
    data=page.request.get(URL+'/api/public/catalog').json();data['_snapshot']['stale']=True
    page.route('**/api/public/catalog?*',lambda route:route.fulfill(json=data))
    page.goto(URL+'/engine',wait_until='domcontentloaded')
    expect(page.locator('#public-freshness')).to_contain_text('Delayed public snapshot',timeout=30000)
    assert '/api/public' not in paths, paths
    assert not errors,errors
    browser.close()
    print('Public read UI passed: server paging, comparison, history, lazy analysis, copy/download, mobile, summary/feed, and stale labels.')
