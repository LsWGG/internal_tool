<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import L from 'leaflet'
import '@geoman-io/leaflet-geoman-free'
import axios from 'axios'

const props=defineProps({confirmAction:{type:Function,default:null},aiPatch:{type:Object,default:null}})
const emit=defineEmits(['notify'])
const notify=(message,type='success')=>emit('notify',message,type)
const sourceMode=ref('path'),sourcePath=ref(''),outputPath=ref(''),shpPath=ref(''),inPlace=ref(false),busy=ref(false)
const sourceFiles=ref([]),shpFiles=ref([]),sourceInput=ref(null),shpInput=ref(null)
const west=ref(''),south=ref(''),east=ref(''),north=ref(''),tasks=ref([])
const canSubmit=computed(()=>sourceMode.value==='upload'?sourceFiles.value.length>0:!!sourcePath.value.trim())
const placeHint=computed(()=>{
  if(inPlace.value&&sourceMode.value==='upload')return '写在这次上传的副本旁边，不会改你电脑上的原文件。只适用于单个 GeoTIFF，瓦片目录不能在原位置裁切。删除任务会删掉这次上传的副本。'
  if(inPlace.value)return '只适用于单个 GeoTIFF，生成同目录的 *_clip.tif，不覆盖源文件。瓦片目录不能在原位置裁切。'
  if(sourceMode.value==='upload')return '留空则写到工具目录并打包。填写外接目录时，结果放在该目录下的任务文件夹里，不打包。删除任务会删掉这次上传的副本。'
  return '留空则写到工具目录并打包。填写外接目录时，结果放在该目录下的任务文件夹里，不打包。'
})
const mapEl=ref()
let map,drawLayer,timer,mapObserver

