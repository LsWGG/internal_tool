import random
import unittest
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_models import Constraint, Fallback
from app.clean_schema import (
    UniqueIndex,
    Violation,
    apply_fallback,
    check_value,
    compiled,
    eval_column,
    is_empty,
    to_float,
)


def build(**kwargs):
    return Constraint(**kwargs)


class PriorityTests(unittest.TestCase):
    def test_empty_reports_non_null_not_regex(self):
        # 这条是硬要求：空值必须报 non_null，否则重试提示与报告都变成噪音。
        constraints = [build(kind="regex", pattern=r"\d+"), build(kind="non_null")]
        for value in (None, ""):
            violation = check_value(value, constraints, "f")
            self.assertEqual(violation.kind, "non_null", value)

    def test_priority_is_independent_of_user_order(self):
        constraints = [
            build(kind="length", min_length=5),
            build(kind="enum", values=["a"]),
            build(kind="non_null"),
        ]
        random.shuffle(constraints)
        # "b" 既是长度不足、又不在枚举内。枚举(2) 优先于 长度(4)。
        self.assertEqual(check_value("b", constraints, "f").kind, "enum")
        # 空值无论怎么排都是 non_null
        self.assertEqual(check_value("", constraints, "f").kind, "non_null")

    def test_warn_severity_does_not_block(self):
        constraints = [
            build(kind="regex", pattern=r"\d+", severity="warn"),
            build(kind="length", min_length=5),
        ]
        # warn 的正则不匹配，但 error 级的长度也不满足 → 报 length，warn 不参与短路
        self.assertEqual(check_value("ab", constraints, "f").kind, "length")
        self.assertIsNone(check_value("abcde", constraints, "f"))


class ScalarColumnarEquivalenceTests(unittest.TestCase):
    """整个列式优化的许可证。等价性不成立，就不许向量化。"""

    VALUES = [
        None, "", " ", "a", "abc", "0", "1", "42", "-7", "3.14", "1e3",
        "男", "女", "未知", "2020-01-01", "2020-13-45", "a@b.com", "not-an-email",
        "https://x.com", "9999999999999999999999", "  padded  ", "a" * 20, "a" * 10001,
        True, False, 0, 1, 3.5, -0.0,
    ]

    def _constraints(self, rng):
        pool = [
            lambda: build(kind="non_null"),
            lambda: build(kind="enum", values=rng.sample(["a", "abc", "男", "女", "1"], k=3)),
            lambda: build(kind="enum", values=["A", "ABC"], case_sensitive=False),
            lambda: build(kind="regex", pattern=r"\d+"),
            lambda: build(kind="regex", pattern=r"a+", mode="search"),
            lambda: build(kind="regex", pattern=r"^[a-z]+$"),
            lambda: build(kind="length", min_length=rng.randint(0, 4)),
            lambda: build(kind="length", max_length=rng.randint(1, 12)),
            lambda: build(kind="length", min_length=2, max_length=6),
            lambda: build(kind="range", min_value=-5, max_value=100),
            lambda: build(kind="range", min_value=0),
            lambda: build(kind="format", value_kind="email"),
            lambda: build(kind="format", value_kind="url"),
            lambda: build(kind="format", value_kind="date", date_formats=["%Y-%m-%d"]),
            lambda: build(kind="format", value_kind="ipv4"),
            lambda: build(kind="type", value_kind="int"),
            lambda: build(kind="type", value_kind="float"),
            lambda: build(kind="type", value_kind="bool"),
        ]
        picked = rng.sample(pool, k=rng.randint(1, 4))
        return [make() for make in picked]

    def test_equivalence_on_random_constraint_sets(self):
        rng = random.Random(20260926)
        for _ in range(400):
            constraints = self._constraints(rng)
            values = [rng.choice(self.VALUES) for _ in range(40)]
            expected = [check_value(v, constraints, "f") for v in values]
            actual = eval_column(values, constraints, "f")
            for i, (want, got) in enumerate(zip(expected, actual)):
                self.assertEqual(
                    want.kind if want else None,
                    got.kind if got else None,
                    f"row {i} value={values[i]!r} constraints={[c.kind for c in constraints]}",
                )

    def test_equivalence_with_unique_prepopulated(self):
        rng = random.Random(7)
        for _ in range(60):
            seed_values = [rng.choice(self.VALUES) for _ in range(10)]
            values = [rng.choice(self.VALUES) for _ in range(30)]
            constraints = [
                build(kind="regex", pattern=r"[a-z]{1,10}", severity="warn"),
                build(kind="unique"),
            ]

            ref_index = UniqueIndex()
            for v in seed_values:
                if check_value(v, constraints, "f", ref_index) is None:
                    ref_index.add(v)
            expected = []
            for v in values:
                violation = check_value(v, constraints, "f", ref_index)
                expected.append(violation)
                if violation is None:
                    ref_index.add(v)

            col_index = UniqueIndex()
            for v in seed_values:
                if check_value(v, constraints, "f", col_index) is None:
                    col_index.add(v)
            actual = eval_column(values, constraints, "f", col_index)

            for i, (want, got) in enumerate(zip(expected, actual)):
                self.assertEqual(
                    want.kind if want else None, got.kind if got else None, f"row {i} {values[i]!r}"
                )

    def test_equivalence_holds_when_short_circuit_skips_unique(self):
        # 前面的约束失败的行不该被 unique 检查；两遍都必须如此，否则索引状态会分叉。
        constraints = [build(kind="non_null"), build(kind="unique")]
        values = ["", "", None, "", "a", "a"]
        col_index = UniqueIndex()
        actual = eval_column(values, constraints, "f", col_index)
        kinds = [v.kind if v else None for v in actual]
        self.assertEqual(kinds, ["non_null"] * 4 + [None, "unique"])


