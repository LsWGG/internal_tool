<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {buildOntologyGraph, canvasEdges, fitGraph, layoutOntology, parseRdfXml, placeEdges} from './ontologyRdf.js'

const KINDS = [
  ['ontology', '本体'], ['class', '类'], ['property', '属性'], ['restriction', '限制'],
  ['individual', '实例'], ['list', '列表'], ['resource', '外部资源'], ['literal', '字面量'],
]
const kindName = Object.fromEntries(KINDS)
const fileInput = ref(null)
const graphRef = ref(null)
const svgRef = ref(null)
const source = ref('')
const fileName = ref('未选择 RDF 文件')
const error = ref('')
const query = ref('')
const triples = ref([])
const nodes = ref([])
const edgeList = ref([])
const folded = ref(0)
const selected = ref(null)
const hovered = ref(null)
const showAnnotations = ref(false)
const showLiterals = ref(false)
const showMetaTypes = ref(false)
const panning = ref(false)
const movedByUser = ref(false)
const viewport = ref({w: 960, h: 640})
const view = ref({x: 40, y: 40, k: 1})
let resizeObserver = null
let stopPan = null

const summary = computed(() => ({
  class: nodes.value.filter(node => node.kind === 'class' || node.kind === 'ontology').length,
  property: nodes.value.filter(node => node.kind === 'property').length,
  individual: nodes.value.filter(node => node.kind === 'individual' || node.kind === 'restriction').length,
  edges: edgeList.value.length,
}))
const hint = computed(() => {
  if (!triples.value.length) return '导入 RDF/XML 后在这里探索'
  const keyword = query.value.trim()
  if (keyword) return `筛选“${keyword}”，回车定位`
  if (folded.value) return `自由拖拽节点 · 已收起 ${folded.value} 条标注、字面量或数据类型`
  return '自由拖拽节点 · 滚轮缩放 · 拖动画布'
})
const groups = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  const matched = nodes.value.filter(node => !keyword || `${node.label} ${node.uri} ${node.lexical}`.toLowerCase().includes(keyword))
  return KINDS.map(([kind, name]) => ({kind, name, items: matched.filter(node => node.kind === kind)})).filter(group => group.items.length)
})
const activeIds = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  if (!keyword && !selected.value) return null
  const ids = new Set()
  const matched = new Set()
  if (keyword) {
    for (const node of nodes.value) {
      if (`${node.label} ${node.uri} ${node.lexical}`.toLowerCase().includes(keyword)) matched.add(node.id)
    }
    for (const id of matched) ids.add(id)
    for (const edge of edgeList.value) {
      if (matched.has(edge.s)) ids.add(edge.o)
      if (matched.has(edge.o)) ids.add(edge.s)
    }
  }
  if (selected.value) {
    ids.add(selected.value.id)
    for (const edge of edgeList.value) {
      if (edge.s === selected.value.id) ids.add(edge.o)
      if (edge.o === selected.value.id) ids.add(edge.s)
    }
  }
  return ids
})
const drawnEdges = computed(() => placeEdges(nodes.value, canvasEdges(nodes.value, edgeList.value)))
const relations = computed(() => {
  const current = selected.value
  if (!current) return []
  const labels = new Map(nodes.value.map(node => [node.id, node.label]))
  const rows = []
  for (const triple of triples.value) {
    if (triple.s !== current.id && triple.o !== current.id) continue
    const incoming = triple.o === current.id && triple.s !== current.id
    const other = incoming ? triple.s : triple.o
    let text = labels.get(other) || triple.oLabel || other
    if (!triple.resource && triple.lang) text += `@${triple.lang}`
    rows.push({
      key: `${incoming ? 'in' : 'out'}\n${triple.pUri}\n${other}\n${triple.lang}\n${triple.o}`,
      dir: incoming ? 'in' : 'out', p: triple.pLabel, pUri: triple.pUri, other, text,
      linked: labels.has(other),
    })
  }
  return rows
})
const relationRows = computed(() => relations.value.slice(0, 160))

