"""I/O 层：探测、读、写。引擎只见纯 Python 的 (表头, 行) —— polars/fastexcel 只活在这一层。

这一层有几个贯穿全局的决定，都是实测逼出来的，改之前先读完：

1. **CSV 用标准库 `csv` 读，不用 polars。** 同一份 0.53GB / 1200 万行文件量出来的对照：

   | 读法 | 吞吐 | 峰值 RSS |
   |---|---|---|
   | `csv.reader` 分批 | 1501k 行/s | **62MB** |
   | `scan_csv().slice().collect()` 分批 | 733k 行/s | 613MB |
   | 四文件并发（各一线程），`csv.reader` | 1926k 行/s | **63MB** |
   | 四文件并发（各一线程），polars slice | 2199k 行/s | 163MB |

   并发下两者接近（差 12%）；单文件下 `csv` 快一倍、内存小一个数量级。更关键的是内存
   **有界是结构性的**，而 polars 那条路我给不出上界：`collect_batches()` 在慢消费者下会让
   RSS 涨到文件的 1.33 倍（实测 0.53GB 文件 → 720MB），而且它会报出与真实物化对不上的批
   高度（「0.53GB 解析 0.2s」这类数字物理上不可能），我无法据此声明任何内存保证。

   两条附带收益也是 polars 给不了的：

   - **参差行可以精确计数**（`len(row) != 表头宽度`）。polars 的 `truncate_ragged_lines`
     是**静默**补齐/截断、不报条数，而报告里必须写出「有几行字段数不对」。
   - **非 UTF-8 不再需要转存**。polars 只有 `scan_csv` 能流式读，而它只吃 utf8/utf8-lossy
     （`utf8-lossy` 会把 GBK 的中文读成一串 U+FFFD 替换字符却不报错，比直接失败危险得多），
     所以曾经必须在任务目录里先转存一份全尺寸 UTF-8 副本。`open(encoding=...)` 就地解码，
     那套转存和它的缓存失效逻辑整个删掉了 —— GB 文件不再多占一份磁盘。

   代价只有一个，且是刻意的：`csv` 逐记录解析、**持 GIL**，所以多文件并发时解析不能像
   polars 那样真并行（上表 1.9M vs 2.2M 行/s 聚合）。相对整条流水线（用户函数子进程、
   大模型、列式求值）这个差距可以忽略。

2. **类型一律是字符串。** `csv` 模块不做任何类型推断，`007`、`1e3`、`13800138000` 原样保留
   —— 这正是清洗工具最该保住的东西，也是 polars 那条路必须靠 `infer_schema_length=0` 去
   压制的行为。类型转换是用户显式配的 cast 算子的事，不是读文件时的副作用。

3. **参差行补齐/截断到表头宽度，并精确计数。** 字段多了截掉、少了补空串，计入 `ragged_rows`
   进报告。`/inspect` 在配置阶段就报出样本里的条数（那时用户还能改），全量精确计数随
   `precount=True` 一起做。

两个标准库陷阱已经在模块加载时处理掉：

- **`csv.field_size_limit` 默认只有 128KB**，超长字段（实测 20 万字符）直接抛
  `Error: field larger than field limit`，整个文件读不出来。加载时抬到平台最大值
  （见 `_raise_field_limit`）。
- **字段里含 NUL 字节不会报错**（实测能正常读出含 NUL 的单元格），不需要特殊处理 ——
  但反过来也意味着「解析错误」计数不能指望 NUL 来触发。

xlsx 另有两条硬约束：

- `to_polars()` 会把整张表物化（实测 105 万行 × 20 列 → 2.1GB RSS），超过阈值必须分块读。
  分块是 O(n²/chunk)：calamine 不支持随机访问，`skip_rows=N` 仍然从表头扫起（实测跳 10 万行
  与读全文同耗时）。用时间换内存。Excel 自身 1048576 行上限让这个 n 有界，所以还能接受。
- `write()` 会把 `=SUM(A1:A9)` 当公式写进去、回读变成 `'0'`（静默改数据）；`write_string`
  才原样保留文本。所以 xlsx 侧一律 `write_string`。另注意 `write_string` 超过 32767 字符会
  **静默截断**并返回 -2，得自己盯着。
"""

from __future__ import annotations

import codecs
import csv
import io
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

import numpy as np
import orjson
import polars as pl

from .clean_models import InputOptions, OutputOptions
from .clean_schema import cell_text as _cell_text

# Excel 的硬上限。写到就该明确失败并建议改用 CSV，而不是产出一个 Excel 打不开的文件。
EXCEL_MAX_ROWS = 1_048_576
EXCEL_MAX_COLS = 16_384
# 单元格文本上限。xlsxwriter 超限会**静默截断**并返回 -2，所以要自己盯着。
EXCEL_MAX_CHARS = 32_767

# xlsx 一次性物化的单元格上限。实测约 0.2KB/格，200 万格 ≈ 400MB。
XLSX_MATERIALIZE_CELLS = 2_000_000

CSV_DELIMITERS = (",", ";", "\t", "|")

# csv.reader 连续抛错这么多条就放弃整个文件。有些畸形输入会让它在同一个位置反复抛错而不
# 前进，没有这道闸门就是死循环。
MAX_CONSECUTIVE_CSV_ERRORS = 1000

_JSONL_KINDS = (".jsonl", ".ndjson")

# UTF-8 BOM。写成 chr() 而不是字面量：源码里一个不可见字符既看不出来也留不住。
_BOM = chr(0xFEFF)
# U+FFFD 替换字符。同上，用 chr() 而不是字面量。
_REPLACEMENT = chr(0xFFFD)

# 公式注入前缀（OWASP）。CSV 没有单元格类型，只能在文本层面加一个引号挡住 Excel 的解析。
_FORMULA_RE = re.compile(r"^[=+\-@\t\r]")


def _raise_field_limit() -> None:
    """把 `csv` 的字段上限从默认的 128KB 抬到平台最大值。

    实测：一个 20 万字符的字段在默认上限下抛 `Error: field larger than field limit`，整个
    文件读不出来。GB 级数据里出现超长文本字段（JSON 片段、备注、报错堆栈）并不罕见，而
    128KB 是道连提示都没有的硬墙。个别平台把上限设成 `sys.maxsize` 会溢出 C long，回落。
    """
    try:
        csv.field_size_limit(sys.maxsize)
    except OverflowError:  # pragma: no cover - 32 位平台
        csv.field_size_limit(2**31 - 1)


_raise_field_limit()



LOGGER = logging.getLogger(__name__)


class CleanIoError(Exception):
    """这一层的所有失败都归它，便于 API 层统一转成 400/413。"""


# =========================================================================== 探测


@dataclass(slots=True)
class Detection:
    """`/inspect` 的返回值。用户在界面上据此确认分隔符/编码/表头。"""

    path: str
    kind: str
    encoding: str = "utf-8"
    encoding_confidence: float = 1.0
    delimiter: str | None = None
    delimiter_confidence: float = 1.0
    headers: list[str] = field(default_factory=list)
    sample_rows: list[list[Any]] = field(default_factory=list)
    sheet_names: list[str] = field(default_factory=list)
    sheet: str | int | None = None
    row_estimate: int | None = None
    ragged_rows: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind,
            "encoding": self.encoding,
            "encoding_confidence": round(self.encoding_confidence, 3),
            "delimiter": self.delimiter,
            "delimiter_confidence": round(self.delimiter_confidence, 3),
            "headers": self.headers,
            "sample_rows": self.sample_rows,
            "sheet_names": self.sheet_names,
            "sheet": self.sheet,
            "row_estimate": self.row_estimate,
            "ragged_rows": self.ragged_rows,
            "warnings": self.warnings,
        }


