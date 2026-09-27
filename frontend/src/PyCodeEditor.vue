<script setup>
/* 用户函数的源码编辑器：CodeMirror 6 + 白名单补全 + 后端静态检查波浪线。

选 CodeMirror 而不是 Monaco：两者都没有 Python 语言服务器，所谓「智能提示」都得自己接；
而 Monaco 的体积是这里的十几倍，本仓库又是全量打包。CM 是纯 ESM、不需要 worker，
`vite.config.js` 一行都不用改。

补全的**词表来自后端**（`/meta.python`，即子进程真正允许的那份白名单），前端一份都不另写：
补全里出现 `open`，用户点一下，写出来的函数必然被闸门拒掉 —— 那是界面在教用户写错的东西。

静态检查也走后端（`clean_worker` 的 AST 闸门），不在前端重新猜语法：同一套规则两处实现，
迟早会出现「编辑器不划线但任务跑不了」。
*/

import {onBeforeUnmount,onMounted,ref,shallowRef,watch} from 'vue'
import {EditorState} from '@codemirror/state'
import {EditorView,drawSelection,dropCursor,highlightActiveLine,highlightActiveLineGutter,
  highlightSpecialChars,keymap,lineNumbers} from '@codemirror/view'
import {defaultKeymap,history,historyKeymap,indentWithTab} from '@codemirror/commands'
import {HighlightStyle,bracketMatching,indentOnInput,indentUnit,syntaxHighlighting} from '@codemirror/language'
import {autocompletion,closeBrackets,closeBracketsKeymap,completionKeymap} from '@codemirror/autocomplete'
import {forEachDiagnostic,forceLinting,lintGutter,lintKeymap,linter} from '@codemirror/lint'
import {localCompletionSource,python,pythonLanguage} from '@codemirror/lang-python'
import {tags} from '@lezer/highlight'

const props=defineProps({
  modelValue:{type:String,default:''},
  // 勾选过的字段名。**没勾的不给补全**：运行时读不到、也写不进去，给了就是同一个错误的两半。
  inputFields:{type:Array,default:()=>[]},
  outputFields:{type:Array,default:()=>[]},
  // `/meta.python`：{builtins, modules, banned}。拿不到时退化成「只有关键字 + 文档内的名字」。
  vocabulary:{type:Object,default:()=>({builtins:[],modules:[]})},
  mode:{type:String,default:'row'},
  entry:{type:String,default:'transform'},
  // 传进来才挂 lint。测试桩（ui_catalog 会 abort 所有 /api）拿不到它，此时不该发请求。
  checkSource:Function,
})
const emit=defineEmits(['update:modelValue'])

/* Python 关键字是语言级常量，写死在这里是安全的 —— 它们不随后端白名单变化。
   只有 `import` 例外：它**故意不提供**，见下面的模块片段（补 `import math` 而不是补
   `import `，让「界面能点的」和「后端能跑的」是同一件事）。 */
const KEYWORDS=['and','as','assert','async','await','break','class','continue','def','del',
  'elif','else','except','finally','for','from','global','if','in','is','lambda','nonlocal',
  'not','or','pass','raise','return','try','while','with','yield','True','False','None']
/* 常用项的一句话说明。没写到的（异常类、`ascii` 这种）就只显示名字：
   编一句错的说明比不编更糟。 */
const BUILTIN_NOTE={len:'长度',str:'转字符串',int:'转整数',float:'转小数',bool:'转真假',
  round:'四舍五入',abs:'绝对值',min:'最小值',max:'最大值',sum:'求和',sorted:'排序（返回新列表）',
  range:'整数序列',enumerate:'带下标的遍历',zip:'按位置配对',dict:'字典',list:'列表',set:'集合',
  tuple:'元组',isinstance:'类型判断',repr:'可读形式',chr:'码点转字符',ord:'字符转码点',
  format:'按格式串拼接',filter:'按条件筛选',map:'逐项映射',any:'任意一个为真',all:'全部为真',
  reversed:'倒序迭代',divmod:'商与余数',iter:'取迭代器',next:'取下一个',hash:'哈希值',
  bytes:'字节串',complex:'复数',frozenset:'不可变集合',slice:'切片对象',type:'类型'}
