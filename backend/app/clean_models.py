"""文件清洗工具的配置模型。

这里只描述「用户配置了什么」，不含任何执行逻辑 —— 执行在 clean_schema / clean_ops /
clean_engine 里。分开的理由是配置要能被完整地哈希（clean_config_hash）用于断点续跑的
一致性校验，而执行逻辑里的任何东西都不该影响那个哈希。

字段是按「目的字段」组织的（FieldSpec），不是平行的 fields/constraints/generates 三张表：
生成规则、约束、回退策略全都是挂在某一个目的字段上的，拆成三张表就得靠字段名去 join，
而字段名拼错时 join 会静默漏掉规则。放在一起则拼错就是拼错。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 违规类型。优先级在 clean_schema 里硬编码，这里的顺序不代表优先级。
ViolationKind = Literal[
    "non_null", "enum", "regex", "length", "range", "format", "type", "unique", "llm_error"
]

ConstraintKind = Literal["non_null", "enum", "regex", "length", "range", "format", "type", "unique"]


class _Model(BaseModel):
    # extra="forbid"：配置来自页面表单与 API JSON，拼错的键必须报错而不是被静默忽略 ——
    # 一个被忽略的约束会让报告声称「全部通过」而实际没检查。
    model_config = ConfigDict(extra="forbid")


class Constraint(_Model):
    """单个字段约束。按 kind 取用不同的参数，由 _check_params 校验。"""

    kind: ConstraintKind
    # error: 违约即走回退；warn: 只计数并进报告，数据原样通过。
    severity: Literal["error", "warn"] = "error"

    # kind="enum"
    values: list[str] = Field(default_factory=list)
    case_sensitive: bool = True

    # kind="regex"。mode="search" 时「截断」策略可以保留匹配到的子串。
    pattern: str = ""
    mode: Literal["fullmatch", "search"] = "fullmatch"

    # kind="length"（按字符数计，不是字节数 —— 中文场景下按字节算长度几乎总是错的）
    min_length: int | None = None
    max_length: int | None = None
    pad_char: str = " "

    # kind="range"
    min_value: float | None = None
    max_value: float | None = None

    # kind="format" / kind="type"
    value_kind: Literal[
        "int", "float", "bool", "str", "date", "datetime", "time", "email", "url", "ipv4"
    ] | None = None
    date_formats: list[str] = Field(default_factory=list)

    # kind="unique"
    unique_scope: Literal["file", "task"] = "task"

    def _check_params(self):
        kind = self.kind
        if kind == "regex":
            if not self.pattern:
                raise ValueError("regex 约束必须提供 pattern")
            # Python 的 re 没有超时。RE2 优先（线性时间），回落时靠这个上限兜底。
            if len(self.pattern) > 4096:
                raise ValueError("正则过长（上限 4096 字符）")
        elif kind == "enum":
            if not self.values:
                raise ValueError("enum 约束必须提供 values")
        elif kind == "length":
            if self.min_length is None and self.max_length is None:
                raise ValueError("length 约束至少要提供 min_length 或 max_length")
            if self.min_length is not None and self.min_length < 0:
                raise ValueError("min_length 不能为负")
            if (
                self.min_length is not None
                and self.max_length is not None
                and self.min_length > self.max_length
            ):
                raise ValueError("min_length 不能大于 max_length")
            if len(self.pad_char) != 1:
                raise ValueError("pad_char 必须是单个字符")
        elif kind == "range":
            if self.min_value is None and self.max_value is None:
                raise ValueError("range 约束至少要提供 min_value 或 max_value")
            if (
                self.min_value is not None
                and self.max_value is not None
                and self.min_value > self.max_value
            ):
                raise ValueError("min_value 不能大于 max_value")
        elif kind == "format":
            if self.value_kind not in ("date", "datetime", "time", "email", "url", "ipv4"):
                raise ValueError("format 约束的 value_kind 必须是 date/datetime/time/email/url/ipv4")
        elif kind == "type":
            if self.value_kind not in ("int", "float", "bool", "str"):
                raise ValueError("type 约束的 value_kind 必须是 int/float/bool/str")

    @model_validator(mode="after")
    def _validate(self):
        self._check_params()
        return self


class Fallback(_Model):
    """约束不通过时的处理。策略语义按违规类型分别定义，见 clean_schema.apply_fallback。"""

    on_violation: Literal["retry", "keep", "truncate"] = "keep"
    # retry 专用：额外重试次数（不含首次尝试）。只对有生成规则的字段有意义。
    max_retries: int = Field(default=2, ge=0, le=10)
    # keep 与 truncate 在无法处理时的兜底值
    default: Any = None
    # truncate 处理 unique 冲突时的后缀
    unique_suffix: str = "_"


class GenerateRule(_Model):
    """字段生成规则。random 与 llm 二选一（由 kind 决定）。"""

    kind: Literal["random", "llm", "sequence", "expression"]
    # 一次生成多个字段时用（expression / llm 的批量产出）
    output_fields: list[str] = Field(default_factory=list)

    # kind="random"。generator 是内置名或 faker 的 "faker.xxx"；params 传给生成器。
    generator: str = ""
    params: dict[str, Any] = Field(default_factory=dict)

    # kind="llm"
    prompt: str = ""          # 模板，${字段名} 引用现有字段
    system: str = ""
    batch_size: int = Field(default=1, ge=1, le=200)
    temperature: float | None = None
    max_tokens: int | None = None

    # kind="expression"：模板语言，同样用 ${字段名}
    expression: str = ""

    # kind="sequence"
    start: int = 1
    step: int = 1
    width: int = 0            # 0 = 不补零

    @model_validator(mode="after")
    def _validate(self):
        if self.kind == "random" and not self.generator:
            raise ValueError("random 生成必须提供 generator")
        if self.kind == "llm" and not self.prompt.strip():
            raise ValueError("llm 生成必须提供 prompt")
        if self.kind == "expression" and not self.expression.strip():
            raise ValueError("expression 生成必须提供 expression")
        if self.batch_size > 1 and self.kind != "llm":
            raise ValueError("batch_size 只对 llm 生成有意义")
        return self


class FieldSpec(_Model):
    """一个目的字段的完整定义：来源、生成、约束、回退。

    source 为空即「新列」—— 这是需求里「仅配置目的字段时则为新列」的落地方式。
    为一个已有目的字段配 source 就是重命名（未配 source 的源列则被丢弃）。
    """

    dest: str
    source: str = ""
    default: Any = None
    generate: GenerateRule | None = None
    constraints: list[Constraint] = Field(default_factory=list)
    fallback: Fallback | None = None

    @model_validator(mode="after")
    def _validate(self):
        if not self.dest.strip():
            raise ValueError("目的字段名不能为空")
        # retry 对没有生成规则的字段是无意义的：原始值无法「重新生成」。
        # 这是最容易配错的一项，所以在创建时就拒绝，而不是运行到一半才发现。
        if self.fallback and self.fallback.on_violation == "retry" and not self.generate:
            raise ValueError(f"字段 {self.dest}：on_violation=retry 需要该字段有生成规则")
        for c in self.constraints:
            if c.kind == "unique" and c.severity != "error":
                raise ValueError(f"字段 {self.dest}：unique 约束的 severity 只能是 error")
        return self


class CleanOp(_Model):
    """声明式清洗算子。刻意保持一个小集合 —— 每个算子都必须是单遍、有界、可能流式的。"""

    op: Literal[
        "trim", "lower", "upper", "nfkc", "replace", "regex_replace", "fill_null",
        "ffill", "cast", "date_format", "dedupe", "drop_null", "concat", "slice", "number_format", "filter",
    ]
    field: str = ""
    fields: list[str] = Field(default_factory=list)
    dest: str = ""
    pattern: str = ""
    replacement: str = ""
    value: Any = None
    separator: str = ""
    start: int | None = None
    end: int | None = None
    value_kind: Literal["int", "float", "str", "date", "datetime"] | None = None
    # date_format：date_format 算子的**输出**格式；date_formats 是**输入**格式候选（空 = ISO）。
    # 与 Constraint 的同名字段口径一致，界面上也能用同一个控件渲染。
    date_format: str = ""
    date_formats: list[str] = Field(default_factory=list)
    # number_format：小数点后位数（None = int 取 0、float 保留原样）与千分位分隔
    decimals: int | None = Field(default=None, ge=0, le=12)
    thousands: bool = False
    condition: Literal["equals", "contains", "starts_with", "ends_with", "in", "is_empty", "not_empty", "gt", "gte", "lt", "lte"] = "equals"
    filter_action: Literal["keep", "drop"] = "keep"
    filter_values: list[str] = Field(default_factory=list)
    ignore_case: bool = False
    # dedupe
    keep: Literal["first"] = "first"     # 只有 first 是单遍可算的
    subset: list[str] = Field(default_factory=list)
    scope: Literal["file", "task"] = "file"

    @model_validator(mode="after")
    def _validate(self):
        needs_field = {
            "trim", "lower", "upper", "nfkc", "replace", "regex_replace", "fill_null",
            "ffill", "cast", "date_format", "slice", "number_format",
        }
        if self.op in needs_field and not self.field:
            raise ValueError(f"{self.op} 算子必须指定 field")
        if self.op == "filter":
            if not self.field:
                raise ValueError("按列过滤必须选择字段")
            if self.condition == "in" and not self.filter_values:
                raise ValueError("按列过滤必须填写至少一个候选值")
            if self.condition not in ("in", "is_empty", "not_empty") and (self.value is None or str(self.value) == ""):
                raise ValueError("按列过滤必须填写比较值")
            if self.condition in ("gt", "gte", "lt", "lte"):
                try:
                    if isinstance(self.value, bool) or not Decimal(str(self.value)).is_finite():
                        raise ValueError("数值过滤必须填写有限数字")
                except InvalidOperation as exc:
                    raise ValueError("数值过滤必须填写有效数字") from exc
        if self.op == "regex_replace" and not self.pattern:
            raise ValueError("regex_replace 必须提供 pattern")
        if self.op == "replace" and not self.pattern:
            # str.replace("", x) 会在每个字符之间插入 x —— 一个空 pattern 不是「什么都不做」
            raise ValueError("replace 的 pattern 不能为空（空串会在每个字符间插入替换文本）")
        if self.op == "concat" and (not self.fields or not self.dest):
            raise ValueError("concat 必须提供 fields 与 dest")
        if self.op == "drop_null" and not self.field and not self.fields:
            # 不指定字段的 drop_null 是「什么都不删」。静默无操作正是最坏的一种配置错误：
            # 任务成功、报告全绿，而用户以为删过了。
            raise ValueError("drop_null 必须指定 field 或 fields")
        if self.op in ("cast", "number_format") and not self.value_kind:
            raise ValueError(f"{self.op} 必须提供 value_kind")
        if self.op == "date_format" and not self.date_format:
            raise ValueError("date_format 必须提供 date_format")
        if self.op == "slice" and self.start is None and self.end is None:
            raise ValueError("slice 至少要提供 start 或 end")
        return self


class PythonOptions(_Model):
    """用户 Python 函数的运行时护栏。与「函数写什么」无关，只跟怎么跑有关。"""

    # 内存上限走 RLIMIT_AS（**虚拟**地址空间，比 RSS 大），所以下限 256MB。
    # 低于 512MB 时子进程会打一条提醒：正常函数也可能因解释器自身开销撞上限。
    memory_mb: int = Field(default=1024, ge=64, le=65536)
    # 重启次数上限。超过就整个任务禁用 Python 函数、数据原样通过 —— 不给一个每块都
    # 超时的函数反复付启动代价的机会。
    max_restarts: int = Field(default=5, ge=0, le=100)
    start_timeout_s: float = Field(default=30.0, gt=0, le=600)
    # 没给函数级 timeout_s 时的默认值
    timeout_default_s: float = Field(default=5.0, gt=0, le=600)
    stderr_per_minute: int = Field(default=50, ge=0, le=10000)


class PythonFunction(_Model):
    """用户自定义 Python 函数。跑在隔离子进程里，见 clean_python 的模块 docstring。"""

    name: str
    phase: Literal["pre", "post"] = "pre"
    # 函数体源码。约定入口为 transform，签名见 clean_worker。
    source: str
    entry: str = "transform"
    input_fields: list[str] = Field(default_factory=list)
    output_fields: list[str] = Field(default_factory=list)
    # row: 逐行 dict 进 dict 出；column: 一批值进一批值出（省掉逐行 IPC 开销）
    mode: Literal["row", "column"] = "row"
    timeout_s: float = Field(default=5.0, gt=0, le=600)
    # 单行抛异常时怎么处理。empty: 把 output_fields 置空（默认 —— 出错的行不该带着
    # 半截结果混进产物）；keep: 保留这一行原值。两种都会计数并打一条 warn。
    on_row_error: Literal["empty", "keep"] = "empty"

    @model_validator(mode="after")
    def _validate(self):
        if not self.source.strip():
            raise ValueError(f"函数 {self.name} 的源码不能为空")
        if self.mode == "column" and len(self.input_fields) != 1:
            raise ValueError(f"函数 {self.name}：column 模式只能有 1 个 input_field")
        if not self.output_fields:
            raise ValueError(f"函数 {self.name}：至少要声明一个 output_field，否则结果无处可写")
        return self


class InputOptions(_Model):
    # None = 自动探测。CSV 用 sniff_delimiter，编码用 BOM + charset-normalizer。
    delimiter: str | None = None
    encoding: str | None = None
    header_row: int = Field(default=0, ge=0)
    sheet: str | int | None = None          # xlsx 工作表；None = 第一个
    # 以 =+-@ 开头的值在 Excel 里是公式。默认强制为字符串并在报告里计数。
    sanitize_formula: bool = True
    # 精确进度（多读一遍文件）vs 估算进度（按大小外推）
    precount: bool = False
    # 目录来源时的文件筛选
    include: list[str] = Field(default_factory=lambda: ["*.csv", "*.xlsx", "*.jsonl", "*.ndjson"])
    recursive: bool = True


class SourceSpec(_Model):
    mode: Literal["upload", "server_path"] = "upload"
    upload_id: str = ""
    paths: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate(self):
        if self.mode == "upload" and not self.upload_id:
            raise ValueError("upload 模式必须提供 upload_id")
        if self.mode == "server_path" and not self.paths:
            raise ValueError("server_path 模式必须提供 paths")
        return self


class OutputOptions(_Model):
    format: Literal["csv", "xlsx", "jsonl", "parquet"] = "csv"
    # 多文件表头不一致时：union 取并集，strict 要求每个文件都有全部映射列
    layout: Literal["union", "strict"] = "union"
    on_missing: Literal["empty", "error", "skip_row"] = "empty"
    csv_delimiter: str = ","
    # 0 = 自动（有 LLM 生成字段时 2000，否则 50000）。窗口越大写放大越小、崩溃重算越多。
    commit_rows: int = Field(default=0, ge=0)
    checkpoint_rows: int = Field(default=256, ge=1)
    # 蓄水池采样量。它是 state.json 里最大的东西，所以不能随意调大（见 clean_state）。
    reservoir_size: int = Field(default=2000, ge=100, le=50000)
    # 报告明细条数：第 5 节「回退样本」每段列几行、第 7 节「字段样本（高频值）」
    # 每个字段列几个值。**它只管报告里列多少，不改变统计精度** —— 频次上限另有下限
    # （见 clean_state：`limit = max(1000, top_k)`），所以调小它不会让误差界变差。
    # 0 = 报告不列这两张明细表，只留聚合计数（几百万行的文件上这是想要的样子）。
    top_k: int = Field(default=8, ge=0, le=1000)
    # 1-in-N 分块用逐行参考实现抽查列式快路径，发现不一致就记 error。0 = 关闭。
    verify_every: int = Field(default=0, ge=0)
    durable_commit: bool = False


class LlmTaskOptions(_Model):
    """任务级的大模型选择与覆盖。密钥不在任务里 —— 见 LlmEndpoint。"""

    endpoint_ids: list[str] = Field(default_factory=list)
    temperature: float | None = None
    max_tokens: int | None = None
    # 只对前 N 行做生成（0 = 全部）。GB 级数据 + 逐行生成 = 上百小时，这是护栏之一。
    sample_rows: int = Field(default=0, ge=0)
    max_calls: int = Field(default=50000, ge=0)
    # 错误率超过它就判失败 —— 一个「成功」但全是回退值的任务比诚实失败更糟
    abort_error_rate: float = Field(default=0.5, ge=0, le=1)


class CleanTaskConfig(_Model):
    name: str = ""
    source: SourceSpec
    input: InputOptions = Field(default_factory=InputOptions)
    fields: list[FieldSpec]
    ops: list[CleanOp] = Field(default_factory=list)
    functions: list[PythonFunction] = Field(default_factory=list)
    python: PythonOptions = Field(default_factory=PythonOptions)
    output: OutputOptions = Field(default_factory=OutputOptions)
    llm: LlmTaskOptions = Field(default_factory=LlmTaskOptions)

    @model_validator(mode="after")
    def _validate(self):
        if not self.fields:
            raise ValueError("至少需要一个目的字段")
        dests = [f.dest for f in self.fields]
        dupes = {d for d in dests if dests.count(d) > 1}
        if dupes:
            raise ValueError(f"目的字段重复：{'、'.join(sorted(dupes))}")
        sources = [f.source for f in self.fields if f.source]
        dupes = {s for s in sources if sources.count(s) > 1}
        if dupes:
            # 同一个源列映射到两个目的字段是合法的（复制），但几乎总是配置错误
            pass
        if any(f.generate and f.generate.kind == "llm" for f in self.fields):
            if not self.llm.endpoint_ids:
                raise ValueError("配置了大模型生成字段，但没有选择任何大模型端点")
        if self.output.layout == "strict":
            for f in self.fields:
                if not f.source:
                    raise ValueError(f"layout=strict 下每个字段都必须有 source，但 {f.dest} 没有")
        dest_set = set(dests)
        for fn in self.functions:
            unknown = [f for f in fn.output_fields if f not in dest_set]
            if unknown:
                raise ValueError(
                    f"函数 {fn.name} 的 output_fields 里有未定义的目的字段："
                    f"{'、'.join(unknown)}（写回不存在的列会被静默丢掉）"
                )
        return self

    def config_hash(self) -> str:
        """配置的稳定指纹，断点续跑用它判断「配置有没有被改过」。

        用 model_dump_json 而不是 model_dump：前者保证键顺序稳定且值可 JSON 化。
        exclude_none=False 是刻意的 —— 一个字段从 None 变成 "" 也算配置变更。
        """
        payload = self.model_dump_json(exclude={"name"})
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]

    def llm_fields(self) -> list[str]:
        return [f.dest for f in self.fields if f.generate and f.generate.kind == "llm"]

    def dest_fields(self) -> list[str]:
        return [f.dest for f in self.fields]


class CleanSourceConfig(_Model):
    """探测（`/inspect`）与数据来源检查需要的**最小**配置：只有来源与读入选项。

    为什么要单独一个模型，而不是给 `CleanTaskConfig` 的校验加个开关：`fields` 为空在
    整条清洗链路上始终是非法的（没有目的字段就没有输出），**只在探测这一步合法** ——
    而探测恰恰是配置字段的前提（不知道表头就没法填映射）。所以那个「这里可以空」的口子
    如果开在主模型上，下游每一处都得重新判一次「这次是探测还是真跑」；开在这里，主模型
    的不变量原样保持。

    带 `extra="forbid"`，所以调用方只投影出 `source` 与 `input` 两个键，别把整份配置丢进来。
    """

    source: SourceSpec
    input: InputOptions = Field(default_factory=InputOptions)


def canonical_json(value: Any) -> str:
    """排序键的紧凑 JSON，用于需要稳定表示的场合（日志、报告、指纹）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