def file_kind(path: str | Path, allow_unknown: bool = False) -> str:
    suffix = Path(path).suffix.lower()
    if suffix in (".csv", ".tsv", ".txt"):
        return "csv"
    if suffix in (".xlsx", ".xlsm"):
        return "xlsx"
    if suffix in _JSONL_KINDS:
        return "jsonl"
    if suffix == ".parquet":
        return "parquet"
    if allow_unknown:
        return "unknown"
    raise CleanIoError(f"不支持的文件类型：{suffix or path}")


def read_head(path: str | Path, limit: int = 262_144) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(limit)


# UTF-16/32 家族。没有 BOM 或 NUL 字节模式作证据时，一律不采信这两种 —— 见下面注释。
_UTF16_FAMILY = {
    "utf-16", "utf16", "utf-16-be", "utf-16-le", "utf-16be", "utf-16le",
    "utf-32", "utf32", "utf-32-be", "utf-32-le", "utf-32be", "utf-32le",
}
# 中文语境的遗留编码家族。并列时优先它（见 detect_encoding 结尾）。
_GB_FAMILY = {"gb18030", "gbk", "gb2312", "gb2312-80", "hz", "euc-cn"}


def _normalize_encoding(name: str) -> str:
    return name.strip().lower().replace("_", "-")


# 单字符最多占几个字节（UTF-8 与 gb18030 都是 4）。
_MAX_CHAR_BYTES = 4


def _tail_tolerant_decode(raw: bytes, encoding: str) -> str | None:
    """严格解码；**只容忍末尾被样本边界切断的那一个字符**。返回 None 表示「不是这个编码」。

    这条容忍是必需的，而且是实测踩出来的：`read_head` 固定读 256KB，而这个边界与字符边界
    毫无关系 —— 一个纯 UTF-8 的中文文件只要第 262144 个字节落在一个三字节字的中间，严格
    解码就会失败，于是探测流程掉进统计推断，把整个 UTF-8 文件判成 gb18030。后果是列名与
    全部数据都解成乱码（`'濮撳悕'` 这种），**而且不报任何错**：逗号与换行都是 ASCII，
    分隔符探测照常成功，只有编码是错的。中文 UTF-8 文件撞上这个边界的概率约三分之二。

    更早位置的失败才是真的不是这个编码，必须如实返回 None。
    """
    try:
        return raw.decode(encoding, errors="strict")
    except (LookupError, TypeError):
        return None
    except UnicodeDecodeError as error:
        # 异常变量在 except 块结束时就失效了，位置必须先取出来。
        cut = error.start
    if cut < len(raw) - _MAX_CHAR_BYTES:
        return None
    try:
        return raw[:cut].decode(encoding, errors="strict")
    except (UnicodeDecodeError, LookupError, TypeError):
        return None


def _decode_candidate(raw: bytes, encoding: str) -> str | None:
    """候选编码是否可信：必须能**严格**解码（允许末尾截断），且解出的文本里含换行。

    换行这一条是关键，不是凑数的。CSV 的头部样本必然含换行，而「编码错配」几乎总把
    换行字节吃进某个多字节字符里，于是解出的文本一行都没有。实测一个 20 字节的 GBK 样本
    被 charset-normalizer 判为 `utf_16_be`（chaos=0.000，看起来完美），解出来是
    `'쏻ퟖⲳ쟊퀊헅죽ⲱ놾ꤊ'` —— 一个换行都没有，被这条挡掉。没有这条检查，一个 GBK 文件会
    被静默地按 UTF-16 读成乱码，而那正是这个工具要处理的输入。
    """
    normalized = _normalize_encoding(encoding)
    if normalized in _UTF16_FAMILY:
        return None
    text = _tail_tolerant_decode(raw, encoding)
    if text is None:
        return None
    return text if ("\n" in text or "\r" in text) else None


def detect_encoding(head: bytes) -> tuple[str, float, list[str]]:
    """返回 (编码, 置信度, 警告)。**BOM 优先于一切统计推断，统计推断必须过验证。**

    顺序不能反：UTF-16 的字节流被 charset-normalizer 猜成 cp1252 时会成功解出一串
    「看起来合理的乱码」（每个字节都是合法 cp1252 字符），比直接报错危险得多。BOM 与
    NUL 字节模式是确定的证据，统计推断只是猜测。

    统计推断这一段的要点是**并列不猜赢**：短样本上 GBK 与 cp949 的分值可以完全一样
    （实测都是 chaos=0.000 且都能解出换行），此时任何一种「选一个」都是赌博。做法是
    按中文语境优先 gb18030，同时把置信度压到 0.4 并**在警告里列出其他可能**，
    让界面去问用户 —— 20 字节的样本本来就不足以可靠区分这两种编码。
    """
    warnings: list[str] = []
    for bom, encoding in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if head.startswith(bom):
            return encoding, 1.0, warnings

    # 无 BOM 的 UTF-16：ASCII 文本在 UTF-16 下每两字节就有一个 0x00。
    sample = head[:4096]
    if sample:
        zeros = sample.count(0)
        if zeros > len(sample) * 0.25:
            even_zeros = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
            odd_zeros = sum(1 for i in range(1, len(sample), 2) if sample[i] == 0)
            encoding = "utf-16-be" if even_zeros > odd_zeros else "utf-16-le"
            warnings.append(f"文件无 BOM，按 NUL 字节分布推断为 {encoding}")
            return encoding, 0.9, warnings

    # 末尾允许被样本边界切断（见 _tail_tolerant_decode）：不带这条容忍，一半以上的中文
    # UTF-8 文件会被判成 gb18030 并把整个文件读成乱码。
    if _tail_tolerant_decode(head, "utf-8") is not None:
        return "utf-8", 1.0, warnings

    try:
        from charset_normalizer import from_bytes
    except ImportError:  # pragma: no cover - charset-normalizer 是显式依赖
        warnings.append("未安装 charset-normalizer，非 UTF-8 输入按 gb18030 处理")
        return "gb18030", 0.3, warnings

    # 只喂头部样本：from_path 会读整个文件，在 GB 级输入上是灾难。
    ranked: list[tuple[float, str]] = []
    for match in from_bytes(head[:65_536]):
        if _decode_candidate(head, match.encoding) is None:
            continue
        ranked.append((float(match.chaos), match.encoding))
    if not ranked:
        for encoding in ("gb18030", "big5", "shift_jis", "cp1252", "latin-1"):
            if _decode_candidate(head, encoding) is not None:
                warnings.append(f"编码探测无可靠结论，暂按 {encoding} 处理，请在界面上确认")
                return encoding, 0.3, warnings
        warnings.append("编码探测失败，按 utf-8 处理（可能有替换字符）")
        return "utf-8", 0.2, warnings

    ranked.sort(key=lambda item: item[0])
    best_chaos = ranked[0][0]
    tied = [item for item in ranked if item[0] <= best_chaos + 1e-9]
    if len(tied) == 1:
        return tied[0][1], 1.0 - min(1.0, best_chaos), warnings

    chosen = next((item for item in tied if _normalize_encoding(item[1]) in _GB_FAMILY), tied[0])
    others = "、".join(item[1] for item in tied if item[1] != chosen[1])
    warnings.append(
        f"编码存在歧义（{chosen[1]} 与 {others} 都能解释该样本），暂按 {chosen[1]} 处理，请确认"
    )
    return chosen[1], 0.4, warnings


