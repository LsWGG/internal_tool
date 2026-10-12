"""智能网页采集页的浏览器回归。

采集目标那个控件不是模板渲染出来的：模板只给一个 <input>，真正的多行框由
App.vue 的 syncCrawlerTargetInput() 在运行时用 replaceWith 换上去（推特/X 的
用户信息、历史推文，以及抖音/TikTok 的账号信息、账号视频）。所以选择器必须同时
覆盖 input 和 textarea，断言也必须走「控件可见 → 能输入 → 按钮解禁」这条真实路径，
而不是去数 DOM 里有没有 input —— 数 input 会得到 0，看起来像坏了，其实是换掉了。

这一段曾经被误判成 bug：style.css 里有一条
  .crawler-page.crawler-account-mode .crawler-keyword-step input{visibility:hidden}
看着像是把账号输入框藏了。但加上这个类的条件与触发上面那次替换的条件完全相同，
规则生效时 input 已经被换成 textarea 了，因此它从未产生过效果。现在规则和
crawler-account-mode 类都已删除，这个用例守住的是「那条路径一直是能用的」。

跑法：dev server 必须在 5174。
    cd frontend && ../.venv/bin/python test_crawler_browser.py
"""
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:5174/'
TWITTER_MODES = ['用户信息', '历史推文', '关键词推文']
TARGET = '.crawler-keyword-step input, .crawler-keyword-step textarea'
# 视频类来源（yt-dlp / news）走的是「粘贴地址」那一步，控件挂在 .crawler-url-step 下。
URL_TARGET = '.crawler-url-step input, .crawler-url-step textarea'


def open_crawler(page):
    page.goto(BASE, wait_until='domcontentloaded')
    page.wait_for_timeout(120)
    page.evaluate("location.hash='crawler'")
    page.locator('.crawler-source-cards button').first.wait_for()
    page.wait_for_timeout(200)


