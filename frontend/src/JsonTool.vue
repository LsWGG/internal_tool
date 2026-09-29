<script setup>
import {computed, nextTick, onBeforeUnmount, ref, watch} from 'vue'
import JsonDiffEditor from './json-tool/JsonDiffEditor.vue'

const props=defineProps({aiPatch:{type:Object,default:null}})
const source = ref(''), comparison = ref(''), mode = ref('formatted')
const indent = ref(2), sorted = ref(false), format = ref('yaml')
const queryInput = ref('$'), query = ref('$')
const result = ref(null), error = ref(''), busy = ref(false), notice = ref('')
const expanded = ref(new Set([''])), fileName = ref('未选择文件'), rightName = ref('对照 JSON')
const fileInput = ref(null), rightFileInput = ref(null), wrap = ref(false)
const views = [{id:'formatted',name:'格式化'},{id:'tree',name:'树形'},{id:'table',name:'表格'},{id:'query',name:'JSONPath'},{id:'convert',name:'格式转换'},{id:'diff',name:'差异对比'}]
const sample = {project:'JSON 工作台',version:1,active:true,store:{books:[{id:1,title:'Vue 实践',price:28,tags:['前端','Vue']},{id:2,title:'数据处理',price:45,tags:['JSON','数据']},{id:3,title:'Web 入门',price:19,tags:[]}]},note:null}
let worker, debounce, deadline, noticeTimer, revision = 0, fileRevision = {left:0,right:0}
const leftEditor = ref(null), rightEditor = ref(null), activeDiff = ref(-1)
const diffReady = computed(() => !busy.value && !error.value && !result.value?.actionError && result.value?.diff)
const leftRanges = computed(() => diffReady.value ? result.value.diff.leftRanges : [])
const rightRanges = computed(() => diffReady.value ? result.value.diff.rightRanges : [])
watch(() => result.value?.diff, () => {activeDiff.value=-1})
async function goToDiff(index) {
  const count = diffReady.value?.rows.length || 0
  if (!count) return
  activeDiff.value=(index+count)%count
  await nextTick()
  leftEditor.value?.reveal(activeDiff.value)
  rightEditor.value?.reveal(activeDiff.value)
}
const lines = computed(() => source.value.split('\n').length)
const visibleTree = computed(() => (result.value?.rows || []).filter(row => row.ancestors.every(parent => expanded.value.has(parent))))
const output = computed(() => result.value?.output ?? '')
const canExport = computed(() => !busy.value && !error.value && !result.value?.actionError && result.value && typeof result.value.output === 'string')
const differenceCounts = computed(() => (result.value?.diff?.rows || []).reduce((counts,row) => {counts[row.type]++;return counts}, {added:0,removed:0,changed:0}))
const extension = computed(() => mode.value==='convert' ? ({minify:'json',yaml:'yaml',csv:'csv',jsonl:'jsonl',json:'json'}[format.value]) : 'json')
const status = computed(() => busy.value ? '正在处理…' : error.value ? '校验未通过' : result.value ? `有效 JSON · ${result.value.type} · ${result.value.nodes.toLocaleString()} 个节点` : '等待输入')