def sniff_delimiter(
    text: str, candidates: Sequence[str] = CSV_DELIMITERS
) -> tuple[str | None, float]:
    """给每个候选分隔符打分，返回 (分隔符, 置信度)。

    **不用 `csv.Sniffer`**：它按整段文本的字符频率投票，单列文件、或某列里逗号特别多的
    文件都会被猜错。**也不按 `\\n` 切行**：引号内可以含换行，切完行数就错了。

    真正的判据是「字段数是否稳定」：对的分隔符让每一行切出同样的列数，错的分隔符切出的
    列数随行内容抖动。所以主分是「众数列宽的占比」，列宽只作次要分，最后偏向靠前的候选
    （逗号、制表符在真实数据里最常见）。
    """
    best_delim: str | None = None
    best_score: tuple[float, int, float] = (-1.0, 0, -1.0)
    for index, delimiter in enumerate(candidates):
        if not delimiter:
            continue
        try:
            rows = [
                row
                for row in _iter_csv(text, delimiter, limit=200)
                if row and any(cell.strip() for cell in row)
            ]
        except csv.Error:
            continue
        if len(rows) < 2:
            continue
        widths = [len(row) for row in rows]
        mode_width = max(set(widths), key=widths.count)
        if mode_width < 2:
            continue
        stability = widths.count(mode_width) / len(widths)
        score = (round(stability, 4), min(mode_width, 50), -float(index))
        if score > best_score:
            best_score = score
            best_delim = delimiter
    if best_delim is None:
        return None, 0.0
    return best_delim, min(1.0, best_score[0])


def _iter_csv(text: str, delimiter: str, limit: int | None = None) -> Iterator[list[str]]:
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for index, row in enumerate(reader):
        if limit is not None and index >= limit:
            return
        yield row


def count_ragged(rows: Sequence[Sequence[Any]], width: int) -> int:
    return sum(1 for row in rows if len(row) != width)


def normalize_headers(cells: Sequence[Any]) -> list[str]:
    """表头单元格 → 列名。

    **探测与读取必须共用这一份实现**，否则 `/inspect` 报出的列名与引擎真正读出来的列名
    会差一个看不见的字符，用户辛苦配好的字段映射就会静默全部失配 —— 而「静默全部失配」
    的表现是输出里整列为空，几乎不可能从结果反推到原因。

    做三件事：剥掉开头的 UTF-8 BOM（用户若显式指定 `encoding="utf-8"` 去读带 BOM 的文件，
    `open` 不会替我们剥，第一个列名会多出一个 U+FEFF）、去首尾空白、空列名补「未命名列N」。
    """
    names: list[str] = []
    for index, cell in enumerate(cells):
        text = "" if cell is None else str(cell)
        text = text.lstrip(_BOM).strip()
        names.append(text or f"未命名列{index + 1}")
    return names


