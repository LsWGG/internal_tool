<script setup>
import {onBeforeUnmount,onMounted,ref} from 'vue'
import axios from 'axios'

const state=ref({running:false,starting:true,url:'http://127.0.0.1:4224',architecture:'',error:''})
let timer

async function refresh(){
  try{state.value=(await axios.get('/api/dbx/status')).data}catch{state.value={...state.value,starting:false,error:'无法连接本项目后端，DBX 启动状态暂时不可用。'}}
}
onMounted(async()=>{await refresh();timer=setInterval(refresh,1500)})
onBeforeUnmount(()=>clearInterval(timer))
</script>

<template>
  <section class="dbx-page">
    <header>
      <div>
        <span class="eyebrow">LOCAL DATABASE WORKSPACE</span>
        <h1>DBX 数据库工作台</h1>
        <p>本地 DBX 随项目自动启动；数据保存在本机运行目录，不会提交到 Git。</p>
      </div>
      <div class="dbx-status" :class="{ready:state.running,error:state.error&&!state.running}">
        <b>{{state.running?'已就绪':state.starting?'正在启动…':'未启动'}}</b>
        <small>{{state.architecture||'检测运行架构中'}}</small>
      </div>
    </header>

    <div v-if="state.running" class="dbx-frame-wrap">
      <iframe class="dbx-frame" :src="state.url" title="DBX 数据库工作台"></iframe>
    </div>
    <div v-else class="dbx-wait" role="status">
      <span>DBX</span>
      <b>{{state.error||'正在启动本地数据库工作台…'}}</b>
      <p>首次启动可能需要几秒钟，请保持此页面打开。</p>
      <button type="button" @click="refresh">重新检查</button>
    </div>
  </section>
</template>

<style scoped>
.dbx-page{display:flex;flex-direction:column;width:100%;min-height:calc(100dvh - 96px);color:var(--ui-text,#2d435f)}
.dbx-page header{display:flex;align-items:center;justify-content:space-between;gap:20px;margin-bottom:12px;padding-bottom:12px;border-bottom:1px solid var(--ui-border,#dbe4f0)}
.dbx-page h1{margin:4px 0;color:inherit;font-size:var(--fs-29,29px)}
.dbx-page p{margin:0;color:var(--ui-muted,#75869b);font-size:var(--fs-13,13px);line-height:1.55}
.dbx-status{display:flex;flex-direction:column;gap:4px;min-width:124px;padding:10px 13px;border:1px solid #d9c987;border-radius:8px;background:#fff7d7;color:#7d6218}
.dbx-status.ready{border-color:#9ac7ad;background:#effaf2;color:#28704c}.dbx-status.error{border-color:#e4b5bd;background:#fff2f3;color:#a33e50}.dbx-status b{font-size:var(--fs-12,12px)}.dbx-status small{font-size:var(--fs-10,10px)}
/* DBX is itself a full database IDE: reserve nearly all available viewport height
   for it instead of applying the shared, compact task-preview height. */
.dbx-frame-wrap{--dbx-iframe-scale:1;flex:1;height:calc(100dvh - 172px);min-height:680px;border:1px solid var(--ui-border,#dbe4f0);border-radius:var(--ui-panel-radius,14px);overflow:hidden;background:#fff;box-shadow:0 8px 28px rgba(48,75,112,.06)}
.dbx-frame{display:block;width:calc(100% / var(--dbx-iframe-scale));height:calc(100% / var(--dbx-iframe-scale));border:0;transform:scale(var(--dbx-iframe-scale));transform-origin:top left}.dbx-wait{display:flex;flex:1;min-height:680px;flex-direction:column;align-items:center;justify-content:center;gap:12px;border:1px dashed #b9c8db;border-radius:var(--ui-panel-radius,14px);background:#fbfcfe;text-align:center}.dbx-wait>span{padding:13px 11px;border:2px solid var(--pop-ink,#252526);background:var(--pop-cyan,#83d9e7);font:800 18px ui-monospace,monospace;box-shadow:3px 3px var(--pop-ink,#252526)}.dbx-wait b{font-size:var(--fs-15,15px)}.dbx-wait p{font-size:var(--fs-12,12px)}.dbx-wait button{padding:8px 12px;border:1px solid var(--pop-ink,#252526);background:var(--pop-yellow,#ffe250);box-shadow:2px 2px var(--pop-ink,#252526);cursor:pointer}
/* DBX's standalone UI is designed around desktop CSS pixels.  On a dense 4K
   panel this preserves a practical control size without cropping the frame. */
@media(min-width:1800px){.dbx-frame-wrap{--dbx-iframe-scale:1.15}}
@media(min-width:2400px){.dbx-frame-wrap{--dbx-iframe-scale:1.25}}
@media(min-width:3200px){.dbx-frame-wrap{--dbx-iframe-scale:1.38}}
@media(min-width:1200px) and (min-resolution:1.5dppx){.dbx-frame-wrap{--dbx-iframe-scale:1.28}}
@media(max-width:700px){.dbx-page header{align-items:flex-start;flex-direction:column}.dbx-frame-wrap,.dbx-wait{height:600px;min-height:420px}}
</style>
