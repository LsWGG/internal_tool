"""清洗任务的 Python 子进程：在一个受限的解释器里跑用户写的转换函数。

# 这是健壮性边界，不是安全边界

威胁模型是「操作者在自己机器上跑自己的代码」，目标只有四条：

1. 函数不能把服务卡死（超时 + 杀进程组）；
2. 函数不能吃掉主机内存（RLIMIT_AS）；
3. 函数不能留下杀不掉的进程树（新会话 + killpg）；
4. 函数的 `print()` 不能污染协议流（协议走 dup 出来的真 stdout）。

它**挡不住**有心越狱的代码 —— 导入白名单和受限 `__builtins__` 拦得住手滑，拦不住
`().__class__.__base__.__subclasses__()`。要真隔离就得上容器（仓库已有 `docker_manager`
可作升级路径）。别把这个文件当成沙箱，也别拿它去跑不受信任的第三方代码。

# 协议

换行分隔的 JSON（orjson），父 → 子只走 stdin，子 → 父只走 dup 出来的 stdout。
每帧都带 `id`，父进程杀过子进程之后管道里可能残留上一轮的帧，**id 不匹配一律丢弃**。

    {"id":1,"op":"init","protocol":1,"seed":7,"memory_mb":1024,
     "functions":[{"name":"f1","source":"def transform(row): ...","entry":"transform",
                   "mode":"row","input_fields":["a"],"output_fields":["b"]}]}
    {"id":2,"op":"call","fn":"f1","fields":["a"],"rows":[[1],[2]]}
    {"id":3,"op":"call","fn":"f2","columns":{"a":[1,2]}}

回帧：

    {"id":2,"ok":true,"rows":[null,{"b":"x"}],"errors":[],"notes":[]}
    {"id":3,"ok":true,"columns":{"x":[1,2]}}
    {"id":2,"ok":false,"kind":"shape_error","error":"..."}

`rows` 的元素是 `null`（这一行不变）或「只含函数真正返回了的那些字段」的字典 —— 稀疏
表示让父进程可以原样把值写回列，不用猜「返回 None 是「不改」还是「改成空」。

# 值跨进程会变成 JSON

`date`/`datetime`/`Decimal` 这些对象在序列化时按 `str` 落成字符串（见 `dumps`）。这是
有意的取舍：协议要么带类型标签、要么带类型丢失，而带标签会让每一帧都背上类型元数据。
所以「用户函数返回的类型」与「回到父进程的类型」可能不同，涉及日期时请显式返回字符串。
"""

from __future__ import annotations

import builtins as _builtins
import ast
import os
import random
import sys
import traceback
from dataclasses import dataclass

PROTOCOL_VERSION = 1

WHITELIST_MODULES = frozenset(
    {
        "math", "re", "json", "orjson", "datetime", "decimal", "statistics", "random",
        "hashlib", "uuid", "itertools", "functools", "collections", "string",
        "unicodedata", "base64", "csv", "io", "typing", "dataclasses", "enum",
        "zoneinfo", "calendar", "time", "operator", "fractions", "bisect", "heapq",
        "copy", "warnings", "re2", "faker", "textwrap", "difflib", "pprint",
    }
)
"""允许导入的模块。全是纯计算/文本处理，没有一个能碰文件系统或网络。"""

BANNED_NAMES = frozenset(
    {
        "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
        "exit", "quit", "globals", "locals", "vars", "memoryview", "delattr",
    }
)
"""禁用内建。`getattr`/`setattr` 保留（写转换函数时常用），`__` 开头的属性访问由 AST 挡住。"""

_ALLOWED_BUILTINS = (
    "abs all any ascii bin bool bytes callable chr complex dict divmod enumerate filter "
    "float format frozenset getattr hasattr hash hex int isinstance issubclass iter len list "
    "map max min next object oct ord pow print range repr reversed round set setattr slice "
    "sorted str sum tuple type zip "
    "True False None NotImplemented Ellipsis "
    "ArithmeticError AssertionError AttributeError BaseException Exception IndexError "
    "KeyError LookupError MemoryError NotImplementedError OverflowError RuntimeError "
    "StopIteration TypeError UnicodeDecodeError UnicodeEncodeError ValueError ZeroDivisionError "
    "property staticmethod classmethod object super __build_class__ "
    # 必须有：`import math` 的字节码就是调 __import__，没有它连白名单里的模块都导不进来
    # （报错是「ImportError: __import__ not found」，看着像环境坏了）。真正的闸门是
    # 上面 validate_source 的 AST 检查 —— 内建白名单管不了 import。
    "__import__"
).split()


