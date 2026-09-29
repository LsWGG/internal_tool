export const HISTORY_KEY='internal-tools.ai-history.v1'
export function snapshotMessages(messages){
  return messages.slice(-100).map(message=>{
    const copy=JSON.parse(JSON.stringify(message))
    delete copy.executing
    copy.restored=true
    if(copy.action||copy.config_patch){
      copy.agentSteps=(copy.agentSteps||[]).map(step=>step.status==='waiting'?{...step,label:'历史方案',detail:'如需执行，请重新提问生成方案'}:step)
    }
    return copy
  })
}
export function readHistory(storage){
  const raw=storage.getItem(HISTORY_KEY)
  if(!raw)return {activeId:null,sessions:[]}
  const data=JSON.parse(raw)
  if(data.version!==1||!Array.isArray(data.sessions))throw new Error('Invalid history')
  const sessions=data.sessions.filter(s=>typeof s.id==='string'&&typeof s.title==='string'&&Array.isArray(s.messages)).slice(0,20).map(s=>({...s,messages:snapshotMessages(s.messages.filter(m=>m&&['user','assistant'].includes(m.role)&&typeof m.content==='string'))}))
  return {activeId:data.activeId,sessions}
}
export function writeHistory(storage,sessions,activeId){
  const recent=[...sessions].sort((a,b)=>b.updatedAt-a.updatedAt).slice(0,20).map(s=>({...s,messages:snapshotMessages(s.messages)}))
  // Keep the current conversation; evict oldest conversations before exceeding the budget.
  while(JSON.stringify(recent).length>1500000&&recent.length>1){
    const index=recent.findLastIndex(s=>s.id!==activeId)
    recent.splice(index,1)
  }
  storage.setItem(HISTORY_KEY,JSON.stringify({version:1,activeId,sessions:recent}))
  return recent
}
