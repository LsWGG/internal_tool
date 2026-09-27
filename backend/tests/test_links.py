"""外链工具（设置弹窗那张表）：服务层与路由。

这一层没有多少业务语义，值得测的是**边界**：地址会被前端写进 iframe 的 `src`（所以
`javascript:` 必须被拒）、id 是整表替换时的身份（所以重复 id 必须报错而不是悄悄多加一行）、
以及文件坏掉时首页不能跟着塌。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.links_api import make_router
from app.links_manager import (CATEGORY_EXTERNAL, ExternalLink, LinksManager, LinksNotFound,
                               frame_policy, load_links, probe_frame_policy)


def link(**overrides) -> dict:
    data = {"name": "内部 Wiki", "url": "https://wiki.example.com/", "description": "团队知识库",
            "category": CATEGORY_EXTERNAL}
    data.update(overrides)
    return data


class StubProbe:
    """嵌入检测的桩：不碰网络，记下每次被问的地址，结论随时能改（传 Exception 就是让它炸）。"""

    def __init__(self, result=(True, "")):
        self.result = result
        self.calls: list[str] = []

    def __call__(self, url: str):
        self.calls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.manager = LinksManager(self.root, probe=StubProbe())

    def tearDown(self):
        self._tmp.cleanup()

    def test_saving_a_new_link_mints_an_id_and_writes_the_file(self):
        saved = self.manager.save({"links": [link()]})["links"]
        self.assertEqual(len(saved), 1)
        self.assertTrue(saved[0]["id"])
        self.assertEqual([item.name for item in load_links(self.manager.file)], ["内部 Wiki"])

    def test_the_same_id_is_an_update_not_a_second_row(self):
        first = self.manager.save({"links": [link()]})["links"][0]
        again = self.manager.save({"links": [dict(first, name="内部 Wiki v2")]})["links"]
        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], first["id"])
        self.assertEqual(again[0]["name"], "内部 Wiki v2")

    def test_a_repeated_id_is_rejected(self):
        """整表替换时 id 就是身份：两条同 id 会让「编辑」变成「复制一份」。"""
        with self.assertRaisesRegex(ValueError, "重复"):
            self.manager.save({"links": [link(id="dup"), link(id="dup", name="另一个")]})

    def test_deleting_a_missing_link_is_a_not_found(self):
        from app.links_manager import LinksNotFound
        self.manager.save({"links": [link()]})
        with self.assertRaises(LinksNotFound):
            self.manager.delete("nope")
        self.assertEqual(len(self.manager.list()["links"]), 1)

    def test_the_category_defaults_to_the_external_group(self):
        saved = self.manager.save({"links": [{"name": "甲", "url": "https://a.example.com"}]})["links"]
        self.assertEqual(saved[0]["category"], CATEGORY_EXTERNAL)

    def test_a_broken_file_is_an_empty_table_not_a_crash(self):
        self.manager.file.parent.mkdir(parents=True, exist_ok=True)
        self.manager.file.write_text("{ 不是 json", encoding="utf-8")
        self.assertEqual(load_links(self.manager.file), [])
        self.assertEqual(self.manager.list(), {"links": []})

    def test_one_broken_row_does_not_take_the_others_with_it(self):
        self.manager.file.parent.mkdir(parents=True, exist_ok=True)
        self.manager.file.write_text(json.dumps({"version": 1, "links": [
            link(id="ok"),
            {"id": "bad", "name": "缺地址"},
        ]}, ensure_ascii=False), encoding="utf-8")
        self.assertEqual([item.id for item in load_links(self.manager.file)], ["ok"])

    def test_a_bare_list_file_still_loads(self):
        """老/手写的文件可能就是一个数组，不该读不出来。"""
        self.manager.file.parent.mkdir(parents=True, exist_ok=True)
        self.manager.file.write_text(json.dumps([link(id="a")], ensure_ascii=False), encoding="utf-8")
        self.assertEqual([item.id for item in load_links(self.manager.file)], ["a"])

    def test_the_file_is_not_locked_down_like_a_secret(self):
        """这里没有密钥，不该照端点池那样存成 0600 —— 那会让人以为里面有什么秘密。"""
        import stat
        self.manager.save({"links": [link()]})
        self.assertNotEqual(stat.S_IMODE(self.manager.file.stat().st_mode), 0o600)


class UrlValidationTests(unittest.TestCase):
    """地址会被原样写进 iframe 的 `src`，所以 scheme 是安全边界。"""

    def test_http_and_https_pass(self):
        self.assertEqual(ExternalLink(name="甲", url="http://127.0.0.1:8080/x").url,
                         "http://127.0.0.1:8080/x")

    def test_other_schemes_are_refused(self):
        for url in ("javascript:alert(1)", "data:text/html,<b>x</b>", "ftp://a.example.com",
                    "file:///etc/passwd", "wiki.example.com", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ExternalLink(name="甲", url=url)

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(ValueError):
            ExternalLink(name="   ", url="https://a.example.com")


class FramePolicyTests(unittest.TestCase):
    """`frame_policy` 是纯函数：给一组响应头，回答「能不能嵌」以及依据是什么。"""

    def test_nothing_declared_means_embeddable(self):
        self.assertEqual(frame_policy({"content-type": "text/html"}), (True, ""))

    def test_x_frame_options_deny_and_sameorigin_block(self):
        for value in ("DENY", "deny", "SAMEORIGIN", "sameorigin"):
            with self.subTest(value=value):
                blocked, why = frame_policy({"x-frame-options": value})
                self.assertFalse(blocked)
                self.assertIn(value, why)

    def test_allow_from_is_ignored_the_way_browsers_ignore_it(self):
        """ALLOW-FROM 现代浏览器直接忽略（整条头作废），这里跟着忽略，免得误报成不能嵌。"""
        self.assertEqual(frame_policy({"x-frame-options": "ALLOW-FROM https://a.example.com"}),
                         (True, ""))

    def test_frame_ancestors_self_blocks_and_the_evidence_is_kept(self):
        blocked, why = frame_policy({"content-security-policy":
                                     "default-src *; frame-ancestors 'self'; img-src *"})
        self.assertFalse(blocked)
        self.assertEqual(why, "frame-ancestors 'self'")

    def test_star_is_the_only_frame_ancestors_that_passes(self):
        self.assertEqual(frame_policy({"content-security-policy": "frame-ancestors *"}), (True, ""))
        blocked, _ = frame_policy({"content-security-policy": "frame-ancestors https://a.example.com"})
        self.assertFalse(blocked)

    def test_every_policy_has_to_allow(self):
        """多个 CSP 头是取交集：只要有一条不让嵌，就是不让嵌。"""
        blocked, _ = frame_policy({"content-security-policy":
                                   ["frame-ancestors *", "frame-ancestors 'self'"]})
        self.assertFalse(blocked)

    def test_other_directives_are_not_mistaken_for_frame_ancestors(self):
        self.assertEqual(frame_policy({"content-security-policy": "frame-src *; script-src 'self'"}),
                         (True, ""))


class ProbeNetworkTests(unittest.TestCase):
    """真的发一次请求（打到本机的临时 http.server），把整条链路走通 —— 包括
    `response.raw.headers.getlist` 这条多值取头的路。外网一次都不碰。"""

    @classmethod
    def setUpClass(cls):
        import http.server
        import threading

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                self.send_response({"blocked": 200, "ok": 200}.get(self.path.lstrip("/"), 404))
                if self.path == "/blocked":
                    self.send_header("Content-Security-Policy",
                                     "default-src *; frame-ancestors 'self'")
                self.send_header("Content-Length", "0")
                self.end_headers()

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()      # shutdown() 只停循环，不关监听套接字（整仓跑在 -W error 下）

    def test_a_site_that_declares_frame_ancestors_is_reported_blocked(self):
        self.assertEqual(probe_frame_policy(f"{self.base}/blocked"),
                         (False, "frame-ancestors 'self'"))

    def test_a_site_that_declares_nothing_is_reported_embeddable(self):
        self.assertEqual(probe_frame_policy(f"{self.base}/ok"), (True, ""))

    def test_an_error_response_is_not_a_verdict(self):
        """4xx/5xx 也会带响应头回来（登录墙、反爬页），拿它当结论等于编。"""
        self.assertEqual(probe_frame_policy(f"{self.base}/missing"), (None, ""))

    def test_an_unreachable_host_is_not_a_verdict(self):
        """.invalid 是 RFC 2606 保留后缀，永远解析不出来：连不上 ≠ 不能嵌。"""
        self.assertEqual(probe_frame_policy("http://embed-check.invalid/"), (None, ""))


class EmbedProbeTests(unittest.TestCase):
    """保存路径上的嵌入检测：该探的探、不该探的不探、结论不接受客户端改写。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.probe = StubProbe()
        self.manager = LinksManager(self.root, probe=self.probe)

    def tearDown(self):
        self._tmp.cleanup()

    def test_a_new_link_is_probed_and_the_verdict_is_stored(self):
        self.probe.result = (False, "frame-ancestors 'self'")
        saved = self.manager.save({"links": [link()]})["links"][0]
        self.assertIs(saved["embeddable"], False)
        self.assertEqual(saved["frame_policy"], "frame-ancestors 'self'")
        self.assertEqual(self.probe.calls, ["https://wiki.example.com/"])
        # 落盘的和返回的是同一份，刷新页面不能变回「未检测」
        self.assertIs(load_links(self.manager.file)[0].embeddable, False)

    def test_an_unchanged_url_is_not_probed_again(self):
        stored = self.manager.save({"links": [link()]})["links"][0]
        self.manager.save({"links": [dict(stored, name="内部 Wiki v2")]})
        self.assertEqual(len(self.probe.calls), 1)

    def test_a_changed_url_is_probed_again(self):
        stored = self.manager.save({"links": [link()]})["links"][0]
        self.manager.save({"links": [dict(stored, url="https://other.example.com/")]})
        self.assertEqual(self.probe.calls, ["https://wiki.example.com/", "https://other.example.com/"])

    def test_a_row_that_could_not_be_probed_is_asked_again(self):
        """上次没问到（站点当时连不上）留的是 None，下次保存顺手再问一次。"""
        self.probe.result = (None, "")
        stored = self.manager.save({"links": [link()]})["links"][0]
        self.assertIsNone(stored["embeddable"])
        self.probe.result = (True, "")
        again = self.manager.save({"links": [stored]})["links"][0]
        self.assertIs(again["embeddable"], True)
        self.assertEqual(len(self.probe.calls), 2)

    def test_the_client_cannot_declare_a_site_embeddable(self):
        """整表 POST 是客户端说了算的，由它带 embeddable 上来等于让浏览器定结论。"""
        self.probe.result = (False, "frame-ancestors 'self'")
        saved = self.manager.save({"links": [link(embeddable=True, frame_policy="")]})["links"][0]
        self.assertIs(saved["embeddable"], False)

    def test_a_probe_that_explodes_does_not_break_the_save(self):
        manager = LinksManager(self.root, probe=StubProbe(RuntimeError("炸了")))
        saved = manager.save({"links": [link()]})["links"][0]
        self.assertIsNone(saved["embeddable"])
        self.assertEqual(len(load_links(manager.file)), 1)

    def test_a_bad_address_is_refused_before_any_probe(self):
        with self.assertRaises(ValueError):
            self.manager.save({"links": [link(url="javascript:alert(1)")]})
        self.assertEqual(self.probe.calls, [])

    def test_several_new_links_are_probed_in_parallel(self):
        """一次改好几条时不该按顺序各等一个超时（检测超时是 4 秒量级）。"""
        import time

        def slow(url):
            time.sleep(0.3)
            return True, ""

        manager = LinksManager(self.root, probe=slow)
        started = time.monotonic()
        manager.save({"links": [link(id=f"l{index}", url=f"https://a{index}.example.com/")
                                for index in range(4)]})
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 0.85, f"4 条串行探测要 1.2 秒，实际 {elapsed:.2f} 秒")

    def test_probe_one_refreshes_a_single_row(self):
        self.probe.result = (None, "")
        stored = self.manager.save({"links": [link()]})["links"][0]
        self.probe.result = (False, "X-Frame-Options: DENY")
        result = self.manager.probe_one(stored["id"])["link"]
        self.assertIs(result["embeddable"], False)
        self.assertEqual(result["frame_policy"], "X-Frame-Options: DENY")
        self.assertIs(load_links(self.manager.file)[0].embeddable, False)

    def test_probe_one_on_a_missing_row_is_a_not_found(self):
        with self.assertRaises(LinksNotFound):
            self.manager.probe_one("nope")