def estimate_rows_from_size(
    path: str | Path, head: bytes, delimiter: str | None, *, skip_rows: int = 0
) -> int | None:
    """按「文件大小 / 头部样本平均行长」外推**数据行**行数。

    **这是估算，不是计数** —— 调用方（进度条、预估）必须按估算对待它，`precount` 开关换
    精确值。但表头不是估算：`skip_rows` 条非数据行是**已知**的偏移，减掉它以后「预估 301
    行、实际 300 行」这种差 1 就不会出现 —— 用户是在预估上确认后才提交的，差 1 也伤可信度。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if not head or size <= 0:
        return None
    text = head.decode("utf-8", errors="replace")
    newlines = text.count("\n")
    if newlines < 2:
        return None
    # 用换行符的**字节**数换算，而不是字符数：多字节字符会让字符计数低估行长。
    bytes_per_row = len(head) / newlines
    if bytes_per_row <= 0:
        return None
    # round 而不是 int：这是点估计，截断会把误差系统性压成负数，也会让「刚好整除」的
    # 文件因为浮点尾数掉一行（100.99999999999999 → 100）
    return max(1, round(size / bytes_per_row) - max(0, int(skip_rows)))


def _csv_encoding_and_delimiter(
    path: str | Path, options: InputOptions
) -> tuple[str, str, bytes]:
    """CSV 的编码与分隔符：用户指定优先，否则探测。`inspect` / `open_reader` / `precount`
    三处必须得到同一个答案，所以只在这里算一次。"""
    head = read_head(path)
    encoding = options.encoding or detect_encoding(head)[0]
    delimiter = options.delimiter
    if not delimiter:
        delimiter = sniff_delimiter(head.decode(encoding, errors="replace"))[0] or ","
    return encoding, delimiter, head


def inspect_file(
    path: str | Path, options: InputOptions | None = None, sample_rows: int = 20
) -> Detection:
    """探测一个文件：编码、分隔符、表头、样本行。`/inspect` 与配置向导都靠它。"""
    options = options or InputOptions()
    path = Path(path)
    if not path.is_file():
        raise CleanIoError(f"文件不存在：{path}")
    kind = file_kind(path)
    detection = Detection(path=str(path), kind=kind)

    if kind == "csv":
        head = read_head(path)
        encoding, confidence, warnings = detect_encoding(head)
        detection.encoding = options.encoding or encoding
        detection.encoding_confidence = 1.0 if options.encoding else confidence
        detection.warnings.extend(warnings)
        text = head.decode(detection.encoding, errors="replace")
        if options.delimiter:
            detection.delimiter = options.delimiter
            detection.delimiter_confidence = 1.0
        else:
            detection.delimiter, detection.delimiter_confidence = sniff_delimiter(text)
            if detection.delimiter is None:
                # 单列文件是合法输入，只是没有分隔符可言。
                detection.delimiter = ","
                detection.delimiter_confidence = 0.0
                detection.warnings.append("未探测到分隔符，按单列文件处理（默认逗号）")
            elif detection.delimiter_confidence < 0.95:
                detection.warnings.append(
                    f"分隔符置信度 {detection.delimiter_confidence:.2f}，请在界面上确认"
                )
        rows = [
            row
            for row in _iter_csv(
                text, detection.delimiter, limit=sample_rows + options.header_row + 1
            )
            if row
        ]
        if not rows:
            raise CleanIoError("文件为空或没有可解析的行")
        if options.header_row >= len(rows):
            raise CleanIoError(f"表头行号 {options.header_row} 超出文件范围")
        detection.headers = normalize_headers(rows[options.header_row])
        detection.sample_rows = rows[options.header_row + 1 :][:sample_rows]
        detection.ragged_rows = count_ragged(detection.sample_rows, len(detection.headers))
        if detection.ragged_rows:
            detection.warnings.append(
                f"样本 {len(detection.sample_rows)} 行里有 {detection.ragged_rows} 行字段数与表头"
                "不一致，将按表头宽度补齐或截断（精确计数需开启 precount）"
            )
        detection.row_estimate = estimate_rows_from_size(
            path, head, detection.delimiter, skip_rows=options.header_row + 1
        )

    elif kind == "xlsx":
        reader = _xlsx_open(path)
        detection.sheet_names = list(reader.sheet_names)
        sheet = _pick_sheet(reader, options.sheet, detection.sheet_names)
        probe = _xlsx_sheet(reader, sheet, options.header_row, 0, sample_rows)
        detection.headers = [str(name) for name in probe.columns]
        # header_row 已经在 load_sheet 里被吃掉了，所以数据行从 0 开始。
        detection.sample_rows = _frame_rows(probe)[:sample_rows]
        detection.ragged_rows = count_ragged(detection.sample_rows, len(detection.headers))
        # **total_height 才是整张表的行数**；height 是「本次 load 出来多少行」（实测
        # n_rows=1 时 height=1、total_height=49）。用错会让进度条永远停在 1%。
        detection.row_estimate = _xlsx_total_height(reader, sheet, options.header_row)
        detection.sheet = sheet
        if detection.sheet_names and len(detection.sheet_names) > 1:
            detection.warnings.append(
                f"工作簿有 {len(detection.sheet_names)} 张表，当前读取：{sheet}"
            )

    elif kind == "jsonl":
        head = read_head(path)
        encoding, confidence, warnings = detect_encoding(head)
        detection.encoding = options.encoding or encoding
        detection.encoding_confidence = confidence
        detection.warnings.extend(warnings)
        rows: list[dict[str, Any]] = []
        with open(path, "r", encoding=detection.encoding, errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = orjson.loads(line)
                except orjson.JSONDecodeError:
                    detection.ragged_rows += 1
                    continue
                if not isinstance(obj, dict):
                    detection.ragged_rows += 1
                    continue
                rows.append(obj)
                if len(rows) >= sample_rows + options.header_row:
                    break
        if not rows:
            raise CleanIoError("没有可解析的 JSON 对象行")
        headers: list[str] = []
        for row in rows:
            for key in row:
                if key not in headers:
                    headers.append(key)
        detection.headers = headers
        # JSONL 没有表头行，所以这里的 header_row 语义是「跳过开头 N 条记录」。
        # 沿用 CSV 的「第 N 行是表头、数据从 N+1 开始」会让 header_row=0 白丢第一条。
        data = rows[options.header_row :]
        detection.sample_rows = [
            [row.get(key) for key in headers] for row in data[:sample_rows]
        ]
        detection.row_estimate = estimate_rows_from_size(
            path, head, None, skip_rows=options.header_row
        )

    elif kind == "parquet":
        frame = pl.read_parquet(path, n_rows=sample_rows)
        detection.headers = [str(name) for name in frame.columns]
        detection.sample_rows = _frame_rows(frame)[:sample_rows]
        detection.row_estimate = int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())

    else:  # pragma: no cover - file_kind 已经拦掉
        raise CleanIoError(f"不支持的文件类型：{path.suffix}")

    if options.sanitize_formula:
        hits = sum(
            1
            for row in detection.sample_rows
            for cell in row
            if isinstance(cell, str) and looks_like_formula(cell)
        )
        if hits:
            detection.warnings.append(
                f"样本里有 {hits} 个疑似公式的文本（以 = + - @ 开头），输出为 CSV 时会被前缀改写；"
                "输出为 xlsx 时按文本写入（不改值）"
            )
    return detection


def _xlsx_open(path: str | Path):
    import fastexcel

    try:
        return fastexcel.read_excel(str(path))
    except Exception as exc:  # fastexcel 的异常类型很细，这里统一收口
        raise CleanIoError(f"无法读取 xlsx：{exc}") from exc


def _pick_sheet(reader: Any, wanted: str | int | None, names: Sequence[str]) -> int | str:
    if wanted is None:
        return 0
    if isinstance(wanted, int):
        if wanted >= len(names):
            raise CleanIoError(f"工作表序号 {wanted} 超出范围（共 {len(names)} 张）")
        return wanted
    if wanted not in names:
        raise CleanIoError(f"找不到工作表：{wanted}")
    return wanted


def _xlsx_sheet(
    reader: Any, sheet: int | str, header_row: int, skip: int, n_rows: int | None
) -> pl.DataFrame:
    try:
        loaded = reader.load_sheet(
            sheet, header_row=header_row, skip_rows=skip or None, n_rows=n_rows
        )
        return loaded.to_polars()
    except Exception as exc:
        raise CleanIoError(f"无法读取 xlsx（第 {skip} 行起）：{exc}") from exc


def _xlsx_total_height(reader: Any, sheet: int | str, header_row: int) -> int:
    """整张表的数据行数。要拿 total_height，不能拿 height —— 见调用处的注释。"""
    try:
        return int(reader.load_sheet(sheet, header_row=header_row, n_rows=1).total_height)
    except Exception as exc:
        raise CleanIoError(f"无法读取 xlsx 行数：{exc}") from exc


# =========================================================================== 单元格文本


def looks_like_formula(text: str) -> bool:
    return bool(text) and bool(_FORMULA_RE.match(text))


def cell_text(value: Any) -> str:
    """写 CSV / JSONL / spool 时的单元格文本。实现搬到 clean_schema，这里只做转发。

    为什么要搬：算子层（clean_ops）取单元格文本时必须与写出用同一份实现，否则
    `concat`/`dedupe`/`number_format` 看到的文本与落盘文本可能不同，而报告是对着算子
    的结果算的 —— 那会让报告与产出文件对不上。留在原处的名字继续可用，测试与既有调用不动。
    """
    return _cell_text(value)


# =========================================================================== 读


class BaseReader:
    """批读器。引擎按批拉取，每批是 `list[list[Any]]`（行主序，与表头等长）。

    行主序是刻意的：清洗算子大多是逐行的，而列式求值要的那一列用一次列表推导就能取出来。
    反过来（列主序）每次逐行算子都要重新组装行，代价更高。
    """

    kind = "base"

    def __init__(self, path: Path, columns: list[str], row_estimate: int | None = None):
        self.path = path
        self.columns = columns
        self.row_estimate = row_estimate
        self._started = False
        self._skipped = 0
        self._noted: set[str] = set()
        self.notes: list[str] = []

    def _note_once(self, key: str, text: str) -> None:
        """同一个 key 只记一次。对着一个 GB 文件按批重复「这里有坏行」会把日志淹掉，
        而这类信息说一遍就够（精确条数走计数器进报告）。"""
        if key not in self._noted:
            self._noted.add(key)
            self.notes.append(text)

    @property
    def skipped(self) -> int:
        return self._skipped

    def skip(self, rows: int) -> None:
        """断点续跑用：跳到第 rows 行（数据行，不含表头）。**必须在读第一批之前调用。**"""
        if self._started:
            raise CleanIoError("已经开读之后再 skip 不可靠，必须在第一批之前调用")
        if rows < 0:
            raise CleanIoError("skip 行数不能为负")
        self._skipped = rows

    def read_batch(self) -> list[list[Any]] | None:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self) -> "BaseReader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class CsvStream:
    """CSV 文件 → 记录（`list[str]`）的迭代器。**所有按记录读 CSV 的地方都走它。**

    存在的理由是「探测与读取必须一致」：`/inspect` 看到的表头、样本行、参差行计数，必须与
    引擎真正读出来的逐条相同。把「跳表头 / 续跑跳行 / 数坏行 / 数替换字符 / 补参差」收在
    一处，就不会出现两处实现各差一行 —— 而差一行会让断点续跑永久错位，是最难查的一类 bug。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        delimiter: str = ",",
        encoding: str = "utf-8",
        header_row: int = 0,
        skip: int = 0,
    ):
        self.path = Path(path)
        self.delimiter = delimiter
        self.encoding = encoding
        self.header_row = max(0, int(header_row))
        self.skip = max(0, int(skip))
        self._columns: list[str] = []
        # 三个计数器都只增不减，由调用方读取后进报告。errors 是「解析不出来的记录数」，
        # replacement_chars 是「解码替换掉的字符数」—— 后者非零意味着编码猜错了，
        # 而它不会抛异常，只有计数能暴露。
        self.errors = 0
        self.replacement_chars = 0
        self.ragged_rows = 0
        self._handle = None
        self._reader: Iterator[list[str]] | None = None
        self._prepared = False
        self._skipping_done = False
        self._consecutive_errors = 0

    # -- 打开与预处理 ------------------------------------------------------
    @property
    def columns(self) -> list[str]:
        """列名。**读它就自动完成打开与表头解析** —— 属性访问不该有「先去调用 open()
        不然拿到空列表」这种前置条件，那正是会静默产出空表头的坑。"""
        if not self._prepared:
            self.open()
        return self._columns

    def open(self) -> "CsvStream":
        """打开文件并吃掉表头，之后 `columns` 可用。**不读数据行**（续跑的跳行要等
        调用方把 skip 定下来，见 `set_skip`）。

        显式调用它的意义是**提前失败**：表头行号越界、文件不存在这类问题在构造阶段就报，
        而不是等引擎跑起来读到一半才发现。
        """
        if self._prepared:
            return self
        # newline="" 是 csv 模块的硬要求：否则引号里的 \r\n 会被文本层先翻译一遍，
        # 含换行的单元格就被切成了两条记录。
        self._handle = open(self.path, "r", encoding=self.encoding, errors="replace", newline="")
        self._reader = iter(
            csv.reader(self._handle, delimiter=self.delimiter, quotechar='"')
        )
        for _ in range(self.header_row):
            if self._next_record() is None:
                break
        header = self._next_record()
        if header is None:
            # **构造失败也要关掉刚打开的文件**：`open()` 是「提前失败」的入口，失败是常态
            # （空文件、表头行越界），而调用方在异常路径上不会去 close 一个没建成的对象。
            # 留下的是一个个泄漏的 fd —— 一个反复预览空产物的服务会慢慢把 fd 用完。
            self.close()
            raise CleanIoError(f"{self.path.name}：表头行 {self.header_row} 超出文件范围")
        self._columns = normalize_headers(header)
        self._prepared = True
        return self

    def set_skip(self, rows: int) -> None:
        """续跑用：从第 rows 条**数据记录**开始读。必须在取第一条记录之前调用 ——
        读到一半再改是错的（前面已经产出的行无法撤回）。"""
        if self._skipping_done:
            raise CleanIoError("已经开读之后再改 skip 不可靠")
        self.skip = max(0, int(rows))

    def _next_record(self) -> list[str] | None:
        """取一条记录。返回 None **只表示文件到尾**，解析失败不算（计入 errors 后继续）。"""
        while True:
            try:
                record = next(self._reader)
            except StopIteration:
                return None
            except csv.Error:
                self.errors += 1
                self._consecutive_errors += 1
                if self._consecutive_errors >= MAX_CONSECUTIVE_CSV_ERRORS:
                    raise CleanIoError(
                        f"{self.path.name}：连续 {self._consecutive_errors} 条记录解析失败，"
                        "已放弃读取（文件可能不是 CSV，或分隔符/引号配置有误）"
                    )
                continue
            self._consecutive_errors = 0
            for cell in record:
                if _REPLACEMENT in cell:
                    self.replacement_chars += cell.count(_REPLACEMENT)
            return record

    def __iter__(self) -> Iterator[list[str]]:
        if not self._prepared:
            self.open()
        if not self._skipping_done:
            for _ in range(self.skip):
                if self._next_record() is None:
                    break
            self._skipping_done = True
        width = len(self.columns)
        while True:
            record = self._next_record()
            if record is None:
                return
            if len(record) != width:
                self.ragged_rows += 1
                if len(record) < width:
                    record = record + [""] * (width - len(record))
                else:
                    record = record[:width]
            yield record

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self._reader = None

    def __enter__(self) -> "CsvStream":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()


