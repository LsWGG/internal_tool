// RDF/XML → 语义图。只保留图谱需要的三元组，不展开具体化（reification）。

const RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
const RDFS = 'http://www.w3.org/2000/01/rdf-schema#'
const OWL = 'http://www.w3.org/2002/07/owl#'
const XSD = 'http://www.w3.org/2001/XMLSchema#'
const XML = 'http://www.w3.org/XML/1998/namespace'
const XMLNS = 'http://www.w3.org/2000/xmlns/'
const SKOS = 'http://www.w3.org/2004/02/skos/core#'
const DC = 'http://purl.org/dc/elements/1.1/'
const DCTERMS = 'http://purl.org/dc/terms/'

const PREFIXES = [
  [RDF, 'rdf'], [RDFS, 'rdfs'], [OWL, 'owl'], [XSD, 'xsd'],
  [SKOS, 'skos'], [DC, 'dc'], [DCTERMS, 'dcterms'],
  ['http://xmlns.com/foaf/0.1/', 'foaf'],
]
const RDF_SYNTAX = new Set(['about', 'ID', 'nodeID', 'resource', 'parseType', 'datatype', 'bagID', 'li', 'aboutEach', 'aboutEachPrefix'])
const ANNOTATION_URIS = new Set([
  RDFS + 'label', RDFS + 'comment', RDFS + 'seeAlso', RDFS + 'isDefinedBy',
  OWL + 'versionInfo', OWL + 'deprecated',
  SKOS + 'prefLabel', SKOS + 'altLabel', SKOS + 'definition', SKOS + 'note',
  DC + 'title', DC + 'description', DC + 'creator',
  DCTERMS + 'title', DCTERMS + 'description', DCTERMS + 'creator',
])
const LABEL_URIS = [RDFS + 'label', SKOS + 'prefLabel', DC + 'title', DCTERMS + 'title', 'http://xmlns.com/foaf/0.1/name']
const META_TYPE_URIS = new Set([
  RDF + 'Property', RDFS + 'Class', RDFS + 'Resource', RDFS + 'Datatype',
  OWL + 'Class', OWL + 'Ontology', OWL + 'Thing', OWL + 'Nothing',
  OWL + 'ObjectProperty', OWL + 'DatatypeProperty', OWL + 'AnnotationProperty',
  OWL + 'FunctionalProperty', OWL + 'InverseFunctionalProperty', OWL + 'TransitiveProperty',
  OWL + 'SymmetricProperty', OWL + 'AsymmetricProperty', OWL + 'ReflexiveProperty', OWL + 'IrreflexiveProperty',
  OWL + 'Restriction', OWL + 'NamedIndividual', OWL + 'DeprecatedClass', OWL + 'DeprecatedProperty',
])
const CLASS_TYPES = new Set([OWL + 'Class', RDFS + 'Class', OWL + 'DeprecatedClass'])
const PROP_TYPES = new Set([
  RDF + 'Property', OWL + 'ObjectProperty', OWL + 'DatatypeProperty', OWL + 'AnnotationProperty',
  OWL + 'FunctionalProperty', OWL + 'InverseFunctionalProperty', OWL + 'TransitiveProperty',
  OWL + 'SymmetricProperty', OWL + 'AsymmetricProperty', OWL + 'ReflexiveProperty', OWL + 'IrreflexiveProperty',
  OWL + 'DeprecatedProperty',
])
const CLASS_SUBJECT = new Set([RDFS + 'subClassOf', OWL + 'equivalentClass', OWL + 'disjointWith', OWL + 'complementOf', OWL + 'intersectionOf', OWL + 'unionOf', OWL + 'oneOf'])
const PROP_SUBJECT = new Set([RDFS + 'domain', RDFS + 'range', RDFS + 'subPropertyOf', OWL + 'inverseOf', OWL + 'equivalentProperty'])
const CLASS_OBJECT = new Set([RDFS + 'subClassOf', OWL + 'equivalentClass', OWL + 'disjointWith', OWL + 'complementOf', OWL + 'someValuesFrom', OWL + 'allValuesFrom'])
const PROP_OBJECT = new Set([RDFS + 'subPropertyOf', OWL + 'inverseOf', OWL + 'onProperty', OWL + 'equivalentProperty'])
const KIND_ORDER = ['ontology', 'class', 'list', 'property', 'restriction', 'individual', 'resource', 'literal']