function readBounds(layer){
  const bounds=layer.getBounds()
  west.value=bounds.getWest().toFixed(6)
  south.value=bounds.getSouth().toFixed(6)
  east.value=bounds.getEast().toFixed(6)
  north.value=bounds.getNorth().toFixed(6)
}
function initMap(){
  if(map||!mapEl.value)return
  map=L.map(mapEl.value,{zoomControl:true}).setView([31.23,121.47],4)
  L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',{maxZoom:20,attribution:'Satellite imagery © Google'}).addTo(map)
  map.pm.addControls({position:'topleft',drawMarker:false,drawCircleMarker:false,drawPolyline:false,drawPolygon:false,drawCircle:false,drawText:false,editMode:false,dragMode:false,cutPolygon:false,removalMode:true,rotateMode:false})
  map.on('pm:create',event=>{
    if(event.shape!=='Rectangle'){map.removeLayer(event.layer);return}
    if(drawLayer)map.removeLayer(drawLayer)
    drawLayer=event.layer
    readBounds(drawLayer)
  })
  map.on('pm:remove',()=>{drawLayer=null;west.value=south.value=east.value=north.value=''})
}
async function loadTasks(){try{tasks.value=(await axios.get('/api/raster-clip/tasks')).data}catch(error){console.error(error)}}
function hasBox(){return [west.value,south.value,east.value,north.value].every(value=>String(value).trim()!=='')}
function clearBox(){if(drawLayer&&map)map.removeLayer(drawLayer);drawLayer=null;west.value=south.value=east.value=north.value=''}
function chooseSource(event){sourceFiles.value=[...event.target.files]}
function chooseShp(event){shpFiles.value=[...event.target.files]}
function applyPatch(values){
  if(!values)return
  if(typeof values.source_path==='string'&&values.source_path.trim()){sourceMode.value='path';sourcePath.value=values.source_path}
  if(typeof values.shp_path==='string')shpPath.value=values.shp_path
  if(typeof values.output_path==='string')outputPath.value=values.output_path
  if(typeof values.in_place==='boolean')inPlace.value=values.in_place
  for(const [key,target] of [['west',west],['south',south],['east',east],['north',north]]){
    if(values[key]!==undefined&&values[key]!==null&&values[key]!=='')target.value=String(values[key])
  }
}
watch(()=>props.aiPatch?.revision,()=>applyPatch(props.aiPatch))
async function submit(){
  if(sourceMode.value==='upload'){
    if(!sourceFiles.value.length)return notify('请选择要上传的目录。','error')
    if(!shpFiles.value.length&&!hasBox())return notify('请上传 Shapefile 目录，或在右侧地图上画一个矩形。','error')
  }else if(!sourcePath.value.trim())return notify('请填写 GeoTIFF 或 z/x/y 瓦片目录的绝对路径。','error')
  else if(!shpPath.value.trim()&&!hasBox())return notify('请填写 Shapefile，或在右侧地图上画一个矩形。','error')
  busy.value=true
  try{
    if(sourceMode.value==='upload'){
      const data=new FormData()
      sourceFiles.value.forEach(file=>data.append('files',file,file.webkitRelativePath||file.name))
      shpFiles.value.forEach(file=>data.append('shp_files',file,file.webkitRelativePath||file.name))
      data.append('output_path',inPlace.value?'':outputPath.value.trim())
      data.append('in_place',inPlace.value?'true':'false')
      if(hasBox()){data.append('west',west.value);data.append('south',south.value);data.append('east',east.value);data.append('north',north.value)}
      await axios.post('/api/raster-clip/uploads',data)
      sourceFiles.value=[];shpFiles.value=[]
      if(sourceInput.value)sourceInput.value.value=''
      if(shpInput.value)shpInput.value.value=''
    }else await axios.post('/api/raster-clip/tasks',{
      source_path:sourcePath.value.trim(),
      output_path:inPlace.value?'':outputPath.value.trim(),
      shp_path:shpPath.value.trim(),
      in_place:inPlace.value,
      west:hasBox()?Number(west.value):null,
      south:hasBox()?Number(south.value):null,
      east:hasBox()?Number(east.value):null,
      north:hasBox()?Number(north.value):null,
    })
    notify('裁切任务已加入队列。')
    await loadTasks()
  }catch(error){notify(error.response?.data?.detail||error.message,'error')}
  finally{busy.value=false}
}
async function control(task,action){
  if(action==='delete'){
    const message=task.request?.upload_dir?'删除这个任务？会删掉这次上传的副本和任务输出，不会改你电脑上的原文件。':task.request?.in_place?'从列表删除这个任务？只会删掉这个任务写出的裁切文件，源文件会保留。':'删除这个任务及其输出？源文件不会被删除。'
    if(props.confirmAction&&!await props.confirmAction(message))return
  }
  try{
    if(action==='delete')await axios.delete(`/api/raster-clip/tasks/${task.id}`)
    else await axios.post(`/api/raster-clip/tasks/${task.id}/${action}`)
    await loadTasks()
  }catch(error){notify(error.response?.data?.detail||error.message,'error')}
}
function statusText(status){return {queued:'排队中',running:'进行中',paused:'已暂停',completed:'已完成',failed:'失败',cancelled:'已取消'}[status]||status}
onMounted(async()=>{await nextTick();initMap();await loadTasks();if(mapEl.value&&typeof ResizeObserver!=='undefined'){mapObserver=new ResizeObserver(()=>map?.invalidateSize({pan:false}));mapObserver.observe(mapEl.value)};timer=setInterval(loadTasks,2000)})
onBeforeUnmount(()=>{clearInterval(timer);mapObserver?.disconnect();map?.remove()})
</script>

