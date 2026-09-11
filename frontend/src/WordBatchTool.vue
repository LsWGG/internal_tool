<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,reactive,ref} from 'vue'
import axios from 'axios'
import {renderAsync} from 'docx-preview'

const props=defineProps({confirmAction:Function})
const emit=defineEmits(['notify'])
const notify=(text,type='info')=>emit('notify',text,type)
const errorOf=e=>typeof e.response?.data?.detail==='string'?e.response.data.detail:e.message||'操作失败'
const template=ref(null),excel=ref(null),workspace=ref(null),sheetName=ref(''),mappings=reactive({}),filename=ref('')
const busy=ref(false),busyText=ref(''),tasks=ref([]),expanded=ref(''),resultPage=ref(1),results=ref({items:[],total:0})
const previewElement=ref(),previewTitle=ref(''),previewUrl=ref(''),previewBusy=ref(false),previewError=ref(''),inputKey=ref(0)
let timer,previewSerial=0,disposed=false,polling=false,resultSerial=0,previewObserver
function fitPreview(){
  const host=previewElement.value,wrapper=host?.querySelector('.wb-docx-wrapper'),page=host?.querySelector('section.wb-docx')
  if(!wrapper||!page||host.clientWidth<1)return
  wrapper.style.zoom=String(Math.min(1,Math.max(.2,(host.clientWidth-2)/(page.offsetWidth+48))))
}
const sheet=computed(()=>workspace.value?.sheets.find(item=>item.name===sheetName.value))
const ready=computed(()=>workspace.value&&workspace.value.fields.every(field=>mappings[field.key]?.column)&&!sheet.value?.warnings.length)
const statuses={queued:'等待生成',running:'正在生成',completed:'全部成功',partial:'部分失败',failed:'生成失败'}
const placeholder=key=>'{{'+key+'}}'
const previewRows=computed(()=>sheet.value?.rows||[])
function chooseSheet(){
  for(const key of Object.keys(mappings))delete mappings[key]
  for(const field of workspace.value?.fields||[]){
    const column=sheet.value?.columns.includes(field.key)?field.key:''
    mappings[field.key]={column,type:field.image||sheet.value?.image_columns.includes(column)?'image':'text',width_mm:40,height_mm:40}
  }
  clearPreview()
}
const config=()=>({sheet:sheetName.value,mappings:JSON.parse(JSON.stringify(mappings)),filename:filename.value})
function clearPreview(){++previewSerial;previewTitle.value='';previewUrl.value='';previewError.value='';previewBusy.value=false;previewElement.value?.replaceChildren()}
async function clearWorkspace(){
  if(workspace.value)await axios.delete(`/api/word-batch/workspaces/${workspace.value.id}`)
  workspace.value=null;sheetName.value='';Object.keys(mappings).forEach(key=>delete mappings[key]);clearPreview()
}
async function reset(){try{await clearWorkspace();template.value=null;excel.value=null;filename.value='';inputKey.value++}catch(e){notify(errorOf(e),'error')}}
async function prepare(){
  if(!template.value||!excel.value)return notify('请选择 Word 模板和 Excel 文件。','error')
  busy.value=true;busyText.value='解析模板与数据…'
  try{
    await clearWorkspace()
    const data=new FormData();data.append('template',template.value);data.append('excel',excel.value)
    workspace.value=(await axios.post('/api/word-batch/workspaces',data)).data
    sheetName.value=workspace.value.sheets[0].name;chooseSheet()
    notify('已识别模板字段，请确认映射并预览首行。','success')
  }catch(e){notify(errorOf(e),'error')}finally{busy.value=false;busyText.value=''}
}
async function showPreview(url,title){
  const serial=++previewSerial;previewBusy.value=true;previewError.value='';previewTitle.value=title;previewUrl.value=url
  await nextTick();previewElement.value?.replaceChildren()
  try{
    const {data}=await axios.get(url,{responseType:'arraybuffer'})
    if(serial!==previewSerial||disposed)return
    // Render into an isolated container; never execute document links or external resources.
    const staging=document.createElement('div')
    await renderAsync(data,staging,null,{className:'wb-docx',inWrapper:true,ignoreWidth:false,ignoreHeight:false,breakPages:true,renderHeaders:true,renderFooters:true,renderFootnotes:true,useBase64URL:true})
    for(const link of staging.querySelectorAll('a')){link.removeAttribute('href');link.style.pointerEvents='none'}
    if(serial!==previewSerial||disposed)return
    previewElement.value.replaceChildren(...staging.childNodes)
    fitPreview()
    previewElement.value.scrollTop=0
    previewElement.value.closest('.wb-preview-panel')?.scrollIntoView({block:'nearest',behavior:'smooth'})
  }catch(e){if(serial===previewSerial)previewError.value='预览失败：'+errorOf(e)+'。可下载后使用 Word 查看。'}finally{if(serial===previewSerial)previewBusy.value=false}
}
async function previewFirst(){
  if(!ready.value)return
  busy.value=true;busyText.value='生成首行预览…'
  try{const {data}=await axios.post(`/api/word-batch/workspaces/${workspace.value.id}/preview`,config());await showPreview(data.url,'首行试生成 · Excel 第 '+previewRows.value[0].row+' 行')}
  catch(e){notify(errorOf(e),'error')}finally{busy.value=false;busyText.value=''}
}
async function generate(){
  if(!ready.value)return
  busy.value=true;busyText.value='创建生成任务…'
  try{
    await axios.post(`/api/word-batch/workspaces/${workspace.value.id}/tasks`,config())
    notify('任务已创建，可在下方查看生成进度。','success')
    await reset();await loadTasks()
  }catch(e){notify(errorOf(e),'error')}finally{busy.value=false;busyText.value=''}
}
async function loadResults(id=expanded.value,page=resultPage.value){
  if(!id)return
  const serial=++resultSerial
  const {data}=await axios.get(`/api/word-batch/tasks/${id}/results`,{params:{page}})
  if(serial===resultSerial&&expanded.value===id){results.value=data;resultPage.value=page}
}
async function loadTasks(){
  if(polling||disposed)return
  polling=true
  try{tasks.value=(await axios.get('/api/word-batch/tasks')).data;if(expanded.value)await loadResults()}
  catch(e){if(!tasks.value.length)notify(errorOf(e),'error')}finally{polling=false}
}
async function toggleResults(task){
  if(expanded.value===task.id){expanded.value='';++resultSerial;return}
  expanded.value=task.id;resultPage.value=1;results.value={items:[],total:0};try{await loadResults()}catch(e){notify(errorOf(e),'error')}
}
async function control(task,action){
  if(action==='delete'&&!await (props.confirmAction?props.confirmAction(`删除“${task.name}”及模板、Excel 数据、Word 结果和 ZIP？`):window.confirm('删除任务及全部文件？')))return
  try{if(action==='delete'){await axios.delete(`/api/word-batch/tasks/${task.id}`);if(expanded.value===task.id)expanded.value='';if(previewUrl.value.includes(`/tasks/${task.id}/`))clearPreview()}else await axios.post(`/api/word-batch/tasks/${task.id}/retry`);await loadTasks()}
  catch(e){notify(errorOf(e),'error')}
}
onMounted(()=>{loadTasks();timer=setInterval(loadTasks,2500);previewObserver=new ResizeObserver(fitPreview);if(previewElement.value)previewObserver.observe(previewElement.value)})
onBeforeUnmount(()=>{disposed=true;clearInterval(timer);previewObserver?.disconnect();++previewSerial;if(workspace.value)axios.delete(`/api/word-batch/workspaces/${workspace.value.id}`).catch(()=>{})})
</script>

