// 工具分类的运行时状态：后端存的是「用户改过的东西」，这里把它和代码里的默认目录合起来，
// 得到首页真正渲染的那一份。
//
// 合并只有这一份实现（首页和设置弹窗都走它），而且**空状态必须渲染成代码里那 6 组 18 卡**：
// test_ui_catalog.py 把全部 /api/** abort 掉，它就是靠这条活着的。
//
// 这个模块 import toolCatalog（代码默认值），**不要** import linkTools —— 调用方把外链
// map 好再传进来，否则 linkTools → catalogState → linkTools 成环。
import {computed,reactive} from 'vue'
import axios from 'axios'
import {externalGroup,toolGroups} from './toolCatalog'

// 代码里的分类，顺序就是首页的默认顺序（「外链工具」排在最后）。
const codeGroups=[...toolGroups,externalGroup]
const codeById=new Map(codeGroups.map(group=>[group.id,group]))
// 18 个内置工具本来的落点。只有它和 tools 里记的不一样时，那条覆盖才有意义。
const codeToolGroup=new Map(toolGroups.flatMap(group=>group.tools.map(tool=>[tool.id,group.id])))

export const catalogState=reactive({order:[],categories:{},tools:{},loaded:false,failed:false})

/** 一个分类算不算存在。内置的永远存在；自建的凭 `categories[id].name` —— 这也是
 *  「自建分类」的定义（order 里挂着一个没有名字的 id 不算一个分类）。 */
function exists(id){return codeById.has(id)||!!catalogState.categories[id]?.name}

/** 有序分类记录：仓库顺序 → 代码顺序 → 只出现在 categories 里的（防手改文件丢数据）。 */
export const categories=computed(()=>{
  const seen=new Set(),order=[]
  const take=id=>{if(id&&!seen.has(id)&&exists(id)){seen.add(id);order.push(id)}}
  ;(catalogState.order||[]).forEach(take)
  codeGroups.forEach(group=>take(group.id))
  Object.keys(catalogState.categories||{}).forEach(take)
  return order.map(id=>{
    const code=codeById.get(id),patch=catalogState.categories[id]||{}
    return {
      id,
      name:patch.name||code?.name||id,
      // `??` 不是 `||`：空字符串是「用户把说明清空了」这个有效覆盖
      description:patch.description??code?.description??'',
      icon:patch.icon||code?.icon||'layers',
      builtin:codeById.has(id),
    }
  })
})

/** 内置工具落在哪个分类：默认是代码里那一组，tools 里记了、且目标分类还在，就听它的。
 *  目标分类被删掉时**忽略覆盖、回默认组** —— 认不出的落点不能让工具凭空消失。 */
function landingOf(toolId,codeGroupId){
  const target=catalogState.tools?.[toolId]
  return target&&exists(target)?target:codeGroupId
}

export function toolGroupId(toolId){return landingOf(toolId,codeToolGroup.get(toolId))}

/** 某个分类里的内置工具（保持代码顺序；外链由调用方自己接在后面）。 */
export function toolsIn(categoryId){
  const out=[]
  toolGroups.forEach(group=>group.tools.forEach(tool=>{
    if(landingOf(tool.id,group.id)===categoryId)out.push(tool)
  }))
  return out
}

export function hasCategory(id){return categories.value.some(item=>item.id===id)}

/** 分类名。认不出的返回「外链工具」（与 linkGroupId 的兜底是同一条规矩）。 */
export function categoryName(id){return categories.value.find(item=>item.id===id)?.name||externalGroup.name}

/** 首页分组：分类 × （内置工具 + 传进来的外链工具）。**不丢空分组** ——
 *  设置弹窗要看见「这个分类现在是空的」，首页那头由调用方过滤（今天就有的那行）。 */
export function mergeGroups(externalTools=[]){
  return categories.value.map(category=>({
    ...category,
    tools:[...toolsIn(category.id),...externalTools.filter(tool=>tool.group===category.id)],
  }))
}

/** 前端铸自建分类的 id：order 必须能引用它，所以不能等服务端回。12 位 hex，够用且好认。
 *
 * **不用 `crypto.randomUUID`**：`npm run dev -- --host 0.0.0.0` 之后从 http://192.168.x.x
 * 打开时它不是 secure context，那个 API 直接是 undefined。 */
export function newCategoryId(){
  return (Date.now().toString(16)+Math.random().toString(16).slice(2,10)).slice(-12)
}

/** 合并视图 → 线上那份稀疏形态。**「和代码默认一样」的字段不写进去**：
 *  写进去就等于把今天的描述和图标冻进文件，代码以后改了动过设置的人也看不到。 */
export function toPayload(){
  const records=categories.value
  const patches={}
  records.forEach(item=>{
    const code=codeById.get(item.id)
    const patch={}
    if(!code||item.name!==code.name)patch.name=item.name
    if(item.description!==(code?.description??''))patch.description=item.description
    if(!code||item.icon!==code.icon)patch.icon=item.icon
    if(Object.keys(patch).length)patches[item.id]=patch
  })
  const tools={}
  toolGroups.forEach(group=>group.tools.forEach(tool=>{
    const target=catalogState.tools?.[tool.id]
    if(target&&target!==group.id&&exists(target))tools[tool.id]=target
  }))
  return {order:records.map(item=>item.id),categories:patches,tools}
}