class RegexEngineTests(unittest.TestCase):
    def test_re2_is_used_for_ordinary_patterns(self):
        pattern = compiled(r"\d{3}-\d{4}")
        self.assertEqual(pattern.engine, "re2")
        self.assertTrue(pattern.matches("555-1234"))
        self.assertFalse(pattern.matches("5551234"))

    def test_backreference_falls_back_to_re(self):
        # RE2 不支持反向引用。回落后必须仍然正确工作，而不是异常。
        pattern = compiled(r"(ab)\1")
        self.assertEqual(pattern.engine, "re")
        self.assertTrue(pattern.matches("abab"))
        self.assertFalse(pattern.matches("abac"))

    def test_engine_choice_is_deterministic(self):
        # 两遍一致性依赖「同一个 pattern 永远选同一个引擎」。
        for _ in range(5):
            self.assertEqual(compiled(r"(ab)\1").engine, "re")
            self.assertEqual(compiled(r"\d+").engine, "re2")

    def test_long_text_under_fallback_engine_is_not_matched(self):
        pattern = compiled(r"(ab)\1")
        self.assertFalse(pattern.matches("ab" * 6000))
        self.assertTrue(pattern.over_limit("ab" * 6000))
        violation = check_value("ab" * 6000, [build(kind="regex", pattern=r"(ab)\1")], "f")
        self.assertEqual(violation.kind, "regex")
        self.assertIn("未做匹配", violation.detail)

    def test_search_mode_finds_substring(self):
        pattern = compiled(r"\d+", mode="search")
        self.assertTrue(pattern.matches("abc123"))
        self.assertEqual(pattern.find("abc123"), "123")