function clamp(value, min, max) {return Math.max(min, Math.min(max, value))}
function syncViewport() {
  const box = graphRef.value
  if (!box || box.clientWidth < 20 || box.clientHeight < 20) return
  viewport.value = {w: Math.round(box.clientWidth), h: Math.round(box.clientHeight)}
}
function frame() {
  syncViewport()
  view.value = fitGraph(nodes.value, viewport.value.w, viewport.value.h, placeEdges(nodes.value, canvasEdges(nodes.value, edgeList.value)))
}
function rebuild(refit) {
  const model = buildOntologyGraph(triples.value, {annotations: showAnnotations.value, literals: showLiterals.value, metaTypes: showMetaTypes.value})
  const laid = layoutOntology(model.nodes, model.edges)
  nodes.value = laid
  edgeList.value = model.edges
  folded.value = model.folded
  const kept = selected.value && laid.find(node => node.id === selected.value.id)
  selected.value = kept || null
  hovered.value = null
  if (refit) {
    movedByUser.value = false
    frame()
  }
}
function fail(message) {
  error.value = message
  triples.value = []
  nodes.value = []
  edgeList.value = []
  folded.value = 0
  selected.value = null
}
function applyText(text) {
  triples.value = parseRdfXml(text)
  query.value = ''
  selected.value = null
  rebuild(true)
}
function choose(event) {
  const file = event.target.files?.[0]
  event.target.value = ''
  if (!file) return
  fileName.value = file.name
  if (file.size > 20 * 1024 * 1024) return fail('文件超过 20 MB。')
  const reader = new FileReader()
  reader.onload = () => {
    const text = String(reader.result || '')
    source.value = text.length > 200000 ? '' : text
    try {applyText(text); error.value = ''} catch (err) {fail(err.message || '解析失败')}
  }
  reader.onerror = () => fail('读取文件失败。')
  reader.readAsText(file)
}
function parseText() {
  fileName.value = '粘贴内容'
  try {applyText(source.value); error.value = ''} catch (err) {fail(err.message || '解析失败')}
}
function clearAll() {
  source.value = ''
  fileName.value = '未选择 RDF 文件'
  error.value = ''
  query.value = ''
  triples.value = []
  nodes.value = []
  edgeList.value = []
  folded.value = 0
  selected.value = null
  view.value = {x: 40, y: 40, k: 1}
}
function svgPoint(event) {
  const svg = svgRef.value
  if (!svg) return {x: 0, y: 0}
  const point = svg.createSVGPoint()
  point.x = event.clientX
  point.y = event.clientY
  const matrix = svg.getScreenCTM()
  if (!matrix) return {x: event.offsetX || 0, y: event.offsetY || 0}
  const mapped = point.matrixTransform(matrix.inverse())
  return {x: mapped.x, y: mapped.y}
}
function graphPoint(event) {
  const point = svgPoint(event)
  return {x: (point.x - view.value.x) / view.value.k, y: (point.y - view.value.y) / view.value.k}
}
function centerOn(node, minScale = 0.95) {
  const k = Math.min(1.6, Math.max(view.value.k, minScale))
  view.value = {k, x: viewport.value.w / 2 - node.x * k, y: viewport.value.h / 2 - node.y * k}
  movedByUser.value = true
}
function zoomBy(factor) {
  const {w, h} = viewport.value
  const {x, y, k} = view.value
  const next = clamp(k * factor, 0.18, 2.6)
  const gx = (w / 2 - x) / k
  const gy = (h / 2 - y) / k
  view.value = {k: next, x: w / 2 - gx * next, y: h / 2 - gy * next}
  movedByUser.value = true
}
function fitAll() {
  if (!nodes.value.length) return
  movedByUser.value = false
  frame()
}
function onWheel(event) {
  if (!nodes.value.length) return
  event.preventDefault()
  const point = svgPoint(event)
  const {x, y, k} = view.value
  const gx = (point.x - x) / k
  const gy = (point.y - y) / k
  const next = clamp(k * (event.deltaY < 0 ? 1.08 : 0.92), 0.18, 2.6)
  view.value = {k: next, x: point.x - gx * next, y: point.y - gy * next}
  movedByUser.value = true
}
function startPan(event) {
  if (event.button !== 0 || !nodes.value.length) return
  if (event.target.closest?.('.ontology-node, .ontology-edge, button, a, input, textarea')) return
  event.preventDefault()
  const matrix = svgRef.value?.getScreenCTM()
  const scaleX = matrix?.a || 1
  const scaleY = matrix?.d || 1
  const start = {cx: event.clientX, cy: event.clientY, x: view.value.x, y: view.value.y}
  panning.value = true
  const move = ev => {
    view.value = {...view.value, x: start.x + (ev.clientX - start.cx) / scaleX, y: start.y + (ev.clientY - start.cy) / scaleY}
    movedByUser.value = true
  }
  const up = () => {
    panning.value = false
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    stopPan = null
  }
  stopPan = up
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}
function startDrag(node, event) {
  if (event.button !== 0) return
  event.stopPropagation()
  event.preventDefault()
  selected.value = node
  const start = graphPoint(event)
  const origin = {x: node.x, y: node.y}
  const target = event.currentTarget
  target.setPointerCapture?.(event.pointerId)
  const move = ev => {
    const point = graphPoint(ev)
    node.x = origin.x + point.x - start.x
    node.y = origin.y + point.y - start.y
    movedByUser.value = true
  }
  const up = () => {
    target.removeEventListener('pointermove', move)
    target.removeEventListener('pointerup', up)
  }
  target.addEventListener('pointermove', move)
  target.addEventListener('pointerup', up)
}
function pickEdge(edge) {
  const node = nodes.value.find(item => item.id === edge.s)
  if (node) selected.value = node
}
function openRelation(id) {
  const node = nodes.value.find(item => item.id === id)
  if (!node) return
  selected.value = node
  centerOn(node)
}
function focusFirst() {
  const first = groups.value[0]?.items[0]
  if (!first) return
  selected.value = first
  centerOn(first)
}
function onKey(event) {
  if (event.target !== graphRef.value) return
  if (event.key === 'Escape') selected.value = null
  if (event.key === '+' || event.key === '=') zoomBy(1.12)
  if (event.key === '-' || event.key === '_') zoomBy(1 / 1.12)
  if (event.key === '0') fitAll()
}
function nodeOn(id) {return !activeIds.value || activeIds.value.has(id)}
function edgeOn(edge) {
  if (hovered.value === edge.id) return true
  return !!(selected.value && (edge.s === selected.value.id || edge.o === selected.value.id))
}
function labelVisible(edge) {
  if (hovered.value === edge.id) return true
  return !!(selected.value && (edge.s === selected.value.id || edge.o === selected.value.id))
}