/** 把回显/读回来的数据装进 state。形状不对的当没有 —— 与后端「文件坏了当空表」同一条规矩。 */
function apply(raw){
  const source=raw&&typeof raw==='object'?raw:{}
  catalogState.order=Array.isArray(source.order)?source.order.filter(id=>typeof id==='string'&&id):[]
  catalogState.categories=source.categories&&typeof source.categories==='object'?source.categories:{}
  catalogState.tools=source.tools&&typeof source.tools==='object'?source.tools:{}
}

/** 读一次。失败只记一笔：后端没起来时首页照常显示代码里的内置目录。**绝不抛** ——
 *  它是被 `Promise.all` 一起等的，抛出去就是 unhandled rejection → pageerror。 */
export async function load(){
  try{
    const {data}=await axios.get('/api/catalog')
    apply(data?.catalog)
    catalogState.failed=false
  }catch(error){
    catalogState.failed=true
    console.error(error)
  }finally{
    catalogState.loaded=true
  }
}

/** 把 state 的顺序固化成「当前看到的顺序」。
 *
 *  所有改动都先走这一步：state.order 里可能一个内置 id 都没有（用户还没改过任何东西），
 *  直接往里 push 会让新分类排到所有内置分类前面。 */
function syncOrder(){catalogState.order=categories.value.map(item=>item.id)}

/** 改一次分类，立刻整表存回去。
 *
 *  乐观更新 + 失败回滚：拖动这种手势如果还要再点一次「保存」，用户只会以为没生效。
 *  `mutate` 只改 state，序列化和「什么时候该发请求」都归这里 —— mutate 前后一模一样
 *  就不发（把一行拖到它自己身上不该产生一次 POST）。 */
export async function commit(mutate){
  const before=toPayload()
  mutate()
  const next=toPayload()
  if(JSON.stringify(before)===JSON.stringify(next))return null
  try{
    const {data}=await axios.post('/api/catalog',{catalog:next})
    apply(data?.catalog)
    return data?.catalog||null
  }catch(error){
    apply(before)
    throw error
  }
}

/** 新建一个分类：**同步**返回 id 和那次 POST 的 promise。
 *
 *  不把 id 藏在 promise 里，是因为调用方拿到 id 的第一件事就是把它选中，而乐观更新是立刻
 *  生效的 —— 等 POST 回来再选中，中间那一拍左栏已经有这一行了，右栏还是上一个分类的表单，
 *  用户这时候打字会被随后到来的同步擦掉。 */
export function addCategory({name,description='',icon='layers'}){
  const id=newCategoryId()
  const done=commit(()=>{
    syncOrder()
    catalogState.order.push(id)
    catalogState.categories[id]={name,description,icon}
  })
  return {id,done}
}

/** 改一个分类的字段。**和代码默认一样的那一项不留覆盖** —— 用户把名字改回默认值，等于
 *  「没改过」，文件里那条覆盖就该消失（否则代码以后改这个分类的描述，他永远看不到）。
 *  自建分类没有代码默认值，三个字段都是它本身的数据，一律保留。 */
export function updateCategory(id,patch){
  return commit(()=>{
    const code=codeById.get(id)
    const next={...(catalogState.categories[id]||{})}
    Object.entries(patch).forEach(([field,value])=>{
      if(code&&value===code[field])delete next[field]
      else next[field]=value
    })
    if(Object.keys(next).length)catalogState.categories[id]=next
    else delete catalogState.categories[id]
  })
}

export function removeCategory(id){
  return commit(()=>{
    syncOrder()
    catalogState.order=catalogState.order.filter(item=>item!==id)
    delete catalogState.categories[id]
    // 指向它的工具落点一起删掉：留着就是一把悬空的指针，等哪天又有同名 id 的分类，
    // 这些工具会莫名其妙自己跑过去。
    Object.keys(catalogState.tools).forEach(toolId=>{
      if(catalogState.tools[toolId]===id)delete catalogState.tools[toolId]
    })
  })
}

/** 把一个分类移到第 index 位（先摘掉再插入，index 按摘掉之后的列表算）。 */
export function moveCategory(id,index){
  return commit(()=>{
    syncOrder()
    const rest=catalogState.order.filter(item=>item!==id)
    rest.splice(Math.max(0,Math.min(rest.length,index)),0,id)
    catalogState.order=rest
  })
}

/** 工具换组。拖回代码里本来的那一组就把覆盖删掉 —— 文件只记「偏离默认」的部分。 */
export function moveTool(toolId,categoryId){
  return commit(()=>{
    if(codeToolGroup.get(toolId)===categoryId)delete catalogState.tools[toolId]
    else catalogState.tools[toolId]=categoryId
  })
}
