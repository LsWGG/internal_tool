<script setup>
import {computed,nextTick,onMounted,ref} from 'vue'
import axios from 'axios'
import {toolGroups} from './toolCatalog'

const props=defineProps({currentView:{type:String,default:'home'},context:{type:Object,default:()=>({})}})
const emit=defineEmits(['navigate','apply-config'])
const open=ref(false),busy=ref(false),input=ref(''),aiStatus=ref('checking'),messages=ref([]),scrollEl=ref(),agentStage=ref(0)
const configured=computed(()=>aiStatus.value==='ready')
const tools=toolGroups.flatMap(group=>group.tools)
const currentTool=computed(()=>tools.find(tool=>tool.id===props.currentView))
const pageName=computed(()=>currentTool.value?.name||'工具门户')
const quickPrompts=computed(()=>({
  home:['我应该使用哪个工具？','帮我规划一个数据处理流程','这个系统能完成什么？'],
  map:['根据当前配置检查下载方案','如何选择 PNG 和 GeoTIFF？','帮我设置 DEM 下载参数'],
  crawler:['检查当前采集配置','帮我选择合适的数据源模式','分析最近一次失败原因'],
  pdf:['检查当前 PDF 转换配置','应用型网页怎样避免排版错位？','推荐结果命名模板'],
  database:['根据需求帮我写 SQL','导入前需要检查什么？','如何安全迁移数据？'],
}[props.currentView]||['介绍当前工具的使用步骤','检查我需要准备哪些数据','有哪些常见错误？']))

