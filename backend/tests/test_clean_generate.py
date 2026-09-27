"""字段生成：模板、随机、序列，以及「生成值与批切分无关」这条续跑的前提。

最后一条是本文件的重点。续跑会把崩溃点之后的行**重新生成**一遍，如果生成值取决于
「这是第几次调用」或者「这批是从第几行开始的」，那么续跑后的产物就会与不中断运行不同 ——
而且差异是静默的（值看着都挺正常），只有逐字节比较才看得出来。所以这里对每种生成器都跑
一遍「一次一整批」对「切成大小不一的多批」，要求结果逐个相同。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clean_generate import (
    FAKER_PROVIDERS,
    CleanGenerateError,
    GenerationBatch,
    RandomGenerator,
    SequenceGenerator,
    TemplateGenerator,
    build_generators,
    deterministic_seed,
    describe_generators,
    generated_fields,
    generator_catalog,
    render_template,
    template_fields,
    validate_template,
)
from app.clean_models import (
    CleanTaskConfig,
    FieldSpec,
    GenerateRule,
    LlmTaskOptions,
    SourceSpec,
)

from pydantic import ValidationError


def batch(indices, rows=None, attempt=0, feedback=None):
    rows = rows if rows is not None else [{"name": f"n{i}"} for i in indices]
    return GenerationBatch(indices=list(indices), rows=list(rows),
                           attempt=attempt, feedback=feedback)


def values(generator, indices, **kwargs):
    """取单字段生成器的一列值（生成器接口返回的是每行一个字典）。"""
    return [item[generator.field_name] for item in generator.generate(batch(indices, **kwargs))]


def in_chunks(generator, indices, sizes):
    """按给定批大小切分后逐个生成，合并成一次一整批的结果。"""
    out: list = []
    position = 0
    for size in sizes:
        chunk = indices[position:position + size]
        position += size
        out.extend(generator.generate(batch(chunk)))
    if position < len(indices):
        out.extend(generator.generate(batch(indices[position:])))
    return out


# =========================================================================== 模板


class TemplateTests(unittest.TestCase):
    def test_substitutes_fields(self):
        self.assertEqual(
            render_template("${省}-${市}", {"省": "广东", "市": "深圳"}), "广东-深圳"
        )

    def test_leaves_text_without_placeholders_alone(self):
        self.assertEqual(render_template("固定值", {}), "固定值")

    def test_strips_whitespace_inside_the_braces(self):
        self.assertEqual(render_template("${ 名称 }", {"名称": "甲"}), "甲")

    def test_supports_dotted_and_spaced_field_names(self):
        """用户表头里就有「金额 (元)」这种名字，模板得能引用它。"""
        self.assertEqual(
            render_template("${金额 (元)}", {"金额 (元)": 12}), "12"
        )

    def test_missing_field_renders_empty_and_does_not_raise(self):
        """多文件任务的缺列是合法的，一行渲染失败不该让整个任务停摆。"""
        self.assertEqual(render_template("[${没有的}]", {"有": 1}), "[]")

    def test_default_value_syntax(self):
        self.assertEqual(render_template("${性别:-未知}", {}), "未知")
        self.assertEqual(render_template("${性别:-未知}", {"性别": ""}), "未知")
        self.assertEqual(render_template("${性别:-未知}", {"性别": "女"}), "女")

    def test_default_value_may_contain_colons_and_newlines(self):
        self.assertEqual(render_template("${x:-a:b\nc}", {}), "a:b\nc")

    def test_values_are_rendered_with_the_cell_text_rules(self):
        """模板与约束求值必须看到同一个字符串，否则「模板拼出来的值」和「约束检查的值」
        会不一样。"""
        self.assertEqual(render_template("${n}", {"n": 15.0}), "15")
        self.assertEqual(render_template("${n}", {"n": None}), "")

    def test_repeated_field_is_replaced_everywhere(self):
        self.assertEqual(render_template("${a}${a}", {"a": "x"}), "xx")

    def test_adjacent_braces_do_not_confuse_it(self):
        self.assertEqual(render_template("${a}${b}", {"a": "1", "b": "2"}), "12")

    def test_template_fields_lists_unique_names_in_order(self):
        self.assertEqual(
            template_fields("${b} ${a} ${b} ${c:-d}"), ["b", "a", "c"]
        )

    def test_template_fields_ignores_empty_braces(self):
        self.assertEqual(template_fields("${} ${a}"), ["a"])

    def test_validate_reports_unknown_fields(self):
        problems = validate_template("${没有} ${有}", ["有"])
        self.assertEqual(len(problems), 1)
        self.assertIn("没有", problems[0])

    def test_validate_suggests_the_right_name_on_a_case_mismatch(self):
        """大小写写错是最常见的一种，报错里直接给出正确写法。"""
        problems = validate_template("${gender}", ["Gender"])
        self.assertEqual(len(problems), 1)
        self.assertIn("Gender", problems[0])

    def test_validate_accepts_a_default_for_an_unknown_field(self):
        """`${性别:-未知}` 引用一个不存在的字段是**有意的**写法（缺列时给默认值），
        不该被校验拦下来。"""
        problems = validate_template("${性别:-未知}", ["姓名"])
        self.assertEqual(problems, [], f"带默认值的字段不该报错：{problems}")


# =========================================================================== 批量无关性


class BatchInvarianceTests(unittest.TestCase):
    """续跑的地基：同一组行号，无论在哪个批里生成，值必须一样。"""

    def _check(self, generator, indices):
        whole = generator.generate(batch(indices))
        for sizes in ([1] * len(indices), [3, 2, 7], [len(indices)]):
            with self.subTest(sizes=sizes, generator=type(generator).__name__):
                self.assertEqual(in_chunks(generator, indices, sizes), whole)

    def test_sequence_is_batch_invariant(self):
        rule = GenerateRule(kind="sequence", start=100, step=5, width=6)
        self._check(SequenceGenerator("no", rule, seed=1), [0, 1, 2, 3, 4, 5, 6])

    def test_template_is_batch_invariant(self):
        rule = GenerateRule(kind="expression", expression="${name}!")
        self._check(TemplateGenerator("out", rule, seed=1), [0, 1, 2, 3])

    def test_random_int_is_batch_invariant(self):
        rule = GenerateRule(kind="random", generator="random_int", params={"min": 1, "max": 99})
        self._check(RandomGenerator("n", rule, seed=1), list(range(7)))

    def test_choice_is_batch_invariant(self):
        rule = GenerateRule(kind="random", generator="choice", params={"values": ["男", "女"]})
        self._check(RandomGenerator("性别", rule, seed=1), list(range(11)))

    def test_faker_is_batch_invariant(self):
        rule = GenerateRule(kind="random", generator="name", params={"locale": "zh_CN"})
        self._check(RandomGenerator("姓名", rule, seed=1), list(range(6)))

    def test_date_is_batch_invariant(self):
        rule = GenerateRule(kind="random", generator="date",
                            params={"start": "2020-01-01", "end": "2020-12-31"})
        self._check(RandomGenerator("日期", rule, seed=1), list(range(6)))

    def test_attempt_changes_the_value_but_stays_deterministic(self):
        """重试必须能拿到**不同**的值 —— 否则回退策略里的 retry 就是死循环。"""
        rule = GenerateRule(kind="random", generator="choice",
                            params={"values": list(range(100))})
        generator = RandomGenerator("n", rule, seed=1)
        first = values(generator, [0], attempt=0)[0]
        self.assertEqual(values(generator, [0], attempt=0)[0], first)
        others = {values(generator, [0], attempt=n)[0] for n in range(1, 12)}
        self.assertGreater(len(others - {first}), 1, "重试拿到的值几乎总是同一个")

    def test_a_new_process_seed_is_stable(self):
        """种子派生不许用内置 hash()：字符串 hash 每个进程带随机盐，重启后就变了。

        这里用两次独立派生模拟「重启」—— 只要实现里混进了 `hash()`，这条就会抖。
        """
        first = deterministic_seed(7, "姓名", 0, 0)
        second = deterministic_seed(7, "姓名", 0, 0)
        self.assertEqual(first, second)

    def test_different_fields_do_not_share_values(self):
        """两个字段用同样的配置也不该产出完全一样的列 —— 派生里必须带上字段名。"""
        rule = GenerateRule(kind="random", generator="choice", params={"values": [1, 2, 3]})
        left = values(RandomGenerator("甲", rule, seed=1), list(range(50)))
        right = values(RandomGenerator("乙", rule, seed=1), list(range(50)))
        self.assertNotEqual(left, right)


# =========================================================================== 序列


class SequenceTests(unittest.TestCase):
    def test_counts_from_the_row_index(self):
        rule = GenerateRule(kind="sequence", start=1, step=1)
        generator = SequenceGenerator("no", rule, seed=0)
        self.assertEqual(values(generator, [0, 1, 2]), [1, 2, 3])

    def test_honours_start_and_step(self):
        rule = GenerateRule(kind="sequence", start=10, step=-2)
        generator = SequenceGenerator("no", rule, seed=0)
        self.assertEqual(values(generator, [0, 1, 2]), [10, 8, 6])

    def test_width_zero_pads(self):
        rule = GenerateRule(kind="sequence", start=7, step=1, width=4)
        generator = SequenceGenerator("no", rule, seed=0)
        self.assertEqual(values(generator, [0]), ["0007"])

    def test_width_does_not_pad_a_negative_number(self):
        rule = GenerateRule(kind="sequence", start=-5, step=1, width=4)
        generator = SequenceGenerator("no", rule, seed=0)
        self.assertEqual(values(generator, [0]), [-5])

    def test_zero_step_is_rejected(self):
        rule = GenerateRule(kind="sequence", start=1, step=0)
        with self.assertRaises(CleanGenerateError):
            SequenceGenerator("no", rule, seed=0)

    def test_absurd_width_is_rejected(self):
        rule = GenerateRule(kind="sequence", start=1, step=1, width=999)
        with self.assertRaises(CleanGenerateError):
            SequenceGenerator("no", rule, seed=0)

    def test_resumed_offsets_continue_the_sequence(self):
        """续跑从第 100 行接着跑时，值必须与不中断运行的第 100 行相同。"""
        rule = GenerateRule(kind="sequence", start=1, step=1, width=5)
        whole = values(SequenceGenerator("no", rule, seed=0), list(range(50)))
        after = values(SequenceGenerator("no", rule, seed=0), list(range(20, 50)))
        self.assertEqual(whole[20:], after)


# =========================================================================== 随机


class RandomTests(unittest.TestCase):
    def test_choice_only_returns_configured_values(self):
        rule = GenerateRule(kind="random", generator="choice",
                            params={"values": ["男", "女"]})
        got = values(RandomGenerator("性别", rule, seed=3), list(range(200)))
        self.assertEqual(set(got), {"男", "女"})

    def test_choice_without_values_is_rejected(self):
        rule = GenerateRule(kind="random", generator="choice")
        with self.assertRaises(CleanGenerateError):
            RandomGenerator("性别", rule, seed=0)

    def test_random_int_stays_in_range(self):
        rule = GenerateRule(kind="random", generator="random_int",
                            params={"min": 5, "max": 9})
        got = values(RandomGenerator("n", rule, seed=3), list(range(300)))
        self.assertTrue(all(5 <= value <= 9 for value in got))

    def test_random_int_with_a_reversed_range_is_rejected(self):
        rule = GenerateRule(kind="random", generator="random_int",
                            params={"min": 9, "max": 5})
        with self.assertRaises(CleanGenerateError):
            RandomGenerator("n", rule, seed=0)

    def test_unknown_generator_lists_the_alternatives(self):
        rule = GenerateRule(kind="random", generator="不认识的")
        with self.assertRaises(CleanGenerateError) as caught:
            RandomGenerator("n", rule, seed=0)
        self.assertIn("choice", str(caught.exception))

    def test_the_catalog_every_entry_actually_runs(self):
        """目录里的每一项都要能真的生成一个值。

        目录是**界面渲染生成器下拉框的依据**，所以它一旦和实现脱节，代价是用户选到一个
        必然失败的名字 —— 而且报错出现在点「创建任务」的时候，离他刚才那个选择很远。
        这条测试就是「目录不许说谎」：逐个名字 build 出来并取一个值。

        需要参数的三个（choice / random_int / date）显式给参数，其余用空 params —— 这也
        顺带证明了「不填参数不会崩」。
        """
        catalog = generator_catalog()
        self.assertGreater(len(catalog), 20)
        by_name = {item["name"]: item for item in catalog}
        self.assertEqual(set(by_name), set(FAKER_PROVIDERS) | {"choice", "random_int", "date"})
        params = {"choice": {"values": ["甲", "乙"]},
                  "random_int": {"min": 1, "max": 9},
                  "date": {"format": "%Y-%m-%d"}}
        for name in by_name:
            rule = GenerateRule(kind="random", generator=name, params=params.get(name, {}))
            generator = RandomGenerator("字段", rule, seed=5)
            got = values(generator, [0, 1])
            self.assertEqual(len(got), 2, name)
            self.assertTrue(all(str(item).strip() for item in got), (name, got))
        # 目录里声明的参数名必须是实现真的读的：界面照它渲染输入框，写错了就是「配了没生效」
        self.assertEqual(by_name["sentence"]["params"], ["nb_words", "variable_nb_words"])
        self.assertEqual(by_name["choice"]["params"], ["values"])
        self.assertEqual(by_name["name"]["params"], [])
        # label 只是显示用：缺了也不该让一个能跑的生成器从界面上消失
        self.assertTrue(all(item["label"] for item in catalog))

    def test_a_random_rule_without_a_generator_is_rejected_at_config_time(self):
        """这条错误在**建配置对象**的时候就该抛出来，而不是等到跑第一行。

        模型层的校验比生成器更早，是刻意的：越早报错，离用户的操作越近。
        """
        with self.assertRaises(ValidationError):
            GenerateRule(kind="random")

    def test_null_ratio_produces_empty_values(self):
        rule = GenerateRule(kind="random", generator="name",
                            params={"null_ratio": 1.0, "locale": "zh_CN"})
        got = values(RandomGenerator("姓名", rule, seed=0), list(range(20)))
        self.assertEqual(got, [""] * 20)

    def test_null_ratio_zero_never_produces_empty(self):
        rule = GenerateRule(kind="random", generator="name", params={"locale": "zh_CN"})
        got = values(RandomGenerator("姓名", rule, seed=0), list(range(50)))
        self.assertTrue(all(value for value in got))

    def test_a_bad_null_ratio_is_rejected(self):
        rule = GenerateRule(kind="random", generator="name", params={"null_ratio": 2})
        with self.assertRaises(CleanGenerateError):
            RandomGenerator("姓名", rule, seed=0)

    def test_faker_values_look_like_the_language(self):
        rule = GenerateRule(kind="random", generator="name", params={"locale": "zh_CN"})
        got = values(RandomGenerator("姓名", rule, seed=1), list(range(5)))
        self.assertTrue(all(value and len(value) >= 2 for value in got), got)

    def test_english_locale_works_too(self):
        rule = GenerateRule(kind="random", generator="name", params={"locale": "en_US"})
        generator = RandomGenerator("name", rule, seed=1)
        got = values(generator, list(range(3)))
        self.assertTrue(all(value.isascii() for value in got), got)

    def test_an_unsupported_locale_is_rejected_clearly(self):
        rule = GenerateRule(kind="random", generator="name", params={"locale": "xx_XX"})
        with self.assertRaises(CleanGenerateError):
            values(RandomGenerator("姓名", rule, seed=0), [0])

    def test_provider_parameters_are_passed_through(self):
        """`variable_nb_words=False` 才给到定长；默认的 True 会在 ±40% 里浮动。"""
        rule = GenerateRule(kind="random", generator="sentence",
                            params={"nb_words": 3, "variable_nb_words": False,
                                    "locale": "en_US"})
        got = values(RandomGenerator("s", rule, seed=1), list(range(5)))
        self.assertTrue(all(len(value.split()) == 3 for value in got), got)

    def test_sentence_length_varies_by_default(self):
        """默认行为确实是浮动的 —— 上面那条测试靠的是显式关掉它，别以为它本来就定长。"""
        rule = GenerateRule(kind="random", generator="sentence",
                            params={"nb_words": 6, "locale": "en_US"})
        got = values(RandomGenerator("s", rule, seed=1), list(range(40)))
        self.assertGreater(len({len(value.split()) for value in got}), 1)

    def test_date_respects_the_configured_window_and_format(self):
        rule = GenerateRule(kind="random", generator="date",
                            params={"start": "2021-01-01", "end": "2021-12-31",
                                    "format": "%Y/%m/%d"})
        got = values(RandomGenerator("日期", rule, seed=1), list(range(30)))
        self.assertTrue(all(value.startswith("2021/") for value in got), got)

    def test_a_single_day_window_does_not_divide_by_zero(self):
        rule = GenerateRule(kind="random", generator="date",
                            params={"start": "2021-05-05", "end": "2021-05-05"})
        got = values(RandomGenerator("日期", rule, seed=1), list(range(5)))
        self.assertEqual(set(got), {"2021-05-05"})

    def test_an_unparseable_date_is_rejected(self):
        rule = GenerateRule(kind="random", generator="date", params={"start": "去年"})
        with self.assertRaises(CleanGenerateError):
            RandomGenerator("日期", rule, seed=0)

    def test_a_reversed_date_window_is_rejected(self):
        rule = GenerateRule(kind="random", generator="date",
                            params={"start": "2025-01-01", "end": "2020-01-01"})
        with self.assertRaises(CleanGenerateError):
            RandomGenerator("日期", rule, seed=0)


# =========================================================================== 装配


def _config(**overrides) -> CleanTaskConfig:
    data = {
        "name": "生成测试",
        "source": SourceSpec(mode="server_path", paths=["/tmp"]),
        "fields": [FieldSpec(dest="id", source="id")],
    }
    data.update(overrides)
    return CleanTaskConfig(**data)


class BuildTests(unittest.TestCase):
    def test_a_field_without_a_rule_gets_no_generator(self):
        self.assertEqual(build_generators(_config()), [])

    def test_generators_follow_field_declaration_order(self):
        config = _config(fields=[
            FieldSpec(dest="id", source="id"),
            FieldSpec(dest="no", generate=GenerateRule(kind="sequence", start=1)),
            FieldSpec(dest="性别", generate=GenerateRule(kind="random", generator="choice",
                                                        params={"values": ["男", "女"]})),
        ])
        generators = build_generators(config, seed=1)
        self.assertEqual(generated_fields(generators), ["no", "性别"])

    def test_one_rule_is_one_generator_instance_per_field(self):
        """两个字段各自一条规则 → 两个实例。共用实例会让第二次生成覆盖第一次的结果。"""
        config = _config(fields=[
            FieldSpec(dest="a", generate=GenerateRule(kind="sequence", start=1)),
            FieldSpec(dest="b", generate=GenerateRule(kind="sequence", start=100)),
        ])
        generators = build_generators(config, seed=1)
        self.assertEqual(len(generators), 2)
        self.assertEqual(generated_fields(generators), ["a", "b"])

    def _llm_config(self):
        return _config(
            fields=[FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}"))],
            llm=LlmTaskOptions(endpoint_ids=["e1"]),
        )

    def test_llm_without_endpoints_is_rejected_at_config_time(self):
        """别的都得先选端点 —— 这条在模型层就拦住了，比跑到生成时才发现早得多。"""
        with self.assertRaises(ValidationError):
            _config(fields=[
                FieldSpec(dest="摘要", generate=GenerateRule(kind="llm", prompt="${正文}")),
            ])

    def test_llm_without_a_factory_is_a_clear_error(self):
        with self.assertRaises(CleanGenerateError) as caught:
            build_generators(self._llm_config())
        self.assertIn("模型池", str(caught.exception))

    def test_llm_uses_the_injected_factory(self):
        """工厂拿到的是整个字段声明 —— 大模型生成器需要 `spec.constraints` 才能把约束
        写进 prompt（「只输出男或女」比让模型自己猜性别格式可靠得多）。"""
        seen = {}

        def factory(spec, rule, seed):
            seen["dest"] = spec.dest
            seen["rule"] = rule
            return SequenceGenerator(spec.dest, GenerateRule(kind="sequence", start=1), seed=seed)

        generators = build_generators(self._llm_config(), seed=4, llm_factory=factory)
        self.assertEqual(seen["dest"], "摘要")
        self.assertEqual(seen["rule"].kind, "llm")
        self.assertEqual(generated_fields(generators), ["摘要"])

    def test_an_unknown_kind_is_rejected(self):
        rule = GenerateRule(kind="random", generator="choice", params={"values": ["x"]})
        config = _config(fields=[FieldSpec(dest="a", generate=rule)])
        object.__setattr__(rule, "kind", "没见过的")
        with self.assertRaises(CleanGenerateError):
            build_generators(config)

    def test_describe_is_safe_for_the_report(self):
        """报告里要写生成方式，而报告会被下载 —— 这句话里不能有任何密钥。"""
        config = _config(fields=[
            FieldSpec(dest="姓名", generate=GenerateRule(kind="random", generator="name")),
            FieldSpec(dest="no", generate=GenerateRule(kind="sequence", start=1, step=1)),
            FieldSpec(dest="out", generate=GenerateRule(kind="expression", expression="${x}")),
        ])
        described = describe_generators(build_generators(config, seed=1))
        self.assertEqual(len(described), 3)
        self.assertIn("随机", described["姓名"])
        self.assertIn("序列", described["no"])

    def test_template_validation_is_available_for_the_config_screen(self):
        config = _config(fields=[
            FieldSpec(dest="out", generate=GenerateRule(kind="expression",
                                                        expression="${没有这个}")),
        ])
        generators = build_generators(config, seed=1)
        with self.assertRaises(CleanGenerateError) as caught:
            generators[0].validate(["id"])
        self.assertIn("没有这个", str(caught.exception))

    def test_template_validation_passes_for_a_real_field(self):
        config = _config(fields=[
            FieldSpec(dest="id", source="id"),
            FieldSpec(dest="out", generate=GenerateRule(kind="expression", expression="${id}")),
        ])
        build_generators(config, seed=1)[0].validate(["id", "out"])


if __name__ == "__main__":
    unittest.main()