const MODULE_NOTE={math:'数学',re:'正则',re2:'正则（线性时间引擎，推荐）',json:'JSON',
  orjson:'JSON（更快）',datetime:'日期时间',time:'时间',decimal:'精确小数',statistics:'统计',
  random:'随机',hashlib:'哈希',uuid:'UUID',itertools:'迭代工具',functools:'函数工具',
  collections:'容器',string:'字符常量',unicodedata:'Unicode 规范化',base64:'Base64',
  csv:'CSV 读写',io:'内存流',typing:'类型标注',dataclasses:'数据类',enum:'枚举',
  zoneinfo:'时区',calendar:'日历',operator:'运算符函数',fractions:'分数',bisect:'二分',
  heapq:'堆',copy:'拷贝',warnings:'告警',faker:'假数据',textwrap:'折行',difflib:'差异',
  pprint:'美化打印'}

const host=ref(null)
/* `shallowRef` 而不是 `ref`：`ref` 的深层代理会包住 CM 的内部对象，破坏它靠身份做的比较。 */
const view=shallowRef(null)
const problems=ref([])
let problemStamp=''

/* 光标前 400 字符里找「一个没闭合的引号」，再回头看它前面是哪个括号。
   只看这一小段而不是整篇：字符串字面量跨行的写法在用户函数里没有实用场景，
   而整篇扫描会在每次按键时重算一遍全文。 */