function stop() { clearTimeout(deadline); worker?.terminate(); worker = null }
function refresh(transform) {
  clearTimeout(debounce); stop(); const id = ++revision
  error.value = ''; busy.value = false
  if (!source.value.trim()) { result.value=null; return }
  if (source.value.length > 5*1024*1024) {error.value='单份 JSON 最大支持 5 MB。'; return}
  busy.value = true
  worker = new Worker(new URL('./json-tool/worker.js', import.meta.url), {type:'module'})
  const fail = message => {if(id !== revision)return;stop();busy.value=false;result.value=null;error.value=message}
  worker.onerror = () => fail('处理线程异常，请检查数据或缩小文件后重试。')
  worker.onmessage = ({data}) => {
    if (id !== revision) return
    stop();busy.value=false
    if (data.error) {result.value=null;error.value=data.error;return}
    if (transform) {
      if(data.result.actionError){error.value=data.result.actionError;return}
      source.value=data.result.output; notice.value=transform==='minify'?'已压缩左栏 JSON':'已格式化左栏 JSON'
      // Recompute even when the formatted source already matches the input.
      refresh()
    } else result.value=data.result
  }
  deadline = setTimeout(() => fail('处理超过 8 秒已停止。请缩小数据范围或简化 JSONPath。'), 8000)
  worker.postMessage({id,text:source.value,right:comparison.value,mode:transform?'convert':mode.value,format:transform||format.value,path:query.value,indent:Number(indent.value),sorted:sorted.value})
}
watch([source,comparison,mode,indent,sorted,format,query], (values, previous) => {
  stop();revision++;if(values[2]!==previous[2]||!source.value.trim())result.value=null;error.value='';busy.value=!!source.value.trim();clearTimeout(debounce)
  debounce=setTimeout(()=>refresh(),250)
})
watch(()=>props.aiPatch, patch=>{
  if(!patch)return
  if(typeof patch.text==='string')source.value=patch.text
  if(typeof patch.comparison==='string')comparison.value=patch.comparison
  if(views.some(view=>view.id===patch.mode))mode.value=patch.mode
  if(['yaml','csv','jsonl','minify','json'].includes(patch.format))format.value=patch.format
  if(typeof patch.path==='string'){queryInput.value=patch.path;query.value=patch.path}
  fileName.value='AI 输入.json'
  refresh()
},{immediate:true})
watch(notice, value=>{clearTimeout(noticeTimer);if(value)noticeTimer=setTimeout(()=>{notice.value=''},6000)})
onBeforeUnmount(()=>{clearTimeout(debounce);clearTimeout(noticeTimer);stop();fileRevision.left++;fileRevision.right++})
function example(){source.value=JSON.stringify(sample,null,2);fileName.value='示例.json';notice.value='已载入示例，可尝试树形浏览、路径查询或差异对比。'}
function clearSource(){source.value='';fileName.value='未选择文件';notice.value=''}
function toggle(pointer){const next=new Set(expanded.value);next.has(pointer)?next.delete(pointer):next.add(pointer);expanded.value=next}
function expandAll(){expanded.value=new Set((result.value?.rows||[]).filter(row=>row.branch).map(row=>row.pointer))}
function runQuery(path){query.value=path??queryInput.value;queryInput.value=query.value;refresh()}
async function upload(event, side='left'){
  const file=event.target.files?.[0];event.target.value='';if(!file)return
  const id=++fileRevision[side]
  if(file.size>5*1024*1024){notice.value='文件超过 5 MB，请拆分后再上传。';return}
  try{const text=await file.text();if(id!==fileRevision[side])return
    if(side==='left'){source.value=text;fileName.value=file.name}else{comparison.value=text;rightName.value=file.name}
    notice.value=`已读取 ${file.name}，内容仅在浏览器本地处理。`
  }catch{notice.value='文件读取失败，请重试。'}
}
async function copy(text=output.value){try{await navigator.clipboard.writeText(text);notice.value='已复制到剪贴板。'}catch{notice.value='无法访问剪贴板，请选中文本后手动复制。'}}
function download(){
  if(!canExport.value)return
  const blob=new Blob([extension.value==='csv'?'\uFEFF':'',output.value],{type:'text/plain;charset=utf-8'})
  const url=URL.createObjectURL(blob),a=document.createElement('a')
  a.href=url;a.download=`${fileName.value==='未选择文件'?'result':fileName.value.replace(/\.[^.]+$/,'')}-${mode.value}.${extension.value}`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),1000)
}
function useSourceAsComparison(){comparison.value=source.value;rightName.value='左栏副本';notice.value='已复制左栏到对照区，可以编辑任意一侧查看差异。'}
</script>