def restricted_builtins() -> dict:
    namespace = {}
    for name in _ALLOWED_BUILTINS:
        value = getattr(_builtins, name, None)
        if value is not None:
            namespace[name] = value
    return namespace


def vocabulary() -> dict[str, list[str]]:
    """编辑器补全要用的词表：**运行时真正允许的那些名字**。

    补全列表和闸门必须是同一份数据。各写一份的结局是：补全里出现 `open`，用户点一下，
    写出来的函数必然被拒 —— 界面在教用户写错的东西。双下划线的两个（`__import__`、
    `__build_class__`）是实现细节，不进补全（闸门也不允许 `__` 开头的属性访问）。
    """
    return {
        "builtins": sorted(
            name for name in _ALLOWED_BUILTINS if not name.startswith("__")
        ),
        "modules": sorted(WHITELIST_MODULES),
        "banned": sorted(BANNED_NAMES),
    }


# =========================================================================== 静态闸门


@dataclass(frozen=True)
class Problem:
    """静态检查的一条问题。

    `text` 是**不含「函数 X：」前缀**的整句话（自己带着「第 N 行：」），所以
    `validate_source` 把它拼上前缀，就是历史上一字不差的报错；`line` 是给编辑器划线用的
    结构化行号（`None` = 定位不到某一行）。

    两档严重程度的分工：`error` 是**闸门**（跑不了，拒跑），`warning` 是提醒（照跑，
    运行时自然会说清到底怎么了）。
    """

    text: str
    line: int | None = None
    severity: str = "error"


def inspect_source(source: str, entry: str = "transform") -> list[Problem]:
    """结构化静态检查：闸门（error）+ 入口提醒（warning）。给编辑器和调试接口用。"""
    try:
        tree = ast.parse(source or "")
    except SyntaxError as exc:
        return [Problem(text=f"语法错误（第 {exc.lineno} 行）：{exc.msg}", line=exc.lineno)]
    return _gate_problems(tree) + _entry_problems(tree, entry)


def validate_source(source: str, name: str = "") -> list[str]:
    """对用户函数源码做静态检查，返回问题列表（空 = 过）。

    刻意保持**保守而简单**：能一眼看懂的规则，报错信息里说清为什么。一个漏报的越狱手段
    不是这里的目标（见模块 docstring），但一条「看不懂就拒绝」的规则会天天误伤正常代码。

    只回报 `error`：闸门是拒跑判据，不该因为一条「入口没写对」的提醒就变红 —— 那条由
    子进程 init 报得更准（「源码里找不到入口 'transform'」），也在这份输出的形状之外。
    """
    label = f"函数 {name}：" if name else ""
    return [
        f"{label}{item.text}"
        for item in inspect_source(source)
        if item.severity == "error"
    ]