<template>
  <section class="wb-page">
    <header class="wb-header"><div><span class="eyebrow">WORD TEMPLATE STUDIO</span><h1>Word 批量生成</h1><p>一份模板，一张数据表，为每一行生成独立文档。</p></div><span class="wb-badge">模板样式保留 · 动态图片</span></header>
    <div class="wb-workspace">
      <div class="wb-config">
        <div class="wb-step-head"><b><i>1</i> 上传模板与数据</b><button v-if="template||workspace" :disabled="busy" @click="reset">重新开始</button></div>
        <div class="wb-examples"><div><b>先用示例试一试</b><small>配套使用可生成 3 份员工信息卡。Excel 已内嵌图片，无需另传素材。</small></div><div><a href="/api/word-batch/examples/word" download>下载 Word 示例</a><a href="/api/word-batch/examples/excel" download>下载 Excel 示例</a></div></div>
        <div :key="inputKey" class="wb-upload-grid">
          <label class="wb-file-field"><span class="wb-file-label">Word 模板</span><small>.docx · 最大 20MB</small><span class="wb-file-control"><b>选择文件</b><em :title="template?.name">{{template?.name||'未选择文件'}}</em></span><input class="wb-file-native" type="file" accept=".docx" :disabled="busy||!!workspace" @change="template=$event.target.files[0]||null"></label>
          <label class="wb-file-field"><span class="wb-file-label">Excel 数据</span><small>.xlsx · 最大 30MB · 第一行为表头</small><span class="wb-file-control"><b>选择文件</b><em :title="excel?.name">{{excel?.name||'未选择文件'}}</em></span><input class="wb-file-native" type="file" accept=".xlsx" :disabled="busy||!!workspace" @change="excel=$event.target.files[0]||null"></label>
          <p class="wb-wide wb-muted">图片直接读取 Excel 中锚定在对应单元格的内嵌图片，无需单独上传。</p>
        </div>
        <details class="wb-guide"><summary>如何制作模板与填写图片字段？</summary><p>在 Word 正文、表格、页眉或页脚中输入 <code v-text="'{{姓名}}'"></code>，对应 Excel 的“姓名”列；每行生成一份文档。</p><p>图片位置填写 <code v-text="'{{%照片}}'"></code>，在 Excel“照片”列的对应行插入图片，并将图片左上角放在该单元格内。系统自动读取内嵌图片，无需另传素材。普通占位符也能在下一步切换为图片。</p><p>图片等比例缩放到指定范围，不裁剪。建议将图片占位符单独放在段落或单元格中，并在 Word 中设置居中。</p><p>公式需先在 Excel 中计算并保存；前导零编号请设为文本或数字格式。仅支持普通占位符，不执行模板脚本、循环和条件表达式。</p></details>
        <button v-if="!workspace" class="wb-primary" :disabled="busy||!template||!excel" @click="prepare">{{busy?busyText:'解析模板与 Excel'}}</button>
        <template v-if="workspace">
          <div class="wb-step-head"><b><i>2</i> 确认数据与字段</b><small>{{workspace.fields.length}} 个字段</small></div>
          <label>工作表<select v-model="sheetName" :disabled="busy" @change="chooseSheet"><option v-for="s in workspace.sheets" :key="s.name" :value="s.name">{{s.name}} · {{s.count}} 行</option></select></label>
          <div v-if="sheet?.warnings.length" class="wb-warning"><p v-for="message in sheet.warnings" :key="message">{{message}}</p></div>
          <div class="wb-mapping-list"><div v-for="field in workspace.fields" :key="field.key" class="wb-mapping">
            <div class="wb-map-heading"><b>{{field.key}}</b><code>{{placeholder((field.image?'%':'')+field.key)}}</code></div>
            <div class="wb-two"><label>Excel 列<select v-model="mappings[field.key].column" :disabled="busy"><option value="">请选择列</option><option v-for="col in sheet.columns" :key="col">{{col}}</option></select></label><label>写入类型<select v-model="mappings[field.key].type" :disabled="busy||field.image"><option value="text">文字</option><option value="image">图片</option></select></label></div>
            <div v-if="mappings[field.key].type==='image'" class="wb-two wb-image-size"><label>最大宽度（mm）<input v-model.number="mappings[field.key].width_mm" type="number" min="1" max="170" :disabled="busy"></label><label>最大高度（mm）<input v-model.number="mappings[field.key].height_mm" type="number" min="1" max="240" :disabled="busy"></label></div>
          </div></div>
          <details class="wb-data" open><summary>Excel 数据预览 · 前 {{previewRows.length}} 行</summary><div class="wb-table-scroll"><table><thead><tr><th>行号</th><th v-for="col in sheet.columns" :key="col">{{col}}</th></tr></thead><tbody><tr v-for="row in previewRows" :key="row.row"><td>{{row.row}}</td><td v-for="col in sheet.columns" :key="col" :title="String(row.values[col])">{{String(row.values[col]).startsWith('__excel_')?'[Excel 内嵌图片]':row.values[col]}}</td></tr></tbody></table></div></details>
          <div class="wb-step-head"><b><i>3</i> 预览与批量生成</b><small>{{sheet.count}} 份 Word</small></div>
          <label>结果文件命名（可选）<input v-model="filename" :disabled="busy" :placeholder="'例如：{{姓名}}_通知书_{{序号}}'"><small>默认使用第一列；重名自动编号，空名称使用模板名与序号。</small></label>
          <div class="wb-submit"><button :disabled="busy||!ready" @click="previewFirst">试生成首行</button><button class="wb-primary" :disabled="busy||!ready" @click="generate">{{busy?busyText:`批量生成 ${sheet.count} 份`}}</button></div>
        </template>
      </div>
      <section class="wb-preview-panel"><div class="wb-preview-head"><div><b>Word 结果预览</b><small>{{previewTitle||'先试生成首行，确认格式后再批量生成。'}}</small></div><a v-if="previewUrl" :href="previewUrl" download>下载当前 Word</a></div><div v-if="previewBusy" class="wb-preview-message">正在加载文档…</div><div v-else-if="previewError" class="wb-preview-message wb-warning">{{previewError}}</div><div v-else-if="!previewTitle" class="wb-preview-empty"><span>W</span><h3>让模板决定文档的样式</h3><p>文字、表格与动态图片将在这里展示。</p><small>浏览器预览与 Microsoft Word 的分页可能略有差异。</small></div><div ref="previewElement" class="wb-preview-content" :class="{hidden:!previewTitle||previewBusy||previewError}"></div><p class="wb-preview-note">预览用于检查内容与图片。最终字体、分页以 Word 打开结果为准。</p></section>
    </div>
    <section class="wb-tasks"><div class="wb-step-head"><b>生成任务</b><small>{{tasks.length}} 个任务</small></div><p v-if="!tasks.length" class="wb-muted">暂时没有任务，生成后可在此查看进度、预览和下载。</p><article v-for="task in tasks" :key="task.id" class="wb-task"><div class="wb-task-top"><div><b>{{task.name}}</b><small>{{task.created_at}} · {{task.message}}</small></div><span class="wb-status" :class="task.status">{{statuses[task.status]}}</span></div><progress :value="task.processed" :max="task.total"></progress><div class="wb-task-actions"><small>{{task.processed}} / {{task.total}} 行 · 成功 {{task.success}} · 失败 {{task.failed}}</small><div><button @click="toggleResults(task)">{{expanded===task.id?'收起结果':'查看结果'}}</button><a v-if="task.success&&!['running','queued'].includes(task.status)" :href="`/api/word-batch/tasks/${task.id}/export`">下载 ZIP</a><button v-if="['partial','failed'].includes(task.status)" @click="control(task,'retry')">重新生成</button><button class="wb-danger" :disabled="['running','queued'].includes(task.status)" @click="control(task,'delete')">删除</button></div></div><div v-if="expanded===task.id" class="wb-results"><p v-if="!results.items.length" class="wb-muted">暂无结果，生成后会自动显示。</p><div v-for="item in results.items" :key="item.index" class="wb-result"><div><b>{{item.name||`Excel 第 ${item.row} 行`}}</b><small>{{item.status==='failed'?item.error:`Excel 第 ${item.row} 行`}}</small></div><div v-if="item.status==='completed'"><button @click="showPreview(`/api/word-batch/tasks/${task.id}/files/${item.index}`,item.name)">预览</button><a :href="`/api/word-batch/tasks/${task.id}/files/${item.index}`">下载</a></div><span v-else class="wb-danger">失败</span></div><div v-if="results.total>20" class="wb-pagination"><button :disabled="resultPage<=1" @click="loadResults(task.id,resultPage-1)">上一页</button><span>{{resultPage}} / {{Math.ceil(results.total/20)}}</span><button :disabled="resultPage*20>=results.total" @click="loadResults(task.id,resultPage+1)">下一页</button></div></div></article></section>
  </section>
