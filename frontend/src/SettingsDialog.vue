<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import axios from 'axios'
import ModelSettings from './ModelSettings.vue'
import {externalGroup} from './toolCatalog'
import {firstChar,frameState,letterTone,linkGroupId,linkUrlError} from './linkTools'
import {addCategory,categories,categoryName,moveCategory,moveTool,removeCategory,toolsIn,updateCategory} from './catalogState'

const props=defineProps({links:{type:Array,default:()=>[]},confirmAction:{type:Function,default:null},iconComponent:[Object,Function],iconNames:{type:Array,default:()=>[]}})
const emit=defineEmits(['close','saved'])

// TAB 由数组驱动：「外链工具」仍是第一个、仍是默认打开的（它是这个弹窗本来的全部内容）。
const tabs=[{id:'links',name:'外链工具'},{id:'cats',name:'工具分类'},{id:'models',name:'大模型配置'}]
const active=ref('links')
const panel=ref(),draft=ref(null),error=ref(''),note=ref(''),busy=ref(false),probing=ref('')

// 嵌入结论说人话。三个状态各有各的说法：「还没问过」不能显示成「可以嵌」——
// 那是两件事，混起来用户就没法判断点下去会发生什么。
function verdict(link){
  const {blocked,known,policy}=frameState(link)
  if(blocked)return `不允许被嵌入${policy?`（${policy}）`:''}，点卡片会直接在新窗口打开。`
  if(known)return '没有禁止嵌入的声明，可以在系统界面里内嵌打开。'
  return '没能问到嵌入结论（连不上或没响应），先按可以嵌入处理，之后可以点「检测」重试。'
}
function verdictLabel(link){
  const {blocked,known}=frameState(link)
  return blocked?'不可嵌入 · 新窗口':(known?'可嵌入':'未检测')
}

// 分类表来自 catalogState 的合并视图（内置的 + 用户新建的，按用户排的顺序），不是代码里那张
// 死表 —— 这个 TAB 改的就是它。categoryName 与首页卡片用的是同一个函数，不会两处说法不同。
const preview=computed(()=>draft.value?{letter:firstChar(draft.value.name),tone:letterTone(draft.value.id||draft.value.name)}:null)

function detailOf(e){const detail=e?.response?.data?.detail;return Array.isArray(detail)?detail.map(item=>item.msg||String(item)).join('；'):String(detail||e?.message||'操作失败')}

function edit(link){
  draft.value=link?{...link}:{id:'',name:'',description:'',url:'',category:externalGroup.id}
  error.value='';note.value=''
  nextTick(()=>panel.value?.querySelector('.settings-form input')?.focus())
}
function cancel(){draft.value=null;error.value=''}

async function save(){
  const item=draft.value
  if(!item.name.trim()){error.value='请填写外链工具名称';return}
  const urlProblem=linkUrlError(item.url)
  if(urlProblem){error.value=urlProblem;return}
  busy.value=true;error.value='';note.value=''
  try{
    // 整表替换：把这一条并回现有表里再交出去（与 clean 端点池同一套动作）。
    const name=item.name.trim(),url=item.url.trim()
    const keep=props.links.filter(existing=>existing.id!==item.id)
    const {data}=await axios.post('/api/links',{links:[...keep,{...item,name,url}]})
    draft.value=null
    // 保存时后端会探一次嵌入政策，结论直接说出来 —— 不然用户点开卡片发现「开了新窗口」
    // 只会以为是坏的。返回的表与请求同序同长，刚交上去的那条就排在 keep 之后。
    const saved=data.links?.[keep.length]
    note.value=`已保存「${name}」，首页已更新。${saved?verdict(saved):''}`
    emit('saved')
  }catch(e){error.value=detailOf(e)}finally{busy.value=false}
}

// 重新检测一条（老数据、或者上次站点连不上时留下的「未检测」）。
async function probe(link){
  probing.value=link.id;error.value='';note.value=''
  try{
    const {data}=await axios.post(`/api/links/${link.id}/probe`)
    note.value=`「${link.name}」${verdict(data.link)}`
    emit('saved')                 // 结论变了，首页那张卡片也得跟着变
  }catch(e){error.value=detailOf(e)}finally{probing.value=''}
}

async function remove(link){
  if(props.confirmAction&&!await props.confirmAction(`删除外链工具“${link.name}”？`))return
  busy.value=true;error.value='';note.value=''
  try{
    await axios.delete(`/api/links/${link.id}`)
    note.value=`已删除「${link.name}」。`
    emit('saved')
  }catch(e){error.value=detailOf(e)}finally{busy.value=false}
}

