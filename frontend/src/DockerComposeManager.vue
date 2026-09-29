<script setup>
import {computed, onMounted, ref} from 'vue'
import axios from 'axios'

const emit = defineEmits(['notify'])
const props = defineProps({confirmAction:{type:Function, required:true}})
const status = ref({available:false,message:'正在检测 Docker…'})
const sudo = ref({enabled:false,password:''})
const projects = ref([])
const selectedId = ref('')
const editor = ref({name:'',content:''})
const creating = ref(true)
const busy = ref(false)
const logs = ref('')
const logService = ref('')
const fileInput = ref()

const templates = {
  blank: {name:'新建服务',content:"services:\n  app:\n    image: nginx:alpine\n    ports:\n      - '8080:80'\n"},
  database: {name:'PostgreSQL 测试',content:"services:\n  postgres:\n    image: postgres:16-alpine\n    environment:\n      POSTGRES_USER: app\n      POSTGRES_PASSWORD: app\n      POSTGRES_DB: app\n    ports:\n      - '5432:5432'\n    volumes:\n      - postgres_data:/var/lib/postgresql/data\nvolumes:\n  postgres_data:\n"},
  redis: {name:'Redis 测试',content:"services:\n  redis:\n    image: redis:7-alpine\n    ports:\n      - '6379:6379'\n    command: redis-server --appendonly yes\n    volumes:\n      - redis_data:/data\nvolumes:\n  redis_data:\n"},
}
const selected = computed(()=>projects.value.find(project=>project.id===selectedId.value))

function notify(message,type='info'){emit('notify',message,type)}
function errorMessage(error){return error.response?.data?.detail||error.message||'操作失败'}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—'}
function serviceClass(value){return /running|up/i.test(value)?'running':/exit|dead|stopped/i.test(value)?'stopped':'unknown'}

