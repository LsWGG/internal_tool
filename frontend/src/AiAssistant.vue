<script setup>
import {computed,nextTick,onMounted,onBeforeUnmount,reactive,ref,watch} from 'vue'
import axios from 'axios'
import {inspectPage,operatePage,describeControl} from './aiPageBridge'
import {toolGroups} from './toolCatalog'
import {renderMarkdown} from './markdown'
import {readHistory,writeHistory,snapshotMessages} from './aiHistory'

const props=defineProps({applyConfiguration:{type:Function,required:true},currentView:{type:String,default:'home'},context:{type:Object,default:()=>({})},pageName:{type:String,default:''}})
const emit=defineEmits(['navigate','apply-config'])
const open=ref(false),busy=ref(false),input=ref(''),aiStatus=ref('checking'),messages=ref([]),scrollEl=ref()
const sessions=ref([]),activeId=ref(crypto.randomUUID()),historyOpen=ref(false),storageError=ref('')
try{const saved=readHistory(localStorage);sessions.value=saved.sessions;const active=saved.sessions.find(s=>s.id===saved.activeId);if(active){activeId.value=active.id;messages.value=active.messages}}
catch{storageError.value='历史记录暂时无法读取；当前聊天仍可使用。'}
function saveHistory(){
  if(messages.value.length){
    const existing=sessions.value.find(s=>s.id===activeId.value)
    const session={id:activeId.value,title:existing?.title||messages.value.find(m=>m.role==='user')?.content.slice(0,36)||'新对话',updatedAt:Date.now(),messages:snapshotMessages(messages.value)}
    sessions.value=[session,...sessions.value.filter(s=>s.id!==activeId.value)].slice(0,20)
  }
  try{sessions.value=writeHistory(localStorage,sessions.value,activeId.value);storageError.value=''}catch{storageError.value='本地存储不可用或空间不足，聊天记录暂未保存。'}
}
function restoreConversation(session){if(locked.value)return;saveHistory();activeId.value=session.id;messages.value=snapshotMessages(session.messages);input.value='';historyOpen.value=false;copied.value=null;saveHistory();scrollBottom()}
function deleteConversation(id){if(locked.value)return;sessions.value=sessions.value.filter(s=>s.id!==id);if(activeId.value===id){messages.value=[];activeId.value=crypto.randomUUID();input.value=''}saveHistory()}
watch(messages,saveHistory,{deep:true,flush:'post'})
const liveActions=ref([])
let chatController
const executing=computed(()=>messages.value.some(message=>message.executing))
const locked=computed(()=>busy.value||executing.value)
const copied=ref(null)
const configured=computed(()=>aiStatus.value==='ready')
const tools=toolGroups.flatMap(group=>group.tools)
const currentTool=computed(()=>tools.find(tool=>tool.id===props.currentView))
// pageName 是外链工具页传进来的：它不在 toolGroups 里，光靠 currentView 找不到名字。
const pageName=computed(()=>props.pageName||currentTool.value?.name||'工具门户')
const quickPrompts=computed(()=>({
  home:['我应该使用哪个工具？','帮我规划一个数据处理流程','这个系统能完成什么？'],
  map:['根据当前配置检查下载方案','如何选择 PNG 和 GeoTIFF？','帮我设置 DEM 下载参数'],
  crawler:['检查当前采集配置','帮我选择合适的数据源模式','分析最近一次失败原因'],
  pdf:['检查当前 PDF 转换配置','应用型网页怎样避免排版错位？','推荐结果命名模板'],
  'pdf-toolbox':['扫描 PDF 如何转成可编辑 Word？','如何添加清晰的文字水印？','帮我选择合并、拆分或提取页面'],
  json:['帮我写 JSONPath 查询表达式','如何定位两份 JSON 的差异？','JSON 转 CSV 有哪些注意事项？'],
  clean:['帮我设计按列过滤规则','如何处理重复行和缺失值？','如何核对清洗前后差异？'],
  database:['根据需求帮我写 SQL','导入前需要检查什么？','如何安全迁移数据？'],
}[props.currentView]||['介绍当前工具的使用步骤','检查我需要准备哪些数据','有哪些常见错误？']))

// 渲染器全站共用一份（见 markdown.js）：报告与回复的表格/列表从此走同一套实现，
// 不再各写一份「看起来差不多」的解析。

