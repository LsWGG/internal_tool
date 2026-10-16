import test from 'node:test'
import assert from 'node:assert/strict'
import {buildOntologyGraph, canvasEdges, fitGraph, layoutOntology, parseRdfDocument, placeEdges} from '../src/ontologyRdf.js'

const RDF = 'http://www.w3.org/1999/02/22-rdf-syntax-ns#'
const RDFS = 'http://www.w3.org/2000/01/rdf-schema#'
const OWL = 'http://www.w3.org/2002/07/owl#'
const XSD = 'http://www.w3.org/2001/XMLSchema#integer'
const XML = 'http://www.w3.org/XML/1998/namespace'
const EX = 'http://example.org/ns#'

function el(local, ns, attrs = {}, children = [], text = '') {
  const attributes = Object.entries(attrs).map(([key, value]) => {
    if (['about', 'resource', 'nodeID', 'ID', 'datatype', 'parseType'].includes(key)) return {localName: key, namespaceURI: RDF, name: `rdf:${key}`, value: String(value)}
    if (key.startsWith('xml:')) return {localName: key.slice(4), namespaceURI: XML, name: key, value: String(value)}
    if (key.includes(':')) {
      const [prefix, name] = key.split(':')
      const namespaceURI = {rdfs: RDFS, owl: OWL, rdf: RDF}[prefix]
      return {localName: name, namespaceURI, name: key, value: String(value)}
    }
    return {localName: key, namespaceURI: null, name: key, value: String(value)}
  })
  return {
    nodeType: 1, localName: local, namespaceURI: ns, attributes, children,
    textContent: text !== '' && text != null ? String(text) : children.map(child => child.textContent || '').join(''),
  }
}
const doc = root => ({documentElement: root})
const has = (triples, s, pUri, o) => triples.some(triple => triple.s === s && triple.pUri === pUri && triple.o === o)

test('typed nodes, repeated descriptions, nodeID and xml:base stay connected', () => {
  const triples = parseRdfDocument(doc(el('RDF', RDF, {'xml:base': 'http://ex.org/o.rdf'}, [
    el('Class', OWL, {ID: 'Person'}, [
      el('subClassOf', RDFS, {resource: '#Agent'}),
      el('label', RDFS, {}, [], '人'),
    ]),
    el('Class', OWL, {ID: 'Agent'}),
    el('Description', RDF, {about: 'http://ex.org/a'}, [el('name', EX, {}, [], 'A')]),
    el('Description', RDF, {about: 'http://ex.org/a'}, [el('age', EX, {datatype: XSD}, [], '3')]),
    el('Description', RDF, {about: 'http://ex.org/p', 'xml:lang': 'zh'}, [
      el('knows', EX, {nodeID: 'bob'}),
      el('label', RDFS, {}, [], '张三'),
    ]),
    el('Description', RDF, {nodeID: 'bob'}, [el('name', EX, {}, [], 'Bob')]),
  ])))
  const person = 'http://ex.org/o.rdf#Person'
  const agent = 'http://ex.org/o.rdf#Agent'
  assert.equal(has(triples, person, RDFS + 'subClassOf', agent), true)
  assert.equal(has(triples, person, RDF + 'type', OWL + 'Class'), true)
  assert.equal(has(triples, agent, RDF + 'type', OWL + 'Class'), true)
  assert.equal(triples.filter(triple => triple.s === 'http://ex.org/a').map(triple => triple.pLabel).sort().join(), 'age,name')
  assert.equal(triples.find(triple => triple.pLabel === 'age').datatype, XSD)
  assert.equal(has(triples, 'http://ex.org/p', EX + 'knows', '_:bob'), true)
  assert.equal(triples.some(triple => triple.s === 'bob'), false)
  assert.equal(triples.find(triple => triple.s === '_:bob' && triple.oLabel === 'Bob')?.resource, false)
  assert.equal(triples.find(triple => triple.s === 'http://ex.org/p' && triple.pUri === RDFS + 'label').lang, 'zh')
})

test('parseType Resource and Collection become blank-node structure, not the parent', () => {
  const triples = parseRdfDocument(doc(el('RDF', RDF, {}, [
    el('Description', RDF, {about: 'http://ex.org/p'}, [
      el('address', EX, {parseType: 'Resource'}, [el('city', EX, {}, [], 'NYC')]),
    ]),
    el('Class', OWL, {about: 'http://ex.org/A'}, [
      el('unionOf', OWL, {parseType: 'Collection'}, [
        el('Class', OWL, {about: 'http://ex.org/B'}),
        el('Class', OWL, {about: 'http://ex.org/C'}),
      ]),
    ]),
  ])))
  const address = triples.find(triple => triple.s === 'http://ex.org/p' && triple.pUri === EX + 'address')
  assert.equal(address.o, '_:b1')
  assert.equal(has(triples, '_:b1', EX + 'city', triples.find(triple => triple.oLabel === 'NYC').o), true)
  assert.equal(triples.some(triple => triple.s === 'http://ex.org/p' && triple.oLabel === 'NYC'), false)
  assert.equal(has(triples, 'http://ex.org/A', OWL + 'unionOf', '_:b2'), true)
  assert.equal(has(triples, '_:b2', RDF + 'first', 'http://ex.org/B'), true)
  assert.equal(has(triples, '_:b3', RDF + 'first', 'http://ex.org/C'), true)
  assert.equal(has(triples, 'http://ex.org/B', RDF + 'type', OWL + 'Class'), true)
})