<template>
  <section class="json-page">
    <header><div><span class="eyebrow">JSON WORKBENCH</span><h1>JSON 解析工作台</h1><p>从原始文本到清晰结构，格式化、查询、转换与双栏对比。</p></div><span class="json-local">本地处理 · 不上传服务器</span></header>
    <div class="json-studio">
    <div class="json-commandbar">
      <div class="json-actions"><button class="json-primary" @click="fileInput.click()">＋ 上传 JSON</button><button @click="example">载入示例</button></div>
      <div class="json-options"><label>缩进 <select v-model="indent" aria-label="JSON 缩进"><option :value="2">2 空格</option><option :value="4">4 空格</option></select></label><label><input v-model="sorted" type="checkbox">按键排序</label><label><input v-model="wrap" type="checkbox">自动换行</label></div>
      <input ref="fileInput" class="json-file" type="file" accept=".json,.txt,application/json" aria-label="上传左栏 JSON 文件" @change="upload($event)">
      <input ref="rightFileInput" class="json-file" type="file" accept=".json,.txt,application/json" aria-label="上传对照 JSON 文件" @change="upload($event,'right')">
    </div>
    <nav class="json-views" aria-label="JSON 结果视图"><button v-for="view in views" :key="view.id" :aria-pressed="mode===view.id" :class="{active:mode===view.id}" @click="mode=view.id">{{view.name}}</button></nav>
    <div class="json-context">
        <div v-if="mode==='query'" class="json-query"><form @submit.prevent="runQuery()"><input v-model="queryInput" aria-label="JSONPath 表达式" spellcheck="false" placeholder="$.store.books[*].title"><button class="json-primary" :disabled="!source.trim()||busy">查询</button></form><div class="json-query-examples"><button @click="runQuery('$')">根节点 $</button><button @click="runQuery('$..title')">递归查找标题</button><button @click="runQuery('$.store.books[?(@.price < 30)]')">价格筛选示例</button></div><small>支持点 / 括号路径、通配符、递归、切片与筛选表达式。</small></div>
        <div v-else-if="mode==='convert'" class="json-convert"><label>目标格式 <select v-model="format" aria-label="转换格式"><option value="yaml">YAML</option><option value="csv">CSV</option><option value="jsonl">JSON Lines</option><option value="minify">压缩 JSON</option><option value="json">格式化 JSON</option></select></label><small v-if="format==='csv'">要求对象数组；嵌套值转为 JSON 字符串，公式前缀加单引号以便安全打开。</small><small v-else-if="format==='jsonl'">要求根节点为数组，每个元素输出一行。</small><small v-else>转换后可复制或下载完整结果。</small></div>
      <div v-else-if="mode==='tree'" class="json-tree-toolbar"><span class="json-context-label">树形浏览</span><button :disabled="!result||busy" @click="expandAll">全部展开</button><button :disabled="!result||busy" @click="expanded=new Set([''])">折叠子节点</button><small>点击节点展开，点击「路径」复制 JSON Pointer</small></div>
      <div v-else-if="mode==='diff'" class="json-diff-tools"><span class="json-context-label">结构对比</span><button @click="rightFileInput.click()">上传对照文件</button><button :disabled="!source" @click="useSourceAsComparison">从左栏复制</button><small>内容高亮：绿 + 新增 · 红 − 删除 · 黄 ~ 修改</small><div class="json-diff-overview" role="status" aria-live="polite">
        <span v-if="busy" class="json-diff-state">正在对比…</span>
        <span v-else-if="error||result?.actionError" class="json-diff-state invalid">! {{error?'原始 JSON 无效':'请检查对照 JSON'}}</span>
        <template v-else-if="result?.diff"><b>{{result.diff.rows.length ? (result.diff.truncated?'发现 1,000+ 处差异':'发现 '+result.diff.rows.length+' 处差异') : '✓ 两份 JSON 一致'}}</b><div class="json-diff-summary"><span class="added">＋ 新增 {{differenceCounts.added}}</span><span class="removed">− 删除 {{differenceCounts.removed}}</span><span class="changed">~ 修改 {{differenceCounts.changed}}</span></div><small v-if="result.diff.truncated">计数为前 1,000 处差异</small><div v-if="result.diff.rows.length" class="json-diff-navigation"><button @click="goToDiff(activeDiff<0 ? result.diff.rows.length-1 : activeDiff-1)" aria-label="上一处差异">↑ 上一处</button><span>{{activeDiff<0 ? '—' : activeDiff+1}} / {{result.diff.rows.length}}</span><button @click="goToDiff(activeDiff+1)" aria-label="下一处差异">↓ 下一处</button></div></template>
        <span v-else class="json-diff-state">等待输入两份 JSON</span>
      </div></div>
      <div v-else class="json-context-hint"><span class="json-context-label">{{mode==='table'?'表格浏览':'格式化预览'}}</span><span>{{mode==='table'?'自动展开对象数组，嵌套内容保留为 JSON':'编辑左栏，右栏实时更新；原文保持不变'}}</span></div>
    </div>
    <div v-if="notice" class="json-notice" role="status">{{notice}}<button aria-label="关闭提示" @click="notice=''">×</button></div>
    <div class="json-workspace">
      <section class="json-pane json-source"><div class="json-pane-head"><div><span class="json-step">01</span><b>{{mode==='diff'?'原始 JSON · A':'原始 JSON'}}</b><small :title="fileName">{{fileName}}</small></div><div class="json-actions"><button :disabled="!source.trim()||busy" @click="refresh('json')" title="格式化左栏原文">美化</button><button :disabled="!source.trim()||busy" @click="refresh('minify')">压缩</button><button :disabled="!source" @click="clearSource">清空</button></div></div>
        <JsonDiffEditor v-if="mode==='diff'" ref="leftEditor" v-model="source" :ranges="leftRanges" :active-index="activeDiff" :wrap="wrap" label="原始 JSON" placeholder="粘贴原始 JSON，差异会直接在内容中标出…" />
        <textarea v-else v-model="source" aria-label="原始 JSON" spellcheck="false" :wrap="wrap?'soft':'off'" placeholder='在此粘贴 JSON，或上传 .json 文件…&#10;&#10;{&#10;  "message": "Hello, JSON",&#10;  "items": [1, 2, 3]&#10;}'></textarea>
        <div class="json-source-foot"><span>{{lines.toLocaleString()}} 行 · {{source.length.toLocaleString()}} 字符</span><span>UTF-8 · 最大 5 MB</span></div>
      </section>
      <section class="json-pane json-result" :aria-busy="busy"><div class="json-pane-head"><div><span class="json-step">02</span><b>{{mode==='diff'?'对照 JSON · B':mode==='query'?'查询结果':mode==='convert'?format.toUpperCase()+' 结果':'解析结果'}}</b></div><div v-if="mode!=='diff'" class="json-actions"><button :disabled="!canExport" @click="copy()">复制</button><button :disabled="!canExport" @click="download">下载</button></div><small v-else :title="rightName">{{rightName}}</small></div>
        <template v-if="mode==='diff'"><JsonDiffEditor ref="rightEditor" v-model="comparison" :ranges="rightRanges" :active-index="activeDiff" :wrap="wrap" label="对照 JSON" placeholder="粘贴第二份 JSON，或从上方上传对照文件…" /><div class="json-source-foot"><span>{{comparison.split('\n').length.toLocaleString()}} 行 · {{comparison.length.toLocaleString()}} 字符</span><span>{{busy?'正在对比…':diffReady?(result.diff.rows.length?'差异已在内容中高亮':'两份内容一致'):'等待有效 JSON'}}</span></div></template>
        <template v-else>
        <div v-if="error" class="json-error" role="alert"><b>请检查原始 JSON</b><p>{{error}}</p><small>仅接受标准 JSON：键名使用双引号，不支持注释或尾随逗号；重复键会报错。</small></div>
        <div v-else-if="result?.actionError" class="json-error" role="alert"><b>{{mode==='query'?'检查查询表达式':mode==='diff'?'检查对照内容':'无法转换'}}</b><p>{{result.actionError}}</p></div>
        <div v-else-if="busy && !result" class="json-empty"><span class="json-symbol">{ }</span><b>正在解析数据</b><p>复杂查询最多运行 8 秒，可随时修改输入。</p></div>
        <div v-else-if="!result" class="json-empty"><span class="json-symbol">{ }</span><b>把复杂数据看清楚</b><p>左栏粘贴 JSON 或上传文件，即可在这里查看结果。</p><button @click="example">用示例试一试 ↗</button></div>
        <template v-else>
          <template v-if="mode==='tree'"><p v-if="result.truncated" class="json-limit">树形视图仅显示前 5,000 个节点，请通过 JSONPath 缩小范围。</p><div class="json-tree"><div v-for="row in visibleTree" :key="row.pointer" class="json-tree-row" :style="{paddingLeft:`${12+Math.min(row.depth,25)*16}px`}"><button v-if="row.branch" class="json-toggle" :aria-label="`${expanded.has(row.pointer)?'折叠':'展开'} ${row.pointer||'根节点'}`" :aria-expanded="expanded.has(row.pointer)" @click="toggle(row.pointer)">{{expanded.has(row.pointer)?'▾':'▸'}}</button><span v-else class="json-leaf">·</span><b>{{row.key}}</b><span class="json-type" :class="row.type">{{row.type}}</span><span class="json-tree-value">{{row.summary}}</span><button class="json-path-copy" :aria-label="`复制路径 ${row.pointer||'根节点'}`" @click="copy(row.pointer)">路径</button></div></div></template>
          <template v-else-if="mode==='table'"><p v-if="result.truncated" class="json-limit">表格仅预览前 200 行、40 列；下载请使用格式转换。</p><div class="json-table-wrap"><table><thead><tr><th v-for="(column,i) in result.columns" :key="i">{{column}}</th></tr></thead><tbody><tr v-for="(row,i) in result.rows" :key="i"><td v-for="(cell,j) in row" :key="j">{{cell}}</td></tr></tbody></table><p v-if="!result.rows.length" class="json-limit">空数组 / 对象，没有可显示的数据。</p></div></template>
          <template v-else-if="mode==='query'"><div class="json-query-count">匹配 {{result.query.count.toLocaleString()}} 项 <small v-if="result.query.truncated">仅显示并导出前 500 项</small></div><div class="json-query-results"><article v-for="(row,i) in result.query.rows" :key="i"><code>{{row.path}}</code><pre>{{JSON.stringify(row.value,null,Number(indent))}}</pre></article><p v-if="!result.query.count" class="json-limit">没有匹配结果，请检查路径或筛选条件。</p></div></template>
          <textarea v-else class="json-output" aria-label="JSON 处理结果" :value="output" readonly spellcheck="false" :wrap="wrap?'soft':'off'"></textarea>
        </template>
        </template>
        <div v-if="mode!=='diff'" class="json-status" :class="{invalid:error,valid:result&&!busy}" role="status"><span>{{busy?'○':error?'!':result?'✓':'○'}}</span>{{status}}</div>
      </section>
    </div>
    <section v-if="mode==='diff'" class="json-diff-report" :aria-busy="busy">
      <div class="json-report-head"><div><b>差异结果</b><small>{{busy?'正在对比…':'JSON Pointer 路径 · 新增 / 删除 / 修改'}}</small></div><div class="json-actions"><button :disabled="!canExport" @click="copy()">复制差异</button><button :disabled="!canExport" @click="download">下载差异</button></div></div>
      <div v-if="error||result?.actionError" class="json-error" role="alert">{{error||result.actionError}}</div>
          <template v-else-if="result?.diff"><div class="json-diff-summary"><span class="added">＋ 新增 {{differenceCounts.added}}</span><span class="removed">− 删除 {{differenceCounts.removed}}</span><span class="changed">~ 修改 {{differenceCounts.changed}}</span></div><p v-if="result.diff.truncated" class="json-limit">仅显示前 1,000 处差异。</p><div class="json-table-wrap"><table v-if="result.diff.rows.length" class="json-diff-table"><thead><tr><th>路径</th><th>左栏原值</th><th>右栏新值</th></tr></thead><tbody><tr v-for="(row,i) in result.diff.rows" :key="i" :class="row.type"><td><b>{{{added:'新增',removed:'删除',changed:'修改'}[row.type]}}</b><button class="json-diff-path" :aria-label="`定位差异 ${row.path}`" @click="goToDiff(i)">{{row.path}}</button></td><td>{{row.before}}</td><td>{{row.after}}</td></tr></tbody></table><div v-else class="json-equal">✓ 两份 JSON 的结构和值一致</div></div></template>
      <p v-else class="json-diff-placeholder">{{busy?'正在比较两份 JSON…':'在上方两栏输入 JSON，结构差异会显示在这里。'}}</p>
    </section>
    </div>
    <p class="json-footnote">严格校验重复键和数字精度。超出安全精度的数字请使用字符串；不会自动改写原文。</p>
  </section>