async function scrollBottom(){await nextTick();if(scrollEl.value)scrollEl.value.scrollTop=scrollEl.value.scrollHeight}
async function send(text=input.value){
  const content=String(text||'').trim();if(!content||locked.value||!configured.value)return
  messages.value.push({role:'user',content});input.value='';liveActions.value=[];busy.value=true;await scrollBottom()
  chatController=new AbortController()
  try{
    const history=messages.value.filter(message=>!message.error&&!message.cancelled).slice(-10).map(message=>{
      let content=message.content
      if(message.role==='assistant'&&(message.action||message.actionResult)){
        content+=`\n\n[Agent 上下文：${JSON.stringify({action:message.action||null,status:message.superseded?'superseded':message.actionDone?'completed':message.actionError?'failed':'pending',result:message.actionResult||null,error:message.actionError||null})}]`
      }
      return {role:message.role,content}
    })
    const responseStream=await fetch('/api/ai/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_view:props.currentView,context:{...props.context,page_controls:inspectPage()},messages:history}),signal:chatController.signal})
    if(!responseStream.ok)throw new Error('助手服务连接失败')
    const reader=responseStream.body.getReader(),decoder=new TextDecoder();let buffer='',data
    function consume(line){if(!line.trim())return;const event=JSON.parse(line);if(event.type==='activity'){liveActions.value.push({...event,time:new Date().toLocaleTimeString()});scrollBottom()}else if(event.type==='result')data=event.data;else if(event.type==='error')throw new Error(event.message)}
    try{while(true){const chunk=await reader.read();if(chunk.done)break;buffer+=decoder.decode(chunk.value,{stream:true});const lines=buffer.split('\n');buffer=lines.pop();lines.forEach(consume)}buffer+=decoder.decode();if(buffer.trim())consume(buffer)}finally{await reader.cancel().catch(()=>{})}
    if(!data)throw new Error('连接已结束，未收到完整结果，请重试')
    const response=reactive({role:'assistant',content:data.answer,...data,agentSteps:buildAgentSteps(data),activities:[...liveActions.value]})
    if(response.action||response.config_patch){
      messages.value.filter(item=>item.role==='assistant'&&(item.action||item.config_patch)&&!item.actionDone).forEach(item=>{item.superseded=true})
    }
    if(response.action?.operation==='page')response.pageControlLabel=describeControl(response.action.parameters.control_id)
    messages.value.push(response)
    if(response.config_patch){
      await props.applyConfiguration(response.config_patch);response.applied=true
      response.activities.push({label:'填写页面',detail:'已发送页面配置：'+(response.config_patch.summary||response.config_patch.target),time:new Date().toLocaleTimeString()})
    }else if(response.navigate_to){emit('navigate',response.navigate_to)}
    if(response.action&&!response.action.requires_confirmation){busy.value=false;await executeMessage(response)}
  }catch(error){const cancelled=error.code==='ERR_CANCELED'||error.name==='AbortError';messages.value.push({role:'assistant',activities:[...liveActions.value],content:cancelled?'已停止等待回复。':error.response?.data?.detail||error.message||(error.code==='ECONNABORTED'?'模型响应超时，可重试本次提问。':'AI 助手暂时不可用，请稍后重试。'),error:!cancelled,cancelled,retryPrompt:content})}
  finally{chatController=null;busy.value=false;await scrollBottom()}
}
function buildAgentSteps(data){
  const manifest=tools.find(item=>item.id===(data.action?.tool_id||data.navigate_to))
  const steps=[]
  for(const observation of data.observations||[])steps.push({label:observation.result?.error?'工具反馈':'读取结果',detail:observation.result?.error||observation.result?.message||'已获取真实数据',status:'done'})
  if(manifest)steps.push({label:'选择能力',detail:manifest.name,status:'done'})
  if(data.action||data.config_patch)steps.push({label:'校验参数',detail:'已通过能力注册表校验',status:'done'})
  if(data.action?.requires_confirmation)steps.push({label:'等待确认',detail:'确认后才会创建、修改或删除数据',status:'waiting'})
  else if(data.action)steps.push({label:'执行工具',detail:'准备读取真实结果',status:'waiting'})
  return steps
}
function planDetails(message){
  const p=message.action?.parameters||message.config_patch?.values||{}
  const sourceNames={generic:'独立网页',news:'分页列表 / 详情发现',twitter:'推特 / X',tiktok:'TikTok',telegram:'Telegram',youtube:'YouTube',wechat:'微信公众号'}
  const formatNames={xlsx:'Excel',csv:'CSV',json:'JSON',jsonl:'JSONL',postgresql:'PostgreSQL',png:'PNG',tif:'GeoTIFF'}
  const details=[]
  if(message.action?.operation==='page'){details.push(['页面动作',p.mode==='fill'?'填写字段':'点击按钮'],['页面控件',message.pageControlLabel||p.control_id]);if(p.value!==undefined)details.push(['填写内容',p.value])}
  if(p.source)details.push(['采集流程',sourceNames[p.source]||p.source])
  if(p.urls?.length)details.push(['入口地址',p.urls.length===1?p.urls[0]:`${p.urls.length} 个地址`])
  if(p.keyword)details.push(['检索目标',p.keyword])
  if(p.account_name)details.push(['账号目标',p.account_name])
  if(p.max_items!==undefined)details.push(['采集数量',p.max_items===-1?'不限量增量':`${p.max_items} 条`])
  if(p.fields?.length)details.push(['保存字段',p.fields.join('、')])
  if(p.output_format)details.push(['输出格式',formatNames[p.output_format]||p.output_format.toUpperCase()])
  for(const [key,label] of Object.entries({region_query:'下载范围',data_type:'数据类型',tile_source:'地图源',zoom_min:'最小层级',zoom_max:'最大层级',filename_template:'命名模板',task_id:'任务 ID',action:'任务操作'})){
    if(p[key]!==undefined)details.push([label,String(p[key])])
  }
  return details
}
function useSuggestion(text){input.value=text;send(text)}
function navigate(id){emit('navigate',id)}
async function applyPatch(patch,message){if(locked.value||message.restored||message.superseded||message.applied)return;try{await props.applyConfiguration(patch);message.applied=true;messages.value.push({role:'assistant',content:'页面配置已更新，请检查结果。'})}catch(error){messages.value.push({role:'assistant',content:error.message||'页面配置未成功应用',error:true})}scrollBottom()}
async function executeMessage(message){
  if(message.restored||message.executing||message.actionDone||message.superseded||executing.value)return
  message.executing=true;message.actionError=null
  try{
    if(message.config_patch&&!message.applied){await props.applyConfiguration(message.config_patch);message.applied=true}
    let result
    if(message.action.operation==='page'){
      if(message.action.tool_id!==props.currentView)throw new Error('工具页面已切换，请重新提问。')
      result={message:operatePage(message.action.parameters),tool_id:props.currentView}
    }else{result=(await axios.post('/api/ai/actions/execute',{action:message.action,confirmed:true})).data}
    message.actionDone=true;message.applied=true
    message.actionResult=result
    const step=message.agentSteps?.find(item=>item.status==='waiting');if(step){step.label='执行完成';step.detail='已收到服务器执行结果';step.status='done'}
    if(result.tool_id)emit('navigate',result.tool_id)
    messages.value.push({role:'assistant',content:result.message||'操作已完成。',download_url:result.download_url,suggestions:message.action.operation==='page'?['继续，根据当前页面执行下一步']:result.task?.id?[`查询任务 ${result.task.id} 的进度`,`获取任务 ${result.task.id} 的处理结果`]:[]})
  }catch(error){message.actionError=error.response?.data?.detail||error.message||'执行失败';const step=message.agentSteps?.find(item=>item.status==='waiting');if(step){step.label='执行失败';step.detail='请核对错误信息后重试'}messages.value.push({role:'assistant',content:error.response?.data?.detail||error.message||'任务执行失败，请检查配置后重试。',error:true})}
  finally{message.executing=false;await scrollBottom()}
}
function onKeydown(event){if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing&&event.keyCode!==229){event.preventDefault();send()}}
async function checkCapabilities(){
  aiStatus.value='checking'
  try{aiStatus.value=(await axios.get('/api/ai/capabilities',{timeout:5000})).data.configured?'ready':'unconfigured'}
  catch{aiStatus.value='backend-offline'}
}
function newConversation(){if(locked.value)return;saveHistory();activeId.value=crypto.randomUUID();historyOpen.value=false;messages.value=[];input.value='';copied.value=null;checkCapabilities()}
async function copyAnswer(message){try{await navigator.clipboard.writeText(message.content);copied.value=message}catch{copied.value=null}}
watch(open,value=>{if(value)checkCapabilities()})
onBeforeUnmount(()=>chatController?.abort())
onMounted(checkCapabilities)
</script>

