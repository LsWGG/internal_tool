import test from 'node:test'
import assert from 'node:assert/strict'
import {parse as parseYaml} from 'yaml'
import {parseDocument, sortKeys, treeRows, tableRows, queryDocument, differences, convertDocument, processDocument} from '../src/json-tool/engine.js'

test('strict JSON accepts root primitives and BOM; refuses duplicate keys and precision loss',()=>{
  for(const value of [null,false,0,'文字',[1,2],{a:1}]) assert.deepEqual(parseDocument(JSON.stringify(value)).value,value)
  assert.equal(parseDocument('\uFEFF{"ok":true}').value.ok,true)
  assert.throws(()=>parseDocument('{"a":1,"a":2}'))
  assert.throws(()=>parseDocument('{"a":1,"a":1}'),/重复键/)
  assert.throws(()=>parseDocument('{"__proto__":1,"__proto__":1}'),/重复键/)
  assert.throws(()=>parseDocument('{"a":1,}'))
  assert.throws(()=>parseDocument('{"id":9007199254740993}'),/安全精度/)
  assert.throws(()=>parseDocument('1e400'),/安全精度/)
})
test('prototype-like keys remain own data through sorting and comparison',()=>{
  const value=parseDocument('{"__proto__":{"safe":true},"constructor":0,"a":1}').value
  const sorted=sortKeys(value)
  assert.equal(Object.hasOwn(sorted,'__proto__'),true)
  assert.equal({}.safe,undefined)
  assert.deepEqual(differences(value,sorted).rows,[])
})
test('query supports recursion, filters, slices and literal dotted keys',()=>{
  for(const primitive of [null,false,0]) assert.equal(queryDocument(primitive,'$').rows[0].value,primitive)
  const value={items:[{price:10,title:'a'},{price:30,title:'b'},{price:20,title:'c'}],'a.b':null}
  assert.deepEqual(queryDocument(value,'$.items[?(@.price < 25)].title').rows.map(r=>r.value),['a','c'])
  assert.deepEqual(queryDocument(value,'$..title').rows.map(r=>r.value),['a','b','c'])
  assert.equal(queryDocument(value,'$.items[0:2]').count,2)
  assert.equal(queryDocument(value,"$['a.b']").rows[0].value,null)
  assert.equal(queryDocument(value,'$.missing').count,0)
  assert.throws(()=>queryDocument(value,'items'),/必须以/)
})
test('tree keeps null, empty branches and escaped JSON Pointer paths',()=>{
  const {rows}=treeRows({'a/b':{'~':null},empty:[]})
  assert.equal(rows.find(r=>r.pointer==='/a~1b/~0').type,'null')
  assert.equal(rows.find(r=>r.pointer==='/empty').branch,false)
  assert.equal(treeRows(Array.from({length:6000},(_,i)=>i)).truncated,true)
})
test('diff ignores key order, distinguishes types, missing vs null and array indices',()=>{
  assert.equal(differences({a:1,b:2},{b:2,a:1}).rows.length,0)
  const rows=differences({a:null,items:[1,2],gone:0},{a:false,items:[1,3,4],new:null}).rows
  assert.deepEqual(rows.map(r=>[r.type,r.path]),[['changed','/a'],['changed','/items/1'],['added','/items/2'],['removed','/gone'],['added','/new']])
})
test('conversion round trips YAML and JSONL; CSV quotes values and neutralizes formulas',()=>{
  const value=[{title:'a,"b"\nc',nested:{yes:true},empty:null},{title:'=1+1',extra:2}]
  assert.deepEqual(parseYaml(convertDocument(value,'yaml')),value)
  assert.deepEqual(convertDocument(value,'jsonl').trim().split('\n').map(JSON.parse),value)
  const csv=convertDocument(value,'csv')
  assert.ok(csv.includes('"a,""b""\nc"'))
  assert.ok(csv.includes('"\'=1+1"'))
  assert.throws(()=>convertDocument({a:1},'csv'),/对象数组/)
  assert.throws(()=>convertDocument({},'jsonl'),/根节点为数组/)
})
test('action errors are separate from valid source; table handles heterogeneous rows',()=>{
  assert.equal(processDocument({text:'{}',mode:'diff',right:'invalid'}).type,'object')
  assert.match(processDocument({text:'{}',mode:'convert',format:'csv'}).actionError,/对象数组/)
  assert.deepEqual(tableRows([{a:null},{b:false}]).rows,[['null','—'],['—','false']])
})

test('inline diff ranges follow original properties, array indices and escaped keys',()=>{
  const text='\uFEFF{\n "same":"😀", "a/b":{"~":1}, "items":[1,2], "gone":{"nested":true}\n}'
  const right='{"items":[1,3,4],"a/b":{"~":9},"same":"😀","new":null}'
  const {diff}=processDocument({text,right,mode:'diff',sorted:true})
  const slices=(input,ranges)=>ranges.map(r=>[r.type,r.path,input.slice(r.from,r.to)])
  assert.deepEqual(slices(text,diff.leftRanges),[
    ['changed','/a~1b/~0','"~":1'],['changed','/items/1','2'],['removed','/gone','"gone":{"nested":true}'],
  ])
  assert.deepEqual(slices(right,diff.rightRanges),[
    ['changed','/items/1','3'],['added','/items/2','4'],['changed','/a~1b/~0','"~":9'],['added','/new','"new":null'],
  ])
  for (const range of [...diff.leftRanges,...diff.rightRanges]) assert.equal(diff.rows[range.index].path,range.path)
})

test('inline diff distinguishes the root from an empty property key and handles multiline replacement',()=>{
  const text='{\n "": {\n "a": 1\n }, "q\\\"": false\n}'
  const result=processDocument({text,right:'{"":null,"q\\\"":true}',mode:'diff'})
  assert.equal(result.diff.leftRanges.length,2)
  assert.equal(text.slice(result.diff.leftRanges[0].from,result.diff.leftRanges[0].to),'"": {\n "a": 1\n }')
  assert.equal(result.diff.rows[0].pointer,'/')
  const root=processDocument({text:' {"":1} ',right:'null',mode:'diff'}).diff
  assert.equal(root.rows[0].pointer,'')
  assert.equal(root.leftRanges[0].from,1)
  assert.equal(root.leftRanges[0].to,7)
  assert.equal(root.rightRanges[0].to,4)
  const equal=processDocument({text:'{"a":1,"b":2}',right:'{ "b": 2, "a": 1 }',mode:'diff'}).diff
  assert.deepEqual(equal.leftRanges,[])
  assert.deepEqual(equal.rightRanges,[])
})