</template>

<style scoped>
.wb-page{color:#30465f;font-size:13px;--wb-line:#dbe4f0;--wb-muted:#74869b;--wb-blue:#4169d8}.wb-header{display:flex;align-items:center;justify-content:space-between;gap:20px;padding-bottom:24px;margin-bottom:24px;border-bottom:1px solid var(--wb-line)}.wb-header h1{font-family:inherit;font-size:30px;font-weight:700;line-height:1.4;letter-spacing:-.5px;margin:6px 0}.wb-header p{color:var(--wb-muted);margin:0;line-height:1.8}.wb-badge{border:1px solid #cbdaf2;border-radius:20px;padding:9px 14px;background:#f3f7ff;color:#5576b7;font-size:11px;white-space:nowrap}.wb-workspace{display:grid;grid-template-columns:minmax(350px,440px) minmax(0,1fr);gap:24px;align-items:stretch;height:clamp(480px,calc(100dvh - 285px),900px);grid-template-rows:minmax(0,1fr)}.wb-config{min-width:0;min-height:0;overflow:auto;padding-right:10px;scrollbar-gutter:stable;overscroll-behavior:contain}.wb-step-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin:0 0 18px}.wb-step-head b{display:flex;align-items:center;gap:9px;font-size:15px}.wb-step-head i{display:grid;place-items:center;width:24px;height:24px;background:#eaf0ff;color:#4169d8;border-radius:8px;font-size:12px;font-style:normal}.wb-step-head small,.wb-muted{color:var(--wb-muted);line-height:1.8}.wb-page button,.wb-page a{font:inherit;border:1px solid var(--wb-line);border-radius:8px;padding:8px 12px;color:#456184;background:#fff;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.5}.wb-page button:hover:not(:disabled),.wb-page a:hover{border-color:#9bb8e6;background:#f2f6ff}.wb-page button:disabled{opacity:.45;cursor:not-allowed}.wb-page .wb-primary{background:var(--wb-blue);color:white;border-color:var(--wb-blue)}.wb-page .wb-danger{color:#b15168}.wb-page label{display:flex;flex-direction:column;gap:8px;min-width:0;margin-bottom:18px;font-size:12px;font-weight:600}.wb-page label span,.wb-page label small{font-size:11px;line-height:1.7;font-weight:400;color:var(--wb-muted)}.wb-page input,.wb-page select{box-sizing:border-box;width:100%;min-width:0;padding:9px 10px;border:1px solid var(--wb-line);border-radius:8px;background:#fff;color:#354d6b;font-family:inherit;font-size:12px;font-weight:400;line-height:1.6}.wb-page input[type=file]{font-size:11px;cursor:pointer}.wb-upload-grid,.wb-two{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.wb-wide{grid-column:1/-1}.wb-guide{padding:14px 16px;border:1px solid var(--wb-line);border-radius:10px;background:#f5f8fd;margin:0 0 20px}.wb-guide summary,.wb-data summary{cursor:pointer;font-size:12px;font-weight:600}.wb-guide p{font-size:12px;line-height:1.9;color:#647990;margin:12px 0 0}.wb-page code{font-size:11px;color:#5a76a3;overflow-wrap:anywhere;white-space:normal}.wb-config>.wb-step-head:not(:first-child){margin-top:28px;padding-top:24px;border-top:1px solid var(--wb-line)}.wb-mapping-list{display:grid;gap:12px;margin:18px 0}.wb-mapping{padding:14px 16px;border:1px solid var(--wb-line);border-radius:10px;background:#ffffffb8}.wb-map-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;margin-bottom:12px}.wb-map-heading b{overflow-wrap:anywhere}.wb-mapping label{margin-bottom:0}.wb-image-size{margin-top:12px}.wb-table-scroll{overflow:auto;max-height:240px;margin-top:12px;border:1px solid var(--wb-line);border-radius:8px}.wb-page table{border-collapse:collapse;width:100%;font-size:11px;text-align:left;white-space:nowrap}.wb-page th,.wb-page td{padding:9px 12px;border-bottom:1px solid var(--wb-line);max-width:200px;overflow:hidden;text-overflow:ellipsis}.wb-page th{background:#f0f5fc;font-weight:600}.wb-submit{display:flex;gap:12px}.wb-submit>.wb-primary{flex:1}.wb-warning{background:#fff8ed;color:#956330;padding:12px;border-radius:8px;font-size:12px;line-height:1.7}.wb-preview-panel{position:relative;min-width:0;height:100%;min-height:0;display:flex;flex-direction:column;border:1px solid var(--wb-line);border-radius:14px;background:#fff;overflow:hidden}.wb-preview-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:18px 20px;border-bottom:1px solid var(--wb-line);flex-shrink:0}.wb-preview-head b{font-size:14px}.wb-preview-head small{display:block;color:var(--wb-muted);font-size:11px;margin-top:7px;line-height:1.7;overflow-wrap:anywhere}.wb-preview-head a{font-size:11px;padding:6px 9px}.wb-preview-empty{display:flex;flex:1;flex-direction:column;justify-content:center;align-items:center;text-align:center;padding:24px;color:var(--wb-muted);gap:10px}.wb-preview-empty>span{display:grid;place-items:center;font-size:32px;font-weight:700;color:#6483c6;width:76px;height:76px;border-radius:18px;background:#edf3ff}.wb-preview-empty h3{color:#405a7b;margin:12px 0 0;font-size:17px}.wb-preview-empty p,.wb-preview-empty small{margin:0;line-height:1.8}.wb-preview-content{flex:1;min-height:0;overflow:auto;background:#eef2f8}.wb-preview-content.hidden{display:none}.wb-preview-content :deep(.wb-docx-wrapper){padding:24px;min-width:min-content;background:#eef2f8;display:flex;align-items:flex-start;flex-direction:column}.wb-preview-content :deep(section.wb-docx){flex-shrink:0;box-shadow:0 3px 15px #314a6a16}.wb-preview-note{flex-shrink:0;padding:12px 18px;margin:0;color:var(--wb-muted);font-size:11px;line-height:1.7;border-top:1px solid var(--wb-line)}.wb-preview-message{padding:24px;color:var(--wb-muted);line-height:1.8}.wb-tasks{margin-top:32px;padding-top:24px;border-top:1px solid var(--wb-line)}.wb-task{padding:18px 20px;background:#fff;border:1px solid var(--wb-line);border-radius:12px;margin-bottom:14px}.wb-task-top,.wb-task-actions,.wb-result{display:flex;align-items:center;justify-content:space-between;gap:16px}.wb-task-top b{font-size:14px;overflow-wrap:anywhere}.wb-task-top small,.wb-result small{display:block;font-size:11px;color:var(--wb-muted);line-height:1.7;margin-top:6px;overflow-wrap:anywhere}.wb-status{padding:5px 10px;border-radius:20px;background:#edf3ff;color:#5478b6;font-size:11px;white-space:nowrap}.wb-status.completed{background:#eaf7f2;color:#368b71}.wb-status.partial,.wb-status.failed{background:#fff1ef;color:#b56658}.wb-task progress{width:100%;height:5px;accent-color:#4169d8;margin:14px 0}.wb-task-actions{flex-wrap:wrap}.wb-task-actions small{font-size:11px;color:var(--wb-muted)}.wb-task-actions>div,.wb-result>div:last-child{display:flex;gap:8px;flex-wrap:wrap}.wb-task-actions button,.wb-task-actions a,.wb-result button,.wb-result a{font-size:11px;padding:6px 10px}.wb-results{margin-top:16px;border-top:1px solid var(--wb-line)}.wb-result{padding:12px 0;border-bottom:1px solid #e9eef5}.wb-result>div:first-child{min-width:0;flex:1}.wb-result b{font-size:12px;overflow-wrap:anywhere}.wb-pagination{display:flex;justify-content:center;align-items:center;gap:14px;margin-top:16px}
@media(max-width:1050px){.wb-workspace{grid-template-columns:minmax(320px,380px) minmax(0,1fr);gap:18px}.wb-preview-head{flex-wrap:wrap}}
@media(max-width:800px){.wb-workspace{grid-template-columns:minmax(0,1fr);height:auto;grid-template-rows:auto auto}.wb-config{overflow:visible;padding-right:0}.wb-preview-panel{position:static;height:650px;min-height:400px}.wb-badge{display:none}}
@media(max-width:480px){.wb-upload-grid{grid-template-columns:1fr}.wb-task{padding:14px}.wb-task-top{align-items:flex-start}.wb-task-actions{gap:12px}.wb-header h1{font-size:25px}.wb-submit{flex-wrap:wrap}.wb-result{gap:8px}.wb-result>div:last-child{flex-direction:column}.wb-preview-head{padding:14px}}
</style>
<style scoped>
.wb-examples{padding:14px 16px;margin-bottom:20px;border:1px solid #d9e4f4;border-radius:10px;background:#f2f6fd}.wb-examples b{font-size:12px;font-weight:600}.wb-examples small{display:block;margin-top:6px;font-size:11px;line-height:1.75;color:#74869b}.wb-examples>div:last-child{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}.wb-examples a{font-size:11px;padding:7px 10px}
</style>
