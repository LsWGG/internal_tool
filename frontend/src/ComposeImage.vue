<script setup>
import {computed,ref,watch,onBeforeUnmount} from 'vue'
import axios from 'axios'
const props=defineProps({image:String,available:Boolean,revision:Number})
const state=ref('unknown'),error=ref('')
const label=computed(()=>!props.available?'未连接 Docker':({unknown:'待检测',checking:'检测中…',exists:'本机已存在',missing:'本机未下载',pulling:'拉取中…',error:'操作失败'})[state.value])
let controller,generation=0
async function run(pull=false){
  if(!props.available||state.value==='pulling')return
  controller?.abort();controller=new AbortController();const current=++generation
  state.value=pull?'pulling':'checking';error.value=''
  try{const r=await axios.post(`/api/compose/images/${pull?'pull':'check'}`,{image:props.image},{signal:controller.signal,timeout:pull?620000:20000});if(current===generation)state.value=r.data.exists?'exists':'missing'}
  catch(e){if(current===generation&&!axios.isCancel(e)){state.value='error';error.value=e.response?.data?.detail||e.message}}
}
watch(()=>[props.image,props.available,props.revision],()=>{if(state.value==='pulling')return;controller?.abort();generation++;state.value='unknown';error.value='';if(props.available)run()},{immediate:true})
onBeforeUnmount(()=>{generation++;controller?.abort()})
</script>
<template><div class="compose-image"><div class="image-status" :class="available?state:'unknown'"><span class="image-dot" role="status" :aria-label="label" :title="error||label" tabindex="0"></span><span>{{image}}</span><button v-if="available&&state==='missing'" @click="run(true)">拉取</button><button v-if="available&&state==='error'" @click="run()">重试</button><small v-if="state==='checking'||state==='pulling'">{{label}}</small></div><small v-if="error" class="image-error">{{error}}</small></div></template>
<style scoped>
.image-dot{width:9px;height:9px;flex:none;border-radius:50%;background:currentColor}.image-status>span:not(.image-dot){color:var(--compose-text)}.image-status.checking,.image-status.pulling{color:var(--compose-yaml-key)}
.compose-image{display:grid;gap:5px;overflow-wrap:anywhere}.image-status{display:flex;gap:8px;align-items:center;flex-wrap:wrap;color:var(--compose-muted)}.image-status.exists{color:var(--compose-ok)}.image-status.missing{color:var(--compose-warn)}.image-status.error,.image-error{color:var(--compose-bad)}.image-status button{padding:3px 8px;border:1px solid var(--compose-line);border-radius:4px;color:var(--compose-text);background:var(--compose-paper);cursor:pointer;font-size:12px}.compose-image small{font-size:11px}
</style>
