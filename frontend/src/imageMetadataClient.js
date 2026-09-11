import MetadataWorker from './metadata.worker.js?worker'
import {metadataUuid} from './metadataUuid'
import {decodeImagePreview,releaseImagePreview} from './metadataPreview'
import {fieldInfo} from './metadataFields'
import {zip,strToU8} from 'fflate'

// Originals and edited copies live in browser storage; no image requests are sent to the server.
let worker, sequence=0, catalogPromise, dbPromise,executeQueue=Promise.resolve()
const pending=new Map(), urls=new Map(), records=new Map(),ruleCheckCache=new Map(),metadataReadCache=new Map()
const structural=new Set(['ImageWidth','ImageHeight','ExifImageWidth','ExifImageHeight','PixelXDimension','PixelYDimension','Compression','BitsPerSample','SamplesPerPixel','StripOffsets','StripByteCounts','TileOffsets','TileByteCounts','PhotometricInterpretation','ExifOffset','GPSInfo','InteropOffset','SubIFD','ThumbnailOffset','ThumbnailLength'])
const allowed=new Set(['JPEG','PNG','WEBP','HEIC','HEIF','AVIF','TIFF','BMP','GIF'])
const normalizedFormat=value=>value==='Extended WEBP'?'WEBP':value
const filePattern=/\.(jpe?g|png|webp|heic|heif|avif|tiff?|bmp|gif)$/i
function resetWorker(message){for(const request of pending.values()){clearTimeout(request.timer);request.reject(new Error(message||'元数据引擎已重置'))}pending.clear();worker?.terminate();worker=null}
function executeOnce(action,file,extension,tags){
  if(!worker){worker=new MetadataWorker();worker.onmessage=({data})=>{const request=pending.get(data.id);if(!request)return;clearTimeout(request.timer);pending.delete(data.id);data.error?request.reject(new Error(data.error)):request.resolve(data.data)};worker.onerror=()=>resetWorker('元数据引擎加载失败，请刷新页面重试')}
  return new Promise((resolve,reject)=>{const id=++sequence,timer=setTimeout(()=>resetWorker('处理超时，当前图片未保存；请使用较小的图片重试'),180000);pending.set(id,{resolve,reject,timer});worker.postMessage({id,action,file,extension,tags})})
}
async function executeWithRetry(action,file,extension,tags){
  try{return await executeOnce(action,file,extension,tags)}
  catch(error){
    if(!/offset is out of bounds|out of bounds offset/i.test(String(error?.message||error)))throw error
    resetWorker('offset is out of bounds; metadata engine reset');return executeOnce(action,file,extension,tags)
  }
}
function execute(action,file,extension,tags){
  const request=executeQueue.then(()=>executeWithRetry(action,file,extension,tags))
  executeQueue=request.catch(()=>{})
  return request
}
function db(){if(!dbPromise)dbPromise=new Promise((resolve,reject)=>{const request=indexedDB.open('image-metadata-studio',1);request.onupgradeneeded=()=>request.result.createObjectStore('images',{keyPath:'id'});request.onsuccess=()=>resolve(request.result);request.onerror=()=>reject(request.error)});return dbPromise}
async function transaction(mode,operation){const database=await db();return new Promise((resolve,reject)=>{const tx=database.transaction('images',mode),request=operation(tx.objectStore('images'));let value;request.onsuccess=()=>{value=request.result};tx.oncomplete=()=>resolve(value);tx.onerror=()=>reject(tx.error);tx.onabort=()=>reject(tx.error||new Error('保存失败，浏览器存储空间可能不足'))})}
function previewUrl(record){const prior=urls.get(record.id);if(prior?.revision===record.revision)return prior.url;if(prior)URL.revokeObjectURL(prior.url);const url=URL.createObjectURL(record.file);urls.set(record.id,{url,revision:record.revision});return url}
function summary(record,preview=true){return {id:record.id,name:record.name,format:record.format,extension:record.extension,size:record.file.size,revision:record.revision,preview_url:preview&&!/^(HEIC|HEIF)$/.test(record.format)?previewUrl(record):null}}
async function find(id){if(records.has(id))return records.get(id);const record=await transaction('readonly',store=>store.get(id));if(!record)throw new Error('图片已删除或不存在');records.set(id,record);return record}
async function raw(file,extension){const data=JSON.parse(await execute('read',file,extension));if(!data?.[0])throw new Error('未读取到图片信息');return data[0]}
async function cachedRaw(record){const key=`${record.id}:${record.revision}`;if(metadataReadCache.has(key))return metadataReadCache.get(key);const metadata=await raw(record.file,record.extension);metadataReadCache.set(key,metadata);if(metadataReadCache.size>300){const oldest=metadataReadCache.keys().next().value;metadataReadCache.delete(oldest)}return metadata}
async function catalog(file,extension){
  if(!catalogPromise)catalogPromise=(async()=>{
    const xml=await execute('catalog',file,extension),end=xml.lastIndexOf('</taginfo>')
    const doc=new DOMParser().parseFromString(end>=0?xml.slice(0,end+10):xml,'application/xml')
    if(doc.querySelector('parsererror'))throw new Error('字段目录加载失败')
    const result={}
    for(const table of doc.querySelectorAll('table')){
      const base=table.getAttribute('g1')||''
      if(!/^[\w-]+$/.test(base))continue
      for(const tag of table.querySelectorAll(':scope > tag')){
        const name=tag.getAttribute('name'),group=tag.getAttribute('g1')||base,key=`${group}:${name}`
        const flags=tag.getAttribute('flags')||''
        if(tag.getAttribute('writable')!=='true'||!/^\w+$/.test(name)||structural.has(name)||['System','File','Composite'].includes(group)||/Unsafe|Protected|Binary|Permanent/i.test(flags)||['undef','struct'].includes(tag.getAttribute('type')))continue
        const description=tag.querySelector('desc[lang="en"]')?.textContent||name
        result[key]={key,group,name,...fieldInfo(name,group,true),englishDescription:description,type:tag.getAttribute('type'),list:/(^|,)(List|Bag|Seq)(,|$)/.test(flags)}
      }
    }
    return result
  })().catch(error=>{catalogPromise=null;throw error})
  return catalogPromise
}
async function inspect(record,metadata,preview=true){
  metadata=metadata||await raw(record.file,record.extension)
  const tags=await catalog(record.file,record.extension)
  const fields=Object.entries(metadata).filter(([key])=>key.includes(':')&&!key.startsWith('ExifTool:')&&!key.startsWith('System:')).map(([key,value])=>{const [group,name]=key.split(':');const writable=!!tags[key]&&!(value&&typeof value==='object'&&!Array.isArray(value))&&!String(value).startsWith('(Binary data');return {key,group,name,...fieldInfo(name,group,writable),value,writable,list:tags[key]?.list||false}})
  const dimension=name=>metadata[`File:${name}`]??metadata[`IFD0:${name}`]??metadata[`PNG:${name}`]??metadata[`QuickTime:${name}`]??Object.entries(metadata).find(([key])=>key.endsWith(':'+name))?.[1]
  return {...summary(record,preview),fields,latitude:metadata['Composite:GPSLatitude']??metadata['GPS:GPSLatitude']??null,longitude:metadata['Composite:GPSLongitude']??metadata['GPS:GPSLongitude']??null,width:dimension('ImageWidth'),height:dimension('ImageHeight')}
}
function equivalent(actual,expected,key=''){
  if(Array.isArray(expected))return JSON.stringify(actual)===JSON.stringify(expected)
  if(typeof actual==='number'&&String(expected).trim()!==''&&Number.isFinite(Number(expected))){
    // EXIF GPSAltitude is stored as an unsigned rational. ExifTool may choose
    // a nearby fraction whose sub-millimetre round-trip value is not bit-for-
    // bit identical to the decimal input. Keep strict checks elsewhere.
    const tolerance=key==='GPS:GPSAltitude'?1e-3:1e-7
    return Math.abs(actual-Number(expected))<=tolerance
  }
  return String(actual??'').trim()===String(expected??'').trim()
}
const pngSignature=[137,80,78,71,13,10,26,10]
function crc32(bytes){let crc=0xffffffff;for(const byte of bytes){crc^=byte;for(let bit=0;bit<8;bit++)crc=(crc>>>1)^((crc&1)?0xedb88320:0)}return (crc^0xffffffff)>>>0}
function pngTextChunk(keyword,value){const encoder=new TextEncoder(),key=encoder.encode(keyword),text=encoder.encode(String(value)),payload=new Uint8Array(key.length+5+text.length);payload.set(key);payload.set(text,key.length+5);const type=encoder.encode('iTXt'),body=new Uint8Array(4+payload.length);body.set(type);body.set(payload,4);const chunk=new Uint8Array(12+payload.length),view=new DataView(chunk.buffer);view.setUint32(0,payload.length);chunk.set(body,4);view.setUint32(8+payload.length,crc32(body));return chunk}
async function writePngCustom(record,values){
  const input=new Uint8Array(await record.file.arrayBuffer());if(!pngSignature.every((byte,index)=>input[index]===byte))throw new Error('自定义私有字段目前仅支持 PNG')
  const names=new Set(Object.keys(values).map(key=>key.replace(/^PNG:/,''))),parts=[input.slice(0,8)];let offset=8,inserted=false
  while(offset+12<=input.length){const view=new DataView(input.buffer,input.byteOffset+offset),length=view.getUint32(0),end=offset+12+length;if(end>input.length)throw new Error('PNG 数据块不完整');const type=new TextDecoder('latin1').decode(input.slice(offset+4,offset+8)),payload=input.slice(offset+8,offset+8+length),keyword=new TextDecoder('latin1').decode(payload.slice(0,payload.indexOf(0)))
    // PNG text/eXIf metadata belongs before image-data chunks. Inserting it
    // just before IEND places it after IDAT and makes ExifTool repair the file
    // on the next edit. Insert before the first IDAT; retain IEND as a fallback
    // for unusual PNG files that contain no image-data chunk.
    if((type==='IDAT'||type==='IEND')&&!inserted){for(const [key,value] of Object.entries(values))parts.push(pngTextChunk(key.replace(/^PNG:/,''),value));inserted=true}
    if(!(['tEXt','zTXt','iTXt'].includes(type)&&names.has(keyword)))parts.push(input.slice(offset,end));offset=end
  }
  if(!inserted)throw new Error('PNG 缺少 IEND 数据块');const size=parts.reduce((sum,item)=>sum+item.length,0),output=new Uint8Array(size);let cursor=0;for(const part of parts){output.set(part,cursor);cursor+=part.length}
  const file=new File([output],record.name,{type:record.file.type}),after=await raw(file,record.extension);for(const [key,value] of Object.entries(values)){const actual=after[`PNG:${key.replace(/^PNG:/,'')}`];if(String(actual??'')!==String(value))throw new Error(`自定义字段回读失败：${key}`)}
  const next={...record,file,revision:Date.now()+Math.random()},detail=await inspect(next,after,false);await transaction('readwrite',store=>store.put(next));records.set(next.id,next);detail.preview_url=previewUrl(next);return detail
}
function sourceValue(metadata,key){const value=metadata[key];if(value===undefined)throw new Error(`缺少源字段：${key}`);return value}
function attitudeValues(metadata){let vector=sourceValue(metadata,'Apple:AccelerationVector');if(typeof vector==='string')vector=vector.trim().split(/[\s,]+/);if(!Array.isArray(vector)||vector.length!==3)throw new Error('Apple:AccelerationVector 不是有效三维向量');const [ax,ay,az]=vector.map(Number);if(![ax,ay,az].every(Number.isFinite))throw new Error('加速度向量包含无效数值');const magnitude=Math.hypot(ax,ay,az);if(magnitude<1e-12)throw new Error('零向量无法估计姿态');if(Math.hypot(ay,az)<1e-12*magnitude)throw new Error('重力沿 X 轴，Roll 不可确定');const yaw=Number(sourceValue(metadata,'GPS:GPSImgDirection')),reference=sourceValue(metadata,'GPS:GPSImgDirectionRef');if(!Number.isFinite(yaw)||yaw<0||yaw>360||!['T','M'].includes(reference))throw new Error('缺少有效的 GPS 拍摄方向或北向基准');return {Pitch:(Math.atan2(-ax,Math.hypot(ay,az))*180/Math.PI).toFixed(8),Roll:(Math.atan2(ay,az)*180/Math.PI).toFixed(8),Yaw:(yaw%360).toFixed(8),YawReference:reference,AttitudeUnits:'degrees',AttitudeMethod:'Pitch=atan2(-ax,hypot(ay,az)); Roll=atan2(ay,az); Yaw=GPSImgDirection'}}
function customRuleValue(metadata,expression){const keys=[...expression.matchAll(/\$\{([^}]+)\}/g)].map(match=>match[1]);let formula=expression;for(const key of keys){const value=Number(sourceValue(metadata,key));if(!Number.isFinite(value))throw new Error(`源字段不是数值：${key}`);formula=formula.replaceAll(`\${${key}}`,String(value))}if(!/^[\d\s+\-*/%().,A-Za-z_]+$/.test(formula)||/\b(?!abs\b|sqrt\b|hypot\b|atan2\b|sin\b|cos\b|tan\b|round\b|min\b|max\b|PI\b)[A-Za-z_]\w*/.test(formula))throw new Error('公式包含不支持的内容');const fn=Function('abs','sqrt','hypot','atan2','sin','cos','tan','round','min','max','PI',`"use strict";return (${formula})`),value=fn(Math.abs,Math.sqrt,Math.hypot,Math.atan2,Math.sin,Math.cos,Math.tan,Math.round,Math.min,Math.max,Math.PI);if(!Number.isFinite(value))throw new Error('公式计算结果不是有限数值');return value}
async function applyMetadataRule(record,rule){const metadata=await raw(record.file,record.extension);if(rule.type==='attitude')return writePngCustom(record,attitudeValues(metadata));const key=String(rule.target||'').trim(),value=customRuleValue(metadata,String(rule.expression||''));if(!key)throw new Error('缺少目标字段');const dictionary=await catalog(record.file,record.extension);return dictionary[key]?edit(record,{updates:{[key]:String(value)}}):writePngCustom(record,{[key]:String(value)})}
async function checkMetadataRule(record,rule){const metadata=await cachedRaw(record);if(rule.type==='attitude'){if(record.format!=='PNG')throw new Error('三维姿态自定义字段当前仅支持写入 PNG');attitudeValues(metadata);return true}const key=String(rule.target||'').trim();if(!key)throw new Error('缺少结果字段');customRuleValue(metadata,String(rule.expression||''));const dictionary=await catalog(record.file,record.extension);if(!dictionary[key]&&record.format!=='PNG')throw new Error(`当前格式不支持新增字段：${key}`);return true}
const ruleSignature=rule=>JSON.stringify({type:rule.type||'',target:rule.target||'',expression:rule.expression||''})
async function cachedRuleCheck(record,rule){const key=`${record.id}:${record.revision}:${ruleSignature(rule)}`;if(ruleCheckCache.has(key))return ruleCheckCache.get(key);let result;try{await checkMetadataRule(record,rule);result={ok:true}}catch(error){result={ok:false,error:error.message}}ruleCheckCache.set(key,result);if(ruleCheckCache.size>2000){const oldest=ruleCheckCache.keys().next().value;ruleCheckCache.delete(oldest)}return result}
async function mapLimited(items,limit,worker){const results=new Array(items.length),cursor={value:0};await Promise.all(Array.from({length:Math.min(limit,items.length)},async()=>{while(cursor.value<items.length){const index=cursor.value++;results[index]=await worker(items[index],index)}}));return results}
async function edit(record,payload){
  if(payload.restore){const next={...record,file:record.original,revision:Date.now()+Math.random()};const detail=await inspect(next,undefined,false);await transaction('readwrite',store=>store.put(next));records.set(next.id,next);detail.preview_url=previewUrl(next);return detail}
  const updates=payload.updates||{},customUpdates=payload.custom_updates||{},deletes=payload.deletes||[],dictionary=await catalog(record.file,record.extension),before=await raw(record.file,record.extension)
  if(Object.keys(customUpdates).length&&!Object.keys(updates).length&&!deletes.length&&!payload.gps&&!payload.clear_all&&!payload.clear_gps)return writePngCustom(record,customUpdates)
  const tags={}
  // TIFF's IFD0 contains image data pointers; deleting the group is unsafe.
  // Clear editable fields individually for every format, preserving structural/vendor blocks.
  if(payload.clear_all){for(const key of Object.keys(before)){if(dictionary[key]&&/^(IFD0|ExifIFD|GPS|InteropIFD|IPTC|XMP-[\w-]+):/.test(key))tags[key]=''}}
  else if(payload.clear_gps){tags['GPS:all']='';tags['XMP-exif:GPS*']='';for(const key of Object.keys(before)){if(dictionary[key]&&/:GPS/.test(key))tags[key]=''}}
  for(const key of [...Object.keys(updates),...deletes]){if(!dictionary[key])throw new Error(`字段不可写：${key}`)}
  for(const key of deletes)tags[key]=''
  for(const [key,value] of Object.entries(updates)){
    if(typeof value==='object'&&!Array.isArray(value))throw new Error(`字段值类型无效：${key}`)
    if(JSON.stringify(value).length>16000)throw new Error('字段内容过长')
    tags[key]=value
  }
  if(payload.gps){const {latitude,longitude,altitude}=payload.gps,lat=Number(latitude),lon=Number(longitude)
    if(!Number.isFinite(lat)||Math.abs(lat)>90||!Number.isFinite(lon)||Math.abs(lon)>180)throw new Error('纬度范围 -90～90，经度范围 -180～180')
    Object.assign(tags,{'GPS:GPSLatitude':Math.abs(lat),'GPS:GPSLatitudeRef':lat<0?'S':'N','GPS:GPSLongitude':Math.abs(lon),'GPS:GPSLongitudeRef':lon<0?'W':'E'})
    if(altitude!==''&&altitude!=null){if(!Number.isFinite(Number(altitude)))throw new Error('海拔数值无效');tags['GPS:GPSAltitude']=Math.abs(Number(altitude));tags['GPS:GPSAltitudeRef']=Number(altitude)<0?1:0}
  }
  if(!Object.keys(tags).length){if(payload.clear_all)return inspect(record,before);throw new Error('没有待保存的修改')}
  const output=await execute('write',record.file,record.extension,tags)
  const file=new File([output],record.name,{type:record.file.type}),after=await raw(file,record.extension)
  if(normalizedFormat(after['File:FileType'])!==normalizedFormat(before['File:FileType']))throw new Error(`文件格式校验失败（${before['File:FileType']} → ${after['File:FileType']}），已保留原文件`)
  for(const key of Object.keys(before).filter(key=>/:(ImageWidth|ImageHeight)$/.test(key))){if(after[key]!==before[key])throw new Error('像素尺寸校验失败，已保留原文件')}
  for(const [key,value] of Object.entries(tags)){
    if(key==='all'||key.endsWith('*')||key.endsWith(':all'))continue
    if(value===''){if(after[key]!=null)throw new Error(`删除校验失败：${key}`)}
    else if(!equivalent(after[key],value,key))throw new Error(`写入值校验失败：${key}，已保留原文件`)
  }
  if(payload.clear_gps&&Object.keys(after).some(k=>k.startsWith('GPS:')||k.startsWith('XMP-exif:GPS')||/:GPS(Latitude|Longitude|Altitude|Coordinates|Position)/.test(k)))throw new Error('GPS 清除不完整，存在受保护的位置字段，已保留原文件')
  const next={...record,file,revision:Date.now()+Math.random()},detail=await inspect(next,after,false)
  await transaction('readwrite',store=>store.put(next));records.set(next.id,next);detail.preview_url=previewUrl(next);return Object.keys(customUpdates).length?writePngCustom(next,customUpdates):detail
}
export async function downloadImage(id){const record=await find(id);saveBlob(record.file,record.name)}
export async function getImagePreview(id){const record=await find(id);return /^(HEIC|HEIF)$/.test(record.format)?decodeImagePreview(record):previewUrl(record)}
export async function downloadJson(id){const detail=await inspect(await find(id));saveBlob(new Blob([JSON.stringify(detail.fields,null,2)],{type:'application/json'}),detail.name+'.metadata.json')}
export function saveBlob(blob,name){const url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
const client={
  async get(path,config={}){
    if(path.endsWith('/capabilities'))return {data:{available:typeof WebAssembly!=='undefined',version:'WebAssembly',error:'浏览器不支持 WebAssembly'}}
    if(path.endsWith('/tags')){const record=records.values().next().value;if(!record)return {data:[]};const tags=await catalog(record.file,record.extension),q=(config.params?.q||'').toLowerCase();return {data:Object.values(tags).filter(t=>(t.key+t.label+t.description).toLowerCase().includes(q)).slice(0,150)}}
    if(path.endsWith('/images')){const all=await transaction('readonly',store=>store.getAll());records.clear();for(const r of all)records.set(r.id,r);return {data:all.sort((a,b)=>b.created-a.created).map(record=>summary(record))}}
    return {data:await inspect(await find(path.split('/').pop()))}
  },
  async post(path,payload,config={}){
    if(path.endsWith('/images')){const images=[],errors=[],files=payload.getAll('files');for(let index=0;index<files.length;index++){const original=files[index];try{
      if(!filePattern.test(original.name))throw new Error('不支持的图片格式')
      config.onUploadProgress?.({loaded:index,total:files.length})
      const extension='.'+original.name.split('.').pop().toLowerCase(),metadata=await raw(original,extension),format=normalizedFormat(metadata['File:FileType']);if(!allowed.has(format))throw new Error('文件内容不是支持的图片')
      const record={id:metadataUuid().replaceAll('-',''),name:original.name,original,file:original,extension,format,revision:Date.now()+Math.random(),created:Date.now()}
      await transaction('readwrite',store=>store.put(record));records.set(record.id,record);images.push(summary(record,false))
    }catch(error){errors.push({name:original.name,error:error.message})}}return {data:{images,errors}}}
    if(path.endsWith('/batch')){const images=[],errors=[];for(const id of payload.ids){try{if(payload.action==='delete'){await transaction('readwrite',store=>store.delete(id));records.delete(id);releaseImagePreview(id);const old=urls.get(id);if(old)URL.revokeObjectURL(old.url);urls.delete(id)}else images.push(await edit(await find(id),payload))}catch(error){errors.push({id,error:error.message})}}return {data:{images,errors}}}
    if(path.endsWith('/rules/apply')){const images=[],errors=[];for(const id of payload.ids){try{images.push(await applyMetadataRule(await find(id),payload.rule||{}))}catch(error){errors.push({id,error:error.message})}}return {data:{images,errors}}}
    if(path.endsWith('/rules/check')){const ids=[...new Set(payload.ids)],rule=payload.rule||{},results=await mapLimited(ids,4,async id=>({id,...await cachedRuleCheck(await find(id),rule)})),eligible=results.filter(item=>item.ok).map(item=>item.id),errors=results.filter(item=>!item.ok).map(({id,error})=>({id,error}));return {data:{eligible,errors,total:ids.length,cached:true}}}
    if(path.endsWith('/export')){const entries={};for(let i=0;i<payload.ids.length;i++){const record=await find(payload.ids[i]),prefix=String(i+1).padStart(3,'0');entries[`${prefix}_${record.name}`]=new Uint8Array(await record.file.arrayBuffer());entries[`metadata/${prefix}.json`]=strToU8(JSON.stringify((await inspect(record)).fields,null,2))}const output=await new Promise((resolve,reject)=>zip(entries,{level:0},(error,data)=>error?reject(error):resolve(data)));return {data:new Blob([output],{type:'application/zip'})}}
    if(path.endsWith('/metadata-export')){const entries={};for(let i=0;i<payload.ids.length;i++){const record=await find(payload.ids[i]),prefix=String(i+1).padStart(3,'0'),safe=record.name.replace(/[^\p{L}\p{N}._-]+/gu,'_');entries[`${prefix}_${safe}.metadata.json`]=strToU8(JSON.stringify((await inspect(record)).fields,null,2))}const output=await new Promise((resolve,reject)=>zip(entries,{level:6},(error,data)=>error?reject(error):resolve(data)));return {data:new Blob([output],{type:'application/zip'})}}
    throw new Error('未知操作')
  },
}
export default client