class ApiTests(unittest.TestCase):
    """路由层：只测 HTTP 状态码与形状。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # 探测定桩：这些用例关心的是路由，不该顺手去连 wiki.example.com
        self.probe = StubProbe()
        app = FastAPI()
        app.include_router(make_router(self.root, probe=self.probe))
        self.app = app

    def tearDown(self):
        self._tmp.cleanup()

    def client(self):
        return TestClient(self.app)

    def test_the_empty_table_is_an_empty_list(self):
        with self.client() as client:
            response = client.get("/api/links")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"links": []})

    def test_create_then_delete_over_http(self):
        with self.client() as client:
            created = client.post("/api/links", json={"links": [link()]})
            self.assertEqual(created.status_code, 200)
            saved = created.json()["links"][0]
            self.assertTrue(saved["id"])
            self.assertEqual(client.get("/api/links").json()["links"], [saved])

            self.assertEqual(client.delete(f"/api/links/{saved['id']}").json(), {"ok": True})
            self.assertEqual(client.get("/api/links").json(), {"links": []})
            self.assertEqual(client.delete(f"/api/links/{saved['id']}").status_code, 404)

    def test_a_bad_address_comes_back_as_400_with_the_reason(self):
        with self.client() as client:
            response = client.post("/api/links", json={"links": [link(url="javascript:alert(1)")]})
            self.assertEqual(response.status_code, 400)
            self.assertIn("http", response.json()["detail"])

    def test_a_body_without_the_list_is_a_400(self):
        with self.client() as client:
            self.assertEqual(client.post("/api/links", json={}).status_code, 400)

    def test_the_route_is_reachable_without_a_trailing_slash(self):
        """写成 `/api/links/` 的话，前端那个不带斜杠的请求会先吃一个 307。"""
        with self.client() as client:
            self.assertNotIn(client.get("/api/links").status_code, (307, 308))

    def test_probing_one_link_over_http(self):
        with self.client() as client:
            saved = client.post("/api/links", json={"links": [link()]}).json()["links"][0]
            self.probe.result = (False, "frame-ancestors 'self'")
            response = client.post(f"/api/links/{saved['id']}/probe")
            self.assertEqual(response.status_code, 200)
            self.assertIs(response.json()["link"]["embeddable"], False)
            # 写回文件了：刷新页面看到的还是这个结论
            self.assertIs(client.get("/api/links").json()["links"][0]["embeddable"], False)
            self.assertEqual(client.post("/api/links/nope/probe").status_code, 404)


if __name__ == "__main__":
    unittest.main()
