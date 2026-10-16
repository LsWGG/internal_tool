<script setup>
import {computed,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import axios from 'axios'

const props=defineProps({confirmAction:{type:Function,default:null},aiPatch:{type:Object,default:null}})
const emit=defineEmits(['notify','open-shp'])
const notify=(message,type='success')=>emit('notify',message,type)
const sourceMode=ref('path'),sourcePath=ref(''),outputPath=ref(''),targetFormat=ref('shp'),inPlace=ref(false),busy=ref(false),tasks=ref([])
const sourceFiles=ref([]),sourceInput=ref(null)
const formats=[{id:'shp',name:'Shapefile'},{id:'geojson',name:'GeoJSON'},{id:'kml',name:'KML'},{id:'gpkg',name:'GeoPackage'},{id:'dxf',name:'DXF'}]
const canSubmit=computed(()=>sourceMode.value==='upload'?sourceFiles.value.length>0:!!sourcePath.value.trim())
const placeHint=computed(()=>{
  if(inPlace.value&&sourceMode.value==='upload')return '写在这次上传的副本里，不会改你电脑上的原目录。不覆盖源文件。删除任务会删掉这次上传的副本。'
  if(inPlace.value)return '新格式和源文件放在同一目录，不覆盖源文件。删除任务只删这次写出的文件。'
  if(sourceMode.value==='upload')return '写入单独的输出目录。留空则放到工具目录并打包，完成后可以下载。删除任务会删掉这次上传的副本。'
  return '写入单独的输出目录。留空则放到工具目录并打包，完成后可以下载。'
})
let timer

async function loadTasks(){try{tasks.value=(await axios.get('/api/vector/tasks')).data}catch(error){console.error(error)}}
function chooseSource(event){sourceFiles.value=[...event.target.files]}
function applyPatch(values){
  if(!values)return
  if(typeof values.source_path==='string'&&values.source_path.trim()){sourceMode.value='path';sourcePath.value=values.source_path}
  if(typeof values.output_path==='string')outputPath.value=values.output_path
  if(typeof values.in_place==='boolean')inPlace.value=values.in_place
  if(formats.some(item=>item.id===values.target_format))targetFormat.value=values.target_format
}
watch(()=>props.aiPatch?.revision,()=>applyPatch(props.aiPatch))
async function submit(){
  if(sourceMode.value==='upload'){if(!sourceFiles.value.length)return notify('请选择要上传的目录。','error')}
  else if(!sourcePath.value.trim())return notify('请填写存放矢量文件的目录。','error')
  busy.value=true
  try{
    if(sourceMode.value==='upload'){
      const data=new FormData()
      sourceFiles.value.forEach(file=>data.append('files',file,file.webkitRelativePath||file.name))
      data.append('target_format',targetFormat.value)
      data.append('output_path',inPlace.value?'':outputPath.value.trim())
      data.append('in_place',inPlace.value?'true':'false')
      await axios.post('/api/vector/uploads',data)
      sourceFiles.value=[]
      if(sourceInput.value)sourceInput.value.value=''
    }else await axios.post('/api/vector/tasks',{source_path:sourcePath.value.trim(),output_path:inPlace.value?'':outputPath.value.trim(),target_format:targetFormat.value,in_place:inPlace.value})
    notify('转换任务已加入队列。')
    await loadTasks()
  }catch(error){notify(error.response?.data?.detail||error.message,'error')}
  finally{busy.value=false}
}
async function control(task,action){
  if(action==='delete'){
    const message=task.request?.upload_dir?'删除这个任务？会删掉这次上传的副本和任务输出，不会改你电脑上的原目录。':task.request?.in_place?'从列表删除这个任务？只会删掉这个任务写出的文件，源文件会保留。':'删除这个任务及其输出？源目录不会被删除。'
    if(props.confirmAction&&!await props.confirmAction(message))return
  }
  try{
    if(action==='delete')await axios.delete(`/api/vector/tasks/${task.id}`)
    else await axios.post(`/api/vector/tasks/${task.id}/${action}`)
    await loadTasks()
  }catch(error){notify(error.response?.data?.detail||error.message,'error')}
}
function statusText(status){return {queued:'排队中',running:'进行中',paused:'已暂停',completed:'已完成',failed:'失败',cancelled:'已取消'}[status]||status}
onMounted(()=>{loadTasks();timer=setInterval(loadTasks,2000)})
onBeforeUnmount(()=>clearInterval(timer))
</script>

<template>
  <section class="vector-page">
    <header><div><span class="eyebrow">VECTOR CONVERT</span><h1>矢量格式转换</h1><p>GeoJSON、KML、GPKG、DXF 与 SHP 互转。只处理一个目录，不递归子目录。</p></div></header>
    <div class="vector-grid">
      <aside class="panel">
        <div class="panel-title"><div><b>转换配置</b><small>服务器路径，或从浏览器上传一个目录</small></div></div>
        <div class="source-mode" role="tablist"><button type="button" :class="{active:sourceMode==='path'}" @click="sourceMode='path'">服务器路径</button><button type="button" :class="{active:sourceMode==='upload'}" @click="sourceMode='upload'">上传目录</button></div>
        <label v-if="sourceMode==='path'">源目录<input v-model="sourcePath" placeholder="/mnt/external/vectors" spellcheck="false"></label>
        <label v-else class="directory-picker"><input ref="sourceInput" type="file" webkitdirectory directory multiple @change="chooseSource"><span>{{sourceFiles.length?`已选择 ${sourceFiles.length} 个文件`:'选择一个矢量目录'}}</span><small>只处理这一层，不进子目录。最多 5000 个文件、2GB</small></label>
        <label>目标格式<select v-model="targetFormat"><option v-for="item in formats" :key="item.id" :value="item.id">{{item.name}}</option></select></label>
        <div class="output-box">
          <label class="check-label"><input v-model="inPlace" type="checkbox"><span>写回源目录，不打包下载</span></label>
          <small>{{placeHint}}</small>
          <label v-if="!inPlace">输出路径<input v-model="outputPath" placeholder="留空则写到工具目录" spellcheck="false"></label>
        </div>
        <button class="primary-action" :disabled="busy||!canSubmit" @click="submit">{{busy?'正在提交…':'加入队列'}}</button>
      </aside>
      <section class="panel notes">
        <span class="eyebrow">ONE DIRECTORY</span>
        <h2>一次只转一个目录</h2>
        <p>不进子目录。坐标系从文件读取，读不到就失败，提示「无法识别坐标系」，不会猜一个。不会另开地图，预览缓存留在 SHP 工具自己的目录里。</p>
        <ul>
          <li><b>SHP</b><span>每个图层一个 shapefile，完成后可在现有预览里打开。</span></li>
          <li><b>GeoJSON</b><span>每个图层一个 .geojson。</span></li>
          <li><b>KML</b><span>每个图层一个文件，写出时转到 EPSG:4326。</span></li>
          <li><b>GPKG</b><span>一个文件里放多个图层。</span></li>
          <li><b>DXF</b><span>只保留几何，属性可能丢掉。</span></li>
        </ul>
      </section>
    </div>
    <section class="queue">
      <div class="queue-head"><div><span class="eyebrow">CONVERT QUEUE</span><h2>转换任务</h2></div></div>
      <div v-if="!tasks.length" class="empty">暂无转换任务。</div>
      <article v-for="task in tasks" :key="task.id" class="task">
        <div class="task-main"><div><b>{{task.request?.source_path||task.id}}</b><small>{{(task.request?.target_format||'').toUpperCase()}} · {{task.request?.upload_dir?'上传的副本':'服务器路径'}} · {{task.request?.in_place?'写回源目录':'单独目录'}}</small></div></div>
        <div class="progress"><div><span>{{task.message}}</span><strong>{{task.progress}}%</strong></div><div class="track"><i :class="task.status" :style="{width:(task.progress||0)+'%'}"></i></div><small v-if="task.error" class="error">{{task.error}}</small></div>
        <span class="badge" :class="task.status">{{statusText(task.status)}}</span>
        <div class="task-actions">
          <button v-if="task.status==='queued'||task.status==='running'" @click="control(task,'pause')">暂停</button>
          <button v-if="task.status==='paused'" @click="control(task,'resume')">继续</button>
          <button v-if="task.status==='failed'||task.status==='cancelled'" @click="control(task,'retry')">重试</button>
          <button v-if="task.result?.shapefile_dir" @click="emit('open-shp',task.result.shapefile_dir)">在 SHP 预览中打开</button>
          <a v-if="task.result?.archive" :href="`/api/vector/tasks/${task.id}/export`">下载</a>
          <button class="danger" @click="control(task,'delete')">删除</button>
        </div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.vector-page{max-width:1460px;margin:0 auto;padding:74px 28px 34px}
.vector-page>header{padding:0 2px 22px;border-bottom:1px solid #d9e3f2}
.vector-page>header p{margin:0;color:#70829b;font-size:var(--fs-13,13px);line-height:1.6}
.eyebrow{color:#5275c7;font-size:var(--fs-10,10px);font-weight:800;letter-spacing:1.7px}
.vector-grid{display:grid;grid-template-columns:minmax(300px,400px) minmax(0,1.45fr);gap:18px;align-items:stretch;margin-top:20px}
.panel{border:1px solid #d8e2ef;border-radius:16px;background:#fff;box-shadow:0 8px 25px #31527c0d}
.vector-grid>aside{display:flex;flex-direction:column;gap:12px;padding:18px;min-width:0}
.vector-grid>aside label{margin-top:0}
.notes{display:flex;min-width:0;min-height:480px;flex-direction:column;justify-content:center;padding:36px 40px;background:linear-gradient(165deg,#f4f8fd,#f7f5fd)}
.notes h2{margin:8px 0 12px;color:#263d5b;font-size:var(--fs-26,26px);line-height:1.3}
.notes>p{max-width:640px;margin:0;color:#5f7088;font-size:var(--fs-14,14px);line-height:1.7}
.notes ul{display:grid;gap:0;max-width:640px;margin:26px 0 0;padding:0;list-style:none;border-top:1px solid #d5deea}
.notes li{display:grid;grid-template-columns:88px minmax(0,1fr);gap:14px;padding:13px 0;border-bottom:1px solid #e1e8f2}
.notes li b{color:#40546d;font-size:var(--fs-12,12px)}
.notes li span{color:#5f7088;font-size:var(--fs-13,13px);line-height:1.55}
.panel-title b{color:#263d5b;font-size:var(--fs-14,14px)}
.panel-title small{color:#8392a6;font-size:var(--fs-10,10px);line-height:1.5}
.source-mode{display:grid;grid-template-columns:1fr 1fr;padding:3px;border-radius:10px;background:#edf2f9}
.source-mode button{padding:8px;border:0;border-radius:8px;color:#657994;background:transparent;font:inherit;font-size:var(--fs-11,11px);cursor:pointer}
.source-mode button.active{color:#315eb9;background:#fff;box-shadow:0 2px 8px #35587d18}
.directory-picker{align-items:center;padding:16px 12px;border:1px dashed #aebfda;border-radius:12px;background:#f7faff;text-align:center;cursor:pointer}
.directory-picker input{display:none;width:auto;height:auto;padding:0}
.directory-picker span{color:#3d5f96;font-size:var(--fs-12,12px)}
.directory-picker small{color:#8392a6;font-size:var(--fs-10,10px);line-height:1.45}
label{display:flex;flex-direction:column;gap:7px;color:#405773;font-size:var(--fs-11,11px);font-weight:650}
input,select{box-sizing:border-box;width:100%;padding:10px 11px;border:1px solid #cedaea;border-radius:9px;outline:0;color:#304965;background:#fbfcff;font:inherit}
.output-box{display:flex;flex-direction:column;gap:8px;padding:12px;border:1px solid #d5deea;border-radius:10px;background:#f7f9fc}
.output-box .check-label{flex-direction:row;align-items:flex-start;gap:8px;margin:0}
.output-box .check-label input{width:16px;height:16px;flex:none;margin:2px 0 0;padding:0}
.output-box .check-label span{font-weight:650;line-height:1.45}
.output-box small{display:block;margin:0;color:#70829b;font-size:var(--fs-11,11px);line-height:1.5;font-weight:400}
.output-box label:not(.check-label){margin:0}
.primary-action{padding:11px;border:0;border-radius:10px;color:#fff;background:linear-gradient(135deg,#4775d5,#6959d6);font-size:var(--fs-11,11px);font-weight:750;cursor:pointer}
.primary-action:disabled{opacity:.45;cursor:not-allowed}
.vector-page .task{grid-template-columns:minmax(200px,1.15fr) minmax(180px,1.3fr) 86px minmax(168px,auto)}
.vector-page .task-main,.vector-page .task-main>div{min-width:0}
.vector-page .task-main b{overflow-wrap:anywhere}
.queue{margin-top:18px}
.error{color:#a34b4b}
@media(max-width:850px){
  .vector-page{padding:66px 14px 24px}
  .vector-grid{grid-template-columns:1fr}
  .notes{min-height:0;padding:24px 20px}
  .notes li{grid-template-columns:72px minmax(0,1fr)}
  .vector-page .task{grid-template-columns:minmax(0,1fr)}
}
</style>
