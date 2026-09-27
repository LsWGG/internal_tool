"""字段生成：随机、序列、模板、以及大模型（在 `clean_llm.py` 里，这里只留接口）。

# 唯一的硬要求：生成值必须与批切分无关

续跑时同一批行会在不同的批边界上被重新生成（崩溃点之后的行要重算）。所以**任何**生成器
都不许用「到目前为止调用了多少次」之类的计数器来决定输出，否则「续跑后的产物与不中断运行
逐字节相同」立刻不成立。这里的做法是：每个值都由 `(任务种子, 字段名, 绝对行号, 第几次尝试)`
四元组经 blake2b 派生出的确定性种子决定 —— 与批大小、批次数、并发无关，重启后也相同。

注意**不能**用内置 `hash()` 来派生：字符串的 hash 每个进程都带随机盐，重启后就变了。

# 生成器一次拿一批

接口是 `generate(batch) -> list`，而不是逐行调用。大模型那条路要按 `batch_size` 合并请求，
逐行接口会逼着它自己攒批；本地生成器忽略批结构即可，代价为零。
"""

from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence

try:  # RE2 是可选加速项：装不上时回落到 re（与 clean_schema 同一条约定）
    import re2 as _re2
except ImportError:  # pragma: no cover - 取决于环境
    _re2 = None

from .clean_models import CleanTaskConfig, FieldSpec, GenerateRule

DEFAULT_LOCALE = "zh_CN"

# 界面上那几个快捷选项。faker 支持的远不止这些，所以界面把它做成「可选可填」而不是
# 下拉框 —— 列全了几百个 locale 也只是把选择成本从打字换成滚动。
SUPPORTED_LOCALES = ("zh_CN", "zh_TW", "en_US", "en_GB", "ja_JP", "ko_KR",
                     "de_DE", "fr_FR", "es_ES", "ru_RU")

# `${字段名}`，字段名里允许空格与点（用户表头里就有这种东西）；`${字段:-默认}` 给默认值。
# 用 RE2 而不是 `re`：模板由用户输入，`re` 上的灾难性回溯会把一个 40 分钟的任务挂在
# 一个批上。这个模式本身没有回溯风险，但保持「用户输入一律走 RE2」这条规则更省心。
#
# 注：`re2` 没有 `DOTALL` 常量，多行点号要用内联的 `(?s)`；`re2` 也没有 `Match` 类型，
# 所以下面的类型标注一律写成 `Any`（否则 `from __future__ import annotations` 只是在
# 延迟报错，不是消除错误）。
_TEMPLATE = (_re2 or re).compile(r"\$\{([^{}]*?)\}")
_FIELD_DEFAULT = (_re2 or re).compile(r"(?s)^(.*?):-(.*)$")


class CleanGenerateError(ValueError):
    """生成配置有问题 —— 在创建任务时就抛，不要等到跑到一半。"""


