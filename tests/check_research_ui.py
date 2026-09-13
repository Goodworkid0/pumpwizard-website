"""Read-only browser smoke against the running research pilot."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
URL='http://127.0.0.1:8765'

with sync_playwright() as playwright:
    browser=playwright.chromium.launch(executable_path='/usr/bin/chromium',headless=True,
        args=['--no-sandbox','--disable-dev-shm-usage'])
    page=browser.new_page(viewport={'width':1440,'height':1000});errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(URL+'/engine')
    expect(page.locator('#strategy-count')).to_have_text('139',timeout=30000)
    assert page.locator('.lab-card').count()==24
    expect(page.locator('#matrix-summary')).to_contain_text('1,000')
    first=page.locator('.lab-card').first.get_attribute('href')
    page.locator('#next-page').click()
    assert page.locator('.lab-card').first.get_attribute('href')!=first
    expect(page.locator('#page-info')).to_contain_text('25–48')
    page.locator('#family-filter').select_option('persistent')
    assert page.locator('.lab-card').count()==10
    page.locator('#strategy-search').fill('missing-cohort-xyz')
    expect(page.locator('#strategy-cards')).to_have_text('No experiments match these filters.')
    page.locator('#strategy-search').fill('')
    page.locator('#strategy-sort').select_option('pnl_24h_usd')
    assert page.locator('.lab-card').count()==10
    detail=page.locator('.lab-card').first.get_attribute('href')
    page.screenshot(path=str(ROOT/'exports/research-pilot-strategies.png'),full_page=True)
    for width in (390,320):
        page.set_viewport_size({'width':width,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'),('strategies overflow',width)
    page.set_viewport_size({'width':1440,'height':1000})
    page.goto(URL+detail)
    expect(page.locator('#strategy-name')).to_contain_text('Persistent leader',timeout=30000)
    expect(page.locator('#thesis')).to_contain_text('both halves')
    assert page.locator('.site-nav a').count()==3
    page.goto(URL+'/research')
    expect(page.locator('#tracked-coins')).not_to_have_text('—',timeout=30000)
    expect(page.locator('#quality-summary')).to_contain_text('late')
    assert page.locator('progress').count()==7
    assert page.locator('[style]').count()==0
    page.goto(URL+'/acp')
    expect(page.locator('#matrix-info')).to_contain_text('100 funded',timeout=30000)
    assert page.locator('#matrix-select option').count()==1000
    assert page.locator('.composer').first.evaluate("e=>getComputedStyle(e).display")=='grid'
    page.goto(URL)
    expect(page.locator('.allocation-link').first).to_be_visible(timeout=30000)
    assert page.locator('.allocation-link').count()<=12
    assert page.locator('.site-header a[href="/acp"]').count()==0
    assert not errors,errors
    result=page.request.get(URL+'/api/public',timeout=30000).json()
    matrix=result['experiment_matrix'];assert matrix['funded']==100 and matrix['staged']==900
    assert len(result['strategies'])==139
    assert all(s['report']['initial_usd']==500 for s in result['strategies'])
    browser.close()
    print(json.dumps({'browser_checks':'passed','strategies':len(result['strategies']),
        'experiment_matrix':matrix,'javascript_errors':errors},indent=2))
