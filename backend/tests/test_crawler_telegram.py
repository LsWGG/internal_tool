import unittest

from app.crawler_manager import CrawlerTaskManager


class TelegramCrawlerTests(unittest.TestCase):
    def setUp(self):
        self.manager = object.__new__(CrawlerTaskManager)

    def test_member_fields_are_mode_specific(self):
        names = [item["name"] for item in self.manager.builtin_fields(
            "telegram", telegram_mode="members"
        )]
        self.assertIn("user_id", names)
        self.assertIn("verified", names)
        self.assertNotIn("message_id", names)

    def test_public_target_normalization(self):
        self.assertEqual(
            self.manager._telegram_target("https://t.me/example_channel"),
            ("example_channel", "https://t.me/example_channel"),
        )
        self.assertEqual(self.manager._telegram_target("@example_group")[0], "example_group")

    def test_public_message_page_is_parsed(self):
        html = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="example/42">
            <a class="tgme_widget_message_author_name">Example</a>
            <div class="tgme_widget_message_text"><p>Hello</p><p>World</p></div>
            <time datetime="2026-09-08T08:00:00+00:00"></time>
            <span class="tgme_widget_message_views">1.2K</span>
            <a class="tgme_widget_message_date" href="https://t.me/example/42"></a>
          </div>
        </div>
        """
        rows = self.manager._telegram_public_rows(html, "example", 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["message_id"], "42")
        self.assertIn("Hello", rows[0]["text"])
        self.assertEqual(rows[0]["views"], "1.2K")

    def test_authorized_modes_require_credentials(self):
        with self.assertRaisesRegex(ValueError, "API ID"):
            self.manager._telegram_credentials({})


if __name__ == "__main__":
    unittest.main()