def _gate_problems(tree: ast.AST) -> list[Problem]:
    """闸门本体：语法以外的硬规则。行号只用 `node.lineno`。

    **不要用 `col_offset`**：它是 UTF-8 字节偏移，而编辑器（CodeMirror）按字符定位 ——
    源码里只要有中文注释，两个偏移就对不上，波浪线会划到别的字上去。所以界面按整行标。
    """
    problems: list[Problem] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in WHITELIST_MODULES:
                    problems.append(Problem(
                        line=node.lineno,
                        text=f"第 {node.lineno} 行：不允许导入 {root!r}"
                             f"（可导入：{'、'.join(sorted(WHITELIST_MODULES))}）",
                    ))
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                problems.append(Problem(
                    line=node.lineno, text=f"第 {node.lineno} 行：不允许相对导入"
                ))
                continue
            root = (node.module or "").split(".")[0]
            if root not in WHITELIST_MODULES:
                problems.append(Problem(
                    line=node.lineno, text=f"第 {node.lineno} 行：不允许导入 {root!r}"
                ))
        elif isinstance(node, ast.Name):
            if node.id in BANNED_NAMES:
                problems.append(Problem(
                    line=node.lineno, text=f"第 {node.lineno} 行：不允许使用 {node.id!r}"
                ))
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                problems.append(Problem(
                    line=node.lineno,
                    text=f"第 {node.lineno} 行：不允许访问双下划线属性 {node.attr!r}",
                ))

    # 模块级循环：写在模块体的循环会在 init 阶段跑，而 init 期间没有任何超时保护能救
    # 「死循环里的一个函数」。带 break 的循环放行（它就是有界的）。
    for node in tree.body:
        if isinstance(node, (ast.For, ast.While)):
            has_break = any(
                isinstance(inner, ast.Break) for inner in ast.walk(node)
            )
            if not has_break:
                problems.append(Problem(
                    line=node.lineno,
                    text=f"第 {node.lineno} 行：模块级的循环必须有 break —— "
                         "模块代码在初始化时执行，一个没有出口的循环会让整个任务卡在这里",
                ))
    return problems


def _entry_problems(tree: ast.AST, entry: str) -> list[Problem]:
    """入口函数的提醒。全是 `warning`：这几条运行时都会以更具体的形式报出来。

    放在这里是因为它们**恰恰是最常见的写错方式**（函数不叫 transform、参数个数不对），
    早一点在编辑器里说，比等用户点「试跑」再说好。
    """
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == entry
        ),
        None,
    )
    if node is None:
        return [Problem(
            severity="warning",
            text=f"源码里没有定义入口函数 {entry!r} —— 运行时会在初始化时报错，"
                 "函数必须叫这个名字（不要改成 clean/convert 之类）",
        )]
    if isinstance(node, ast.AsyncFunctionDef):
        return [Problem(
            line=node.lineno, severity="warning",
            text=f"第 {node.lineno} 行：入口 {entry!r} 是 async 函数，这里不会被 await，"
                 "请写成普通函数",
        )]
    args = node.args
    total = len(args.posonlyargs) + len(args.args)
    required = total - len(args.defaults)
    if args.vararg is None and (total < 1 or required > 1):
        return [Problem(
            line=node.lineno, severity="warning",
            text=f"第 {node.lineno} 行：入口 {entry!r} 需要接收且只接收 1 个参数"
                 f"（当前 {total} 个）—— 运行时是 fn({{字段: 值}}) 这样调的",
        )]
    return []


# =========================================================================== 序列化


try:
    import orjson as _orjson

    def dumps(value) -> bytes:
        # default=str：date/datetime/Decimal 落成字符串（见模块 docstring 的说明）
        return _orjson.dumps(value, default=str)

    def loads(data):
        return _orjson.loads(data)

except ImportError:  # pragma: no cover - 只在缺 orjson 的环境里走到
    import json as _json

    def dumps(value) -> bytes:
        return _json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")

    def loads(data):
        return _json.loads(data)


# =========================================================================== 函数装载


class LoadedFunction:
    __slots__ = ("name", "fn", "mode", "input_fields", "output_fields")

    def __init__(self, name, fn, mode, input_fields, output_fields):
        self.name = name
        self.fn = fn
        self.mode = mode
        self.input_fields = tuple(input_fields)
        self.output_fields = tuple(output_fields)


def load_functions(specs, seed: int | None = None) -> tuple[dict[str, LoadedFunction], list[str]]:
    """编译并装载用户函数。返回 (函数表, 问题列表)。

    每个函数都在**自己的命名空间**里 exec：两个函数各自 `import re` 或定义同名辅助函数时
    不会互相覆盖（同一命名空间下后定义的会赢，那种 bug 极难查）。
    """
    loaded: dict[str, LoadedFunction] = {}
    problems: list[str] = []
    if seed is not None:
        random.seed(seed)
    base = restricted_builtins()
    for spec in specs:
        name = spec.get("name", "")
        source = spec.get("source", "")
        entry = spec.get("entry") or "transform"
        found = validate_source(source, name)
        if found:
            problems.extend(found)
            continue
        namespace = {"__builtins__": dict(base), "SEED": seed}
        try:
            exec(compile(source, f"<clean:{name}>", "exec"), namespace)
        except Exception as exc:  # 模块级代码自己炸了
            problems.append(
                f"函数 {name}：装载失败（模块级代码抛异常）：{type(exc).__name__}: {exc}"
            )
            continue
        fn = namespace.get(entry)
        if fn is None:
            problems.append(f"函数 {name}：源码里找不到入口 {entry!r}")
            continue
        if not callable(fn):
            problems.append(f"函数 {name}：{entry!r} 不是可调用的对象")
            continue
        loaded[name] = LoadedFunction(
            name,
            fn,
            spec.get("mode") or "row",
            spec.get("input_fields") or [],
            spec.get("output_fields") or [],
        )
    return loaded, problems


