// Snapshot-scoped element handles; the model never supplies selectors or scripts.
let handles=new Map(), revision='', pageLocation=''
const visible=el=>el.isConnected&&el.getClientRects().length&&!el.closest('.ai-native-panel,.ai-native-launcher')
const sensitive=el=>el.type==='password'||/password|secret|token|api.?key|密钥|密码/i.test([el.name,el.id,el.placeholder,el.getAttribute('aria-label'),el.labels?.[0]?.textContent].join(' '))
export function inspectPage(){
  revision=crypto.randomUUID();pageLocation=location.hash;handles=new Map()
  const controls=[]
  for(const el of document.querySelectorAll('button,input,textarea,select')){
    if(!visible(el)||el.disabled||el.readOnly||sensitive(el)||['hidden','checkbox','radio','file'].includes(el.type))continue
    const label=(el.getAttribute('aria-label')||el.labels?.[0]?.textContent||el.textContent||el.placeholder||el.title||'').trim().slice(0,100)
    if(!label)continue
    const id=String(controls.length);handles.set(id,el)
    controls.push({id,label,kind:el.tagName.toLowerCase(),type:el.type,requires_upload:el.type==='file',options:el.tagName==='SELECT'?[...el.options].map(o=>({value:o.value,label:o.text})):undefined})
    if(controls.length>=150)break
  }
  return {revision,controls}
}
export function operatePage(parameters){
  if(parameters.revision!==revision||location.hash!==pageLocation)throw new Error('页面已更新，请重新提问读取最新控件。')
  const el=handles.get(parameters.control_id)
  if(!el||!visible(el)||el.disabled||sensitive(el))throw new Error('控件已不可用，请重新读取页面。')
  if(el.type==='file')throw new Error('请在页面选择本地文件，上传后继续提问。')
  if(parameters.mode==='click'){
    if(el.tagName!=='BUTTON')throw new Error('此控件不支持点击操作。')
    el.click();return '已点击页面按钮；请以页面任务状态为准。'
  }
  if(!['INPUT','TEXTAREA','SELECT'].includes(el.tagName)||el.readOnly)throw new Error('此控件不能填写。')
  if(['checkbox','radio','submit','button'].includes(el.type))throw new Error('此控件需要手动选择。')
  const value=String(parameters.value??'')
  if(el.tagName==='SELECT'&&![...el.options].some(o=>o.value===value&&!o.disabled))throw new Error('选项不存在。')
  const prototype=el.tagName==='SELECT'?HTMLSelectElement.prototype:el.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype
  Object.getOwnPropertyDescriptor(prototype,'value').set.call(el,value)
  el.dispatchEvent(new Event('input',{bubbles:true}));el.dispatchEvent(new Event('change',{bubbles:true}));el.focus()
  return '已填写页面控件。'
}

export function describeControl(id){const el=handles.get(id);return el?(el.getAttribute("aria-label")||el.labels?.[0]?.textContent||el.textContent||el.placeholder||el.title||id).trim().slice(0,100):id}
