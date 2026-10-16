<script setup>
import {computed, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import {buildOntologyGraph, parseRdfXml} from './ontologyRdf.js'

const RDF_NS = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
const RDFS_NS = 'http://www.w3.org/2000/01/rdf-schema#'
const OWL_NS = 'http://www.w3.org/2002/07/owl#'
const KINDS = [
  ['ontology', '本体'], ['class', '类'], ['property', '属性'], ['restriction', '限制'],
  ['individual', '实例'], ['list', '列表'], ['resource', '外部资源'], ['literal', '字面量'],
]
const kindName = Object.fromEntries(KINDS)
const MODE_NAMES = {overview: '概览', fields: '字段', full: '完整'}
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
const displayMode = ref('overview')
const fieldClassId = ref('')
const layerOn = ref({property: true, class: true, individual: true})
const panning = ref(false)
const movedByUser = ref(false)
const viewport = ref({w: 960, h: 640})
const cam = ref({yaw: 0.4, pitch: 0.78, zoom: 1})
let resizeObserver = null
let stopPan = null
let spinFrame = 0
const reduceMotion = typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

const HEX = Array.from({length: 6}, (_, index) => {
  const angle = -Math.PI / 2 + index * Math.PI / 3
  return `${(Math.cos(angle) * 7).toFixed(1)},${(Math.sin(angle) * 7).toFixed(1)}`
}).join(' ')
const classNodes = computed(() => nodes.value.filter(node => node.kind === 'class' || node.kind === 'ontology'))
const propertyNodes = computed(() => nodes.value.filter(node => node.kind === 'property'))
const classIds = computed(() => new Set(classNodes.value.map(node => node.id)))
const nodeById = computed(() => new Map(nodes.value.map(node => [node.id, node])))
const fieldClass = computed(() => nodeById.value.get(fieldClassId.value) || null)
const fieldsForClass = computed(() => {
  if (!fieldClass.value) return []
  const domains = new Set(triples.value.filter(triple => triple.pUri === RDFS_NS + 'domain' && triple.o === fieldClass.value.id).map(triple => triple.s))
  return propertyNodes.value.filter(node => domains.has(node.id))
})
const displayNodes = computed(() => {
  if (displayMode.value === 'overview') return classNodes.value
  if (displayMode.value === 'fields') return fieldClass.value ? [fieldClass.value, ...fieldsForClass.value] : []
  return nodes.value
})
const overviewEdges = computed(() => {
  const direct = edgeList.value.filter(edge => classIds.value.has(edge.s) && classIds.value.has(edge.o) && edge.pUri !== RDF_NS + 'type').map(edge => ({...edge, visualKind: 'class'}))
  const objectProperties = new Set(triples.value.filter(triple => triple.pUri === RDF_NS + 'type' && triple.o === OWL_NS + 'ObjectProperty').map(triple => triple.s))
  const derived = []
  const seen = new Set()
  for (const property of propertyNodes.value) {
    const domains = triples.value.filter(triple => triple.s === property.id && triple.pUri === RDFS_NS + 'domain' && classIds.value.has(triple.o)).map(triple => triple.o)
    const ranges = triples.value.filter(triple => triple.s === property.id && triple.pUri === RDFS_NS + 'range' && classIds.value.has(triple.o)).map(triple => triple.o)
    if (!ranges.length || (!objectProperties.has(property.id) && !domains.length)) continue
    for (const domain of domains) for (const range of ranges) {
      const key = `${property.id}\n${domain}\n${range}`
      if (seen.has(key)) continue
      seen.add(key)
      derived.push({
        id: `property:${property.id}:${domain}:${range}`,
        s: domain, o: range, label: property.label, pUri: property.uri,
        propertyId: property.id, visualKind: 'property', showLabel: true,
      })
    }
  }
  return [...direct, ...derived]
})
const fieldEdges = computed(() => {
  if (!fieldClass.value) return []
  return fieldsForClass.value.map(property => ({
    id: `field:${fieldClass.value.id}:${property.id}`,
    s: fieldClass.value.id, o: property.id, label: property.label,
    pUri: RDFS_NS + 'domain', propertyId: property.id, visualKind: 'property', showLabel: true,
  }))
})
const canvasMessage = computed(() => {
  if (displayMode.value === 'fields' && !fieldClass.value) return '从左侧选择一个类，或在概览中选中类后查看字段。'
  if (displayMode.value === 'fields' && !fieldsForClass.value.length) return `「${fieldClass.value.label}」暂未定义字段。`
  if (displayMode.value === 'full' && !scene.value.nodes.length) return '图层都关了。在右上角重新打开一层。'
  return ''
})
const displayEdges = computed(() => {
  if (displayMode.value === 'overview') return overviewEdges.value
  if (displayMode.value === 'fields') return fieldEdges.value
  return edgeList.value
})
const summary = computed(() => ({
  class: classNodes.value.length,
  property: propertyNodes.value.length,
  individual: nodes.value.filter(node => node.kind === 'individual' || node.kind === 'restriction').length,
  edges: displayEdges.value.length,
}))
const hint = computed(() => {
  if (!triples.value.length) return '导入 RDF/XML 后在这里探索'
  const keyword = query.value.trim()
  if (keyword) return `筛选“${keyword}”，回车定位`
  if (displayMode.value === 'overview') return '概览只保留类关系 · 选择一个类查看字段'
  if (displayMode.value === 'fields') return fieldClass.value ? `正在查看「${fieldClass.value.label}」的字段` : '选择一个类后查看字段'
  if (folded.value) return `拖拽旋转 · 已收起 ${folded.value} 条标注、字面量或数据类型`
  return '完整 RDF 结构 · 可开关图层'
})
const navigationNodes = computed(() => {
  if (displayMode.value === 'overview') return classNodes.value
  if (displayMode.value === 'fields') return fieldClass.value ? [fieldClass.value, ...fieldsForClass.value] : classNodes.value
  return nodes.value
})
const groups = computed(() => {
  const keyword = query.value.trim().toLowerCase()
  const matched = navigationNodes.value.filter(node => !keyword || `${node.label} ${node.uri} ${node.lexical}`.toLowerCase().includes(keyword))
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
    for (const edge of displayEdges.value) {
      if (matched.has(edge.s) || matched.has(edge.propertyId)) ids.add(edge.o)
      if (matched.has(edge.o) || matched.has(edge.propertyId)) ids.add(edge.s)
    }
  }
  if (selected.value) {
    ids.add(selected.value.id)
    for (const edge of displayEdges.value) {
      if (edgeTouches(edge, selected.value.id)) {
        ids.add(edge.s)
        ids.add(edge.o)
      }
    }
  }
  return ids
})
const LAYER = {property: {y: 124, radius: 268}, class: {y: 0, radius: 186}, individual: {y: -124, radius: 112}}
const FLOOR_NAME = {property: '属性', class: '类', individual: '实例'}
const layerCounts = computed(() => {
  const counts = {property: 0, class: 0, individual: 0}
  for (const node of displayNodes.value) counts[layerOf(node.kind)] += 1
  return counts
})
function layerOf(kind) {
  if (kind === 'property') return 'property'
  if (kind === 'class' || kind === 'ontology') return 'class'
  return 'individual'
}
function posesOf(list) {
  const groups = {property: [], class: [], individual: []}
  for (const node of list) groups[layerOf(node.kind)].push(node)
  const poses = []
  for (const kind of ['property', 'class', 'individual']) {
    const group = groups[kind]
    if (!group.length) continue
    const layer = LAYER[kind]
    const radius = group.length < 2 ? 0 : layer.radius + Math.max(0, group.length - 8) * 16
    group.forEach((node, index) => {
      const turn = (index / group.length) * Math.PI * 2 - Math.PI / 2
      poses.push({...node, layer: kind, wx: Math.cos(turn) * radius, wy: layer.y, wz: Math.sin(turn) * radius})
    })
  }
  return poses
}
function projectPoint(x, y, z) {
  const {yaw, pitch, zoom} = cam.value
  const cy = Math.cos(yaw)
  const sy = Math.sin(yaw)
  const x1 = x * cy - z * sy
  const z1 = x * sy + z * cy
  const cp = Math.cos(pitch)
  const sp = Math.sin(pitch)
  const y2 = y * cp - z1 * sp
  const z2 = y * sp + z1 * cp
  const focal = 980
  const scale = focal / Math.max(140, focal - z2)
  const {w, h} = viewport.value
  return {
    x: w / 2 + x1 * scale * zoom,
    y: h / 2 - y2 * scale * zoom,
    z: z2,
    s: Math.max(0.58, Math.min(1.35, scale * zoom)),
  }
}
function discGeometry(kind, radius) {
  const y = LAYER[kind].y
  const points = Array.from({length: 72}, (_, index) => {
    const turn = (index / 72) * Math.PI * 2
    return projectPoint(Math.cos(turn) * radius, y, Math.sin(turn) * radius)
  })
  const top = points.reduce((best, point) => point.y < best.y ? point : best)
  return {
    z: points.reduce((sum, point) => sum + point.z, 0) / points.length,
    d: `${points.map((point, index) => `${index ? 'L' : 'M'}${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ')}Z`,
    lx: top.x,
    ly: top.y - 6,
  }
}
const kindById = computed(() => new Map(displayNodes.value.map(node => [node.id, node.kind])))
const scene = computed(() => {
  const poses = posesOf(displayNodes.value).filter(node => displayMode.value !== 'full' || layerOn.value[node.layer])
  const byId = new Map(poses.map(node => [node.id, node]))
  const kinds = ['property', 'class', 'individual'].filter(kind => poses.some(node => node.layer === kind))
  const discs = kinds.map(kind => {
    const ring = poses.filter(node => node.layer === kind).reduce((max, node) => Math.max(max, Math.hypot(node.wx, node.wz)), 0)
    const radius = Math.max(LAYER[kind].radius * 0.72, ring) + 70
    const disc = discGeometry(kind, radius)
    return {...disc, kind, name: FLOOR_NAME[kind], ring: discGeometry(kind, radius * 0.56).d}
  }).sort((a, b) => a.z - b.z)
  let axis = ''
  if (kinds.length > 1) {
    const ys = kinds.map(kind => LAYER[kind].y)
    const hi = projectPoint(0, Math.max(...ys), 0)
    const lo = projectPoint(0, Math.min(...ys), 0)
    axis = `M ${hi.x.toFixed(1)} ${hi.y.toFixed(1)} L ${lo.x.toFixed(1)} ${lo.y.toFixed(1)}`
  }
  const projected = poses.map(node => {
    const point = projectPoint(node.wx, node.wy, node.wz)
    return {...node, sx: point.x, sy: point.y, z: point.z, s: point.s}
  }).sort((a, b) => a.z - b.z)
  const near = Math.max(...projected.map(node => node.z), 0)
  const far = Math.min(...projected.map(node => node.z), 0)
  const span = near - far
  if (span > 36) for (const node of projected) node.back = (node.z - far) / span < 0.38
  const edges = displayEdges.value.map(edge => {
    const from = byId.get(edge.s)
    const to = byId.get(edge.o)
    if (!from || !to) return null
    const a = projectPoint(from.wx, from.wy, from.wz)
    const b = projectPoint(to.wx, to.wy, to.wz)
    return {...edge, d: `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} L ${b.x.toFixed(1)} ${b.y.toFixed(1)}`, lx: (a.x + b.x) / 2, ly: (a.y + b.y) / 2 - 8, z: (a.z + b.z) / 2}
  }).filter(Boolean).sort((a, b) => a.z - b.z)
  return {discs, nodes: projected, edges, axis}
})
function setMode(mode) {
  if (!MODE_NAMES[mode] || displayMode.value === mode) return
  displayMode.value = mode
  if (mode === 'full') layerOn.value = {property: true, class: true, individual: true}
  frame()
}
function selectNode(node, openFields = false) {
  selected.value = node
  if (node.kind === 'class') fieldClassId.value = node.id
  if (node.kind === 'ontology') fieldClassId.value = ''
  if ((node.kind === 'class' || node.kind === 'ontology') && (openFields || displayMode.value === 'fields') && node.kind === 'class') setMode('fields')
  centerOn(node)
}
function openFields(node = selected.value) {
  if (!node || (node.kind !== 'class' && node.kind !== 'ontology')) return
  fieldClassId.value = node.kind === 'class' ? node.id : ''
  setMode('fields')
  centerOn(node)
}
function toggleLayer(kind) {
  if (!layerCounts.value[kind]) return
  layerOn.value = {...layerOn.value, [kind]: !layerOn.value[kind]}
}
function sourceKind(edge) {return edge.visualKind || kindById.value.get(edge.s) || 'class'}
function edgeMarker(edge) {
  const kind = sourceKind(edge)
  if (kind === 'property') return 'url(#ontology-dot-property)'
  if (kind === 'literal' || kind === 'individual' || kind === 'restriction') return 'url(#ontology-dot-literal)'
  return 'url(#ontology-dot-class)'
}
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
function resetCam() {
  const count = displayNodes.value.length || 1
  cam.value = {yaw: 0.4, pitch: 0.78, zoom: clamp(1.15 - count * 0.01, 0.52, 1.05)}
}
function frame() {
  syncViewport()
  resetCam()
}
function rebuild(refit) {
  const model = buildOntologyGraph(triples.value, {annotations: showAnnotations.value, literals: showLiterals.value, metaTypes: showMetaTypes.value})
  nodes.value = model.nodes
  edgeList.value = model.edges
  folded.value = model.folded
  const kept = selected.value && model.nodes.find(node => node.id === selected.value.id)
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
  fieldClassId.value = ''
  displayMode.value = 'overview'
  showAnnotations.value = false
  showLiterals.value = false
  showMetaTypes.value = false
}
function applyText(text) {
  triples.value = parseRdfXml(text)
  query.value = ''
  selected.value = null
  fieldClassId.value = ''
  displayMode.value = 'overview'
  showAnnotations.value = false
  showLiterals.value = false
  showMetaTypes.value = false
  layerOn.value = {property: true, class: true, individual: true}
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
  fieldClassId.value = ''
  displayMode.value = 'overview'
  showAnnotations.value = false
  showLiterals.value = false
  showMetaTypes.value = false
  layerOn.value = {property: true, class: true, individual: true}
  resetCam()
}
function centerOn(node) {
  const pose = posesOf(displayNodes.value).find(item => item.id === node.id)
  if (!pose) return
  if (!layerOn.value[pose.layer]) layerOn.value = {...layerOn.value, [pose.layer]: true}
  cam.value = {...cam.value, yaw: Math.PI / 2 - Math.atan2(pose.wz, pose.wx)}
  movedByUser.value = true
}
function zoomBy(factor) {
  cam.value = {...cam.value, zoom: clamp(cam.value.zoom * factor, 0.45, 2.4)}
}
function fitAll() {
  if (!nodes.value.length) return
  movedByUser.value = false
  frame()
}
function onWheel(event) {
  if (!nodes.value.length) return
  event.preventDefault()
  zoomBy(event.deltaY < 0 ? 1.08 : 0.92)
}
function startPan(event) {
  if (event.button !== 0 || !nodes.value.length) return
  if (event.target.closest?.('.ontology-stat, .ontology-legend-card, .ontology-dock, button, a, input, textarea')) return
  event.preventDefault()
  const start = {x: event.clientX, y: event.clientY, yaw: cam.value.yaw, pitch: cam.value.pitch}
  const nodeId = event.target.closest?.('.ontology-node')?.dataset.id
  let moved = false
  panning.value = true
  const move = ev => {
    const dx = ev.clientX - start.x
    const dy = ev.clientY - start.y
    if (dx * dx + dy * dy > 16) moved = true
    cam.value = {yaw: start.yaw + dx * 0.008, pitch: clamp(start.pitch + dy * 0.005, 0.28, 1.32), zoom: cam.value.zoom}
    movedByUser.value = true
  }
  const up = () => {
    panning.value = false
    if (!moved && nodeId) {
      const node = displayNodes.value.find(item => item.id === nodeId)
      if (node) selectNode(node)
    }
    window.removeEventListener('pointermove', move)
    window.removeEventListener('pointerup', up)
    stopPan = null
  }
  stopPan = up
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', up)
}
function pickEdge(edge) {
  const node = nodeById.value.get(edge.propertyId || edge.s)
  if (node) selectNode(node)
}
function openRelation(id) {
  const node = nodeById.value.get(id)
  if (!node) return
  selectNode(node)
}
function focusFirst() {
  const first = groups.value[0]?.items[0]
  if (!first) return
  selectNode(first)
}
function onKey(event) {
  if (event.target !== graphRef.value) return
  if (event.key === 'Escape') {
    selected.value = null
    if (displayMode.value === 'fields') setMode('overview')
  }
  if (event.key === '+' || event.key === '=') zoomBy(1.12)
  if (event.key === '-' || event.key === '_') zoomBy(1 / 1.12)
  if (event.key === '0') fitAll()
}
function edgeTouches(edge, id) {
  return edge.s === id || edge.o === id || edge.propertyId === id
}
function nodeOn(id) {return !activeIds.value || activeIds.value.has(id)}
function edgeOn(edge) {
  if (hovered.value === edge.id) return true
  return !!(selected.value && edgeTouches(edge, selected.value.id))
}
function labelVisible(edge) {
  if (displayMode.value === 'overview' && edge.showLabel) return true
  if (hovered.value === edge.id) return true
  return !!(selected.value && edgeTouches(edge, selected.value.id))
}

watch([showAnnotations, showLiterals, showMetaTypes], () => {if (triples.value.length) rebuild(true)})
onMounted(() => {
  syncViewport()
  resizeObserver = new ResizeObserver(() => syncViewport())
  if (graphRef.value) {
    resizeObserver.observe(graphRef.value)
    graphRef.value.addEventListener('wheel', onWheel, {passive: false})
  }
  const spin = () => {
    if (!reduceMotion && !panning.value && nodes.value.length) cam.value = {...cam.value, yaw: cam.value.yaw + 0.004}
    spinFrame = requestAnimationFrame(spin)
  }
  spinFrame = requestAnimationFrame(spin)
})
onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  graphRef.value?.removeEventListener('wheel', onWheel)
  stopPan?.()
  cancelAnimationFrame(spinFrame)
})
</script>

