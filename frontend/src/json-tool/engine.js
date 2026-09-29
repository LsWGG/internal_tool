import {isSafeNumber} from 'lossless-json'
import {JSONPath} from 'jsonpath-plus'
import {stringify as yamlStringify} from 'yaml'

export const MAX_BYTES = 5 * 1024 * 1024
const MAX_NODES = 5000
const has = (value, key) => Object.prototype.hasOwnProperty.call(value, key)
export const kind = value => value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value
export const pointerKey = key => String(key).replace(/~/g, '~0').replace(/\//g, '~1')
const container = value => value !== null && typeof value === 'object'
const brief = value => container(value) ? `${Array.isArray(value) ? 'Array' : 'Object'} · ${Object.keys(value).length} 项` : JSON.stringify(value)

export function parseDocument(text) {
  if (!text.trim()) throw new Error('请粘贴 JSON 或上传文件。')
  if (new TextEncoder().encode(text).length > MAX_BYTES) throw new Error('单份文件最大支持 5 MB，请拆分后再处理。')
  const clean = text.replace(/^\uFEFF/, '')
  const value = JSON.parse(clean)
  // Inspect tokens independently: preserve own __proto__ keys, reject even equal
  // duplicate values, and validate the original numeric lexeme before rounding.
  const stack = []
  const tokens = /"(?:\\.|[^"\\])*"|[{}\[\]:,]|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null/g
  for (const match of clean.matchAll(tokens)) {
    const token = match[0], current = stack.at(-1)
    if (token === '{' || token === '[') {
      stack.push({object:token === '{', keys:new Set(), key:true})
      if (stack.length > 100) throw new Error('嵌套层级超过 100 层，请拆分数据后重试。')
    } else if (token === '}' || token === ']') stack.pop()
    else if (token === ',') { if (current?.object) current.key = true }
    else if (token[0] === '"' && current?.object && current.key) {
      const key = JSON.parse(token)
      if (current.keys.has(key)) throw new Error(`重复键 ${token}（位置 ${match.index + 1}），请修改后重试。`)
      current.keys.add(key); current.key = false
    } else if (/^-?\d/.test(token) && !isSafeNumber(token)) {
      throw new Error(`数字 ${token} 超出安全精度。请将其写为带引号的字符串，以保留原始值。`)
    }
  }
  let nodes = 0
  const visit = (item, depth) => {
    if (depth > 100) throw new Error('嵌套层级超过 100 层，请拆分数据后重试。')
    nodes++
    if (container(item)) for (const child of Object.values(item)) visit(child, depth + 1)
  }
  visit(value, 0)
  return {value, nodes}
}

export function sortKeys(value) {
  if (Array.isArray(value)) return value.map(sortKeys)
  if (!container(value)) return value
  return Object.fromEntries(Object.keys(value).sort().map(key => [key, sortKeys(value[key])]))
}

export function treeRows(value) {
  const rows = []
  let truncated = false
  function visit(item, key, pointer, depth, ancestors) {
    if (rows.length >= MAX_NODES) { truncated = true; return }
    const branch = container(item) && Object.keys(item).length > 0
    rows.push({key, pointer, depth, ancestors, branch, type:kind(item), summary:brief(item)})
    if (container(item)) for (const [name, child] of Object.entries(item)) {
      visit(child, name, `${pointer}/${pointerKey(name)}`, depth + 1, [...ancestors, pointer])
      if (truncated) break
    }
  }
  visit(value, '$', '', 0, [])
  return {rows, truncated}
}

export function tableRows(value) {
  if (Array.isArray(value) && value.every(row => kind(row) === 'object')) {
    const keys = [...new Set(value.slice(0, 200).flatMap(Object.keys))]
    const columns = keys.slice(0, 40)
    return {columns, rows:value.slice(0, 200).map(row => columns.map(key => has(row, key) ? JSON.stringify(row[key]) : '—')), truncated:value.length > 200 || keys.length > 40}
  }
  const entries = container(value) ? Object.entries(value) : [['$', value]]
  return {columns:['键 / 索引', '类型', '值'], rows:entries.slice(0, 200).map(([key, item]) => [key, kind(item), JSON.stringify(item)]), truncated:entries.length > 200}
}

export function queryDocument(value, path) {
  if (!path.trim().startsWith('$')) throw new Error('JSONPath 必须以 $ 开头。')
  // The browser worker uses JSONPath's restricted evaluator; no native eval.
  const matches = path.trim() === '$' ? [{path:'$',pointer:'',value}] : (JSONPath({path, json:value, resultType:'all', wrap:true, eval:'safe'}) || [])
  return {count:matches.length, rows:matches.slice(0, 500).map(match => ({path:match.path, pointer:match.pointer, value:match.value})), truncated:matches.length > 500}
}

