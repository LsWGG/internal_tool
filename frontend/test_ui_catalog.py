from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', lambda route: route.abort())
    page.goto('http://127.0.0.1:5174/')
    page.locator('.catalog-card').first.wait_for()
    assert page.locator('.catalog-card').count() == 15
    assert page.locator('.catalog-group').count() == 6
    assert len(set(page.locator('.catalog-card').evaluate_all('(items)=>items.map(x=>x.hash)'))) == 15
    fixed_top = page.locator('.catalog-head').bounding_box()['y']
    for group in ['geo','database','documents','images','collection','delivery']:
        page.locator(f'.catalog-nav button').nth(['geo','database','documents','images','collection','delivery'].index(group)).click()
        page.wait_for_function('''group=>{
          const box=document.querySelector('.catalog-scroll'),target=box.querySelector('[data-group="'+group+'"]');
          return Math.abs(target.getBoundingClientRect().top-box.getBoundingClientRect().top-parseFloat(getComputedStyle(box).paddingTop))<3;
        }''', arg=group)
        assert page.locator('.catalog-head').bounding_box()['y'] == fixed_top
    page.locator('.catalog-nav button').first.click()
    page.screenshot(path='/private/tmp/portal-refined-desktop.png', full_page=True)
    page.get_by_role('searchbox').fill('HEIC')
    assert page.locator('.catalog-card').count() == 2
    page.get_by_role('searchbox').fill('不存在工具123')
    assert page.locator('.catalog-empty').is_visible()
    page.get_by_role('button', name='查看全部工具').click()
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.screenshot(path='/private/tmp/portal-refined-mobile.png', full_page=True)
    routes=['map','propzone','gjb','shp','database','es','nebula','pdf','word-batch','md-word','image-convert','image-metadata','crawler','trending','docker']
    workspaces={'map':'.workspace','propzone':'.propzone-workspace','gjb':'.gjb-workspace','shp':'.shp-workspace','es':'.es-workspace','nebula':'.es-workspace','pdf':'.pdf-workspace','word-batch':'.wb-workspace','md-word':'.md-word-workspace','image-convert':'.image-convert-workspace','docker':'.docker-workspace'}
    measurements=[]
    for route in routes:
        page.set_viewport_size({'width':1440,'height':1000})
        page.evaluate('(route)=>location.hash=route',route)
        page.wait_for_timeout(250)
        page.locator('.back-home').wait_for()
        page.wait_for_function('!document.querySelector(".catalog-page")')
        title = page.locator('main h1').first
        title.wait_for()
        measurements.append((route,title.evaluate('(el)=>getComputedStyle(el).fontSize')))
        assert title.evaluate('(el)=>getComputedStyle(el).fontSize') == '29px', route
        if route in workspaces:
            layout=page.locator(workspaces[route]).evaluate('(el)=>({columns:getComputedStyle(el).gridTemplateColumns,height:el.getBoundingClientRect().height})')
            assert layout['columns'].split()[0] == '400px', (route,layout)
            assert abs(layout['height']-580)<1, (route,layout)
        assert page.evaluate('window.scrollY===0'), route
        page.screenshot(path='/private/tmp/ui-'+route+'.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        measurements.append((route, page.evaluate('document.documentElement.scrollWidth-innerWidth')))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), route
    print(measurements)
    print('PAGE ERRORS',errors)
    assert not errors
    page.unroute_all(behavior='wait')
    browser.close()