# =========================================================================== 执行


def _error_text(exc: BaseException) -> str:
    """异常文本。只取最后一行堆栈 —— 报告里不需要整个调用链，但需要出错的位置。"""
    lines = traceback.format_exception_only(type(exc), exc)
    return "".join(lines).strip()


def run_rows(fn: LoadedFunction, fields, rows):
    """逐行执行。单行抛异常**不是**整批失败：那一行记为错误、结果置 null（不改），
    其余行照常处理。一个函数在 100 万行里被一行脏数据绊倒，不该让整个任务失败。"""
    out: list = []
    errors: list[dict] = []
    notes: set[str] = set()
    declared = set(fn.output_fields)
    for index, values in enumerate(rows):
        try:
            result = fn.fn(dict(zip(fields, values)))
        except MemoryError:
            # 内存超限不算「这一行有问题」：同一批的下一行也会一样失败，而且进程的分配器
            # 这时可能已经不可靠。抛出去让父进程按块级失败处理（杀掉重启）。
            raise
        except Exception as exc:
            errors.append({"row": index, "error": _error_text(exc)})
            out.append(None)
            continue
        if result is None:
            out.append(None)
            continue
        if not isinstance(result, dict):
            errors.append(
                {"row": index, "error": f"返回值必须是 dict 或 None，得到 {type(result).__name__}"}
            )
            out.append(None)
            continue
        extra = set(result) - declared
        if extra:
            notes.add(
                "返回了未声明的字段，已忽略："
                + "、".join(sorted(extra))
                + "（要写回数据请把它们加进 output_fields）"
            )
        changed = {key: result[key] for key in result if key in declared}
        out.append(changed or None)
    return out, errors, sorted(notes)


def run_columns(fn: LoadedFunction, columns: dict):
    """整列模式：一次拿到一批值、返回一批值。省掉逐行组包与 Python 层循环。"""
    result = fn.fn(dict(columns))
    notes: list[str] = []
    if not isinstance(result, dict):
        raise TypeError(f"返回值必须是 dict，得到 {type(result).__name__}")
    declared = set(fn.output_fields)
    extra = set(result) - declared
    if extra:
        notes.append(
            "返回了未声明的字段，已忽略："
            + "、".join(sorted(extra))
            + "（要写回数据请把它们加进 output_fields）"
        )
    width = len(next(iter(columns.values()))) if columns else 0
    out = {}
    for key in result:
        if key not in declared:
            continue
        values = result[key]
        if not isinstance(values, (list, tuple)):
            raise TypeError(f"字段 {key!r} 的返回值必须是列表，得到 {type(values).__name__}")
        if len(values) != width:
            raise ValueError(
                f"字段 {key!r} 返回了 {len(values)} 个值，这一批有 {width} 行 —— "
                "整列模式必须一进一出等长"
            )
        out[key] = list(values)
    return out, notes


# =========================================================================== 主循环


def _claim_stdout():
    """把真正的 stdout 拿到手，然后把 fd 1 换成 stderr。

    之后用户代码（包括它 fork 出来的孙子进程）无论怎么 `print`，都写进 stderr，
    协议流不会被打断。这是「print 免疫」的全部实现。
    """
    protocol = os.fdopen(os.dup(1), "wb")
    try:
        os.dup2(2, 1)
    except OSError:  # pragma: no cover - 没有 stderr 的极端环境
        pass
    return protocol


def _write(protocol, frame) -> None:
    protocol.write(dumps(frame))
    protocol.write(b"\n")
    protocol.flush()