def pick_source(page, label):
    page.locator('.crawler-source-cards button').filter(has_text=label).first.click()
    page.wait_for_timeout(150)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.route('**/api/**', lambda route: route.abort())

    open_crawler(page)

    # --- 结构 -------------------------------------------------------------
    assert page.locator('.crawler-source-cards button').count() == 9
    assert page.locator('.crawler-progress li span').all_inner_texts() == \
        ['数据来源', '采集目标', '提取内容', '保存方式']
    # 四个步骤各有一个编号标题：来源 / 目标 / 字段 / 输出。
    steps = '.crawler-config .crawler-step>b, .crawler-config .crawler-url-step>b, ' \
            '.crawler-config .crawler-fields-head>b, .crawler-config .crawler-finish>b'
    assert page.locator(steps).count() == 4

    # --- 推特 / X：三种模式下采集目标控件都必须可用 -------------------------
    pick_source(page, '推特')
    assert page.locator('.crawler-twitter-modes button').count() == 3
    assert '用户信息' in page.locator('.crawler-twitter-modes button.active').inner_text()

    for mode_label in TWITTER_MODES:
        page.locator('.crawler-twitter-modes button').filter(has_text=mode_label).first.click()
        # syncCrawlerTargetInput 在 50ms 后替换节点，等过它再断言。
        page.wait_for_timeout(350)

        field = page.locator(TARGET)
        assert field.count() == 1, f'{mode_label}: 期望恰好一个采集目标控件，实际 {field.count()}'
        assert field.first.is_visible(), f'{mode_label}: 采集目标控件不可见'
        assert field.first.evaluate('(el)=>getComputedStyle(el).visibility') == 'visible', mode_label

        submit = page.locator('.crawler-submit')
        assert submit.is_disabled(), f'{mode_label}: 空输入时按钮应禁用'
        field.first.fill('@OpenAI')
        page.wait_for_timeout(150)
        assert not submit.is_disabled(), f'{mode_label}: 填写账号后按钮应解禁'
        assert page.locator('.crawler-summary.is-ready').is_visible(), mode_label
        field.first.fill('')
        page.wait_for_timeout(120)
        assert submit.is_disabled(), f'{mode_label}: 清空后按钮应恢复禁用'

    # 两种「最多采集」输入框不能同时出现（同绑 crawlerMaxItems，各有各的 v-if）。
    # 只有它们是 max="500"，每次访问间隔和 SQL 端口都不是，所以这个选择器是准的。
    limit = page.locator('.crawler-config input[type=number][max="500"]')
    assert limit.count() == 1, f'最多采集输入框出现了 {limit.count()} 个'

    # 采集页在 390px 下不应横向溢出。
    page.set_viewport_size({'width': 390, 'height': 844})
    page.wait_for_timeout(250)
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), '采集页 390px 横向溢出'
    page.screenshot(path='/tmp/crawler-390.png', full_page=True)
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.wait_for_timeout(200)
    page.screenshot(path='/tmp/crawler-1440.png', full_page=True)

    # --- 预览按钮不能被第一次点击「点飞」 ---------------------------------
    # previewCrawlerData 会把预览面板 scrollIntoView 到视口上沿。面板在文档里排在按钮之后，
    # 于是按钮被顺手挤出屏幕（实测约 -56px），用户改完字段再点就落在空处，看起来像「只能点一次」。
    # 这里全程用鼠标坐标点击、不手动滚动，走的就是用户那条路径。
    # 路由按注册的倒序匹配，后注册的这条会盖住上面那条 abort。
    preview_calls = []

    def fulfill_fields(route):
        route.fulfill(status=200, content_type='application/json',
                      body='[{"name":"title","label":"标题"},{"name":"url","label":"链接"}]')

    def fulfill_preview(route):
        preview_calls.append(1)
        route.fulfill(status=200, content_type='application/json',
                      body='{"sampled":1,"columns":["title","url"],'
                           '"rows":[{"title":"样本","url":"https://a.com"}],"mode":"cards"}')

    page.route('**/api/crawler/fields*', fulfill_fields)
    page.route('**/api/crawler/data-preview', fulfill_preview)

    pick_source(page, '抖音')          # 换来源会重新拉字段，这一步之后按钮才可用
    pick_source(page, '推特')
    page.locator(TARGET).first.fill('@OpenAI')
    page.wait_for_timeout(250)

    preview = page.locator('.crawler-discovery-box .crawler-data-preview-btn')
    assert not preview.is_disabled(), '填好目标后预览按钮仍禁用'
    for attempt in range(3):
        box = preview.bounding_box()
        page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
        page.wait_for_timeout(350)
        top, bottom = preview.evaluate(
            '(el)=>{const r=el.getBoundingClientRect();return [r.top,r.bottom];}')
        assert top >= 0 and bottom <= 1000, \
            f'第 {attempt + 1} 次预览后按钮被挤出视口：top={top} bottom={bottom}'
        assert preview.evaluate(
            """(el)=>{const r=el.getBoundingClientRect();
                const hit=document.elementFromPoint(r.left+r.width/2,r.top+r.height/2);
                return !!hit&&(hit===el||el.contains(hit));}"""), \
            f'第 {attempt + 1} 次预览后按钮中心点被别的元素挡住'
    assert len(preview_calls) == 3, f'连点 3 次只发出了 {len(preview_calls)} 次预览请求'
    assert page.locator('.crawler-data-preview').is_visible(), '预览面板没露面'

    # --- 保存方式里的媒体选项 -------------------------------------------
    # 这组选项只在「会下载文件」的来源上出现，所以先切到 yt-dlp 视频源。
    # 它们默认全关 —— 不勾任何东西时行为与过去一致，先守住这一点再逐项确认。
    pick_source(page, 'yt-dlp')
    page.locator(URL_TARGET).first.fill('https://vimeo.com/123456')
    page.wait_for_timeout(250)

    finish = page.locator('.crawler-finish')
    finish.scroll_into_view_if_needed()
    page.wait_for_timeout(200)

    labels = finish.locator('label').filter(has_text='下载内容')
    assert labels.count() == 1, '没找到「下载内容」选项'
    mode_select = labels.locator('select')

    assert mode_select.input_value() == 'video', '下载内容默认应该是视频文件'
    assert finish.locator('label').filter(has_text='字幕').locator('select').input_value() == 'none'
    assert finish.locator('label').filter(has_text='封面图').locator('select').input_value() == 'none'
    # 两个复选框默认都不勾：勾了就会改变默认产出。
    assert not finish.locator('label:has-text("写入标题等元数据") input[type=checkbox]').is_checked()
    assert not finish.locator('label:has-text("跳过已下载的视频") input[type=checkbox]').is_checked()

    # 切成 MP3 后，「嵌入视频」字幕必须被收回来：MP3 容器嵌不了字幕，
    # yt-dlp 只会静默跳过并留下旁挂文件，界面上不能留一个不会生效的选择。
    subtitle_select = finish.locator('label').filter(has_text='字幕').locator('select')
    subtitle_select.select_option('embed')
    page.wait_for_timeout(120)
    assert subtitle_select.input_value() == 'embed', '先确认嵌入是可选的'
    mode_select.select_option('audio_mp3')
    page.wait_for_timeout(200)
    assert subtitle_select.input_value() == 'sidecar', \
        f'切到 MP3 后字幕应自动降级为单独文件，实际是 {subtitle_select.input_value()}'
    # Playwright 的 is_disabled() 只认 button/select/input/textarea，对 <option> 一律返回 False，
    # 所以这里直接读 DOM 属性。
    embed_disabled = subtitle_select.locator('option[value=embed]').evaluate('(el)=>el.disabled')
    assert embed_disabled, 'MP3 模式下「嵌入视频」应当是禁用状态'
    assert 'MP3' in finish.locator('label').filter(has_text='字幕').locator('small').inner_text()

    # 概要行要跟着改口，不能还写着「ZIP + 720p 视频」。
    summary = page.locator('.crawler-summary').inner_text()
    assert 'MP3 音频' in summary, f'切换到 MP3 后概要仍写着：{summary}'
    assert '720p 视频' not in summary, f'切换到 MP3 后概要仍写着：{summary}'

    mode_select.select_option('video')
    page.wait_for_timeout(200)
    assert '720p 视频' in page.locator('.crawler-summary').inner_text()

    # 两个勾选项要待在自己那一行里，且不随模式换列。
    # 曾经它们和上面四组「标题 + 下拉框」挤同一个 3 列网格，于是切换到 MP3（视频清晰度一消失）
    # 整行就重排、它们跟着左移。位置飘加上长得像个裸复选框，看起来不像能点的控件。
    flags = finish.locator('.crawler-media-flags')
    assert flags.count() == 1, '两个勾选项没有被收进独立的一行'
    assert flags.locator('.crawler-media-check').count() == 2
    video_box = flags.bounding_box()
    mode_select.select_option('audio_mp3')
    page.wait_for_timeout(250)
    mp3_box = flags.bounding_box()
    assert abs(video_box['x'] - mp3_box['x']) < 1 and abs(video_box['width'] - mp3_box['width']) < 1, \
        f'勾选项在 MP3 模式下挪了位置：{video_box} → {mp3_box}'
    mode_select.select_option('video')
    page.wait_for_timeout(200)

    # 「最多采集」那个数字框不能因为新控件多出来一个。
    assert page.locator('.crawler-config input[type=number][max="500"]').count() == 1

    assert errors == [], errors
    browser.close()
    print('crawler browser checks passed')