test('property attributes and duplicate triples collapse, labels stay on the node', () => {
  const triples = parseRdfDocument(doc(el('RDF', RDF, {}, [
    el('Class', OWL, {about: 'http://ex.org/Person', 'rdfs:label': '人'}, [
      el('subClassOf', RDFS, {resource: 'http://ex.org/Agent'}),
      el('label', RDFS, {}, [], '人'),
    ]),
  ])))
  assert.equal(triples.filter(triple => triple.pUri === RDFS + 'label').length, 1)
  const graph = buildOntologyGraph(triples)
  const person = graph.nodes.find(node => node.uri === 'http://ex.org/Person')
  const agent = graph.nodes.find(node => node.uri === 'http://ex.org/Agent')
  assert.equal(person.kind, 'class')
  assert.equal(person.label, '人')
  assert.equal(agent.kind, 'class')
  assert.equal(graph.nodes.some(node => node.kind === 'literal'), false)
  assert.equal(graph.edges.some(edge => edge.o === OWL + 'Class'), false)
  assert.equal(graph.edges.some(edge => edge.label === 'rdfs:subClassOf'), true)
  assert.ok(graph.folded >= 2)
  const withMeta = buildOntologyGraph(triples, {annotations: true, metaTypes: true})
  assert.equal(withMeta.edges.some(edge => edge.o === OWL + 'Class'), true)
  assert.equal(withMeta.nodes.some(node => node.kind === 'literal' && node.lexical === '人'), true)
})

test('related nodes float apart without overlapping boxes', () => {
  const nodes = [
    {id: 'a', label: '甲', short: '甲', kind: 'class', w: 80, h: 34},
    {id: 'b', label: '乙', short: '乙', kind: 'class', w: 80, h: 34},
    {id: 'c', label: '姓名', short: '姓名', kind: 'property', w: 90, h: 34},
  ]
  const edges = [{id: 0, s: 'a', o: 'b', label: 'rdfs:subClassOf'}, {id: 1, s: 'c', o: 'a', label: 'rdfs:domain'}]
  const laid = layoutOntology(nodes, edges)
  const a = laid.find(node => node.id === 'a')
  const b = laid.find(node => node.id === 'b')
  const c = laid.find(node => node.id === 'c')
  const dist = (p, q) => Math.hypot(p.x - q.x, p.y - q.y)
  const hits = (p, q) => Math.abs(p.x - q.x) < (p.w + q.w) / 2 && Math.abs(p.y - q.y) < (p.h + q.h) / 2
  assert.equal(hits(a, b) || hits(a, c) || hits(b, c), false)
  assert.ok(dist(c, a) < dist(c, b))
  assert.equal(canvasEdges(laid, edges).some(edge => edge.label === 'rdfs:domain'), true)
  const loop = placeEdges(
    [{id: 'a', x: 100, y: 40, w: 80, h: 34}, {id: 'b', x: 100, y: 120, w: 80, h: 34}],
    [{id: 0, s: 'a', o: 'b', label: 'rdfs:subClassOf'}],
  )[0]
  assert.match(loop.d, /C /)
  assert.ok(loop.lx < 60)
  const across = placeEdges(
    [{id: 'a', x: 100, y: 80, w: 80, h: 34}, {id: 'b', x: 360, y: 80, w: 80, h: 34}],
    [{id: 0, s: 'a', o: 'b', label: 'rdfs:domain'}],
  )
  assert.equal(across[0].d, 'M 140 80 L 320 80')
  const frame = fitGraph(laid, 800, 600, placeEdges(laid, edges))
  assert.ok(Number.isFinite(frame.k) && frame.k > 0)
})

test('literals and xsd datatypes stay off the canvas until asked for', () => {
  const XSD_STRING = 'http://www.w3.org/2001/XMLSchema#string'
  const triples = parseRdfDocument(doc(el('RDF', RDF, {}, [
    el('Class', OWL, {about: 'http://ex.org/Warehouse'}),
    el('DatatypeProperty', OWL, {about: 'http://ex.org/capacity'}, [
      el('domain', RDFS, {resource: 'http://ex.org/Warehouse'}),
      el('range', RDFS, {resource: XSD_STRING}),
      el('sourceBinding', EX, {}, [], '{"source":"pg","source_path":"capacity"}'),
    ]),
  ])))
  const graph = buildOntologyGraph(triples)
  assert.equal(graph.nodes.some(node => node.kind === 'literal'), false)
  assert.equal(graph.nodes.some(node => node.id === XSD_STRING), false)
  assert.equal(graph.nodes.some(node => node.kind === 'property'), true)
  const withValues = buildOntologyGraph(triples, {literals: true, metaTypes: true})
  const literal = withValues.nodes.find(node => node.kind === 'literal')
  assert.equal(literal.lexical.includes('source_path'), true)
  assert.equal(literal.short.includes('capacity'), true)
  assert.equal(withValues.nodes.some(node => node.id === XSD_STRING), true)
})
