<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import axios from 'axios'
import {composeMetadata} from './composeMetadata'
import ComposeImage from './ComposeImage.vue'
import ContainerInspect from './ContainerInspect.vue'
import ComposeProjectMenu from './ComposeProjectMenu.vue'
import ComposeYamlEditor from './ComposeYamlEditor.vue'

const emit = defineEmits(['notify'])
const props = defineProps({confirmAction:{type:Function, required:true}})
const status = ref({available:false,message:'正在检测 Docker…'})
const imageRevision=ref(0)
const sudo = ref({enabled:false,password:''})
const projects = ref([])
const selectedId = ref('')
const editor = ref({name:'',content:''})
const creating = ref(true)
const editorOpen = ref(false)
const editorPanel = ref()
const nameInput = ref()
const dropping = ref(false)
const busy = ref(false)
const logs = ref('')
const liveLogs=ref(true), followLogs=ref(true), logLoading=ref(false), logSearch=ref(''), logError=ref(''), logUpdated=ref(''), logTail=ref(300), logView=ref()
const visibleLogs=computed(()=>logSearch.value?logs.value.split('\n').filter(line=>line.toLowerCase().includes(logSearch.value.toLowerCase())).join('\n'):logs.value)
let logTimer, disposed=false, logController, logGeneration=0
function cancelLogs(){logGeneration++;logController?.abort();logLoading.value=false}
function logScroll(){if(logView.value&&logView.value.scrollHeight-logView.value.scrollTop-logView.value.clientHeight>40)followLogs.value=false}
async function latestLogs(){followLogs.value=true;await nextTick();if(logView.value)logView.value.scrollTop=logView.value.scrollHeight}
function downloadText(content,name){const url=URL.createObjectURL(new Blob([content],{type:'text/plain;charset=utf-8'}));const a=document.createElement('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}
function duplicateProject(project=selected.value){if(!project||busy.value)return;creating.value=true;editingId.value='';editor.value={name:`${project.name} 副本`,content:project.content};editorProblems.value=[];editorBaseline.value=JSON.stringify(editor.value);editorOpen.value=true}
async function pollLogs(){
  if(disposed)return
  if(liveLogs.value&&detailTab.value==='logs'&&selectedId.value&&status.value.available&&!document.hidden&&!busy.value)await loadLogs()
  if(!disposed)logTimer=setTimeout(pollLogs,2000)
}
const fileInput = ref()
const workspace = ref()
const footnote = ref()
const workspaceHeight = ref(640)
/* 脚注与工作台之间的那段留白，量高度时要一起减掉（见 .compose-footnote 的 margin-top）。 */
const NOTE_GAP = 14
let layoutObserver
/* 工作区高度 = 视口高度 - 它自己的上沿 - 页脚那句话 - 宿主给底部留的空白。
   那 80px 是 main.ui-shell 的 padding-bottom：早先只减 20px，于是整页永远多出
   一条 60px 的滚动条（两侧栏内的滚动本来就不该有第二层）。页脚同理 —— 它也在
   视口里，不算进来的话也会多出一条滚动条。 */
function fitWorkspace(){
  const element=workspace.value
  if(!element)return
  const host=element.closest('main')
  const styles=host?getComputedStyle(host):null
  const reserved=(styles?parseFloat(styles.paddingBottom)||0:0)+(styles?parseFloat(styles.marginBottom)||0:0)
  const note=footnote.value?footnote.value.getBoundingClientRect().height+NOTE_GAP:0
  workspaceHeight.value=Math.max(420,window.innerHeight-element.getBoundingClientRect().top-note-reserved-8)
}

/* 新建弹窗只给一份能直接改的最小骨架。原来的「快速模板」下拉已删掉：它占一行，
   而且选错就把已经写好的 yaml 整段覆盖。 */
const NEW_NAME = '新建服务'
const NEW_CONTENT = "services:\n  app:\n    image: nginx:alpine\n    ports:\n      - '8080:80'\n"
/* 编辑框清空时显示的骨架。写成脚本里的常量而不是模板里的属性：`&#10;` 这种实体在属性里
   能不能解出换行要看编译器，字符串常量没有这个问题。 */
const PLACEHOLDER = 'services:\n  app:\n    image: nginx:alpine'
const selected = computed(()=>projects.value.find(project=>project.id===selectedId.value))
const search=ref(''), stateFilter=ref(''), actionId=ref(''), actionName=ref(''), editingId=ref(''), editorBaseline=ref('')
const stateOf=p=>!status.value.available?'unknown':runningCount(p)?'running':p.services?.length?'stopped':'undeployed'
const stateLabel=p=>({unknown:'状态未知',running:'运行中',stopped:'已停止',undeployed:'未部署'}[stateOf(p)])
const progressLabel=computed(()=>({start:'启动中…',stop:'停止中…',restart:'重启中…',down:'移除中…'}[actionName.value]||'处理中…'))
function menuChoice(choice,p){if(choice==='edit')openEdit(p);if(choice==='copy')duplicateProject(p);if(choice==='export')downloadText(p.content,p.name+'.yaml');if(choice==='down')action('down',p.id);if(choice==='delete')remove(p.id)}
const detailTab=ref('services')
const accessOpen=ref(false)
const filteredProjects=computed(()=>projects.value.filter(p=>p.name.toLowerCase().includes(search.value.toLowerCase())&&(!stateFilter.value||stateOf(p)===stateFilter.value)))
const pageNumber=ref(1), pageSize=ref(10)
const pageCount=computed(()=>Math.max(1,Math.ceil(filteredProjects.value.length/pageSize.value)))
const pageProjects=computed(()=>filteredProjects.value.slice((pageNumber.value-1)*pageSize.value,pageNumber.value*pageSize.value))
watch([search,stateFilter,pageSize],()=>{pageNumber.value=1})
watch(pageCount,count=>{pageNumber.value=Math.min(pageNumber.value,count)})
const runningCount=p=>(p.services||[]).filter(s=>serviceClass(s.state)==='running').length

function notify(message,type='info'){emit('notify',message,type)}
function errorMessage(error){return error.response?.data?.detail||error.message||'操作失败'}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—'}
const yamlMetadata=computed(()=>Object.fromEntries(projects.value.map(project=>[project.id,composeMetadata(project.content)])))
function projectValues(project,key){const actual=[...new Set((project.services||[]).flatMap(service=>{const value=service[key];return Array.isArray(value)?value:value?[value]:[]}))];return actual.length?actual:yamlMetadata.value[project.id]?.[key]||[]}
function serviceClass(value){return /running|up/i.test(value)?'running':/exit|dead|stopped/i.test(value)?'stopped':'unknown'}

async function refresh(){
  try{
    const [nextStatus,nextProjects]=await Promise.all([axios.get('/api/compose/status'),axios.get('/api/compose/projects')])
    status.value=nextStatus.data;projects.value=nextProjects.data
    imageRevision.value++
    sudo.value.enabled=Boolean(nextStatus.data.use_sudo)
    if(selectedId.value&&!projects.value.some(project=>project.id===selectedId.value))clearSelection()
  }catch(error){notify(errorMessage(error),'error')}
}
async function applyAccess(){
  if(busy.value)return
  if(sudo.value.enabled&&!sudo.value.password&&!status.value.password_set)return notify('请输入 sudo 密码，或关闭 sudo 模式。','info')
  busy.value=true
  try{
    await axios.post('/api/compose/access',{use_sudo:sudo.value.enabled,sudo_password:sudo.value.password})
    sudo.value.password='';await refresh();notify(sudo.value.enabled?'sudo 权限已在本次会话启用。':'已关闭 sudo 权限模式。','success')
  }catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}

/* 编辑器报上来的问题（跟屏幕上那些波浪线是同一批）。只用来决定「保存」能不能点。 */
const editorProblems=ref([])
/* 语法检测交给后端同一份 PyYAML：前端自己再实现一遍语法，迟早会出现
   「编辑器不划线、保存却失败」。检查本身失败（后端还没重启、网络断了）时返回空 ——
   检测是帮忙的，**不该因为它自己坏了就不让人保存**。 */
async function checkSource(content){
  try{return (await axios.post('/api/compose/validate',{content})).data}
  catch{return {problems:[]}}
}
/* 只有 error 级挡保存。warning（没写 image、顶层字段拼错…）是「compose 多半跑不起来」，
   不是这个文件的语法错，拿它拦人就是替用户做决定了。 */
const blocked=computed(()=>editorProblems.value.some(item=>item.severity==='error'))
const validation=ref(null), validating=ref(false)
async function validateConfig(){
  if(!selected.value||validating.value)return
  const id=selected.value.id
  validating.value=true
  try{const result=await axios.post('/api/compose/validate',{content:selected.value.content});if(selectedId.value===id)validation.value=result.data.problems||[]}
  catch(error){notify('校验未完成：'+errorMessage(error),'error')}
  finally{validating.value=false}
}
watch(selectedId,()=>{validation.value=null})

/* 新建/编辑走弹窗：编辑区不再常驻占位，页面留给「服务收藏 + 运行与日志」。
   弹窗打开时才把 editor 填上，关闭即丢弃 —— 没有草稿状态要维护。 */
function openCreate(){
  if(busy.value)return
  editingId.value=''
  creating.value=true
  editor.value={name:NEW_NAME,content:NEW_CONTENT}
  editorProblems.value=[]   // 上一个项目的问题清单不能留给下一个：它会挡着「保存」
  editorBaseline.value=JSON.stringify(editor.value)
  editorOpen.value=true
}
function openEdit(project=selected.value){
  if(!project||busy.value)return
  editingId.value=project.id
  creating.value=false
  editor.value={name:project.name,content:project.content}
  editorProblems.value=[]
  editorBaseline.value=JSON.stringify(editor.value)
  editorOpen.value=true
}
async function closeEditor(){if(busy.value)return;if(JSON.stringify(editor.value)!==editorBaseline.value&&!await props.confirmAction('配置有未保存的修改，确定放弃？'))return;editorOpen.value=false}
watch(editorOpen,open=>{if(open)nextTick(()=>(creating.value?nameInput.value:editorPanel.value)?.focus())})

function selectProject(project){selectedId.value=project.id;logs.value='';detailTab.value='services'}
function clearSelection(){selectedId.value='';logs.value=''}

async function save(startAfter=false){
  if(busy.value)return
  const wasCreating=creating.value
  busy.value=true
  try{
    const checked=await axios.post('/api/compose/validate',{content:editor.value.content})
    editorProblems.value=checked.data.problems||[]
    if(blocked.value){notify('Compose 格式校验未通过，请修正编辑器标出的错误。','error');return}
    const response=creating.value?await axios.post('/api/compose/projects',editor.value):await axios.put(`/api/compose/projects/${editingId.value}`,editor.value)
    await refresh();selectProject(response.data);editorOpen.value=false
    if(startAfter&&status.value.available){busy.value=false;await action('start',response.data.id);return}
    notify(wasCreating?'Compose 项目已保存。':'compose.yaml 已更新。','success')
  }catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}

/* 导入既有「点按钮选文件」，也支持把 .yml 拖到页面上任意位置：
   拖拽事件挂在 .compose-page 上（弹窗 Teleport 到了 body，不受影响），
   dragover 只在带文件时 preventDefault —— 否则页面里选中文本的拖拽会被吞掉。 */
function hasFiles(event){return Array.from(event.dataTransfer?.types||[]).includes('Files')}
function dragOver(event){
  if(!hasFiles(event))return
  event.preventDefault()
  if(event.dataTransfer)event.dataTransfer.dropEffect='copy'
  dropping.value=true
}
function dragLeave(event){if(event.currentTarget.contains(event.relatedTarget))return;dropping.value=false}
function dropFiles(event){event.preventDefault();dropping.value=false;const file=event.dataTransfer?.files?.[0];if(file)uploadFile(file)}
async function uploadFile(file){
  if(!file||busy.value)return
  if(!/\.(ya?ml)$/i.test(file.name))return notify('请拖入 .yml 或 .yaml 文件。','info')
  const data=new FormData();data.append('file',file);data.append('name',file.name.replace(/\.(ya?ml)$/i,''))
  busy.value=true
  try{const response=await axios.post('/api/compose/projects/import',data);await refresh();selectProject(response.data);notify('已导入 compose 文件。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
function importFile(event){const file=event.target.files?.[0];event.target.value='';uploadFile(file)}

async function action(name,id=selectedId.value){
  if(busy.value||!status.value.available)return
  const project=projects.value.find(p=>p.id===id);if(!project)return
  if(name==='down'&&!await props.confirmAction(`停止并移除“${project.name}”创建的容器与网络？`))return
  if(busy.value)return
  busy.value=true;actionId.value=id;actionName.value=name
  try{await axios.post(`/api/compose/projects/${id}/${name}`);await refresh();notify('操作完成。','success')}
  catch(error){notify(errorMessage(error),'error')}
  finally{busy.value=false;actionId.value='';actionName.value=''}
}
async function loadLogs(){
  if(!selected.value||logLoading.value)return
  const id=selected.value.id, generation=++logGeneration
  logController=new AbortController()
  logLoading.value=true
  try{
    const response=await axios.get(`/api/compose/projects/${id}/logs`,{params:{tail:logTail.value},timeout:45000,signal:logController.signal})
    if(disposed||id!==selectedId.value||generation!==logGeneration)return
    logs.value=response.data.logs||'';logError.value='';logUpdated.value=new Date().toLocaleTimeString()
    await nextTick();if(followLogs.value&&logView.value)logView.value.scrollTop=logView.value.scrollHeight
  }catch(error){if(!axios.isCancel(error)&&!disposed&&generation===logGeneration)logError.value=errorMessage(error)}finally{if(generation===logGeneration)logLoading.value=false}
}
watch([detailTab,selectedId,logTail],()=>{cancelLogs();logError.value='';logUpdated.value='';if(detailTab.value==='logs'&&selectedId.value&&status.value.available)loadLogs()})
async function remove(id=selectedId.value){
  const project=projects.value.find(p=>p.id===id)
  if(busy.value||!project||!await props.confirmAction(`删除“${project.name}”的 compose 文件记录？运行中的容器不会自动停止。`))return
  busy.value=true
  try{await axios.delete(`/api/compose/projects/${id}`);await refresh();notify('Compose 项目已删除。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
onMounted(async()=>{
  fitWorkspace()
  layoutObserver=new ResizeObserver(fitWorkspace)
  layoutObserver.observe(workspace.value.previousElementSibling)
  window.addEventListener('resize',fitWorkspace)
  await refresh()
  pollLogs()
})
onBeforeUnmount(()=>{disposed=true;cancelLogs();clearTimeout(logTimer);layoutObserver?.disconnect();window.removeEventListener('resize',fitWorkspace)})
</script>

<template>
  <section class="compose-page" @dragover="dragOver" @dragleave="dragLeave" @drop.prevent="dropFiles">
    <header class="compose-head">
      <div><span class="eyebrow">LOCAL COMPOSE WORKSPACE</span><h1>Docker 服务管理</h1><p>集中保存 compose 文件，按需启动临时测试与开发服务。</p></div>
      <span class="compose-local">本机 Docker · 文件不出本机</span>
    </header>

    <!-- 一整块工作台：状态栏在上，两栏在下，共用一条墨线框 —— 别的工具页也是这个做法，
         页头因此只剩标题，工作台拿到视口里剩下的全部高度。 -->
    <div class="compose-studio">
      <div class="compose-engine-bar" :class="{ready:status.available}">
        <div class="compose-engine"><i></i><b>{{status.available?'Docker 已就绪':'Docker 不可用'}}</b><small :title="status.message">{{status.available?`Engine ${status.docker_version} · Compose ${status.compose_version}`:status.message}}</small><button type="button" class="compose-btn" @click="refresh">刷新检测</button></div>
        <button class="compose-btn" @click="accessOpen=!accessOpen" :aria-expanded="accessOpen">连接设置 {{accessOpen?'▴':'▾'}}</button>
        <div v-if="accessOpen" class="compose-access">
          <label class="compose-access-toggle"><input v-model="sudo.enabled" type="checkbox" role="switch"><span><b>sudo 权限</b><small>用于访问本机 Docker</small></span></label>
          <span class="compose-access-badge" :class="{on:status.use_sudo}">{{status.use_sudo?'已启用 · 本次会话':'普通用户模式'}}</span>
          <!-- 密码是 sudo 的补充设置，接在开关后面同一行就够：它只在本次后端会话里有效，
               这句说明放进 title —— 为它单开一行，代价比一个 tooltip 大得多。 -->
          <label v-if="sudo.enabled" class="compose-access-password" title="密码只在本次后端会话里有效，重启后端后需要重新设置">
            <span>{{status.password_set?'更新密码':'sudo 密码'}}</span>
            <input v-model="sudo.password" type="password" autocomplete="off" :placeholder="status.password_set?'留空保留当前密码':'输入本机用户密码'" @keydown.enter.prevent="applyAccess">
          </label>
          <button type="button" class="compose-btn accent" :disabled="busy" @click="applyAccess">应用设置</button>
        </div>
      </div>

      <div ref="workspace" class="compose-layout" :class="{'is-list':!selected}" :style="{'--compose-height':`${workspaceHeight}px`}">
        <section v-if="!selected" class="compose-overview">
          <div class="compose-table-toolbar">
            <div><b>Compose 项目</b><small>{{projects.length}} 个项目 · {{projects.filter(p=>runningCount(p)>0).length}} 个运行中项目</small></div>
            <input v-model="search" aria-label="搜索项目" placeholder="搜索项目名称…"><select v-model="stateFilter" aria-label="状态筛选"><option value="">全部状态</option><option value="running">运行中</option><option value="stopped">已停止</option><option value="undeployed">未部署</option><option value="unknown">状态未知</option></select>
            <button class="compose-btn" :disabled="busy" @click="refresh">刷新</button>
            <button class="compose-btn" :disabled="busy" @click="fileInput?.click()">导入 YAML</button>
            <button class="compose-btn primary" :disabled="busy" @click="openCreate">＋ 新建项目</button>
          </div>
          <div class="compose-table-scroll"><table class="compose-table">
            <thead><tr><th>项目名称</th><th>状态</th><th title="优先显示容器实际镜像，未部署时显示 YAML 配置">镜像</th><th>创建时间</th><th>IP 地址</th><th title="优先显示容器实际发布端口，未部署时显示 YAML 配置；变量保留原样">发布端口</th><th>最近更新</th><th>快捷操作</th></tr></thead>
            <tbody><tr v-for="project in pageProjects" :key="project.id">
              <td><button class="compose-name-link" @click="selectProject(project)">{{project.name}}</button></td>
              <td><span class="compose-state-pill" :class="{running:stateOf(project)==='running'}">{{stateLabel(project)}}</span></td>
              <td class="compose-metadata"><ComposeImage v-for="value in projectValues(project,'image')" :key="value" :image="value" :available="status.available" :revision="imageRevision"/><span v-if="!projectValues(project,'image').length">—</span></td>
              <td class="compose-metadata"><div v-for="value in projectValues(project,'created_at')" :key="value">{{date(value)}}</div><span v-if="!projectValues(project,'created_at').length">—</span></td>
              <td class="compose-metadata"><div v-for="value in projectValues(project,'ip_addresses')" :key="value">{{value}}</div><span v-if="!projectValues(project,'ip_addresses').length">—</span></td>
              <td class="compose-metadata"><div v-for="value in projectValues(project,'published_ports')" :key="value">{{value}}</div><span v-if="!projectValues(project,'published_ports').length">—</span></td>
              <td>{{date(project.updated_at)}}</td>
              <td><div class="compose-row-actions"><button class="compose-btn" :disabled="busy||!status.available" @click="action(runningCount(project)?'stop':'start',project.id)">{{actionId===project.id?progressLabel:runningCount(project)?'停止':'启动'}}</button><button class="compose-btn" @click="selectProject(project);detailTab='logs'">日志</button><button class="compose-btn" @click="selectProject(project)">管理</button><ComposeProjectMenu :disabled="busy" :available="status.available" :deployed="Boolean(project.services?.length)" @choose="menuChoice($event,project)"/></div></td>
            </tr></tbody>
          </table>
          <div v-if="!filteredProjects.length" class="compose-list-empty"><b>{{projects.length?'没有匹配的项目':'创建你的第一个测试环境'}}</b><p>新建 Compose 项目，或拖入已有的 .yml / .yaml 文件。</p><button v-if="!projects.length" class="compose-btn primary" @click="openCreate">＋ 新建项目</button></div></div>
          <nav class="compose-pagination" aria-label="项目分页"><span>共 {{filteredProjects.length}} 个项目</span><label>每页<select v-model.number="pageSize" aria-label="每页项目数"><option :value="10">10 条</option><option :value="20">20 条</option><option :value="50">50 条</option></select></label><button class="compose-btn" :disabled="pageNumber===1" @click="pageNumber=1">首页</button><button class="compose-btn" :disabled="pageNumber===1" @click="pageNumber--">上一页</button><span aria-live="polite">{{pageNumber}} / {{pageCount}}</span><button class="compose-btn" :disabled="pageNumber===pageCount" @click="pageNumber++">下一页</button><button class="compose-btn" :disabled="pageNumber===pageCount" @click="pageNumber=pageCount">末页</button></nav>
        </section>
        <section v-else class="compose-detail">
          <header class="compose-detail-header"><div class="compose-detail-title"><button class="compose-btn" :disabled="busy" @click="clearSelection">← 项目列表</button><div><h2>{{selected.name}}</h2><small>更新于 {{date(selected.updated_at)}}</small></div><span class="compose-state-pill" :class="{running:stateOf(selected)==='running'}">{{stateLabel(selected)}}</span></div>
          <div class="compose-detail-actions"><button class="compose-btn primary" :disabled="busy||!status.available" @click="action('start')">启动 / 更新部署</button><button class="compose-btn" :disabled="busy||!status.available" @click="action('stop')">停止</button><button class="compose-btn" :disabled="busy||!status.available" @click="action('restart')">重启</button><button class="compose-btn" :disabled="busy" @click="openEdit(selected)">编辑配置</button><ComposeProjectMenu :disabled="busy" :available="status.available" :deployed="Boolean(selected.services?.length)" @choose="menuChoice($event,selected)"/><span v-if="busy" role="status">{{progressLabel}}</span></div>
          </header><nav class="compose-detail-tabs" aria-label="项目详情"><button v-for="tab in [{id:'services',name:'状态'},{id:'inspect',name:'容器 Inspect'},{id:'config',name:'Compose 配置'},{id:'logs',name:'运行日志'}]" :key="tab.id" :class="{'is-current':detailTab===tab.id}" @click="detailTab=tab.id">{{tab.name}}</button></nav>
          <div v-if="detailTab==='services'" class="compose-table-scroll"><table class="compose-table"><thead><tr><th>服务名称</th><th>状态</th><th>运行信息</th><th>操作</th></tr></thead><tbody><tr v-for="(service,index) in selected.services" :key="index"><td>{{service.name}}</td><td><span class="compose-state-pill" :class="{running:serviceClass(service.state)==='running'}">{{service.state}}</span></td><td>{{service.status||'—'}}</td><td><button class="compose-btn" :disabled="busy||!status.available" @click="detailTab='logs';loadLogs()">查看日志</button></td></tr></tbody></table><div v-if="!selected.services?.length" class="compose-list-empty"><b>尚无服务状态</b><p>点击“启动 / 更新部署”创建服务，或刷新检测以获取最新状态。</p></div></div>
          <ContainerInspect v-else-if="detailTab==='inspect'" :project-id="selectedId" :available="status.available"/><div v-else-if="detailTab==='config'" class="compose-config-preview"><div class="compose-table-toolbar"><b>compose.yaml</b><button class="compose-btn" :disabled="busy" @click="openEdit(selected)">编辑配置</button></div>          <div class="compose-validation"><button class="compose-btn" :disabled="validating" @click="validateConfig">{{validating?'校验中…':'校验 Compose 格式'}}</button><span v-if="validation&&validation.length===0" role="status">✓ YAML 语法与基础 Compose 结构检查通过</span><ul v-if="validation?.length" role="status"><li v-for="(problem,index) in validation" :key="index">第 {{problem.line}} 行 · {{problem.severity==='error'?'错误':'提示'}}：{{problem.text}}</li></ul></div>
<pre>{{selected.content}}</pre></div>
          <div v-else class="compose-log-panel">
            <div class="compose-table-toolbar"><b>运行日志</b><select v-model="logTail" aria-label="日志行数"><option :value="300">最近 300 行</option><option :value="1000">最近 1000 行</option><option :value="2000">最近 2000 行</option></select><button class="compose-btn" :disabled="logLoading||!status.available" @click="loadLogs">{{logLoading?'读取中…':'刷新'}}</button><button class="compose-btn" :aria-pressed="liveLogs" @click="liveLogs=!liveLogs">{{liveLogs?'暂停自动刷新':'开启自动刷新'}}</button><button class="compose-btn" :disabled="!logs" @click="downloadText(logs,selected.name+'.log')">下载日志</button></div>
            <div class="compose-log-options"><input v-model="logSearch" aria-label="搜索日志" placeholder="筛选日志关键词…"><label><input type="checkbox" v-model="followLogs">跟随最新日志</label><small>{{liveLogs?'每 2 秒自动刷新':'自动刷新已暂停'}} · {{logUpdated?'更新于 '+logUpdated:'等待日志'}}</small></div>
            <p v-if="logError" class="compose-log-error" role="status">{{logError}}</p>
            <button v-if="!followLogs" class="compose-btn compose-latest" @click="latestLogs">↓ 回到最新</button><pre ref="logView" class="compose-logs" @scroll="logScroll">{{visibleLogs||(logSearch?'没有匹配的日志':'暂无日志输出')}}</pre>
          </div>
        </section>
        <input ref="fileInput" class="compose-file-input" hidden type="file" accept=".yml,.yaml" @change="importFile">
      </div>
    </div>

    <p ref="footnote" class="compose-footnote">compose 文件保存在本机；「启动 / 移除」只作用于这个项目自己创建的容器与网络。</p>

    <Teleport to="body">
      <Transition name="modal">
        <div v-if="editorOpen" class="modal-mask" @click.self="closeEditor" @dragover.prevent @drop.prevent>
          <section ref="editorPanel" class="compose-editor-card" role="dialog" aria-modal="true" aria-labelledby="compose-editor-title" tabindex="-1" @keydown.esc="closeEditor">
            <header class="compose-editor-card-head"><div><b id="compose-editor-title">{{creating?'新建 Compose 项目':'编辑 Compose 项目'}}</b><small>{{creating?'粘贴 compose.yaml，保存后出现在项目列表中。已有 .yml 文件也可以直接拖到页面上导入。':'改完保存即写回这个项目的 compose.yaml，运行中的容器不会自动重启。'}}</small></div><button type="button" class="compose-editor-close" aria-label="关闭" @click="closeEditor">×</button></header>
            <label class="compose-name">项目名称<input ref="nameInput" v-model.trim="editor.name" placeholder="例如：接口联调环境"></label>
            <div class="compose-source"><span>compose.yaml</span><ComposeYamlEditor v-model="editor.content" :check-source="checkSource" :placeholder="PLACEHOLDER" @problems="editorProblems=$event"></ComposeYamlEditor></div>
            <footer class="compose-editor-card-actions"><button type="button" :disabled="busy" @click="closeEditor">取消</button><button type="button" class="primary" :disabled="busy||blocked" :title="blocked?'compose.yaml 里还有语法错误，先照编辑器下面的提示改好':''" @click="save(false)">{{busy?'处理中…':creating?'保存项目':'保存修改'}}</button><button type="button" class="start-now" :disabled="busy||blocked||!status.available" :title="blocked?'compose.yaml 里还有语法错误，先照编辑器下面的提示改好':''" @click="save(true)">保存并启动</button></footer>
          </section>
        </div>
      </Transition>
    </Teleport>
  </section>
</template>

<style scoped>
/* 一整块样式。这个页面此前叠了五层补丁（.compose-page 前缀的重写），这次按别的工具页
   （#json 那种「一块工作台」）重排，索性并成一份，避免再往上面加第六层。

   颜色全部走下面这组 --compose-* 局部令牌：亮色写在 :root，暗色只重定义同一组名字。
   令牌挂在 :root 而不是 .compose-page 上，是因为编辑弹窗 Teleport 到了 body、不在页面里
   —— 挂 :root 两处都继承得到，暗色也就只需要一份覆盖，不用给每个选择器各写一条。 */
:global(:root){
  --compose-ink:#252526;      /* 墨线：框、按钮边、强调文字 */
  --compose-text:#252526;     /* 正文 */
  --compose-muted:#62625b;    /* 次要文字 */
  --compose-line:#b5b3a8;     /* 分隔细线 */
  --compose-paper:#fffef9;    /* 面板底 */
  --compose-soft:#f4f2e9;     /* 次级底（右栏、按钮条） */
  --compose-tint:#edf7f7;     /* 栏头底 */
  --compose-note:#fff9dc;     /* 空态 / 拖入区底 */
  --compose-note-line:#9d936c;
  --compose-active:#dff7fa;   /* 选中底 */
  --compose-accent:#ffe250;--compose-on-accent:#252526;--compose-cyan:#83d9e7;
  --compose-ok:#1f6b47;--compose-ok-bg:#effaf2;--compose-ok-line:#7fc0a5;
  --compose-warn:#8a5a12;
  --compose-bad:#a33e50;--compose-bad-bg:#fff2f3;--compose-bad-line:#cc8b98;
  --compose-code-bg:#fffef9;--compose-code-text:#3c4a46;
  /* YAML 高亮的五个颜色。亮色的取值直接抄 PyCodeEditor 的那套（键=蓝、字符串=绿、
     注释=灰蓝斜体、标签/锚点=紫、标点=灰），这样两个编辑器看着是同一支笔写的。 */
  --compose-yaml-key:#3562bd;
  --compose-yaml-string:#2f7c63;
  --compose-yaml-comment:#93a7bf;
  --compose-yaml-ref:#a03fa0;
  --compose-yaml-mark:#74869b;
  /* 硬阴影也做成令牌：暗色下全局用的是近黑的 #080b10，墨线在暗色里是灰蓝的，
     直接拿 --compose-ink 当阴影色会糊成一片。 */
  --compose-shadow-sm:2px 2px var(--compose-ink);
  --compose-shadow:3px 3px var(--compose-ink);
  --compose-shadow-lg:4px 4px var(--compose-ink);
  /* 同一行里的控件（按钮、输入框、下拉框）统一这么高：横平竖直靠的是这一个数，
     而不是每处各写各的 padding。 */
  --compose-control:34px;
  /* 输入框、下拉框的底：跟代码块同一档「凹下去」的底 —— 亮色是纸白，暗色比面板深一档，
     所以两种主题里都只靠一圈细边把框划出来。 */
  --compose-field:var(--compose-code-bg);
}
:global(html[data-theme=dark]){
  --compose-ink:#526071;--compose-text:#e9edf3;--compose-muted:#adb7c5;--compose-line:#526071;
  --compose-paper:#222833;--compose-soft:#1b2029;--compose-tint:#222e3c;--compose-note:#273342;
  --compose-note-line:#566c83;--compose-active:#234652;
  --compose-ok:#a9e7c5;--compose-ok-bg:#203e32;--compose-ok-line:#659c7d;
  --compose-warn:#f6dfa0;
  --compose-bad:#ffc0cc;--compose-bad-bg:#432631;--compose-bad-line:#bc7188;
  --compose-code-bg:#18222e;--compose-code-text:#c5d9f2;
  /* 同一组高亮色在暗底上各提一档亮度：#18222e 比纸白暗得多，照搬亮色那组会糊掉。 */
  --compose-yaml-key:#7fb3f0;
  --compose-yaml-string:#7fd0a8;
  --compose-yaml-comment:#8296ad;
  --compose-yaml-ref:#e0a3e0;
  --compose-yaml-mark:#8798ad;
  --compose-shadow-sm:2px 2px #080b10;
  --compose-shadow:3px 3px #080b10;
  --compose-shadow-lg:4px 4px #080b10;
}

.compose-page{color:var(--compose-text);min-width:0}
.compose-page .compose-file-input{display:none!important}
.compose-overview .compose-table{min-width:1260px}.compose-metadata{font-size:12px;min-width:135px;max-width:260px;overflow-wrap:anywhere}.compose-metadata>div+div{margin-top:5px}
.compose-page .compose-access label{margin:0;flex-direction:row;align-items:center;color:var(--compose-text)}
.compose-page .compose-access-password{flex:0 1 auto;min-width:0}
.compose-page .compose-access-toggle input{flex:none;padding:0}
.compose-page .compose-detail-header{margin:0}
.compose-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.compose-head h1{margin:6px 0;font-size:var(--fs-29)}.compose-head p{margin:4px 0;color:var(--compose-muted)}.compose-local{font-size:12px;color:var(--compose-muted)}
.compose-studio{margin-top:18px;border:1px solid var(--compose-line);border-radius:6px;background:var(--compose-paper);min-width:0}
.compose-engine-bar{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:12px;padding:12px 16px;border-bottom:1px solid var(--compose-line)}
.compose-engine{display:flex;align-items:center;gap:10px;min-width:0}.compose-engine i{width:9px;height:9px;border-radius:50%;background:var(--compose-warn);flex:none}.compose-engine b{color:var(--compose-warn);white-space:nowrap}.compose-engine-bar.ready b{color:var(--compose-ok)}.compose-engine-bar.ready i{background:var(--compose-ok)}.compose-engine small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--compose-muted)}.compose-engine .compose-btn{margin-left:auto}
.compose-access{grid-column:1/-1;display:flex;align-items:center;gap:16px;flex-wrap:wrap;border-top:1px solid var(--compose-line);padding-top:12px}.compose-access-toggle,.compose-access-password{display:flex;flex-direction:row;align-items:center;gap:8px;margin:0}.compose-access-toggle span{display:grid}.compose-access-toggle small,.compose-access-badge{color:var(--compose-muted);font-size:12px}.compose-access-toggle input{width:18px;height:18px;margin:0}.compose-access-password input{width:240px;max-width:100%}.compose-access-badge.on{color:var(--compose-ok)}
.compose-page .compose-btn{min-height:34px;flex:none;padding:6px 12px;border:1px solid var(--compose-line);border-radius:4px;background:var(--compose-paper);color:var(--compose-text);font:inherit;font-size:var(--fs-12);white-space:nowrap;cursor:pointer}.compose-page .compose-btn:hover:not(:disabled){background:var(--compose-active)}.compose-page .compose-btn.primary{background:var(--compose-accent);color:var(--compose-on-accent);box-shadow:none}.compose-page .compose-btn:disabled{opacity:.5;cursor:not-allowed}
.compose-page input:not([type=checkbox]),.compose-page select,.compose-name input{box-sizing:border-box;min-width:0;min-height:34px;padding:6px 10px;border:1px solid var(--compose-line);border-radius:4px;background:var(--compose-field);color:var(--compose-text);font:inherit;font-size:var(--fs-12)}
.compose-layout{height:var(--compose-height);min-width:0}.compose-overview,.compose-detail{height:100%;display:flex;flex-direction:column;min-width:0;min-height:0}.compose-overview>.compose-table-scroll{flex:1}
.compose-pagination{display:flex;align-items:center;justify-content:flex-end;gap:8px;flex-wrap:wrap;flex:none;padding:12px 16px;border-top:1px solid var(--compose-line);font-size:var(--fs-12)}.compose-pagination>span:first-child{margin-right:auto}.compose-page .compose-pagination label{display:flex;flex-direction:row;align-items:center;gap:8px;margin:0;color:var(--compose-muted)}
.compose-table-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:14px 16px;border-bottom:1px solid var(--compose-line);flex:none}.compose-table-toolbar>div:first-child{margin-right:auto}.compose-table-toolbar small{display:block;margin-top:3px;color:var(--compose-muted)}.compose-table-toolbar input{width:220px}
.compose-table-scroll{overflow:auto;min-height:0;min-width:0}.compose-detail>.compose-table-scroll{flex:1}.compose-table{width:100%;border-collapse:collapse;text-align:left;font-size:var(--fs-12);min-width:720px}.compose-page .compose-table :is(th,td){padding:14px 16px;border:0;border-bottom:1px solid var(--compose-line);vertical-align:middle}.compose-table th{background:var(--compose-soft);color:var(--compose-muted);white-space:nowrap}.compose-table tbody tr:hover{background:var(--compose-tint)}.compose-row-actions{display:flex;gap:6px;align-items:center}.compose-page .compose-name-link{max-width:500px;overflow-wrap:anywhere;border:0;background:transparent;color:var(--compose-yaml-key);text-align:left;padding:0;cursor:pointer;font-weight:650}
.compose-state-pill{display:inline-flex;white-space:nowrap;padding:4px 9px;border-radius:5px;background:var(--compose-soft);color:var(--compose-muted);font-size:12px}.compose-state-pill.running{background:var(--compose-ok-bg);color:var(--compose-ok)}
.compose-list-empty{padding:48px 20px;text-align:center;color:var(--compose-muted)}.compose-list-empty p{margin:12px 0 20px}
.compose-detail-header{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:14px 16px;flex-wrap:wrap;flex:none}.compose-detail-title{display:flex;align-items:center;gap:12px;min-width:0;flex:1}.compose-detail-title>div{min-width:0}.compose-detail-title h2{margin:0;font-size:18px;overflow-wrap:anywhere}.compose-detail-title small{color:var(--compose-muted);font-size:12px}.compose-detail-actions{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.compose-detail-tabs{display:flex;gap:20px;padding:0 16px;border-bottom:1px solid var(--compose-line);flex:none;overflow:auto}.compose-page .compose-detail-tabs button{border:0;border-bottom:3px solid transparent;background:transparent;color:var(--compose-muted);padding:12px 0;white-space:nowrap;cursor:pointer}.compose-page .compose-detail-tabs .is-current{border-bottom-color:var(--compose-cyan);color:var(--compose-text)}
.compose-log-panel,.compose-config-preview{display:flex;flex-direction:column;flex:1;min-height:0;position:relative}.compose-log-options{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 16px}.compose-log-options label{display:flex;flex-direction:row;align-items:center;gap:6px;margin:0}.compose-log-options input[type=checkbox]{width:16px;height:16px;margin:0}.compose-log-options small{color:var(--compose-muted)}.compose-log-panel pre,.compose-config-preview pre{flex:1;min-height:0;max-height:none;margin:0 16px 16px;padding:16px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--compose-code-bg);color:var(--compose-code-text);border:1px solid var(--compose-line);font:13px/1.8 ui-monospace,monospace}
.compose-latest{position:absolute;right:32px;bottom:30px;z-index:1}.compose-log-error{margin:0 16px 8px;padding:8px;color:var(--compose-bad);background:var(--compose-bad-bg)}.compose-validation{padding:12px 16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;flex:none}.compose-validation ul{width:100%;max-height:100px;overflow:auto}.compose-validation>span{color:var(--compose-ok)}
.compose-footnote{margin:14px 0 0;color:var(--compose-muted);font-size:12px}
@media(max-width:700px){.compose-local{display:none}.compose-engine{flex-wrap:wrap}.compose-engine small{flex-basis:100%;order:3}.compose-engine-bar{padding:12px}.compose-access-password{flex-wrap:wrap}.compose-access-password input{width:100%}.compose-table-toolbar>div:first-child{width:100%}.compose-table-toolbar input{flex:1;width:150px}.compose-detail-title{flex-wrap:wrap}.compose-detail-header{gap:10px}.compose-detail-actions{width:100%}.compose-detail-tabs{gap:14px}.compose-layout:not(.is-list){height:760px}.compose-log-options>input{width:100%}}
.compose-editor-card{display:grid;grid-template-rows:auto auto minmax(0,1fr) auto;box-sizing:border-box;width:min(880px,100%);height:min(88vh,880px);overflow:hidden;border:2px solid var(--compose-ink);border-radius:3px;background:var(--compose-paper);box-shadow:var(--compose-shadow-lg)}
.compose-editor-card-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:14px 18px;border-bottom:1px solid var(--compose-line);background:var(--compose-tint)}
.compose-editor-card-head b{font-size:var(--fs-15)}
.compose-editor-card-head small{display:block;margin-top:3px;font-size:var(--fs-10);line-height:1.6;color:var(--compose-muted)}
.compose-editor-close{display:grid;flex:none;width:28px;height:28px;place-items:center;border:1px solid transparent;border-radius:3px;background:transparent;color:var(--compose-muted);font-size:var(--fs-18);line-height:1;cursor:pointer}
.compose-editor-close:hover{border-color:var(--compose-ink);background:var(--compose-cyan);color:var(--compose-on-accent)}
/* 项目名跟标签同一行：少一层竖排，编辑框就能多一行。 */
.compose-name{display:flex;flex-direction:row;align-items:center;gap:10px;margin:14px 18px 0;font-size:var(--fs-11);font-weight:700}
.compose-name input{flex:1}
.compose-source{display:flex;min-height:0;flex-direction:column;gap:6px;margin:12px 18px 0;font-size:var(--fs-11);font-weight:700}
.compose-source>span{flex:none;color:var(--compose-muted)}
/* 行号槽 + 高亮的字号、边框、聚焦描边都在 ComposeYamlEditor.vue 里（那是个真编辑器，
   外框和 CM 内部一起长）；这一行只把它的位置钉在标签下面，剩余高度全给它。 */
.compose-editor-card-actions{display:flex;flex-wrap:wrap;align-items:center;justify-content:flex-end;gap:8px;margin-top:12px;padding:12px 18px;border-top:1px solid var(--compose-line);background:var(--compose-soft)}
/* flex:none 是为了压掉 style.css 里全局的 `.primary{flex:2}` —— 那是给左表单右按钮的老布局用的，
   在这里只会让「保存」比「取消」宽出一截。 */
.compose-editor-card-actions button{font:inherit;flex:none;font-size:var(--fs-12);min-height:34px;padding:7px 14px;border:1px solid var(--compose-ink);border-radius:3px;background:var(--compose-paper);color:var(--compose-text);font-weight:700;cursor:pointer}
.compose-editor-card-actions button:hover:not(:disabled){background:var(--compose-cyan);color:var(--compose-on-accent)}
.compose-editor-card-actions .primary{border:2px solid var(--compose-ink);background:var(--compose-accent);color:var(--compose-on-accent);box-shadow:var(--compose-shadow-sm);font-weight:750}
.compose-editor-card-actions .primary:hover:not(:disabled){background:#ffce36;color:var(--compose-on-accent)}
.compose-editor-card-actions .start-now{border-color:var(--compose-ok-line);background:var(--compose-ok-bg);color:var(--compose-ok)}
.compose-editor-card-actions .start-now:hover:not(:disabled){background:var(--compose-ok-bg);color:var(--compose-ok);box-shadow:2px 2px var(--compose-ok-line)}
.compose-editor-card-actions button:disabled{opacity:1;background:var(--compose-soft);border-color:var(--compose-line);color:var(--compose-muted);box-shadow:none;cursor:not-allowed}


</style>