<template>
  <section class="ontology-page">
    <header>
      <div>
        <span class="eyebrow">ONTOLOGY GRAPH</span>
        <h1>本体文件可视化</h1>
        <p>先看类之间的结构，再按需展开字段与完整 RDF 细节。</p>
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
            <button v-for="item in group.items" :key="item.id" type="button" :class="{active: selected?.id === item.id}" :title="item.uri || item.lexical || item.label" @click="selectNode(item, item.kind === 'class' && displayMode === 'overview')">
              <i :class="item.kind"></i><span>{{item.label}}</span>
              <em v-if="item.kind === 'class' && displayMode === 'overview'">字段</em>
            </button>
          </section>
        </div>
      </aside>
      <div class="ontology-stage">
        <div ref="graphRef" class="ontology-graph" :class="{empty: !nodes.length, panning}" tabindex="0" @pointerdown="startPan" @keydown="onKey">
          <div v-if="nodes.length" class="ontology-stat" :title="hint">
            <span>类 <b>{{summary.class}}</b></span>
            <i></i>
            <span>属性 <b class="prop">{{summary.property}}</b></span>
            <i></i>
            <span>关系 <b class="rel">{{summary.edges}}</b></span>
          </div>
          <div v-if="nodes.length" class="ontology-legend-card">
            <div class="ontology-mode-tabs" role="tablist" aria-label="图谱视图">
              <button v-for="(name, mode) in MODE_NAMES" :key="mode" type="button" role="tab" :class="{active: displayMode === mode}" :aria-selected="displayMode === mode" @click.stop="setMode(mode)">{{name}}</button>
            </div>
            <p class="ontology-mode-note">{{hint}}</p>
            <template v-if="displayMode === 'full'">
              <span class="rule"></span>
              <button v-for="floor in [['property','属性'],['class','类'],['individual','实例']]" :key="floor[0]" type="button" :class="{off: !layerOn[floor[0]]}" :disabled="!layerCounts[floor[0]]" :aria-pressed="layerOn[floor[0]]" @click.stop="toggleLayer(floor[0])">
                <i :class="floor[0]"></i><span>{{floor[1]}}</span><b>{{layerCounts[floor[0]]}}</b><em class="eye"></em>
              </button>
              <span class="rule"></span>
              <button type="button" :class="{off: !showLiterals}" :aria-pressed="showLiterals" title="把字面量取值画到画布上" @click.stop="showLiterals = !showLiterals"><i class="literal"></i><span>字面量</span><em class="eye"></em></button>
              <button type="button" :class="{off: !showAnnotations}" :aria-pressed="showAnnotations" title="显示 label、comment 等标注关系" @click.stop="showAnnotations = !showAnnotations"><i class="resource"></i><span>标注</span><em class="eye"></em></button>
              <button type="button" :class="{off: !showMetaTypes}" :aria-pressed="showMetaTypes" title="显示 owl:Class、xsd:string 等元类型" @click.stop="showMetaTypes = !showMetaTypes"><i class="individual"></i><span>元类型</span><em class="eye"></em></button>
            </template>
          </div>
          <div class="ontology-dock">
            <input v-model="query" aria-label="搜索" placeholder="搜索名称或 URI" @keydown.enter.prevent="focusFirst">
            <button type="button" class="ontology-btn" aria-label="放大" @click="zoomBy(1.12)">+</button>
            <button type="button" class="ontology-btn" aria-label="缩小" @click="zoomBy(1 / 1.12)">−</button>
            <button type="button" class="ontology-btn" @click="fitAll">适应</button>
            <button type="button" class="ontology-btn" :disabled="!triples.length" @click="rebuild(true)">重排</button>
          </div>
          <svg v-if="nodes.length" ref="svgRef" class="ontology-svg" :viewBox="`0 0 ${viewport.w} ${viewport.h}`" preserveAspectRatio="none" @dblclick.self="fitAll">
            <defs>
              <marker id="ontology-dot-class" markerUnits="userSpaceOnUse" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="7" markerHeight="6" orient="auto"><circle cx="3" cy="4" r="1.7" fill="var(--ont-class)"/></marker>
              <marker id="ontology-dot-property" markerUnits="userSpaceOnUse" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="7" markerHeight="6" orient="auto"><circle cx="3" cy="4" r="1.7" fill="var(--ont-prop)"/></marker>
              <marker id="ontology-dot-literal" markerUnits="userSpaceOnUse" viewBox="0 0 10 8" refX="9" refY="4" markerWidth="7" markerHeight="6" orient="auto"><circle cx="3" cy="4" r="1.7" fill="var(--ont-lit)"/></marker>
              <filter id="ontology-floor" x="-24%" y="-24%" width="148%" height="148%"><feDropShadow dx="0" dy="12" stdDeviation="7" flood-color="var(--ui-text)" flood-opacity="0.16"/></filter>
            </defs>
            <path v-if="scene.axis" class="ontology-axis" :d="scene.axis"/>
            <g v-for="disc in scene.discs" :key="disc.kind" class="ontology-floor">
              <path class="field" :class="disc.kind" :d="disc.d"/>
              <path class="field-ring" :class="disc.kind" :d="disc.ring"/>
              <text class="field-label" :class="disc.kind" :x="disc.lx" :y="disc.ly" text-anchor="middle">{{disc.name}}</text>
            </g>
            <g v-for="edge in scene.edges" :key="edge.id" class="ontology-edge" :class="[sourceKind(edge), {on: edgeOn(edge), off: activeIds && !edgeOn(edge)}]" @pointerdown.stop="pickEdge(edge)" @pointerenter="hovered = edge.id" @pointerleave="hovered = null">
              <path class="hit" :d="edge.d"/>
              <path class="line" :d="edge.d" :marker-end="edgeMarker(edge)"/>
              <text v-if="labelVisible(edge)" :x="edge.lx" :y="edge.ly">{{edge.label}}</text>
              <title>{{edge.pUri}}</title>
            </g>
            <g v-for="node in scene.nodes" :key="node.id" :data-id="node.id" class="ontology-node" :class="[node.kind, {selected: selected?.id === node.id, off: !nodeOn(node.id), back: node.back}]" :transform="`translate(${node.sx} ${node.sy}) scale(${node.s.toFixed(3)})`" @dblclick.stop="centerOn(node)">
              <rect class="hit" :x="-(node.labelW || 48) / 2" y="-28" :width="node.labelW || 48" height="44"/>
              <text class="label" text-anchor="middle" y="-14">{{node.short}}</text>
              <circle v-if="node.kind === 'class'" class="mark" r="6.5"/>
              <circle v-else-if="node.kind === 'ontology'" class="mark ring" r="6.5"/>
              <polygon v-else-if="node.kind === 'property'" class="mark" points="0,-7 7,0 0,7 -7,0"/>
              <polygon v-else class="mark" :points="HEX"/>
              <circle v-if="selected?.id === node.id" class="select-ring" r="11"/>
              <title>{{node.label}}{{node.uri ? `\n${node.uri}` : ''}}</title>
            </g>
          </svg>
          <div v-else class="ontology-empty">
            <span></span>
            <b>导入 RDF/XML 开始探索</b>
            <p>默认从类关系概览开始。选择一个类后，可以只查看它的字段。</p>
          </div>
          <p v-if="nodes.length && canvasMessage" class="ontology-layer-empty">{{canvasMessage}}</p>
        </div>
        <aside v-if="selected" class="ontology-inspector">
          <div class="ontology-inspector-head">
            <span class="kind"><i :class="selected.kind"></i>{{kindName[selected.kind] || '节点'}}</span>
            <button type="button" aria-label="关闭" @click="selected = null">×</button>
          </div>
          <b>{{selected.label}}</b>
          <small v-if="selected.uri" class="uri">{{selected.uri}}</small>
          <small v-else-if="selected.lang || selected.datatypeLabel" class="uri">{{selected.lang ? `@${selected.lang}` : ''}} {{selected.datatypeLabel}}</small>
          <p class="ontology-rel-count">相关关系 {{relations.length}} 条</p>
          <button v-if="selected.kind === 'class'" type="button" class="ontology-inspector-action" @click="openFields(selected)">查看字段 <span>{{fieldsForClass.length}}</span></button>
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
.ontology-page{color:var(--ui-text);--ont-class:#4d6fe0;--ont-prop:#c4922a;--ont-lit:#a85bc8}
:global(html[data-theme=dark]) .ontology-page{--ont-class:#8aa4ff;--ont-prop:#f0c14a;--ont-lit:#d9a0f0}
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
.ontology-paste textarea{width:100%;box-sizing:border-box;border:1px solid var(--ui-border);border-radius:2px;background:var(--pop-paper,#fffef9);color:var(--ui-text)}
.ontology-paste textarea{min-height:120px;resize:vertical;padding:8px 10px;font:12px/1.6 ui-monospace,SFMono-Regular,monospace}
.ontology-parse{width:100%;min-height:38px;margin:2px 0 4px;cursor:pointer}
.ontology-parse:disabled{opacity:.45;cursor:not-allowed}
.ontology-error{margin:0;padding:8px 10px;border:1px solid var(--ui-danger);border-radius:2px;background:var(--ui-danger-soft);color:var(--ui-danger);font-size:12px;line-height:1.6}
.ontology-legend{display:flex;flex-wrap:wrap;gap:8px 12px;color:var(--ui-muted);font-size:11px}
.ontology-legend span,.ontology-nav button{display:inline-flex;align-items:center;gap:6px}
.ontology-legend i,.ontology-legend-card i,.ontology-nav i,.ontology-inspector .kind i{width:8px;height:8px;flex:none;border:0;border-radius:50%}
.ontology-page i.class,.ontology-node.class .mark{background:var(--ont-class);fill:var(--ont-class)}
.ontology-page i.property,.ontology-node.property .mark{background:var(--ont-prop);fill:var(--ont-prop)}
.ontology-page i.individual,.ontology-node.individual .mark,.ontology-page i.literal,.ontology-node.literal .mark{background:var(--ont-lit);fill:var(--ont-lit)}
.ontology-page :is(i.resource,i.list,i.restriction){background:var(--ui-muted)}
.ontology-node.ontology .mark{fill:none;stroke:var(--ont-prop);stroke-width:1.6}
.ontology-node :is(.resource,.list,.restriction) .mark{fill:var(--ui-muted)}
.ontology-nav{display:grid;gap:8px;min-height:0}
.ontology-nav section{display:grid;gap:4px}
.ontology-nav h3{margin:6px 0 0;color:var(--ui-muted);font-size:11px;font-weight:800;letter-spacing:.08em}
.ontology-nav button{display:flex;width:100%;min-height:30px;padding:4px 6px;border:1px solid transparent;background:transparent;color:var(--ui-text);text-align:left;cursor:pointer;box-shadow:none}
.ontology-nav button span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ontology-nav button em{margin-left:auto;color:var(--ont-class);font-size:10px;font-style:normal;opacity:.8}
.ontology-nav button.active{border-color:var(--ui-border);background:var(--pop-yellow);color:var(--pop-ink,#252526)}
.ontology-stage{position:relative;min-width:0;min-height:0;display:flex;flex-direction:column}
.ontology-stat,.ontology-legend-card,.ontology-dock{position:absolute;z-index:3;border:1px solid color-mix(in srgb,var(--ui-text) 8%,transparent);border-radius:10px;background:color-mix(in srgb,var(--pop-paper,#fffef9) 94%,transparent);box-shadow:0 8px 24px color-mix(in srgb,var(--ui-text) 7%,transparent)}
.ontology-stat{top:16px;left:16px;display:flex;align-items:center;gap:10px;height:40px;padding:0 14px;color:var(--ui-text);font-size:13px;font-weight:600}
.ontology-stat b{margin-left:4px;color:var(--ont-class);font-weight:700}
.ontology-stat b.prop{color:var(--ont-prop)}
.ontology-stat b.rel{color:var(--ont-lit)}
.ontology-stat>i{width:3px;height:3px;border-radius:50%;background:var(--ui-muted)}
.ontology-legend-card{top:16px;right:16px;display:grid;gap:2px;min-width:188px;padding:8px 12px}
.ontology-legend-card>span,.ontology-legend-card button{display:flex;align-items:center;gap:8px;min-height:28px;color:var(--ui-text);font-size:13px;font-weight:600}
.ontology-mode-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:3px;padding:3px;border-radius:8px;background:color-mix(in srgb,var(--ui-text) 6%,transparent)}
.ontology-mode-tabs button{justify-content:center;min-height:28px;padding:0 8px;border-radius:6px;color:var(--ui-muted);font-size:12px;font-weight:700}
.ontology-mode-tabs button.active{background:var(--pop-yellow);color:var(--pop-ink,#252526);box-shadow:0 2px 7px color-mix(in srgb,var(--ui-text) 10%,transparent)}
.ontology-mode-note{max-width:220px;margin:5px 2px 3px;color:var(--ui-muted);font-size:11px;line-height:1.45}
.ontology-legend-card button{width:100%;padding:0;border:0;border-radius:6px;background:transparent;box-shadow:none;text-align:left;cursor:pointer}
.ontology-mode-tabs button{width:auto}
.ontology-legend-card button span{flex:1}
.ontology-legend-card button b{color:var(--ui-muted);font-size:11px;font-weight:650;font-variant-numeric:tabular-nums}
.ontology-mode-tabs button span{flex:none}
.ontology-legend-card button.off,.ontology-legend-card button:disabled{opacity:.38}
.ontology-legend-card button:disabled{cursor:default}
.ontology-legend-card>span.rule{display:block;min-height:0;height:1px;margin:4px 2px;background:color-mix(in srgb,var(--ui-text) 12%,transparent)}
.ontology-legend-card .eye{display:inline-block;width:15px;height:10px;border:1.5px solid currentColor;border-radius:10px;position:relative;font-style:normal}
.ontology-legend-card .eye::before{content:"";position:absolute;left:4px;top:1.5px;width:5px;height:5px;border-radius:50%;background:currentColor}
.ontology-dock{left:50%;bottom:14px;translate:-50% 0;display:flex;align-items:center;gap:2px;height:40px;padding:4px}
.ontology-dock input{width:168px;height:32px;margin:0;padding:0 10px;border:0;border-radius:8px;background:color-mix(in srgb,var(--ui-text) 5%,transparent);color:var(--ui-text);font:600 12px Inter,"PingFang SC","Microsoft YaHei",sans-serif}
.ontology-dock input:focus{outline:none;background:var(--pop-paper,#fffef9)}
.ontology-btn{min-height:32px;padding:0 10px;border:1px solid transparent;border-radius:8px;background:transparent;color:var(--ui-text);box-shadow:none;font-size:12px;font-weight:600;cursor:pointer}
.ontology-btn:hover,.ontology-nav button:hover{background:color-mix(in srgb,var(--pop-cyan) 55%,var(--pop-paper,#fffef9));color:var(--pop-ink,#252526)}
.ontology-btn:disabled{opacity:.45;cursor:not-allowed}
.ontology-graph{position:relative;flex:1;min-height:0;overflow:hidden;background:radial-gradient(ellipse 88% 52% at 50% 46%,color-mix(in srgb,var(--ont-class) 10%,transparent),transparent 70%),var(--pop-paper,#fffef9);cursor:grab;touch-action:none;outline:none}
.ontology-graph.panning,.ontology-node:active{cursor:grabbing}
.ontology-graph.empty{cursor:default}
.ontology-svg{position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none}
.field{stroke-width:1.25;pointer-events:none;filter:url(#ontology-floor)}
.field.property{fill:color-mix(in srgb,var(--ont-prop) 20%,var(--pop-paper,#fffef9));stroke:color-mix(in srgb,var(--ont-prop) 72%,transparent)}
.field.class{fill:color-mix(in srgb,var(--ont-class) 16%,var(--pop-paper,#fffef9));stroke:color-mix(in srgb,var(--ont-class) 64%,transparent)}
.field.individual{fill:color-mix(in srgb,var(--ont-lit) 16%,var(--pop-paper,#fffef9));stroke:color-mix(in srgb,var(--ont-lit) 64%,transparent)}
.field-ring{fill:none;stroke-width:1;stroke-dasharray:2 7;pointer-events:none}
.field-ring.property{stroke:color-mix(in srgb,var(--ont-prop) 58%,transparent)}
.field-ring.class{stroke:color-mix(in srgb,var(--ont-class) 50%,transparent)}
.field-ring.individual{stroke:color-mix(in srgb,var(--ont-lit) 50%,transparent)}
.field-label{font:600 12px Inter,"PingFang SC","Microsoft YaHei",sans-serif;pointer-events:none}
.field-label.property{fill:var(--ont-prop)}
.field-label.class{fill:var(--ont-class)}
.field-label.individual{fill:var(--ont-lit)}
.ontology-axis{fill:none;stroke:color-mix(in srgb,var(--ui-text) 32%,transparent);stroke-width:1;stroke-dasharray:2 6;pointer-events:none}
.ontology-edge .hit{fill:none;stroke:transparent;stroke-width:14;pointer-events:stroke}
.ontology-edge .line{fill:none;stroke:var(--ont-class);stroke-width:1.35;stroke-linecap:round;stroke-dasharray:1.5 6;pointer-events:none}
.ontology-edge.property .line{stroke:var(--ont-prop);stroke-dasharray:5 4}
.ontology-edge.literal .line,.ontology-edge.individual .line,.ontology-edge.restriction .line{stroke:var(--ont-lit)}
.ontology-edge.on .line{stroke-width:1.8;stroke-dasharray:none}
.ontology-edge.off{opacity:.12}
.ontology-edge text{fill:var(--ui-muted);stroke:var(--pop-paper,#fffef9);stroke-width:5px;paint-order:stroke;font:500 11px Inter,"PingFang SC","Microsoft YaHei",sans-serif;text-anchor:middle;pointer-events:none}
.ontology-node{cursor:grab;user-select:none}
.ontology-node .hit{fill:transparent}
.ontology-node .label{fill:var(--ont-class);stroke:color-mix(in srgb,var(--pop-paper,#fffef9) 88%,transparent);stroke-width:4px;paint-order:stroke;font:600 12px Inter,"PingFang SC","Microsoft YaHei",sans-serif;pointer-events:none}
.ontology-node.property .label{fill:var(--ont-prop)}
.ontology-node.literal .label,.ontology-node.individual .label,.ontology-node.restriction .label{fill:var(--ont-lit)}
.ontology-node.ontology .label,.ontology-node.resource .label{fill:var(--ui-text)}
.ontology-node .mark{filter:drop-shadow(0 2px 1px color-mix(in srgb,var(--ui-text) 16%,transparent))}
.ontology-node .select-ring{fill:none;stroke:#b43358;stroke-width:1.3}
.ontology-node.back:not(.selected):not(.off){opacity:.62}
.ontology-node.off{opacity:.16}
.ontology-layer-empty{position:absolute;inset:0;z-index:2;display:grid;place-items:center;margin:0;padding:24px;color:var(--ui-muted);font-size:13px;text-align:center;pointer-events:none}
.ontology-empty{position:absolute;inset:0;display:grid;place-items:center;align-content:center;gap:8px;padding:24px;text-align:center}
.ontology-empty span{width:72px;height:72px;margin:0 auto 6px;border:1px solid color-mix(in srgb,var(--ont-class) 55%,transparent);border-radius:50%;box-shadow:0 0 0 10px color-mix(in srgb,var(--ont-class) 12%,transparent)}
.ontology-empty b{color:var(--ui-text);font-size:16px;font-weight:650}
.ontology-empty p{max-width:340px;margin:0;color:var(--ui-muted);font-size:12px;line-height:1.7}
.ontology-inspector-action:hover{background:color-mix(in srgb,var(--ont-class) 16%,var(--pop-paper,#fffef9))}
.ontology-inspector{position:absolute;top:auto;right:16px;bottom:64px;z-index:4;width:min(300px,calc(100% - 32px));max-height:min(420px,calc(100% - 300px));overflow:auto;display:grid;align-content:start;gap:6px;padding:14px;border:1px solid color-mix(in srgb,var(--ui-text) 12%,var(--ui-border));border-radius:12px;background:color-mix(in srgb,var(--pop-paper,#fffef9) 94%,transparent);box-shadow:0 16px 40px color-mix(in srgb,var(--ui-text) 8%,transparent)}
.ontology-inspector-head{display:flex;justify-content:space-between;align-items:center;gap:8px}
.ontology-inspector .kind{display:inline-flex;align-items:center;gap:6px;color:var(--ui-muted);font-size:12px;font-weight:650}
.ontology-inspector b{font-size:14px;line-height:1.5;overflow-wrap:anywhere}
.ontology-inspector .uri,.ontology-rel-count{color:var(--ui-muted);font-size:11px;line-height:1.5;overflow-wrap:anywhere}
.ontology-inspector-action{display:flex;align-items:center;justify-content:space-between;width:100%;padding:7px 9px;border:1px solid color-mix(in srgb,var(--ont-class) 30%,var(--ui-border));border-radius:6px;background:color-mix(in srgb,var(--ont-class) 8%,var(--pop-paper,#fffef9));color:var(--ont-class);font-size:12px;font-weight:700;text-align:left;cursor:pointer}
.ontology-inspector-action span{font-variant-numeric:tabular-nums}
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
  .ontology-stat,.ontology-legend-card{left:12px;right:12px;width:auto}
  .ontology-legend-card{top:64px}
  .ontology-dock{left:12px;right:12px;translate:0;width:auto}
  .ontology-dock input{width:100%}
  .ontology-inspector{top:auto;bottom:64px;left:10px;right:10px;width:auto;max-height:42%}
}
</style>
