<script setup>
/* compose.yaml 的编辑器：CodeMirror 6 + YAML 高亮 + 语法检测。

  跟 `PyCodeEditor.vue` 是同一套做法（CM6、不用 worker、`vite.config.js` 一行不改），
   只有一处不同值得说明：

   **检查走后端**（`POST /api/compose/validate`，同一个 PyYAML）。理由和那份组件里写的一样：
   语法规则在前端再实现一遍，迟早会出现「编辑器不划线、但保存/启动失败」。所以检查源由父组件
   以 `checkSource` 传进来，本组件自己不 import 任何 YAML 解析器 —— 前端只负责把后端给的
   `{line,text,severity}` 画成波浪线。

   高亮的颜色全部写 `var(--compose-*)`：那些令牌挂 `:root`，亮/暗各一套，于是这个编辑器
   不需要为暗色再写一份主题（CM 的 theme 就是普通 CSS，变量照常生效）。 */

import {onBeforeUnmount,onMounted,ref,shallowRef,watch} from 'vue'
import {EditorState} from '@codemirror/state'
import {EditorView,drawSelection,dropCursor,highlightActiveLine,highlightActiveLineGutter,
  highlightSpecialChars,keymap,lineNumbers,placeholder as placeholderExtension} from '@codemirror/view'
import {defaultKeymap,history,historyKeymap,indentWithTab} from '@codemirror/commands'
import {HighlightStyle,bracketMatching,indentOnInput,indentUnit,syntaxHighlighting} from '@codemirror/language'
import {forEachDiagnostic,forceLinting,lintGutter,lintKeymap,linter} from '@codemirror/lint'
import {yaml} from '@codemirror/lang-yaml'
import {tags} from '@lezer/highlight'

const props=defineProps({
  modelValue:{type:String,default:''},
  placeholder:{type:String,default:''},
  // 传进来才挂 lint。拿不到它（或检查本身失败）就没有波浪线，但**不拦着保存**。
  checkSource:Function,
})
const emit=defineEmits(['update:modelValue','problems'])

const host=ref(null)
/* `shallowRef` 而不是 `ref`：`ref` 的深层代理会包住 CM 的内部对象，破坏它靠身份做的比较。 */
const view=shallowRef(null)
const problems=ref([])
let problemStamp=''

/* 后端的 {line,text,severity} → CM 的诊断。划**整行**：后端给的是行号，而列偏移在两边
   的计量单位不同（后端按 UTF-8 字节、编辑器按字符），划到具体某一列上，中文注释一多就会歪。 */
function lintSource(editorView){
  const source=editorView.state.doc.toString()
  if(typeof props.checkSource!=='function'||!source.trim())return []
  return Promise.resolve(props.checkSource(source)).then(result=>{
    const doc=editorView.state.doc
    return ((result&&result.problems)||[]).map(item=>{
      const line=doc.line(Math.min(Math.max(1,item.line||1),doc.lines))
      /* 零长度的标记 CM 画不出来，问题会只剩清单里那一行、正文上什么都没有 —— 兜一格的宽度，
         让它至少有个可见的落点（后端也会避开空白行，这里是第二道）。 */
      const end=line.to>line.from?line.to:Math.min(line.from+1,doc.length)
      return {from:line.from,to:end,
        severity:item.severity==='error'?'error':'warning',message:item.text}
    })
  }).catch(()=>[])
}

/* 问题清单从**编辑器自己的诊断**里读，不在 lintSource 里顺手存一份：那个 Promise 可能已经
   过期（边打字边检查），CM 会丢掉过期诊断，自己存的那份却会留下 —— 屏幕上的清单和波浪线
   就对不上了。父组件拿这份清单决定「保存」能不能点（有 error 才拦）。 */
function collectProblems(state){
  const found=[]
  forEachDiagnostic(state,diagnostic=>{
    const line=state.doc.lineAt(diagnostic.from).number
    found.push({line,severity:diagnostic.severity,message:diagnostic.message})
  })
  const stamp=JSON.stringify(found)
  if(stamp===problemStamp)return
  problemStamp=stamp
  problems.value=found
  emit('problems',found)
}

/* 外框（边框/圆角/高度/滚动）走下面 `<style scoped>` 的 `:deep()`，和仓库既有惯用法一致；
   CM 内部的这些（光标、选中、行号槽、波浪线、提示气泡）留在 theme 里 —— 它们跟着编辑器自己
   生成的 DOM 走，提示气泡还可能被挂到 body 下，那时 scoped 样式够不着。 */
const theme=EditorView.theme({
  '&':{height:'100%',backgroundColor:'var(--compose-field)',color:'var(--compose-code-text)'},
  '.cm-scroller':{overflow:'auto'},
  '.cm-content':{padding:'12px 0',caretColor:'var(--compose-ink)'},
  '.cm-cursor, .cm-dropCursor':{borderLeftColor:'var(--compose-ink)'},
  '&.cm-focused .cm-selectionBackground, .cm-selectionBackground, .cm-content ::selection':
    {backgroundColor:'var(--compose-active)'},
  '.cm-activeLine':{backgroundColor:'var(--compose-tint)'},
  // 占位符是一段真 YAML：不写 pre 的话换行会被折成一行，看着像乱码。
  '.cm-placeholder':{color:'var(--compose-muted)',whiteSpace:'pre'},
  '.cm-gutters':{backgroundColor:'var(--compose-soft)',color:'var(--compose-muted)',border:'none',
    borderRight:'1px solid var(--compose-line)'},
  '.cm-activeLineGutter':{backgroundColor:'var(--compose-tint)',color:'var(--compose-text)'},
  '.cm-lintRange-error':{backgroundImage:'none',borderBottom:'2px wavy var(--compose-bad)'},
  '.cm-lintRange-warning':{backgroundImage:'none',borderBottom:'2px wavy var(--compose-warn)'},
  '.cm-tooltip':{border:'1px solid var(--compose-line)',borderRadius:'3px',
    background:'var(--compose-paper)',color:'var(--compose-text)',boxShadow:'var(--compose-shadow-sm)'},
  '.cm-tooltip-lint':{padding:'4px 8px',fontSize:'var(--fs-11)'},
  '.cm-diagnostic':{padding:'3px 0',borderLeft:'0'},
})

