<script setup>
import {ref,watch,onBeforeUnmount} from 'vue'
import axios from 'axios'
const props=defineProps({projectId:String,available:Boolean})
const containers=ref([]),loading=ref(false),error=ref(''),reveal=ref(false)
let controller,epoch=0
const text=v=>v===null||v===undefined||v===''?'—':typeof v==='object'?JSON.stringify(v,null,2):String(v)
function groups(c){return [
 ['基本信息',{'名称':c.Name,'容器 ID':c.Id,'镜像':c.Config?.Image,'镜像 ID':c.Image,'创建时间':c.Created,'启动命令':[c.Path,...(c.Args||[])].join(' '),'工作目录':c.Config?.WorkingDir,'用户':c.Config?.User}],
 ['运行状态',{'状态':c.State?.Status,'运行中':c.State?.Running,'退出码':c.State?.ExitCode,'启动时间':c.State?.StartedAt,'停止时间':c.State?.FinishedAt,'错误':c.State?.Error,'健康检查':c.State?.Health}],
 ['网络与端口',{'网络模式':c.HostConfig?.NetworkMode,'端口映射':c.NetworkSettings?.Ports,'网络':c.NetworkSettings?.Networks}],
 ['挂载与资源',{'挂载':c.Mounts,'重启策略':c.HostConfig?.RestartPolicy,'内存上限（字节）':c.HostConfig?.Memory,'CPU 配额':c.HostConfig?.NanoCpus,'特权模式':c.HostConfig?.Privileged}],
 ['环境变量',Object.fromEntries((c.Config?.Env||[]).map(v=>{const i=v.indexOf('=');return [i<0?v:v.slice(0,i),reveal.value?(i<0?'':v.slice(i+1)):'••••••']}))],
 ['标签',c.Config?.Labels||{}]
]}
async function load(){controller?.abort();const current=++epoch;containers.value=[];error.value='';if(!props.available)return;controller=new AbortController();loading.value=true;try{const r=await axios.get(`/api/compose/projects/${props.projectId}/inspect`,{signal:controller.signal});if(current===epoch)containers.value=r.data.containers||[]}catch(e){if(current===epoch&&!axios.isCancel(e))error.value=e.response?.data?.detail||e.message}finally{if(current===epoch)loading.value=false}}
watch(()=>[props.projectId,props.available],()=>{reveal.value=false;load()},{immediate:true})
onBeforeUnmount(()=>{epoch++;controller?.abort()})
</script>
<template><section class="inspect-panel"><div class="inspect-toolbar"><b>容器 Inspect · 只读</b><button :disabled="loading||!available" @click="load">{{loading?'读取中…':'刷新信息'}}</button><button @click="reveal=!reveal">{{reveal?'隐藏':'显示'}}环境变量值</button></div><p v-if="error" role="alert">{{error}}</p><p v-else-if="!available">Docker 未连接，请先检查连接设置。</p><p v-else-if="!loading&&!containers.length">当前项目没有容器。部署后可查看容器信息。</p><article v-for="c in containers" :key="c.Id"><h3>{{c.Name?.replace(/^\//,'')}}</h3><section v-for="[name,fields] in groups(c)" :key="name" class="inspect-group"><h4>{{name}}</h4><dl><div v-for="(value,key) in fields" :key="key"><dt>{{key}}</dt><dd><pre v-if="typeof value==='object'&&value">{{text(value)}}</pre><span v-else>{{text(value)}}</span></dd></div></dl></section></article></section></template>
<style scoped>
.inspect-panel{overflow:auto;flex:1;min-height:0;padding:16px;color:var(--compose-text)}.inspect-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.inspect-toolbar b{margin-right:auto}.inspect-toolbar button{padding:8px 12px;border:1px solid var(--compose-line);border-radius:4px;background:var(--compose-paper);color:var(--compose-text);cursor:pointer}.inspect-group{border:1px solid var(--compose-line);border-radius:5px;margin:14px 0;overflow:hidden}.inspect-group h4{margin:0;padding:12px 16px;background:var(--compose-soft)}dl{margin:0}dl>div{display:grid;grid-template-columns:minmax(130px,22%) minmax(0,1fr);border-top:1px solid var(--compose-line)}dt,dd{margin:0;padding:12px 16px;overflow-wrap:anywhere}dt{color:var(--compose-muted)}dd pre{margin:0;max-height:300px;overflow:auto;white-space:pre-wrap;font:12px/1.6 monospace;color:var(--compose-code-text);background:var(--compose-code-bg);padding:10px}@media(max-width:600px){dl>div{grid-template-columns:1fr}dt{padding-bottom:0}}
</style>