<template>
  <section class="raster-clip-page">
    <header><div><span class="eyebrow">RASTER CLIP</span><h1>栅格裁切</h1><p>用 Shapefile 或框选范围裁切 GeoTIFF、DEM 和瓦片目录。瓦片只整张复制，不裁像素。</p></div></header>
    <div class="raster-clip-grid">
      <aside class="panel">
        <div class="panel-title"><div><b>裁切配置</b><small>服务器路径，或从浏览器上传目录</small></div></div>
        <div class="source-mode" role="tablist"><button type="button" :class="{active:sourceMode==='path'}" @click="sourceMode='path'">服务器路径</button><button type="button" :class="{active:sourceMode==='upload'}" @click="sourceMode='upload'">上传目录</button></div>
        <template v-if="sourceMode==='path'">
          <label>源文件或瓦片目录<input v-model="sourcePath" placeholder="/mnt/external/dem.tif" spellcheck="false"></label>
          <label>Shapefile（优先于框选）<input v-model="shpPath" placeholder="/mnt/external/clip.shp" spellcheck="false"></label>
        </template>
        <template v-else>
          <label class="directory-picker"><input ref="sourceInput" type="file" webkitdirectory directory multiple @change="chooseSource"><span>{{sourceFiles.length?`已选择 ${sourceFiles.length} 个文件`:'选择 GeoTIFF 或 z/x/y 目录'}}</span><small>单个 tif，或一套 z/x/y 瓦片。最多 5000 个文件、2GB</small></label>
          <label class="directory-picker"><input ref="shpInput" type="file" webkitdirectory directory multiple @change="chooseShp"><span>{{shpFiles.length?`已选择 ${shpFiles.length} 个文件`:'可选：Shapefile 目录'}}</span><small>目录里只放一个 .shp 和它的伴随文件</small></label>
        </template>
        <div class="bound-block">
          <div class="bound-head"><b>经纬度范围</b><small>与右侧矩形是同一范围</small></div>
          <div class="bound-grid">
            <label>西<input v-model="west" inputmode="decimal" placeholder="west"></label>
            <label>南<input v-model="south" inputmode="decimal" placeholder="south"></label>
            <label>东<input v-model="east" inputmode="decimal" placeholder="east"></label>
            <label>北<input v-model="north" inputmode="decimal" placeholder="north"></label>
          </div>
        </div>
        <div class="output-box">
          <label class="check-label"><input v-model="inPlace" type="checkbox"><span>写在原文件旁边，不打包下载</span></label>
          <small>{{placeHint}}</small>
          <label v-if="!inPlace">输出路径<input v-model="outputPath" placeholder="留空则写到工具目录" spellcheck="false"></label>
        </div>
        <button class="primary-action" :disabled="busy||!canSubmit" @click="submit">{{busy?'正在提交…':'加入队列'}}</button>
        <p class="field-help">坐标系自动识别。没有坐标系时任务会失败，不会猜测。Shapefile 优先于上面的范围。</p>
      </aside>
      <div class="panel map-panel">
        <div class="map-toolbar"><div><b>框选范围</b><small>用左上角的矩形工具画一个范围</small></div><button type="button" :disabled="!hasBox()" @click="clearBox">清除</button></div>
        <div ref="mapEl" class="clip-map"></div>
      </div>
    </div>
    <section class="queue">
      <div class="queue-head"><div><span class="eyebrow">CLIP QUEUE</span><h2>裁切任务</h2></div></div>
      <div v-if="!tasks.length" class="empty">暂无裁切任务。</div>
      <article v-for="task in tasks" :key="task.id" class="task">
        <div class="task-main"><div><b>{{task.request?.source_path||task.id}}</b><small>{{task.request?.upload_dir?'上传的副本':'服务器路径'}} · {{task.request?.in_place?'原文件旁边':'单独目录'}}</small></div></div>
        <div class="progress"><div><span>{{task.message}}</span><strong>{{task.progress}}%</strong></div><div class="track"><i :class="task.status" :style="{width:(task.progress||0)+'%'}"></i></div><small v-if="task.error" class="error">{{task.error}}</small></div>
        <span class="badge" :class="task.status">{{statusText(task.status)}}</span>
        <div class="task-actions">
          <button v-if="task.status==='queued'||task.status==='running'" @click="control(task,'pause')">暂停</button>
          <button v-if="task.status==='paused'" @click="control(task,'resume')">继续</button>
          <button v-if="task.status==='failed'||task.status==='cancelled'" @click="control(task,'retry')">重试</button>
          <a v-if="task.result?.archive" :href="`/api/raster-clip/tasks/${task.id}/export`">下载</a>
          <button class="danger" @click="control(task,'delete')">删除</button>
        </div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.raster-clip-page{max-width:1460px;margin:0 auto;padding:74px 28px 34px}