watch([showAnnotations, showLiterals, showMetaTypes], () => {if (triples.value.length) rebuild(true)})
onMounted(() => {
  syncViewport()
  resizeObserver = new ResizeObserver(() => {
    const previous = {...viewport.value}
    syncViewport()
    if (!nodes.value.length || movedByUser.value) return
    if (previous.w === viewport.value.w && previous.h === viewport.value.h) return
    frame()
  })
  if (graphRef.value) {
    resizeObserver.observe(graphRef.value)
    graphRef.value.addEventListener('wheel', onWheel, {passive: false})
  }
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  graphRef.value?.removeEventListener('wheel', onWheel)
  stopPan?.()
})
</script>

<template>
  <section class="ontology-page">
    <header>
      <div>
        <span class="eyebrow">ONTOLOGY GRAPH</span>
        <h1>本体文件可视化</h1>
        <p>导入 RDF/XML 本体，查看类、属性、实例之间的语义关系。</p>
      </div>
      <div class="ontology-stats" aria-label="图谱统计">
        <span><b>{{summary.class}}</b>类</span>
        <span><b>{{summary.property}}</b>属性</span>
        <span><b>{{summary.individual}}</b>实例</span>
        <span><b>{{summary.edges}}</b>关系</span>
      </div>
    </header>
    <div class="ontology-workspace">
      <aside class="ontology-sidebar">
        <div class="ontology-section-title"><b>数据源</b><span>RDF / XML</span></div>
        <button type="button" class="ontology-upload" @click="fileInput.click()">
          <span>＋</span><b>导入本体文件</b><small>.rdf · .owl · .xml · 最大 20 MB</small>
        </button>
        <input ref="fileInput" type="file" accept=".rdf,.owl,.xml,.rdfs,application/rdf+xml,text/xml,application/xml" hidden @change="choose">
        <div class="ontology-file">
          <span>◈</span>
          <div><b>{{fileName}}</b><small>{{nodes.length ? '已解析 · 可探索' : '等待导入文件'}}</small></div>
          <button v-if="nodes.length || source" type="button" aria-label="清空" @click="clearAll">×</button>
        </div>
        <label class="ontology-paste">粘贴 RDF 内容
          <textarea v-model="source" rows="8" placeholder="粘贴 RDF/XML 内容…"></textarea>
        </label>
        <button type="button" class="primary ontology-parse" :disabled="!source.trim()" @click="parseText">解析并生成图谱</button>
        <p v-if="error" class="ontology-error" role="alert">{{error}}</p>
        <div class="ontology-legend">
          <span><i class="class"></i>类</span>
          <span><i class="property"></i>属性</span>
          <span><i class="individual"></i>实例</span>
          <span><i class="literal"></i>字面量</span>
        </div>
        <div v-if="nodes.length" class="ontology-nav">
          <p v-if="!groups.length" class="ontology-muted">没有匹配的节点</p>
          <section v-for="group in groups" :key="group.kind">
            <h3>{{group.name}} {{group.items.length}}</h3>
            <button v-for="item in group.items" :key="item.id" type="button" :class="{active: selected?.id === item.id}" :title="item.uri || item.lexical || item.label" @click="selected = item; centerOn(item)">
              <i :class="item.kind"></i><span>{{item.label}}</span>
            </button>
          </section>
        </div>
      </aside>
      <div class="ontology-stage">
        <div class="ontology-toolbar">
          <div><b>语义关系图谱</b><small>{{hint}}</small></div>
          <label>搜索<input v-model="query" placeholder="名称或 URI" @keydown.enter.prevent="focusFirst"></label>
          <button type="button" class="ontology-btn" :class="{active: showAnnotations}" :aria-pressed="showAnnotations" title="显示 label、comment 等标注关系" @click="showAnnotations = !showAnnotations">标注</button>
          <button type="button" class="ontology-btn" :class="{active: showLiterals}" :aria-pressed="showLiterals" title="把字面量取值画到画布上，默认收在右侧详情里" @click="showLiterals = !showLiterals">字面量</button>
          <button type="button" class="ontology-btn" :class="{active: showMetaTypes}" :aria-pressed="showMetaTypes" title="显示 owl:Class、xsd:string 等元类型和数据类型" @click="showMetaTypes = !showMetaTypes">元类型</button>
          <button type="button" class="ontology-btn" aria-label="放大" @click="zoomBy(1.12)">+</button>
          <button type="button" class="ontology-btn" aria-label="缩小" @click="zoomBy(1 / 1.12)">−</button>
          <button type="button" class="ontology-btn" @click="fitAll">适应</button>
          <button type="button" class="ontology-btn" :disabled="!triples.length" @click="rebuild(true)">重排</button>
        </div>
        <div ref="graphRef" class="ontology-graph" :class="{empty: !nodes.length, panning}" tabindex="0" @pointerdown="startPan" @keydown="onKey">
          <svg v-if="nodes.length" ref="svgRef" class="ontology-svg" :viewBox="`0 0 ${viewport.w} ${viewport.h}`" preserveAspectRatio="none" @dblclick.self="fitAll">
            <defs>
              <marker id="ontology-arrow" markerUnits="userSpaceOnUse" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M1 1.2 L6.2 4 L1 6.8" fill="none" stroke="var(--ui-muted)" stroke-width="1.15" stroke-linecap="round" stroke-linejoin="round"/>
              </marker>
              <marker id="ontology-arrow-on" markerUnits="userSpaceOnUse" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M1 1.2 L6.2 4 L1 6.8" fill="none" stroke="var(--ui-text)" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"/>
              </marker>
            </defs>
            <g :transform="`translate(${view.x} ${view.y}) scale(${view.k})`">
              <g v-for="edge in drawnEdges" :key="edge.id" class="ontology-edge" :class="{on: edgeOn(edge), off: activeIds && !edgeOn(edge)}" @pointerdown.stop="pickEdge(edge)" @pointerenter="hovered = edge.id" @pointerleave="hovered = null">
                <path class="hit" :d="edge.d"/>
                <path class="line" :d="edge.d" :marker-end="edgeOn(edge) ? 'url(#ontology-arrow-on)' : 'url(#ontology-arrow)'"/>
                <text v-if="labelVisible(edge)" :x="edge.lx" :y="edge.ly">{{edge.label}}</text>
                <title>{{edge.pUri}}</title>
              </g>
              <g v-for="node in nodes" :key="node.id" class="ontology-node" :class="[node.kind, {selected: selected?.id === node.id, off: !nodeOn(node.id)}]" :transform="`translate(${node.x} ${node.y})`" @pointerdown="startDrag(node, $event)" @dblclick.stop="centerOn(node, 1.25)">
                <rect class="card" :x="-node.w / 2" :y="-node.h / 2" :width="node.w" :height="node.h" rx="4"/>
                <rect class="spine" :x="-node.w / 2" :y="-node.h / 2" width="3" :height="node.h"/>
                <text text-anchor="start" :x="-node.w / 2 + 12" dy="0.35em">{{node.short}}</text>
                <title>{{node.label}}{{node.uri ? `\n${node.uri}` : ''}}</title>
              </g>
            </g>
          </svg>
          <div v-else class="ontology-empty">
            <span>◎</span>
            <b>导入 RDF/XML 开始探索</b>
            <p>节点按语义关系自由散开，可以拖拽摆放。字面量和 xsd 数据类型默认收在详情里。</p>
          </div>
        </div>
        <aside v-if="selected" class="ontology-inspector">
          <div class="ontology-inspector-head">
            <span class="kind">{{kindName[selected.kind] || '节点'}}</span>
            <button type="button" aria-label="关闭" @click="selected = null">×</button>
          </div>
          <b>{{selected.label}}</b>
          <small v-if="selected.uri" class="uri">{{selected.uri}}</small>
          <small v-else-if="selected.lang || selected.datatypeLabel" class="uri">{{selected.lang ? `@${selected.lang}` : ''}} {{selected.datatypeLabel}}</small>
          <p class="ontology-rel-count">相关关系 {{relations.length}} 条</p>
          <ul class="ontology-rels">
            <li v-for="rel in relationRows" :key="rel.key">
              <button type="button" :title="rel.text" :disabled="!rel.linked" @click="openRelation(rel.other)">
                <em>{{rel.dir === 'in' ? '←' : '→'}}</em><b>{{rel.p}}</b><span>{{rel.text}}</span>
              </button>
            </li>
          </ul>
          <p v-if="relations.length > relationRows.length" class="ontology-muted">还有 {{relations.length - relationRows.length}} 条未展开</p>
        </aside>
      </div>
    </div>
  </section>