def deterministic_seed(*parts: Any) -> int:
    """由若干部分派生的 64 位确定性种子。跨进程、跨重启、跨批切分都相同。"""
    joined = "\x00".join(str(part) for part in parts)
    digest = hashlib.blake2b(joined.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


# =========================================================================== 模板


def template_refs(text: str) -> list[tuple[str, bool]]:
    """模板引用到的 `(字段名, 是否给了默认值)`，按出现顺序去重。

    第二个分量决定校验时的宽严：`${性别:-未知}` 是**有意**声明「这一列可能没有，缺了就用
    默认值」，在多文件任务（`layout="union"`）里完全合法；而 `${性别}` 引用一个不存在的
    字段几乎总是拼错了。
    """
    seen: list[tuple[str, bool]] = []
    names: set[str] = set()
    for match in _TEMPLATE.finditer(text or ""):
        name, default = _split_default(match.group(1))
        name = name.strip()
        if not name or name in names:
            continue
        names.add(name)
        seen.append((name, default is not None))
    return seen


def template_fields(text: str) -> list[str]:
    """模板里引用到的字段名（按出现顺序，去重）。字段插入器与预览都用它。"""
    return [name for name, _ in template_refs(text)]


def _split_default(inside: str) -> tuple[str, str | None]:
    match = _FIELD_DEFAULT.match(inside)
    if match is None:
        return inside, None
    return match.group(1), match.group(2)


def cell_text(value: Any) -> str:
    """与 `clean_schema.cell_text` 同口径：模板渲染与约束求值看到的是同一个字符串。"""
    from .clean_schema import cell_text as _text

    return _text(value)


def render_template(text: str, row: Mapping[str, Any], *, missing: str = "") -> str:
    """把 `${字段}` 替换成该行的值。`${字段:-默认}` 在字段缺失或为空时用默认值。

    缺失字段渲染成 `missing` 而**不是**抛异常：多文件任务里某些文件可能没有那一列
    （`layout="union"` 下缺列是合法的），一行渲染失败不该让整个任务停摆。
    """

    def replace(match: Any) -> str:
        name, default = _split_default(match.group(1))
        name = name.strip()
        value = row.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            if default is not None:
                return default
            return missing
        return cell_text(value)

    return _TEMPLATE.sub(replace, text or "")


def validate_template(text: str, available: Sequence[str]) -> list[str]:
    """返回问题列表（空列表 = 没问题）。字段名对不上是最常见的配置错误，
    值得在创建任务时就说清楚，而不是等几百万行跑出一堆空值。

    给了默认值的引用不算问题，见 `template_refs`。
    """
    problems: list[str] = []
    names = list(available)
    lowered = {name.casefold(): name for name in names}
    for name, has_default in template_refs(text):
        if name in names:
            continue
        if has_default:
            continue
        if name.casefold() in lowered:
            problems.append(f"模板引用了「{name}」，实际字段是「{lowered[name.casefold()]}」")
            continue
        problems.append(f"模板引用了不存在的字段「{name}」")
    return problems


# =========================================================================== 批


@dataclass(slots=True)
class GenerationBatch:
    """一批待生成的行。

    `indices` 是**文件内绝对行号**，生成器只许用它（和行里的值）来决定输出。`feedback`
    只在重试时给出：与 `indices` 等长，每项是一条「上次为什么不合格」的说明。
    """

    indices: list[int]
    rows: list[Mapping[str, Any]]
    attempt: int = 0
    feedback: list[str] | None = None

    def __len__(self) -> int:
        return len(self.indices)


class BaseGenerator:
    """生成器接口。一次拿一批，返回与 `batch.indices` 等长的**每行一个字典**。

    为什么是字典而不是一列值：一条生成规则可以产出多个字段（一次大模型请求同时抽
    「摘要」和「关键词」是它最划算的用法）。用一列值就要为每个字段再跑一遍请求，
    或者引入「本批已经算过了」这种跨批会失效的状态。字典接口把多字段变成自然表达，
    单字段只是 `{字段: 值}` 这个特例。

    `output_fields` 是这个生成器负责写入的列；本地生成器全是单字段。
    """

    def __init__(self, field_name: str, rule: GenerateRule, *, seed: int = 0):
        self.field_name = field_name
        self.rule = rule
        self.seed = int(seed)
        self.output_fields: tuple[str, ...] = (field_name,)

    def generate(self, batch: GenerationBatch) -> list[dict[str, Any]]:
        """默认实现是逐行的（本地生成器都走这条）；大模型那条路覆写它来合并请求。"""
        return [
            {self.field_name: self._value(index, row, batch.attempt)}
            for index, row in zip(batch.indices, batch.rows)
        ]

    def _value(self, index: int, row: Mapping[str, Any], attempt: int) -> Any:
        raise NotImplementedError

    def validate(self, available: Sequence[str]) -> None:
        """创建任务时的校验。默认无事可做 —— 本地生成器的参数在构造时就校验过了。"""
        return None


# =========================================================================== 随机


# 允许的 faker 提供者。刻意做成白名单而不是「取任意属性名」：`getattr(fake, 用户输入)`
# 让配置能调用对象上的任意方法，那是一个不该开的口子。另外把信用卡号之类的东西排除在
# 外 —— 生成假数据不需要它们，而它们出现在产物里只会让人紧张。
FAKER_PROVIDERS: dict[str, str] = {
    "name": "name",
    "first_name": "first_name",
    "last_name": "last_name",
    "user_name": "user_name",
    "email": "email",
    "company_email": "company_email",
    "phone": "phone_number",
    "company": "company",
    "job": "job",
    "address": "address",
    "city": "city",
    "province": "province",
    "country": "country",
    "postcode": "postcode",
    "word": "word",
    "sentence": "sentence",
    "paragraph": "paragraph",
    "text": "text",
    "uuid": "uuid4",
    "url": "url",
    "domain": "domain_name",
    "ipv4": "ipv4",
    "color": "color_name",
    "currency": "currency_code",
    "iban": "iban",
    "ssn": "ssn",
}

# faker 提供者里接受参数的几个。白名单而不是 `**params` 透传：透传会让一个拼错的参数名
# 变成 faker 内部的 TypeError，报错点离用户的操作很远。
_PROVIDER_ARGS: dict[str, tuple[str, ...]] = {
    # `variable_nb_words` 默认是 True，也就是「3 个词」实际会给 2-4 个。想要字数稳定
    # 必须显式关掉它 —— 这个参数得能传下去，否则用户配了 nb_words 还是拿不到定长输出。
    "sentence": ("nb_words", "variable_nb_words"),
    "paragraph": ("nb_sentences",),
    "text": ("max_nb_chars",),
    "word": ("ext_word_list",),
    "pystr": ("min_chars", "max_chars"),
}


# 界面上的中文名。生成器的**可用名单**是白名单驱动的（上面两个），这里只是给它配个
# 说法 —— 两者分开，是为了让「加一个生成器」只需要改一处（白名单），忘了配中文名只会
# 退回显示英文名，而不是让一个能跑的生成器在界面上消失。
_GENERATOR_LABELS: dict[str, str] = {
    "name": "姓名", "first_name": "名", "last_name": "姓", "user_name": "用户名",
    "email": "邮箱", "company_email": "企业邮箱", "phone": "电话", "company": "公司",
    "job": "职位", "address": "地址", "city": "城市", "province": "省份",
    "country": "国家", "postcode": "邮编", "word": "单词", "sentence": "句子",
    "paragraph": "段落", "text": "长文本", "uuid": "UUID", "url": "网址",
    "domain": "域名", "ipv4": "IPv4", "color": "颜色", "currency": "货币代码",
    "iban": "IBAN", "ssn": "身份证号",
}


def generator_catalog() -> list[dict[str, Any]]:
    """随机生成器的目录：名字、中文名、以及每个生成器额外接受哪些参数。

    放在服务端是为了让**界面上能选的生成器和后端能跑的生成器是同一份**。前端各写一份
    名字表，结果只会是一个「界面上有、后端不认」的选项 —— 一次必然失败的提交，而且报错
    离用户的操作很远。参数名也一样：界面照它渲染输入框，就不会出现「配了 nb_words 但
    后端没收到」这种沉默失效。
    """
    items = [
        {"name": "choice", "label": "从固定值里选", "params": ["values"]},
        {"name": "random_int", "label": "随机整数", "params": ["min", "max"]},
        {"name": "date", "label": "随机日期", "params": ["format", "start", "end"]},
    ]
    for name in sorted(FAKER_PROVIDERS):
        items.append({"name": name, "label": _GENERATOR_LABELS.get(name, name),
                      "params": list(_PROVIDER_ARGS.get(name, ()))})
    items.sort(key=lambda item: item["name"])
    return items


class RandomGenerator(BaseGenerator):
    """faker 的语义化随机值 + 几个显式生成器（choice / random_int / date）。

    每个值都从 `(种子, 字段, 行号, 尝试次数)` 重新播种再取 —— faker 的内部状态是全局
    的，不逐值重播种的话，批切分一变值就变，续跑就不可能逐字节一致。
    """

    def __init__(self, field_name: str, rule: GenerateRule, *, seed: int = 0):
        super().__init__(field_name, rule, seed=seed)
        params = dict(rule.params or {})
        self.name = (rule.generator or "").strip()
        if not self.name:
            raise CleanGenerateError(f"字段「{field_name}」的随机生成规则没有指定 generator")
        self.locale = str(params.get("locale") or DEFAULT_LOCALE)
        self.null_ratio = float(params.get("null_ratio") or 0.0)
        if not 0.0 <= self.null_ratio <= 1.0:
            raise CleanGenerateError(f"字段「{field_name}」的 null_ratio 必须在 0 到 1 之间")
        self.params = params
        self._values = list(params.get("values") or [])
        self._fake = None
        if self.name == "choice":
            if not self._values:
                raise CleanGenerateError(f"字段「{field_name}」用 choice 生成但没有给 values")
        elif self.name == "random_int":
            self._min = int(params.get("min", 0))
            self._max = int(params.get("max", 1000))
            if self._max < self._min:
                raise CleanGenerateError(f"字段「{field_name}」的 max 小于 min")
        elif self.name in ("date", "date_between"):
            self._date_format = str(params.get("format") or "%Y-%m-%d")
            self._start = _parse_date(params.get("start")) or date(2000, 1, 1)
            self._end = _parse_date(params.get("end")) or date(2030, 12, 31)
            if self._end < self._start:
                raise CleanGenerateError(f"字段「{field_name}」的结束日期早于开始日期")
            if self.name == "date":
                # `date` 只说「要一个日期」，区间取配置的；`date_between` 同义，
                # 保留两个名字是因为用户在界面上看到的是前者
                self.name = "date_between"
        elif self.name not in FAKER_PROVIDERS:
            raise CleanGenerateError(
                f"字段「{field_name}」的生成器「{self.name}」不认识；"
                f"可用：choice、random_int、date、{', '.join(sorted(FAKER_PROVIDERS))}"
            )

    def _rng(self, index: int, attempt: int) -> random.Random:
        return random.Random(deterministic_seed(self.seed, self.field_name, index, attempt))

    def _value(self, index: int, row: Mapping[str, Any], attempt: int) -> Any:
        rng = self._rng(index, attempt)
        if self.null_ratio and rng.random() < self.null_ratio:
            return ""
        if self.name == "choice":
            return rng.choice(self._values)
        if self.name == "random_int":
            return rng.randint(self._min, self._max)
        if self.name == "date_between":
            span = (self._end - self._start).days
            offset = rng.randint(0, span) if span else 0
            return (self._start + timedelta(days=offset)).strftime(self._date_format)
        fake = self._faker()
        fake.seed_instance(deterministic_seed(self.seed, self.field_name, index, attempt))
        provider = getattr(fake, FAKER_PROVIDERS[self.name])
        args = self._call_args()
        return provider(**args) if args else provider()

    def _faker(self):
        if self._fake is None:
            from faker import Faker

            try:
                self._fake = Faker(self.locale)
            except AttributeError as exc:            # faker 不认识这个 locale
                raise CleanGenerateError(
                    f"faker 不支持语言区域「{self.locale}」"
                ) from exc
        return self._fake

    def _call_args(self) -> dict[str, Any]:
        allowed = _PROVIDER_ARGS.get(self.name, ())
        args = {name: self.params[name] for name in allowed if name in self.params}
        if self.name == "pystr":
            args.setdefault("min_chars", 4)
            args.setdefault("max_chars", 12)
        return args


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    raise CleanGenerateError(f"日期「{value}」看不懂，请用 2020-01-31 这种写法")


# =========================================================================== 序列


class SequenceGenerator(BaseGenerator):
    """`start + step * 行号`，按 `width` 左补零。

    刻意**按文件内行号算**而不是维护一个自增计数器：行号来自已提交的 `rows_in`，所以续跑
    天然接得上，不需要额外的状态。代价是每个文件都从 `start` 重新开始 —— 对「给每个产物
    文件编号」这个用法来说正是想要的；要跨文件连续编号得用模板拼文件名的字段。
    """

    def __init__(self, field_name: str, rule: GenerateRule, *, seed: int = 0):
        super().__init__(field_name, rule, seed=seed)
        self.start = int(rule.start)
        self.step = int(rule.step)
        self.width = int(rule.width or 0)
        if self.step == 0:
            raise CleanGenerateError(f"字段「{field_name}」的序列步长不能为 0")
        if not 0 <= self.width <= 32:
            raise CleanGenerateError(f"字段「{field_name}」的序列宽度要在 0 到 32 之间")

    def _value(self, index: int, row: Mapping[str, Any], attempt: int) -> Any:
        value = self.start + self.step * index
        if self.width and value >= 0:
            return f"{value:0{self.width}d}"
        return value


# =========================================================================== 模板


class TemplateGenerator(BaseGenerator):
    """`kind="expression"`：把 `expression` 当模板渲染（`${字段}` 引用已有字段）。

    不做算术、不 eval。需求要的是「用 ${字段名} 引用现有字段」，而 eval 会把一个数据
    清洗工具变成代码执行入口 —— 那件事已经由 `clean_python` 的子进程隔离明确承担了，
    不应该再从模板这条路偷偷开一个没有隔离的口子。
    """

    def __init__(self, field_name: str, rule: GenerateRule, *, seed: int = 0):
        super().__init__(field_name, rule, seed=seed)
        self.template = rule.expression or ""
        if not self.template.strip():
            raise CleanGenerateError(f"字段「{field_name}」的模板是空的")

    def _value(self, index: int, row: Mapping[str, Any], attempt: int) -> Any:
        return render_template(self.template, row)

    def validate(self, available: Sequence[str]) -> None:
        problems = validate_template(self.template, available)
        if problems:
            raise CleanGenerateError(
                f"字段「{self.field_name}」：" + "；".join(problems)
            )


# =========================================================================== 装配


class LlmGenerator(BaseGenerator):
    """占位：真正的实现在 `clean_llm.py`，由它注册进来。

    放在这里是为了让「引擎只跟 `BaseGenerator` 打交道」这条边界成立 —— 引擎不需要知道
    大模型的存在，也不需要知道端口池、熔断、缓存这些东西。
    """

    def __init__(self, *args: Any, **kwargs: Any):
        raise CleanGenerateError("大模型生成器由 clean_llm 提供，不要直接构造")


def build_generator(
    spec: FieldSpec,
    *,
    seed: int = 0,
    llm_factory: Any = None,
) -> BaseGenerator | None:
    """按字段的生成规则造一个生成器。没有生成规则时返回 None。

    `llm_factory` 是 `(spec, rule, seed) -> BaseGenerator` 的工厂，由 `clean_llm` 注入 ——
    本地生成这条路上不 import 大模型模块，测试也就不需要网络。
    """
    rule = spec.generate
    if rule is None:
        return None
    if rule.kind == "random":
        return RandomGenerator(spec.dest, rule, seed=seed)
    if rule.kind == "sequence":
        return SequenceGenerator(spec.dest, rule, seed=seed)
    if rule.kind == "expression":
        return TemplateGenerator(spec.dest, rule, seed=seed)
    if rule.kind == "llm":
        if llm_factory is None:
            raise CleanGenerateError(
                f"字段「{spec.dest}」要用大模型生成，但这次运行没有配好模型池"
            )
        return llm_factory(spec, rule, seed)
    raise CleanGenerateError(f"字段「{spec.dest}」的生成类型「{rule.kind}」不认识")


def build_generators(
    config: CleanTaskConfig,
    *,
    seed: int = 0,
    llm_factory: Any = None,
) -> list[BaseGenerator]:
    """生成器列表，顺序即字段声明顺序（后面的字段能引用前面已生成的值）。

    返回列表而不是「字段名 → 生成器」的字典：一条规则可以管多个字段，字典形式会让同一条
    规则被构造两次（大模型那条路就是两次请求、两份钱）。列表让「一条规则 = 一个生成器
    实例」成为结构上的事实，多字段由实例的 `output_fields` 表达。
    """
    generators: list[BaseGenerator] = []
    for spec in config.fields:
        generator = build_generator(spec, seed=seed, llm_factory=llm_factory)
        if generator is not None:
            generators.append(generator)
    return generators


def generated_fields(generators: Sequence[BaseGenerator]) -> list[str]:
    """全部会被生成覆盖的目的字段（按声明顺序）。"""
    fields: list[str] = []
    for generator in generators:
        for name in generator.output_fields:
            if name not in fields:
                fields.append(name)
    return fields


def describe_generators(generators: Sequence[BaseGenerator]) -> dict[str, str]:
    """给报告用的一句话说明（不含密钥，可以安全落盘）。"""
    described: dict[str, str] = {}
    for generator in generators:
        rule = generator.rule
        if rule.kind == "random":
            text = f"随机（{rule.generator}，{getattr(generator, 'locale', '')}）"
        elif rule.kind == "sequence":
            text = f"序列（{rule.start} 起，步长 {rule.step}）"
        elif rule.kind == "expression":
            text = "模板"
        elif rule.kind == "llm":
            text = generator.describe() if hasattr(generator, "describe") else "大模型"
        else:
            text = rule.kind
        for name in generator.output_fields:
            described[name] = text
    return described
