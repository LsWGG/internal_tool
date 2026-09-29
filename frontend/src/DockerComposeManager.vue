<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import axios from 'axios'
import ComposeYamlEditor from './ComposeYamlEditor.vue'

const emit = defineEmits(['notify'])
const props = defineProps({confirmAction:{type:Function, required:true}})
const status = ref({available:false,message:'正在检测 Docker…'})
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
const logService = ref('')
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

function notify(message,type='info'){emit('notify',message,type)}
function errorMessage(error){return error.response?.data?.detail||error.message||'操作失败'}
function date(value){return value?new Date(value).toLocaleString('zh-CN',{hour12:false}):'—'}
function serviceClass(value){return /running|up/i.test(value)?'running':/exit|dead|stopped/i.test(value)?'stopped':'unknown'}

async function refresh(){
  try{
    const [nextStatus,nextProjects]=await Promise.all([axios.get('/api/compose/status'),axios.get('/api/compose/projects')])
    status.value=nextStatus.data;projects.value=nextProjects.data
    sudo.value.enabled=Boolean(nextStatus.data.use_sudo)
    if(selectedId.value&&!projects.value.some(project=>project.id===selectedId.value))clearSelection()
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

/* 新建/编辑走弹窗：编辑区不再常驻占位，页面留给「服务收藏 + 运行与日志」。
   弹窗打开时才把 editor 填上，关闭即丢弃 —— 没有草稿状态要维护。 */
function openCreate(){
  creating.value=true
  editor.value={name:NEW_NAME,content:NEW_CONTENT}
  editorProblems.value=[]   // 上一个项目的问题清单不能留给下一个：它会挡着「保存」
  editorOpen.value=true
}
function openEdit(){
  const project=selected.value;if(!project)return
  creating.value=false
  editor.value={name:project.name,content:project.content}
  editorProblems.value=[]
  editorOpen.value=true
}
function closeEditor(){if(!busy.value)editorOpen.value=false}
watch(editorOpen,open=>{if(open)nextTick(()=>(creating.value?nameInput.value:editorPanel.value)?.focus())})

function selectProject(project){selectedId.value=project.id;logs.value='';logService.value=''}
function clearSelection(){selectedId.value='';logs.value='';logService.value=''}

async function save(startAfter=false){
  const wasCreating=creating.value
  busy.value=true
  try{
    const response=creating.value?await axios.post('/api/compose/projects',editor.value):await axios.put(`/api/compose/projects/${selectedId.value}`,editor.value)
    await refresh();selectProject(response.data);editorOpen.value=false
    if(startAfter&&status.value.available){await action('start');return}
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
  if(!file)return
  if(!/\.(ya?ml)$/i.test(file.name))return notify('请拖入 .yml 或 .yaml 文件。','info')
  const data=new FormData();data.append('file',file);data.append('name',file.name.replace(/\.(ya?ml)$/i,''))
  busy.value=true
  try{const response=await axios.post('/api/compose/projects/import',data);await refresh();selectProject(response.data);notify('已导入 compose 文件。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
function importFile(event){const file=event.target.files?.[0];event.target.value='';uploadFile(file)}

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
  try{await axios.delete(`/api/compose/projects/${selected.value.id}`);await refresh();clearSelection();notify('Compose 项目已删除。','success')}catch(error){notify(errorMessage(error),'error')}finally{busy.value=false}
}
onMounted(async()=>{
  fitWorkspace()
  layoutObserver=new ResizeObserver(fitWorkspace)
  layoutObserver.observe(workspace.value.previousElementSibling)
  window.addEventListener('resize',fitWorkspace)
  await refresh()
})
onBeforeUnmount(()=>{layoutObserver?.disconnect();window.removeEventListener('resize',fitWorkspace)})
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
        <span class="compose-bar-divider"></span>
        <div class="compose-access">
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

      <div ref="workspace" class="compose-layout" :style="{'--compose-height':`${workspaceHeight}px`}">
        <aside class="compose-library">
          <div class="compose-library-head">
            <div class="compose-pane-title"><span class="compose-step">01</span><b>服务收藏</b><small>{{projects.length}} 个已保存项目</small></div>
            <div class="compose-library-tools">
              <button type="button" class="compose-btn primary" @click="openCreate">＋ 新建</button>
              <button type="button" class="compose-btn" aria-label="导入 compose 文件" title="导入 compose 文件" @click="fileInput?.click()">导入</button>
              <input ref="fileInput" type="file" accept=".yml,.yaml" @change="importFile">
            </div>
          </div>
          <div class="compose-import" :class="{'is-over':dropping}">
            <b>{{dropping?'松手即导入':'把 .yml / .yaml 拖到这里'}}</b>
            <small>拖入即导入，也可以点上面的「导入」选文件</small>
          </div>
          <div class="compose-library-list">
            <button v-for="project in projects" :key="project.id" class="compose-project" :class="{'is-active':project.id===selectedId}" @click="selectProject(project)"><span><b>{{project.name}}</b><small>{{project.services?.filter(item=>serviceClass(item.state)==='running').length||0}} 个运行中 · {{date(project.updated_at)}}</small></span><i>{{project.services?.length||0}}</i></button>
            <div v-if="!projects.length" class="compose-empty">还没有已保存的 compose 文件。点「＋ 新建」写一个，或把 .yml 文件拖上来。</div>
          </div>
        </aside>

        <section class="compose-runtime">
          <div class="compose-runtime-head">
            <div class="compose-pane-title"><span class="compose-step">02</span><b>{{selected?selected.name:'未选择项目'}}</b><small :title="selected?`更新于 ${date(selected.updated_at)}`:'从左侧选一个项目'">{{selected?`更新于 ${date(selected.updated_at)}`:'从左侧选一个项目'}}</small></div>
            <div class="compose-runtime-tools">
              <div class="compose-actions"><button class="compose-btn primary" :disabled="!selected||busy||!status.available" @click="action('start')">启动</button><button class="compose-btn" :disabled="!selected||busy||!status.available" @click="action('restart')">重启</button><button class="compose-btn" :disabled="!selected||busy||!status.available" @click="action('stop')">停止</button><button class="compose-btn danger" :disabled="!selected||busy||!status.available" @click="action('down')">移除</button></div>
              <span class="compose-tools-split"></span>
              <div class="compose-file-actions"><button class="compose-btn" :disabled="!selected||busy" @click="openEdit">编辑 compose</button><button class="compose-btn danger" :disabled="!selected||busy" @click="remove">删除记录</button></div>
            </div>
          </div>
          <div v-if="selected?.services?.length" class="compose-services"><article v-for="service in selected.services" :key="service.name"><span class="service-dot" :class="serviceClass(service.state)"></span><div><b>{{service.name}}</b><small>{{service.status||service.state}}</small></div><em :class="serviceClass(service.state)">{{service.state}}</em></article></div>
          <div v-else-if="selected" class="compose-empty runtime"><span class="compose-empty-symbol">yml</span><b>还没有容器状态</b><p>点上面的「启动」创建并运行这个项目的服务。</p></div>
          <div v-else class="compose-empty runtime"><span class="compose-empty-symbol">yml</span><b>先选一个项目</b><p>左侧列表里选一个，这里会显示它的容器状态与日志。</p></div>
          <div class="compose-logs-head"><b>日志</b><select v-model="logService"><option value="">全部服务</option><option v-for="service in selected?.services||[]" :key="service.name" :value="service.name">{{service.name}}</option></select><button class="compose-btn" :disabled="!selected||busy||!status.available" @click="loadLogs">查看日志</button></div>
          <pre class="compose-logs">{{logs||'选择服务后点击“查看日志”。'}}</pre>
        </section>
      </div>
    </div>

    <p ref="footnote" class="compose-footnote">compose 文件保存在本机；「启动 / 移除」只作用于这个项目自己创建的容器与网络。</p>

    <Teleport to="body">
      <Transition name="modal">
        <div v-if="editorOpen" class="modal-mask" @click.self="closeEditor" @dragover.prevent @drop.prevent>
          <section ref="editorPanel" class="compose-editor-card" role="dialog" aria-modal="true" aria-labelledby="compose-editor-title" tabindex="-1" @keydown.esc="closeEditor">
            <header class="compose-editor-card-head"><div><b id="compose-editor-title">{{creating?'新建 Compose 项目':'编辑 Compose 项目'}}</b><small>{{creating?'粘贴 compose.yaml，保存后出现在左侧收藏里。已有 .yml 文件也可以直接拖到页面上导入。':'改完保存即写回这个项目的 compose.yaml，运行中的容器不会自动重启。'}}</small></div><button type="button" class="compose-editor-close" aria-label="关闭" @click="closeEditor">×</button></header>
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
.compose-page{color:var(--compose-text)}

/* 页头只剩标题和一个「跑在哪」的标签：状态栏搬进工作台，页头越矮，工作台越高。 */
.compose-head{display:flex;align-items:center;justify-content:space-between;gap:20px}
.compose-head h1{margin:7px 0 5px;font-size:var(--fs-29);color:var(--compose-text)}
.compose-head p{color:var(--compose-muted)}
.compose-local{flex:none;padding:9px 12px;border:1px solid var(--compose-ink);background:var(--compose-active);color:var(--compose-text);font-size:var(--fs-11);white-space:nowrap}

/* ---------- 工作台：状态栏 ---------- */
/* 状态栏、两栏共用一条 2px 墨线与硬阴影，不再各自浮一张卡片。 */
.compose-studio{margin-top:20px;overflow:hidden;border:2px solid var(--compose-ink);border-radius:3px;background:var(--compose-paper);box-shadow:var(--compose-shadow-lg)}
/* 网格而不是 flex-wrap：状态文案再长也不会把右半段挤到第二行去。
   一行里的控件都取 --compose-control 的高度，于是无论里面是文字、开关还是输入框，
   上下边都落在同两条线上；左右留白 16px，与两个栏头、内容区共用同一条竖线。 */
.compose-engine-bar{display:grid;grid-template-columns:minmax(0,1fr) 1px auto;align-items:center;gap:8px 16px;min-width:0;padding:12px 16px;border-bottom:1px solid var(--compose-line)}
.compose-engine{display:flex;min-width:0;align-items:center;gap:10px}
.compose-engine>i{width:10px;height:10px;flex:none;border:1px solid var(--compose-ink);border-radius:50%;background:var(--compose-warn)}
.compose-engine-bar.ready .compose-engine>i{background:var(--compose-ok)}
.compose-engine b{flex:none;font-size:var(--fs-12);color:var(--compose-warn);white-space:nowrap}
.compose-engine-bar.ready .compose-engine b{color:var(--compose-ok)}
.compose-engine small{min-width:0;overflow:hidden;font-size:var(--fs-10);color:var(--compose-muted);text-overflow:ellipsis;white-space:nowrap}
/* 状态栏和日志行里的按钮小一号字，但不改高度：同一行的高度只有 --compose-control 一个数。 */
.compose-engine .compose-btn,.compose-logs-head .compose-btn{font-size:var(--fs-11)}
.compose-bar-divider{align-self:stretch;width:1px;flex:none;background:var(--compose-line)}
/* 权限这一组平常也是一行：只有密码框允许被压缩（它 flex-shrink 且有 min-width），
   压到实在放不下才换行 —— 换行只发生在 ≤900 的媒体查询里。 */
.compose-access{display:flex;min-width:0;flex-wrap:nowrap;align-items:center;gap:10px}
/* margin:0 是必须的：style.css 顶部有一条全局 label{margin-top:17px;display:flex;
   flex-direction:column}，它给的是「一列表单」的排版。这一行里的两个 label 要是留着
   那 17px，整条状态栏就会高出一截，控件也全被压低 —— 横平竖直就是被它破坏的。 */
.compose-access-toggle{display:flex;flex:none;flex-direction:row;align-items:center;gap:9px;margin:0;cursor:pointer}
.compose-access-toggle>span{display:flex;flex-direction:column;gap:1px}
.compose-access-toggle b{font-size:var(--fs-11);white-space:nowrap}
.compose-access-toggle small{font-size:var(--fs-9);font-weight:400;color:var(--compose-muted);white-space:nowrap}
.compose-access-toggle input{position:relative;appearance:none;flex:none;width:34px;height:20px;padding:0;border:1px solid var(--compose-line);border-radius:20px;background:var(--compose-soft);cursor:pointer}
.compose-access-toggle input::after{content:'';position:absolute;top:3px;left:3px;width:12px;height:12px;border-radius:50%;background:var(--compose-muted);transition:transform .15s}
.compose-access-toggle input:checked{border-color:var(--compose-ok-line);background:var(--compose-ok-bg)}
.compose-access-toggle input:checked::after{background:var(--compose-ok);transform:translateX(14px)}
/* 状态用小药丸，不再是一截悬空的灰字：定高 24px，在 34px 的一行里居中。 */
.compose-access-badge{display:inline-flex;height:24px;align-items:center;padding:0 8px;border:1px solid var(--compose-line);border-radius:999px;background:var(--compose-soft);color:var(--compose-muted);font-size:var(--fs-9);white-space:nowrap}
.compose-access-badge.on{border-color:var(--compose-ok-line);background:var(--compose-ok-bg);color:var(--compose-ok)}
/* 密码是 sudo 的补充设置，就接在开关后面（见模板注释）：能缩但不撑走别的。 */
.compose-access-password{display:flex;min-width:0;flex:0 1 240px;flex-direction:row;align-items:center;gap:8px;margin:0;font-size:var(--fs-10)}
.compose-access-password span{flex:none;color:var(--compose-muted);white-space:nowrap}
/* 输入框与下拉框共用一套：纸底、一圈细边、3px 圆角，聚焦才亮一道青边 —— 和别的工具页
   里的表单是同一副长相。高度统一到 --compose-control，和同一行的按钮齐平。
   下拉框的箭头与右侧 32px 内边距来自全局 selectMenu.css（那条带 !important），不必重写。 */
.compose-access-password input,
.compose-name input,
.compose-logs-head select{box-sizing:border-box;min-width:0;min-height:var(--compose-control);padding:7px 10px;border:1px solid var(--compose-line);border-radius:3px;background-color:var(--compose-field);color:var(--compose-text);font:inherit;font-size:var(--fs-12);transition:border-color .15s,box-shadow .15s}
.compose-access-password input:focus,
.compose-name input:focus,
.compose-logs-head select:focus{outline:0;border-color:#356c76;box-shadow:0 0 0 3px #83d9e766}
.compose-access-password input::placeholder,.compose-name input::placeholder{color:var(--compose-muted);opacity:.85}
.compose-access-password input{flex:1 1 auto}
/* 下拉框另外还要压过全局 selectMenu.css 里写死的 36px（那条选择器带 #app，是个 ID），
   否则它比同一行的「查看日志」高 2px —— 差这 2px 就是不对齐。 */
#app .compose-logs-head select{min-height:var(--compose-control)}

/* 页面上除了项目卡片之外，按钮都带 .compose-btn —— 用一个类而不是 button 元素选择器：
   .compose-page button（0,2,1）会压过 .compose-project（0,2,0），把「长得像按钮、其实是卡片」
   的项目行一起改掉。.primary / .accent / .danger 是在这个类上再加一层，靠多出一段类的
   特异性压过 :hover 的基础底色，所以每条 hover 都要自己写一遍底色（不能只写阴影）。 */
.compose-page .compose-btn{font:inherit;font-size:var(--fs-12);flex:none;min-height:34px;padding:7px 12px;border:1px solid var(--compose-ink);border-radius:3px;background:var(--compose-paper);color:var(--compose-text);white-space:nowrap;cursor:pointer}
.compose-page .compose-btn:not(:disabled):hover{background:var(--compose-active);border-color:var(--compose-ink)}
.compose-page .compose-btn:disabled{opacity:1;background:var(--compose-soft);border-color:var(--compose-line);color:var(--compose-muted);box-shadow:none;cursor:not-allowed}
.compose-page .compose-btn.primary{border:2px solid var(--compose-ink);background:var(--compose-accent);color:var(--compose-on-accent);box-shadow:var(--compose-shadow-sm);font-weight:750}
.compose-page .compose-btn.primary:not(:disabled):hover{background:#ffce36}
.compose-page .compose-btn.accent{border-color:var(--compose-ink);background:var(--compose-cyan);color:var(--compose-on-accent);font-size:var(--fs-11);font-weight:700}
.compose-page .compose-btn.accent:not(:disabled):hover{background:#5fcede}
.compose-page .compose-btn.danger{border-color:var(--compose-bad-line);background:var(--compose-bad-bg);color:var(--compose-bad)}
.compose-page .compose-btn.danger:not(:disabled):hover{background:var(--compose-bad-bg);box-shadow:var(--compose-shadow-sm)}
.compose-library-tools input{display:none}

/* ---------- 两栏：左「服务收藏」，右「运行与日志」 ---------- */
/* 每一栏自己滚动，整页不滚：整页高度由 fitWorkspace 按视口现算。 */
.compose-layout{display:grid;grid-template-columns:clamp(250px,16vw,310px) minmax(0,1fr);height:var(--compose-height);min-height:0;overflow:hidden;background:var(--compose-paper)}
.compose-library{display:flex;min-width:0;min-height:0;flex-direction:column;border-right:1px solid var(--compose-line)}
.compose-runtime{display:flex;min-width:0;min-height:0;flex-direction:column;background:var(--compose-soft)}

/* 两个栏头共用一套：编号 + 标题 + 一句状态，右侧是这一栏的操作。 */
.compose-library-head,.compose-runtime-head{display:flex;flex:none;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:10px 14px;padding:12px 16px;border-bottom:1px solid var(--compose-line);background:var(--compose-tint)}
.compose-pane-title{display:flex;min-width:0;align-items:center;gap:9px}
.compose-pane-title b{min-width:0;overflow:hidden;font-size:var(--fs-13);text-overflow:ellipsis;white-space:nowrap}
.compose-pane-title small{min-width:0;overflow:hidden;font-size:var(--fs-10);color:var(--compose-muted);text-overflow:ellipsis;white-space:nowrap}
.compose-step{display:grid;flex:none;place-items:center;width:24px;height:24px;border:1px solid var(--compose-ink);border-radius:2px;background:var(--compose-accent);color:var(--compose-on-accent);font-size:var(--fs-10);font-weight:800}
.compose-library-tools,.compose-runtime-tools{display:flex;min-width:0;flex-wrap:wrap;align-items:center;gap:8px}
.compose-actions,.compose-file-actions{display:flex;flex-wrap:wrap;gap:7px}
/* 文件操作那两个字小一号：与容器操作同一行，但要让眼睛分得出是两组。
   选择器得带上 .compose-btn，否则被上面基础规则的同名属性压住（它更具体）。 */
.compose-page .compose-file-actions .compose-btn{font-size:var(--fs-11)}
/* 容器操作与文件操作之间的那道细线：两组按钮不是一回事。 */
.compose-tools-split{align-self:stretch;width:1px;min-height:22px;background:var(--compose-line)}

/* 一个够大、但不会把整栏占掉的落点：110px 上下，文案居中，始终留在栏顶不随列表滚走。 */
.compose-import{display:grid;flex:none;min-height:110px;place-items:center;align-content:center;gap:7px;margin:14px 16px;padding:14px 12px;border:2px dashed var(--compose-note-line);border-radius:3px;background:var(--compose-note);text-align:center}
.compose-import b{font-size:var(--fs-12);color:var(--compose-text)}
.compose-import small{max-width:220px;font-size:var(--fs-10);line-height:1.75;color:var(--compose-muted)}
.compose-import.is-over{border-style:solid;border-color:var(--compose-ok-line);background:var(--compose-ok-bg)}
.compose-import.is-over b{color:var(--compose-ok)}
.compose-library-list{display:flex;min-height:0;flex:1;flex-direction:column;gap:8px;overflow:auto;padding:0 16px 16px}
.compose-project{display:flex;width:100%;min-height:52px;align-items:center;justify-content:space-between;gap:9px;padding:10px 12px;border:1px solid var(--compose-line);border-radius:3px;background:var(--compose-paper);color:var(--compose-text);text-align:left;cursor:pointer}
/* 选中态叫 is-active 而不是 active：全局有一条 button.active 的规则（亮色见
   design-system.css:43，暗色见 color-theme.css:13），那是给视图切换那种按钮用的黄底，
   落到这排项目卡片上就成了「选中 = 一整条黄」。换个类名，两边都不用互相顶。 */
.compose-project:hover,.compose-project.is-active{border-color:var(--compose-ink);background:var(--compose-active)}
.compose-project.is-active{box-shadow:var(--compose-shadow-sm)}
.compose-project span{display:flex;min-width:0;flex-direction:column;gap:3px}
.compose-project b{overflow:hidden;font-size:var(--fs-12);text-overflow:ellipsis;white-space:nowrap}
.compose-project small{overflow:hidden;font-size:var(--fs-9);color:var(--compose-muted);text-overflow:ellipsis;white-space:nowrap}
.compose-project>i{display:grid;min-width:22px;height:22px;flex:none;place-items:center;border:1px solid var(--compose-line);border-radius:2px;background:var(--compose-soft);color:var(--compose-muted);font-size:var(--fs-10);font-style:normal;font-variant-numeric:tabular-nums}
.compose-project.is-active>i{border-color:var(--compose-ink);background:var(--compose-accent);color:var(--compose-on-accent);font-weight:700}

/* 左栏空态是一句提示；右栏空态是整块留白，给一个符号才不像出了错。 */
.compose-empty{margin:0;padding:18px 14px;border:1px dashed var(--compose-note-line);border-radius:3px;background:var(--compose-note);color:var(--compose-muted);font-size:var(--fs-11);line-height:1.75;text-align:center}
.compose-empty.runtime{display:flex;flex:none;min-height:180px;flex-direction:column;align-items:center;justify-content:center;gap:12px;margin:14px 16px 0;padding:24px}
.compose-empty.runtime b{font-size:var(--fs-15);color:var(--compose-text)}
.compose-empty.runtime p{max-width:320px;line-height:1.8;color:var(--compose-muted)}
.compose-empty-symbol{padding:10px 14px;border:2px solid var(--compose-ink);border-radius:3px;background:var(--compose-cyan);color:var(--compose-on-accent);font:800 var(--fs-15)/1 ui-monospace,SFMono-Regular,Menlo,monospace;box-shadow:var(--compose-shadow)}

.compose-services{display:flex;flex:none;max-height:200px;flex-direction:column;overflow:auto;margin:14px 16px 0;border:1px solid var(--compose-line);border-radius:3px;background:var(--compose-paper)}
.compose-services article{display:flex;align-items:center;gap:9px;padding:10px 12px;border-bottom:1px solid var(--compose-line)}
.compose-services article:last-child{border-bottom:0}
.service-dot{width:9px;height:9px;flex:none;border:1px solid var(--compose-ink);border-radius:50%;background:var(--compose-muted)}
.service-dot.running{background:var(--compose-ok)}
.service-dot.stopped{background:var(--compose-bad)}
.compose-services div{display:flex;min-width:0;flex:1;flex-direction:column;gap:2px}
.compose-services b{font-size:var(--fs-11)}
.compose-services small{overflow:hidden;font-size:var(--fs-9);color:var(--compose-muted);text-overflow:ellipsis;white-space:nowrap}
.compose-services em{flex:none;padding:2px 7px;border:1px solid var(--compose-line);border-radius:2px;background:var(--compose-soft);color:var(--compose-muted);font-size:var(--fs-10);font-style:normal}
.compose-services em.running{border-color:var(--compose-ok-line);background:var(--compose-ok-bg);color:var(--compose-ok)}
.compose-services em.stopped{border-color:var(--compose-bad-line);background:var(--compose-bad-bg);color:var(--compose-bad)}

.compose-logs-head{display:flex;flex:none;flex-wrap:wrap;align-items:center;gap:10px;margin:14px 16px 0;padding:10px 12px;border:1px solid var(--compose-line);border-radius:3px;background:var(--compose-paper)}
.compose-logs-head b{font-size:var(--fs-13)}
.compose-logs-head select{max-width:200px;margin-left:auto;font-size:var(--fs-11);cursor:pointer}
/* 日志是一段等宽输出：左边留一道青色竖条，和别的工具页里的终端块一致。 */
.compose-logs{flex:1;min-height:90px;margin:10px 16px 16px;overflow:auto;padding:14px 16px;border:1px solid var(--compose-line);border-left:4px solid var(--compose-cyan);border-radius:3px;background:var(--compose-code-bg);color:var(--compose-code-text);font:var(--fs-12)/1.8 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap}

/* 新建/编辑弹窗。遮罩与过渡用全局的 .modal-mask / .modal-*（style.css），卡片沿用本页的墨线 + 硬阴影。
   四行：标题 / 项目名称 / yaml / 按钮。高度写死不随内容变，minmax(0,1fr) 那一行把剩下的空间
   全给编辑框 —— 这是这个弹窗唯一值得大的地方。 */
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

.compose-footnote{margin:14px 0 0;color:var(--compose-muted);font-size:var(--fs-11);line-height:1.8}

/* 暗色下 color-theme.css:8 会把 .ui-shell 里的 b/small/p 一律拉成 color:inherit，
   正文倒是对的，语义色与次要文字就全没了。下面用 html[data-theme=dark] 前缀把特异性
   抬到那条规则之上（而不是再堆一个 !important 去和它打），把颜色要回来。
   —— 前缀要写成普通选择器，**不能写 `:global(html[data-theme=dark]) .foo`**：
   Vue 的作用域编译会把 `:global(...)` 之后的部分整段吞掉，编译结果只剩
   `html[data-theme=dark]`，规则变成往 <html> 上刷属性（DbxPage.vue:51 那一整块暗色
   就是这么失效的）。作用域属性本来就只加在最后一段上，普通前缀不会被打上。 */
html[data-theme=dark] .compose-engine b{color:var(--compose-warn)}
html[data-theme=dark] .compose-engine-bar.ready .compose-engine b{color:var(--compose-ok)}
html[data-theme=dark] .compose-import.is-over b{color:var(--compose-ok)}
html[data-theme=dark] .compose-engine small,
html[data-theme=dark] .compose-pane-title small,
html[data-theme=dark] .compose-import small,
html[data-theme=dark] .compose-project small,
html[data-theme=dark] .compose-services small,
html[data-theme=dark] .compose-access-toggle small,
html[data-theme=dark] .compose-access-password span,
html[data-theme=dark] .compose-empty.runtime p,
html[data-theme=dark] .compose-footnote{color:var(--compose-muted)}
/* 暗色下 color-theme.css:11 给 .ui-shell 里每个 button（项目行也是 button）都刷了一层
   !important 的底色，选中的那张卡片得把它顶回来 —— 这里用 !important 是没办法：
   对面是 !important，比的是重要性而不是特异性。:hover 那条一起写上，否则鼠标停在
   选中的行上时又会被全局的 hover 底色盖掉。 */
html[data-theme=dark] .compose-project.is-active,
html[data-theme=dark] .compose-project.is-active:hover:not(:disabled){background:var(--compose-active)!important;border-color:var(--compose-cyan)!important;color:var(--compose-text)!important}
/* 编辑弹窗 Teleport 到了 body，不在 .ui-shell 里，拿不到全局那条「暗色下聚焦用青色描边」
   的规则（color-theme.css:14），这里用同一套填上，免得暗色里聚焦圈是暗青色、看不见。 */
html[data-theme=dark] .compose-name input:focus{border-color:var(--compose-cyan);box-shadow:0 0 0 3px #83d9e759}

@media(max-width:1100px){
 .compose-layout{grid-template-columns:clamp(220px,26vw,260px) minmax(0,1fr)}
 /* 栏头与卡片的留白收到 12px 时，状态栏也跟着收：三条竖线始终是同一条。 */
 .compose-engine-bar{padding:10px 12px}
 .compose-library-head,.compose-runtime-head{padding:10px 12px}
 .compose-import{margin:12px}
 .compose-library-list{padding:0 12px 12px}
 .compose-services,.compose-empty.runtime,.compose-logs-head{margin-left:12px;margin-right:12px}
 .compose-logs{margin:10px 12px 12px}
}
@media(max-width:900px){
 .compose-local{display:none}
 /* 到这儿权限那一组才允许换行（放不下时「应用设置」落到第二行），竖分隔线跨两行会显得
   断开，直接去掉。 */
 .compose-engine-bar{grid-template-columns:minmax(0,1fr) auto}
 .compose-bar-divider{display:none}
 .compose-access{flex-wrap:wrap;justify-content:flex-end}
}
@media(max-width:700px){
 /* 状态栏整条落到标题下面，一行一行排，不再有跨行的竖分隔线。 */
 .compose-engine-bar{display:flex;flex-direction:column;align-items:stretch;gap:9px}
 .compose-engine{flex-wrap:wrap}
 .compose-bar-divider{display:none}
 .compose-access-badge{display:none}
 /* 窄屏不再把两栏钉在视口高度上：整页滚动，右栏给一个自己的高度。 */
 .compose-layout{display:block;height:auto;overflow:visible}
 .compose-library{border-right:0;border-bottom:1px solid var(--compose-line)}
 .compose-library-list{max-height:260px}
 .compose-runtime{min-height:520px}
 .compose-logs{min-height:220px}
 /* 窄屏把两组操作铺满整行。选择器要写成 .compose-page ... .compose-btn：基础规则里
    .compose-btn{flex:none} 比 .compose-actions button 更具体，直接写后者会被它压住。 */
 .compose-actions,.compose-file-actions{width:100%}
 .compose-page .compose-actions .compose-btn,.compose-page .compose-file-actions .compose-btn{flex:1 1 68px}
 .compose-tools-split{display:none}
 /* 弹窗回到自然高度：卡片自己不带滚动条，写死高度会把编辑框压到 min-height 以下。 */
 .compose-editor-card{width:100%;height:auto;max-height:92vh}
 /* 卡片在这里是自然高度，1fr 那行没有可分的空间 —— 给编辑器一个下限，它自己再撑满。 */
 .compose-source{min-height:240px}
 .compose-editor-card-actions button{flex:1;min-width:100px}
}
</style>