<template>
  <Teleport to="body">
    <button class="ai-native-launcher" :class="{active:open}" aria-label="打开 AI 助手" @click="open=!open">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2.8 14 8l5.2 2-5.2 2-2 5.2-2-5.2-5.2-2L10 8l2-5.2Zm6.2 12.4.9 2.3 2.3.9-2.3.9-.9 2.3-.9-2.3-2.3-.9 2.3-.9.9-2.3Z"/></svg>
      <span>AI</span>
    </button>
    <Transition name="ai-drawer">
      <aside v-if="open" class="ai-native-panel" aria-label="系统 AI 助手">
        <header><div class="ai-native-mark"><svg viewBox="0 0 24 24"><path d="M12 2.8 14 8l5.2 2-5.2 2-2 5.2-2-5.2-5.2-2L10 8l2-5.2Z"/></svg></div><div><b>AI 工作助手</b><small>正在理解：{{pageName}}</small></div><button class="ai-new-chat" :disabled="locked" :aria-expanded="historyOpen" @click="historyOpen=!historyOpen">历史</button><button class="ai-new-chat" :disabled="locked||!messages.length" title="保存当前对话，开始新的任务" @click="newConversation">新对话</button><button aria-label="关闭" @click="open=false">×</button></header>
        <section v-if="historyOpen" class="ai-history"><small>最近 20 个会话 · 仅保存在当前浏览器</small><p v-if="!sessions.length">暂无历史聊天记录</p><div v-for="session in sessions" :key="session.id" :class="{active:session.id===activeId}"><button class="ai-history-open" :disabled="locked" @click="restoreConversation(session)"><b>{{session.title}}</b><small>{{new Date(session.updatedAt).toLocaleString()}} · {{session.messages.length}} 条</small></button><button :disabled="locked" :aria-label="`删除会话：${session.title}`" @click="deleteConversation(session.id)">删除</button></div></section>
        <div v-if="storageError" class="ai-history-error" role="status">{{storageError}}</div>
        <div ref="scrollEl" class="ai-native-body">
          <section v-if="!messages.length" class="ai-native-welcome"><span>AI NATIVE</span><h2>现在想完成什么？</h2><p>我会结合当前页面给出步骤、选择工具或生成可确认的配置。</p><div><button v-for="item in quickPrompts" :key="item" :disabled="locked||!configured" @click="send(item)">{{item}}<i>↗</i></button></div></section>
          <template v-for="(message,index) in messages" :key="index">
            <div class="ai-native-message" :class="[message.role,{error:message.error}]"><span>{{message.role==='assistant'?'AI':'你'}}</span><div v-if="message.role==='assistant'" class="ai-native-markdown" v-html="renderMarkdown(message.content)"></div><p v-else>{{message.content}}</p></div>
            <details v-if="message.activities?.length" class="ai-live-actions"><summary>本次动作 · {{message.activities.length}} 条</summary><article v-for="(event,index) in message.activities" :key="index"><header><b>{{event.label}}</b><small>{{event.time}}</small></header><p>{{event.detail}}</p><pre v-if="event.action">{{JSON.stringify(event.action,null,2)}}</pre></article></details>
            <div v-if="message.role==='assistant'&&message.agentSteps?.length&&!message.error" class="ai-agent-trace">
              <div v-for="step in message.agentSteps" :key="step.label" :class="step.status"><i>{{step.status==='waiting'?'·':'✓'}}</i><span><b>{{step.label}}</b><small>{{step.detail}}</small></span></div>
            </div>
            <div v-if="message.role==='assistant'" class="ai-message-tools"><button v-if="!message.error&&!message.cancelled" @click="copyAnswer(message)">{{copied===message?'已复制':'复制答复'}}</button><button v-if="message.retryPrompt" :disabled="locked||!configured" @click="send(message.retryPrompt)">重新提问</button></div>
            <dl v-if="message.role==='assistant'&&planDetails(message).length" class="ai-agent-plan">
              <template v-for="item in planDetails(message)" :key="item[0]"><dt>{{item[0]}}</dt><dd>{{item[1]}}</dd></template>
            </dl>
            <div v-if="message.role==='assistant'&&(message.navigate_to||message.config_patch||message.action)" class="ai-native-actions">
              <button v-if="message.navigate_to" @click="navigate(message.navigate_to)">打开{{tools.find(item=>item.id===message.navigate_to)?.name||'建议工具'}}</button>
              <button v-if="message.config_patch&&!message.action&&!message.applied&&!message.superseded&&!message.restored" :disabled="locked" class="primary" @click="applyPatch(message.config_patch,message)">应用建议配置</button>
              <span v-if="message.restored&&!message.actionDone" class="ai-native-action-expired">历史方案，请重新提问后执行</span><span v-else-if="message.superseded" class="ai-native-action-expired">已由新方案替换</span>
              <button v-else-if="message.action&&!message.actionDone" class="primary" :disabled="locked" @click="executeMessage(message)">{{message.executing?'正在执行…':message.action.label}}</button>
              <span v-if="message.actionDone" class="ai-native-action-done">✓ 已完成</span>
            </div>
            <div v-if="message.role==='assistant'&&message.suggestions?.length" class="ai-native-followups"><button v-for="item in message.suggestions" :key="item" :disabled="locked||!configured" @click="useSuggestion(item)">{{item}}</button></div>
            <a v-if="message.download_url" class="ai-native-download" :href="message.download_url">下载任务结果</a>
          </template>
          <section v-if="busy" class="ai-live-actions ai-live-progress" aria-label="实时执行进展">
            <div class="ai-progress-top"><span><i aria-hidden="true"></i>正在处理 <small>{{liveActions.length}} 个动作</small></span><button @click="chatController?.abort()">停止等待</button></div>
            <div class="ai-action-timeline" role="log" aria-live="polite" aria-relevant="additions">
              <article v-for="(event,index) in liveActions" :key="index" :class="{'is-current':index===liveActions.length-1}"><header><b>{{event.label}}</b><small>{{event.time}}</small></header><p>{{event.detail}}</p><details v-if="event.action"><summary>调用详情</summary><pre>{{JSON.stringify(event.action,null,2)}}</pre></details></article>
            </div>
          </section>
        </div>
        <footer><div v-if="aiStatus==='checking'" class="ai-native-unconfigured info">正在检查 AI 服务…</div><div v-else-if="aiStatus==='backend-offline'" class="ai-native-unconfigured">后端服务未连接，请启动或重启服务。<button @click="checkCapabilities">重新检测</button></div><div v-else-if="aiStatus==='unconfigured'" class="ai-native-unconfigured">后端已连接，但尚未配置模型，请在系统设置 → 大模型配置中设置。<button @click="checkCapabilities">重新检测</button></div><div class="ai-native-input"><textarea v-model="input" rows="2" :disabled="executing||!configured" :placeholder="`询问关于“${pageName}”的问题…`" @keydown="onKeydown"></textarea><button :disabled="locked||!configured||!input.trim()" aria-label="发送" @click="send()">↑</button></div><small>读取与查询可直接完成，创建、删除和数据写入需要你确认。</small></footer>
      </aside>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ai-native-launcher{position:fixed;right:24px;bottom:22px;z-index:1300;display:flex;align-items:center;justify-content:center;gap:7px;width:calc(58px * var(--ui-scale));height:calc(42px * var(--ui-scale));border:1px solid #c8d8f5;border-radius:14px;color:#fff;background:linear-gradient(135deg,#426fe3,#7659e8);box-shadow:0 12px 32px #355dbf42;cursor:pointer;font:700 12px Inter,"PingFang SC",sans-serif;transition:.18s}.ai-native-launcher:hover{transform:translateY(-2px);box-shadow:0 16px 38px #355dbf52}.ai-native-launcher.active{opacity:0;pointer-events:none}.ai-native-launcher svg{width:19px;fill:currentColor}
.ai-native-panel{position:fixed;right:18px;bottom:18px;z-index:1301;display:flex;flex-direction:column;width:min(calc(410px * var(--ui-scale)),calc(100vw - 24px));height:min(calc(680px * var(--ui-scale)),calc(100dvh - 36px));overflow:hidden;border:1px solid #d6e0ef;border-radius:20px;color:#2b405d;background:#f8fafe;box-shadow:0 24px 70px #29466b33;font-family:Inter,"PingFang SC",sans-serif}.ai-native-panel>header{display:flex;align-items:center;gap:11px;flex:none;padding:15px 16px;border-bottom:1px solid #e0e7f1;background:#fff}.ai-native-mark{display:grid;place-items:center;width:35px;height:35px;border-radius:11px;color:#fff;background:linear-gradient(135deg,#416fe2,#7659e8)}.ai-native-mark svg{width:20px;fill:currentColor}.ai-native-panel header>div:nth-child(2){display:flex;min-width:0;flex:1;flex-direction:column}.ai-native-panel header b{font-size:var(--fs-13,13px)}.ai-native-panel header small{margin-top:3px;color:#8190a5;font-size:var(--fs-10,10px)}.ai-native-panel header>button{width:32px;height:32px;border:0;border-radius:9px;color:#71829a;background:transparent;font-size:var(--fs-22,22px);cursor:pointer}.ai-native-panel header>button:hover{background:#f0f3f8}
.ai-native-body{flex:1;min-height:0;padding:18px;overflow-y:auto;scrollbar-width:thin}.ai-native-welcome>span{color:#5d78bd;font-size:var(--fs-9,9px);font-weight:800;letter-spacing:1.5px}.ai-native-welcome h2{margin:8px 0 6px;font-size:var(--fs-21,21px);letter-spacing:-.4px}.ai-native-welcome p{color:#76879e;font-size:var(--fs-11,11px);line-height:1.7}.ai-native-welcome>div{display:grid;gap:8px;margin-top:20px}.ai-native-welcome button{display:flex;align-items:center;justify-content:space-between;padding:12px 13px;border:1px solid #dce5f1;border-radius:11px;color:#465d7b;background:#fff;text-align:left;font-size:var(--fs-11,11px);cursor:pointer}.ai-native-welcome button:hover{border-color:#abc2eb;color:#3d65c5;background:#f7faff}.ai-native-welcome i{font-style:normal;color:#8aa2ca}
.ai-native-message{display:grid;grid-template-columns:27px minmax(0,1fr);align-items:start;gap:9px;margin-bottom:15px}.ai-native-message>span{display:grid;place-items:center;width:27px;height:27px;border-radius:8px;color:#526d9c;background:#e8eef9;font-size:var(--fs-9,9px);font-weight:800}.ai-native-message.user>span{color:#fff;background:#5675c7}.ai-native-message p{margin:0;padding:10px 12px;border:1px solid #e0e7f0;border-radius:4px 12px 12px;color:#435872;background:#fff;font-size:var(--fs-11,11px);line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere}.ai-native-message.user p{border:0;border-radius:12px 4px 12px;color:#fff;background:#526fc0}.ai-native-message.error p{color:#a54752;background:#fff5f6;border-color:#f1dadd}.ai-native-actions{display:flex;gap:7px;margin:-7px 0 14px 36px}.ai-native-actions button{padding:7px 10px;border:1px solid #bdcce4;border-radius:8px;color:#4d6587;background:#fff;font-size:var(--fs-10,10px);cursor:pointer}.ai-native-actions button.primary{border-color:#4e70cc;color:#fff;background:#4e70cc}.ai-native-followups{display:flex;flex-wrap:wrap;gap:6px;margin:-5px 0 16px 36px}.ai-native-followups button{padding:5px 8px;border:0;border-radius:7px;color:#667b98;background:#edf2f9;font-size:var(--fs-9,9px);cursor:pointer}.ai-native-thinking{display:flex;align-items:center;gap:4px;margin:4px 0 15px 36px;color:#8391a4;font-size:var(--fs-10,10px)}.ai-native-thinking i{width:5px;height:5px;border-radius:50%;background:#6984c7;animation:ai-pulse 1s infinite alternate}.ai-native-thinking i:nth-child(2){animation-delay:.2s}.ai-native-thinking i:nth-child(3){animation-delay:.4s}.ai-native-thinking span{margin-left:5px}
.ai-native-panel>footer{flex:none;padding:12px 14px 13px;border-top:1px solid #dfe7f1;background:#fff}.ai-native-input{display:flex;align-items:end;gap:8px;padding:8px 8px 8px 11px;border:1px solid #cedbeb;border-radius:13px;background:#fbfcff;box-shadow:0 0 0 3px transparent;transition:.15s}.ai-native-input:focus-within{border-color:#829fdd;box-shadow:0 0 0 3px #5275cf15}.ai-native-input textarea{flex:1;min-height:38px;max-height:100px;padding:2px 0;border:0;outline:0;resize:none;color:#334b68;background:transparent;font:11px/1.6 inherit}.ai-native-input button{display:grid;place-items:center;width:31px;height:31px;border:0;border-radius:9px;color:#fff;background:#526fc9;font-size:var(--fs-17,17px);cursor:pointer}.ai-native-input button:disabled{opacity:.35;cursor:not-allowed}.ai-native-panel footer>small{display:block;margin-top:7px;color:#929fb0;font-size:var(--fs-8,8px);text-align:center}.ai-native-unconfigured{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;padding:8px;border-radius:8px;color:#9d4d55;background:#fff1f2;font-size:var(--fs-10,10px)}.ai-native-unconfigured.info{color:#557096;background:#eef4fd}.ai-native-unconfigured>button{padding:4px 7px;border:1px solid currentColor;border-radius:6px;color:inherit;background:transparent;font-size:var(--fs-9,9px);white-space:nowrap;cursor:pointer}.ai-drawer-enter-active,.ai-drawer-leave-active{transition:.2s}.ai-drawer-enter-from,.ai-drawer-leave-to{opacity:0;transform:translateY(12px) scale(.98)}
.ai-native-actions button:disabled{opacity:.55;cursor:wait}.ai-native-action-done{align-self:center;color:#2b8b67;font-size:var(--fs-10,10px);font-weight:700}
.ai-native-action-expired{align-self:center;color:#8b98aa;font-size:var(--fs-10,10px)}
.ai-native-download{display:inline-flex;margin:-7px 0 14px 36px;padding:7px 11px;border-radius:8px;color:#fff;background:#4e70cc;text-decoration:none;font-size:var(--fs-10,10px);font-weight:700}
.ai-agent-trace{display:grid;gap:6px;margin:-7px 0 14px 36px;padding:10px 11px;border:1px solid #dfe7f2;border-radius:11px;background:#f4f7fc}.ai-agent-trace>div{display:flex;align-items:center;gap:8px}.ai-agent-trace i{display:grid;place-items:center;width:17px;height:17px;border-radius:50%;color:#fff;background:#5d7acc;font-size:var(--fs-9,9px);font-style:normal}.ai-agent-trace .waiting i{color:#5471bd;background:#e3eafa}.ai-agent-trace span{display:flex;min-width:0;align-items:baseline;gap:7px}.ai-agent-trace b{color:#435a79;font-size:var(--fs-10,10px)}.ai-agent-trace small{overflow:hidden;color:#8593a7;font-size:var(--fs-9,9px);text-overflow:ellipsis;white-space:nowrap}
.ai-agent-running{margin:4px 0 15px 36px;padding:11px;border:1px solid #dce6f4;border-radius:11px;background:#fff}.ai-agent-running>span{display:block;margin-bottom:9px;color:#506990;font-size:var(--fs-9,9px);font-weight:800;letter-spacing:.5px}.ai-agent-running>div{display:flex;align-items:center;color:#7f8fa5;font-size:var(--fs-9,9px)}.ai-agent-running i{display:grid;place-items:center;width:17px;height:17px;margin-right:4px;border-radius:50%;color:#788ca9;background:#edf1f7;font-size:var(--fs-8,8px);font-style:normal}.ai-agent-running i.active{color:#fff;background:#5576d1;box-shadow:0 0 0 4px #5576d118}.ai-agent-running i.done{color:#fff;background:#62a087}.ai-agent-running b{flex:1;height:1px;margin:0 6px;background:#e0e7f0}.ai-agent-running small{display:block;margin-top:9px;color:#8997aa;font-size:var(--fs-9,9px)}
.ai-agent-plan{display:grid;grid-template-columns:58px minmax(0,1fr);gap:6px 9px;margin:-7px 0 14px 36px;padding:11px 12px;border:1px solid #dce5f2;border-radius:11px;background:#fff}.ai-agent-plan dt{color:#8795a8;font-size:var(--fs-9,9px)}.ai-agent-plan dd{min-width:0;margin:0;overflow:hidden;color:#415977;font-size:var(--fs-10,10px);font-weight:600;text-overflow:ellipsis;white-space:nowrap}
.ai-native-markdown{min-width:0;margin:0;padding:10px 12px;border:1px solid #e0e7f0;border-radius:4px 12px 12px;color:#435872;background:#fff;font-size:var(--fs-11,11px);line-height:1.7;overflow-wrap:anywhere}.ai-native-message.error .ai-native-markdown{color:#a54752;background:#fff5f6;border-color:#f1dadd}.ai-native-markdown :deep(p){margin:0 0 8px}.ai-native-markdown :deep(p:last-child){margin-bottom:0}.ai-native-markdown :deep(h1),.ai-native-markdown :deep(h2),.ai-native-markdown :deep(h3),.ai-native-markdown :deep(h4),.ai-native-markdown :deep(h5){margin:2px 0 8px;color:#304a70;font-size:var(--fs-12,12px);line-height:1.5}.ai-native-markdown :deep(pre){margin:6px 0 9px;padding:9px 11px;border-radius:8px;background:#f2f5fa;border:1px solid #e2e8f1;overflow:auto}.ai-native-markdown :deep(pre) :deep(code){padding:0;background:none;font-size:var(--fs-10,10px)}.ai-native-markdown :deep(table){border-collapse:collapse;width:100%;margin:6px 0 9px;font-size:var(--fs-10,10px)}.ai-native-markdown :deep(th),.ai-native-markdown :deep(td){padding:5px 7px;border:1px solid #e2e8f1;text-align:left}.ai-native-markdown :deep(th){background:#f2f6fc;color:#3f5c86}.ai-native-markdown :deep(ol),.ai-native-markdown :deep(ul){margin:5px 0 9px;padding-left:19px}.ai-native-markdown :deep(li){margin:3px 0;padding-left:2px}.ai-native-markdown :deep(strong){color:#314e78;font-weight:750}.ai-native-markdown :deep(code){padding:1px 4px;border-radius:4px;color:#405e91;background:#edf2fa;font:10px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.ai-native-markdown :deep(a){color:#416bc8;text-decoration:none;border-bottom:1px solid #b9caf0}
@keyframes ai-pulse{to{opacity:.25;transform:translateY(-2px)}}
@media(max-width:560px){.ai-native-panel{inset:8px;width:auto;height:auto}.ai-native-launcher{right:15px;bottom:15px}.ai-native-body{padding:15px}}
.ai-native-panel header>.ai-new-chat{width:auto;white-space:nowrap;font-size:11px;padding:6px 8px;border:1px solid currentColor;border-radius:3px}
.ai-message-tools{display:flex;gap:8px;margin:-8px 0 14px 36px}
.ai-message-tools button,.ai-agent-running>button{border:1px solid #c4c6bc;background:#fffef9;color:#202022;border-radius:3px;padding:5px 8px;font-size:11px;cursor:pointer}
.ai-native-panel button:disabled{opacity:.45;cursor:not-allowed}
.ai-history{flex:none;max-height:240px;overflow:auto;padding:12px 16px;background:#f5f3e9;border-bottom:1px solid #c4c6bc}.ai-history>small,.ai-history p{font-size:11px;color:#68706a}.ai-history>div{display:flex;gap:8px;align-items:center;margin-top:7px;padding:6px;border:1px solid #c4c6bc;background:#fffef9;border-radius:3px}.ai-history>div.active{border-color:#202022;background:#e1f5f5}.ai-history button{border:0;background:transparent;color:#202022;padding:5px;cursor:pointer;font-size:11px}.ai-history .ai-history-open{flex:1;min-width:0;text-align:left}.ai-history-open b{display:block;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;font-size:12px}.ai-history-open small{display:block;margin-top:4px;font-size:10px;color:#68706a}.ai-history-error{padding:8px 16px;font-size:11px;background:#fff1d6;color:#795b20}
.ai-live-actions{margin:10px 0 14px 36px;color:var(--ui-text);font-size:12px;line-height:1.5;min-width:0}
.ai-live-progress{padding:12px 14px;border:1px solid var(--ui-border,#526071);border-radius:6px;background:var(--ui-surface-subtle)}
.ai-progress-top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-bottom:10px;border-bottom:1px solid var(--ui-border,#526071)}
.ai-progress-top>span{display:flex;align-items:center;gap:8px;font-weight:650}.ai-progress-top small{font-size:10px;font-weight:400;color:var(--ui-text-muted)}
.ai-progress-top i{width:7px;height:7px;border-radius:50%;background:#58b6c2;box-shadow:0 0 0 3px #58b6c21c}
.ai-progress-top button{flex:none;padding:4px 8px;min-height:28px;border:1px solid var(--ui-border,#526071);border-radius:4px;background:var(--ui-surface);color:var(--ui-text);font-size:11px;cursor:pointer}
.ai-live-actions article{position:relative;padding:0 0 12px 17px;margin:0;border:0;border-left:1px solid var(--ui-border,#526071);background:transparent;border-radius:0;box-shadow:none}
.ai-live-actions article::before{content:'';position:absolute;left:-4px;top:5px;width:7px;height:7px;border-radius:50%;background:#80909e}
.ai-live-actions article.is-current::before{background:#58b6c2;box-shadow:0 0 0 3px #58b6c21c}
.ai-live-actions article:last-child{padding-bottom:0;border-left-color:transparent}
.ai-action-timeline{padding:12px 0 0 4px}.ai-live-actions header{display:flex;justify-content:space-between;align-items:baseline;gap:10px;padding:0;margin:0;background:transparent;border:0;box-shadow:none}
.ai-live-actions header b{font-size:12px;font-weight:650}.ai-live-actions header small{font-size:10px;white-space:nowrap;font-variant-numeric:tabular-nums;color:var(--ui-text-muted)}
.ai-live-actions p{margin:3px 0 0;padding:0;font-size:11px;font-weight:400;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;color:var(--ui-text-muted)}
.ai-live-actions pre{overflow:auto;max-height:180px;margin:6px 0;font-size:11px;white-space:pre-wrap;overflow-wrap:anywhere}.ai-live-actions summary{cursor:pointer;font-size:11px;padding:4px 0}.ai-live-actions>summary{margin-bottom:8px}
@media(max-width:560px){.ai-live-actions{margin-left:0}.ai-live-progress{padding:10px}.ai-progress-top{gap:6px}}
</style>
