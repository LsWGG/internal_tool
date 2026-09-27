import MarkdownIt from 'markdown-it'

/* 全站唯一的 Markdown 渲染器。
 *
 * 为什么是 markdown-it 而不是手写：回归报告与 AI 回复的骨架都是**表格与列表**，而
 * 「GFM 表格」恰好是行式渲染器最容易写错的地方（单元格里的 `|`、表格紧跟列表、代码块
 * 里出现表格）。手写的那个还只认 3 个标题级别、不支持围栏代码块与表格。
 *
 * 安全性靠两条默认设置，它们合起来让 `v-html` 在这里是安全的：
 *   1. `html:false` —— 输入里的原始 HTML 一律**转义**，没有任何标签能穿过。这正是
 *      不需要 DOMPurify 的原因：没有 HTML 可注入，而不是「注入了再消毒」。
 *   2. `validateLink` 用 markdown-it 自带的默认实现 —— `javascript:`/`vbscript:`/
 *      `file:`/`data:`（除了图片）都发不出链接。
 * 任何往这里加 `html:true` 的改动都必须同时引入一个消毒库，不要只改这一个开关。
 */
const md = new MarkdownIt({
  html: false,
  linkify: false,
  // 聊天里的换行是有意义的（旧渲染器逐行成段），而报告是块结构的、不受影响
  breaks: true,
  typographer: false,
})

// 表格外面包一层可横向滚动的容器：报告里的宽表在窄屏上会撑破整个页面
if (md.renderer.rules.table_open) {
  const open = md.renderer.rules.table_open
  md.renderer.rules.table_open = (tokens, idx, options, env, self) =>
    '<div class="md-table-scroll">' + open(tokens, idx, options, env, self)
  md.renderer.rules.table_close = (tokens, idx, options, env, self) =>
    self.renderToken(tokens, idx, options) + '</div>'
}

// 外部链接新窗口打开时必须带 noopener：`window.opener` 能让被打开的页面反向操纵本页
md.renderer.rules.link_open = (tokens, idx, options, env, self) => {
  tokens[idx].attrSet('target', '_blank')
  tokens[idx].attrSet('rel', 'noopener noreferrer')
  return self.renderToken(tokens, idx, options)
}

export function renderMarkdown(value) {
  return md.render(String(value ?? ''))
}

export default md