</template>

<style scoped>
.json-page{color:var(--pop-ink);--json-line:#c3c1b6;--json-muted:#65655d;font-size:var(--fs-12)}
.json-page header{display:flex;justify-content:space-between;align-items:center;gap:20px}.json-local{flex:none;padding:9px 12px;border:1px solid var(--pop-ink);background:#e3f7f9;font-size:var(--fs-11)}
.json-page button,.json-page select{font:inherit;color:var(--pop-ink);border:1px solid var(--json-line);border-radius:3px;background:#fffef9;padding:8px 11px;min-height:34px;cursor:pointer}.json-page button:hover:not(:disabled){background:#e3f7f9;border-color:var(--pop-ink)}.json-page button:disabled{color:#77776f;background:#eeece4;cursor:not-allowed}.json-page .json-primary{background:var(--pop-yellow);border-color:var(--pop-ink);font-weight:750;box-shadow:2px 2px var(--pop-ink)}
.json-commandbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px;padding:16px 0 20px}.json-actions,.json-options{display:flex;gap:9px;align-items:center;flex-wrap:wrap}.json-options label{display:flex;align-items:center;gap:7px;white-space:nowrap}.json-options input{accent-color:var(--pop-ink)}.json-file{display:none}
.json-notice{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px;padding:10px 14px;background:#fff5c0;border:1px solid #beaf68;line-height:1.7}.json-notice button{border:0;background:transparent;font-size:18px}
.json-workspace{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px;align-items:start}.json-pane{display:flex;flex-direction:column;min-width:0;height:clamp(580px,72vh,940px);border:2px solid var(--pop-ink);border-radius:3px;box-shadow:4px 4px var(--pop-ink);background:#fffef9;overflow:hidden}.json-pane-head{display:flex;justify-content:space-between;align-items:center;gap:12px;padding:14px 16px;border-bottom:1px solid var(--json-line);background:#e1f6f9;flex:none}.json-pane-head>div{display:flex;align-items:center;gap:9px;min-width:0}.json-pane-head small{color:var(--json-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px}.json-step{display:grid;place-items:center;width:26px;height:26px;border:1px solid var(--pop-ink);background:var(--pop-yellow);font-weight:800;flex:none}.json-pane-head b{white-space:nowrap;font-size:var(--fs-13)}
.json-source>textarea,.json-output{flex:1;min-height:0;width:100%;border:0;resize:none;background:#fffef9;color:#253436;padding:18px;font:13px/1.8 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;tab-size:2;outline-offset:-3px!important}.json-source>textarea::placeholder{color:#8c8c81}.json-source-foot{display:flex;justify-content:space-between;gap:12px;padding:11px 16px;border-top:1px solid var(--json-line);font-size:var(--fs-10);color:var(--json-muted)}
.json-views{display:flex;gap:8px;flex-wrap:wrap;padding:6px;margin:0 0 18px;border:1px solid var(--pop-ink);border-radius:3px;background:var(--pop-soft)}.json-views button{padding:9px 16px;font-size:var(--fs-12);border-color:transparent;background:transparent}.json-views button.active,.json-views button.active:hover{background:var(--pop-yellow);border-color:var(--pop-ink);font-weight:750}.json-status{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #e1dfd4;font-size:var(--fs-11);color:var(--json-muted);flex:none}.json-status.valid{color:#286445}.json-status.invalid{color:#a22f42;background:#fff1f3}
.json-error{padding:20px;color:#a22f42;overflow:auto;background:#fff5f6;line-height:1.8}.json-error p{color:inherit;margin:10px 0;overflow-wrap:anywhere}.json-error small{color:#72555a}.json-empty{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:28px;text-align:center;gap:16px;background:var(--pop-soft)}.json-empty b{font-size:var(--fs-18)}.json-empty p{line-height:1.8;color:var(--json-muted);max-width:290px}.json-symbol{padding:15px 20px;font:800 30px ui-monospace,monospace;background:var(--pop-cyan);border:2px solid var(--pop-ink);box-shadow:3px 3px var(--pop-ink)}
.json-query,.json-convert{padding:14px;border-bottom:1px solid var(--json-line);display:grid;gap:10px;flex:none}.json-query form{display:flex;gap:9px}.json-query input{width:100%;min-width:0;border:1px solid var(--json-line);padding:9px;font:12px ui-monospace,monospace;border-radius:3px}.json-query-examples{display:flex;gap:6px;flex-wrap:wrap}.json-query-examples button{font-size:var(--fs-10);min-height:28px;padding:4px 7px}.json-query small,.json-convert small{color:var(--json-muted);line-height:1.6}.json-convert label{display:flex;align-items:center;gap:10px}
.json-tree-toolbar{display:flex;gap:8px;padding:10px 12px;align-items:center;flex-wrap:wrap}.json-tree-toolbar small{color:var(--json-muted);font-size:var(--fs-10)}.json-tree{flex:1;min-height:0;overflow:auto;padding:8px 0}.json-tree-row{display:flex;align-items:center;gap:8px;min-height:35px;white-space:nowrap;padding-right:12px;font:12px/1.6 ui-monospace,monospace}.json-tree-row:hover{background:#f4f7ec}.json-tree-row .json-toggle{min-height:24px;width:24px;padding:0;flex:none}.json-leaf{display:inline-block;width:24px;text-align:center;flex:none;color:#999}.json-tree-value{max-width:300px;overflow:hidden;text-overflow:ellipsis;color:#42655d}.json-type{font-size:10px;background:#efede4;border:1px solid #dedcd2;padding:0 4px;color:#69695e}.json-type.string{background:#e6f5e9;color:#37684a}.json-type.number{background:#fff3be;color:#705607}.json-tree-row .json-path-copy{margin-left:auto;min-height:24px;font-size:10px;padding:2px 5px;flex:none}
.json-table-wrap,.json-query-results{flex:1;min-height:0;overflow:auto}.json-page table{border-collapse:collapse;min-width:100%;font-size:var(--fs-12)}.json-page th{position:sticky;top:0;background:#fff3bb;z-index:1;text-align:left}.json-page th,.json-page td{padding:12px;border-bottom:1px solid #dddcd3;border-right:1px solid #eeece4;vertical-align:top;min-width:120px;max-width:300px;overflow-wrap:anywhere}.json-page td{white-space:pre-wrap;font-family:ui-monospace,monospace}.json-page tbody tr:hover{background:#f0f8f7}.json-query-count,.json-diff-summary{padding:10px 14px;border-bottom:1px solid var(--json-line);display:flex;gap:12px;flex-wrap:wrap}.json-query-count small{color:var(--json-muted)}.json-query-results article{padding:14px;border-bottom:1px solid var(--json-line)}.json-query-results article>code{display:block;color:#576659;font-size:11px;overflow-wrap:anywhere;margin-bottom:9px}.json-query-results pre{margin:0;font:12px/1.7 ui-monospace,monospace;white-space:pre-wrap;overflow-wrap:anywhere}.json-limit{padding:12px 14px;background:#fff7d9;color:#776438;font-size:var(--fs-11);line-height:1.7}.json-diff-table code{display:block;margin-top:6px}.json-diff-table td:first-child{font-size:11px}.added{color:#256747;background:#edf8ef}.removed{color:#a22f42;background:#fff0f3}.changed{color:#7b5b12;background:#fff6d7}.json-diff-summary span{padding:4px 7px;border:1px solid currentColor;border-radius:2px}.json-equal{padding:30px;color:#286445;text-align:center}.json-footnote{margin:18px 0;color:var(--json-muted);font-size:var(--fs-11);line-height:1.8}
@media(min-width:1800px){.json-source>textarea,.json-output{font-size:var(--fs-13)}}
@media(max-width:1000px){.json-local{display:none}.json-pane-head{padding:12px}.json-pane-head small{display:none}.json-pane-head .json-actions{gap:5px}.json-pane-head button{font-size:11px;padding:6px 8px}.json-workspace{gap:14px}}
@media(max-width:760px){.json-workspace{grid-template-columns:minmax(0,1fr)}.json-source{height:400px}.json-result{height:660px}.json-commandbar{padding-top:4px}.json-options{gap:12px}.json-pane-head{flex-wrap:wrap}.json-tree-row{font-size:11px}.json-source-foot{font-size:10px}.json-page th,.json-page td{min-width:100px}}
/* Keep generic form rules from turning compact toolbar controls into fields. */
#app .json-page .json-options label{display:flex;flex-direction:row;align-items:center;gap:7px;margin:0;padding:0;font-size:var(--fs-11);color:var(--json-muted)}
#app .json-page .json-options input[type=checkbox]{width:14px;height:14px;min-height:0;margin:0;padding:0}
#app .json-page .json-options select{margin:0;width:auto;min-height:34px}
#app .json-page button{white-space:nowrap}
#app .json-page .json-query form>button{flex:none}
#app .json-page .json-query input{margin:0;min-height:38px}
#app .json-page .json-convert label{margin:0;padding:0;flex-direction:row}
#app .json-page .json-convert select{margin:0;width:auto}
/* One continuous studio keeps controls and content aligned across views. */
.json-studio{border:2px solid var(--pop-ink);border-radius:4px;box-shadow:4px 4px var(--pop-ink);background:var(--pop-paper);overflow:hidden}
.json-commandbar{padding:14px 18px;gap:12px;border-bottom:1px solid #dedcd2;background:#fffef9}
.json-views{border:0;border-radius:0;margin:0;padding:8px 14px;gap:6px;border-bottom:1px solid var(--json-line);background:var(--pop-soft)}
.json-views button{padding:8px 15px;min-height:36px}
.json-context{min-height:68px;padding:12px 18px;display:flex;align-items:center;border-bottom:1px solid var(--json-line);background:#fffef9}
.json-context-label{color:var(--pop-ink);font-size:var(--fs-12);font-weight:750;white-space:nowrap}
.json-context-hint,.json-diff-tools{display:flex;align-items:center;gap:12px;flex-wrap:wrap;color:var(--json-muted);font-size:var(--fs-11)}
.json-diff-tools{width:100%}.json-diff-tools small{font-size:var(--fs-11)}
.json-diff-overview{width:100%;display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-top:1px solid #dedcd2;padding-top:12px;min-height:40px;color:var(--pop-ink)}
.json-diff-overview .json-diff-summary{padding:0;gap:8px;border-bottom:0}.json-diff-navigation{display:flex;align-items:center;gap:8px;margin-left:auto}.json-diff-navigation span{font-variant-numeric:tabular-nums;color:var(--json-muted)}.json-page .json-diff-path{display:block;margin-top:6px;padding:0;border:0;background:none;min-height:24px;text-align:left;white-space:normal;overflow-wrap:anywhere;text-decoration:underline}.json-diff-state{color:var(--json-muted)}.json-diff-state.invalid{color:#a32848}
.json-context .json-query{width:100%;padding:0;border:0;display:grid;grid-template-columns:minmax(180px,1fr) auto;gap:12px;align-items:center}
.json-context .json-query>small{display:none}
.json-context .json-query-examples{gap:6px}.json-context .json-query-examples button{background:var(--pop-soft);border-color:transparent}
.json-context .json-convert{width:100%;padding:0;border:0;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.json-context .json-convert small{flex:1;min-width:180px}
.json-context .json-tree-toolbar{padding:0;gap:10px}
.json-workspace{gap:0;align-items:stretch}
.json-pane{height:clamp(500px,65vh,850px);border:0;border-radius:0;box-shadow:none}
.json-source{border-right:1px solid var(--json-line)}
.json-pane-head{min-height:62px;padding:12px 16px;background:#edf7f7;gap:8px}
.json-pane-head .json-actions{gap:5px;flex-wrap:nowrap}
.json-pane-head .json-actions button{padding:6px 9px;min-height:32px;font-size:var(--fs-11);background:#fffef9}
.json-pane-head>div:first-child{flex:1;overflow:hidden}
.json-pane-head small{max-width:120px;font-size:var(--fs-10)}
.json-step{width:23px;height:23px;font-size:var(--fs-10);background:#fffef9;border-color:#9aaba8;color:#4e6661}
.json-source-foot,.json-status{min-height:38px;padding:10px 14px;border-top:1px solid #dedcd2;border-bottom:0;background:#f8f7f1;font-size:var(--fs-10);flex:none}
.json-notice{position:fixed;z-index:1400;left:50%;bottom:24px;transform:translateX(-50%);width:max-content;max-width:min(700px,calc(100vw - 36px));margin:0;padding:10px 14px;border:1px solid var(--pop-ink);border-radius:3px;background:#fff5c0;box-shadow:3px 3px var(--pop-ink);font-size:var(--fs-12)}
.json-notice button{min-height:24px;padding:0 4px;flex:none}
.json-diff-report{border-top:2px solid var(--pop-ink);background:#fffef9}
.json-report-head{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 18px;background:#edf7f7;border-bottom:1px solid var(--json-line)}
.json-report-head>div:first-child{display:flex;align-items:center;gap:12px;flex-wrap:wrap}.json-report-head small{color:var(--json-muted);font-size:var(--fs-11)}
.json-diff-report .json-table-wrap{max-height:420px}
.json-diff-report .json-diff-summary{padding:12px 18px}
.json-diff-placeholder{padding:26px 18px;text-align:center;color:var(--json-muted);font-size:var(--fs-12)}
.json-empty{background:#f7f6ef}.json-symbol{font-size:26px;padding:12px 16px;box-shadow:2px 2px var(--pop-ink)}
@media(max-width:1100px){.json-context .json-query{grid-template-columns:minmax(0,1fr)}.json-pane-head small{display:none}.json-context{min-height:68px}}
@media(max-width:760px){
 .json-commandbar{padding:12px;gap:14px}.json-commandbar>.json-actions{width:100%}.json-options{gap:10px}
 .json-views{padding:8px;gap:4px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}.json-views button{padding:8px 4px;font-size:11px}
 .json-context{padding:12px;min-height:64px}.json-context-hint{gap:7px}.json-context-hint>span:last-child{width:100%;line-height:1.7}
 .json-context .json-query-examples{flex-wrap:wrap}.json-context .json-query-examples button{padding:5px 6px;font-size:10px}
 .json-context .json-convert{gap:8px}.json-context .json-convert small{width:100%;flex:auto}
 .json-pane{height:500px}.json-source{height:340px;border-right:0;border-bottom:1px solid var(--pop-ink)}
 .json-pane-head{padding:10px 12px;min-height:56px;flex-wrap:nowrap}.json-pane-head b{font-size:12px}.json-pane-head .json-actions button{padding:6px 7px;font-size:10px}
 .json-pane-head .json-step{display:none}.json-report-head{align-items:flex-start;padding:12px;flex-wrap:wrap}.json-report-head>div:first-child{gap:6px}
 .json-notice{bottom:72px;font-size:11px}.json-diff-tools{gap:8px}.json-diff-tools small{width:100%;line-height:1.7}
}
</style>