class CsvReader(BaseReader):
    """CSV 读。走标准库 `csv`，理由与实测数据见模块 docstring 第 1 条。

    `skip` 是**按记录重放**而不是按字节定位：引号里可以含换行，所以「第 N 行」不等于
    「第 N 个换行」，只有真的解析才能数准。代价是续跑要重放已读过的部分（GB 文件约十几秒，
    只发生在恢复时），换来的是「一行引号写错就永久错位」这类问题不存在。
    """

    kind = "csv"

    def __init__(
        self, path: Path, delimiter: str, encoding: str, options: InputOptions, batch_rows: int
    ):
        self._options = options
        self._batch_rows = max(1, int(batch_rows))
        self._records: Iterator[list[str]] | None = None
        self._stream = CsvStream(
            path, delimiter=delimiter, encoding=encoding, header_row=options.header_row
        )
        # 构造时就要知道列名（引擎要先建表头），但此时还没到能定 skip 的时候。
        self._stream.open()
        super().__init__(
            path, self._stream.columns,
            # 进度条的分母与 `/estimate` 必须来自同一个式子，否则「预估 300 行、跑到 301」
            # 这种差 1 会让 ETA 在最后一刻跳一下
            estimate_rows_from_size(path, read_head(path), delimiter,
                                    skip_rows=options.header_row + 1),
        )

    # 三个计数器直通 CsvStream：报告要的是整个文件的数，不是这一批的。
    @property
    def ragged_rows(self) -> int:
        return self._stream.ragged_rows

    @property
    def parse_errors(self) -> int:
        return self._stream.errors

    @property
    def replacement_chars(self) -> int:
        return self._stream.replacement_chars

    def skip(self, rows: int) -> None:
        super().skip(rows)
        self._stream.set_skip(rows)

    def read_batch(self) -> list[list[Any]] | None:
        if self._records is None:
            self._started = True
            self._records = iter(self._stream)
        rows: list[list[Any]] = []
        for record in self._records:
            rows.append(record)
            if len(rows) >= self._batch_rows:
                break
        self._note_counters()
        if not rows:
            return None
        self._skipped += len(rows)
        return rows

    def _note_counters(self) -> None:
        """计数器非零时各报一次（只报一次，不按批刷屏）。

        计数本身是精确的、随时可读（见属性），这里给的是一条「实时可见」的提示；措辞刻意
        不带数字 —— 数字每批都在涨，写进去反而误导。精确值由报告读属性拿到。
        """
        if self._stream.ragged_rows:
            self._note_once(
                "ragged", "存在字段数与表头不一致的行，已按表头宽度补齐或截断（见 ragged_rows）"
            )
        if self._stream.errors:
            self._note_once("parse_error", "存在解析失败的记录，已跳过（见 parse_errors）")
        if self._stream.replacement_chars:
            self._note_once(
                "replacement",
                f"按 {self._stream.encoding} 解码时遇到无法解码的字节，已替换为 U+FFFD 并原样"
                "写入输出，请在界面上确认编码",
            )

    def close(self) -> None:
        self._stream.close()
        self._records = None


