"""模板里调用的每个名字，`<script setup>` 里都必须真的存在。

这条检查来自两个真实故障：`CleanTool.vue` 的模板调用了 `sum(...)` 与 `pct(...)`，两个
函数都**没写**。`<script setup>` 下模板里解析不到的名字会退化成组件属性，于是渲染时报
`_ctx.sum is not a function` —— 整块 DOM 不再更新，界面上表现为「点了没反应」：试跑结果
不出现、字段统计切不过去（因为切 tab 的那次渲染就炸了，屏幕上留着上一个 tab）。

Vue 编译器不查这个，浏览器只在**那条分支真的渲染时**才报，所以最典型的漏检路径是：
页面渲染正常、点进某个 tab 才炸。这个脚本不需要浏览器，把这类名字静态找出来。

用法：python test_template_bindings.py   （只用标准库）
"""

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent / "src"

# 模板里合法出现、但不由 `<script setup>` 定义的名字：JS 内置与模板自带变量。
GLOBALS = {
    "if", "for", "while", "switch", "return", "typeof", "catch", "in", "of", "new",
    "Number", "String", "Boolean", "Array", "Object", "JSON", "Math", "Date", "RegExp",
    "Intl", "Promise", "Error", "Set", "Map",
    "parseInt", "parseFloat", "isNaN", "isFinite",
    "encodeURIComponent", "decodeURIComponent", "structuredClone",
    "setTimeout", "clearTimeout",
}


def strip_strings(text: str) -> str:
    """去掉模板里的单引号与反引号串。

    只去这两种：Vue 属性本身用双引号，所以 `"..."` 里的内容仍然是表达式（`fn(x)` 这种
    真调用必须在里面）；而 CSS 函数是写在 `` `transform:translate3d(…)` `` 这类字符串里的，
    不去掉就会把它们当成函数调用。
    """
    text = re.sub(r"`[^`]*`", "``", text, flags=re.S)
    return re.sub(r"'[^'\n]*'", "''", text)


# 模板里带表达式的属性：`:x=` / `@x=` / `v-名字=`。**只看这些里的内容** ——
# 正文（两个标签之间的字）是散文，`<code>def transform(row)</code>` 这样的说明文字
# 会长得和函数调用一模一样，扫正文必然误报。
EXPR_ATTR = re.compile(
    r"""(?::|@|v-bind:|v-on:|v-if|v-else-if|v-for|v-show|v-model[\w.]*)[\w.:-]*\s*=\s*(["'])(.*?)\1""",
    re.S,
)
HANDLER_ATTR = re.compile(r"""(?:@|v-on:)[\w.:-]+\s*=\s*(["'])(.*?)\1""", re.S)
MUSTACHE = re.compile(r"\{\{(.*?)\}\}", re.S)
# 裸标识符。`\w` 在 Python 里默认是 Unicode 语义，于是 `保存` 这种中文绑定名也算 ——
# 它确实是合法的 JS 标识符，界面上也确实会写。
BARE_HANDLER = re.compile(r"^(?!\d)[\w$]+$")


def expression_spans(template: str) -> list[str]:
    return MUSTACHE.findall(template) + [value for _, value in EXPR_ATTR.findall(template)]


def called_in_template(template: str) -> set[str]:
    """模板表达式里以**裸标识符**形式被调用（或被当成事件处理函数）的名字。

    排除 `obj.method(` / `a?.b(` / `this.x(` 这类属性访问 —— 它们不是 setup 绑定。
    """
    found: set[str] = set()
    for span in expression_spans(template):
        found |= set(re.findall(r"(?<![\w.$?])([A-Za-z_$][\w$]*)\s*\(", strip_strings(span)))
        # `@click="保存"` 这种不带括号的处理器同样是 setup 绑定，缺了它点击时才炸
        for _, value in HANDLER_ATTR.findall(span):
            if BARE_HANDLER.match(value.strip()):
                found.add(value.strip())
    for _, value in HANDLER_ATTR.findall(template):
        if BARE_HANDLER.match(value.strip()):
            found.add(value.strip())
    return {name for name in found if not name.startswith("$")}


def defined_in_script(script: str) -> set[str]:
    names = set(re.findall(r"(?:function|const|let|var)\s+([A-Za-z_$][\w$]*)", script))
    for block in re.findall(r"import[^\n]*?\{([^}]*)\}", script):
        names |= {part.strip().split(" as ")[-1].strip()
                  for part in block.split(",") if part.strip()}
    names |= set(re.findall(r"import\s+([A-Za-z_$][\w$]*)\s+from", script))
    # `const {a, b} = props` / `const [x] = ...` 这类解构也带出绑定
    for block in re.findall(r"(?:const|let|var)\s*\{([^}]*)\}\s*=", script):
        names |= {part.strip().split(":")[-1].strip().split("=")[0].strip()
                  for part in block.split(",") if part.strip()}
    return names


# 检查器自己的用例。不做这一步的话，一个「永远通过」的检查器看起来和全绿一模一样 ——
# 它必须被证明能抓到那两个真实故障里出现过的东西。
SELF_CASES = (
    ("缺函数要被抓到", "<div>{{ sum(a) }}</div>", "const a=1", ["sum"]),
    ("属性访问不算调用", "<div>{{ item.trim() }}</div>", "const item=ref('')", []),
    ("字符串里的 CSS 函数不算", '<img :style="{transform:`translate3d(1px) scale(2)`}">',
     "const x=1", []),
    ("单引号里包着的表达式仍要检查", "<div :class=\"{'x':pct(v)>1}\"></div>", "const v=1", ["pct"]),
    ("import 进来的名字算已定义", "<div>{{fmt(x)}}</div>",
     "import a, {fmt} from './c'", []),
    ("正文里的代码示例不算调用", "<p>调用 <code>def transform(row)</code> 即可</p>",
     "const x=1", []),
    ("不带括号的事件处理器也要检查", '<button @click="保存">存</button>',
     "const x=1", ["保存"]),
    ("v-for 的循环变量不算", '<div v-for="item in rows">{{item.trim()}}</div>',
     "const rows=[]", []),
)


def self_check() -> list[str]:
    problems = []
    for label, template, script, expected in SELF_CASES:
        missing = sorted(called_in_template(template)
                         - (defined_in_script(script) | GLOBALS))
        if missing != expected:
            problems.append(f"{label}：期望 {expected}，实际 {missing}")
    return problems


def main() -> int:
    problems = self_check()
    for problem in problems:
        print(f"FAIL 检查器自身：{problem}", file=sys.stderr)
    if problems:
        return 1

    checked = skipped = 0
    failures = []
    for path in sorted(SRC.glob("*.vue")):
        source = path.read_text(encoding="utf-8")
        if "<template>" not in source or "<script setup>" not in source:
            print(f"跳过 {path.name}（没有 <template> 或 <script setup>）")
            skipped += 1
            continue
        template = source.split("<template>", 1)[1].rsplit("</template>", 1)[0]
        script = source.split("<script setup>", 1)[1].rsplit("</script>", 1)[0]
        called = called_in_template(template)
        known = defined_in_script(script) | GLOBALS
        missing = sorted(called - known)
        checked += 1
        print(f"{path.name}: 模板调用 {len(called)} 个名字，未定义 {missing}")
        if missing:
            failures.append((path.name, missing))

    print(f"检查 {checked} 个组件，跳过 {skipped} 个")
    if failures:
        for name, missing in failures:
            print(f"FAIL {name}: 模板调用了未定义的 {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
