<script setup>
import {onMounted, onBeforeUnmount, ref, watch} from 'vue'
import {EditorState, StateEffect, StateField, Compartment, RangeSet} from '@codemirror/state'
import {EditorView, Decoration, GutterMarker, gutter, lineNumbers, keymap, drawSelection, placeholder} from '@codemirror/view'
import {defaultKeymap, history, historyKeymap} from '@codemirror/commands'

const props = defineProps({
  modelValue:{type:String,default:''}, ranges:{type:Array,default:()=>[]},
  label:String, placeholder:String, wrap:Boolean, activeIndex:{type:Number,default:-1},
})
const emit = defineEmits(['update:modelValue'])
const host = ref(null)
const wrapping = new Compartment()
const updateRanges = StateEffect.define()
let view
const labels = {added:'新增',removed:'删除',changed:'修改'}
const symbols = {added:'+',removed:'−',changed:'~'}

class DiffMarker extends GutterMarker {
  constructor(type, path) { super(); this.type=type; this.path=path }
  eq(other) { return this.type===other.type && this.path===other.path }
  toDOM() {
    const marker = document.createElement('span')
    marker.className=`json-diff-marker ${this.type}`
    marker.textContent=symbols[this.type]
    marker.title=`${labels[this.type]} ${this.path}`
    marker.setAttribute('aria-label', marker.title)
    return marker
  }
}

function decorations(state, ranges) {
  const marks = [], markers = [], lines = new Set()
  for (const range of ranges) {
    if (range.from < 0 || range.to > state.doc.length || range.from >= range.to) continue
    marks.push(Decoration.mark({
      class:`json-diff-token ${range.type}${range.index===props.activeIndex?' current':''}`,
      attributes:{title:`${labels[range.type]} ${range.path}`},
    }).range(range.from,range.to))
    const start = state.doc.lineAt(range.from).from
    if (!lines.has(start)) {
      lines.add(start)
      marks.push(Decoration.line({class:`json-diff-line ${range.type}`}).range(start))
      markers.push(new DiffMarker(range.type,range.path).range(start))
    }
  }
  return {marks:Decoration.set(marks,true),markers:RangeSet.of(markers,true)}
}
const highlights = StateField.define({
  create:state=>decorations(state,props.ranges),
  update(value,tr) {
    // Old offsets must not be painted on newly edited text while the worker runs.
    if (tr.docChanged) value={marks:Decoration.none,markers:RangeSet.empty}
    for (const effect of tr.effects) if (effect.is(updateRanges)) value=decorations(tr.state,effect.value)
    return value
  },
  provide:field=>EditorView.decorations.from(field,value=>value.marks),
})

onMounted(()=>{
  view=new EditorView({parent:host.value,state:EditorState.create({doc:props.modelValue,extensions:[
    highlights, lineNumbers(), gutter({class:'json-diff-gutter',markers:v=>v.state.field(highlights).markers}),
    history(), keymap.of([...defaultKeymap,...historyKeymap]), drawSelection(),
    placeholder(props.placeholder || ''), wrapping.of(props.wrap?EditorView.lineWrapping:[]),
    EditorView.contentAttributes.of({'aria-label':props.label,'aria-multiline':'true',role:'textbox',spellcheck:'false'}),
    EditorView.updateListener.of(update=>{if(update.docChanged)emit('update:modelValue',update.state.doc.toString())}),
    EditorView.theme({
      '&':{height:'100%',fontSize:'13px',color:'#253436',backgroundColor:'#fffef9'},
      '&.cm-focused':{outline:'none'},
      '.cm-scroller':{overflow:'auto',fontFamily:'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',lineHeight:'1.8'},
      '.cm-content':{padding:'14px 0',caretColor:'#172e30'},
      '.cm-line':{padding:'0 14px'},
      '.cm-gutters':{backgroundColor:'#f8f7f1',color:'#92958b',borderRight:'1px solid #e3e2d9'},
      '.cm-gutterElement':{fontSize:'11px'},
      '.cm-placeholder':{color:'#8c8c81'},
      '.json-diff-gutter':{width:'20px'},
      '.json-diff-marker':{fontWeight:'800',display:'block',textAlign:'center'},
      '.json-diff-marker.added':{color:'#24764c'},
      '.json-diff-marker.removed':{color:'#b53452'},
      '.json-diff-marker.changed':{color:'#976800'},
      '.json-diff-line.added':{backgroundColor:'#f1faf4'},
      '.json-diff-line.removed':{backgroundColor:'#fff3f5'},
      '.json-diff-line.changed':{backgroundColor:'#fffae9'},
      '.json-diff-token':{borderRadius:'2px',padding:'1px 0',boxDecorationBreak:'clone'},
      '.json-diff-token.added':{backgroundColor:'#ccefd9'},
      '.json-diff-token.removed':{backgroundColor:'#ffd7df'},
      '.json-diff-token.changed':{backgroundColor:'#ffe59a'},
      '.json-diff-token.current':{textDecoration:'underline',textDecorationColor:'#786b49',textUnderlineOffset:'4px'},
      '.cm-selectionBackground':{backgroundColor:'#b7dfe5 !important'},
    }),
  ]})})
})
watch(()=>props.modelValue,value=>{
  if (view && value!==view.state.doc.toString()) view.dispatch({changes:{from:0,to:view.state.doc.length,insert:value}})
})
watch([()=>props.ranges,()=>props.activeIndex],()=>{view?.dispatch({effects:updateRanges.of(props.ranges)})})
watch(()=>props.wrap,value=>{view?.dispatch({effects:wrapping.reconfigure(value?EditorView.lineWrapping:[])})})
function reveal(index) {
  const range=props.ranges.find(item=>item.index===index)
  if (view && range) view.dispatch({effects:EditorView.scrollIntoView(range.from,{y:'center',x:'nearest'})})
}
defineExpose({reveal})
onBeforeUnmount(()=>view?.destroy())
</script>

<template><div ref="host" class="json-diff-editor"></div></template>

<style scoped>
.json-diff-editor{flex:1;min-height:0;min-width:0;overflow:hidden}
.json-diff-editor:focus-within{box-shadow:inset 0 0 0 1px #8eb8b7}
</style>