async function refresh(){
  try{
    const [nextStatus,nextProjects]=await Promise.all([axios.get('/api/compose/status'),axios.get('/api/compose/projects')])
    status.value=nextStatus.data;projects.value=nextProjects.data
    sudo.value.enabled=Boolean(nextStatus.data.use_sudo)
    if(selectedId.value&&!projects.value.some(project=>project.id===selectedId.value))resetEditor()
  }catch(error){notify(errorMessage(error),'error')}
}
async function applyAccess(){
  if(sudo.value.enabled&&!sudo.value.password&&!status.value.password_set)return notify('请输入 sudo 密码，或关闭 sudo 模式。','info')
  busy.value=true
  try{
    await axios.post('/api/compose/access',{use_sudo:sudo.value.enabled,sudo_password:sudo.value.password})
    sudo.value.password='';await refresh();notify(sudo.value.enabled?'sudo 权限已在本次会话启用。':'已关闭 sudo 权限模式。','success')
  }catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
function resetEditor(template='blank'){
  const value=templates[template];selectedId.value='';creating.value=true;editor.value={name:value.name,content:value.content};logs.value='';logService.value=''
}
function selectProject(project){selectedId.value=project.id;creating.value=false;editor.value={name:project.name,content:project.content};logs.value='';logService.value=''}
function useTemplate(event){const key=event.target.value;resetEditor(key)}
async function save(){
  const wasCreating=creating.value
  busy.value=true
  try{
    const response=creating.value?await axios.post('/api/compose/projects',editor.value):await axios.put(`/api/compose/projects/${selectedId.value}`,editor.value)
    await refresh();selectProject(response.data);notify(wasCreating?'Compose 项目已保存。':'compose.yaml 已更新。','success')
  }catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
async function importFile(event){
  const file=event.target.files?.[0];if(!file)return
  const data=new FormData();data.append('file',file);data.append('name',file.name.replace(/\.(ya?ml)$/i,''))
  busy.value=true
  try{const response=await axios.post('/api/compose/projects/import',data);await refresh();selectProject(response.data);notify('已导入 compose 文件。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false;event.target.value=''}
}
async function action(name){
  if(!selected.value)return
  if(name==='down'&&!await props.confirmAction(`停止并移除“${selected.value.name}”创建的容器与网络？`))return
  busy.value=true
  try{const response=await axios.post(`/api/compose/projects/${selected.value.id}/${name}`);await refresh();selectProject(response.data);notify({start:'服务已启动。',stop:'服务已停止。',restart:'服务已重启。',down:'服务已移除。'}[name],'success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
async function loadLogs(){
  if(!selected.value)return
  busy.value=true
  try{logs.value=(await axios.get(`/api/compose/projects/${selected.value.id}/logs`,{params:{service:logService.value,tail:300}})).data.logs||'（暂无日志输出）'}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
async function remove(){
  if(!selected.value||!await props.confirmAction(`删除“${selected.value.name}”的 compose 文件记录？运行中的容器不会自动停止。`))return
  busy.value=true
  try{await axios.delete(`/api/compose/projects/${selected.value.id}`);await refresh();resetEditor();notify('Compose 项目已删除。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
onMounted(async()=>{resetEditor();await refresh()})
</script>

<template>
  <section class="compose-page">
    <header class="compose-head">
      <div><span class="eyebrow">LOCAL COMPOSE WORKSPACE</span><h1>Docker 服务管理</h1><p>集中保存 compose 文件，按需启动临时测试与开发服务。</p></div>
      <div class="compose-engine-area"><div class="compose-engine" :class="{ready:status.available}"><i></i><div><b>{{status.available?'Docker 已就绪':'Docker 不可用'}}</b><small>{{status.available?`Engine ${status.docker_version} · Compose ${status.compose_version}`:status.message}}</small></div><button type="button" @click="refresh">刷新检测</button></div><div class="compose-access"><label><input v-model="sudo.enabled" type="checkbox"> 使用 sudo 访问 Docker</label><input v-if="sudo.enabled&&!status.password_set" v-model="sudo.password" type="password" autocomplete="current-password" placeholder="sudo 密码"><small v-else-if="sudo.enabled">密码已在本次会话中设置</small><button type="button" :disabled="busy" @click="applyAccess">应用</button></div></div>
    </header>

    <div class="compose-layout">
      <aside class="compose-library">
        <div class="compose-library-head"><b>服务收藏</b><button type="button" @click="resetEditor()">＋ 新建</button></div>
        <div class="compose-import"><button type="button" @click="fileInput?.click()">导入 compose 文件</button><input ref="fileInput" type="file" accept=".yml,.yaml" @change="importFile"></div>
        <div v-if="!projects.length" class="compose-empty">还没有已保存的 compose 文件。</div>
        <button v-for="project in projects" :key="project.id" class="compose-project" :class="{active:project.id===selectedId}" @click="selectProject(project)"><span><b>{{project.name}}</b><small>{{project.services?.filter(item=>serviceClass(item.state)==='running').length||0}} 个运行中 · {{date(project.updated_at)}}</small></span><i>{{project.services?.length||0}}</i></button>
      </aside>

      <main class="compose-editor">
        <div class="compose-editor-head"><div><b>{{creating?'创建 Compose 项目':'编辑 Compose 项目'}}</b><small>{{creating?'可从模板开始，也可粘贴已有 YAML。':'修改后保存，再启动或重启服务。'}}</small></div><label v-if="creating">快速模板<select @change="useTemplate"><option value="blank">Nginx 示例</option><option value="database">PostgreSQL</option><option value="redis">Redis</option></select></label></div>
        <label class="compose-name">项目名称<input v-model.trim="editor.name" placeholder="例如：接口联调环境"></label>
        <label class="compose-source"><span>compose.yaml</span><textarea v-model="editor.content" spellcheck="false" placeholder="services:\n  app:\n    image: nginx:alpine"></textarea></label>
        <div class="compose-editor-actions"><button type="button" class="primary" :disabled="busy" @click="save">{{busy?'处理中…':creating?'保存项目':'保存修改'}}</button><button v-if="selected" type="button" class="danger" :disabled="busy" @click="remove">删除记录</button></div>
      </main>

      <aside class="compose-runtime">
        <div class="compose-runtime-head"><div><b>运行状态</b><small>{{selected?selected.name:'请先选择或保存一个项目'}}</small></div><div class="compose-actions"><button :disabled="!selected||busy||!status.available" @click="action('start')">启动</button><button :disabled="!selected||busy||!status.available" @click="action('restart')">重启</button><button :disabled="!selected||busy||!status.available" @click="action('stop')">停止</button><button class="danger" :disabled="!selected||busy||!status.available" @click="action('down')">移除</button></div></div>
        <div v-if="selected?.services?.length" class="compose-services"><article v-for="service in selected.services" :key="service.name"><span class="service-dot" :class="serviceClass(service.state)"></span><div><b>{{service.name}}</b><small>{{service.status||service.state}}</small></div><em>{{service.state}}</em></article></div><div v-else class="compose-empty runtime">服务启动后会在此显示容器状态。</div>
        <div class="compose-logs-head"><b>日志</b><select v-model="logService"><option value="">全部服务</option><option v-for="service in selected?.services||[]" :key="service.name" :value="service.name">{{service.name}}</option></select><button :disabled="!selected||busy||!status.available" @click="loadLogs">查看日志</button></div>
        <pre class="compose-logs">{{logs||'选择服务后点击“查看日志”。'}}</pre>
      </aside>
    </div>
  </section>
</template>

<style scoped>
.compose-page{color:var(--ui-text)}.compose-head{display:flex;align-items:center;justify-content:space-between;gap:24px;margin-bottom:22px;padding-bottom:20px;border-bottom:1px solid var(--ui-border)}.compose-head h1{margin:5px 0;font-size:var(--fs-29)}.compose-head p{margin:0}.compose-engine{display:flex;align-items:center;gap:10px;min-width:310px;padding:10px 12px;border:1px solid #d0ad65;border-radius:8px;background:#fff7d7;color:#775b1b}.compose-engine.ready{border-color:#89be9f;background:#effaf2;color:#246b47}.compose-engine>i{width:9px;height:9px;border-radius:50%;background:currentColor;box-shadow:0 0 0 4px color-mix(in srgb,currentColor 13%,transparent)}.compose-engine>div{display:flex;min-width:0;flex:1;flex-direction:column}.compose-engine b{font-size:var(--fs-12)}.compose-engine small{overflow:hidden;color:inherit;font-size:var(--fs-10);opacity:.8;text-overflow:ellipsis;white-space:nowrap}.compose-engine button{border:1px solid currentColor;border-radius:4px;background:transparent;color:inherit;font-size:var(--fs-10);cursor:pointer}.compose-layout{display:grid;grid-template-columns:minmax(220px,.72fr) minmax(390px,1.45fr) minmax(320px,1fr);min-height:720px;border:1px solid var(--pop-ink,#252526);background:var(--ui-surface);box-shadow:4px 4px var(--pop-ink,#252526)}.compose-library,.compose-editor,.compose-runtime{min-width:0;padding:18px}.compose-library,.compose-editor{border-right:1px solid var(--ui-border)}.compose-library-head,.compose-editor-head,.compose-runtime-head,.compose-logs-head{display:flex;align-items:center;justify-content:space-between;gap:10px}.compose-library-head b,.compose-editor-head b,.compose-runtime-head b{font-size:var(--fs-15)}.compose-library-head button,.compose-import button{border:1px solid var(--pop-ink,#252526);border-radius:3px;background:var(--pop-cyan,#83d9e7);color:var(--pop-ink,#252526);font-weight:700;cursor:pointer}.compose-import{margin:14px 0}.compose-import input{display:none}.compose-import button{width:100%;padding:8px}.compose-project{display:flex;width:100%;align-items:center;justify-content:space-between;gap:9px;padding:11px 8px;border:1px solid transparent;border-bottom-color:var(--ui-border);background:transparent;color:inherit;text-align:left;cursor:pointer}.compose-project:hover,.compose-project.active{border-color:var(--pop-ink,#252526);background:#e4f6f8}.compose-project span{display:flex;min-width:0;flex-direction:column;gap:3px}.compose-project b{overflow:hidden;font-size:var(--fs-12);text-overflow:ellipsis;white-space:nowrap}.compose-project small{overflow:hidden;font-size:var(--fs-9);text-overflow:ellipsis;white-space:nowrap}.compose-project>i{display:grid;width:22px;height:22px;place-items:center;border:1px solid var(--pop-ink,#252526);border-radius:50%;font-size:var(--fs-10);font-style:normal}.compose-empty{padding:22px 12px;color:var(--ui-muted);font-size:var(--fs-11);line-height:1.7;text-align:center}.compose-editor{display:flex;flex-direction:column;gap:14px}.compose-editor-head small,.compose-runtime-head small{display:block;margin-top:3px}.compose-editor-head label{display:flex;align-items:center;gap:7px;font-size:var(--fs-10)}.compose-editor select,.compose-logs-head select{border:1px solid var(--ui-border);border-radius:3px;background:var(--ui-surface);color:inherit}.compose-name{display:flex;flex-direction:column;gap:6px;font-size:var(--fs-11);font-weight:700}.compose-name input{padding:9px;border:1px solid var(--ui-border);border-radius:3px;background:var(--ui-surface);color:inherit}.compose-source{display:flex;min-height:0;flex:1;flex-direction:column;gap:6px;font-size:var(--fs-11);font-weight:700}.compose-source textarea{min-height:420px;flex:1;resize:vertical;padding:13px;border:1px solid var(--pop-ink,#252526);border-radius:3px;background:#17232a;color:#daf3f6;font:var(--fs-12)/1.65 ui-monospace,SFMono-Regular,Menlo,monospace}.compose-editor-actions,.compose-actions{display:flex;gap:8px}.compose-editor-actions button,.compose-actions button,.compose-logs-head button{padding:7px 10px;border:1px solid var(--pop-ink,#252526);border-radius:3px;background:var(--ui-surface);color:inherit;font-weight:700;cursor:pointer}.compose-editor-actions .primary,.compose-actions button:first-child{background:var(--pop-yellow,#ffe250);color:var(--pop-ink,#252526)}button.danger{border-color:#a33e50!important;background:#fff2f3!important;color:#a33e50!important}.compose-editor-actions button:disabled,.compose-actions button:disabled,.compose-logs-head button:disabled{opacity:.5;cursor:not-allowed}.compose-runtime{display:flex;min-height:0;flex-direction:column;gap:16px;background:var(--ui-soft)}.compose-runtime-head{align-items:flex-start}.compose-actions{flex-wrap:wrap;justify-content:flex-end}.compose-services{display:flex;max-height:200px;flex-direction:column;overflow:auto;border:1px solid var(--ui-border);background:var(--ui-surface)}.compose-services article{display:flex;align-items:center;gap:9px;padding:9px;border-bottom:1px solid var(--ui-border)}.compose-services article:last-child{border:0}.service-dot{width:8px;height:8px;border-radius:50%;background:#a5a9ac}.service-dot.running{background:#43a46e}.service-dot.stopped{background:#d05a68}.compose-services div{display:flex;min-width:0;flex:1;flex-direction:column}.compose-services b{font-size:var(--fs-11)}.compose-services small{overflow:hidden;font-size:var(--fs-9);text-overflow:ellipsis;white-space:nowrap}.compose-services em{font-size:var(--fs-9);font-style:normal}.runtime{margin:auto 0}.compose-logs-head{margin-top:auto}.compose-logs-head select{max-width:120px;padding:5px}.compose-logs-head button{font-size:var(--fs-10)}.compose-logs{min-height:230px;max-height:360px;margin:0;overflow:auto;padding:12px;border:1px solid var(--pop-ink,#252526);background:#17232a;color:#d4f5fa;font:var(--fs-10)/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}@media(max-width:1100px){.compose-layout{grid-template-columns:minmax(200px,.7fr) 1.3fr}.compose-runtime{grid-column:1/-1;border-top:1px solid var(--ui-border)}.compose-runtime{min-height:420px}.compose-logs{max-height:260px}}@media(max-width:700px){.compose-head{align-items:flex-start;flex-direction:column}.compose-engine{box-sizing:border-box;min-width:0;width:100%}.compose-layout{display:block}.compose-library,.compose-editor{border-right:0;border-bottom:1px solid var(--ui-border)}.compose-library{max-height:340px;overflow:auto}.compose-source textarea{min-height:340px}}
:global(html[data-theme=dark]) .compose-engine{border-color:#a18b4c;background:#443b23;color:#f6dfa0}:global(html[data-theme=dark]) .compose-engine.ready{border-color:#659c7d;background:#203e32;color:#a9e7c5}:global(html[data-theme=dark]) .compose-layout{border-color:#526071;background:#222833;box-shadow:4px 4px #080b10}:global(html[data-theme=dark]) .compose-runtime{background:#1b2029}:global(html[data-theme=dark]) .compose-project.active,:global(html[data-theme=dark]) .compose-project:hover{border-color:#83d9e7;background:#234652}:global(html[data-theme=dark]) .compose-source textarea,:global(html[data-theme=dark]) .compose-logs{border-color:#526071;background:#18222e;color:#dcebf7}
.compose-engine-area{display:flex;min-width:330px;flex-direction:column;gap:7px}.compose-access{display:flex;align-items:center;justify-content:flex-end;gap:7px;font-size:var(--fs-10)}.compose-access label{display:flex;align-items:center;gap:4px;white-space:nowrap}.compose-access input[type=password]{width:106px;padding:5px 7px;border:1px solid var(--ui-border);border-radius:3px;background:var(--ui-surface);color:inherit}.compose-access small{font-size:var(--fs-9);color:var(--ui-muted)}.compose-access button{padding:5px 8px;border:1px solid var(--pop-ink,#252526);border-radius:3px;background:var(--pop-cyan,#83d9e7);color:var(--pop-ink,#252526);font-size:var(--fs-10);font-weight:700;cursor:pointer}@media(max-width:700px){.compose-engine-area{min-width:0;width:100%}.compose-access{justify-content:flex-start;flex-wrap:wrap}}
/* `main` has a generic desktop cap elsewhere in the app. This workbench's editor
   is a grid cell, so explicitly opt it out to prevent the narrow centered column. */
.compose-layout{grid-template-columns:minmax(260px,.7fr) minmax(520px,1.35fr) minmax(430px,1.05fr);min-height:clamp(720px,calc(100dvh - 235px),1320px)}.compose-editor{width:100%;max-width:none!important;margin:0!important;padding:22px!important;box-sizing:border-box}.compose-library,.compose-runtime{padding:22px}.compose-source textarea{min-height:520px}.compose-runtime{gap:20px}.compose-empty.runtime{margin:0;padding:28px 16px;border:1px dashed var(--ui-border);background:var(--ui-surface)}@media(max-width:1100px){.compose-layout{grid-template-columns:minmax(220px,.72fr) 1.3fr}.compose-editor{padding:18px!important}.compose-runtime{padding:18px}}@media(max-width:700px){.compose-layout{min-height:0}.compose-source textarea{min-height:360px}}
</style>