export function differences(left, right) {
  const rows = []
  let truncated = false
  function add(type, path, a, b) {
    if (rows.length >= 1000) { truncated = true; return }
    rows.push({type, pointer:path, path:path || '/', before:a === undefined ? '（不存在）' : JSON.stringify(a), after:b === undefined ? '（不存在）' : JSON.stringify(b)})
  }
  function visit(a, b, path) {
    if (truncated || Object.is(a, b)) return
    if (kind(a) !== kind(b) || !container(a)) { add('changed', path, a, b); return }
    for (const key of new Set([...Object.keys(a), ...Object.keys(b)])) {
      const p = `${path}/${pointerKey(key)}`
      if (!has(a, key)) add('added', p, undefined, b[key])
      else if (!has(b, key)) add('removed', p, a[key], undefined)
      else visit(a[key], b[key], p)
      if (truncated) break
    }
  }
  visit(left, right, '')
  return {rows, truncated}
}

// Resolve structural differences against the original text, including minified
// JSON. This runs in the worker after validation and never rewrites either input.
export function diffTextRanges(text, rows, side) {
  const wanted = new Map(rows.flatMap((row, index) =>
    row.type === (side === 'left' ? 'added' : 'removed') ? [] : [[row.pointer, {type:row.type, index, path:row.path}]]))
  if (!wanted.size) return []
  const tokens = text.matchAll(/"(?:\\.|[^"\\])*"|[{}\[\]:,]|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null/g)
  let token = tokens.next().value
  const next = () => { const current = token; token = tokens.next().value; return current }
  const ranges = []
  function visit(pointer, propertyStart) {
    const first = next()
    const from = propertyStart ?? first.index
    let to = first.index + first[0].length
    if (first[0] === '{') {
      while (token[0] !== '}') {
        const key = next(); next() // colon
        visit(`${pointer}/${pointerKey(JSON.parse(key[0]))}`, key.index)
        if (token[0] === ',') next()
      }
      to = next().index + 1
    } else if (first[0] === '[') {
      let index = 0
      while (token[0] !== ']') {
        visit(`${pointer}/${index++}`)
        if (token[0] === ',') next()
      }
      to = next().index + 1
    }
    if (wanted.has(pointer)) ranges.push({from, to, ...wanted.get(pointer)})
  }
  visit('')
  return ranges.sort((a,b) => a.from - b.from)
}

export function convertDocument(value, format, indent = 2) {
  if (format === 'json') return JSON.stringify(value, null, indent)
  if (format === 'minify') return JSON.stringify(value)
  if (format === 'yaml') return yamlStringify(value, {indent:2, lineWidth:0})
  if (format === 'jsonl') {
    if (!Array.isArray(value)) throw new Error('JSON Lines 转换要求根节点为数组，每个元素输出一行。')
    return value.map(item => JSON.stringify(item)).join('\n') + (value.length ? '\n' : '')
  }
  if (format === 'csv') {
    if (!Array.isArray(value) || !value.every(item => kind(item) === 'object')) throw new Error('CSV 转换要求根节点为对象数组；嵌套对象会保留为 JSON 字符串。')
    const columns = [...new Set(value.flatMap(Object.keys))]
    // Quote every cell, preserve newlines/commas, and neutralize spreadsheet formulas.
    const cell = value => {
      let text = value == null ? '' : typeof value === 'string' ? value : JSON.stringify(value)
      if (/^[\s]*[=+\-@\t\r]/.test(text)) text = `'${text}`
      return `"${text.replace(/"/g, '""')}"`
    }
    return [columns.map(cell).join(','), ...value.map(row => columns.map(key => cell(has(row, key) ? row[key] : null)).join(','))].join('\r\n')
  }
  throw new Error('不支持的转换格式。')
}

export function processDocument({text, mode, indent = 2, sorted = false, path = '$', right = '', format = 'yaml'}) {
  const parsed = parseDocument(text)
  const value = sorted ? sortKeys(parsed.value) : parsed.value
  const result = {nodes:parsed.nodes, type:kind(value), bytes:new TextEncoder().encode(text).length}
  if (mode === 'tree' || mode === 'table') result.output = JSON.stringify(value, null, indent)
  if (mode === 'tree') Object.assign(result, treeRows(value))
  else if (mode === 'table') Object.assign(result, tableRows(value))
  else if (mode === 'query') {
    try { result.query = queryDocument(value, path); result.output = JSON.stringify(result.query.rows.map(row => row.value), null, indent) }
    catch (error) { result.actionError = `查询失败：${error.message}` }
  } else if (mode === 'diff') {
    try {
      result.diff = differences(value, parseDocument(right).value)
      result.diff.leftRanges = diffTextRanges(text, result.diff.rows, 'left')
      result.diff.rightRanges = diffTextRanges(right, result.diff.rows, 'right')
      result.output = JSON.stringify(result.diff.rows, null, indent)
    }
    catch (error) { result.actionError = `右栏 JSON：${error.message}` }
  } else {
    try { result.output = convertDocument(value, mode === 'convert' ? format : 'json', indent) }
    catch (error) { result.actionError = error.message }
  }
  return result
}