</template>

<style scoped>
.ontology-page{color:var(--ui-text);--ont-class:#2f9aab;--ont-prop:#c89600;--ont-lit:#d45b86}
.ontology-stats{display:flex;flex-wrap:wrap;gap:8px}
.ontology-stats span{display:flex;align-items:baseline;gap:4px;padding:6px 8px;border:1px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);box-shadow:2px 2px var(--ui-border);color:var(--ui-muted);font-size:12px}
.ontology-stats b{color:var(--ui-text);font-size:18px;line-height:1}
.ontology-workspace{display:grid;grid-template-columns:minmax(260px,320px) minmax(0,1fr);height:min(780px,calc(100dvh - 230px));min-height:560px;border:2px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);box-shadow:5px 5px var(--ui-border);overflow:hidden}
.ontology-sidebar{min-width:0;min-height:0;display:flex;flex-direction:column;gap:10px;padding:16px;overflow:auto;background:var(--pop-soft,var(--ui-surface-subtle));border-right:1px solid var(--ui-border)}
.ontology-section-title{display:flex;justify-content:space-between;align-items:center;padding-bottom:10px;border-bottom:2px solid var(--ui-border)}
.ontology-section-title b{font-size:14px;font-weight:800}
.ontology-section-title span{padding:2px 6px;border:1px solid var(--pop-ink,#252526);background:var(--pop-yellow);color:var(--pop-ink,#252526);font-size:10px;font-weight:800;letter-spacing:.08em}
.ontology-upload{display:grid;place-items:center;gap:4px;width:100%;padding:16px 10px;border:2px dashed var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);color:var(--ui-text);box-shadow:3px 3px var(--ui-border);cursor:pointer}
.ontology-upload span{font-size:28px;line-height:1;color:currentColor}
.ontology-upload b{font-size:13px}
.ontology-upload small,.ontology-file small,.ontology-muted{color:var(--ui-muted);font-size:11px;line-height:1.5}
.ontology-upload:hover{background:var(--pop-cyan);color:var(--pop-ink,#252526)}
.ontology-file{display:flex;align-items:center;gap:8px;padding:8px;border:1px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9)}
.ontology-file>span{color:var(--ui-text);font-size:16px}
.ontology-file div{min-width:0;flex:1}
.ontology-file b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.ontology-file button,.ontology-inspector-head button{border:0;background:transparent;color:var(--ui-muted);font-size:18px;cursor:pointer}
.ontology-paste{display:flex;flex-direction:column;gap:6px;margin:0;color:var(--ui-text);font-size:12px;font-weight:750}
.ontology-paste textarea,.ontology-toolbar input{width:100%;box-sizing:border-box;border:1px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);color:var(--ui-text)}
.ontology-paste textarea{min-height:120px;resize:vertical;padding:8px 10px;font:12px/1.6 ui-monospace,SFMono-Regular,monospace}
.ontology-parse{width:100%;min-height:38px;margin:2px 0 4px;cursor:pointer}
.ontology-parse:disabled{opacity:.45;cursor:not-allowed}
.ontology-error{margin:0;padding:8px 10px;border:1px solid var(--ui-danger);border-radius:2px;background:var(--ui-danger-soft);color:var(--ui-danger);font-size:12px;line-height:1.6}
.ontology-legend{display:flex;flex-wrap:wrap;gap:8px 12px;color:var(--ui-muted);font-size:11px}
.ontology-legend span,.ontology-nav button{display:inline-flex;align-items:center;gap:6px}
.ontology-legend i,.ontology-nav i{width:10px;height:10px;flex:none;border:1px solid var(--ui-border)}
.ontology-page i.class,.ontology-node.class .spine{background:var(--ont-class);fill:var(--ont-class)}
.ontology-page i.property,.ontology-node.property .spine{background:var(--ont-prop);fill:var(--ont-prop)}
.ontology-page i.individual{background:var(--pop-paper,#fffef9)}
.ontology-page i.literal,.ontology-node.literal .spine{background:var(--ont-lit);fill:var(--ont-lit)}
.ontology-page :is(i.resource,i.list,i.restriction,i.ontology){background:var(--pop-soft,#f4f2e9)}
.ontology-node.ontology .spine,.ontology-node.individual .spine,.ontology-node.resource .spine,.ontology-node.list .spine,.ontology-node.restriction .spine{fill:var(--ui-muted)}
.ontology-nav{display:grid;gap:8px;min-height:0}
.ontology-nav section{display:grid;gap:4px}
.ontology-nav h3{margin:6px 0 0;color:var(--ui-muted);font-size:11px;font-weight:800;letter-spacing:.08em}
.ontology-nav button{display:flex;width:100%;min-height:30px;padding:4px 6px;border:1px solid transparent;background:transparent;color:var(--ui-text);text-align:left;cursor:pointer;box-shadow:none}
.ontology-nav button span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ontology-nav button.active{border-color:var(--ui-border);background:var(--pop-yellow);color:var(--pop-ink,#252526)}
.ontology-stage{position:relative;min-width:0;min-height:0;display:flex;flex-direction:column}
.ontology-toolbar{display:flex;align-items:center;gap:8px;min-height:58px;padding:10px 12px;border-bottom:2px solid var(--ui-border);background:var(--pop-paper,#fffef9)}
.ontology-toolbar>div{display:grid;gap:2px;min-width:0}
.ontology-toolbar b{font-size:15px;font-weight:800}
.ontology-toolbar small{color:var(--ui-muted);font-size:11px;line-height:1.4}
.ontology-toolbar label{display:flex;flex-direction:row;align-items:center;gap:6px;margin:0 0 0 auto;color:var(--ui-muted);font-size:12px;font-weight:750}
.ontology-toolbar input{width:min(220px,24vw);min-height:32px;padding:6px 8px}
.ontology-btn{min-height:32px;padding:5px 9px;border:1px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);color:var(--ui-text);box-shadow:2px 2px var(--ui-border);font-weight:750;cursor:pointer}
.ontology-btn:hover,.ontology-nav button:hover{background:var(--pop-cyan);color:var(--pop-ink,#252526)}
.ontology-btn.active{background:var(--pop-yellow);color:var(--pop-ink,#252526)}
.ontology-btn:disabled{opacity:.45;cursor:not-allowed}
.ontology-graph{position:relative;flex:1;min-height:0;overflow:hidden;background:var(--pop-paper,#fffef9);background-image:radial-gradient(color-mix(in srgb,var(--ui-text) 10%,transparent) .7px,transparent .7px);background-size:22px 22px;cursor:grab;touch-action:none;outline:none}
.ontology-graph.panning,.ontology-node:active{cursor:grabbing}
.ontology-graph.empty{cursor:default}
.ontology-svg{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none}
.ontology-edge .hit{fill:none;stroke:transparent;stroke-width:14;pointer-events:stroke}
.ontology-edge .line{fill:none;stroke:color-mix(in srgb,var(--ui-text) 42%,transparent);stroke-width:1.15;stroke-linecap:round;pointer-events:none}
.ontology-edge.on .line{stroke:var(--ui-text);stroke-width:1.6}
.ontology-edge.off{opacity:.14}
.ontology-edge text{fill:var(--ui-muted);stroke:var(--pop-paper,#fffef9);stroke-width:4px;paint-order:stroke;font:600 11px Inter,"PingFang SC","Microsoft YaHei",sans-serif;text-anchor:middle;pointer-events:none}
.ontology-node{cursor:grab;user-select:none}
.ontology-node .card{fill:var(--pop-paper,#fffef9);stroke:color-mix(in srgb,var(--ui-text) 16%,var(--ui-border));stroke-width:1}
.ontology-node .spine{stroke:none}
.ontology-node text{fill:var(--ui-text);font:600 12px Inter,"PingFang SC","Microsoft YaHei",sans-serif;pointer-events:none}
.ontology-node.selected .card{stroke:#b43358;stroke-width:1.5}
.ontology-node.off{opacity:.18}
.ontology-empty{position:absolute;inset:0;display:grid;place-items:center;align-content:center;gap:6px;padding:24px;text-align:center}
.ontology-empty span{display:grid;place-items:center;width:64px;height:64px;margin:0 auto 8px;border:2px solid var(--ui-border);background:var(--pop-cyan);color:var(--pop-ink,#252526);box-shadow:3px 3px var(--ui-border);font-size:28px}
.ontology-empty b{color:var(--ui-text);font-size:16px}
.ontology-empty p{max-width:380px;margin:0;color:var(--ui-muted);font-size:12px;line-height:1.7}
.ontology-inspector{position:absolute;top:70px;right:12px;z-index:2;width:min(300px,calc(100% - 24px));max-height:calc(100% - 84px);overflow:auto;display:grid;align-content:start;gap:6px;padding:12px;border:2px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);box-shadow:4px 4px var(--ui-border)}
.ontology-inspector-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ontology-inspector .kind{display:inline-flex;padding:2px 6px;border:1px solid var(--pop-ink,#252526);background:var(--pop-yellow);color:var(--pop-ink,#252526);font-size:10px;font-weight:800}
.ontology-inspector b{font-size:14px;line-height:1.5;overflow-wrap:anywhere}
.ontology-inspector .uri,.ontology-rel-count{color:var(--ui-muted);font-size:11px;line-height:1.5;overflow-wrap:anywhere}
.ontology-rels{display:grid;gap:4px;margin:0;padding:0;list-style:none}
.ontology-rels button{display:grid;grid-template-columns:auto auto minmax(0,1fr);gap:6px;align-items:start;width:100%;padding:5px 6px;border:1px solid var(--ui-border);background:var(--pop-soft,#f4f2e9);color:var(--ui-text);text-align:left;cursor:pointer}
.ontology-rels button:disabled{cursor:default;opacity:.82}
.ontology-rels em{font-style:normal;color:var(--ui-muted)}
.ontology-rels b{font-size:11px}
.ontology-rels span{font-size:12px;line-height:1.45;overflow-wrap:anywhere}
@media(max-width:760px){
  .ontology-workspace{grid-template-columns:1fr;height:auto;min-height:0}
  .ontology-sidebar{border-right:0;border-bottom:1px solid var(--ui-border);max-height:none}
  .ontology-graph{height:68vh;min-height:480px}
  .ontology-toolbar{flex-wrap:wrap}
  .ontology-toolbar label{width:100%;margin-left:0}
  .ontology-toolbar input{width:100%}
  .ontology-inspector{left:10px;right:10px;width:auto}
}
</style>