class XlsxReader(BaseReader):
    """xlsx 读。小表一次物化后切片；大表按行区间分块重读（用时间换内存，见模块 docstring）。"""

    kind = "xlsx"

    def __init__(self, path: Path, options: InputOptions, batch_rows: int):
        self._options = options
        self._batch_rows = max(1, int(batch_rows))
        self._reader = _xlsx_open(path)
        self._sheet = _pick_sheet(self._reader, options.sheet, list(self._reader.sheet_names))
        probe = _xlsx_sheet(self._reader, self._sheet, options.header_row, 0, 1)
        columns = [str(name) for name in probe.columns]
        height = _xlsx_total_height(self._reader, self._sheet, options.header_row)
        self._height = height
        self._frame: pl.DataFrame | None = None
        self._cursor = 0
        if height == 0:
            self._chunk_rows = 0
        else:
            self._chunk_rows = max(1, int(batch_rows))
        super().__init__(path, columns, height)
        width = max(1, len(columns))
        self._chunked = height * width > XLSX_MATERIALIZE_CELLS
        if self._chunked:
            # 分块的块大小要按「单元格预算」反算，否则宽表一样会撑爆内存。
            self._chunk_rows = max(1, min(self._chunk_rows, XLSX_MATERIALIZE_CELLS // width))
            self.notes.append(
                f"表较大（{height} 行 × {width} 列），按行分块读取以避免整表载入内存；"
                "xlsx 分块是重复扫描，会比 CSV 慢，建议大表改用 CSV"
            )
        else:
            self._frame = _xlsx_sheet(self._reader, self._sheet, options.header_row, 0, None)

    @property
    def chunked(self) -> bool:
        return self._chunked

    def read_batch(self) -> list[list[Any]] | None:
        self._started = True
        if not self._chunked:
            assert self._frame is not None
            rows = _frame_rows(self._frame) if self._skipped == 0 else None
            if rows is None:
                # 续跑：一次性物化的表用 skip 之后就只取走剩余部分，避免重切全表。
                all_rows = _frame_rows(self._frame)
                rows = all_rows[self._skipped :]
            if not rows:
                return None
            batch = rows[: self._batch_rows]
            self._skipped += len(batch)
            return batch
        if self._cursor >= self._height:
            return None
        take = min(self._chunk_rows, self._height - self._cursor)
        frame = _xlsx_sheet(self._reader, self._sheet, self._options.header_row, self._cursor, take)
        rows = _frame_rows(frame)
        if not rows:
            return None
        self._cursor += len(rows)
        self._skipped = self._cursor
        return rows

    def close(self) -> None:
        self._frame = None
        self._reader = None


class JsonlReader(BaseReader):
    """JSONL 读。表头取头部样本里出现过的键的并集，**之后固定**。

    之后的行走同一套列：新出现的键被计进 `unknown_keys` 而不是悄悄扩张表头 ——
    一个边读边长列的表头会让前面积累的统计与后面对不上。
    """

    kind = "jsonl"

    def __init__(self, path: Path, encoding: str, options: InputOptions, batch_rows: int):
        self._encoding = encoding
        self._batch_rows = max(1, int(batch_rows))
        self.unknown_keys = 0
        # bad_lines 是**唯一**的坏行计数，只由 read_batch 累加。探测阶段的坏行只是提前
        # 告知（同样的行 read_batch 还会再读一遍），所以单独记，绝不能加进 bad_lines ——
        # 那会让报告里的「解析错误」把头部样本里的行数算两遍。
        self.bad_lines = 0
        self._handle = None
        self._skip_target = 0
        columns, probe_bad = self._probe(path)
        super().__init__(path, columns,
                         estimate_rows_from_size(path, read_head(path), None,
                                                 skip_rows=options.header_row))
        if probe_bad:
            self.notes.append(f"头部样本里有 {probe_bad} 行不是合法 JSON 对象，将跳过")

    def _probe(self, path: Path) -> tuple[list[str], int]:
        columns: list[str] = []
        bad = 0
        with open(path, "r", encoding=self._encoding, errors="replace") as handle:
            for index, line in enumerate(handle):
                if index > 200:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = orjson.loads(line)
                except orjson.JSONDecodeError:
                    bad += 1
                    continue
                if isinstance(obj, dict):
                    for key in obj:
                        if key not in columns:
                            columns.append(key)
        return columns, bad

    def skip(self, rows: int) -> None:
        """"行" 指**成功解析出的记录**，与 read_batch 的计数口径一致 —— 口径不一致
        会让续跑之后错位若干行。"""
        super().skip(rows)
        self._skipped = 0
        self._skip_target = rows

    def read_batch(self) -> list[list[Any]] | None:
        self._started = True
        if self._handle is None:
            self._handle = open(self.path, "r", encoding=self._encoding, errors="replace")
            target = self._skip_target
            consumed = 0
            while consumed < target:
                line = self._handle.readline()
                if not line:
                    return None
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    consumed += 1
            self._skipped = consumed
        rows: list[list[Any]] = []
        while len(rows) < self._batch_rows:
            line = self._handle.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                obj = orjson.loads(line)
            except orjson.JSONDecodeError:
                self.bad_lines += 1
                continue
            if not isinstance(obj, dict):
                self.bad_lines += 1
                continue
            self.unknown_keys += sum(1 for key in obj if key not in self.columns)
            rows.append([obj.get(key) for key in self.columns])
        if not rows:
            return None
        self._skipped += len(rows)
        return rows

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


class ParquetReader(BaseReader):
    """Parquet 读。**这里保留 polars**：parquet 是列式二进制格式，标准库读不了。

    刻意**不用** `collect_batches()`，改成显式 `slice(offset, n).collect()`：实测每批成本
    随偏移只温和增长（0.053s @0% → 0.114s @92%），内存有界；而 `collect_batches()` 的预读
    没有上界（见模块 docstring 第 1 条）。顺带一个好处是续跑的跳行就是 `offset` 本身，
    不需要「先 skip 再 collect」那套状态。
    """

    kind = "parquet"

    def __init__(self, path: Path, batch_rows: int):
        self._batch_rows = max(1, int(batch_rows))
        self._lazy: pl.LazyFrame | None = None
        self._offset = 0
        frame = pl.scan_parquet(str(path)).head(1).collect()
        columns = [str(name) for name in frame.columns]
        total = int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())
        super().__init__(path, columns, total)

    def read_batch(self) -> list[list[Any]] | None:
        if self._lazy is None:
            self._started = True
            self._lazy = pl.scan_parquet(str(self.path))
            self._offset = self._skipped
        rows = _frame_rows(self._lazy.slice(self._offset, self._batch_rows).collect())
        if not rows:
            return None
        self._offset += len(rows)
        self._skipped = self._offset
        return rows

    def close(self) -> None:
        self._lazy = None


def _frame_rows(frame: pl.DataFrame) -> list[list[Any]]:
    """polars DataFrame → 行主序的 Python 列表。

    `.rows()` 是最快的整体物化方式（比 `to_dicts()` 快得多，后者每行建一个 dict）。
    归一化只做一件事：把 numpy 标量换成 Python 标量，否则下游的 `isinstance(x, int)`
    会因为 `np.int64` 不是 `int` 而判错。
    """
    if frame.height == 0:
        return []
    out: list[list[Any]] = []
    for row in frame.rows():
        out.append(
            [
                None
                if value is None
                else (value.item() if isinstance(value, np.generic) else value)
                for value in row
            ]
        )
    return out


def open_reader(
    path: str | Path, options: InputOptions, batch_rows: int = 50_000
) -> BaseReader:
    """按文件类型开一个批读器。非 UTF-8 的 CSV 就地按该编码解码，不需要转存。"""
    path = Path(path)
    kind = file_kind(path)
    if kind == "csv":
        encoding, delimiter, _ = _csv_encoding_and_delimiter(path, options)
        return CsvReader(path, delimiter, encoding, options, batch_rows)
    if kind == "xlsx":
        return XlsxReader(path, options, batch_rows)
    if kind == "jsonl":
        head = read_head(path)
        encoding = options.encoding or detect_encoding(head)[0]
        return JsonlReader(path, encoding, options, batch_rows)
    if kind == "parquet":
        return ParquetReader(path, batch_rows)
    raise CleanIoError(f"不支持的文件类型：{path.suffix}")


def precount(path: str | Path, options: InputOptions) -> int:
    """精确行数。多完整解析一遍整个文件 —— 只在用户显式要求精确进度时调用。

    CSV 用 `CsvStream` **按记录**数，不是数换行符：引号里可以含换行，数换行会数多。而
    进度条的分母一旦偏大，进度就永远走不到 100%，比没有精确进度更糟。
    """
    path = Path(path)
    kind = file_kind(path)
    if kind == "xlsx":
        reader = _xlsx_open(path)
        sheet = _pick_sheet(reader, options.sheet, list(reader.sheet_names))
        return _xlsx_total_height(reader, sheet, options.header_row)
    if kind == "parquet":
        return int(pl.scan_parquet(str(path)).select(pl.len()).collect().item())
    if kind == "jsonl":
        head = read_head(path)
        encoding = options.encoding or detect_encoding(head)[0]
        total = 0
        skipped = 0
        with open(path, "r", encoding=encoding, errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                # header_row 在 JSONL 上的语义是「跳过开头 N 条记录」（见 inspect_file），
                # 精确计数也必须照着跳，否则它和预估、和进度条分母三处各不相同
                if skipped < options.header_row:
                    skipped += 1
                    continue
                total += 1
        return total
    encoding, delimiter, _ = _csv_encoding_and_delimiter(path, options)
    stream = CsvStream(
        path, delimiter=delimiter, encoding=encoding, header_row=options.header_row
    )
    try:
        # 表头在 open() 里已经被吃掉，所以剩下的记录条数就是数据行数。
        return sum(1 for _ in stream)
    finally:
        stream.close()


# =========================================================================== 写


class BaseWriter:
    """批写器。`flush()` 的返回值就是提交点要记录的那个字节数 —— 恢复时靠它截断。

    写入顺序协议（调用方必须遵守）：`write` → `flush` → 所有 append-only 文件都 flush 完
    → **最后**写 state.json。这个顺序本身就是一致性协议，state.json 才是提交记录。
    """

    kind = "base"

    def __init__(self, path: Path, columns: list[str], counters: dict[str, int] | None = None):
        self.path = path
        self.columns = columns
        self.counters = counters if counters is not None else {}

    def bump(self, key: str, amount: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + amount

    def write(self, rows: Sequence[Sequence[Any]]) -> None:
        raise NotImplementedError

    def flush(self) -> int:
        raise NotImplementedError

    def close(self) -> Path:
        raise NotImplementedError

    def abort(self) -> None:
        raise NotImplementedError

    def suspend(self) -> None:
        """**暂停/取消时收手**：保留已提交的字节，不做任何收尾转换，也不删文件。

        三个方法的分工必须清楚，否则取消一次就会毁掉续跑：

        - `close()`：正常完成。spool 转成目标格式、原子替换 —— 半途调用会产出一个
          「看起来完整」的半截文件。
        - `abort()`：这个文件的产物**全部作废**。半途调用会把已提交的 spool 也删掉，
          于是「取消后能否续跑」变成「看你在哪一步取消」。
        - `suspend()`：就是本条。关掉句柄让已写入的字节留在磁盘上，状态里记的
          `out_bytes` 仍然有效，恢复时截断到那里再接着写。
        """
        raise NotImplementedError


def _split_path(path: Path, suffix: str) -> Path:
    """中间文件：`foo.xlsx` → `foo.xlsx.part`。刻意留在同目录 —— `os.replace` 只在
    同一文件系统内是原子的，而「同目录」是保证这一点最简单的方式。"""
    return path.with_name(path.name + suffix)


def append_target(path: str | Path, options: OutputOptions) -> Path:
    """`open_writer` 真正**追加写**的那个文件。

    断点续跑要按它记录与校验字节数，所以这个命名规则只能有一处实现：xlsx/parquet 写的是
    `.part.csv` spool（`SpoolWriter`），其余格式就是最终文件本身。两边各写一遍命名规则，
    就是「记录了 A 的字节数、去截断 B」这类续跑错位的开始。
    """
    p = Path(path)
    return _split_path(p, ".part.csv") if options.format in ("xlsx", "parquet") else p


def _open_append(path: Path, append_bytes: int | None) -> tuple[Any, bool]:
    """打开续写目标，返回 (句柄, 是否从头开始写)。

    `append_bytes` 是上个提交点记录的字节数。**截断必须在这里做**，不能推给调用方：
    崩溃时文件尾部往往有若干条已写出但未提交的行，如果只以 `"ab"` 追加，那些行会留在
    文件里、新行接在它们后面 —— 产出「看起来正常但中间重复了几行」的结果，比直接报错
    难查得多。调用方即使忘了先 `os.truncate`，这里也不会错。

    两种对不上的情况都**拒绝续写**而不是悄悄从头开始（悄悄重跑会让用户以为产物是续着写的）：
    提交点字节数大于文件实际大小、或文件根本不存在。
    """
    fresh = append_bytes is None or int(append_bytes) <= 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if fresh:
        return open(path, "wb", buffering=1024 * 256), True
    if not path.exists():
        raise CleanIoError(f"{path.name} 不存在，但提交点记录了 {append_bytes} 字节，无法续写")
    size = os.path.getsize(path)
    if size < int(append_bytes):
        raise CleanIoError(
            f"{path.name} 只有 {size} 字节，提交点却记录了 {append_bytes} 字节，"
            "产物与状态不一致，拒绝续写（请重跑该文件）"
        )
    handle = open(path, "r+b", buffering=1024 * 256)
    handle.truncate(int(append_bytes))
    handle.seek(0, os.SEEK_END)
    return handle, False


class _CsvSink:
    """CSV 行的实际写出者。CsvWriter 与 SpoolWriter 共用，避免两处引号规则漂移。"""

    def __init__(self, handle, delimiter: str = ","):
        self.handle = handle
        self.stream = io.TextIOWrapper(handle, encoding="utf-8", newline="", write_through=True)
        self.writer = csv.writer(
            self.stream,
            delimiter=delimiter,
            quotechar='"',
            quoting=csv.QUOTE_MINIMAL,
            lineterminator="\n",
        )

    def row(self, values: Sequence[str]) -> None:
        self.writer.writerow(values)

    def flush(self) -> int:
        self.stream.flush()
        self.handle.flush()
        return os.fstat(self.handle.fileno()).st_size

    def close(self) -> None:
        try:
            self.stream.flush()
        finally:
            self.stream.close()


class CsvWriter(BaseWriter):
    """CSV 直写（可追加，因此天然可续跑）。"""

    kind = "csv"

    def __init__(
        self,
        path: Path,
        columns: list[str],
        delimiter: str = ",",
        sanitize_formula: bool = True,
        append_bytes: int | None = None,
        counters: dict[str, int] | None = None,
    ):
        super().__init__(path, columns, counters)
        self.delimiter = delimiter
        self.sanitize_formula = sanitize_formula
        self._handle, fresh = _open_append(path, append_bytes)
        self._sink = _CsvSink(self._handle, delimiter)
        if fresh:
            self._sink.row(self.columns)

    def _cell(self, value: Any) -> str:
        text = cell_text(value)
        if self.sanitize_formula and looks_like_formula(text):
            # CSV 没有单元格类型，只能在文本前加一个引号挡住 Excel 的公式解析。
            # 这是**数据改动**，所以必须计数并进报告。
            self.bump("formula_sanitized")
            return "'" + text
        return text

    def write(self, rows: Sequence[Sequence[Any]]) -> None:
        for row in rows:
            self._sink.row([self._cell(value) for value in row])

    def flush(self) -> int:
        return self._sink.flush()

    def close(self) -> Path:
        try:
            self._sink.flush()
        finally:
            self._sink.close()
        return self.path

    def suspend(self) -> None:
        try:
            self._sink.close()
        except Exception:
            LOGGER.debug("挂起 CSV 写入失败", exc_info=True)

    def abort(self) -> None:
        try:
            self._sink.close()
        except Exception:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


class JsonlWriter(BaseWriter):
    """JSONL 直写。`orjson` 在这里是热路径（每行都要过一遍）。"""

    kind = "jsonl"

    def __init__(
        self,
        path: Path,
        columns: list[str],
        append_bytes: int | None = None,
        counters: dict[str, int] | None = None,
    ):
        super().__init__(path, columns, counters)
        self._handle, _ = _open_append(path, append_bytes)

    def write(self, rows: Sequence[Sequence[Any]]) -> None:
        for row in rows:
            record = {key: value for key, value in zip(self.columns, row)}
            self._handle.write(orjson.dumps(record, option=orjson.OPT_APPEND_NEWLINE))

    def flush(self) -> int:
        self._handle.flush()
        return os.fstat(self._handle.fileno()).st_size

    def close(self) -> Path:
        try:
            self.flush()
        finally:
            self._handle.close()
        return self.path

    def suspend(self) -> None:
        try:
            self._handle.close()
        except Exception:
            LOGGER.debug("挂起 JSONL 写入失败", exc_info=True)

    def abort(self) -> None:
        try:
            self._handle.close()
        except Exception:
            pass
        try:
            self.path.unlink()
        except OSError:
            pass


class SpoolWriter(BaseWriter):
    """xlsx / parquet 的基类：先写 CSV spool，`close()` 时一次性转成目标格式。

    为什么不直接写目标格式：xlsxwriter（以及 openpyxl 只写模式）和 parquet 都**无法追加**
    已写出的流，而断点续跑要求「截断到上次提交点再接着写」。spool 是可追加的 CSV，于是
    四种输出格式在续跑上只有一条代码路径。

    spool 里**不做公式消毒**：xlsx 侧用 `write_string` 写文本本身就是安全的，不需要改数据；
    真按 CSV 交付才需要前缀（见 `CsvWriter._cell`）。
    """

    def __init__(
        self,
        path: Path,
        columns: list[str],
        append_bytes: int | None,
        counters: dict[str, int] | None,
    ):
        super().__init__(path, columns, counters)
        # spool 的路径必须与 append_target() 一致：续跑记录的字节数就是按 append_target
        # 算出来的，两处命名一旦漂移就会「记录 A 的字节数、去截断 B」。测试里有一条
        # 专门的交叉断言守着这件事。
        self.spool_path = _split_path(path, ".part.csv")
        self.target_path = _split_path(path, ".part")
        self._handle, fresh = _open_append(self.spool_path, append_bytes)
        self._sink = _CsvSink(self._handle)
        if fresh:
            self._sink.row(columns)

    def write(self, rows: Sequence[Sequence[Any]]) -> None:
        for row in rows:
            self._sink.row([cell_text(value) for value in row])

    def flush(self) -> int:
        return self._sink.flush()

    def close(self) -> Path:
        try:
            self._sink.flush()
        finally:
            self._sink.close()
        self._convert()
        os.replace(self.target_path, self.path)
        try:
            self.spool_path.unlink()
        except OSError:
            pass
        return self.path

    def _convert(self) -> None:
        raise NotImplementedError

    def suspend(self) -> None:
        """只关 spool 句柄，**不转换**。

        `close()` 在这里会把只写了一半的 spool 转成一个看起来完整的 xlsx；`abort()` 会把
        spool 删掉，续跑就没有可接的东西了。
        """
        try:
            self._sink.close()
        except Exception:
            LOGGER.debug("挂起 spool 写入失败", exc_info=True)

    def abort(self) -> None:
        try:
            self._sink.close()
        except Exception:
            pass
        for candidate in (self.spool_path, self.target_path, self.path):
            try:
                candidate.unlink()
            except OSError:
                pass

    def _spool(self) -> Iterator[list[str]]:
        """流式读回 spool，逐条产出。

        **用 `CsvStream` 而不是 polars**：spool 是我们自己用 `csv.writer`（逗号、双引号、
        `\\n`、QUOTE_MINIMAL）写的，用 `csv.reader` 读回来是严格对称的往返；中间夹一层
        Rust 的 CSV 解析器只会多一层语义差异，而那份实现没有内存上界（模块 docstring
        第 1 条），spool 又可能有几十 GB。返回的单元格**不做任何转换** —— spool 里的文本
        就是 `cell_text` 写下的最终文本。
        """
        stream = CsvStream(self.spool_path, delimiter=",", encoding="utf-8")
        try:
            for record in stream:
                yield record
        finally:
            stream.close()


class XlsxWriter(SpoolWriter):
    """xlsx 输出：spool → xlsxwriter（`constant_memory`，逐行落盘，内存恒定）。"""

    kind = "xlsx"

    def _convert(self) -> None:
        import xlsxwriter

        if len(self.columns) > EXCEL_MAX_COLS:
            raise CleanIoError(
                f"列数 {len(self.columns)} 超过 Excel 上限 {EXCEL_MAX_COLS}，请改用 CSV 或 Parquet"
            )
        workbook = xlsxwriter.Workbook(str(self.target_path), {"constant_memory": True})
        try:
            sheet = workbook.add_worksheet("清洗结果")
            for col_index, name in enumerate(self.columns):
                # 表头也用 write_string：表头名若以 = 开头，write_row 会把它当公式。
                sheet.write_string(0, col_index, name)
            row_index = 1
            for row in self._spool():
                if row_index >= EXCEL_MAX_ROWS:
                    raise CleanIoError(
                        f"行数超过 Excel 上限 {EXCEL_MAX_ROWS}，请改用 CSV 或 Parquet 输出"
                    )
                for col_index, value in enumerate(row):
                    if value == "":
                        continue
                    if len(value) > EXCEL_MAX_CHARS:
                        # xlsxwriter 会静默截断并返回 -2。静默截断就是静默改数据，自己记账。
                        value = value[:EXCEL_MAX_CHARS]
                        self.bump("cell_truncated")
                    sheet.write_string(row_index, col_index, value)
                row_index += 1
        finally:
            workbook.close()


class ParquetWriter(SpoolWriter):
    """parquet 输出：spool → `sink_parquet`（流式，内存有界）。"""

    kind = "parquet"

    def _convert(self) -> None:
        # 这里保留 polars：`sink_parquet` 是真正的流式写出，内存有界，而标准库没有 parquet
        # 写入。代价是**所有列都是 Utf8** —— 这正是我们要的：读的时候一律按字符串读
        # （模块 docstring 第 2 条），中途若让 parquet 自己推断类型，`007` 就会变成 `7`。
        pl.scan_csv(
            str(self.spool_path),
            has_header=True,
            infer_schema_length=0,
            empty_string_is_null=False,
            truncate_ragged_lines=True,
            raise_if_empty=False,
        ).sink_parquet(str(self.target_path))


def open_writer(
    path: str | Path,
    columns: list[str],
    options: OutputOptions,
    append_bytes: int | None = None,
    counters: dict[str, int] | None = None,
    sanitize_formula: bool = True,
) -> BaseWriter:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if options.format == "csv":
        return CsvWriter(
            path, columns, options.csv_delimiter, sanitize_formula, append_bytes, counters
        )
    if options.format == "jsonl":
        return JsonlWriter(path, columns, append_bytes, counters)
    if options.format == "xlsx":
        return XlsxWriter(path, columns, append_bytes, counters)
    if options.format == "parquet":
        return ParquetWriter(path, columns, append_bytes, counters)
    raise CleanIoError(f"不支持的输出格式：{options.format}")


# =========================================================================== 目录


def list_input_files(roots: Sequence[str | Path], options: InputOptions) -> list[Path]:
    """展开输入路径为待处理的文件列表。目录按 `include` 通配筛选，结果**排序**后返回 ——
    排序是为了让「同一份配置 + 同一批输入」永远得到同样的文件顺序，断点续跑才对得上。"""
    patterns = [pattern.lower() for pattern in (options.include or [])]
    found: list[Path] = []
    for root in roots:
        root_path = Path(root)
        if root_path.is_file():
            found.append(root_path)
            continue
        if not root_path.is_dir():
            raise CleanIoError(f"路径不存在：{root_path}")
        walker = root_path.rglob("*") if options.recursive else root_path.glob("*")
        for candidate in walker:
            if not candidate.is_file() or candidate.name.startswith("."):
                continue
            if patterns and not any(candidate.match(pattern) for pattern in patterns):
                continue
            found.append(candidate)
    return sorted({path.resolve() for path in found}, key=lambda item: str(item))