/* 标签表来自 lezer 的 yaml 语法：键是 `definition(propertyName)`，**普通值走 `content`**
   （不是 `string`，只有引号里的才是 string）。所以值不单独上色 —— 让它保持正文色，
   一屏 YAML 里该跳出来的是键。 */
const highlight=HighlightStyle.define([
  {tag:tags.definition(tags.propertyName),color:'var(--compose-yaml-key)',fontWeight:'650'},
  {tag:tags.string,color:'var(--compose-yaml-string)'},
  {tag:tags.special(tags.string),color:'var(--compose-yaml-string)',fontStyle:'italic'},
  {tag:tags.lineComment,color:'var(--compose-yaml-comment)',fontStyle:'italic'},
  {tag:[tags.labelName,tags.typeName,tags.keyword],color:'var(--compose-yaml-ref)'},
  {tag:[tags.separator,tags.punctuation,tags.squareBracket,tags.brace],color:'var(--compose-yaml-mark)'},
])

function extensions(){
  const list=[
    lineNumbers(),highlightActiveLineGutter(),highlightSpecialChars(),history(),
    drawSelection(),dropCursor(),indentOnInput(),bracketMatching(),
    highlightActiveLine(),
    // compose 的缩进就是两个空格（官方示例全是），tabSize 跟着它，不然行号槽里数不出来。
    indentUnit.of('  '),EditorState.tabSize.of(2),
    yaml(),theme,syntaxHighlighting(highlight),EditorView.lineWrapping,
    keymap.of([...defaultKeymap,...historyKeymap,...lintKeymap,indentWithTab]),
    EditorView.updateListener.of(update=>{
      if(update.docChanged){
        const value=update.state.doc.toString()
        if(value!==props.modelValue)emit('update:modelValue',value)
      }
      collectProblems(update.state)
    }),
  ]
  if(props.placeholder)list.push(placeholderExtension(props.placeholder))
  if(typeof props.checkSource==='function')list.push(lintGutter(),linter(lintSource,{delay:400}))
  return list
}

onMounted(()=>{
  const instance=new EditorView({
    state:EditorState.create({doc:props.modelValue||'',extensions:extensions()}),
    parent:host.value,
  })
  view.value=instance
  // 打开一个**本来就有错**的项目时立刻检查一次：否则要等到用户敲第一个键才出现波浪线，
  // 而那时他已经在改了。
  if((props.modelValue||'').trim())forceLinting(instance)
})

onBeforeUnmount(()=>{
  view.value?.destroy()
  view.value=null
})

/* 父子同步的闭环，两句都必须在（同 PyCodeEditor）：
   - updateListener 只在 doc 真变了时 emit（否则父组件一改值就回环）；
   - 这里先比一次文本，相同就不 dispatch（否则「父改 → 子 dispatch → 子 emit → 父改」死循环）。 */
watch(()=>props.modelValue,value=>{
  const instance=view.value
  if(!instance)return
  const text=value??''
  if(instance.state.doc.toString()===text)return
  instance.dispatch({changes:{from:0,to:instance.state.doc.length,insert:text}})
})
</script>

<template>
  <div class="compose-yaml">
    <div ref="host" class="compose-yaml-host"></div>
    <p v-if="problems.length" class="compose-yaml-problems">
      <span v-for="(item,at) in problems" :key="at" :class="item.severity">
        第 {{item.line}} 行 · {{item.message}}
      </span>
    </p>
  </div>
</template>

<style scoped>
.compose-yaml{display:flex;min-width:0;min-height:0;flex:1;flex-direction:column}
/* 390px 下不横向溢出的前提：这一层必须能被压缩。 */
.compose-yaml-host{position:relative;min-width:0;min-height:0;flex:1}
.compose-yaml :deep(.cm-editor){height:100%;border:1px solid var(--compose-line);border-radius:3px;
  background:var(--compose-field);font:var(--fs-13)/1.7 ui-monospace,SFMono-Regular,Menlo,monospace}
.compose-yaml :deep(.cm-editor.cm-focused){outline:0;border-color:#356c76;box-shadow:0 0 0 3px #83d9e766}
.compose-yaml-problems{margin:8px 0 0;font-size:var(--fs-11);line-height:1.7}
/* 一条一行：一条里带行号和整句话，中间不给任何可点区域（点这里没有动作，看着像按钮是骗人）。 */
.compose-yaml-problems span{display:block;overflow:hidden;color:var(--compose-bad);text-overflow:ellipsis;white-space:nowrap}
.compose-yaml-problems span.warning{color:var(--compose-warn)}
/* 暗色下弹窗不在 .ui-shell 里，拿不到全局那条青色聚焦描边（同 DockerComposeManager 的处理）。 */
html[data-theme=dark] .compose-yaml :deep(.cm-editor.cm-focused){border-color:var(--compose-cyan);box-shadow:0 0 0 3px #83d9e759}
</style>
