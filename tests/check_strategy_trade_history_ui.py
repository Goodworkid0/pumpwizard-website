"""Read-only check that every closed trade is accessible through bounded pages."""
from playwright.sync_api import sync_playwright, expect

URL = 'http://127.0.0.1:8765'

with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path='/usr/bin/chromium', headless=True,
        args=['--no-sandbox', '--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    public = page.request.get(URL + '/api/public/summary', timeout=30000).json()
    strategy = next(item for item in public['strategies'] if item['report']['closed_count'] > 0)
    endpoint = URL + '/api/public/strategies/' + strategy['id'] + '/trades'
    first = page.request.get(endpoint+'?limit=200', timeout=30000).json()
    trades = list(first['trades']); cursor = first['next_before']
    while cursor:
        data = page.request.get(endpoint+'?limit=200&snapshot_max_id='+str(first['snapshot_max_id'])+'&before='+str(cursor), timeout=30000).json()
        trades.extend(data['trades']); cursor=data['next_before']
    assert len(trades) == first['total']
    assert len({row['id'] for row in trades}) == len(trades)
    page.goto(URL + '/engine/' + strategy['id'], wait_until='domcontentloaded')
    expect(page.locator('#closed-count')).to_contain_text('showing 1–', timeout=30000)
    assert page.locator('#closed-positions tr').count() == min(25,len(trades))
    browser.close()
    print('complete paginated instance trade-history UI check passed')
