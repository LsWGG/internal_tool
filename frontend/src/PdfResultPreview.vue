<script setup>
import {ref,watch,onBeforeUnmount} from 'vue'
import axios from 'axios'
const props=defineProps({blob:{type:Blob,required:true}})
const page=ref(1),count=ref(1),url=ref(''),busy=ref(false),error=ref(''),zoom=ref(100)
let controller,sequence=0
async function render(){
  const id=++sequence;controller?.abort();controller=new AbortController();busy.value=true;error.value=''
  if(url.value){URL.revokeObjectURL(url.value);url.value=''}
  try{const data=new FormData();data.append('file',props.blob,'result.pdf');data.append('page',String(page.value));const response=await axios.post('/api/pdf-toolbox/preview',data,{responseType:'blob',signal:controller.signal,timeout:60000});if(id!==sequence)return;count.value=Number(response.headers['x-pdf-pages'])||1;url.value=URL.createObjectURL(response.data)}
  catch(e){if(id!==sequence||e.code==='ERR_CANCELED')return;error.value=e.response?.status===404?'预览服务尚未加载，请重启后端后重试。':'页面预览失败，请重试；仍可下载 PDF 查看。'}
  finally{if(id===sequence)busy.value=false}
}
watch(()=>props.blob,()=>{page.value=1;render()},{immediate:true})
function turn(delta){page.value+=delta;render()}
onBeforeUnmount(()=>{sequence++;controller?.abort();if(url.value)URL.revokeObjectURL(url.value)})
</script>
<template><div class="pdf-rendered-preview"><div class="pdf-render-toolbar"><span>处理结果 · 与下载文件一致</span><div><button :disabled="busy||page<=1" @click="turn(-1)" aria-label="上一页">←</button><span>{{page}} / {{count}} 页</span><button :disabled="busy||page>=count" @click="turn(1)" aria-label="下一页">→</button><button :disabled="zoom<=75" @click="zoom-=25" aria-label="缩小">−</button><button @click="zoom=100" title="适合窗口">{{zoom}}%</button><button :disabled="zoom>=200" @click="zoom+=25" aria-label="放大">＋</button></div></div><div class="pdf-render-canvas" :aria-busy="busy"><p v-if="busy" role="status">正在渲染第 {{page}} 页…</p><p v-else-if="error" role="alert">{{error}} <button @click="render">重试</button></p><img v-else-if="url" :src="url" :style="{width:zoom+'%'}" :alt="`处理后 PDF 第 ${page} 页（含已写入的水印）`"></div></div></template>
<style scoped>
.pdf-rendered-preview{display:flex;flex:1;min-width:0;flex-direction:column;color:var(--ui-text);background:var(--ui-surface-subtle)}.pdf-render-toolbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px;padding:10px 14px;border-bottom:1px solid var(--pop-line);font-size:11px;background:var(--ui-surface)}.pdf-render-toolbar>div{display:flex;align-items:center;gap:8px}.pdf-rendered-preview button{padding:5px 8px;border:1px solid var(--pop-line);border-radius:3px;background:var(--ui-surface);color:var(--ui-text);cursor:pointer}.pdf-rendered-preview button:disabled{opacity:.5;cursor:default}.pdf-render-canvas{height:620px;overflow:auto;padding:16px;box-sizing:border-box;text-align:center}.pdf-render-canvas img{display:block;max-width:none;height:auto;margin:0 auto;background:white;box-shadow:0 2px 10px #0004}.pdf-render-canvas p{padding:30px 10px;font-size:12px}@media(max-width:680px){.pdf-render-canvas{height:460px}}
</style>