class UniqueIndexTests(unittest.TestCase):
    def test_empty_values_never_duplicate(self):
        index = UniqueIndex()
        for value in ("", None, "", None):
            self.assertFalse(index.probe(value))
            index.add(value)
        self.assertEqual(index.count, 0)

    def test_probe_after_add(self):
        index = UniqueIndex()
        index.add("a")
        self.assertTrue(index.probe("a"))
        self.assertFalse(index.probe("b"))

    def test_check_batch_matches_sequential_semantics(self):
        index = UniqueIndex()
        index.add("seed")
        values = ["seed", "a", "a", "b", "a", "", ""]
        duplicates = index.check_batch(values)
        self.assertEqual(duplicates, [True, False, True, False, True, False, False])

    def test_resolution_must_add_incrementally(self):
        """[X_1, X, X] —— 检测说不重复，但逐行解决时 X 的重复会去试 X_1。

        如果 X_1 没有在解决 row1 时立刻进索引，row3 会拿到重复的 X_1 并写进输出。
        这个测试是 check_batch 两阶段契约的守卫。
        """
        index = UniqueIndex()
        values = ["X_1", "X", "X"]
        duplicates = index.check_batch(values)
        self.assertEqual(duplicates, [False, False, True])

        fallback = Fallback(on_violation="truncate", unique_suffix="_")
        constraint = build(kind="unique")

        violation = Violation("f", "unique", "X", "")

        # 正确做法：解决到 row1 时就把 X_1 记进索引，所以 row3 必须绕开它
        good = UniqueIndex()
        good.add("X_1")
        good.add("X")
        result = apply_fallback("X", violation, fallback, constraint, good)
        self.assertTrue(result.changed)
        self.assertEqual(result.value, "X_2")

        # 错误做法（攒到块末批量 add）：索引里还没有 X_1，于是 row3 拿到 X_1 ——
        # 一个与 row1 完全相同的值被写进输出。两者结果不同，正是这个契约存在的理由。
        bad = UniqueIndex()
        wrong = apply_fallback("X", violation, fallback, constraint, bad)
        self.assertEqual(wrong.value, "X_1")
        self.assertNotEqual(wrong.value, result.value)

    def test_compaction_preserves_membership(self):
        index = UniqueIndex()
        values = [f"v{i}" for i in range(UniqueIndex.PENDING_LIMIT + 500)]
        for value in values:
            index.add(value)
        for value in values:
            self.assertTrue(index.probe(value), value)
        self.assertFalse(index.probe("nope"))
        self.assertEqual(index.count, len(values))

    def test_log_roundtrip_and_truncate(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unique.log"
            index = UniqueIndex(log)
            for i in range(1000):
                index.add(f"v{i}")
            index.flush()
            self.assertEqual(log.stat().st_size, 1000 * 8)

            # 模拟崩溃：日志写了 1200 条，但 state.json 只记录到 1000 条
            for i in range(200):
                index.add(f"x{i}")
            index.flush()
            self.assertEqual(log.stat().st_size, 1200 * 8)

            index.truncate_to(1000)
            self.assertEqual(log.stat().st_size, 1000 * 8)
            index.close()

            rebuilt = UniqueIndex.rebuild(log, 1000)
            self.assertEqual(rebuilt.count, 1000)
            self.assertTrue(rebuilt.probe("v999"))
            # 崩溃后多写的那 200 条必须被丢掉，否则重放不幂等
            self.assertFalse(rebuilt.probe("x0"))
            rebuilt.close()

    def test_rebuild_matches_live_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "u.log"
            index = UniqueIndex(log)
            values = ["a", "b", "a", "c", "", "d"]
            for value in values:
                index.add(value)
            index.flush()
            count = index.count
            index.close()

            rebuilt = UniqueIndex.rebuild(log, count)
            for value in values:
                self.assertEqual(index.probe(value), rebuilt.probe(value), value)
            rebuilt.close()


class FallbackTests(unittest.TestCase):
    def violation(self, kind, value=None, detail=""):
        return Violation("f", kind, value, detail)

    def test_keep_leaves_value_untouched(self):
        # keep 必须是彻底的 no-op：不改值、不标 degraded、不报 changed。
        # 任何一种违规类型下都不该有例外。
        by_kind = {
            "non_null": build(kind="non_null"),
            "enum": build(kind="enum", values=["x"]),
            "regex": build(kind="regex", pattern=r"\d"),
            "length": build(kind="length", max_length=1),
            "range": build(kind="range", min_value=0, max_value=1),
            "format": build(kind="format", value_kind="email"),
            "unique": build(kind="unique"),
        }
        for kind, constraint in by_kind.items():
            result = apply_fallback(
                "原值", self.violation(kind, "原值"), Fallback(on_violation="keep"), constraint
            )
            self.assertEqual(result.value, "原值", kind)
            self.assertFalse(result.changed, kind)
            self.assertFalse(result.degraded, kind)

    def test_non_null_truncate_fills_default(self):
        constraint = build(kind="non_null")
        result = apply_fallback("", self.violation("non_null", ""),
                                Fallback(on_violation="truncate", default="未知"), constraint)
        self.assertEqual(result.value, "未知")
        self.assertTrue(result.changed)

    def test_non_null_truncate_without_default_gives_empty_string(self):
        result = apply_fallback(None, self.violation("non_null", None),
                                Fallback(on_violation="truncate"), build(kind="non_null"))
        self.assertEqual(result.value, "")

    def test_enum_truncate_degrades_to_keep_with_warning(self):
        # 枚举值无法截断 —— 必须标 degraded，否则日志里分不清「做成了」和「放弃了」
        result = apply_fallback("other", self.violation("enum", "other"),
                                Fallback(on_violation="truncate"), build(kind="enum", values=["a"]))
        self.assertEqual(result.value, "other")
        self.assertTrue(result.degraded)
        self.assertIn("无法截断", result.note)

    def test_length_truncate_slices_and_pads(self):
        over = apply_fallback("abcdefghij", self.violation("length", "abcdefghij"),
                              Fallback(on_violation="truncate"), build(kind="length", max_length=4))
        self.assertEqual(over.value, "abcd")
        self.assertTrue(over.changed)

        under = apply_fallback("ab", self.violation("length", "ab"),
                               Fallback(on_violation="truncate"),
                               build(kind="length", min_length=5, pad_char="*"))
        self.assertEqual(under.value, "ab***")
        self.assertTrue(under.changed)

    def test_range_truncate_clamps(self):
        constraint = build(kind="range", min_value=0, max_value=100)
        low = apply_fallback("-5", self.violation("range", "-5"),
                             Fallback(on_violation="truncate"), constraint)
        self.assertEqual(low.value, 0)
        self.assertTrue(low.changed)

        high = apply_fallback("999", self.violation("range", "999"),
                              Fallback(on_violation="truncate"), constraint)
        self.assertEqual(high.value, 100)

    def test_range_truncate_keeps_integers_integral(self):
        # 整数列不该因为钳制变成 3.0
        constraint = build(kind="range", min_value=0.0, max_value=3.0)
        result = apply_fallback("99", self.violation("range", "99"),
                                Fallback(on_violation="truncate"), constraint)
        self.assertEqual(result.value, 3)
        self.assertIsInstance(result.value, int)

    def test_range_truncate_on_non_numeric_degrades(self):
        constraint = build(kind="range", min_value=0, max_value=10)
        result = apply_fallback("abc", self.violation("range", "abc"),
                                Fallback(on_violation="truncate"), constraint)
        self.assertEqual(result.value, "abc")
        self.assertTrue(result.degraded)

    def test_format_truncate_degrades(self):
        constraint = build(kind="format", value_kind="email")
        result = apply_fallback("nope", self.violation("format", "nope"),
                                Fallback(on_violation="truncate"), constraint)
        self.assertTrue(result.degraded)

    def test_regex_search_truncate_keeps_match(self):
        constraint = build(kind="regex", pattern=r"\d+", mode="search")
        result = apply_fallback("订单12345号", self.violation("regex", "订单12345号"),
                                Fallback(on_violation="truncate"), constraint)
        self.assertEqual(result.value, "12345")
        self.assertTrue(result.changed)

    def test_regex_fullmatch_truncate_degrades(self):
        constraint = build(kind="regex", pattern=r"\d+")
        result = apply_fallback("abc", self.violation("regex", "abc"),
                                Fallback(on_violation="truncate"), constraint)
        self.assertTrue(result.degraded)

    def test_unique_truncate_appends_suffix(self):
        index = UniqueIndex()
        index.add("a")
        result = apply_fallback("a", self.violation("unique", "a"),
                                Fallback(on_violation="truncate", unique_suffix="_"),
                                build(kind="unique"), index)
        self.assertEqual(result.value, "a_1")
        self.assertTrue(result.changed)

    def test_unique_truncate_without_index_degrades(self):
        result = apply_fallback("a", self.violation("unique", "a"),
                                Fallback(on_violation="truncate"), build(kind="unique"), None)
        self.assertTrue(result.degraded)

    def test_retry_is_rejected_here(self):
        with self.assertRaises(ValueError):
            apply_fallback("a", self.violation("enum", "a"),
                           Fallback(on_violation="retry"), build(kind="enum", values=["b"]))


class HelperTests(unittest.TestCase):
    def test_is_empty_does_not_strip(self):
        # strip 会改动数据；空白串是数据事实，该由 trim 算子显式处理
        self.assertFalse(is_empty(" "))
        self.assertTrue(is_empty(""))
        self.assertTrue(is_empty(None))

    def test_to_float_is_strict(self):
        self.assertEqual(to_float("3.5"), 3.5)
        self.assertEqual(to_float(" 42 "), 42.0)
        self.assertIsNone(to_float("1,234"))
        self.assertIsNone(to_float("abc"))
        self.assertIsNone(to_float(None))
        # bool 是 int 的子类，但 "True" 不是数值
        self.assertIsNone(to_float(True))

    def test_compiled_is_cached(self):
        self.assertIs(compiled(r"\d+"), compiled(r"\d+"))
        self.assertIsNot(compiled(r"\d+"), compiled(r"\d+", "search"))


if __name__ == "__main__":
    unittest.main()
