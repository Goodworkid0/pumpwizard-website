"""Read-only browser and byte-range checks; never downloads the full archive."""
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

URL=os.environ.get('PUMP_TEST_URL','http://127.0.0.1:8765').rstrip('/')
with sync_playwright() as p:
    browser=p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,args=['--no-sandbox','--disable-dev-shm-usage'])
    page=browser.new_page(viewport={'width':1440,'height':1000}); errors=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    if os.environ.get('PUMP_TEST_LOCAL_ASSETS'):
        def css(route): route.fulfill(path=str(Path(__file__).resolve().parents[1]/'web/research.css'),content_type='text/css')
        page.route('**/research.css',css)
    page.goto(URL+'/research',wait_until='domcontentloaded')
    link=page.locator('#replay-package-list a').first
    expect(link).to_be_visible(timeout=30000)
    archive=link.get_attribute('href')
    response=page.request.get(URL+archive,headers={'Range':'bytes=0-1023'})
    assert response.status==206 and len(response.body())==1024 and response.body()[:2]==b'PK'
    response=page.request.head(URL+archive)
    assert response.status==200 and int(response.headers['content-length'])>1000
    assert page.request.get(URL+archive,headers={'Range':'bytes=1-0'}).status==416
    page.set_viewport_size({'width':390,'height':900})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Research overflow'
    page.goto(URL+'/engine/t2-persistent-combined',wait_until='domcontentloaded')
    expect(page.locator('.strategy-export-options summary')).to_be_visible(timeout=30000)
    page.locator('.strategy-export-options summary').click()
    expect(page.locator('.replay-package-options a')).to_be_visible(timeout=30000)
    assert page.locator('.replay-package-options a').get_attribute('href')==archive
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Instance overflow'
    assert not errors,errors
    browser.close()
    print('Replay UI passed: discoverability, instance link, byte ranges, HEAD, invalid-range rejection, mobile layout and no JavaScript errors.')