function stringContext(context){
  const text=context.state.sliceDoc(Math.max(0,context.pos-400),context.pos)
  const match=/(['"])([^'"]*)$/.exec(text)
  if(!match)return null
  const before=text.slice(0,match.index)
  const opener=(/([([{,])\s*$/.exec(before)||[])[1]||''
  return {typed:match[2],opener}
}

/* 字段名：**只在字符串里给**。裸标识符写字段名是 `NameError`，补全不该把人往那儿带。
   `[` `(` 后面给输入字段（`row['…']`、`row.get('…')`），`{` 后面给写回字段
   （`return {'…': …}`）；逗号与定位不到括号时两边都给 —— 给多了只是列表长一点，
   给少了用户就得自己回忆字段名。 */
function fieldSource(context){
  if(context.view?.composing)return null      // 中文输入法选词中：弹补全吞掉回车很难受
  const hit=stringContext(context)
  if(!hit)return null
  const opener=hit.opener
  const wantsInput=opener==='['||opener==='('||opener===''||opener===','
  const wantsOutput=opener==='{'||opener===''||opener===','
  const roles=new Map()
  if(wantsInput)for(const name of props.inputFields)roles.set(name,'输入字段')
  if(wantsOutput)for(const name of props.outputFields)
    roles.set(name,roles.has(name)?'输入 · 写回字段':'写回字段')
  if(!roles.size)return null
  return {
    from:context.pos-hit.typed.length,
    options:[...roles].map(([label,detail])=>({label,type:'property',detail,apply:label})),
  }
}

function vocabularySource(context){
  if(context.view?.composing)return null
  if(stringContext(context))return null       // 字符串里归字段补全管，这里别插嘴
  const explicit=context.explicit
  const word=context.matchBefore(/[A-Za-z_]\w*/)
  if(!word&&!explicit)return null
  if(word&&word.from===word.to&&!explicit)return null
  const options=[]
  for(const name of props.vocabulary.builtins||[])
    options.push({label:name,type:'function',detail:BUILTIN_NOTE[name]||'内置函数'})
  for(const name of KEYWORDS)options.push({label:name,type:'keyword',detail:'关键字'})
  // 入参名跟着模式走：整列模式收到的是 {字段: [值…]}，写 `row` 会直接 NameError。
  options.push(props.mode==='column'
    ?{label:'columns',type:'variable',detail:'入参：{字段: [值…]}'}
    :{label:'row',type:'variable',detail:'入参：{字段: 值}'})
  for(const name of props.vocabulary.modules||[])
    options.push({label:`import ${name}`,type:'keyword',detail:MODULE_NOTE[name]||'白名单模块'})
  return {from:word?word.from:context.pos,options}
}

/* 静态检查：把后端给的 {line,text,severity} 变成诊断。
   划线划**整行**：后端给的是行号（`Problem.line`），而列偏移在后端是 UTF-8 字节、
   在编辑器里是字符 —— 源码里只要有中文注释，两者就对不上，波浪线会划到别的字上。 */
function lintSource(editorView){
  const source=editorView.state.doc.toString()
  if(typeof props.checkSource!=='function'||!source.trim())return []
  return Promise.resolve(props.checkSource(source)).then(result=>{
    const doc=editorView.state.doc
    return ((result&&result.problems)||[]).map(item=>{
      // 定位不到行的问题（「源码里找不到入口」这类）标在第 1 行：不标的话行号槽里什么都
      // 没有，用户只能挨个悬停去找。
      const line=doc.line(Math.min(Math.max(1,item.line||1),doc.lines))
      return {
        from:line.from,to:line.to,
        severity:item.severity==='error'?'error':'warning',
        message:item.text,
      }
    })
  }).catch(()=>[])
}

/* 卡片里那份问题清单直接从**编辑器自己的诊断**里读。
   不在 lintSource 里顺手存一份：那个 Promise 可能已经过期（边打字边检查），
   CM 会丢掉过期诊断，自己存的那份却会留下 —— 屏幕上的清单和波浪线就对不上了。 */
function collectProblems(state){
  const found=[]
  forEachDiagnostic(state,diagnostic=>{
    found.push({severity:diagnostic.severity,message:diagnostic.message})
  })
  const stamp=JSON.stringify(found)
  if(stamp===problemStamp)return
  problemStamp=stamp
  problems.value=found
}

/* 样式分两处，不是随手写的：
   - 外框（border / 圆角 / 字号 / 最小高度 / 内部滚动）走下面 `<style scoped>` 的 `:deep()`，
     跟着仓库既有惯用法，也和卡片里其它控件的尺寸口径一致；
   - CM 内部的那些（选中底色、光标、补全弹窗、波浪线）留在 `EditorView.theme` 里 ——
     补全弹窗被祖先 `overflow:hidden` 裁掉时唯一的解法是把它挂到 `document.body` 下，
     那时 scoped 样式就够不着它了，而 theme 是 CM 自己注入的全局规则，跟随 DOM 走。 */
const lightTheme=EditorView.theme({
  '&':{backgroundColor:'#fff'},
  '.cm-content':{caretColor:'#3562bd'},
  '.cm-cursor, .cm-dropCursor':{borderLeftColor:'#3562bd'},
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
    {backgroundColor:'#dbe7fb'},
  '.cm-activeLine':{backgroundColor:'#f7faff'},
  '.cm-activeLineGutter':{backgroundColor:'#f7faff',color:'#7d8fa5'},
  '.cm-gutters':{backgroundColor:'#fbfcfe',color:'#9aabc0',border:'none'},
  '.cm-lineNumbers .cm-gutterElement':{padding:'0 6px 0 8px'},
  '.cm-tooltip':{border:'1px solid #dbe4f0',borderRadius:'8px',background:'#fff',
    boxShadow:'0 8px 24px rgba(48,75,112,.14)'},
  '.cm-tooltip-autocomplete ul li[aria-selected]':{background:'#eef3fc',color:'#2f5bb0'},
  '.cm-completionDetail':{color:'#74869b',fontStyle:'normal',marginLeft:'8px'},
  '.cm-tooltip.cm-tooltip-autocomplete > ul':{maxHeight:'220px',fontFamily:'inherit'},
  '.cm-lintRange-error':{backgroundImage:'none',borderBottom:'2px wavy #c2607a'},
  '.cm-lintRange-warning':{backgroundImage:'none',borderBottom:'2px wavy #d8a35a'},
})

const highlight=HighlightStyle.define([
  {tag:tags.keyword,color:'#a03fa0'},
  {tag:tags.controlKeyword,color:'#a03fa0'},
  {tag:[tags.string,tags.special(tags.string)],color:'#2f7c63'},
  {tag:tags.comment,color:'#93a7bf',fontStyle:'italic'},
  {tag:tags.number,color:'#8a6224'},
  {tag:tags.bool,color:'#8a6224'},
  {tag:tags.null,color:'#8a6224'},
  {tag:tags.definition(tags.variableName),color:'#3562bd'},
  {tag:tags.function(tags.variableName),color:'#405e91'},
  {tag:tags.operator,color:'#5e7791'},
  {tag:tags.propertyName,color:'#405e91'},
  {tag:tags.punctuation,color:'#74869b'},
])

function extensions(){
  const list=[
    lineNumbers(),highlightActiveLineGutter(),highlightSpecialChars(),history(),
    drawSelection(),dropCursor(),EditorState.allowMultipleSelections.of(true),
    indentOnInput(),bracketMatching(),closeBrackets(),
    // 默认缩进是 2 个空格，`lang-python` 不会覆盖它 —— 不显式设成 4，用户按回车得到的
    // 缩进和自己的源码对不上（后端 `ruff format` 也是 4 个）。
    indentUnit.of('    '),EditorState.tabSize.of(4),
    python(),syntaxHighlighting(highlight),EditorView.lineWrapping,
    autocompletion({
      // `override` 会把 `python()` 自带的两个来源**一起换掉**，其中 `globalCompletion`
      // 给的是完整 Python 内置表（含 `open`/`eval`）—— 必须换成后端白名单那一份。
      // `localCompletionSource`（文档内的局部名）是导出的，原样列回来，不必自己实现。
      override:[fieldSource,vocabularySource,localCompletionSource],
      // 弹窗朝上开：卡片外面包着 `overflow:hidden` 的步骤体，朝下会被裁掉。
      aboveCursor:true,
      icons:false,
    }),
    keymap.of([...closeBracketsKeymap,...defaultKeymap,...historyKeymap,
      ...completionKeymap,...lintKeymap,indentWithTab]),
    EditorView.updateListener.of(update=>{
      if(update.docChanged){
        const value=update.state.doc.toString()
        if(value!==props.modelValue)emit('update:modelValue',value)
      }
      collectProblems(update.state)
    }),
  ]
  if(typeof props.checkSource==='function'){
    list.push(lintGutter(),linter(lintSource,{delay:400}))
  }
  return list
}

onMounted(()=>{
  const instance=new EditorView({
    state:EditorState.create({doc:props.modelValue||'',extensions:extensions()}),
    parent:host.value,
  })
  view.value=instance
  // 打开一张**本来就有错**的卡片时立刻检查一次：否则要等到用户敲第一个键才出现波浪线，
  // 而那时他已经在改了。
  if((props.modelValue||'').trim())forceLinting(instance)
})

onBeforeUnmount(()=>{
  view.value?.destroy()
  view.value=null
})

/* 父子同步的闭环，两句都必须在：
   - updateListener 只在 doc 真变了时 emit（否则父组件一改值就回环）；
   - 这里先比一次文本，相同就不 dispatch（否则「父改 → 子 dispatch → 子 emit → 父改」死循环）。
   程序化写入（「用这段」「格式化」）走的就是这条 dispatch 路径，所以 Ctrl+Z 能撤销。 */
watch(()=>props.modelValue,value=>{
  const instance=view.value
  if(!instance)return
  const text=value??''
  if(instance.state.doc.toString()===text)return
  instance.dispatch({changes:{from:0,to:instance.state.doc.length,insert:text}})
})
</script>

<template>
  <div class="py-editor-wrap">
    <div ref="host" class="py-editor"></div>
    <ul v-if="problems.length" class="clean-fn-problems">
      <li v-for="(item,at) in problems" :key="at" :class="item.severity">{{item.message}}</li>
    </ul>
  </div>
</template>

<style scoped>
/* `:deep()` 而不是 `EditorView.theme`：仓库已有这个惯用法（见 .clean-report :deep(h1)），
   编译出来是「类 + 属性 + 类」，稳压 CM 自己的单类基础规则，样式也仍在一个 <style scoped> 里。 */
.py-editor-wrap{min-width:0}
/* 390px 下不横向溢出的前提：这一层必须能被压缩。 */
.py-editor{position:relative;min-width:0}
.py-editor :deep(.cm-editor){min-height:calc(150px * var(--ui-scale));border:1px solid var(--clean-line,#dbe4f0);border-radius:8px;background:#fff;font:var(--fs-11,11px)/1.75 ui-monospace,SFMono-Regular,Menlo,monospace}
.py-editor :deep(.cm-editor.cm-focused){outline:0;border-color:#87a7df;box-shadow:0 0 0 3px rgba(65,111,209,.12)}
/* 长源码在编辑器内部滚，别把配置列撑到几千像素高。 */
.py-editor :deep(.cm-scroller){max-height:calc(260px * var(--ui-scale));overflow:auto}
.py-editor :deep(.cm-content){padding:6px 0}
.clean-fn-problems{margin:8px 0 0;padding-left:20px;font-size:var(--fs-11,11px);line-height:1.85}
.clean-fn-problems li{color:#a8566a;overflow-wrap:anywhere}
.clean-fn-problems li.warning{color:#8a6224}
</style>