/* ── 工具分类 TAB ─────────────────────────────────────────────────────────────
   左栏是分类，右栏是选中那个分类的详情。改动分两种时机，故意的：

     * 拖拽（排序、换组）**立刻整表 POST** —— 拖完关掉弹窗不能丢；
     * 名称/说明/图标走「保存」按钮且 dirty 才可点 —— 它们是同一组字段，逐字保存和
       「点一圈图标预览＝12 次 POST」都不能接受。

   两条写路径也不对称：内置工具 chip 有本地状态可回滚（catalogState 的乐观 + 还原），
   外链的归属存在 links.json 里、由 App 通过 props 拥有，乐观不起来 —— 成功了就让
   App 重拉（`emit('saved')`），失败只报错，界面本来就没动过。 */

const selectedId=ref('')
const selected=computed(()=>categories.value.find(item=>item.id===selectedId.value)||categories.value[0]||null)
const catName=ref(''),catDescription=ref(''),catIcon=ref('layers')
// 只认 id 的变化：分类列表因为一次拖拽重算时不能把用户正在打字的草稿冲掉。
watch(()=>selected.value?.id,()=>syncForm(),{immediate:true})
function syncForm(){
  const record=selected.value
  catName.value=record?.name||''
  catDescription.value=record?.description||''
  catIcon.value=record?.icon||'layers'
}
const catDirty=computed(()=>{
  const record=selected.value
  if(!record)return false
  return catName.value.trim()!==record.name||catDescription.value.trim()!==record.description||catIcon.value!==record.icon
})

function linksIn(categoryId){return props.links.filter(link=>linkGroupId(link.category)===categoryId)}
const countOf=categoryId=>toolsIn(categoryId).length+linksIn(categoryId).length

function selectCategory(id){selectedId.value=id}

async function saveCategory(){
  const record=selected.value
  if(!record)return
  const name=catName.value.trim()
  if(!name){error.value='请填写分类名称';return}
  busy.value=true;error.value='';note.value=''
  try{
    await updateCategory(record.id,{name,description:catDescription.value.trim(),icon:catIcon.value||'layers'})
    note.value=`已保存「${name}」，首页已更新。`
  }catch(e){error.value=`保存失败：${detailOf(e)}`}finally{busy.value=false}
}

async function createCategory(){
  busy.value=true;error.value='';note.value=''
  // 建出来就叫「新分类」，名字在右栏改完点保存。先落一个默认名是为了让这一行立刻能拖、
  // 能排序 —— 一个还没名字的分类在合并视图里根本不存在（exists 认的就是 name）。
  // 选中必须**紧跟**在乐观更新后面（见 addCategory 的注释）：晚一步，右栏就还是上一个
  // 分类的表单，用户这时打的字会被随后到来的同步擦掉。
  const {id,done}=addCategory({name:'新分类',description:'',icon:'layers'})
  selectedId.value=id
  await nextTick()
  panel.value?.querySelector('.settings-cat-form input')?.focus()
  try{await done}
  catch(e){error.value=`新建失败：${detailOf(e)}`}
  finally{busy.value=false}
}

/** 删掉这个分类之后，里面的东西会去哪儿（写进二次确认的副标题，别让用户猜）。 */
function removalNote(record){
  const builtin=toolsIn(record.id).length,links=linksIn(record.id).length
  const parts=[]
  if(builtin)parts.push(`${builtin} 个内置工具回到原来的分类`)
  if(links)parts.push(`${links} 个外链工具回到「外链工具」`)
  return parts.length?`分类里的工具不会被删除：${parts.join('，')}。`:'这个分类里现在没有工具。'
}

async function dropSelected(){
  const record=selected.value
  if(!record||record.builtin)return
  const confirmed=props.confirmAction?await props.confirmAction(`删除分类“${record.name}”？`,'确认删除',{note:removalNote(record)}):true
  if(!confirmed)return
  busy.value=true;error.value='';note.value=''
  try{
    await removeCategory(record.id)
    selectedId.value=''          // 交给合并视图落到第一个分类；watch 会把表单一起换过去
    note.value=`已删除分类「${record.name}」。`
  }catch(e){error.value=`删除失败：${detailOf(e)}`}finally{busy.value=false}
}

/* ── 拖拽 ────────────────────────────────────────────────────────────────────
   **Chromium 在 dragover 期间读不到 `dataTransfer.getData()`**（只有 types 可读），所以悬停
   高亮和落点判断认的都是下面这个 `drag` ref。`setData` 照样写：那是标准路径，拖出浏览器
   时也靠它。不引拖拽库是因为这里只有两种东西、两种落点。 */

const drag=ref(null),dropInto=ref(''),dropEdge=ref(null),moving=ref('')

