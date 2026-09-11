<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,ref} from 'vue'
import L from 'leaflet'
import axios from 'axios'
import {tileSources} from './tileSources'

const props=defineProps({confirmAction:{type:Function,default:null}})
const emit=defineEmits(['notify'])
const mapEl=ref(),mode=ref('online'),url=ref(''),name=ref(''),minZoom=ref(0),maxZoom=ref(20),opacity=ref(100)
const files=ref([]),input=ref(),uploading=ref(false),layers=ref([]),activeId=ref('')
const onlineLayerName=ref(''),baseVisible=ref(true),previewVisible=ref(true)
const onlinePresets=tileSources.map(source=>({...source,min:source.minZoom,max:source.maxZoom}))
const presetId=ref('osm_standard')
let map,baseLayer,previewLayer
const activeLayer=computed(()=>activeId.value==='online'?{kind:'online',name:onlineLayerName.value||'在线 XYZ 图层'}:layers.value.find(layer=>layer.id===activeId.value)?{kind:'upload',name:layers.value.find(layer=>layer.id===activeId.value).name}:{kind:'base',name:'Google 卫星影像'})

const notify=(message,type='success')=>emit('notify',message,type)
function initMap(){
  if(map||!mapEl.value)return
  map=L.map(mapEl.value,{zoomControl:true}).setView([31.23,121.47],5)
  baseLayer=L.tileLayer('https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',{maxZoom:20,attribution:'Satellite imagery © Google'}).addTo(map)
}
function normalizeUrl(value){return String(value||'').trim().replace(/\{zoom\}/gi,'{z}').replace(/\{col\}/gi,'{x}').replace(/\{row\}/gi,'{y}')}
function quadkey(x,y,z){let key='';for(let level=z;level>0;level--){let digit=0,mask=1<<(level-1);if(x&mask)digit++;if(y&mask)digit+=2;key+=digit}return key}
function applyPreset(id=presetId.value){
  const preset=onlinePresets.find(item=>item.id===id)
  if(!preset)return
  url.value=preset.url;minZoom.value=preset.min;maxZoom.value=preset.max;onlineLayerName.value=preset.name
}
function showOnline(){
  const template=normalizeUrl(url.value)
  const preset=onlinePresets.find(item=>item.id===presetId.value)
  const validTokens=preset?.quadkey?template.includes('{q}'):['{z}','{x}','{y}'].every(token=>template.includes(token))
  if(!/^https?:\/\//i.test(template)||!validTokens)return notify('请输入包含 {z}、{x}、{y} 的 HTTP/HTTPS 瓦片地址。','error')
  if(previewLayer)map.removeLayer(previewLayer)
  const options={minZoom:Number(minZoom.value),maxNativeZoom:Number(maxZoom.value),maxZoom:24,opacity:Number(opacity.value)/100,errorTileUrl:'',attribution:preset?.attribution||''}
  if(preset?.quadkey){const QuadLayer=L.TileLayer.extend({getTileUrl(coords){return template.replace('{q}',quadkey(coords.x,coords.y,coords.z))}});previewLayer=new QuadLayer('',options)}
  else if(preset?.id==='amap_hybrid'){
    const labels=L.tileLayer('https://wprd02.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scl=1&style=8&ltype=7&x={x}&y={y}&z={z}',options)
    previewLayer=L.layerGroup([L.tileLayer(template,options),labels])
  }else previewLayer=L.tileLayer(template,options)
  previewLayer.addTo(map)
  previewVisible.value=true
  const exactPreset=onlinePresets.find(item=>item.url===url.value)
  if(exactPreset)onlineLayerName.value=exactPreset.name
  else try{onlineLayerName.value=new URL(template).hostname}catch{onlineLayerName.value='在线 XYZ 图层'}
  activeId.value='online';notify('在线瓦片图层已加载。')
}
function chooseDirectory(event){files.value=[...event.target.files].filter(file=>/\.(png|jpe?g|webp)$/i.test(file.name))}
async function uploadDirectory(){
  if(!files.value.length)return notify('请选择包含 z/x/y 图片的瓦片目录。','error')
  const data=new FormData();files.value.forEach(file=>data.append('files',file,file.webkitRelativePath||file.name));data.append('name',name.value)
  uploading.value=true
  try{const layer=(await axios.post('/api/tile-preview/layers',data)).data;files.value=[];name.value='';if(input.value)input.value.value='';await loadLayers();showLocal(layer);notify(`已识别 ${layer.tile_count} 个瓦片，层级 Z${layer.min_zoom}–Z${layer.max_zoom}。`)}
  catch(error){notify(error.response?.data?.detail||error.message,'error')}finally{uploading.value=false}
}
async function loadLayers(){try{layers.value=(await axios.get('/api/tile-preview/layers')).data}catch(error){console.error(error)}}
function showLocal(layer){
  if(previewLayer)map.removeLayer(previewLayer)
  previewLayer=L.tileLayer(`/api/tile-preview/layers/${layer.id}/{z}/{x}/{y}`,{minZoom:layer.min_zoom,maxNativeZoom:layer.max_zoom,maxZoom:24,opacity:Number(opacity.value)/100,errorTileUrl:''}).addTo(map)
  previewVisible.value=true
  activeId.value=layer.id
  const b=layer.bounds;map.fitBounds([[b.south,b.west],[b.north,b.east]],{padding:[24,24],maxZoom:layer.max_zoom})
}
function updateOpacity(){previewLayer?.setOpacity(Number(opacity.value)/100)}
function toggleBase(){baseVisible.value=!baseVisible.value;if(baseVisible.value)baseLayer.addTo(map);else map.removeLayer(baseLayer)}
function togglePreview(){if(!previewLayer)return;previewVisible.value=!previewVisible.value;if(previewVisible.value)previewLayer.addTo(map);else map.removeLayer(previewLayer)}
function clearPreview(){if(previewLayer)map.removeLayer(previewLayer);previewLayer=null;activeId.value=''}
async function removeLayer(layer){
  if(props.confirmAction&&!await props.confirmAction(`删除“${layer.name}”及上传的全部瓦片文件？`))return
  try{await axios.delete(`/api/tile-preview/layers/${layer.id}`);if(activeId.value===layer.id)clearPreview();await loadLayers();notify('本地瓦片图层已删除。')}
  catch(error){notify(error.response?.data?.detail||error.message,'error')}
}
onMounted(async()=>{applyPreset();await nextTick();initMap();await loadLayers()})
onBeforeUnmount(()=>map?.remove())
</script>

<template>
  <section class="tile-viewer-page">
    <header><div><span class="eyebrow">MAP TILE VIEWER</span><h1>地图瓦片查看</h1><p>加载在线 XYZ 链接或本地瓦片目录，在地图中检查覆盖范围、层级和图像质量。</p></div></header>
    <div class="tile-viewer-workspace">
      <aside class="panel tile-viewer-config">
        <div class="panel-title"><div><b>加载瓦片</b><small>支持标准 Web Mercator XYZ 瓦片</small></div></div>
        <div class="segmented"><button :class="{active:mode==='online'}" @click="mode='online'">在线链接</button><button :class="{active:mode==='local'}" @click="mode='local'">上传目录</button></div>
        <template v-if="mode==='online'">
          <label>内置在线链接<select v-model="presetId" @change="applyPreset()"><option v-for="preset in onlinePresets" :key="preset.id" :value="preset.id">{{preset.name}} · Z{{preset.min}}–Z{{preset.max}}</option><option value="custom">自定义链接</option></select><small>选择后自动填入地址及有效层级，仍可继续修改</small></label>
          <label class="tile-url">瓦片地址<textarea v-model="url" rows="3" placeholder="https://example.com/tiles/{z}/{x}/{y}.png"></textarea><small>支持 {z}/{x}/{y}；Bing 内置链接使用 {q} QuadKey</small></label>
          <div class="tile-zoom-grid"><label>最小层级<input v-model.number="minZoom" type="number" min="0" max="24"></label><label>最大层级<input v-model.number="maxZoom" type="number" min="0" max="24"></label></div>
          <button class="primary-action" @click="showOnline">加载在线瓦片</button>
        </template>
        <template v-else>
          <label>图层名称（可选）<input v-model="name" placeholder="默认使用目录名称"></label>
          <label class="directory-picker"><input ref="input" type="file" webkitdirectory directory multiple @change="chooseDirectory"><span>{{files.length?`已选择 ${files.length} 个图片瓦片`:'选择 z/x/y 瓦片目录'}}</span><small>PNG · JPG · JPEG · WEBP，最大 2GB</small></label>
          <button class="primary-action" :disabled="uploading||!files.length" @click="uploadDirectory">{{uploading?'正在上传并识别…':'上传并预览'}}</button>
        </template>
        <label class="opacity-control">图层透明度 <b>{{opacity}}%</b><input v-model.number="opacity" type="range" min="0" max="100" @input="updateOpacity"></label>
        <div class="layer-catalog">
          <div class="layer-group-title"><span>系统底图</span><small>系统自带</small></div>
          <article class="base-layer-row"><span class="source-icon base">底</span><div><b>Google 卫星影像</b><small>不是上传内容</small></div><button class="visibility-toggle" :class="{off:!baseVisible}" @click="toggleBase">{{baseVisible?'显示中':'已隐藏'}}</button></article>
          <div class="layer-group-title custom"><span>我的自定义图层</span><small>{{layers.length+(activeId==='online'?1:0)}} 个</small></div>
          <article v-if="activeId==='online'" class="custom-layer online-row active"><span class="source-icon online">链</span><div><b>{{onlineLayerName}}</b><small>在线 URL · Z{{minZoom}}–Z{{maxZoom}}</small></div><button class="visibility-toggle" :class="{off:!previewVisible}" @click="togglePreview">{{previewVisible?'显示中':'已隐藏'}}</button></article>
          <article v-for="layer in layers" :key="layer.id" class="custom-layer upload-row" :class="{active:activeId===layer.id}"><span class="source-icon upload">传</span><button class="layer-main" @click="showLocal(layer)"><b>{{layer.name}}</b><small>本地上传 · Z{{layer.min_zoom}}–Z{{layer.max_zoom}} · {{layer.tile_count}} 个</small></button><button v-if="activeId===layer.id" class="visibility-toggle" :class="{off:!previewVisible}" @click="togglePreview">{{previewVisible?'显示中':'已隐藏'}}</button><button class="layer-delete" title="删除上传图层" @click="removeLayer(layer)">×</button></article>
          <div v-if="!layers.length&&activeId!=='online'" class="empty-custom-layers">尚未加载在线链接或上传目录</div>
        </div>
      </aside>
      <section class="panel tile-viewer-map-panel"><div class="map-toolbar"><div><b>瓦片预览</b><small><span class="source-badge" :class="activeLayer.kind">{{activeLayer.kind==='base'?'内置底图':activeLayer.kind==='online'?'在线链接':'上传目录'}}</span>{{activeLayer.name}}</small></div><button v-if="activeId" @click="clearPreview">仅显示内置底图</button></div><div class="map-stage"><div ref="mapEl" class="tile-viewer-map"></div><div class="map-layer-key"><span><i class="base"></i>内置底图</span><span><i class="online"></i>在线链接</span><span><i class="upload"></i>上传目录</span></div></div></section>
    </div>
  </section>
</template>

<style scoped>
.tile-viewer-page{max-width:1460px;margin:0 auto;padding:74px 28px 34px}.tile-viewer-page>header{padding:0 2px 22px;border-bottom:1px solid #d9e3f2}.tile-viewer-page h1{margin:7px 0 5px;color:#203858;font-size:28px}.tile-viewer-page p{margin:0;color:#70829b;font-size:13px}.eyebrow{color:#5275c7;font-size:10px;font-weight:800;letter-spacing:1.7px}.tile-viewer-workspace{display:grid;grid-template-columns:350px minmax(0,1fr);gap:18px;margin-top:20px}.panel{border:1px solid #d8e2ef;border-radius:16px;background:#fff;box-shadow:0 8px 25px #31527c0d}.tile-viewer-config{display:flex;flex-direction:column;gap:15px;padding:18px}.panel-title{display:flex;justify-content:space-between}.panel-title div{display:flex;flex-direction:column;gap:3px}.panel-title b{color:#263d5b;font-size:14px}.panel-title small,label small,.map-toolbar small{color:#8392a6;font-size:10px}.segmented{display:grid;grid-template-columns:1fr 1fr;padding:3px;border-radius:10px;background:#edf2f9}.segmented button{padding:8px;border:0;border-radius:8px;color:#657994;background:transparent;cursor:pointer}.segmented button.active{color:#315eb9;background:#fff;box-shadow:0 2px 8px #35587d18}label{display:flex;flex-direction:column;gap:7px;color:#405773;font-size:11px;font-weight:650}input,textarea,select{box-sizing:border-box;width:100%;padding:10px 11px;border:1px solid #cedaea;border-radius:9px;outline:0;color:#304965;background:#fbfcff;font:11px/1.5 inherit}select{appearance:none;padding-right:34px;background:#fbfcff url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23687992' stroke-width='2'%3E%3Cpath d='m6 9 6 6 6-6'/%3E%3C/svg%3E") no-repeat right 11px center}textarea{resize:vertical}.tile-zoom-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.primary-action{padding:11px;border:0;border-radius:10px;color:#fff;background:linear-gradient(135deg,#4775d5,#6959d6);font-size:11px;font-weight:750;cursor:pointer}.primary-action:disabled{opacity:.45;cursor:not-allowed}.directory-picker{align-items:center;padding:23px 12px;border:1px dashed #aebfda;border-radius:12px;background:#f7faff;text-align:center;cursor:pointer}.directory-picker input{display:none}.directory-picker span{color:#3d5f96;font-size:12px}.opacity-control{display:grid;grid-template-columns:1fr auto;align-items:center;padding-top:4px}.opacity-control input{grid-column:1/-1;padding:0;border:0}.local-layer-list{display:grid;gap:7px;padding-top:4px;border-top:1px solid #e5ebf3}.list-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:2px}.list-heading b{font-size:11px}.list-heading small{font-size:9px;color:#8795a8}.local-layer-list article{display:flex;align-items:center;border:1px solid #e0e7f0;border-radius:10px;overflow:hidden}.local-layer-list article.active{border-color:#7999da;background:#f5f8ff}.layer-main{display:flex;min-width:0;flex:1;flex-direction:column;align-items:start;gap:3px;padding:9px 10px;border:0;background:transparent;text-align:left;cursor:pointer}.layer-main b{max-width:100%;overflow:hidden;color:#405774;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.layer-main small{color:#8a99ac;font-size:9px}.layer-delete{width:34px;height:34px;border:0;color:#a05b66;background:transparent;font-size:18px;cursor:pointer}.tile-viewer-map-panel{min-width:0;overflow:hidden}.map-toolbar{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid #e0e7f0}.map-toolbar>div{display:flex;flex-direction:column;gap:3px}.map-toolbar b{color:#304963;font-size:13px}.map-toolbar button{padding:7px 10px;border:1px solid #cbd8e9;border-radius:8px;color:#5a6f89;background:#fff;font-size:10px;cursor:pointer}.tile-viewer-map{height:650px;background:#e8eef5}@media(max-width:900px){.tile-viewer-page{padding:66px 14px 24px}.tile-viewer-workspace{grid-template-columns:1fr}.tile-viewer-map{height:520px}}
.layer-catalog{display:grid;gap:7px;padding-top:4px;border-top:1px solid #e5ebf3}.layer-catalog article{display:flex;align-items:center;gap:8px;min-width:0;padding-left:8px;border:1px solid #e0e7f0;border-radius:10px;overflow:hidden;background:#fff}.layer-catalog article.active{border-color:#7999da;background:#f5f8ff}.layer-catalog article>div{display:flex;min-width:0;flex:1;flex-direction:column;gap:3px;padding:9px 8px 9px 0}.layer-catalog article>div b{overflow:hidden;color:#405774;font-size:10px;text-overflow:ellipsis;white-space:nowrap}.layer-catalog article>div small{color:#8a99ac;font-size:9px}.source-badge{display:inline-flex;flex:none;align-items:center;width:max-content;padding:3px 6px;border-radius:5px;font-size:8px;font-weight:800;line-height:1.2}.source-badge.base{color:#53677f;background:#e8edf3}.source-badge.online{color:#3566bd;background:#e6efff}.source-badge.upload{color:#6a50ad;background:#eee9fb}.layer-catalog .layer-main{padding-left:0}.map-toolbar small{display:flex;align-items:center;gap:7px}.map-stage{position:relative}.map-layer-key{position:absolute;right:12px;bottom:12px;z-index:500;display:flex;gap:10px;padding:7px 9px;border:1px solid #dbe4ef;border-radius:9px;color:#61758f;background:#fffffff0;box-shadow:0 4px 14px #344d6c1a;font-size:9px}.map-layer-key span{display:flex;align-items:center;gap:4px}.map-layer-key i{width:7px;height:7px;border-radius:50%}.map-layer-key i.base{background:#8998aa}.map-layer-key i.online{background:#4d7bd5}.map-layer-key i.upload{background:#8064c5}
.layer-group-title{display:flex;align-items:center;justify-content:space-between;margin-top:2px;padding:5px 2px 2px;color:#5f7188;font-size:9px;font-weight:800;letter-spacing:.5px}.layer-group-title.custom{margin-top:7px;padding-top:10px;border-top:1px dashed #dce5f0;color:#526fbd}.layer-group-title small{color:#96a2b1;font-size:8px;font-weight:600;letter-spacing:0}.source-icon{display:grid;place-items:center;flex:none;width:25px;height:25px;border-radius:7px;font-size:9px;font-weight:900}.source-icon.base{color:#5e6f83;background:#e9edf2}.source-icon.online{color:#2861bf;background:#e3eeff}.source-icon.upload{color:#6746b3;background:#eee7ff}.layer-catalog article.custom-layer{border-left-width:3px}.layer-catalog article.online-row{border-left-color:#4d7bd5}.layer-catalog article.upload-row{border-left-color:#8064c5}.visibility-toggle{flex:none;margin-right:7px;padding:5px 7px;border:1px solid #b8cae5;border-radius:7px;color:#3562ad;background:#f5f8ff;font-size:8px;font-weight:700;cursor:pointer}.visibility-toggle.off{border-color:#d6dde7;color:#8996a7;background:#f4f6f8}.empty-custom-layers{padding:10px;border:1px dashed #d8e1ec;border-radius:9px;color:#95a1b1;text-align:center;font-size:9px}
</style>