function escapeHtml(value){return String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]))}
function inlineMarkdown(value){return escapeHtml(value).replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')}
function renderMarkdown(value){
  const output=[];let listType=''
  const closeList=()=>{if(listType){output.push(`</${listType}>`);listType=''}}
  for(const raw of String(value||'').replace(/\r\n?/g,'\n').split('\n')){
    const line=raw.trim();if(!line){closeList();continue}
    const heading=line.match(/^(#{1,3})\s+(.+)$/),ordered=line.match(/^\d+[.)、]\s*(.+)$/),unordered=line.match(/^[-*+]\s+(.+)$/)
    if(heading){closeList();const level=Math.min(heading[1].length+2,5);output.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`)}
    else if(ordered||unordered){const type=ordered?'ol':'ul';if(listType!==type){closeList();listType=type;output.push(`<${type}>`)}output.push(`<li>${inlineMarkdown((ordered||unordered)[1])}</li>`)}
    else{closeList();output.push(`<p>${inlineMarkdown(line)}</p>`)}
  }
  closeList();return output.join('')
}

async function scrollBottom(){await nextTick();if(scrollEl.value)scrollEl.value.scrollTop=scrollEl.value.scrollHeight}
async function send(text=input.value){
  const content=String(text||'').trim();if(!content||busy.value)return
  messages.value.push({role:'user',content});input.value='';busy.value=true;agentStage.value=0;await scrollBottom()
  const stageTimer=setInterval(()=>{if(agentStage.value<2)agentStage.value+=1},850)
  try{
    const history=messages.value.map(message=>{
      let content=message.content
      if(message.role==='assistant'&&(message.action||message.actionResult)){
        content+=`\n\n[Agent 上下文：${JSON.stringify({action:message.action||null,result:message.actionResult||null})}]`
      }
      return {role:message.role,content}
    })
    const {data}=await axios.post('/api/ai/chat',{current_view:props.currentView,context:props.context,messages:history})
    agentStage.value=3
    const response={role:'assistant',content:data.answer,...data,agentSteps:buildAgentSteps(data)}
    if(response.action){
      const previous=[...messages.value].reverse().find(item=>item.role==='assistant'&&item.action&&!item.actionDone&&!item.superseded)
      if(previous)previous.superseded=true
    }
    messages.value.push(response)
    if(response.action&&!response.action.requires_confirmation)await executeMessage(response)
  }catch(error){messages.value.push({role:'assistant',content:error.response?.data?.detail||'AI 助手暂时不可用，请稍后重试。',error:true})}
  finally{clearInterval(stageTimer);busy.value=false;await scrollBottom()}
}
function buildAgentSteps(data){
  const manifest=tools.find(item=>item.id===(data.action?.tool_id||data.navigate_to))
  const steps=[{label:'理解需求',detail:`结合“${pageName.value}”页面分析目标`,status:'done'}]
  if(manifest)steps.push({label:'选择能力',detail:manifest.name,status:'done'})
  if(data.action||data.config_patch)steps.push({label:'校验参数',detail:'已通过能力注册表校验',status:'done'})
  if(data.action?.requires_confirmation)steps.push({label:'等待确认',detail:'确认后才会创建、修改或删除数据',status:'waiting'})
  else if(data.action)steps.push({label:'执行工具',detail:'已自动读取并返回真实结果',status:'done'})
  return steps
}
function planDetails(message){
  const p=message.action?.parameters||{}
  const sourceNames={generic:'独立网页',news:'分页列表 / 详情发现',twitter:'推特 / X',tiktok:'TikTok',telegram:'Telegram',youtube:'YouTube',wechat:'微信公众号'}
  const formatNames={xlsx:'Excel',csv:'CSV',json:'JSON',jsonl:'JSONL',postgresql:'PostgreSQL',png:'PNG',tif:'GeoTIFF'}
  const details=[]
  if(p.source)details.push(['采集流程',sourceNames[p.source]||p.source])
  if(p.urls?.length)details.push(['入口地址',p.urls.length===1?p.urls[0]:`${p.urls.length} 个地址`])
  if(p.keyword)details.push(['检索目标',p.keyword])
  if(p.account_name)details.push(['账号目标',p.account_name])
  if(p.max_items!==undefined)details.push(['采集数量',p.max_items===-1?'不限量增量':`${p.max_items} 条`])
  if(p.fields?.length)details.push(['保存字段',p.fields.join('、')])
  if(p.output_format)details.push(['输出格式',formatNames[p.output_format]||p.output_format.toUpperCase()])
  return details
}
function useSuggestion(text){input.value=text;send(text)}
function navigate(id){emit('navigate',id)}
function applyPatch(patch,message){message.applied=true;emit('apply-config',patch);messages.value.push({role:'assistant',content:`已将“${patch.summary||'建议配置'}”填入页面，请检查后再创建任务。`});scrollBottom()}
async function executeMessage(message){
  if(message.executing||message.actionDone)return
  message.executing=true
  try{
    if(message.config_patch&&!message.applied){message.applied=true;emit('apply-config',message.config_patch)}
    const {data:result}=await axios.post('/api/ai/actions/execute',{action:message.action,confirmed:true})
    message.actionDone=true;message.applied=true
    message.actionResult=result
    if(result.tool_id)emit('navigate',result.tool_id)
    messages.value.push({role:'assistant',content:result.message||'操作已完成。',download_url:result.download_url})
  }catch(error){messages.value.push({role:'assistant',content:error.response?.data?.detail||error.message||'任务创建失败，请检查配置后重试。',error:true})}
  finally{message.executing=false;await scrollBottom()}
}
function onKeydown(event){if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send()}}
async function checkCapabilities(){
  aiStatus.value='checking'
  try{aiStatus.value=(await axios.get('/api/ai/capabilities',{timeout:5000})).data.configured?'ready':'unconfigured'}
  catch{aiStatus.value='backend-offline'}
}
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
        <header><div class="ai-native-mark"><svg viewBox="0 0 24 24"><path d="M12 2.8 14 8l5.2 2-5.2 2-2 5.2-2-5.2-5.2-2L10 8l2-5.2Z"/></svg></div><div><b>AI 工作助手</b><small>正在理解：{{pageName}}</small></div><button aria-label="关闭" @click="open=false">×</button></header>
        <div ref="scrollEl" class="ai-native-body">
          <section v-if="!messages.length" class="ai-native-welcome"><span>AI NATIVE</span><h2>现在想完成什么？</h2><p>我会结合当前页面给出步骤、选择工具或生成可确认的配置。</p><div><button v-for="item in quickPrompts" :key="item" @click="send(item)">{{item}}<i>↗</i></button></div></section>
          <template v-for="(message,index) in messages" :key="index">
            <div class="ai-native-message" :class="[message.role,{error:message.error}]"><span>{{message.role==='assistant'?'AI':'你'}}</span><div v-if="message.role==='assistant'" class="ai-native-markdown" v-html="renderMarkdown(message.content)"></div><p v-else>{{message.content}}</p></div>
            <div v-if="message.role==='assistant'&&message.agentSteps?.length&&!message.error" class="ai-agent-trace">
              <div v-for="step in message.agentSteps" :key="step.label" :class="step.status"><i>{{step.status==='waiting'?'·':'✓'}}</i><span><b>{{step.label}}</b><small>{{step.detail}}</small></span></div>
            </div>
            <dl v-if="message.role==='assistant'&&planDetails(message).length" class="ai-agent-plan">
              <template v-for="item in planDetails(message)" :key="item[0]"><dt>{{item[0]}}</dt><dd>{{item[1]}}</dd></template>
            </dl>
            <div v-if="message.role==='assistant'&&(message.navigate_to||message.config_patch||message.action)" class="ai-native-actions">
              <button v-if="message.navigate_to" @click="navigate(message.navigate_to)">打开{{tools.find(item=>item.id===message.navigate_to)?.name||'建议工具'}}</button>
              <button v-if="message.config_patch&&!message.action&&!message.applied" class="primary" @click="applyPatch(message.config_patch,message)">应用建议配置</button>
              <span v-if="message.superseded" class="ai-native-action-expired">已由新方案替换</span>
              <button v-else-if="message.action&&!message.actionDone" class="primary" :disabled="message.executing" @click="executeMessage(message)">{{message.executing?'正在执行…':message.action.label}}</button>
              <span v-if="message.actionDone" class="ai-native-action-done">✓ 已完成</span>
            </div>
            <div v-if="message.role==='assistant'&&message.suggestions?.length" class="ai-native-followups"><button v-for="item in message.suggestions" :key="item" @click="useSuggestion(item)">{{item}}</button></div>
            <a v-if="message.download_url" class="ai-native-download" :href="message.download_url">下载任务结果</a>
          </template>
          <div v-if="busy" class="ai-agent-running"><span>Agent 正在工作</span><div><i :class="{done:agentStage>0,active:agentStage===0}">1</i>理解需求<b></b><i :class="{done:agentStage>1,active:agentStage===1}">2</i>选择能力<b></b><i :class="{done:agentStage>2,active:agentStage===2}">3</i>校验方案</div><small>{{['正在理解目标和页面上下文','正在匹配系统能力','正在校验工具参数'][Math.min(agentStage,2)]}}</small></div>
        </div>
        <footer><div v-if="aiStatus==='checking'" class="ai-native-unconfigured info">正在检查 AI 服务…</div><div v-else-if="aiStatus==='backend-offline'" class="ai-native-unconfigured">后端服务未连接，请启动或重启服务。<button @click="checkCapabilities">重新检测</button></div><div v-else-if="aiStatus==='unconfigured'" class="ai-native-unconfigured">后端已连接，但尚未配置 AI 模型环境变量。<button @click="checkCapabilities">重新检测</button></div><div class="ai-native-input"><textarea v-model="input" rows="2" :disabled="busy||!configured" :placeholder="`询问关于“${pageName}”的问题…`" @keydown="onKeydown"></textarea><button :disabled="busy||!configured||!input.trim()" aria-label="发送" @click="send()">↑</button></div><small>读取与查询可直接完成，创建、删除和数据写入需要你确认。</small></footer>
      </aside>
    </Transition>
  </Teleport>
</template>

<style scoped>
.ai-native-launcher{position:fixed;right:24px;bottom:22px;z-index:1300;display:flex;align-items:center;justify-content:center;gap:7px;width:58px;height:42px;border:1px solid #c8d8f5;border-radius:14px;color:#fff;background:linear-gradient(135deg,#426fe3,#7659e8);box-shadow:0 12px 32px #355dbf42;cursor:pointer;font:700 12px Inter,"PingFang SC",sans-serif;transition:.18s}.ai-native-launcher:hover{transform:translateY(-2px);box-shadow:0 16px 38px #355dbf52}.ai-native-launcher.active{opacity:0;pointer-events:none}.ai-native-launcher svg{width:19px;fill:currentColor}
.ai-native-panel{position:fixed;right:18px;bottom:18px;z-index:1301;display:flex;flex-direction:column;width:min(410px,calc(100vw - 24px));height:min(680px,calc(100dvh - 36px));overflow:hidden;border:1px solid #d6e0ef;border-radius:20px;color:#2b405d;background:#f8fafe;box-shadow:0 24px 70px #29466b33;font-family:Inter,"PingFang SC",sans-serif}.ai-native-panel>header{display:flex;align-items:center;gap:11px;flex:none;padding:15px 16px;border-bottom:1px solid #e0e7f1;background:#fff}.ai-native-mark{display:grid;place-items:center;width:35px;height:35px;border-radius:11px;color:#fff;background:linear-gradient(135deg,#416fe2,#7659e8)}.ai-native-mark svg{width:20px;fill:currentColor}.ai-native-panel header>div:nth-child(2){display:flex;min-width:0;flex:1;flex-direction:column}.ai-native-panel header b{font-size:13px}.ai-native-panel header small{margin-top:3px;color:#8190a5;font-size:10px}.ai-native-panel header>button{width:32px;height:32px;border:0;border-radius:9px;color:#71829a;background:transparent;font-size:22px;cursor:pointer}.ai-native-panel header>button:hover{background:#f0f3f8}
.ai-native-body{flex:1;min-height:0;padding:18px;overflow-y:auto;scrollbar-width:thin}.ai-native-welcome>span{color:#5d78bd;font-size:9px;font-weight:800;letter-spacing:1.5px}.ai-native-welcome h2{margin:8px 0 6px;font-size:21px;letter-spacing:-.4px}.ai-native-welcome p{color:#76879e;font-size:11px;line-height:1.7}.ai-native-welcome>div{display:grid;gap:8px;margin-top:20px}.ai-native-welcome button{display:flex;align-items:center;justify-content:space-between;padding:12px 13px;border:1px solid #dce5f1;border-radius:11px;color:#465d7b;background:#fff;text-align:left;font-size:11px;cursor:pointer}.ai-native-welcome button:hover{border-color:#abc2eb;color:#3d65c5;background:#f7faff}.ai-native-welcome i{font-style:normal;color:#8aa2ca}
.ai-native-message{display:grid;grid-template-columns:27px minmax(0,1fr);align-items:start;gap:9px;margin-bottom:15px}.ai-native-message>span{display:grid;place-items:center;width:27px;height:27px;border-radius:8px;color:#526d9c;background:#e8eef9;font-size:9px;font-weight:800}.ai-native-message.user>span{color:#fff;background:#5675c7}.ai-native-message p{margin:0;padding:10px 12px;border:1px solid #e0e7f0;border-radius:4px 12px 12px;color:#435872;background:#fff;font-size:11px;line-height:1.75;white-space:pre-wrap;overflow-wrap:anywhere}.ai-native-message.user p{border:0;border-radius:12px 4px 12px;color:#fff;background:#526fc0}.ai-native-message.error p{color:#a54752;background:#fff5f6;border-color:#f1dadd}.ai-native-actions{display:flex;gap:7px;margin:-7px 0 14px 36px}.ai-native-actions button{padding:7px 10px;border:1px solid #bdcce4;border-radius:8px;color:#4d6587;background:#fff;font-size:10px;cursor:pointer}.ai-native-actions button.primary{border-color:#4e70cc;color:#fff;background:#4e70cc}.ai-native-followups{display:flex;flex-wrap:wrap;gap:6px;margin:-5px 0 16px 36px}.ai-native-followups button{padding:5px 8px;border:0;border-radius:7px;color:#667b98;background:#edf2f9;font-size:9px;cursor:pointer}.ai-native-thinking{display:flex;align-items:center;gap:4px;margin:4px 0 15px 36px;color:#8391a4;font-size:10px}.ai-native-thinking i{width:5px;height:5px;border-radius:50%;background:#6984c7;animation:ai-pulse 1s infinite alternate}.ai-native-thinking i:nth-child(2){animation-delay:.2s}.ai-native-thinking i:nth-child(3){animation-delay:.4s}.ai-native-thinking span{margin-left:5px}
.ai-native-panel>footer{flex:none;padding:12px 14px 13px;border-top:1px solid #dfe7f1;background:#fff}.ai-native-input{display:flex;align-items:end;gap:8px;padding:8px 8px 8px 11px;border:1px solid #cedbeb;border-radius:13px;background:#fbfcff;box-shadow:0 0 0 3px transparent;transition:.15s}.ai-native-input:focus-within{border-color:#829fdd;box-shadow:0 0 0 3px #5275cf15}.ai-native-input textarea{flex:1;min-height:38px;max-height:100px;padding:2px 0;border:0;outline:0;resize:none;color:#334b68;background:transparent;font:11px/1.6 inherit}.ai-native-input button{display:grid;place-items:center;width:31px;height:31px;border:0;border-radius:9px;color:#fff;background:#526fc9;font-size:17px;cursor:pointer}.ai-native-input button:disabled{opacity:.35;cursor:not-allowed}.ai-native-panel footer>small{display:block;margin-top:7px;color:#929fb0;font-size:8px;text-align:center}.ai-native-unconfigured{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px;padding:8px;border-radius:8px;color:#9d4d55;background:#fff1f2;font-size:10px}.ai-native-unconfigured.info{color:#557096;background:#eef4fd}.ai-native-unconfigured>button{padding:4px 7px;border:1px solid currentColor;border-radius:6px;color:inherit;background:transparent;font-size:9px;white-space:nowrap;cursor:pointer}.ai-drawer-enter-active,.ai-drawer-leave-active{transition:.2s}.ai-drawer-enter-from,.ai-drawer-leave-to{opacity:0;transform:translateY(12px) scale(.98)}
.ai-native-actions button:disabled{opacity:.55;cursor:wait}.ai-native-action-done{align-self:center;color:#2b8b67;font-size:10px;font-weight:700}
.ai-native-action-expired{align-self:center;color:#8b98aa;font-size:10px}
.ai-native-download{display:inline-flex;margin:-7px 0 14px 36px;padding:7px 11px;border-radius:8px;color:#fff;background:#4e70cc;text-decoration:none;font-size:10px;font-weight:700}
.ai-agent-trace{display:grid;gap:6px;margin:-7px 0 14px 36px;padding:10px 11px;border:1px solid #dfe7f2;border-radius:11px;background:#f4f7fc}.ai-agent-trace>div{display:flex;align-items:center;gap:8px}.ai-agent-trace i{display:grid;place-items:center;width:17px;height:17px;border-radius:50%;color:#fff;background:#5d7acc;font-size:9px;font-style:normal}.ai-agent-trace .waiting i{color:#5471bd;background:#e3eafa}.ai-agent-trace span{display:flex;min-width:0;align-items:baseline;gap:7px}.ai-agent-trace b{color:#435a79;font-size:10px}.ai-agent-trace small{overflow:hidden;color:#8593a7;font-size:9px;text-overflow:ellipsis;white-space:nowrap}
.ai-agent-running{margin:4px 0 15px 36px;padding:11px;border:1px solid #dce6f4;border-radius:11px;background:#fff}.ai-agent-running>span{display:block;margin-bottom:9px;color:#506990;font-size:9px;font-weight:800;letter-spacing:.5px}.ai-agent-running>div{display:flex;align-items:center;color:#7f8fa5;font-size:9px}.ai-agent-running i{display:grid;place-items:center;width:17px;height:17px;margin-right:4px;border-radius:50%;color:#788ca9;background:#edf1f7;font-size:8px;font-style:normal}.ai-agent-running i.active{color:#fff;background:#5576d1;box-shadow:0 0 0 4px #5576d118}.ai-agent-running i.done{color:#fff;background:#62a087}.ai-agent-running b{flex:1;height:1px;margin:0 6px;background:#e0e7f0}.ai-agent-running small{display:block;margin-top:9px;color:#8997aa;font-size:9px}
.ai-agent-plan{display:grid;grid-template-columns:58px minmax(0,1fr);gap:6px 9px;margin:-7px 0 14px 36px;padding:11px 12px;border:1px solid #dce5f2;border-radius:11px;background:#fff}.ai-agent-plan dt{color:#8795a8;font-size:9px}.ai-agent-plan dd{min-width:0;margin:0;overflow:hidden;color:#415977;font-size:10px;font-weight:600;text-overflow:ellipsis;white-space:nowrap}
.ai-native-markdown{min-width:0;margin:0;padding:10px 12px;border:1px solid #e0e7f0;border-radius:4px 12px 12px;color:#435872;background:#fff;font-size:11px;line-height:1.7;overflow-wrap:anywhere}.ai-native-message.error .ai-native-markdown{color:#a54752;background:#fff5f6;border-color:#f1dadd}.ai-native-markdown :deep(p){margin:0 0 8px}.ai-native-markdown :deep(p:last-child){margin-bottom:0}.ai-native-markdown :deep(h3),.ai-native-markdown :deep(h4),.ai-native-markdown :deep(h5){margin:2px 0 8px;color:#304a70;font-size:12px;line-height:1.5}.ai-native-markdown :deep(ol),.ai-native-markdown :deep(ul){margin:5px 0 9px;padding-left:19px}.ai-native-markdown :deep(li){margin:3px 0;padding-left:2px}.ai-native-markdown :deep(strong){color:#314e78;font-weight:750}.ai-native-markdown :deep(code){padding:1px 4px;border-radius:4px;color:#405e91;background:#edf2fa;font:10px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}.ai-native-markdown :deep(a){color:#416bc8;text-decoration:none;border-bottom:1px solid #b9caf0}
@keyframes ai-pulse{to{opacity:.25;transform:translateY(-2px)}}
@media(max-width:560px){.ai-native-panel{inset:8px;width:auto;height:auto}.ai-native-launcher{right:15px;bottom:15px}.ai-native-body{padding:15px}}
</style>