function startToolDrag(id,event){
  drag.value={kind:'tool',id}
  if(event.dataTransfer){event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',`tool:${id}`)}
}
/** 拖分类行**不**改选中：选中只由点击决定。
 *
 *  曾经在这里调 `selectCategory(item.id)`，结果整条拖拽是坏的 —— 它不是「拖了没反应」而是
 *  浏览器直接把这次拖拽掐掉：`dragstart` 之后立刻 `dragend`，中间一个 `dragover`/`drop` 都
 *  没有。原因是右栏跟着换内容 → 面板高度变 → 整个左列（包括被拖的那一行）在指针底下位移。
 *  实测把这个位移推迟一个 tick 更糟：不中止了，但行在拖拽途中从指针下移走，`drop` 永远落不
 *  下来（事件序列变成一串 dragenter/dragleave 的抖动，盘上什么都不写）。
 *  所以这一条是硬约束：**拖拽开始时别动布局**。 */
function startCatDrag(item,event){
  drag.value={kind:'cat',id:item.id}
  if(event.dataTransfer){event.dataTransfer.effectAllowed='move';event.dataTransfer.setData('text/plain',`cat:${item.id}`)}
}
function endDrag(){drag.value=null;dropInto.value='';dropEdge.value=null}

/** 鼠标在这行的上半还是下半（决定分类插到它前面还是后面）。 */
function belowHalf(event){
  const box=event.currentTarget.getBoundingClientRect()
  return event.clientY>box.top+box.height/2
}
function catClass(item){
  const edge=dropEdge.value?.id===item.id?dropEdge.value:null
  return {
    'is-active':selected.value?.id===item.id,
    'is-drop-into':drag.value?.kind==='tool'&&dropInto.value===item.id,
    'is-drop-before':!!edge&&!edge.after,
    'is-drop-after':!!edge&&edge.after,
  }
}
function catDragOver(item,event){
  if(!drag.value)return
  event.preventDefault()
  if(event.dataTransfer)event.dataTransfer.dropEffect='move'
  if(drag.value.kind==='tool'){dropInto.value=item.id;dropEdge.value=null;return}
  if(drag.value.id===item.id){dropEdge.value=null;return}     // 拖到自己身上：不画插入线
  dropInto.value=''
  dropEdge.value={id:item.id,after:belowHalf(event)}
}
function catDrop(item,event){
  event.preventDefault()
  event.stopPropagation()       // 别让左列那条「落到空白处」的兜底再跑一次
  const dragged=drag.value,below=belowHalf(event)
  endDrag()
  if(!dragged)return
  if(dragged.kind==='tool'){moveChip(dragged.id,item.id);return}
  if(dragged.id===item.id)return
  const list=categories.value
  const from=list.findIndex(row=>row.id===dragged.id)
  let target=list.findIndex(row=>row.id===item.id)
  if(from>=0&&from<target)target-=1     // 被拖的那行先摘掉，插入点要在剩下的列表里数
  moveCategoryTo(dragged.id,below?target+1:target)
}
/** 左列空白处：只有分类拖拽认这里（落到列表末尾）。工具 chip 落在空白处＝什么都不做 ——
 *  空白离某一行只有几像素，误触就是把工具悄悄换了组。 */
function listDragOver(event){if(drag.value)event.preventDefault()}
function listDrop(event){
  const dragged=drag.value
  endDrag()
  if(dragged?.kind!=='cat')return
  event.preventDefault()
  moveCategoryTo(dragged.id,categories.value.length-1)
}

async function moveCategoryTo(id,index){
  error.value='';note.value=''
  try{await moveCategory(id,index)}
  catch(e){error.value=`排序失败，已还原：${detailOf(e)}`}
}
async function moveToolTo(id,target){
  error.value='';note.value=''
  try{await moveTool(id,target)}
  catch(e){error.value=`移动失败，已还原：${detailOf(e)}`}
}
async function moveLinkTo(link,target){
  // 比的是**存的值**而不是眼下显示的分组：分类被删掉、靠回落显示在「外链工具」的那条外链，
  // 拖回来时才能把 category 真的修回去。
  if(String(link.category??'').trim()===target)return
  error.value='';note.value=''
  try{
    await axios.post('/api/links',{links:props.links.map(item=>item.id===link.id?{...item,category:target}:item)})
    note.value=`已把「${link.name}」移到「${categoryName(target)}」。`
    emit('saved')                 // 地址没变，后端复用原来的嵌入结论，不重探
  }catch(e){error.value=`移动失败：${detailOf(e)}`}
}
function moveChip(toolId,target){
  if(!String(toolId).startsWith('link:'))return moveToolTo(toolId,target)
  const link=props.links.find(item=>`link:${item.id}`===toolId)
  return link?moveLinkTo(link,target):undefined
}
function shift(item,delta){
  const index=categories.value.findIndex(row=>row.id===item.id),target=index+delta
  if(index<0||target<0||target>=categories.value.length)return
  moveCategoryTo(item.id,target)
}

// Esc 关弹窗。两道判断都是「同一个按键别关两层」：
//   * defaultPrevented：让给下拉 —— selectMenu 的捕获监听会先吃掉 Escape 去关它自己的菜单；
//   * 确认框还开着时（删除的二次确认，见下面 .modal-mask 的 z-index）：那层才是焦点所在。
function onKeydown(event){
  if(event.key!=='Escape'||event.defaultPrevented)return
  if(document.querySelector('.confirm-card'))return
  emit('close')
}
onMounted(()=>{document.addEventListener('keydown',onKeydown);panel.value?.focus()})
onBeforeUnmount(()=>document.removeEventListener('keydown',onKeydown))
</script>

<template>
  <Teleport to="body">
    <Transition name="modal">
      <div class="modal-mask" @click.self="emit('close')">
        <section ref="panel" class="settings-panel" role="dialog" aria-modal="true" aria-labelledby="settings-title" tabindex="-1">
          <header class="settings-head">
            <div><span class="eyebrow">SETTINGS</span><h2 id="settings-title">系统设置</h2></div>
            <button class="settings-close" type="button" aria-label="关闭设置" @click="emit('close')">×</button>
          </header>
          <nav class="settings-tabs" aria-label="设置分类">
            <button v-for="tab in tabs" :key="tab.id" type="button" :class="{active:active===tab.id}" :aria-current="active===tab.id?'true':undefined" @click="active=tab.id">{{tab.name}}</button>
          </nav>
          <div class="settings-body">
            <!-- 报错位两个 TAB 共用（拖拽失败也落在这一行）：toast 栈是 z-index 10000，比遮罩的
                 11000 低，弹窗开着时的 toast 会被压在半透明模糊的遮罩后面，等于没提示。 -->
            <p v-if="error" class="settings-error" role="alert">{{error}}</p>
            <p v-else-if="note" class="settings-note" role="status">{{note}}</p>

            <template v-if="active==='links'">
              <p class="settings-intro">外链工具会出现在工具首页，点开后在系统界面里内嵌打开。LOGO 由名称首字自动生成；保存时会读一次目标站点的响应头，判断它允不允许被嵌入 —— 声明了不允许的（X-Frame-Options / CSP frame-ancestors），点卡片会直接在新窗口打开。</p>
              <ul v-if="props.links.length" class="settings-list">
                <li v-for="link in props.links" :key="link.id">
                  <span class="settings-logo" :style="letterTone(link.id)">{{firstChar(link.name)}}</span>
                  <div class="settings-link-text"><div class="settings-link-name"><b>{{link.name}}</b><span class="settings-verdict" :class="{'is-blocked':frameState(link).blocked,'is-unknown':!frameState(link).known}" :title="verdict(link)">{{verdictLabel(link)}}</span></div><small>{{categoryName(link.category)}} · {{link.url}}</small><p v-if="link.description">{{link.description}}</p></div>
                  <div class="settings-row-actions"><button type="button" :disabled="probing===link.id" @click="probe(link)">{{probing===link.id?'检测中…':'检测'}}</button><button type="button" @click="edit(link)">编辑</button><button type="button" class="danger" @click="remove(link)">删除</button></div>
                </li>
              </ul>
              <p v-else class="settings-empty">还没有外链工具。添加之后它会出现在工具首页的「外链工具」分组里。</p>
              <form v-if="draft" class="settings-form" @submit.prevent="save">
                <b>{{draft.id?'编辑外链工具':'添加外链工具'}}</b>
                <div class="settings-preview"><span class="settings-logo is-preview" :style="preview.tone">{{preview.letter}}</span><small>LOGO 取名称首字，颜色跟随主题自动生成</small></div>
                <label>名称<input v-model="draft.name" maxlength="40" placeholder="例如：内部 Wiki"></label>
                <label>说明<input v-model="draft.description" maxlength="80" placeholder="可选，一句话说明它能做什么"></label>
                <label>地址<input v-model="draft.url" autocomplete="off" spellcheck="false" placeholder="https://…"></label>
                <label>所属分类<select v-model="draft.category"><option v-for="item in categories" :key="item.id" :value="item.id">{{item.name}}</option></select></label>
                <div class="settings-form-actions"><button type="button" @click="cancel">取消</button><button class="primary" type="submit" :disabled="busy">{{busy?'保存中…':'保存'}}</button></div>
              </form>
              <button v-else class="settings-add" type="button" @click="edit(null)">＋ 添加外链工具</button>
            </template>

            <ModelSettings v-else-if="active==='models'" />
            <div v-else class="settings-cats">
              <div class="settings-cat-pane">
                <ul class="settings-cat-list" aria-label="分类列表" @dragover="listDragOver" @drop="listDrop">
                  <li v-for="(item,index) in categories" :key="item.id" :data-cat="item.id" class="settings-cat-row" :class="catClass(item)" draggable="true" @click="selectCategory(item.id)" @dragstart="startCatDrag(item,$event)" @dragend="endDrag" @dragover="catDragOver(item,$event)" @drop="catDrop(item,$event)">
                    <span class="settings-cat-grip" aria-hidden="true">⠿</span>
                    <span class="settings-cat-icon"><component :is="iconComponent" :name="item.icon" /></span>
                    <span class="settings-cat-name">{{item.name}}</span>
                    <small class="settings-cat-count">{{countOf(item.id)}}</small>
                    <span class="settings-cat-move">
                      <button type="button" :disabled="index===0" aria-label="上移" title="上移" @click.stop="shift(item,-1)">↑</button>
                      <button type="button" :disabled="index===categories.length-1" aria-label="下移" title="下移" @click.stop="shift(item,1)">↓</button>
                    </span>
                  </li>
                </ul>
                <button class="settings-add" type="button" :disabled="busy" @click="createCategory">＋ 新建分类</button>
              </div>

              <div v-if="selected" class="settings-cat-detail">
                <form class="settings-cat-form" @submit.prevent="saveCategory">
                  <b>{{selected.builtin?'内置分类':'自建分类'}} · {{selected.name}}</b>
                  <label>名称<input v-model="catName" maxlength="40" placeholder="例如：数据处理"></label>
                  <label>说明<input v-model="catDescription" maxlength="160" placeholder="可选，一句话说明这一组放什么"></label>
                  <div>
                    <span class="settings-field-label">图标</span>
                    <div class="settings-icon-grid" role="group" aria-label="分类图标">
                      <button v-for="name in iconNames" :key="name" type="button" class="settings-icon-choice" :class="{active:catIcon===name}" :aria-pressed="catIcon===name" :title="name" @click="catIcon=name"><component :is="iconComponent" :name="name" /></button>
                    </div>
                  </div>
                  <div class="settings-form-actions"><button type="button" :disabled="!catDirty" @click="syncForm">还原</button><button class="primary" type="submit" :disabled="!catDirty||busy">{{busy?'保存中…':'保存'}}</button></div>
                </form>

                <!-- 内置分类为什么没有删除按钮，就写在它本来会出现的位置上。 -->
                <p v-if="selected.builtin" class="settings-hint">内置分类可以改名、换图标、拖排序，但不能删除：后端读不到配置时，首页要用代码里的这一份兜底。</p>
                <button v-else class="settings-cat-delete" type="button" :disabled="busy" @click="dropSelected">删除分类</button>

                <div>
                  <span class="settings-field-label">这个分类里的工具</span>
                  <div v-if="countOf(selected.id)" class="settings-chips">
                    <span v-for="tool in toolsIn(selected.id)" :key="tool.id" class="settings-chip" :data-tool="tool.id" draggable="true" @dragstart="startToolDrag(tool.id,$event)" @dragend="endDrag">
                      <span class="settings-chip-icon" :style="letterTone(tool.id)"><component :is="iconComponent" :name="tool.icon" /></span>{{tool.name}}
                      <select v-if="moving===tool.id" class="settings-chip-select" :value="selected.id" @change="moveChip(tool.id,$event.target.value)" @blur="moving=''"><option v-for="item in categories" :key="item.id" :value="item.id">{{item.name}}</option></select>
                      <button v-else type="button" class="settings-chip-move" :aria-label="`把 ${tool.name} 移到别的分类`" @click="moving=tool.id">移动到…</button>
                    </span>
                    <span v-for="link in linksIn(selected.id)" :key="link.id" class="settings-chip" :data-tool="`link:${link.id}`" :data-link="link.id" draggable="true" @dragstart="startToolDrag(`link:${link.id}`,$event)" @dragend="endDrag">
                      <span class="settings-chip-icon" :style="letterTone(link.id)">{{firstChar(link.name)}}</span>{{link.name}}
                      <select v-if="moving===`link:${link.id}`" class="settings-chip-select" :value="selected.id" @change="moveChip(`link:${link.id}`,$event.target.value)" @blur="moving=''"><option v-for="item in categories" :key="item.id" :value="item.id">{{item.name}}</option></select>
                      <button v-else type="button" class="settings-chip-move" :aria-label="`把 ${link.name} 移到别的分类`" @click="moving=`link:${link.id}`">移动到…</button>
                    </span>
                  </div>
                  <p v-else class="settings-chip-empty">这个分类里还没有工具。把别的分类里的工具拖到左边这一行，或者用工具上的「移动到…」。</p>
                </div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>

<style scoped>
/* 弹窗配方照 App.vue 的确认框：Teleport + Transition(modal) + 现成的 .modal-mask（style.css）。
   错误提示必须留在面板里：toast 栈是 z-index 10000，比遮罩的 11000 低，弹窗开着时的
   toast 会被压在半透明模糊的遮罩后面，等于没提示。 */
/* 比全局遮罩（style.css 的 .modal-mask，11000）低一档：删除的二次确认是**另一层**遮罩，
   两层同 z-index 时后挂上来的赢，而这一层挂得晚 —— 那样确认框会被设置面板整个盖住，
   看起来就是「点了删除没反应」。 */
.modal-mask{z-index:10900}
/* 880 是两个 TAB 里宽的那个（左栏 210 + 右栏表单）需要的宽度；`min(…,100%)` 保证窄屏不溢出。 */
.settings-panel{display:flex;flex-direction:column;width:min(880px,100%);height:min(720px,calc(100dvh - 48px));max-height:calc(100dvh - 48px);border:1px solid #d9e2ef;border-radius:var(--ui-panel-radius,14px);background:var(--ui-surface,#fff);box-shadow:0 24px 64px #29415f38;outline:none;overflow:hidden}
.modal-enter-from .settings-panel,.modal-leave-to .settings-panel{transform:translateY(10px)}
.settings-head{flex:none;display:flex;align-items:center;justify-content:space-between;gap:16px;padding:20px 22px 16px;border-bottom:1px solid #e4eaf2}
.settings-head h2{margin:5px 0 0;color:#203653;font-size:var(--fs-18,18px);font-weight:700;line-height:1.4}
.settings-head .eyebrow{color:#6685ad;font-size:var(--fs-10,10px);letter-spacing:1.8px}
.settings-close{display:grid;place-items:center;flex:none;width:32px;height:32px;border:1px solid transparent;border-radius:9px;background:transparent;color:#7c8ca0;font-size:var(--fs-20,20px);line-height:1;cursor:pointer}
.settings-close:hover{border-color:#dae3f0;color:#4266b9;background:#f7fafe}
.settings-tabs{display:flex;gap:6px;flex:none;padding:12px 22px 0;border-bottom:1px solid #e4eaf2}
.settings-tabs button{padding:9px 13px;border:1px solid transparent;border-radius:9px 9px 0 0;background:transparent;color:#61748d;font-family:inherit;font-size:var(--fs-12,12px);font-weight:600;cursor:pointer}
.settings-tabs button:hover{color:#4266b9;background:#f5f9ff}
.settings-tabs button.active{border-color:#d4e0f7;border-bottom-color:transparent;background:#eaf0fd;color:#4266b9}
.settings-body{flex:1;min-height:0;padding:20px 22px 22px;overflow:auto;scrollbar-gutter:stable;overscroll-behavior:contain}
.settings-intro{margin:0 0 16px;color:var(--ui-muted,#75869b);font-size:var(--fs-12,12px);line-height:1.8}
.settings-error,.settings-note{margin:0 0 16px;padding:10px 12px;border:1px solid #e5b8bf;border-radius:9px;background:var(--ui-danger-soft,#fff2f3);color:#a4404f;font-size:var(--fs-12,12px);line-height:1.7;overflow-wrap:anywhere}
.settings-note{border-color:#bcdccd;background:var(--ui-success-soft,#eaf7f2);color:#2c6f57}
.settings-empty{margin:0 0 16px;padding:22px;border:1px dashed #d7e1ef;border-radius:11px;color:#8290a3;font-size:var(--fs-12,12px);line-height:1.8;text-align:center}
.settings-list{margin:0 0 16px;padding:0;list-style:none;border:1px solid #e4eaf2;border-radius:11px;overflow:hidden}
.settings-list li{display:grid;grid-template-columns:38px minmax(0,1fr) auto;align-items:center;gap:12px;padding:12px 14px;border-bottom:1px solid #eef2f7}
.settings-list li:last-child{border-bottom:0}
.settings-logo{display:grid;place-items:center;flex:none;width:38px;height:38px;border:1px solid #e2ebf9;border-radius:10px;font-size:var(--fs-17,17px);font-weight:650;line-height:1;box-shadow:var(--ds-inner-hi,inset 0 1px 0 #fff)}
.settings-logo.is-preview{width:44px;height:44px;font-size:var(--fs-20,20px)}
.settings-link-text{min-width:0}
.settings-link-text b{display:block;color:#2d4564;font-size:var(--fs-13,13px);font-weight:650}
.settings-link-name{display:flex;align-items:center;gap:8px;min-width:0}
.settings-link-name b{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* 三个结论三种脸色：可嵌入是中性、不可嵌入用系统里那个「选中蓝」（就是用户要留意的那条）、
   未检测是灰的。三者形状一致，只有颜色和文字在变。 */
.settings-verdict{flex:none;padding:1px 7px;border:1px solid #e6eaf0;border-radius:6px;background:#f4f6f9;color:#8592a5;font-size:var(--fs-10,10px);font-weight:600;line-height:1.7;white-space:nowrap}
.settings-verdict.is-blocked{border-color:#d4e0f7;background:#eaf0fd;color:#4266b9}
.settings-verdict.is-unknown{border-style:dashed}
.settings-link-text small{display:block;margin-top:3px;color:#8592a5;font-size:var(--fs-10,10px);overflow-wrap:anywhere}
.settings-link-text p{margin:5px 0 0;color:#7a8ba0;font-size:var(--fs-11,11px);line-height:1.7}
.settings-row-actions{display:flex;gap:6px;flex:none}
.settings-row-actions button,.settings-form-actions button,.settings-add,.settings-chip-move,.settings-cat-delete{min-height:32px;padding:6px 12px;border:1px solid #ccd6e3;border-radius:8px;background:#fff;color:#52657d;font-family:inherit;font-size:var(--fs-11,11px);font-weight:600;cursor:pointer}
.settings-row-actions button:hover,.settings-form-actions button:hover,.settings-add:hover,.settings-chip-move:hover,.settings-cat-delete:hover{border-color:#9fb0c5;background:#f5f8fc}
.settings-row-actions button:disabled,.settings-form-actions button:disabled,.settings-add:disabled,.settings-chip-move:disabled,.settings-cat-delete:disabled{opacity:.6;cursor:wait}
.settings-form-actions button:disabled{cursor:default}
.settings-row-actions .danger,.settings-cat-delete{border-color:#e6c2c6;color:#b3495a}
.settings-row-actions .danger:hover,.settings-cat-delete:hover{border-color:#d49aa3;background:#fdf5f6}
.settings-add{width:100%;border-style:dashed;border-color:#c3d3ea;color:#4169d8;background:#f7fafe;font-size:var(--fs-12,12px)}
.settings-form,.settings-cat-form{display:grid;gap:12px;padding:16px;border:1px solid #dbe5f2;border-radius:11px;background:var(--ui-surface-subtle,#f8fafd)}
.settings-form>b,.settings-cat-form>b{color:#30465f;font-size:var(--fs-13,13px)}
.settings-form label,.settings-cat-form label{display:grid;gap:6px;color:#60758e;font-size:var(--fs-11,11px);font-weight:600}
.settings-form input,.settings-form select,.settings-cat-form input{width:100%;min-width:0;height:var(--ui-control-height,36px);padding:8px 10px;border:1px solid #d7e1ef;border-radius:var(--ui-control-radius,8px);background:#fff;color:#304c70;font-family:inherit;font-size:var(--fs-12,12px);outline:none}
.settings-form input:focus-visible,.settings-form select:focus-visible,.settings-cat-form input:focus-visible{border-color:#7ba0e2;box-shadow:0 0 0 3px #3f6ed621}
.settings-preview{display:flex;align-items:center;gap:11px}
.settings-preview small{color:#8592a5;font-size:var(--fs-10,10px)}
.settings-form-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:2px}
/* 全局 .primary 是 flex:2（style.css），在这一行里会把「保存」拉满、把「取消」挤成一条 */
.settings-form-actions button{flex:0 0 auto;min-width:88px}
.settings-form-actions .primary{border-color:#3d63b7;background:var(--ui-primary,#416fd1);color:#fff}
.settings-form-actions .primary:hover{background:var(--ui-primary-hover,#315fbe)}
.settings-form-actions .primary:disabled{opacity:.6;cursor:default}
/* 左栏分类 / 右栏详情。窄一点给左栏，右栏是弹性的一列（minmax(0,1fr) 才能让它里面的
   长名字不把整行撑破）。 */
.settings-cats{display:grid;grid-template-columns:minmax(170px,210px) minmax(0,1fr);gap:16px;align-items:start}
.settings-cat-pane{display:grid;gap:8px;min-width:0}
.settings-cat-list{margin:0;padding:0;list-style:none;display:grid;gap:4px}
.settings-cat-row{position:relative;display:grid;grid-template-columns:auto auto minmax(0,1fr) auto auto;align-items:center;gap:7px;padding:7px 8px;border:1px solid #e4eaf2;border-radius:9px;background:#fff;cursor:pointer}
.settings-cat-row:hover{border-color:#c9d8ee;background:#f8fbff}
.settings-cat-row.is-active{border-color:#c3d5f4;background:#eef4fe}
.settings-cat-row.is-drop-into{border-color:#7ba0e2;background:#e8f0fe;box-shadow:0 0 0 2px #3f6ed62e}
.settings-cat-row.is-drop-before::before,.settings-cat-row.is-drop-after::after{content:'';position:absolute;left:2px;right:2px;height:2px;border-radius:2px;background:#4b78d4}
.settings-cat-row.is-drop-before::before{top:-3px}
.settings-cat-row.is-drop-after::after{bottom:-3px}
.settings-cat-grip{color:#b9c6d8;font-size:var(--fs-11,11px);line-height:1;cursor:grab}
.settings-cat-icon{display:grid;place-items:center;flex:none;width:26px;height:26px;border:1px solid #e2ebf9;border-radius:8px;background:#f2f7ff;color:#527bc7}
.settings-cat-icon :deep(svg){width:15px;height:15px}
.settings-cat-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#2d4564;font-size:var(--fs-12,12px);font-weight:650}
.settings-cat-count{color:#8592a5;font-size:var(--fs-10,10px);line-height:1}
.settings-cat-move{display:flex;gap:2px}
.settings-cat-move button{display:grid;place-items:center;width:22px;height:22px;padding:0;border:1px solid transparent;border-radius:6px;background:transparent;color:#8296ad;font-size:var(--fs-11,11px);line-height:1;cursor:pointer}
.settings-cat-move button:hover:not(:disabled){border-color:#dae3f0;background:#fff;color:#4266b9}
.settings-cat-move button:disabled{opacity:.3;cursor:default}
.settings-cat-detail{display:grid;gap:12px;min-width:0}
.settings-field-label{display:block;margin:0 0 7px;color:#60758e;font-size:var(--fs-11,11px);font-weight:600}
.settings-icon-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(38px,1fr));gap:6px}
.settings-icon-choice{display:grid;place-items:center;height:36px;padding:0;border:1px solid #dbe5f2;border-radius:9px;background:#fff;color:#5b7cae;cursor:pointer}
.settings-icon-choice:hover{border-color:#b8cdea}
.settings-icon-choice.active{border-color:#7ba0e2;background:#eef4fe;color:#3a63b5;box-shadow:0 0 0 2px #3f6ed61f}
.settings-icon-choice :deep(svg){width:18px;height:18px}
.settings-hint{margin:0;color:#8592a5;font-size:var(--fs-11,11px);line-height:1.8}
.settings-chip-empty{margin:0;padding:12px;border:1px dashed #d7e1ef;border-radius:9px;color:#8290a3;font-size:var(--fs-11,11px);line-height:1.8;text-align:center}
.settings-chips{display:flex;flex-wrap:wrap;gap:7px}
.settings-chip{display:inline-flex;align-items:center;gap:7px;max-width:100%;padding:4px 7px 4px 6px;border:1px solid #dbe5f2;border-radius:9px;background:#fff;color:#31465f;font-size:var(--fs-12,12px);font-weight:650;cursor:grab;overflow-wrap:anywhere}
.settings-chip:hover{border-color:#c9d8ee;background:#f8fbff}
.settings-chip-icon{display:grid;place-items:center;flex:none;width:22px;height:22px;border:1px solid #e2ebf9;border-radius:7px;background:#f2f7ff;color:#527bc7;font-size:var(--fs-11,11px);font-weight:650;line-height:1}
.settings-chip-icon :deep(svg){width:14px;height:14px}
.settings-chip-move{min-height:26px;padding:3px 8px}
.settings-chip-select{height:26px;max-width:130px;padding:0 6px;border:1px solid #c9d8ee;border-radius:7px;background:#fff;color:#31465f;font-family:inherit;font-size:var(--fs-11,11px)}
@media(max-width:560px){
  .settings-head{padding:16px 16px 13px}
  .settings-tabs{padding:10px 16px 0}
  .settings-body{padding:16px}
  .settings-list li{grid-template-columns:34px minmax(0,1fr);gap:10px}
  .settings-row-actions{grid-column:1/-1;justify-content:flex-end}
  /* 两栏改上下堆叠：390 宽下拖拽基本用不了，chip 上的「移动到…」才是主要交互。 */
  .settings-cats{grid-template-columns:minmax(0,1fr)}
  .settings-cat-list{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
}
</style>