def _apply_memory_limit(memory_mb: int) -> str:
    """给自己套上地址空间上限。返回提示文本（空 = 没问题）。

    注意 `RLIMIT_AS` 管的是**虚拟**地址空间，比常说的内存占用大：解释器自身的映射、
    每个线程的栈、arena 的预留都算在内。所以下限给 256MB，低于 512MB 时提醒一句 ——
    卡太紧会在一个完全正常的函数里炸出 MemoryError。
    """
    if memory_mb <= 0:
        return ""
    note = ""
    if memory_mb < 256:
        note = f"内存上限 {memory_mb}MB 低于 256MB，解释器自身就可能撞上，已按 256MB 处理"
        memory_mb = 256
    elif memory_mb < 512:
        note = f"内存上限 {memory_mb}MB 偏低，正常函数也可能因解释器开销触发 MemoryError"
    try:
        import resource

        limit = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception as exc:  # 平台不支持就放弃限制，但要说出来
        return f"无法设置内存上限（{type(exc).__name__}: {exc}），本次不做内存限制"
    return note


def main(argv=None) -> int:
    protocol = _claim_stdout()
    source = sys.stdin.buffer
    first = source.readline()
    if not first:
        return 0
    try:
        frame = loads(first)
    except Exception as exc:
        sys.stderr.write(f"[clean_worker] 初始帧不是合法 JSON：{exc}\n")
        return 3

    frame_id = frame.get("id")
    if frame.get("op") != "init":
        _write(protocol, {"id": frame_id, "ok": False, "kind": "protocol_error",
                          "error": f"第一帧必须是 init，收到 {frame.get('op')!r}"})
        return 3
    if int(frame.get("protocol", 0)) != PROTOCOL_VERSION:
        _write(protocol, {
            "id": frame_id, "ok": False, "kind": "protocol_error",
            "error": f"协议版本不匹配：父进程 {frame.get('protocol')}，本进程 {PROTOCOL_VERSION}",
        })
        return 3

    note = _apply_memory_limit(int(frame.get("memory_mb") or 0))
    loaded, problems = load_functions(
        frame.get("functions") or [], seed=frame.get("seed")
    )
    if problems:
        # 一个函数有问题就整批拒绝：半个装载的进程会让「哪些函数生效了」变得说不清
        _write(protocol, {"id": frame_id, "ok": False, "kind": "init_error",
                          "error": "；".join(problems[:5]), "problems": problems})
        return 0
    _write(protocol, {"id": frame_id, "ok": True, "loaded": sorted(loaded),
                      "notice": note})

    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            request = loads(line)
        except Exception as exc:
            # 帧都读不懂说明协议已经错位了，继续跑只会给出越来越离谱的结果
            sys.stderr.write(f"[clean_worker] 无法解析请求帧：{exc}\n")
            return 3
        _write(protocol, _dispatch(loaded, request))
    return 0


def _dispatch(loaded: dict, request) -> dict:
    frame_id = request.get("id")
    name = request.get("fn")
    fn = loaded.get(name)
    if fn is None:
        return {"id": frame_id, "ok": False, "kind": "protocol_error",
                "error": f"未装载的函数：{name!r}"}
    try:
        if "columns" in request:
            out, notes = run_columns(fn, request.get("columns") or {})
            return {"id": frame_id, "ok": True, "columns": out, "notes": notes}
        rows = request.get("rows") or []
        out, errors, notes = run_rows(fn, request.get("fields") or [], rows)
        return {"id": frame_id, "ok": True, "rows": out, "errors": errors, "notes": notes}
    except MemoryError:
        return {"id": frame_id, "ok": False, "kind": "memory_error",
                "error": f"函数 {name} 触发了内存上限"}
    except (TypeError, ValueError) as exc:
        return {"id": frame_id, "ok": False, "kind": "shape_error",
                "error": f"函数 {name}：{_error_text(exc)}"}
    except Exception as exc:
        return {"id": frame_id, "ok": False, "kind": "runtime_error",
                "error": f"函数 {name}：{_error_text(exc)}",
                "traceback": traceback.format_exc()[-2000:]}


if __name__ == "__main__":
    sys.exit(main())
