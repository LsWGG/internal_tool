import unittest
from unittest.mock import MagicMock

from bs4 import BeautifulSoup

from app.crawler_manager import CrawlerTaskManager


class NewsDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(CrawlerTaskManager)
        self.manager._checkpoint = MagicMock()
        self.manager._update = MagicMock()

    def soup(self, value):
        return BeautifulSoup(value, "html.parser")

    def test_uses_numbered_pagination_and_increments_page_path(self):
        html = '<ul><li>1</li><li><a href="/foreign-relations/2/">2</a></li><li><a href="/foreign-relations/2/">下一页 &gt;&gt;</a></li></ul>'
        self.assertEqual(
            self.manager._next_news_page(
                "https://cn.nytimes.com/foreign-relations/", self.soup(html)
            ),
            "https://cn.nytimes.com/foreign-relations/2/",
        )
        self.assertEqual(
            self.manager._next_news_page(
                "https://example.com/news/page/2/", self.soup("")
            ),
            "https://example.com/news/page/3/",
        )

    def test_directory_listing_gets_page_two_fallback(self):
        self.assertEqual(
            self.manager._next_news_page(
                "https://cn.nytimes.com/foreign-relations/", self.soup("")
            ),
            "https://cn.nytimes.com/foreign-relations/2/",
        )

    def test_excludes_sidebar_and_non_article_sections(self):
        html = '''
          <div class="sectionWrapper">
            <article><h2><a href="/world/20260907/valid-story/">这是一篇有效的国际新闻标题</a></h2></article>
            <div class="hotStory"><h2><a href="/china/20260907/popular/?utm_source=mostviewed-daily">最受欢迎内容不应被采集</a></h2></div>
          </div>
          <aside class="sidebar most-viewed"><h2><a href="/china/20260907/sidebar-story/">侧栏热门文章不应被采集</a></h2></aside>
          <article><h2><a href="/topic/foreign-relations/">专题导航不应被采集</a></h2></article>
          <article><h2><a href="/slideshow/20260907/photos/">幻灯片入口不应被采集</a></h2></article>
        '''
        self.assertEqual(
            self.manager._article_links(
                "https://cn.nytimes.com/foreign-relations/", self.soup(html)
            ),
            ["https://cn.nytimes.com/world/20260907/valid-story/"],
        )

    def test_nytimes_falls_back_when_server_html_has_no_section_wrapper(self):
        html = '''
          <div class="story"><h2><a href="/world/20260906/server-rendered/">服务端页面中的有效新闻标题</a></h2></div>
          <div class="hotStory"><h2><a href="/china/20260907/popular/?utm_source=mostviewed-daily">最受欢迎文章</a></h2></div>
        '''
        self.assertEqual(
            self.manager._article_links(
                "https://cn.nytimes.com/foreign-relations/", self.soup(html)
            ),
            ["https://cn.nytimes.com/world/20260906/server-rendered/"],
        )

    def test_loading_shell_is_detected_and_overlay_is_removed(self):
        shell = self.soup('<html><body><header>豆瓣 douban</header><div class="loading">载入中...</div></body></html>')
        self.assertTrue(self.manager._preview_is_loading_shell(shell))
        self.manager._prepare_generic_preview(shell, "https://movie.douban.com/subject/1292052/")
        self.assertNotIn("载入中", shell.get_text())

    def test_lazy_preview_image_uses_real_source(self):
        soup = self.soup('<img src="blank.gif" data-original="/poster.jpg" style="display:none">')
        self.manager._prepare_generic_preview(soup, "https://movie.douban.com/subject/1292052/")
        image = soup.img
        self.assertEqual(image["src"], "https://movie.douban.com/poster.jpg")
        self.assertIn("visibility:visible", image["style"])

    def test_data_preview_contains_only_selected_fields(self):
        self.manager._discover_news_urls = MagicMock(return_value=["https://example.com/story/1"])
        self.manager._fetch_html = MagicMock(return_value='''
          <html><head><title>测试新闻标题</title><meta name="description" content="不应展示的简介"></head>
          <body><article><p>不应展示的正文</p></article></body></html>
        ''')
        result = self.manager.data_preview({
            "source": "news", "urls": ["https://example.com/news/"],
            "fields": [{"name": "title", "enabled": True}], "sample_size": 3,
        })
        self.assertEqual(result["columns"], ["title"])
        self.assertEqual(set(result["rows"][0]), {"title"})

    def test_data_preview_does_not_restore_defaults_after_clear_all(self):
        with self.assertRaisesRegex(ValueError, "至少一个字段"):
            self.manager.data_preview({
                "source": "news", "urls": ["https://example.com/news/"], "fields": [],
            })

    def test_discovers_requested_fifty_items_across_pages(self):
        def listing(url, _request):
            page = 1
            tail = url.rstrip("/").rsplit("/", 1)[-1]
            if tail.isdigit():
                page = int(tail)
            start = (page - 1) * 20
            return '<div class="sectionWrapper">' + "".join(
                f'<article><h2><a href="/world/20260907/story-{number}/">第 {number} 篇有效新闻文章标题</a></h2></article>'
                for number in range(start + 1, start + 21)
            ) + "</div>"

        self.manager._fetch_listing_html = listing
        result = self.manager._discover_news_urls(
            ["https://cn.nytimes.com/foreign-relations/"],
            {"max_items": 50, "max_pages": 5},
            "test",
        )
        self.assertEqual(len(result), 50)
        self.assertIn("story-50", result[-1])


if __name__ == "__main__":
    unittest.main()