export function shortUri(uri = '') {
  const raw = String(uri)
  if (!raw || raw.startsWith('_:')) return raw
  for (const [ns, prefix] of PREFIXES) if (raw.startsWith(ns)) return `${prefix}:${raw.slice(ns.length)}`
  const clean = raw.replace(/[/#]+$/, '')
  return clean.split(/[/#]/).pop() || raw
}

export function parseRdfXml(text) {
  const source = String(text || '').replace(/^﻿/, '').trim()
  if (!source) throw new Error('没有可解析的 RDF 内容。')
  if (!source.includes('<')) {
    if (/^(@prefix|prefix\s|@base)\b/i.test(source)) throw new Error('这像是 Turtle，当前只支持 RDF/XML。')
    throw new Error('无法解析 RDF/XML。当前只支持 RDF/XML。')
  }
  if (typeof DOMParser === 'undefined') throw new Error('当前环境无法解析 XML。')
  const xml = new DOMParser().parseFromString(source, 'application/xml')
  if (!xml || xml.querySelector?.('parsererror')) throw new Error('无法解析 RDF/XML；请确认文件是有效的 RDF 文档。')
  return parseRdfDocument(xml)
}

export function parseRdfDocument(xml) {
  const root = xml?.documentElement
  if (!root || root.localName === 'parsererror') throw new Error('无法解析 RDF/XML；请确认文件是有效的 RDF 文档。')
  const triples = []
  const seen = new Set()
  const blankIds = new WeakMap()
  const liCount = new Map()
  let blankN = 0

  function blank() {return `_:b${++blankN}`}
  function attrsOf(el) {return el?.attributes ? [...el.attributes] : []}
  function attrByLocal(el, local) {
    return attrsOf(el).find(attr => attr.localName === local || attr.name === local || attr.name === `rdf:${local}` || attr.name === `xml:${local}`)?.value || ''
  }
  function elements(el) {
    const list = el?.children ? [...el.children] : [...(el?.childNodes || [])]
    return list.filter(node => node?.nodeType === 1)
  }
  function resolveUri(uri, base) {
    if (!uri || uri.startsWith('_:')) return uri || ''
    if (!base) return uri
    try {return new URL(uri, base).href} catch {return uri}
  }
  function baseOf(el, ctx) {
    const own = attrByLocal(el, 'base')
    return own ? resolveUri(own, ctx.base || '') : (ctx.base || '')
  }
  function add(triple) {
    if (!triple.s || !triple.o) return
    const key = `${triple.s}\n${triple.pUri}\n${triple.o}\n${triple.lang || ''}`
    if (seen.has(key)) return
    seen.add(key)
    triples.push(triple)
  }
  function addResource(s, pred, o) {
    add({s, o, pUri: pred.uri, pLabel: shortUri(pred.uri), oLabel: shortUri(o), resource: true, lang: '', datatype: ''})
  }
  function addLiteral(s, pred, lexical, lang, datatype) {
    const value = String(lexical)
    add({
      s, o: `lit:${datatype || ''}:${lang || ''}:${value}`, pUri: pred.uri, pLabel: shortUri(pred.uri),
      oLabel: value, resource: false, lang: lang || '', datatype: datatype || '',
    })
  }
  function nodeRef(el, ctx) {
    const base = baseOf(el, ctx)
    const about = attrByLocal(el, 'about')
    if (about) return resolveUri(about, base)
    const id = attrByLocal(el, 'ID')
    if (id) return resolveUri(`#${id}`, base)
    const nodeID = attrByLocal(el, 'nodeID')
    if (nodeID) return `_:${nodeID}`
    if (!blankIds.has(el)) blankIds.set(el, blank())
    return blankIds.get(el)
  }
  function predicateOf(subject, prop) {
    if (prop.localName === 'li' && (prop.namespaceURI === RDF || !prop.namespaceURI)) {
      const n = (liCount.get(subject) || 0) + 1
      liCount.set(subject, n)
      return {uri: `${RDF}_${n}`, local: `_${n}`}
    }
    return {uri: prop.namespaceURI ? prop.namespaceURI + prop.localName : prop.localName, local: prop.localName}
  }
  function propertyAttrs(el, sid, base, lang) {
    for (const attr of attrsOf(el)) {
      const ns = attr.namespaceURI || ''
      const local = attr.localName || ''
      const name = attr.name || ''
      if (!local || ns === XML || ns === XMLNS || name.startsWith('xmlns')) continue
      if ((ns === RDF || !ns) && RDF_SYNTAX.has(local)) continue
      const pred = {uri: ns ? ns + local : local, local}
      if (ns === RDF && local === 'type') addResource(sid, pred, resolveUri(attr.value, base))
      else addLiteral(sid, pred, attr.value, lang, '')
    }
  }
  function walkNode(el, ctx) {
    if (!el || el.nodeType !== 1) return
    const base = baseOf(el, ctx)
    const lang = attrByLocal(el, 'lang') || ctx.lang || ''
    const sid = nodeRef(el, ctx)
    const syntactic = el.namespaceURI === RDF && (el.localName === 'Description' || el.localName === 'RDF')
    if (!syntactic && el.namespaceURI) addResource(sid, {uri: RDF + 'type', local: 'type'}, el.namespaceURI + el.localName)
    propertyAttrs(el, sid, base, lang)
    const next = {base, lang}
    for (const child of elements(el)) walkProperty(sid, child, next)
  }
  function walkProperty(subject, prop, ctx) {
    const pred = predicateOf(subject, prop)
    const base = baseOf(prop, ctx)
    const lang = attrByLocal(prop, 'lang') || ctx.lang || ''
    const resource = attrByLocal(prop, 'resource')
    const nodeID = attrByLocal(prop, 'nodeID')
    const parseType = attrByLocal(prop, 'parseType').toLowerCase()
    const datatype = attrByLocal(prop, 'datatype')
    if (resource) return addResource(subject, pred, resolveUri(resource, base))
    if (nodeID) return addResource(subject, pred, `_:${nodeID}`)
    if (parseType === 'resource') {
      const id = blank()
      addResource(subject, pred, id)
      const childCtx = {base, lang}
      for (const child of elements(prop)) walkProperty(id, child, childCtx)
      return
    }
    if (parseType === 'literal') return addLiteral(subject, pred, prop.textContent ?? '', lang, datatype ? resolveUri(datatype, base) : '')
    if (parseType === 'collection') {
      const items = elements(prop)
      if (!items.length) return addResource(subject, pred, RDF + 'nil')
      let current = blank()
      addResource(subject, pred, current)
      items.forEach((item, index) => {
        const oid = nodeRef(item, {base, lang})
        walkNode(item, {base, lang})
        addResource(current, {uri: RDF + 'first', local: 'first'}, oid)
        if (index < items.length - 1) {
          const next = blank()
          addResource(current, {uri: RDF + 'rest', local: 'rest'}, next)
          current = next
        } else addResource(current, {uri: RDF + 'rest', local: 'rest'}, RDF + 'nil')
      })
      return
    }
    const kids = elements(prop)
    if (kids.length) {
      for (const kid of kids) {
        addResource(subject, pred, nodeRef(kid, {base, lang}))
        walkNode(kid, {base, lang})
      }
      return
    }
    const text = prop.textContent?.trim() ?? ''
    if (text) addLiteral(subject, pred, text, lang, datatype ? resolveUri(datatype, base) : '')
  }

  const rootIsRdf = root.localName === 'RDF'
  const ctx = rootIsRdf ? {base: attrByLocal(root, 'base'), lang: attrByLocal(root, 'lang')} : {base: '', lang: ''}
  for (const node of (rootIsRdf ? elements(root) : [root])) walkNode(node, ctx)
  if (!triples.length) throw new Error('未发现 RDF 三元组。请确认文件是 RDF/XML，而不是 Turtle 或 JSON-LD。')
  return triples
}

function indexTriples(triples) {
  const bySubject = new Map()
  const byObject = new Map()
  for (const triple of triples) {
    if (!bySubject.has(triple.s)) bySubject.set(triple.s, [])
    if (!byObject.has(triple.o)) byObject.set(triple.o, [])
    bySubject.get(triple.s).push(triple)
    byObject.get(triple.o).push(triple)
  }
  return {bySubject, byObject}
}

function hasPred(list, uris) {
  return !!list?.some(triple => uris.has(triple.pUri))
}

function kindOf(id, bySubject, byObject) {
  if (id.startsWith('lit:')) return 'literal'
  const outgoing = bySubject.get(id) || []
  const incoming = byObject.get(id) || []
  const types = outgoing.filter(triple => triple.pUri === RDF + 'type' && triple.resource).map(triple => triple.o)
  if (types.includes(OWL + 'Ontology')) return 'ontology'
  if (types.some(type => CLASS_TYPES.has(type))) return 'class'
  if (types.includes(OWL + 'Restriction')) return 'restriction'
  if (types.some(type => PROP_TYPES.has(type))) return 'property'
  if (outgoing.some(triple => triple.pUri === RDF + 'first')) return 'list'
  if (hasPred(outgoing, PROP_SUBJECT)) return 'property'
  if (hasPred(outgoing, CLASS_SUBJECT)) return 'class'
  if (hasPred(incoming, PROP_OBJECT)) return 'property'
  if (hasPred(incoming, CLASS_OBJECT)) return 'class'
  if (!outgoing.length) return 'resource'
  return 'individual'
}

function literalPreview(text) {
  const raw = String(text || '').trim()
  if (!raw.startsWith('{') && !raw.startsWith('[')) return raw
  try {
    const data = JSON.parse(raw)
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const bits = [data.source_path, data.source_name || data.source].filter(Boolean)
      if (bits.length) return bits.slice(0, 2).join(' · ')
    }
  } catch { /* 普通字面量，保持原文 */ }
  return raw
}

function nodeLabel(id, kind, outgoing, triples) {
  if (id.startsWith('lit:')) return outgoing.find(triple => triple.o === id)?.oLabel || triples.find(triple => triple.o === id)?.oLabel || ''
  for (const uri of LABEL_URIS) {
    const hit = outgoing.find(triple => triple.pUri === uri && !triple.resource)
    if (hit) return hit.oLabel
  }
  if (kind === 'list') return '列表'
  if (kind === 'restriction') {
    const on = outgoing.find(triple => triple.pUri === OWL + 'onProperty')
    return on ? `限制·${shortUri(on.o)}` : '限制'
  }
  if (id.startsWith('_:')) {
    const type = outgoing.find(triple => triple.pUri === RDF + 'type' && triple.resource && !META_TYPE_URIS.has(triple.o))
    if (type) return shortUri(type.o)
    if (/^_:b\d+$/.test(id)) return `空白${id.slice(3)}`
    return id.slice(2)
  }
  return shortUri(id)
}

function charWidth(ch) {return ch.charCodeAt(0) > 255 ? 13 : 7.4}

function shorten(label) {
  const chars = [...String(label || '—')]
  let width = 0
  let count = 0
  for (const ch of chars) {
    if (width + charWidth(ch) > 168) break
    width += charWidth(ch)
    count += 1
  }
  if (count >= chars.length) return {text: chars.join('') || '—', width}
  const text = `${chars.slice(0, Math.max(1, count - 1)).join('')}…`
  return {text, width: [...text].reduce((sum, ch) => sum + charWidth(ch), 0)}
}

function hiddenTriple(triple, options) {
  const annotation = ANNOTATION_URIS.has(triple.pUri)
  if (!triple.resource) {
    if (annotation) return options.annotations !== true
    return options.literals !== true
  }
  if (options.metaTypes !== true && triple.pUri === RDF + 'type' && META_TYPE_URIS.has(triple.o)) return true
  // xsd:string 这类共用数据类型会变成星形枢纽，默认收进详情
  if (options.metaTypes !== true && triple.o.startsWith(XSD)) return true
  return false
}

export function buildOntologyGraph(triples, options = {}) {
  const visible = triples.filter(triple => !hiddenTriple(triple, options))
  const ids = new Set()
  for (const triple of triples) ids.add(triple.s)
  for (const triple of visible) ids.add(triple.o)
  const {bySubject, byObject} = indexTriples(triples)
  const nodes = [...ids].filter(Boolean).map(id => {
    const outgoing = bySubject.get(id) || []
    const kind = kindOf(id, bySubject, byObject)
    const literal = id.startsWith('lit:') ? triples.find(triple => triple.o === id) : null
    const label = nodeLabel(id, kind, outgoing, triples) || shortUri(id)
    const fitted = shorten(kind === 'literal' ? literalPreview(label) : label)
    return {
      id, kind, label, short: fitted.text, uri: id.startsWith('lit:') ? '' : id,
      lexical: literal?.oLabel || '', lang: literal?.lang || '',
      datatypeLabel: literal?.datatype ? shortUri(literal.datatype) : '',
      w: Math.max(88, Math.min(196, fitted.width + 28)), h: 34,
    }
  })
  const present = new Set(nodes.map(node => node.id))
  const edges = visible.filter(triple => present.has(triple.s) && present.has(triple.o)).map((triple, index) => ({
    id: index, s: triple.s, o: triple.o, label: triple.pLabel, pUri: triple.pUri,
  }))
  return {nodes, edges, folded: triples.length - visible.length}
}

export function canvasEdges(nodes, edges = []) {
  const ids = new Set(nodes.map(node => node.id))
  return edges.filter(edge => ids.has(edge.s) && ids.has(edge.o))
}

function overlaps(a, b, padX = 22, padY = 14) {
  return Math.abs(a.x - b.x) < (a.w + b.w) / 2 + padX && Math.abs(a.y - b.y) < (a.h + b.h) / 2 + padY
}

export function layoutOntology(nodes, edges = []) {
  const list = nodes.map(node => ({...node, card: null}))
  const byId = new Map(list.map(node => [node.id, node]))
  for (const node of list) if (!KIND_ORDER.includes(node.kind)) node.kind = 'individual'
  const count = list.length
  if (!count) return []
  const ring = 140 + count * 12
  list.forEach((node, index) => {
    const turn = (index / count) * Math.PI * 2 - Math.PI / 2
    const inner = node.kind === 'class' || node.kind === 'ontology' ? 0.15 : 1
    node.x = Math.cos(turn) * ring * inner
    node.y = Math.sin(turn) * ring * inner
    node.vx = 0
    node.vy = 0
  })
  const links = edges.filter(edge => edge && edge.s !== edge.o && byId.has(edge.s) && byId.has(edge.o))
  const ideal = 156
  const steps = Math.min(280, 140 + count * 3)
  for (let step = 0; step < steps; step += 1) {
    for (let i = 0; i < count; i += 1) {
      for (let j = i + 1; j < count; j += 1) {
        const a = list[i]
        const b = list[j]
        const dx = a.x - b.x || 0.5
        const dy = a.y - b.y || 0.3
        const dist = Math.hypot(dx, dy) || 1
        const force = 16000 / (dist * dist)
        a.vx += (dx / dist) * force
        a.vy += (dy / dist) * force
        b.vx -= (dx / dist) * force
        b.vy -= (dy / dist) * force
      }
    }
    for (const edge of links) {
      const a = byId.get(edge.s)
      const b = byId.get(edge.o)
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.hypot(dx, dy) || 1
      const mag = (dist - ideal) * 0.06
      a.vx += (dx / dist) * mag
      a.vy += (dy / dist) * mag
      b.vx -= (dx / dist) * mag
      b.vy -= (dy / dist) * mag
    }
    for (const node of list) {
      node.vx = (node.vx - node.x * 0.0015) * 0.78
      node.vy = (node.vy - node.y * 0.0015) * 0.78
      node.x += Math.max(-40, Math.min(40, node.vx))
      node.y += Math.max(-40, Math.min(40, node.vy))
    }
  }
  for (let pass = 0; pass < 48; pass += 1) {
    let moved = false
    for (let i = 0; i < count; i += 1) {
      for (let j = i + 1; j < count; j += 1) {
        const a = list[i]
        const b = list[j]
        if (!overlaps(a, b, 8, 8)) continue
        const dx = a.x - b.x || 0.5
        const dy = a.y - b.y || 0.3
        const gapX = (a.w + b.w) / 2 + 16
        const gapY = (a.h + b.h) / 2 + 12
        const overlapX = gapX - Math.abs(dx)
        const overlapY = gapY - Math.abs(dy)
        if (overlapX < overlapY) {
          const sign = dx >= 0 ? 1 : -1
          a.x += sign * overlapX / 2
          b.x -= sign * overlapX / 2
        } else {
          const sign = dy >= 0 ? 1 : -1
          a.y += sign * overlapY / 2
          b.y -= sign * overlapY / 2
        }
        moved = true
      }
    }
    if (!moved) break
  }
  const degree = new Map(list.map(node => [node.id, 0]))
  for (const edge of links) {
    degree.set(edge.s, (degree.get(edge.s) || 0) + 1)
    degree.set(edge.o, (degree.get(edge.o) || 0) + 1)
  }
  const linked = list.filter(node => degree.get(node.id))
  if (linked.length && linked.length < list.length) {
    const cx = linked.reduce((sum, node) => sum + node.x, 0) / linked.length
    const floor = Math.max(...linked.map(node => node.y + (node.h || 34) / 2))
    let row = 0
    for (const node of list) {
      if (degree.get(node.id)) continue
      node.x = cx + (row % 4) * ((node.w || 88) + 20)
      node.y = floor + 64 + Math.floor(row / 4) * 48
      row += 1
    }
  }
  for (const node of list) {
    delete node.vx
    delete node.vy
  }
  return list
}

function num(value) {return Math.round(value * 10) / 10}

function borderPoint(node, tx, ty) {
  const dx = tx - node.x
  const dy = ty - node.y
  if (!dx && !dy) return {x: node.x, y: node.y - node.h / 2}
  const sx = dx ? (node.w / 2) / Math.abs(dx) : Infinity
  const sy = dy ? (node.h / 2) / Math.abs(dy) : Infinity
  const scale = Math.min(sx, sy)
  return {x: node.x + dx * scale, y: node.y + dy * scale}
}

function sideOf(node, other) {
  const dx = other.x - node.x
  const dy = other.y - node.y
  if (Math.abs(dx) * (node.h || 34) >= Math.abs(dy) * (node.w || 88)) return dx >= 0 ? 'right' : 'left'
  return dy >= 0 ? 'bottom' : 'top'
}

function port(node, side, slot = 0, count = 1) {
  const vertical = side === 'left' || side === 'right'
  const span = vertical ? (node.h || 34) : (node.w || 88)
  const usable = Math.max(6, span - 8)
  const t = count <= 1 ? 0.5 : (slot + 1) / (count + 1)
  const along = -usable / 2 + usable * t
  if (side === 'right') return {x: node.x + node.w / 2, y: node.y + along}
  if (side === 'left') return {x: node.x - node.w / 2, y: node.y + along}
  if (side === 'bottom') return {x: node.x + along, y: node.y + node.h / 2}
  return {x: node.x + along, y: node.y - node.h / 2}
}

function curveBetween(start, end, spread) {
  if (!spread && Math.abs(start.y - end.y) <= 8) {
    return {d: `M ${num(start.x)} ${num(start.y)} L ${num(end.x)} ${num(end.y)}`, lx: num((start.x + end.x) / 2), ly: num((start.y + end.y) / 2 - 8)}
  }
  const dx = end.x - start.x
  const dy = end.y - start.y
  const len = Math.hypot(dx, dy) || 1
  const bow = spread || Math.min(16, len * 0.06)
  const cx = (start.x + end.x) / 2 + (-dy / len) * bow
  const cy = (start.y + end.y) / 2 + (dx / len) * bow
  return {
    d: `M ${num(start.x)} ${num(start.y)} Q ${num(cx)} ${num(cy)} ${num(end.x)} ${num(end.y)}`,
    lx: num(start.x * 0.25 + cx * 0.5 + end.x * 0.25),
    ly: num(start.y * 0.25 + cy * 0.5 + end.y * 0.25 - 6),
  }
}

function curveOf(a, b, spread) {
  if (a.id === b.id) {
    const x = a.x + a.w / 2
    return {
      d: `M ${num(x)} ${num(a.y - 8)} C ${num(x + 64)} ${num(a.y - 46)}, ${num(x + 64)} ${num(a.y + 46)}, ${num(x)} ${num(a.y + 8)}`,
      lx: num(x + 70), ly: num(a.y),
    }
  }
  if (Math.abs(a.x - b.x) < 4) {
    const extra = 54 + Math.abs(spread)
    const x1 = a.x - a.w / 2
    const x2 = b.x - b.w / 2
    const cpx = x1 - extra
    return {
      d: `M ${num(x1)} ${num(a.y)} C ${num(cpx)} ${num(a.y)}, ${num(cpx)} ${num(b.y)}, ${num(x2)} ${num(b.y)}`,
      lx: num(cpx), ly: num((a.y + b.y) / 2),
    }
  }
  const start = borderPoint(a, b.x, b.y)
  const end = borderPoint(b, a.x, a.y)
  const from = a.id <= b.id ? a : b
  const to = a.id <= b.id ? b : a
  const dx = to.x - from.x
  const dy = to.y - from.y
  const len = Math.hypot(dx, dy) || 1
  if (!spread) return {d: `M ${num(start.x)} ${num(start.y)} L ${num(end.x)} ${num(end.y)}`, lx: num((start.x + end.x) / 2), ly: num((start.y + end.y) / 2 - 8)}
  const cx = (start.x + end.x) / 2 + (-dy / len) * spread
  const cy = (start.y + end.y) / 2 + (dx / len) * spread
  return {
    d: `M ${num(start.x)} ${num(start.y)} Q ${num(cx)} ${num(cy)} ${num(end.x)} ${num(end.y)}`,
    lx: num(start.x * 0.25 + cx * 0.5 + end.x * 0.25),
    ly: num(start.y * 0.25 + cy * 0.5 + end.y * 0.25 - 6),
  }
}

export function placeEdges(nodes, edges) {
  const map = new Map(nodes.map(node => [node.id, node]))
  const groups = new Map()
  for (const edge of edges) {
    const key = edge.s === edge.o ? `loop\n${edge.s}` : [edge.s, edge.o].sort().join('\n')
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key).push(edge)
  }
  const buckets = new Map()
  const items = []
  for (const group of groups.values()) {
    group.forEach((edge, index) => {
      const a = map.get(edge.s)
      const b = map.get(edge.o)
      if (!a || !b || a.x == null || b.x == null) return
      const spread = (index - (group.length - 1) / 2) * 28
      if (a.id === b.id || Math.abs(a.x - b.x) < 4) {
        items.push({edge, direct: curveOf(a, b, spread)})
        return
      }
      const outSide = sideOf(a, b)
      const inSide = sideOf(b, a)
      const item = {edge, a, b, spread, outSide, inSide, outKey: `${a.id}\n${outSide}`, inKey: `${b.id}\n${inSide}`}
      for (const key of [item.outKey, item.inKey]) {
        if (!buckets.has(key)) buckets.set(key, [])
        buckets.get(key).push(item)
      }
      items.push(item)
    })
  }
  for (const [key, bucket] of buckets) {
    const side = key.split('\n')[1]
    const along = side === 'left' || side === 'right' ? 'y' : 'x'
    const unique = [...new Set(bucket)]
    unique.sort((left, right) => {
      const other = item => (item.outKey === key ? item.b : item.a)[along]
      return other(left) - other(right)
    })
    unique.forEach((item, index) => {
      if (item.outKey === key) {item.outSlot = index; item.outCount = unique.length}
      if (item.inKey === key) {item.inSlot = index; item.inCount = unique.length}
    })
  }
  return items.map(item => {
    if (item.direct) return {...item.edge, ...item.direct}
    const start = port(item.a, item.outSide, item.outSlot, item.outCount)
    const end = port(item.b, item.inSide, item.inSlot, item.inCount)
    return {...item.edge, ...curveBetween(start, end, item.spread)}
  })
}

export function fitGraph(nodes, width, height, edges = []) {
  if (!nodes.length || width < 20 || height < 20) return {x: 40, y: 40, k: 1}
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
  for (const node of nodes) {
    minX = Math.min(minX, node.x - node.w / 2)
    maxX = Math.max(maxX, node.x + node.w / 2)
    minY = Math.min(minY, node.y - node.h / 2)
    maxY = Math.max(maxY, node.y + node.h / 2)
    if (node.card) {
      minX = Math.min(minX, node.card.x)
      maxX = Math.max(maxX, node.card.x + node.card.w)
      minY = Math.min(minY, node.card.y)
      maxY = Math.max(maxY, node.card.y + node.card.h)
    }
  }
  for (const edge of edges) {
    if (edge.lx == null) continue
    minX = Math.min(minX, edge.lx - 48)
    maxX = Math.max(maxX, edge.lx + 48)
    minY = Math.min(minY, edge.ly - 20)
    maxY = Math.max(maxY, edge.ly + 20)
  }
  minX -= 32
  maxX += 32
  minY -= 24
  maxY += 24
  const boxW = Math.max(1, maxX - minX)
  const boxH = Math.max(1, maxY - minY)
  const k = Math.min(1.35, Math.max(0.18, Math.min((width - 24) / boxW, (height - 24) / boxH)))
  return {k, x: (width - boxW * k) / 2 - minX * k, y: (height - boxH * k) / 2 - minY * k}
}