.raster-clip-page>header{padding:0 2px 22px;border-bottom:1px solid #d9e3f2}
.raster-clip-page p{margin:0;color:#70829b;font-size:var(--fs-13,13px)}
.eyebrow{color:#5275c7;font-size:var(--fs-10,10px);font-weight:800;letter-spacing:1.7px}
.raster-clip-grid{display:grid;grid-template-columns:360px minmax(0,1fr);gap:18px;margin-top:20px}
.panel{border:1px solid #d8e2ef;border-radius:16px;background:#fff;box-shadow:0 8px 25px #31527c0d}
.raster-clip-grid>aside{display:flex;flex-direction:column;gap:12px;padding:18px;min-width:0}
.raster-clip-grid>aside label{margin-top:0}
.panel-title b{color:#263d5b;font-size:var(--fs-14,14px)}
.panel-title small,.field-help{color:#8392a6;font-size:var(--fs-10,10px);line-height:1.5}
.source-mode{display:grid;grid-template-columns:1fr 1fr;padding:3px;border-radius:10px;background:#edf2f9}
.source-mode button{padding:8px;border:0;border-radius:8px;color:#657994;background:transparent;font:inherit;font-size:var(--fs-11,11px);cursor:pointer}
.source-mode button.active{color:#315eb9;background:#fff;box-shadow:0 2px 8px #35587d18}
.directory-picker{align-items:center;padding:16px 12px;border:1px dashed #aebfda;border-radius:12px;background:#f7faff;text-align:center;cursor:pointer}
.directory-picker input{display:none;width:auto;height:auto;padding:0}
.directory-picker span{color:#3d5f96;font-size:var(--fs-12,12px)}
.directory-picker small{color:#8392a6;font-size:var(--fs-10,10px);line-height:1.45}
label{display:flex;flex-direction:column;gap:7px;color:#405773;font-size:var(--fs-11,11px);font-weight:650}
input{box-sizing:border-box;width:100%;padding:10px 11px;border:1px solid #cedaea;border-radius:9px;outline:0;color:#304965;background:#fbfcff;font:inherit}
.bound-block{display:flex;flex-direction:column;gap:8px;padding:12px;border:1px solid #e1e8f2;border-radius:10px;background:#f8fafc}
.bound-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px}
.bound-head b{color:#40546d;font-size:var(--fs-12,12px)}
.bound-head small{color:#8392a6;font-size:var(--fs-10,10px)}
.bound-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.output-box{display:flex;flex-direction:column;gap:8px;padding:12px;border:1px solid #d5deea;border-radius:10px;background:#f7f9fc}
.output-box .check-label{flex-direction:row;align-items:flex-start;gap:8px;margin:0}
.output-box .check-label input{width:16px;height:16px;flex:none;margin:2px 0 0;padding:0}
.output-box .check-label span{font-weight:650;line-height:1.45}
.output-box small{display:block;margin:0;color:#70829b;font-size:var(--fs-11,11px);line-height:1.5;font-weight:400}
.output-box label:not(.check-label){margin:0}
.primary-action{padding:11px;border:0;border-radius:10px;color:#fff;background:linear-gradient(135deg,#4775d5,#6959d6);font-size:var(--fs-11,11px);font-weight:750;cursor:pointer}
.primary-action:disabled{opacity:.45;cursor:not-allowed}
.map-panel{display:flex;min-width:0;flex-direction:column;overflow:hidden}
.map-toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px;padding:12px 14px;border-bottom:1px solid #e0e7f0}
.map-toolbar b{display:block;color:#304963;font-size:var(--fs-13,13px)}
.map-toolbar small{color:#8392a6;font-size:var(--fs-10,10px);line-height:1.45}
.map-toolbar button{flex:none;padding:6px 10px;border:1px solid #cbd8e9;border-radius:8px;color:#5a6f89;background:#fff;font-size:var(--fs-11,11px);cursor:pointer}
.map-toolbar button:disabled{opacity:.45;cursor:not-allowed}
.clip-map{flex:1;min-height:520px;background:#e8eef5}
.raster-clip-page .task{grid-template-columns:minmax(200px,1.15fr) minmax(180px,1.4fr) 86px minmax(150px,auto)}
.raster-clip-page .task-main,.raster-clip-page .task-main>div{min-width:0}
.raster-clip-page .task-main b{overflow-wrap:anywhere}
.queue{margin-top:18px}
.error{color:#a34b4b}
@media(max-width:850px){
  .raster-clip-page{padding:66px 14px 24px}
  .raster-clip-grid{grid-template-columns:1fr}
  .clip-map{min-height:420px}
  .raster-clip-page .task{grid-template-columns:minmax(0,1fr)}
}
</style>
