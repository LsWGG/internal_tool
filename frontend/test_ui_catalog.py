from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', lambda route: route.abort())
    page.goto('http://127.0.0.1:5174/')
    page.locator('.catalog-card').first.wait_for()
    # 22 = the built-in tools in toolCatalog.js. The store's own external shortcuts (a second
    # card in 文档处理 and in 开发工具) come from /api/links, which this suite aborts, so they
    # are absent here -- that absence is also why these two counts must stay equal.
    assert page.locator('.catalog-card').count() == 22
    assert page.locator('.catalog-group').count() == 6
    assert len(set(page.locator('.catalog-card').evaluate_all('(items)=>items.map(x=>x.hash)'))) == 22
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
    # Every built-in route, in catalog order. The pages listed below either never had the shared
    # 400/580 workbench or replaced it on purpose (tile-viewer keeps its own 350px column,
    # json/mermaid/compose declare their own grids) -- they are still swept for title size,
    # scroll position and 390px overflow, they just have no entry in `workspaces`.
    routes=['map','tile-viewer','propzone','gjb','shp','dbx','database','es','nebula',
            'pdf-toolbox','pdf','word-batch','md-word','mermaid-export','clean',
            'image-convert','image-metadata','crawler','trending','json','compose-manager','docker']
    workspaces={'map':'.workspace','propzone':'.propzone-workspace','gjb':'.gjb-workspace','shp':'.shp-workspace','es':'.es-workspace','nebula':'.es-workspace','pdf':'.pdf-workspace','word-batch':'.wb-workspace','md-word':'.md-word-workspace','image-convert':'.image-convert-workspace','docker':'.docker-workspace','clean':'.clean-workspace'}
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
        if route == 'clean':
            # The clean page opts out of the shared workbench geometry on purpose. Its config column
            # carries a seven-step form, not three inputs plus a canvas, so the 400x580 box with an
            # inner scrollbar made it unusable; it declares its own grid in CleanTool.vue and needs
            # the config column to grow with the form rather than scroll inside itself. Asserted
            # semantically (wider than the shared 400px, not the shared 580px tall, stage sticky,
            # column not self-scrolling) so future spacing tweaks don't have to chase literals.
            layout=page.locator(workspaces[route]).evaluate('''(el)=>({
              columns:getComputedStyle(el).gridTemplateColumns,
              height:el.getBoundingClientRect().height,
              stage:getComputedStyle(document.querySelector('.clean-stage')).position,
              cfgScroll:document.querySelector('.clean-config').scrollHeight,
              cfgClient:document.querySelector('.clean-config').clientHeight,
            })''')
            assert float(layout['columns'].split()[0].rstrip('px')) >= 560, (route,layout)
            assert abs(layout['height']-580) >= 1, (route,layout)
            assert layout['stage'] == 'sticky', (route,layout)
            assert layout['cfgScroll'] <= layout['cfgClient']+1, (route,layout)
        elif route in workspaces:
            layout=page.locator(workspaces[route]).evaluate('(el)=>({columns:getComputedStyle(el).gridTemplateColumns,height:el.getBoundingClientRect().height})')
            assert layout['columns'].split()[0] == '400px', (route,layout)
            assert abs(layout['height']-580)<1, (route,layout)
        assert page.evaluate('window.scrollY===0'), route
        page.screenshot(path='/private/tmp/ui-'+route+'.png',full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        measurements.append((route, page.evaluate('document.documentElement.scrollWidth-innerWidth')))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), route
    # 3840x2160. Nothing above this line exercises a viewport wider than 1440, so without this
    # block the whole >=1800 branch of ui-scale.css has no automated guard at all. The literals
    # below are the >=3200 tier's contract: --ui-frame:2 makes the content/browse column 2752px
    # (1376x2) and the catalog 2880px over six columns, and the portal is full-bleed (its shell
    # is fixed to 100vw, so the head spans the viewport rather than the 2752 content column).
    page.set_viewport_size({'width':3840,'height':2160})
    page.evaluate('()=>location.hash="home"')
    page.wait_for_timeout(400)
    page.locator('.catalog-card').first.wait_for()
    portal = page.evaluate('''()=>({
      cards: getComputedStyle(document.querySelector('.catalog-cards')).gridTemplateColumns.split(' ').length,
      cardWidth: document.querySelector('.catalog-card').getBoundingClientRect().width,
      headW: document.querySelector('.catalog-head').getBoundingClientRect().width,
      vw: innerWidth,
      overflow: document.documentElement.scrollWidth - innerWidth,
    })''')
    assert portal['cards'] == 6, portal
    assert abs(portal['cardWidth'] - 607) <= 2, portal
    assert abs(portal['headW'] - portal['vw']) <= 1, portal
    assert portal['overflow'] <= 0, portal

    # The workbench selector is scoped per route on purpose. The map page is the one route kept
    # in the DOM by v-show rather than v-if, so on any other route a bare `.workspace` query
    # matches the *hidden* map workbench: the element exists, is display:none, and its height of
    # 0 would sail through a `> 880` assertion.
    quad = []
    for route, ws_sel in [('map','.map-page .workspace'), ('word-batch','.wb-workspace')]:
        page.evaluate('(route)=>location.hash=route',route)
        page.wait_for_timeout(300)
        page.locator('.back-home').wait_for()
        page.wait_for_function('!document.querySelector(".catalog-page")')
        quad.append((route, page.evaluate('''(sel)=>{
          const sec=document.querySelector('main.ui-shell>section');
          const h1=document.querySelector('main h1');
          const ws=document.querySelector(sel);
          return {w:Math.round(sec.getBoundingClientRect().width),
                  h1:parseFloat(getComputedStyle(h1).fontSize),
                  sidebar:getComputedStyle(ws).gridTemplateColumns.split(' ')[0],
                  stage:Math.round(ws.getBoundingClientRect().height),
                  scroll:document.documentElement.scrollHeight-innerHeight,
                  overflow:document.documentElement.scrollWidth-innerWidth};
        }''', ws_sel)))
    for route,m in quad:
        # 2752 = 1376 x --ui-frame 2; the stage is the >=3200 tier's measured 700px-of-chrome
        # offset (2160 - 700), which is what keeps these two pages from scrolling at 4K.
        assert abs(m['w']-2752) <= 2, (route,m)
        assert abs(m['h1']-37.7) < 0.6, (route,m)
        assert m['sidebar'] == '520px', (route,m)
        assert abs(m['stage']-1460) <= 2, (route,m)
        assert m['scroll'] <= 0, (route,m)
        assert m['overflow'] <= 0, (route,m)

    # image-metadata gets a different guard because it has no workbench at all until a file is
    # loaded -- its .metadata-workspace sits behind a v-else. What it does render is the empty
    # state, whose min-height is 480px * --ui-scale: one of the few places a :root token has to
    # reach inside a scoped component to have any effect, so it is the thing worth pinning at 4K.
    page.evaluate('()=>location.hash="image-metadata"')
    page.wait_for_timeout(300)
    page.locator('.metadata-empty').wait_for()
    meta = page.evaluate('''()=>({
      w: Math.round(document.querySelector('.metadata-page').getBoundingClientRect().width),
      empty: Math.round(document.querySelector('.metadata-empty').getBoundingClientRect().height),
      overflow: document.documentElement.scrollWidth - innerWidth,
    })''')
    assert abs(meta['w']-2752) <= 2, meta
    assert abs(meta['empty']-624) <= 2, meta
    assert meta['overflow'] <= 0, meta
    print(quad, meta)
    page.screenshot(path='/private/tmp/portal-refined-4k.png', full_page=True)

    print(measurements)
    print('PAGE ERRORS',errors)
    assert not errors
    page.unroute_all(behavior='wait')
    browser.close()
