<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,reactive,ref,watch} from 'vue'
import axios from 'axios'
import {renderMarkdown} from './markdown'
import PyCodeEditor from './PyCodeEditor.vue'

const props=defineProps({confirmAction:Function})
const emit=defineEmits(['notify'])
const notify=(text,type='info')=>emit('notify',text,type)
const errorOf=e=>typeof e.response?.data?.detail==='string'?e.response.data.detail:e.message||'操作失败'

/* 状态文案一律取自服务端：前端再写一份中文映射，迟早会出现「已完成」与「完成」两种说法，
   而任务列表、报告与日志里说的应该是同一件事。 */
const meta=ref(null)
const statusText=s=>meta.value?.status_text?.[s]||s
const fileStatusText=s=>meta.value?.file_status_text?.[s]||s

// ==================================================================== 步骤折叠
/* 七步表单堆在一列里，光是滚到底就够烦的。手风琴：一次只展开一步，步骤头常驻并显示完成
   摘要（几个文件、几个字段、几条规则），点标题或顶部导航切换。
   正文用 `v-show` 而不是 `v-if`：收起时控件仍然挂载，已填的值不会因为折叠丢掉，测试也能对
   隐藏的 input 直接 `set_input_files`。
   只有用户自己点才切换 —— 动作成功后自动把正在读的那一步收起来，比不收更烦人。 */
const STEPS=[
  {key:'upload',title:'数据来源'},
  {key:'mapping',title:'表头映射'},
  {key:'ops',title:'清洗规则'},
  {key:'generate',title:'字段生成'},
  {key:'limits',title:'约束与回退'},
  {key:'check',title:'校验与预览'},
  {key:'run',title:'执行'},
]
const openStep=ref('upload')
const allOpen=ref(false)
const isOpen=key=>allOpen.value||openStep.value===key
function toggleStep(key){
  if(allOpen.value){allOpen.value=false;openStep.value=key;return}
  openStep.value=openStep.value===key?'':key
}
function openAt(key){allOpen.value=false;openStep.value=key}
/* 步骤头右侧的一句话摘要。空串 = 这一步还没做，序号圆点据此保持灰色。 */
function stepSummary(key){
  const constraints=fields.value.reduce((total,field)=>total+(field.constraints?.length||0),0)
  return {
    upload:sourceMode.value==='upload'
      ?(uploadInfo.value?.count?`${uploadInfo.value.count} 个文件`:'')
      :(pathList.value.length?`${pathList.value.length} 个路径`:''),
    mapping:fields.value.length?`${fields.value.length} 个字段`:'',
    ops:(ops.value.length||functions.value.length)?`${ops.value.length} 条规则 · ${functions.value.length} 个函数`:'',
    generate:llmFields.value.length?`${llmFields.value.length} 个大模型字段`:'',
    limits:constraints?`${constraints} 条约束`:'',
    check:dry.value?(dry.value.ok?'试跑通过':'试跑失败'):'',
    run:estimate.value?'已获取预估':'',
  }[key]||''
}

// ==================================================================== 数据来源
const sourceMode=ref('upload')
const uploadBusy=ref(false),uploadInfo=ref(null),uploadProgress=ref('')
const serverPaths=ref(''),browse=ref(null),browsePath=ref(''),browseBusy=ref(false)
const fileInput=ref(),dirInput=ref()
// 「选择文件」的 input 只是给系统对话框一个过滤提示；后端才是真校验（扩展名 + zip 展开）。
const FILE_ACCEPT=computed(()=>[(meta.value?.extensions||[]).join(',')||'.csv,.xlsx,.jsonl,.parquet','.zip'].join(','))

const sourceReady=computed(()=>sourceMode.value==='upload'
  ?!!uploadInfo.value?.count
  :!!pathList.value.length)
const pathList=computed(()=>serverPaths.value.split('\n').map(item=>item.trim()).filter(Boolean))

async function pickFiles(event){
  const files=[...event.target.files||[]]
  if(!files.length)return
  uploadBusy.value=true;uploadProgress.value=`正在上传 ${files.length} 个文件…`
  try{
    const data=new FormData()
    // webkitdirectory 给的是相对目录结构的路径，后端按它还原目录树
    data.append('relative_paths',JSON.stringify(files.map(file=>file.webkitRelativePath||file.name)))
    for(const file of files)data.append('files',file)
    uploadInfo.value=(await axios.post('/api/clean/uploads',data)).data
    await refreshMeta()   // 剩余空间变了：这一次上传刚占掉一块
    notify(`已上传 ${uploadInfo.value.count} 个文件（${formatSize(uploadInfo.value.bytes)}），数据盘剩余 ${formatSize(meta.value?.storage?.free_bytes)}。`,'success')
    await doInspect()
  }catch(e){notify(errorOf(e),'error')}finally{uploadBusy.value=false;uploadProgress.value=''}
}
async function dropUpload(){
  if(!uploadInfo.value)return
  try{await axios.delete(`/api/clean/uploads/${uploadInfo.value.id}`)}catch(e){/* 目录已经不在了也无妨 */}
  // `inspectFile` 是上一次探测**选中**的那个文件的 key，它跟着被删掉的上传一起失效。
  // 不清它，之后再传一批文件时 `doInspect` 会把旧 key 发上去，服务端回一句「输入里没有
  // 这个文件」，界面只剩一条提示、第 2 步永远空着 —— 看起来像「探测按钮坏了」。
  uploadInfo.value=null;inspectResult.value=null;inspectFile.value=''
  await refreshMeta()   // 删掉的那份还回盘了
  // 两个 input 都要清空：不清的话，移除之后再选**同一个**文件不会触发 change 事件，
  // 界面看起来像「点了没反应」。
  if(fileInput.value)fileInput.value.value=''
  if(dirInput.value)dirInput.value.value=''
}
async function loadBrowse(path=''){
  browseBusy.value=true
  try{browse.value=(await axios.get('/api/clean/browse',{params:{path}})).data;browsePath.value=browse.value.path}
  catch(e){notify(errorOf(e),'error')}finally{browseBusy.value=false}
}
function addServerPath(path){
  if(pathList.value.includes(path))return
  serverPaths.value=[...pathList.value,path].join('\n')
}

// ==================================================================== 输入选项
const inputOpts=reactive({delimiter:'',encoding:'',header_row:0,precount:false,recursive:true,sanitize_formula:true})
const inspectResult=ref(null),inspectBusy=ref(false),inspectFile=ref('')
// 表头映射那一步的实时预览：要几行、以及「当前这份样本是按哪套选项探出来的」。
// 后者防的是自触发 —— 探测会把探到的分隔符/编码回填进输入框，没有这个哨兵就会再探一遍。
const inspectRows=ref(20)
let inspectAnswered=''
const delimiterHint=computed(()=>{
  const item=inspectResult.value
  if(!item?.delimiter)return''
  const low=Math.min(item.delimiter_confidence??1,item.encoding_confidence??1)
  return low<0.8?'探测结果不太确定，请核对分隔符与编码后再继续。':''
})
const inspectKey=()=>`${inspectFile.value}\u0000${inputOpts.delimiter}\u0000${inputOpts.encoding}`
let inspectTimer=0,inspectPending=false,inspectShow=false
/* 发一次探测，拿到结果就走。`inspectFile` 记的是**上一次探测选中**的文件 key，而 key 是按来源
   算的（上传模式是 `in/a.csv` 这种相对路径、服务器路径模式是另一套根目录下的相对路径）—— 换了
   来源、改了路径列表、或重传了一批文件之后，那个 key 就不在新输入里了。带着它去探测，服务端只会
   回一句「输入里没有这个文件」，界面上看起来像「探测按钮坏了」（`dropUpload` 的注释记的是同一
   个坑，只是触发点不同）。所以被拒时清掉旧 key 重试一次，让服务端自己挑第一个文件 —— 与
   `loadPreview` 里那条重试同一个道理。 */
async function inspectOnce(){
  const body={config:buildConfig(),rows:inspectRows.value}
  try{
    return (await axios.post('/api/clean/inspect',{...body,file:inspectFile.value})).data
  }catch(e){
    // 只认 404（服务层的 `CleanNotFound`）：那正是「这个 key 不在这次输入里」那一句。别的错误
    // （路径被拒 400、没有可处理的文件 400）重试一遍只会得到同一条提示，用户平白多挨一次报错。
    if(!inspectFile.value||e?.response?.status!==404)throw e
    return (await axios.post('/api/clean/inspect',{...body,file:''})).data
  }
}
/* `show=true` = 用户明确说「我要看数据」（点「探测表头」「刷新预览」、换预览文件或行数）：
   探完把结果区切到「源数据」。600ms 防抖的自动重探不传 —— 停在任务结果页打字改分隔符时，
   面板不该自己跳走。 */
async function doInspect(show=false){
  if(!sourceReady.value)return
  inspectShow=inspectShow||show
  if(inspectBusy.value){inspectPending=true;return}   // 正在探：跑完补一次，别静默丢掉这次刷新
  inspectBusy.value=true
  try{
    const data=await inspectOnce()
    inspectResult.value=data
    inspectFile.value=data.key||''
    if(!inputOpts.delimiter)inputOpts.delimiter=data.delimiter||''
    if(!inputOpts.encoding)inputOpts.encoding=data.encoding||''
    if(!fields.value.length&&data.headers?.length)mapFromHeaders()
    inspectAnswered=inspectKey()   // 这份样本对应的就是这组选项，回填不再触发重新探测
    // 切来源放在数据到手**之后**：在那之前「源数据」还不可用，提前设会被上面的兜底 watch 弹回去。
    if(inspectShow){inspectShow=false;showSource('raw')}
  }catch(e){
    // 这次没探成（路径被拒、文件读不了）：把「我要看数据」的意图一起丢掉。留着它的话，用户之后
    // 随手改分隔符触发的那次自动重探会**自作主张**把面板跳到源数据 —— 那正是上面那条防抖规则
    // 要避免的事（用户以为自己只是在打字）。
    inspectShow=false
    notify(errorOf(e),'error')
  }finally{
    inspectBusy.value=false
    // 补的那一次也要看哨兵：切换文件时 `@change` 与这条 watch 会同时触发，慢格式（xlsx、
    // parquet）下第一次还没回来就排了第二次 —— 不比一下就会白发一遍请求。
    if(inspectPending){inspectPending=false;if(inspectKey()!==inspectAnswered)doInspect()}
  }
}
// 「实时」：改了分隔符/编码、或换了一个文件看，就重新探一次。防抖是因为这几个是输入框，
// 而且重探对大文件（xlsx 要开工作簿、parquet 要扫行数）不是免费的。
watch([()=>inputOpts.delimiter,()=>inputOpts.encoding,inspectFile],()=>{
  if(!sourceReady.value||inspectKey()===inspectAnswered)return
  clearTimeout(inspectTimer)
  inspectTimer=setTimeout(()=>{if(inspectKey()!==inspectAnswered)doInspect()},600)
})
onBeforeUnmount(()=>clearTimeout(inspectTimer))
const headers=computed(()=>inspectResult.value?.headers||[])
const sampleRows=computed(()=>inspectResult.value?.sample_rows||[])
const inspectFiles=computed(()=>inspectResult.value?.files||[])
// ==================================================================== 字段映射
const fields=ref([])
// 每个键都先摆好：模板里的 `field.generate.params.locale` 读的是 `params` 的属性，
// 而 `params` 本身是 undefined 时那不是「空值」而是 TypeError，整页直接白。
const newField=()=>({dest:'',source:'',constraints:[],fallback:null,fallbackEnabled:false,
  generate:{kind:'none',generator:'',params:{},prompt:'',system:'',batch_size:1,
            start:1,step:1,width:0,expression:''}})
const unmappedDest=computed(()=>fields.value.filter(item=>!item.dest.trim()).length)
function mapFromHeaders(){
  fields.value=headers.value.map(name=>({...newField(),dest:name,source:name}))
}
function addField(){fields.value.push(newField())}
function removeField(index){fields.value.splice(index,1)}
function usedSource(column,index){
  return fields.value.some((item,position)=>position!==index&&item.source===column)
}
// 「只填目标字段」= 新列。这是需求里的一条规则，界面上必须写出来而不是让人猜。
const isNewColumn=item=>!!item.dest.trim()&&!item.source
// 源列 → 引用它的目的字段（一个源列被两个字段引用是允许的，所以是数组）。
// 实时预览靠它标「未映射」，与第 6 步的「删除」同义：没有字段引用的源列不进产物。
const sourceMap=computed(()=>{
  const used={}
  for(const field of fields.value){
    const name=(field.source||'').trim()
    if(name)used[name]=[...(used[name]||[]),field.dest.trim()||'(未命名)']
  }
  return used
})
const mappedTo=name=>sourceMap.value[name]||[]
/* 只填了目标字段的「新列」：源文件里没有这一列，产物里才有。表头映射的右侧预览原本只列得出
   文件里的列，新列会整个消失 —— 而「我加了哪几列」正是这一步要看得见的东西。 */
const newColumns=computed(()=>fields.value.filter(isNewColumn).map(item=>item.dest.trim()))

// ==================================================================== 清洗规则
const OPS=[
  {op:'trim',label:'去首尾空白',field:true},
  {op:'lower',label:'转小写',field:true},
  {op:'upper',label:'转大写',field:true},
  {op:'nfkc',label:'全角转半角',field:true},
  {op:'replace',label:'子串替换',field:true,pattern:true,replacement:true},
  {op:'regex_replace',label:'正则替换',field:true,pattern:true,replacement:true},
  {op:'fill_null',label:'空值填充',field:true,value:true},
  {op:'ffill',label:'向下填充',field:true},
  {op:'cast',label:'类型转换',field:true,valueKind:['int','float','str','date','datetime']},
  {op:'date_format',label:'日期格式化',field:true,dateFormat:true},
  // 目标类型是**必填**（服务端 `number_format 必须提供 value_kind`）：它决定小数位留空时
  // 取整还是保留原样。这里漏了它，界面上就没有这个下拉，用户配到这条规则必被服务端拒掉。
  {op:'number_format',label:'数值格式化',field:true,valueKind:['float','int'],decimals:true,thousands:true},
  {op:'slice',label:'截取子串',field:true,slice:true},
  {op:'concat',label:'合并多列',fields:true,dest:true,separator:true},
  {op:'dedupe',label:'按列去重',fields:true,scope:true},
  {op:'drop_null',label:'删除空值行',fields:true},
]
const ops=ref([])
const opSpec=op=>OPS.find(item=>item.op===op)
function ensureOp(op){
  // 目标类型不是每个算子都有，切换算子时旧值也可能不在新算子的候选里（`str` → 数值格式化的
  // `float/int`）。留着它就是一个渲染成空白、却是必填的下拉 —— 提交时才报错。
  const spec=opSpec(op.op)
  if(spec?.valueKind&&!spec.valueKind.includes(op.value_kind))op.value_kind=spec.valueKind[0]
}
function addOp(){ops.value.push({op:'trim',field:fields.value[0]?.dest||'',pattern:'',replacement:'',value:'',value_kind:'str',date_format:'',decimals:2,thousands:false,fields:[],dest:'',separator:'',scope:'file'})}
function removeOp(index){ops.value.splice(index,1)}
/* 卡片头部的那一句摘要。多张规则卡片外观完全一样时，「第几条、在做什么」只能靠它区分 ——
   所以它必须跟着当前取值走：只重复一遍算子名（下拉里已经写着）等于没写。 */
function opSummary(op){
  const spec=opSpec(op.op)
  const parts=[]
  if(spec?.fields){
    parts.push(op.fields?.length?`字段：${op.fields.join('、')}`:'还没选字段')
  }else if(spec?.field){
    parts.push(op.field?`字段：${op.field}`:'还没选字段')
  }
  if(['replace','regex_replace'].includes(op.op)&&op.pattern){
    parts.push(`「${op.pattern}」→「${op.replacement||''}」`)
  }
  if(op.op==='fill_null')parts.push(`空值填「${op.value||''}」`)
  if(op.op==='cast')parts.push(`转成 ${op.value_kind}`)
  if(op.op==='date_format')parts.push(`格式 ${op.date_format||'（未填）'}`)
  if(op.op==='number_format')parts.push(`${op.value_kind==='int'?'取整':'保留小数'}${op.thousands?' · 千分位':''}`)
  if(op.op==='slice')parts.push(`第 ${op.start===''||op.start==null?0:op.start}–${op.end===''||op.end==null?'末尾':op.end} 个字符`)
  if(op.op==='concat')parts.push(`合并到「${op.dest||'（未填）'}」`)
  if(op.op==='dedupe')parts.push(op.scope==='task'?'整个任务内去重':'每个文件内去重')
  if(op.op==='drop_null')parts.push('任一为空即删掉整行')
  return parts.join(' · ')
}
/* 多选字段改成勾选块后，数组由这里维护：勾上追加、取消删掉。
   **不要再给同一个 checkbox 加 `v-model`** —— 两个监听器会各改一次数组，
   整列模式（只允许 1 个输入字段）会因此留下 2 个，被服务端直接拒掉。 */
function toggleField(list,name){
  const at=list.indexOf(name)
  if(at>=0){list.splice(at,1);return}
  list.push(name)
}

// ==================================================================== 自定义 Python 函数
/* 声明式规则之外的逃生口：函数在隔离子进程里跑（见后端 clean_worker 的 docstring ——
   那是**健壮性**边界，不是安全边界）。界面上有两件事必须说准，否则用户会写出注定失败
   的函数：
   1. 函数只看得见「输入字段」里勾中的列（后端传的就是 `dict(zip(input_fields, 值))`），
      没勾就什么都看不到 —— 不是「拿到整行然后自己挑」。
   2. 写回的字段必须是第 2 步已声明的目的字段，写别的会被静默丢掉（后端校验会直接拒绝
      配置，所以这里用同一份 `identityFields` 做选项）。 */
const pyQuote=name=>JSON.stringify(String(name))
/* 默认源码骨架用第一个已声明的目的字段名，而不是写死的「姓名」：用户拿到手的第一步
   是「把它改成我的字段名」，能省掉这一步就省掉。 */
function pyDefaultSource(){
  const name=identityFields.value[0]||'姓名'
  return [
    'def transform(row):',
    `    # row 里只有「输入字段」勾中的列，例如 {${pyQuote(name)}: " 张三 "}`,
    '    # 返回 {"写回字段": 新值}；返回 None 表示这一行不改',
    `    value = str(row.get(${pyQuote(name)}, ""))`,
    `    return {${pyQuote(name)}: value.strip()}`,
  ].join('\n')
}
/* 例子里的字段名跟着第 2 步声明的目的字段走：粘贴进源码框就能跑，不用先把「姓名」改成
   自己数据里的名字。一个字段都没声明时退回占位名，并在提示里说清要先声明字段。 */
const pySamples=computed(()=>{
  const first=identityFields.value[0]||'姓名'
  const second=identityFields.value[1]||first
  const named=identityFields.value.length>0
  const warn=named?'':'（下面的字段名是占位名：先在第 2 步声明字段，这里会自动换成你的字段名）'
  return [
    {
      title:'例 1：逐行清洗一个字段',
      note:`最常用的一种：一行进、一行出。这里把 ${first} 的首尾空白去掉。${warn}`,
      mode:'row',inputs:[first],outputs:[first],
      source:[
        'def transform(row):',
        `    value = str(row.get(${pyQuote(first)}, ""))`,
        '    if not value:',
        '        return None              # 空的就不动它，交给约束去判断',
        `    return {${pyQuote(first)}: value.strip()}`,
      ].join('\n'),
    },
    {
      title:'例 2：按条件跳过某些行（返回 None）',
      note:`返回 None 的一行原样保留：${second} 不满足条件时不动它，让后面的约束去报违规。${warn}`,
      mode:'row',inputs:[second],outputs:[second],
      source:[
        'def transform(row):',
        `    value = str(row.get(${pyQuote(second)}, ""))`,
        '    if len(value) < 4:',
        '        return None              # 这一行不改，保持原值',
        '    cleaned = value.replace(" ", "").replace("-", "")',
        `    return {${pyQuote(second)}: cleaned}`,
      ].join('\n'),
    },
    {
      title:'例 3：整列一起改（column 模式）',
      note:`整列进出：拿到 ${first} 的一整列，返回等长的一列。「输入字段」只能勾 1 个。${warn}`,
      mode:'column',inputs:[first],outputs:[first],
      source:[
        'def transform(columns):',
        `    values = columns.get(${pyQuote(first)}, [])`,
        '    # 返回的列表必须与输入等长（列表推导天然等长，别用 filter 改变长度）',
        `    return {${pyQuote(first)}: [str(v).strip() for v in values]}`,
      ].join('\n'),
    },
  ]
})
const functions=ref([])
/* `uid` 是给 `:key` 用的稳定标识，`opened` 记录这张卡片有没有被展开过。
   两者都不能少：
   - 列表原来用 `:key="index"`，删掉第 1 张卡片会让第 2 张的组件实例被第 3 个函数的数据
     「认领」—— 编辑器会带着**上一个函数的撤销栈**，用户按 Ctrl+Z 撤掉的是别人的代码；
   - 编辑器要等到卡片展开时才挂载：卡片是 `<details>`，步骤体又是 `v-show`，在
     `display:none` 里初始化量到的尺寸全是 0。等真展开了再建，CM 就总是在有尺寸的容器里
     初始化，也不会为用户没打开过的卡片付代价。 */
function addFunction(){
  functions.value.push({uid:`fn-${Math.random().toString(36).slice(2,10)}`,
    name:'',phase:'pre',mode:'row',input_fields:[],output_fields:[],
    timeout_s:5,on_row_error:'empty',source:pyDefaultSource(),showGuide:false,
    opened:false,testRows:20,test:null,testBusy:false,formatBusy:false})
}
/* `<details>` 的 toggle 事件**不冒泡**，所以处理器只能挂在元素自己身上（见模板）；
   `fn.opened` 一旦置真就不再收回 —— 折叠时把编辑器留着（`v-show` 式），撤销历史才在。 */
function markOpened(fn){fn.opened=true}
// ==================================================================== 源码的检查 / 格式化 / 试跑
const pyVocab=computed(()=>meta.value?.python||{builtins:[],modules:[]})
const pyEntry=computed(()=>pyVocab.value.entry||'transform')
const formatterReady=computed(()=>!!pyVocab.value.formatter?.available)
/* 检查接口挂过一次就不再每次都弹提示（按 400ms 防抖来调，一次故障能弹几十条）。
   但**第一次必须说出来**：用户那台后端如果是旧进程，这里是 404 —— 静默失败看起来就是
   「编辑器坏了」，而他不会想到要去重启服务。 */
let checkWarned=false
async function checkSource(source){
  try{
    return (await axios.post('/api/clean/functions/check',{source,entry:pyEntry.value})).data
  }catch(e){
    if(!checkWarned){checkWarned=true;notify(errorOf(e),'error')}
    return null
  }
}
async function formatFunction(fn){
  if(fn.formatBusy)return
  fn.formatBusy=true
  const before=fn.source
  try{
    const {data}=await axios.post('/api/clean/functions/format',{name:fn.name,source:before})
    if(!data.ok){notify(data.error||'格式化失败','error');return}
    // 等待期间用户又改了源码：这一次的结果已经对不上了，丢掉比默默覆盖好。
    if(fn.source!==before){notify('源码在格式化期间被改过，已放弃这次结果。','info');return}
    if(!data.changed){notify('没有需要改的地方。','info');return}
    fn.source=data.source
    fn.test=null            // 排版后过期的试跑结果别留着（内容一样，但文本变了）
    notify('已格式化（可以 Ctrl+Z 撤销）。','success')
  }catch(e){notify(errorOf(e),'error')}
  finally{fn.formatBusy=false}
}
async function runFunctionTest(fn,index){
  if(fn.testBusy)return
  fn.testBusy=true
  try{
    const {data}=await axios.post('/api/clean/functions/test',
      {config:buildConfig(),index,rows:Number(fn.testRows||20)})
    // 记下跑的是哪一版源码：之后用户一改，面板就要说明那些结果是上一版的。
    data.source=fn.source
    fn.test=data
    // 结果面板在结果区，这里记下是哪个函数并把来源切过去。「没有执行」「执行失败」也切：
    // 那两种结果同样要让人看见原因。窄屏（<1100px）下结果区在配置列**下面**，提示里别写「右侧」。
    fnTestId.value=fn.uid
    stageSource.value='fn'
    if(!data.ran)notify(data.error||'没有执行，请到「函数试跑」里看说明。','error')
    else if(!data.ok)notify(data.error||'函数执行失败，请到「函数试跑」里看说明。','error')
    else notify(`试跑完成：${data.sample.rows} 行，耗时 ${data.seconds} 秒。结果在「函数试跑」里。`,'success')
  }catch(e){notify(errorOf(e),'error')}
  finally{fn.testBusy=false}
}
const CELL_EMPTY='（空）'
const showValue=v=>v===''||v===null||v===undefined?CELL_EMPTY:String(v)
/* 逐行/整列两种模式共用一个单元格渲染器：一个对象的 `k=v`，或一个数组的值列表。 */
function cellText(value){
  if(Array.isArray(value))return value.map(showValue).join('，')
  return Object.entries(value||{}).map(([key,item])=>`${key}=${showValue(item)}`).join('，')
}
/* 「用这段」：模式与字段一起换掉，只换源码的话例子必然跑不通（比如 column 模式的例子被
   塞进 row 模式，或者字段没勾 → 后端读到的是一个空 row）。 */
function useSample(fn,sample){
  fn.mode=sample.mode
  fn.input_fields=[...sample.inputs]
  fn.output_fields=[...sample.outputs]
  fn.source=sample.source
  notify(sample.mode==='column'
    ?'例子已填入，并切到整列模式（输入字段只留 1 个）。'
    :'例子已填入，并设好了输入/写回字段。','success')
}
function removeFunction(index){functions.value.splice(index,1)}
/* 输入字段比写回字段多一层约束：整列模式只允许 1 个（服务端按这个校验，多的会被拒），
   逐行模式上限 8 个。超限时明说，而不是静默丢掉刚点的那一下。 */
function toggleFnInput(fn,name){
  const at=fn.input_fields.indexOf(name)
  if(at>=0){fn.input_fields.splice(at,1);return}
  if(fn.mode==='column'){fn.input_fields=[name];return}
  if(fn.input_fields.length>=8){notify('逐行模式最多选 8 个输入字段','info');return}
  fn.input_fields.push(name)
}
function functionPayload(fn,index){
  return {
    name:fn.name||`函数 ${index+1}`,
    phase:fn.phase,
    mode:fn.mode,
    source:fn.source||'',
    input_fields:[...(fn.input_fields||[])],
    output_fields:[...(fn.output_fields||[])],
    timeout_s:Number(fn.timeout_s||5),
    on_row_error:fn.on_row_error,
  }
}

// ==================================================================== 字段生成
// 生成器名单来自 `/meta`（后端 `generator_catalog()`），**不在前端再列一份**。前端列一份
// 的结局是界面上出现一个后端不认的选项 —— 一次注定失败的提交，而且报错离用户的操作很远。
const generators=computed(()=>meta.value?.generators||[])
const PARAM_KIND={values:'list',ext_word_list:'list',variable_nb_words:'bool',
  min:'int',max:'int',nb_words:'int',nb_sentences:'int',max_nb_chars:'int',
  min_chars:'int',max_chars:'int',format:'text',start:'text',end:'text'}
const PARAM_LABEL={values:'可选值',ext_word_list:'自定义词表',variable_nb_words:'允许词数浮动',
  min:'最小值',max:'最大值',nb_words:'词数',nb_sentences:'句子数',max_nb_chars:'最大字符数',
  min_chars:'最小长度',max_chars:'最大长度',format:'日期格式',start:'起始日期',end:'结束日期'}
const paramKind=name=>PARAM_KIND[name]||'text'
const paramsOf=field=>generators.value.find(item=>item.name===field.generate.generator)?.params||[]
const listText=(field,name)=>(field.generate.params[name]||[]).join('\n')
function setList(field,name,text){
  field.generate.params[name]=text.split('\n').map(item=>item.trim()).filter(Boolean)
}
function ensureGen(field){
  if(!field.generate.params)field.generate.params={}
  if(!field.generate.generator)field.generate.generator=generators.value[0]?.name||'name'
}
// 勾选框与 `fallback` 对象分开存：勾上时补一份默认策略，取消勾选时把 `enabled` 关掉。
// 只写「勾选」那半边的话，取消勾选后策略面板会留在屏幕上，而提交时它又按未启用处理 ——
// 屏幕上看到的和会跑起来的不是同一件事。
function toggleFallback(field,on){
  if(!field.fallback)field.fallback={enabled:false,on_violation:'retry',max_retries:2,default:''}
  field.fallback.enabled=!!on
}
const identityFields=computed(()=>fields.value.map(item=>item.dest).filter(Boolean))
const promptPreviewFor=field=>{
  const row={}
  const view=preview.value
  if(view?.columns){
    const index=view.columns.indexOf(field.dest)
    view.before?.forEach((values,position)=>{if(values[index]!==undefined&&values[index]!=='')row[view.columns[position]]=values[index]})
  }
  for(const name of identityFields.value)if(row[name]===undefined)row[name]='（示例）'
  return renderTemplate(field.generate?.prompt||'',row)
}
function renderTemplate(text,row){
  return String(text||'').replace(/\$\{([^}]+)\}/g,(all,body)=>{
    const [name,fallback]=String(body).split(':-')
    const value=row[String(name).trim()]
    if(value===undefined||value===null||value==='')return fallback!==undefined?fallback:''
    return String(value)
  })
}
function insertField(field,event,name){
  const box=event.target.closest('.clean-prompt').querySelector('textarea')
  const token='${'+name+'}'
  const start=box.selectionStart??(field.generate.prompt||'').length
  const end=box.selectionEnd??start
  const text=field.generate.prompt||''
  field.generate.prompt=text.slice(0,start)+token+text.slice(end)
  nextTick(()=>{box.focus();box.selectionStart=box.selectionEnd=start+token.length})
}

// ==================================================================== 模型池
const pool=ref([]),poolSentinel=ref('********'),poolBusy=ref(false),poolOpen=ref(false)
const draft=ref(null),testResult=ref(null),testBusy=ref(false)
async function loadPool(){
  try{const {data}=await axios.get('/api/clean/llm/endpoints');pool.value=data.endpoints||[];poolSentinel.value=data.sentinel||'********'}
  catch(e){notify(errorOf(e),'error')}
}
function editEndpoint(item){
  draft.value=item?JSON.parse(JSON.stringify(item)):{id:'',name:'',url:'',model:'',api_key:'',max_concurrency:4,weight:1,rpm:0,timeout_s:60,connect_timeout_s:5,max_retries:2,temperature:0.3,enabled:true}
  testResult.value=null
}
async function saveEndpoint(){
  const item=draft.value
  if(!item.url.trim()||!item.model.trim())return notify('端点地址与模型名都要填。','error')
  poolBusy.value=true
  try{
    const keep=pool.value.filter(existing=>existing.id!==item.id)
    const {data}=await axios.post('/api/clean/llm/endpoints',{endpoints:[...keep,item]})
    pool.value=data.endpoints||[];draft.value=null
    notify('模型端点已保存。','success')
  }catch(e){notify(errorOf(e),'error')}finally{poolBusy.value=false}
}
async function removeEndpoint(item){
  if(!await confirmAction(`删除模型端点“${item.name||item.model}”？`))return
  try{await axios.delete(`/api/clean/llm/endpoints/${item.id}`);await loadPool()}catch(e){notify(errorOf(e),'error')}
}
async function testEndpoint(item){
  testBusy.value=true;testResult.value=null
  try{
    const payload=item.id?{}:{endpoint:item}
    const {data}=item.id
      ?await axios.post(`/api/clean/llm/endpoints/${item.id}/test`,payload)
      :await axios.post('/api/clean/llm/endpoints/test',payload)
    testResult.value={...data,for:item.id||'draft'}
  }catch(e){testResult.value={ok:false,message:errorOf(e),for:item.id||'draft'}}finally{testBusy.value=false}
}
const llm=reactive({endpoint_ids:[],sample_rows:0,max_calls:50000,abort_error_rate:0.5})
const llmFields=computed(()=>fields.value.filter(item=>item.generate?.kind==='llm'))
const selectedEndpoints=computed(()=>pool.value.filter(item=>llm.endpoint_ids.includes(item.id)))
/* 并发上限 = Σ max_concurrency，**不含权重**。权重决定「谁来干」（比例），不决定「同时能干几件」，
   乘进去会得到一个比端点真实容量大的数字 —— 而这个数字与「处理预估」面板里后端算的那个
   （`total_concurrency()`，同样不含权重）会当场打架。 */
const concurrency=computed(()=>selectedEndpoints.value.filter(item=>item.enabled).reduce((total,item)=>total+(item.max_concurrency||1),0))

// ==================================================================== 输出
const output=reactive({format:'csv',csv_delimiter:',',layout:'union',on_missing:'empty',commit_rows:0,top_k:8})
/* 报告明细条数：空输入框按默认 8 走，**不能**当成 0 —— 0 的语义是「不要明细」，
   而清空输入框的用户想要的是「回到默认」，把这两件事混起来等于替他关掉明细。 */
function reportDetailCount(){
  const raw=output.top_k
  if(raw===''||raw===null||raw===undefined)return 8
  const value=Math.round(Number(raw))
  return Number.isFinite(value)?Math.min(1000,Math.max(0,value)):8
}

// ==================================================================== 组装配置
function constraintPayload(item){
  const base={kind:item.kind,severity:item.severity||'error'}
  if(item.kind==='enum')return{...base,values:item.values}
  if(item.kind==='regex')return{...base,pattern:item.pattern||'',mode:item.mode||'fullmatch'}
  if(item.kind==='length'){
    const payload={...base}
    if(item.max_length!==''&&item.max_length!==null&&item.max_length!==undefined)payload.max_length=Number(item.max_length)
    if(item.min_length!==''&&item.min_length!==null&&item.min_length!==undefined)payload.min_length=Number(item.min_length)
    return payload
  }
  if(item.kind==='range'){
    const payload={...base}
    if(item.min_value!==''&&item.min_value!==null&&item.min_value!==undefined)payload.min_value=Number(item.min_value)
    if(item.max_value!==''&&item.max_value!==null&&item.max_value!==undefined)payload.max_value=Number(item.max_value)
    return payload
  }
  if(item.kind==='format')return{...base,value_kind:item.value_kind||'email'}
  if(item.kind==='type')return{...base,value_kind:item.value_kind||'int'}
  if(item.kind==='unique')return{...base,unique_scope:item.unique_scope||'task'}
  return base
}
function fieldPayload(field){
  const spec={dest:field.dest.trim(),source:field.source}
  const kind=field.generate?.kind||'none'
  if(kind==='random'){
    // 空输入一律不传：`type=number` 清空后是 `''`，而 `int('')` 会在后端抛一个与用户
    // 操作毫无关系的 ValueError。不传就是走服务端默认值。
    const params={}
    for(const [key,value] of Object.entries(field.generate.params||{})){
      if(value===''||value===null||value===undefined)continue
      params[key]=value
    }
    spec.generate={kind:'random',generator:field.generate.generator,params}
  }else if(kind==='llm'){
    spec.generate={kind:'llm',prompt:field.generate.prompt||'',batch_size:Number(field.generate.batch_size||1)}
    if(field.generate.system)spec.generate.system=field.generate.system
  }else if(kind==='sequence'){
    spec.generate={kind:'sequence',start:Number(field.generate.start||1),step:Number(field.generate.step||1),width:Number(field.generate.width||0)}
  }else if(kind==='expression'){
    spec.generate={kind:'expression',expression:field.generate.expression||''}
  }
  const constraints=field.constraints.filter(item=>item.kind).map(constraintPayload)
  if(constraints.length)spec.constraints=constraints
  if(field.fallback?.enabled){
    const fallback={on_violation:field.fallback.on_violation,max_retries:Number(field.fallback.max_retries??2)}
    if(field.fallback.default!==''&&field.fallback.default!==undefined)fallback.default=field.fallback.default
    spec.fallback=fallback
  }
  return spec
}
function opPayload(op){
  const payload={op:op.op}
  if(op.field)payload.field=op.field
  if(opSpec(op.op)?.fields&&op.fields?.length)payload.fields=op.fields
  if(op.op==='concat'&&op.dest)payload.dest=op.dest
  if(['replace','regex_replace'].includes(op.op)){payload.pattern=op.pattern||'';payload.replacement=op.replacement||''}
  if(op.op==='fill_null')payload.value=op.value
  if(op.op==='ffill')payload.separator=''
  if(op.op==='cast')payload.value_kind=op.value_kind
  if(op.op==='date_format')payload.date_format=op.date_format
  if(op.op==='number_format'){
    payload.value_kind=op.value_kind
    // 小数位留空 ≠ 0 位。留空的语义是「按目标类型来」（int 取整 / float 保留原样），
    // 也就是后端 `decimals is None` 那一段；直接 `Number('')` 会变成 0 位，把它吃掉。
    payload.decimals=op.decimals===''||op.decimals===null||op.decimals===undefined
      ?null:Number(op.decimals)
    payload.thousands=!!op.thousands
  }
  if(op.op==='slice'){
    if(op.start!==''&&op.start!==null&&op.start!==undefined)payload.start=Number(op.start)
    if(op.end!==''&&op.end!==null&&op.end!==undefined)payload.end=Number(op.end)
  }
  if(op.op==='dedupe'){payload.keep='first';payload.scope=op.scope||'file'}
  return payload
}
function buildConfig(){
  const config={
    name:taskName.value||'',
    source:sourceMode.value==='upload'
      ?{mode:'upload',upload_id:uploadInfo.value?.id||''}
      :{mode:'server_path',paths:pathList.value},
    input:{header_row:Number(inputOpts.header_row||0),precount:!!inputOpts.precount,recursive:!!inputOpts.recursive,sanitize_formula:!!inputOpts.sanitize_formula},
    fields:fields.value.filter(item=>item.dest.trim()).map(fieldPayload),
    ops:ops.value.map(opPayload),
    functions:functions.value.map(functionPayload),
    output:{format:output.format,csv_delimiter:output.csv_delimiter||',',layout:output.layout,on_missing:output.on_missing,commit_rows:Number(output.commit_rows||0),top_k:reportDetailCount()},
    llm:{endpoint_ids:[...llm.endpoint_ids],sample_rows:Number(llm.sample_rows||0),max_calls:Number(llm.max_calls||0),abort_error_rate:Number(llm.abort_error_rate||0.5)},
  }
  if(inputOpts.delimiter)config.input.delimiter=inputOpts.delimiter
  if(inputOpts.encoding)config.input.encoding=inputOpts.encoding
  return config
}
const taskName=ref('')

// ==================================================================== 干跑与预估
const dry=ref(null),dryBusy=ref(false),dryRows=ref(200)
const estimate=ref(null),estimateBusy=ref(false),estimateSeen=ref(false)
const configReady=computed(()=>sourceReady.value&&fields.value.length>0&&!unmappedDest.value)
/* 干跑行数：界面上的数字框可以改（1–5000，与后端 DRY_RUN_MAX_ROWS 同一个上限）。
   空输入框按默认 200 走，不是 0 —— 0 行干跑等于什么都没验。 */
function dryRowCount(){
  const raw=dryRows.value
  const value=Math.round(Number(raw))
  if(raw===''||raw===null||raw===undefined||!Number.isFinite(value))return 200
  return Math.min(5000,Math.max(1,value))
}
async function runValidate(){
  if(!configReady.value)return notify('先准备数据来源并至少配置一个字段。','error')
  dryBusy.value=true
  try{
    // 参数名是 `rows`（后端 `validate` 读的就是它）。以前这里发的是 `sample_rows`，
    // 服务端根本不看 —— 界面上写着「200 行」，实际也永远是 200 行，改不了。
    const {data}=await axios.post('/api/clean/validate',{config:buildConfig(),rows:dryRowCount()})
    dry.value=data
    if(!data.ok)notify(data.error||'试跑失败，请看第 6 步的说明。','error')
    else{
      // 试跑完直接把结果区切到这次试跑的两栏对照：用户点「试跑」要看的正是「会变成什么样」
      stageSource.value='dry'
      stageTab.value='preview'
      // 不说「右侧」：窄屏（<1100px）时结果区是在配置列**下面**，说右侧就指错了地方。
      notify('试跑完成，「试跑预览」里可以看前几行的清洗前后对比与计数。','success')
    }
  }catch(e){notify(errorOf(e),'error')}finally{dryBusy.value=false}
}
async function runEstimate(){
  if(!configReady.value)return notify('先准备数据来源并至少配置一个字段。','error')
  estimateBusy.value=true
  try{
    estimate.value=(await axios.post('/api/clean/estimate',{config:buildConfig()})).data
    estimateSeen.value=true
    stageSource.value='estimate'   // 数字在结果区的「处理预估」里，点完就切过去
  }
  catch(e){notify(errorOf(e),'error')}finally{estimateBusy.value=false}
}
const estimateText=computed(()=>{
  const item=estimate.value?.estimate
  if(!item)return''
  // 服务端在给不出时间时把两个端点都留成 null（不是 0，也不是猜一个数），并在 notes 里
  // 写明原因 —— 这里就说清「按实际速率」，别再复述一遍原因。
  if(item.seconds_low===null||item.seconds_low===undefined)return'按运行时实际速率'
  return`约 ${duration(item.seconds_low)} – ${duration(item.seconds_high)}`
})
/* 「这个数是怎么来的」：面板上说清用的是哪次观测到的延迟。用户判断准不准全靠它 ——
   没有历史时后端按 2 秒保守估，这里就不显示这一句（notes 里已经写了）。
   一秒以下按毫秒显示：本地小模型的 300ms 走 `duration` 会被四舍五入成「0 秒」。 */
const estimateLatency=computed(()=>{
  const ms=estimate.value?.estimate?.avg_latency_ms||0
  if(ms<=0)return''
  return ms<1000?`${Math.round(ms)} 毫秒`:duration(ms/1000)
})

// ==================================================================== 任务
const tasks=ref([]),current=ref(null),busy=ref(false)
const sortedTasks=computed(()=>[...tasks.value].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))))
/* 刷新会并发发生（点完按钮一次、终态事件一次），而**响应不保证按发出顺序回来**。不挡这一下
   的话，先发出的那次晚回来就会用「还在跑」的旧快照盖掉刚拿到的终态 —— 界面于是停在
   「处理中」，直到下一次刷新为止。generation 是发出序号，回来的不是最新一次就丢掉。 */
let listGen=0
async function loadTasks(){
  const gen=++listGen
  try{
    const data=(await axios.get('/api/clean/tasks')).data.tasks||[]
    if(gen!==listGen)return
    tasks.value=data
    /* `selectTask` 存进 `current` 的是**列表里的那个对象**。换了数组之后旧对象就成了孤儿：
       它不再被后续的 SSE 更新命中（`applyEvent` 按 id 从新数组里找），详情面板会停在上一次
       的值上。所以刷新之后必须把 `current` 指回列表里的那一个。 */
    const same=current.value&&tasks.value.find(item=>item.id===current.value.id)
    if(same)current.value=same
  }catch(e){/* SSE 会补上 */}
}
async function submit(){
  if(!estimateSeen.value)return notify('请先在第 7 步获取预估，确认后再提交。','error')
  busy.value=true
  try{
    // 创建时回传干跑用的同一个种子：正式跑出来的随机字段与试跑时确认过的逐个相同
    const payload={config:buildConfig()}
    if(dry.value?.seed)payload.seed=dry.value.seed
    const {data}=await axios.post('/api/clean/tasks',payload)
    notify('任务已创建，可在下方查看进度与实时日志。','success')
    await loadTasks();await selectTask(data.id)
  }catch(e){notify(errorOf(e),'error')}finally{busy.value=false}
}
async function control(task,action){
  if(action==='delete'&&!await confirmAction(`删除“${task.name||task.id}”及其全部产物？`))return
  // 「暂停」只是让工作线程在下一个检查点停下，进程还活着 —— GB 级任务想真停下来只能取消。
  // 取消不删产物：已提交的部分留在磁盘上，「续跑」从提交点接着跑。所以这个确认框的
  // 文案必须说「保留 + 可续跑」，不能用共享弹窗默认的「删除后无法恢复」。
  if(action==='cancel'&&!await confirmAction(`取消“${task.name||task.id}”？`,{
    title:'确认取消任务',
    note:'已处理的部分会保留，之后可以续跑',
    confirmText:'确认取消',
  }))return
  try{
    if(action==='delete'){await axios.delete(`/api/clean/tasks/${task.id}`);if(current.value?.id===task.id)current.value=null}
    else await axios.post(`/api/clean/tasks/${task.id}/${action}`)
    await loadTasks()
  }catch(e){notify(errorOf(e),'error')}
}
async function selectTask(id){
  const found=tasks.value.find(item=>item.id===id)
  current.value=found||(await axios.get(`/api/clean/tasks/${id}`)).data
  stageTab.value='preview'
  stageSource.value='task'
  // `previewFile` 也要清：它记的是**上一个任务**的文件名，留着它新任务的首屏预览必然取不到
  logs.value=[];logSeen.value=0;reportMd.value='';stats.value=null;preview.value=null;previewFile.value=''
  await Promise.all([loadLogs(),loadPreview(),loadReport(),loadStats()])
  openTaskStream()
}
function progressOf(task){
  if(!task)return 0
  const total=Number(task.total||0),current=Number(task.current||0)
  if(!total)return Number(task.progress||0)
  return Math.max(Number(task.progress||0),Math.round(current/total*1000)/10)
}
function etaText(task){
  if(!task)return''
  // ETA 只在还会继续推进的状态下有意义。跑完后服务端把 eta_low/high 落到 0.0，
  // 照原样显示就是「剩余约 0 秒」—— 一个已经结束的任务不需要剩余时间。
  if(!['running','queued','paused'].includes(task.status))return''
  const low=task.eta_low,high=task.eta_high
  let text=''
  if(low!=null&&high!=null&&low>0)text=`剩余约 ${duration(low)} – ${duration(high)}`
  else if(task.rate)text=`速度 ${Math.round(task.rate)} 行/秒`
  if(task.unstable)text+='（速率不稳定）'
  return text
}

// ==================================================================== 舞台
const stageTab=ref('preview')
const STAGE_TABS=[
  {id:'preview',label:'数据预览'},
  // 试跑是一次性的：它没有日志流，也没有落盘的字段统计。这两个页签在试跑预览下置灰，
  // 而不是让用户点进去看一张空表再猜为什么是空的。
  {id:'logs',label:'运行日志',taskOnly:true},
  {id:'report',label:'回归报告'},
  {id:'stats',label:'字段统计',taskOnly:true},
]
/* 结果区显示哪一份数据。五个来源按「产生它的动作」排：raw = 探测表头后的源数据、
   fn = 函数试跑、dry = 刚才那次试跑、estimate = 提交前的预估、task = 某个任务的产物。
   左边只做配置、右边只做展示，所以源数据与试跑函数的结果也在这里，不再挤在配置列里。
   可用性**只看身份存在**（inspectResult / fn.test / dry.preview / estimate / current），
   不看载荷是否加载完 —— `selectTask` 先置 task 再 await 那几份数据，若按 !!preview 判定，
   面板会在加载中滑到别的来源、加载完再滑回来。 */
const SOURCES=[{id:'raw',label:'源数据'},{id:'fn',label:'函数试跑'},{id:'dry',label:'试跑预览'},
               {id:'estimate',label:'处理预估'},{id:'task',label:'任务结果'}]
const fnTestId=ref('')                     // 刚试跑过的那个函数（runFunctionTest 写入）
const stageFn=computed(()=>functions.value.find(fn=>fn.uid===fnTestId.value&&fn.test)||null)
const preview=ref(null),previewRows=ref(50),previewBusy=ref(false)
const dryPreview=computed(()=>dry.value?.preview||null)
const stageAvailable=id=>({raw:!!inspectResult.value,fn:!!stageFn.value,dry:!!dryPreview.value,
                           estimate:!!estimate.value,task:!!current.value})[id]||false
const stageSource=ref('raw')
/* 请求的来源不可用（任务被删、函数格式化后 `test` 被清）就退到第一个可用的，都不可用即空态。
   写回 watch 保证 `stageSource` 永远指向一个可用来源：不然「陈旧请求」会在数据到手的那一刻
   把面板从用户正看的地方拽走（例如停在任务结果页改分隔符，防抖重探让 raw 变可用）。 */
const shownSource=computed(()=>stageAvailable(stageSource.value)?stageSource.value
                            :(SOURCES.find(item=>stageAvailable(item.id))?.id||''))
watch(shownSource,value=>{if(value&&value!==stageSource.value)stageSource.value=value})
const switchSources=computed(()=>SOURCES.filter(item=>stageAvailable(item.id)))
// 四个页签只在「任务结果 / 试跑预览」下有意义：源数据、函数试跑、处理预估各自只有一块内容。
const showsTabs=computed(()=>shownSource.value==='task'||shownSource.value==='dry')
/* 来源开关是「数据预览」这一档的东西：切到运行日志/回归报告/字段统计，看的已经不是那五份
   数据，页签旁边再摆一排「源数据 / 函数试跑 / …」只会让人以为页签是开关的下一层。
   但源数据、函数试跑、处理预估下**没有页签**（showsTabs 为假），那时开关是唯一的出口，
   收起来就把这三个来源锁死了 —— 所以「没页签」同样要显示。 */
const showSwitcher=computed(()=>switchSources.value.length>1
                             &&(!showsTabs.value||stageTab.value==='preview'))
const viewingDry=computed(()=>shownSource.value==='dry')
function showSource(id){
  if(!stageAvailable(id))return
  stageSource.value=id
  if(id==='dry')stageTab.value='preview'
}
const shownPreview=computed(()=>viewingDry.value?dryPreview.value:(shownSource.value==='task'?preview.value:null))
const shownReport=computed(()=>viewingDry.value?dry.value?.report||'':(shownSource.value==='task'?reportMd.value:''))
const logs=ref([]),logSeen=ref(0),logLevel=ref('info'),follow=ref(true),logBox=ref(),gapNotice=ref('')
const reportMd=ref(''),reportBusy=ref(false)
const stats=ref(null),statsBusy=ref(false)
const LEVELS=[{value:'debug',label:'全部'},{value:'info',label:'信息'},{value:'warn',label:'告警'},{value:'error',label:'错误'}]

async function loadPreview(retry=true){
  if(!current.value)return
  previewBusy.value=true
  try{
    const {data}=await axios.get(`/api/clean/tasks/${current.value.id}/preview`,{params:{rows:previewRows.value,file:previewFile.value}})
    preview.value=data
    previewFile.value=data.file||''
  }catch(e){
    /* 换一个任务看结果时，`previewFile` 还留着**上一个任务**的文件名，而文件名是按任务解析的
       （上传模式是 `in/a.csv` 这样的键，服务器路径模式是完整路径）。服务端只会回一句「输入里
       没有这个文件」，界面上就一直停在「选一个文件查看前后对比」。所以拿空文件重试一次，
       让服务端自己挑第一个文件。 */
    if(retry&&previewFile.value){previewFile.value='';return loadPreview(false)}
    notify(errorOf(e),'error')
  }finally{previewBusy.value=false}
}
const previewFile=ref('')
function diffClass(before,after){
  const left=before===''||before===null||before===undefined
  const right=after===''||after===null||after===undefined
  if(left&&right)return''
  if(left&&!right)return'added'
  if(!left&&right)return'removed'
  return String(before)===String(after)?'':'changed'
}
/* 两栏的表头**不是同一份东西**，这一点必须让用户看得见：
   左栏是文件里的列名（`before_columns`），右栏是目的字段名（`columns`）。前
   `columns.length` 列一一对应（左 `i` 列就是右 `i` 列的源列），末尾多出来的那几列是
   **会被丢弃**的源列 —— 它们只在左栏出现，右栏（产物）里没有。
   拿目的字段名当左表头是个曾经的真 bug：重命名（source → dest）和被丢弃的列都看不出
   来，用户以为看的是自己的文件，其实看的是输出结构的投影。 */
const shownBeforeColumns=computed(()=>{
  const view=shownPreview.value
  if(!view)return[]
  // 读不出文件时服务端给不出左表头（那种情况一行数据都没有）—— 退回目的字段名，
  // 免得整张表连表头都没有
  return view.before_columns?.length?view.before_columns:(view.columns||[])
})
const droppedFrom=computed(()=>shownPreview.value?.columns?.length||0)
const droppedNames=computed(()=>shownBeforeColumns.value.slice(droppedFrom.value))
function isDroppedColumn(column){return column>=droppedFrom.value}
function isGeneratedColumn(column){
  return(shownPreview.value?.generated||[]).includes(shownPreview.value?.columns?.[column])
}
function syncScroll(event,other){
  const source=event.target
  const peer=source.closest('.clean-compare')?.querySelector(`.clean-pane[data-side="${other}"]`)
  if(peer)peer.scrollTop=source.scrollTop
}
async function loadLogs(){
  if(!current.value)return
  try{
    const {data}=await axios.get(`/api/clean/tasks/${current.value.id}/logs`,{params:{after:logSeen.value,limit:2000,level:logLevel.value}})
    if(data.events?.length){logs.value=[...logs.value,...data.events];logSeen.value=Math.max(logSeen.value,...data.events.map(item=>item.seq||0))}
    if(data.source==='file'&&!logs.value.length)logs.value=[]
    await scrollLogs()
  }catch(e){/* 日志拉不到不该打断页面 */}
}
async function loadReport(){
  if(!current.value)return
  reportBusy.value=true
  try{
    const {data}=await axios.get(`/api/clean/tasks/${current.value.id}/report`,{responseType:'text',transformResponse:value=>value})
    reportMd.value=typeof data==='string'?data:JSON.stringify(data)
  }catch(e){reportMd.value=''}finally{reportBusy.value=false}
}
async function loadStats(){
  if(!current.value)return
  statsBusy.value=true
  try{stats.value=(await axios.get(`/api/clean/tasks/${current.value.id}/stats`)).data}
  catch(e){stats.value=null}finally{statsBusy.value=false}
}
async function scrollLogs(){
  if(!follow.value)return
  await nextTick()
  if(logBox.value)logBox.value.scrollTop=logBox.value.scrollHeight
}
const visibleLogs=computed(()=>{
  const order={debug:0,info:1,warn:2,error:3}
  const floor=order[logLevel.value]??1
  return logs.value.filter(item=>(order[item.level]??1)>=floor)
})

// ==================================================================== SSE
/* `event:` 字段被服务端设成了事件类型（status/progress/log/...），所以 `onmessage`
   **一条都收不到** —— 必须按类型 addEventListener。这是最容易被误判成「后端没推」的坑。 */
let listStream=null,taskStream=null
/* `error` 也要听：任务级的失败原因（比如续跑被拒）是**普通事件**而不是日志行，
   不听它就只在 REST 补齐时才现身 —— 恰恰是用户最需要当场看到的那一条。 */
const KINDS=['status','progress','log','fallback','llm','report','resync','error']
/* 终态 = 不会自己再变的状态。到了这里就得重新拉一次列表，因为「续跑」按钮读的是服务端
   算出来的 `resume` 决策（输入是否被改过、提交点在哪），SSE 的 status 事件里没有它。 */
const TERMINAL=['completed','failed','cancelled','interrupted']
function applyEvent(item){
  const task=tasks.value.find(entry=>entry.id===item.task_id)
  const data=item.data||{}
  // 带状态的事件有两种：正常的 `status`，和 `error`（续跑被拒这类任务级原因也带 `status`）。
  const next=item.kind==='status'||item.kind==='error'?String(data.status||''):''
  if(task){
    if(next&&task.status!==next){
      // 只改 `status` 不改文案是不够的：徽章渲染的是 `status_text||statusText(status)`，
      // 而 `status_text` 由服务端 `public_task()` 算、SSE 里没有这个字段。留着它，取消之后
      // 徽章会一直写着「处理中」—— 屏幕上看到的与真实状态是两回事。
      task.status=next
      delete task.status_text
    }
    // 终态要重新拉一次列表：`resume` 是服务端按输入指纹与提交点算的，SSE 里没有它，不刷新
    // 的话「续跑」按钮永远不会出现（行里留下的是取消前那句「不能续跑：任务正在运行」）。
    if(next&&TERMINAL.includes(next))loadTasks()
    if(item.kind==='progress'){
      task.progress=Math.max(Number(task.progress||0),Number(data.pct??task.progress??0))
      if(data.done!=null)task.current=data.done
      if(data.total!=null)task.total=data.total
      task.eta_low=data.eta_low??null;task.eta_high=data.eta_high??null
      task.rate=data.rate??task.rate;task.unstable=!!data.unstable
    }
    if(item.message)task.message=item.message
  }else if(next&&item.task_id){
    loadTasks()
  }
  if(current.value?.id===item.task_id){
    if(item.kind==='progress'){
      current.value.progress=task?.progress??current.value.progress
      current.value.current=data.done??current.value.current
      current.value.total=data.total??current.value.total
      current.value.eta_low=data.eta_low??null
      current.value.eta_high=data.eta_high??null
      current.value.rate=data.rate??current.value.rate
      current.value.unstable=!!data.unstable
    }else if(next){
      current.value.status=next
      if(TERMINAL.includes(next)){
        // 报告/统计/预览是**跑完之后才存在**的：终态一到就取，别让用户对着空面板点刷新
        loadReport();loadStats();loadPreview()
      }
    }else if(item.kind==='report'){
      loadReport()
    }
  }
}
function onEvent(kind,event){
  let payload={}
  try{payload=JSON.parse(event.data||'{}')}catch(e){return}
  if(kind==='resync'){
    gapNotice.value=`日志中间有 ${payload.gap??'若干'} 条被省略，正在重新同步…`
    resyncLogs();return
  }
  if(kind==='log'||kind==='fallback'||kind==='llm'||kind==='error'){
    if(current.value?.id===payload.task_id){
      logs.value.push(payload)
      logSeen.value=Math.max(logSeen.value,payload.seq||0)
      scrollLogs()
    }
  }
  applyEvent(payload)
}
function attach(stream){
  for(const kind of KINDS)stream.addEventListener(kind,event=>onEvent(kind,event))
  return stream
}
function openListStream(){
  if(listStream)return
  listStream=attach(new EventSource('/api/clean/events'))
  listStream.onerror=()=>{/* EventSource 自己会重连，带上 Last-Event-ID */}
}
function openTaskStream(){
  if(taskStream)taskStream.close()
  if(!current.value){taskStream=null;return}
  gapNotice.value=''
  taskStream=attach(new EventSource(`/api/clean/tasks/${current.value.id}/events?after=${logSeen.value}`))
  taskStream.onerror=()=>{}
}
async function resyncLogs(){
  try{
    const {data}=await axios.get(`/api/clean/tasks/${current.value.id}/logs`,{params:{after:logSeen.value,limit:5000,level:logLevel.value}})
    for(const item of data.events||[]){
      if(logSeen.value<=(item.seq||0)){logs.value.push(item);logSeen.value=item.seq||logSeen.value}
    }
  }catch(e){/* 补齐失败就等下一次 */}
  if(!logs.value.length)await loadLogs()
  gapNotice.value=''
  await scrollLogs()
}

// ==================================================================== 工具
function formatSize(bytes){
  const value=Number(bytes||0)
  if(value<1024)return`${value} B`
  const units=['KB','MB','GB','TB']
  let size=value/1024,index=0
  while(size>=1024&&index<units.length-1){size/=1024;index+=1}
  return`${size.toFixed(1)} ${units[index]}`
}
// 实时预览的格子：空值写成「（空）」—— 一列全是空白时，看不出是「没数据」还是界面坏了。
// jsonl 的值可以是嵌套结构，转成 JSON 至少是能读的。
function cellShown(value){
  if(value===null||value===undefined||value==='')return'（空）'
  if(typeof value==='object')return JSON.stringify(value)
  return String(value)
}
function duration(seconds){
  const value=Math.max(0,Math.round(Number(seconds||0)))
  if(value<60)return`${value} 秒`
  if(value<3600)return`${Math.round(value/60)} 分钟`
  const hours=Math.floor(value/3600),minutes=Math.round(value%3600/60)
  return minutes?`${hours} 小时 ${minutes} 分`:`${hours} 小时`
}
function dateText(value){
  if(!value)return''
  const date=new Date(value)
  return Number.isNaN(date.getTime())?String(value):date.toLocaleString('zh-CN',{hour12:false})
}
// 百分比口径必须与报告一致（`clean_stats._pct` 是 `ratio*100` 保留两位），否则同一个
// 空值率在报告里是 `3.20%`、在统计表里是 `3.2%`，看起来像两个数。
function pct(ratio){
  if(ratio===null||ratio===undefined||ratio==='')return'—'
  const value=Number(ratio)
  return Number.isFinite(value)?`${(value*100).toFixed(2)}%`:'—'
}
const confirmAction=async(text,extra)=>props.confirmAction?props.confirmAction(text,undefined,extra):window.confirm(text)
function violated(field){
  const item=dry.value
  if(!item?.violations)return 0
  return Object.entries(item.violations).filter(([key])=>key.split('|')[0]===field).reduce((total,[,count])=>total+count,0)
}
function fallbackHits(field){
  const item=dry.value
  if(!item?.fallbacks)return 0
  return Object.entries(item.fallbacks).filter(([key])=>key.split('|')[0]===field).reduce((total,[,count])=>total+count,0)
}
/* 试跑预览里那张「各字段违规/回退」表要列出哪些字段：**从计数字典的键反推**，不遍历当前
   `fields` —— 干跑之后用户往往接着改字段，拿当前 fields 去查上一次干跑的计数，会把真违规
   过的字段漏掉（那些数字就再也看不到了）。数值仍走 violated()/fallbackHits()，口径只此一处。 */
const dryFieldRows=computed(()=>{
  const names=new Set()
  for(const key of Object.keys(dry.value?.violations||{}))names.add(key.split('|')[0])
  for(const key of Object.keys(dry.value?.fallbacks||{}))names.add(key.split('|')[0])
  return [...names].map(name=>({name,violations:violated(name),fallbacks:fallbackHits(name)}))
    .filter(row=>row.violations||row.fallbacks)
})
// 违规/回退计数是 `字段|违规类型`（回退多一段 `|策略`）→ 次数 的字典，界面上既要按字段
// 取一份，也要取总数。缺了这个函数模板里的 `sum(...)` 会解析成组件属性，报
// 「_ctx.sum is not a function」并把整块渲染掉 —— 表格里用得越多，炸得越彻底。
function sum(counters){
  if(!counters)return 0
  return Object.values(counters).reduce((total,count)=>total+Number(count||0),0)
}

// 数据盘剩余空间在 meta 里，所以传完/删完必须重取一次：不重取的话，用户看到的还是进这一页
// 时的那个数，传了 300GB 之后还显示「剩余 500GB」—— 而上传本身不再有大小上限，这个数就是
// 他判断「还能不能继续传」的唯一依据。
async function refreshMeta(){
  try{meta.value=(await axios.get('/api/clean/meta')).data}catch(e){notify(errorOf(e),'error')}
}
onMounted(async()=>{
  await refreshMeta()
  await Promise.all([loadTasks(),loadPool()])
  openListStream()
})
onBeforeUnmount(()=>{listStream?.close();taskStream?.close()})
</script>

<template>
  <section class="clean-page">
    <header class="clean-header"><div><span class="eyebrow">DATA CLEANING PIPELINE</span><h1>文件清洗工具</h1><p>上传 CSV / Excel 目录，按规则清洗与生成字段，跑完做回归验证并给出报告。</p></div><span class="clean-badge">断点续跑 · 实时日志 · 处理预估</span></header>

    <div class="clean-workspace">
      <aside class="panel clean-config" autocomplete="off" spellcheck="false">
        <!-- 七步导航：点一下直接跳到那一步。手风琴的当前步在下面高亮。 -->
        <div class="clean-steps">
          <button v-for="(step,index) in STEPS" :key="step.key" type="button" class="clean-step-pill"
                  :class="{active:openStep===step.key,done:!!stepSummary(step.key)}" @click="openAt(step.key)">
            <i>{{index+1}}</i><span>{{step.title}}</span>
          </button>
          <button type="button" class="clean-expand" @click="allOpen=!allOpen">{{allOpen?'收起全部':'全部展开'}}</button>
        </div>

        <section class="clean-step" :class="{open:isOpen('upload'),done:!!stepSummary('upload')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('upload')" @click="toggleStep('upload')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">1</i><b>数据来源</b>
            <small class="clean-step-sum">{{stepSummary('upload')}}</small>
          </button>
          <div v-show="isOpen('upload')" class="clean-step-body">
            <div class="clean-segmented">
              <button type="button" :class="{active:sourceMode==='upload'}" @click="sourceMode='upload'">上传文件</button>
              <button type="button" :class="{active:sourceMode==='server_path'}" @click="sourceMode='server_path'">服务器路径</button>
            </div>
            <template v-if="sourceMode==='upload'">
              <!-- 两个并排的按钮 + 两个藏起来的原生 input：`webkitdirectory` 会强制唤起系统目录
                   选择器，所以「选单个文件」必须有独立的一个 input（后端本来就吃单文件）。 -->
              <div class="clean-file">
                <div class="clean-file-actions">
                  <input ref="fileInput" data-source="files" type="file" multiple :accept="FILE_ACCEPT" :disabled="uploadBusy" @change="pickFiles">
                  <input ref="dirInput" data-source="directory" type="file" webkitdirectory multiple :disabled="uploadBusy" @change="pickFiles">
                  <button type="button" :disabled="uploadBusy" @click="fileInput.click()">选择文件</button>
                  <button type="button" :disabled="uploadBusy" @click="dirInput.click()">选择目录</button>
                </div>
                <p class="clean-file-hint">可多选。<b>大小不限</b>，只受数据盘剩余空间（{{meta?.storage?formatSize(meta.storage.free_bytes):'…'}}）限制，支持 {{(meta?.extensions||[]).join(' / ')}} 与 .zip。</p>
                <p v-if="uploadBusy" class="clean-hint">{{uploadProgress}}</p>
                <div v-if="uploadInfo" class="clean-summary"><b>{{uploadInfo.count}} 个文件 · {{formatSize(uploadInfo.bytes)}}</b><span>{{uploadInfo.files.slice(0,4).map(item=>item.path).join('、')}}{{uploadInfo.files.length>4?' …':''}}</span><button type="button" class="clean-danger" @click="dropUpload">移除</button></div>
              </div>
            </template>
            <template v-else>
              <label>目录或文件路径（每行一个）<textarea v-model="serverPaths" rows="3" :placeholder="(meta?.allowed_roots||[])[0]||'/data/input'"></textarea><small>只允许读取白名单根目录下的路径：{{(meta?.allowed_roots||[]).join('、')||'未配置'}}</small></label>
              <details class="clean-guide" :open="!sourceReady"><summary>浏览服务器目录</summary>
                <div class="clean-browse"><button type="button" :disabled="browseBusy" @click="loadBrowse(browsePath)">刷新</button><code>{{browse?.path||'未加载'}}</code></div>
                <div v-if="browse" class="clean-browse-list">
                  <button v-if="browse.parent!==''" type="button" @click="loadBrowse(browse.parent)">↑ 上级</button>
                  <button v-for="item in browse.dirs" :key="item.path" type="button" @click="loadBrowse(item.path)">📁 {{item.name}}</button>
                  <button v-for="item in browse.files" :key="item.path" type="button" @click="addServerPath(item.path)">＋ {{item.name}} <small>{{formatSize(item.size)}}</small></button>
                </div>
              </details>
            </template>
            <div class="clean-row clean-next"><button type="button" :disabled="!sourceReady" @click="openAt('mapping')">下一步：表头映射 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('mapping'),done:!!stepSummary('mapping')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('mapping')" @click="toggleStep('mapping')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">2</i><b>表头映射</b>
            <small class="clean-step-sum">{{stepSummary('mapping')}}</small>
          </button>
          <div v-show="isOpen('mapping')" class="clean-step-body">
        <p v-if="!sourceReady" class="clean-hint">先在第 1 步准备数据来源。</p>
        <template v-else>
          <div class="clean-row"><button :disabled="inspectBusy" @click="doInspect(true)">{{inspectBusy?'探测中…':'探测表头'}}</button><button :disabled="!headers.length" @click="mapFromHeaders">按表头一键映射</button><button @click="addField">添加字段</button></div>
          <div class="clean-two"><label>分隔符<input v-model="inputOpts.delimiter" placeholder="自动"></label><label>编码<input v-model="inputOpts.encoding" placeholder="自动"></label></div>
          <!-- 探测摘要与真实数据的预览都在结果区的「源数据」里：左边只留配置，数据去右边看。
               上面这两个输入框一改就自动重探（600ms 防抖），那边的预览跟着变，但**不会**把面板
               从用户正在看的地方拽走（见 doInspect 的 show 参数）。 -->
          <p v-if="unmappedDest" class="clean-warn">有 {{unmappedDest}} 个字段没有填目标字段名。</p>
          <div class="clean-fields">
            <div v-for="(field,index) in fields" :key="index" class="clean-field">
              <input v-model.trim="field.dest" placeholder="目标字段">
              <span class="clean-arrow">←</span>
              <select v-model="field.source"><option value="">（新列）</option><option v-for="name in headers" :key="name" :value="name" :disabled="usedSource(name,index)">{{name}}</option></select>
              <button class="clean-danger" @click="removeField(index)">×</button>
              <small v-if="isNewColumn(field)">只填目标字段 = 新建一列</small>
            </div>
          </div>
          <label>行数口径<select v-model="output.layout"><option value="union">多文件表头取并集（推荐）</option><option value="strict">严格：每个文件都必须有全部列</option></select><small>缺列时：<select v-model="output.on_missing"><option value="empty">留空</option><option value="error">报错</option><option value="skip_row">跳过该行</option></select></small></label>
          <label class="clean-check"><input v-model="inputOpts.precount" type="checkbox"><span>精确行数（多读一遍文件，进度更准）</span></label>
        </template>
            <div class="clean-row clean-next"><button type="button" :disabled="!fields.length" @click="openAt('ops')">下一步：清洗规则 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('ops'),done:!!stepSummary('ops')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('ops')" @click="toggleStep('ops')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">3</i><b>清洗规则</b>
            <small class="clean-step-sum">{{stepSummary('ops')}}</small>
          </button>
          <div v-show="isOpen('ops')" class="clean-step-body">
        <button type="button" :disabled="!fields.length" @click="addOp">添加规则</button>
        <div v-for="(op,index) in ops" :key="index" class="clean-op">
          <!-- 规则类型下拉必须是卡片里的第一个 select（浏览器套件用 `card.locator("select").first`
               定位它）。序号徽章是 <i>，不会插到它前面去。 -->
          <div class="clean-op-head">
            <i class="clean-op-no">{{index+1}}</i>
            <select v-model="op.op" @change="ensureOp(op)"><option v-for="item in OPS" :key="item.op" :value="item.op">{{item.label}}</option></select>
            <small class="clean-op-sum">{{opSummary(op)}}</small>
            <button class="clean-danger" @click="removeOp(index)" title="删除这条规则">×</button>
          </div>
          <div class="clean-two">
            <label v-if="opSpec(op.op)?.field">目标字段<select v-model="op.field"><option value="">请选择</option><option v-for="name in identityFields" :key="name" :value="name">{{name}}</option></select></label>
            <div v-if="opSpec(op.op)?.fields" class="clean-field-group">
              <span class="clean-check-label">字段（可多选）</span>
              <div class="clean-check-group">
                <label v-for="name in identityFields" :key="name" class="clean-check" :class="{on:op.fields.includes(name)}"><input type="checkbox" :checked="op.fields.includes(name)" @change="toggleField(op.fields,name)"><span>{{name}}</span></label>
              </div>
            </div>
          </div>
          <div class="clean-two">
            <label v-if="opSpec(op.op)?.pattern">{{op.op==='regex_replace'?'正则':'查找'}}<input v-model="op.pattern" :placeholder="op.op==='regex_replace'?'\\\\d+':'待替换的子串'"></label>
            <label v-if="opSpec(op.op)?.replacement">替换为<input v-model="op.replacement" placeholder="留空表示删除"></label>
          </div>
          <div class="clean-two">
            <label v-if="opSpec(op.op)?.value">填充值<input v-model="op.value"></label>
            <label v-if="opSpec(op.op)?.valueKind">目标类型<select v-model="op.value_kind"><option v-for="kind in opSpec(op.op).valueKind" :key="kind" :value="kind">{{kind}}</option></select></label>
            <label v-if="opSpec(op.op)?.dateFormat">输出格式<input v-model="op.date_format" placeholder="%Y-%m-%d"></label>
          </div>
          <div class="clean-two">
            <label v-if="opSpec(op.op)?.decimals">小数位<input v-model.number="op.decimals" type="number" min="0" max="12"></label>
            <label v-if="opSpec(op.op)?.thousands" class="clean-check"><input v-model="op.thousands" type="checkbox"><span>千分位</span></label>
            <label v-if="opSpec(op.op)?.slice">起始位置<input v-model.number="op.start" type="number" placeholder="0"></label>
            <label v-if="opSpec(op.op)?.slice">结束位置<input v-model.number="op.end" type="number" placeholder="末尾"></label>
          </div>
          <div class="clean-two">
            <label v-if="opSpec(op.op)?.dest">合并到<input v-model="op.dest" placeholder="目标字段"></label>
            <label v-if="opSpec(op.op)?.separator">分隔符<input v-model="op.separator" placeholder="例如 , "></label>
            <label v-if="opSpec(op.op)?.scope">去重范围<select v-model="op.scope"><option value="file">每个文件内</option><option value="task">整个任务</option></select></label>
          </div>
        </div>

        <div class="clean-fn-head"><b>自定义 Python 函数</b><small>{{functions.length}} 个</small><button :disabled="!fields.length" @click="addFunction">添加函数</button></div>
        <p v-if="!functions.length" class="clean-hint">声明式规则不够用时写这里：函数只看得见「输入字段」里勾中的列，写回值只能落在第 2 步已声明的目的字段上。</p>
        <details v-for="(fn,index) in functions" :key="fn.uid" class="clean-gen clean-fn" @toggle="markOpened(fn)">
          <summary><b>{{fn.name||'（未命名函数）'}}</b><small>{{fn.phase==='pre'?'清洗前':'清洗后'}} · {{fn.mode==='row'?'逐行':'整列'}} · 写回 {{fn.output_fields.join('、')||'（未选择）'}}</small></summary>
          <div class="clean-two">
            <label>名称<input v-model="fn.name" placeholder="例如 手机号脱敏"></label>
            <label>时机<select v-model="fn.phase"><option value="pre">清洗前（声明式规则之前）</option><option value="post">清洗后（约束校验之前）</option></select></label>
          </div>
          <div class="clean-two">
            <label>模式<select v-model="fn.mode" @change="fn.mode==='column'&&(fn.input_fields=fn.input_fields.slice(0,1))"><option value="row">逐行 row（{字段: 值} → {字段: 新值}）</option><option value="column">整列 column（{字段: [值…]} → {字段: [新值…]}）</option></select></label>
            <label>行出错时<select v-model="fn.on_row_error"><option value="empty">把写回字段置空</option><option value="keep">保留原值</option></select></label>
          </div>
          <div class="clean-two">
            <!-- 多选用勾选块而不是 <select multiple>：原生列表框不能改样式，蓝底高亮也不说明
                 「已选中几个」。外层是 div 不是 label —— label 包住整块时，点提示文字会激活
                 里面第一颗 checkbox。 -->
            <div class="clean-field-group clean-in-fields">
              <span class="clean-check-label">输入字段（可多选，最多 {{fn.mode==='column'?1:8}} 个）</span>
              <div class="clean-check-group">
                <label v-for="name in identityFields" :key="name" class="clean-check" :class="{on:fn.input_fields.includes(name)}"><input type="checkbox" :checked="fn.input_fields.includes(name)" @change="toggleFnInput(fn,name)"><span>{{name}}</span></label>
              </div>
            </div>
            <div class="clean-field-group clean-out-fields">
              <span class="clean-check-label">写回字段（可多选）</span>
              <div class="clean-check-group">
                <label v-for="name in identityFields" :key="name" class="clean-check" :class="{on:fn.output_fields.includes(name)}"><input type="checkbox" :checked="fn.output_fields.includes(name)" @change="toggleField(fn.output_fields,name)"><span>{{name}}</span></label>
              </div>
            </div>
          </div>
          <p v-if="!identityFields.length" class="clean-hint">先在第 2 步探测表头并映射字段，这里才有可勾的字段。</p>
          <label>超时（秒，超时杀进程组并重启子进程）<input v-model.number="fn.timeout_s" type="number" min="0.1" max="600" step="0.5"></label>
          <ul class="clean-list">
            <li><b>读到什么</b>：入参里只有你在上面「输入字段」里勾中的列，不是整行。没勾就什么都读不到。</li>
            <li><b>写回什么</b>：返回值里只能是「写回字段」里勾中的目的字段（第 2 步声明过的），写别的会被丢掉。</li>
            <li><b>返回 <code>None</code></b>：这一行/这一列不改动，保持原值。逐行模式下这就是「跳过这一行」。</li>
            <li><b>运行环境</b>：在隔离子进程里跑，超时会杀掉再重启；日期这类对象跨进程按字符串传，涉及日期请显式返回字符串。</li>
          </ul>
          <!-- 不能写成嵌套的 <details>：那张卡片自己就是一个 <details>，再来一个 <summary>
               会让「点开这张卡」这件事在 DOM 上出现两个同名的东西（浏览器套件里
               `card.locator("summary")` 立刻变成 strict mode 冲突）。按钮 + v-show 一样能折叠。 -->
          <div class="clean-row"><button type="button" class="clean-guide-toggle" @click="fn.showGuide=!fn.showGuide" :aria-expanded="!!fn.showGuide">{{fn.showGuide?'收起示例 ▲':'不会写？点开看 3 个能直接用的例子 ▼'}}</button></div>
          <div v-show="fn.showGuide" class="clean-fn-guide">
            <p class="clean-hint">点「用这段」会把例子填进这个函数的源码框，并改成例子需要的模式与字段 —— 填完请核对字段名是否是你数据里的。</p>
            <div v-for="(sample,at) in pySamples" :key="at" class="clean-sample">
              <header><b>{{sample.title}}</b><small>{{sample.note}}</small><button type="button" @click="useSample(fn,sample)">用这段</button></header>
              <pre>{{sample.source}}</pre>
            </div>
          </div>
          <!-- 不能用 <label> 包住编辑器：这块里第一颗 checkbox 会被「点文字就激活」的
               label 语义吃掉（同 :1057 的注释）。用 span 提示 + 普通 div 的结构。 -->
          <div class="clean-fn-source">
            <span class="clean-check-label">源码（入口 {{pyEntry}}，补全里只会出现后端白名单允许的东西）</span>
            <PyCodeEditor v-if="fn.opened" v-model="fn.source"
                          :input-fields="fn.input_fields" :output-fields="fn.output_fields"
                          :vocabulary="pyVocab" :mode="fn.mode" :entry="pyEntry"
                          :check-source="checkSource"></PyCodeEditor>
          </div>
          <!-- 工具条与结果面板都在「源码」之后、「删除这个函数」之前：卡片里的第一颗
               input 必须仍是「名称」、`input[type=number].first` 必须仍是「超时」
               （浏览器套件按位置选它们，见 test_clean_browser.add_function）。 -->
          <div class="clean-toolbar clean-fn-actions">
            <button v-if="formatterReady" type="button" :disabled="fn.formatBusy" @click="formatFunction(fn)">{{fn.formatBusy?'格式化中…':'格式化'}}</button>
            <label>试跑行数<input v-model.number="fn.testRows" type="number" min="1" max="5000" step="5"></label>
            <button type="button" class="clean-primary" :disabled="fn.testBusy" @click="runFunctionTest(fn,index)">{{fn.testBusy?'试跑中…':'用真实数据试跑这个函数'}}</button>
          </div>
          <!-- 试跑的结果面板在结果区（点「用真实数据试跑这个函数」会自动切过去）：
               左侧只留源码与触发它的那条工具条。 -->

          <div class="clean-row"><button class="clean-danger" @click="removeFunction(index)">删除这个函数</button></div>
        </details>
            <div class="clean-row clean-next"><button type="button" :disabled="!fields.length" @click="openAt('generate')">下一步：字段生成 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('generate'),done:!!stepSummary('generate')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('generate')" @click="toggleStep('generate')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">4</i><b>字段生成</b>
            <small class="clean-step-sum">{{stepSummary('generate')}}</small>
          </button>
          <div v-show="isOpen('generate')" class="clean-step-body">
        <p v-if="!fields.length" class="clean-hint">先在第 2 步配置字段。</p>
        <details v-for="field in fields" v-else :key="field.dest" class="clean-gen">
          <summary><b>{{field.dest||'（未命名）'}}</b><small>{{field.generate?.kind==='none'||!field.generate?'不改动':{random:'随机生成',llm:'大模型生成',sequence:'序号',expression:'模板拼接'}[field.generate.kind]}}</small></summary>
          <label>生成方式<select v-model="field.generate.kind" @change="ensureGen(field)">
            <option value="none">不改动（用来源列的值）</option>
            <option value="random">随机生成</option>
            <option value="sequence">序号</option>
            <option value="expression">模板拼接</option>
            <option value="llm">大模型生成（从已有字段提取）</option>
          </select></label>
          <template v-if="field.generate.kind==='random'">
            <label>生成器<select v-model="field.generate.generator" @change="ensureGen(field)"><option v-for="item in generators" :key="item.name" :value="item.name">{{item.label}}（{{item.name}}）</option></select></label>
            <div class="clean-two">
              <!-- locale 是「可选可填」（后端原话）：给几个常用值做快捷点选，其余的自己输。
                   原来这里挂 `<datalist>`，那是浏览器自带的提示框，样式改不动、位置也不受控。 -->
              <div class="clean-field-group">
                <span class="clean-check-label">locale（点一下填入，再点清空）</span>
                <input v-model="field.generate.params.locale" placeholder="zh_CN">
                <div class="clean-chips clean-locale">
                  <button v-for="name in (meta?.locales||[])" :key="name" type="button"
                          :class="{active:field.generate.params.locale===name}"
                          @click="field.generate.params.locale=field.generate.params.locale===name?'':name">{{name}}</button>
                </div>
              </div>
              <label>空值比例<input v-model="field.generate.params.null_ratio" type="number" min="0" max="1" step="0.05" placeholder="0"></label>
            </div>
            <!-- v-for 与 v-if 不能落在同一个元素上：Vue 3 里 v-if 先求值，那时 `name` 还不存在 -->
            <template v-for="name in paramsOf(field)" :key="name">
              <label v-if="paramKind(name)==='list'">{{PARAM_LABEL[name]||name}}（每行一个）<textarea :value="listText(field,name)" rows="3" @input="setList(field,name,$event.target.value)"></textarea></label>
              <label v-else-if="paramKind(name)==='bool'" class="clean-check"><input type="checkbox" :checked="!!field.generate.params[name]" @change="field.generate.params[name]=$event.target.checked"><span>{{PARAM_LABEL[name]||name}}</span></label>
              <label v-else-if="paramKind(name)==='int'">{{PARAM_LABEL[name]||name}}<input v-model.number="field.generate.params[name]" type="number"></label>
              <label v-else>{{PARAM_LABEL[name]||name}}<input v-model="field.generate.params[name]"></label>
            </template>
          </template>
          <template v-if="field.generate.kind==='sequence'">
            <div class="clean-two"><label>起始<input v-model.number="field.generate.start" type="number"></label><label>步长<input v-model.number="field.generate.step" type="number"></label></div>
            <label>补零位数<input v-model.number="field.generate.width" type="number" min="0" placeholder="0 = 不补零"></label>
          </template>
          <template v-if="field.generate.kind==='expression'">
            <label>模板<input v-model="field.generate.expression" placeholder="例如 ${城市}-${编号}"></label>
          </template>
          <template v-if="field.generate.kind==='llm'">
            <div class="clean-prompt">
              <label>提示词（用 <code>${字段名}</code> 引用已有字段，<code>${字段名:-默认值}</code> 给缺省值）
                <textarea v-model="field.generate.prompt" rows="4" placeholder="用一句话介绍 ${姓名}，他住在 ${城市}。"></textarea>
              </label>
              <div class="clean-chips"><span>插入字段</span><button v-for="name in identityFields" :key="name" :disabled="name===field.dest" @click="insertField(field,$event,name)">{{name}}</button></div>
              <p class="clean-hint">渲染预览：{{promptPreviewFor(field)||'（提示词为空）'}}</p>
            </div>
            <div class="clean-two">
              <label>批量合并（一次请求生成 N 行）<input v-model.number="field.generate.batch_size" type="number" min="1" max="200"></label>
              <label>系统提示（可选）<input v-model="field.generate.system" placeholder="例如：只输出结果，不要解释"></label>
            </div>
          </template>
        </details>

        <details class="clean-guide" @toggle="poolOpen=$event.target.open">
          <summary>模型池（{{pool.length}} 个端点，选中 {{llm.endpoint_ids.length}} 个）</summary>
          <div class="clean-pool">
            <label class="clean-check" v-for="item in pool" :key="item.id"><input v-model="llm.endpoint_ids" type="checkbox" :value="item.id" :disabled="!item.enabled"><span><b>{{item.name||item.model}}</b><small>{{item.model}} · 并发 {{item.max_concurrency}} · 权重 {{item.weight}} · {{item.key_set?'已设密钥':'无密钥'}} · 延迟 {{item.ewma_ms||'—'}}ms</small></span><button @click.prevent="editEndpoint(item)">编辑</button><button class="clean-danger" @click.prevent="removeEndpoint(item)">删除</button></label>
            <p v-if="!pool.length" class="clean-hint">还没有端点。添加一个 OpenAI 兼容的 chat/completions 地址。</p>
            <button class="clean-primary" @click="editEndpoint(null)">添加端点</button>
            <p class="clean-hint">按加权最小在途数分配请求：并发高、权重大的端点会分到更多调用；变慢的端点会自动被少选。</p>
          </div>
        </details>
        <div v-if="draft" class="clean-endpoint">
          <div class="clean-two">
            <label>名称<input v-model="draft.name" placeholder="例如 主力 GPT"></label>
            <label>模型<input v-model="draft.model" placeholder="gpt-4o-mini"></label>
          </div>
          <label>地址（chat/completions）<input v-model="draft.url" placeholder="https://api.example.com/v1/chat/completions"></label>
          <label>密钥<input v-model="draft.api_key" :placeholder="draft.id?'留空或填 '+poolSentinel+' 表示不改':'可选'"></label>
          <div class="clean-two">
            <label>最大并发<input v-model.number="draft.max_concurrency" type="number" min="1"></label>
            <label>权重<input v-model.number="draft.weight" type="number" min="1"></label>
          </div>
          <div class="clean-two">
            <label>读取超时（秒）<input v-model.number="draft.timeout_s" type="number" min="1"></label>
            <label>连接超时（秒）<input v-model.number="draft.connect_timeout_s" type="number" min="1"></label>
          </div>
          <div class="clean-two">
            <label>每分钟请求上限（0 = 不限）<input v-model.number="draft.rpm" type="number" min="0"></label>
            <label>失败重试<input v-model.number="draft.max_retries" type="number" min="0" max="10"></label>
          </div>
          <div class="clean-row"><button :disabled="poolBusy" class="clean-primary" @click="saveEndpoint">保存</button><button :disabled="testBusy" @click="testEndpoint(draft)">{{testBusy?'测试中…':'测试连接'}}</button><button @click="draft=null">取消</button></div>
          <p v-if="testResult&&testResult.for==='draft'" :class="testResult.ok?'clean-ok':'clean-warn'">{{testResult.ok?`连通（${testResult.seconds} 秒）：${testResult.message}`:testResult.message}}</p>
          <!-- 「最大并发」填的是**服务端真能同时跑几个**，不是「我希望它跑几个」。填大了不会更快：
               请求会在服务端排队，每个请求的耗时（含排队）都会被记进历史延迟，预估跟着一起虚高。
               本地 ollama 默认一次只跑一个请求（实测并发 4 个请求的墙钟是 1 个的 4 倍），要让它真并行
               得给它设 OLLAMA_NUM_PARALLEL。 -->
          <p class="clean-hint">「最大并发」要填<b>服务端真能同时处理几个</b>：填大了不会更快 —— 请求会在服务端排队，排队时间还会被算进历史延迟，让预估一起变虚。本地 ollama 默认一次只跑一个（要真并行需给它设 <code>OLLAMA_NUM_PARALLEL</code>），那就填 1。</p>
        </div>
        <div v-if="llmFields.length" class="clean-two">
          <label>只对前 N 行生成（0 = 全部）<input v-model.number="llm.sample_rows" type="number" min="0"></label>
          <label>调用次数上限<input v-model.number="llm.max_calls" type="number" min="0"></label>
        </div>
        <p v-if="llmFields.length" class="clean-hint">按 {{selectedEndpoints.length}} 个端点、合计并发上限 {{concurrency}} 估算。<template v-if="!llm.sample_rows&&inspectResult?.row_estimate>20000">行数较大且未限制生成行数，建议设置「只对前 N 行生成」。</template></p>

            <div class="clean-row clean-next"><button type="button" :disabled="!fields.length" @click="openAt('limits')">下一步：约束与回退 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('limits'),done:!!stepSummary('limits')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('limits')" @click="toggleStep('limits')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">5</i><b>约束与回退</b>
            <small class="clean-step-sum">{{stepSummary('limits')}}</small>
          </button>
          <div v-show="isOpen('limits')" class="clean-step-body">
        <p v-if="!fields.length" class="clean-hint">先在第 2 步配置字段。</p>
        <details v-for="field in fields" v-else :key="'c'+field.dest" class="clean-gen clean-limits">
          <summary><b>{{field.dest||'（未命名）'}}</b><small>{{field.constraints.length}} 条约束</small></summary>
          <div v-for="(rule,index) in field.constraints" :key="index" class="clean-rule">
            <div class="clean-row">
              <select v-model="rule.kind"><option value="non_null">不能为空</option><option value="enum">枚举取值</option><option value="regex">正则匹配</option><option value="length">长度限制</option><option value="range">数值区间</option><option value="format">格式校验</option><option value="type">类型校验</option><option value="unique">唯一</option></select>
              <select v-model="rule.severity"><option value="error">错误</option><option value="warn">仅告警</option></select>
              <button class="clean-danger" @click="field.constraints.splice(index,1)">×</button>
            </div>
            <label v-if="rule.kind==='enum'">允许值（每行一个）<textarea :value="(rule.values||[]).join('\n')" rows="3" @input="rule.values=$event.target.value.split('\n').map(item=>item.trim()).filter(Boolean)"></textarea></label>
            <div class="clean-two">
              <label v-if="rule.kind==='regex'">正则<input v-model="rule.pattern" placeholder="例如 ^1[3-9]\\d{9}$"></label>
              <label v-if="rule.kind==='regex'">匹配方式<select v-model="rule.mode"><option value="fullmatch">整串匹配</option><option value="search">包含即通过</option></select></label>
            </div>
            <div class="clean-two">
              <label v-if="rule.kind==='length'">最小长度<input v-model="rule.min_length" type="number" min="0"></label>
              <label v-if="rule.kind==='length'">最大长度<input v-model="rule.max_length" type="number" min="0"></label>
            </div>
            <div class="clean-two">
              <label v-if="rule.kind==='range'">最小值<input v-model="rule.min_value" type="number"></label>
              <label v-if="rule.kind==='range'">最大值<input v-model="rule.max_value" type="number"></label>
            </div>
            <div class="clean-two">
              <label v-if="rule.kind==='format'">格式<select v-model="rule.value_kind"><option value="email">邮箱</option><option value="url">网址</option><option value="ipv4">IPv4</option><option value="date">日期</option><option value="datetime">日期时间</option><option value="time">时间</option></select></label>
              <label v-if="rule.kind==='type'">类型<select v-model="rule.value_kind"><option value="int">整数</option><option value="float">小数</option><option value="str">文本</option><option value="bool">布尔</option><option value="date">日期</option><option value="datetime">日期时间</option></select></label>
              <label v-if="rule.kind==='unique'">唯一范围<select v-model="rule.unique_scope"><option value="task">整个任务</option><option value="file">每个文件内</option></select></label>
            </div>
          </div>
          <button @click="field.constraints.push({kind:'length',severity:'error',values:[],pattern:'',mode:'fullmatch',min_length:'',max_length:'',min_value:'',max_value:'',value_kind:'email',unique_scope:'task'})">添加约束</button>
          <label class="clean-check"><input type="checkbox" :checked="!!field.fallback?.enabled" @change="toggleFallback(field,$event.target.checked)"><span>违规时按策略修正（不勾选则不回退，违规值原样留下）</span></label>
          <template v-if="field.fallback?.enabled">
            <div class="clean-two">
              <label>策略<select v-model="field.fallback.on_violation"><option value="retry">重新生成</option><option value="keep">保留原值</option><option value="truncate">截断/修正</option></select></label>
              <label>最多重试<input v-model.number="field.fallback.max_retries" type="number" min="0" max="10"></label>
            </div>
            <label>兜底默认值<input v-model="field.fallback.default" placeholder="留空表示填空字符串"></label>
            <p class="clean-hint">每次触发回退都会记一条告警（同一字段/类型只报首次与数量级），计数始终精确。</p>
          </template>
        </details>

            <div class="clean-row clean-next"><button type="button" @click="openAt('check')">下一步：校验与预览 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('check'),done:!!stepSummary('check')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('check')" @click="toggleStep('check')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">6</i><b>校验与预览</b>
            <small class="clean-step-sum">{{stepSummary('check')}}</small>
          </button>
          <div v-show="isOpen('check')" class="clean-step-body">
        <div class="clean-toolbar">
          <label>试跑行数<input v-model.number="dryRows" type="number" min="1" max="5000"></label>
          <button type="button" :disabled="dryBusy||!configReady" @click="runValidate">{{dryBusy?'试跑中…':`试跑前 ${dryRowCount()} 行`}}</button>
          <span v-if="dry" :class="dry.ok?'clean-ok':'clean-warn'">{{dry.ok?'试跑通过':'试跑失败'}}</span>
        </div>
        <p class="clean-hint">试跑用完整流水线跑前 N 行（上限 5000），只跑第一个文件；提交任务时会把这次试跑的随机种子一起带上，所以正式产出的前几行与这里看到的逐个相同。</p>
        <!-- 试跑成功的计数、对比表与报告都在结果区的「试跑预览」里（点「试跑」会自动切过去）；
             失败时这一行必须留在原地 —— 它是刚点下去的那个按钮的结果。 -->
        <div v-if="dry&&!dry.ok" class="clean-warn">{{dry.error}}</div>

            <div class="clean-row clean-next"><button type="button" @click="openAt('run')">下一步：执行 →</button></div>
          </div>
        </section>

        <section class="clean-step" :class="{open:isOpen('run'),done:!!stepSummary('run')}">
          <button type="button" class="clean-step-head" :aria-expanded="isOpen('run')" @click="toggleStep('run')">
            <i class="clean-step-arrow"></i><i class="clean-step-no">7</i><b>执行</b>
            <small class="clean-step-sum">{{stepSummary('run')}}</small>
          </button>
          <div v-show="isOpen('run')" class="clean-step-body">
        <label>任务名称<input v-model="taskName" placeholder="例如 9 月客户名单清洗"></label>
        <label>输出格式<select v-model="output.format"><option value="csv">CSV</option><option value="xlsx">Excel</option><option value="jsonl">JSONL</option><option value="parquet">Parquet</option></select></label>
        <div class="clean-two">
          <label v-if="output.format==='csv'">输出分隔符<input v-model="output.csv_delimiter" maxlength="1"></label>
          <label>提交间隔（行，0 = 自动）<input v-model.number="output.commit_rows" type="number" min="0"></label>
        </div>
        <div class="clean-two">
          <label>报告明细条数（0 = 不列明细）<input v-model.number="output.top_k" type="number" min="0" max="1000"></label>
        </div>
        <p class="clean-hint">报告里第 5、7 节逐条列多少行。几百万行的文件上设小一点（或 0）能让报告短到读得完：<b>聚合计数与统计精度都不受它影响</b>，逐条明细写在下载包的 <code>fallback.jsonl</code> 里（每触发一次回退一行）。</p>
        <button class="clean-block" :disabled="estimateBusy||!configReady" @click="runEstimate">{{estimateBusy?'估算中…':'获取处理预估'}}</button>
        <!-- 预估的数字在结果区的「处理预估」里（点「获取处理预估」会自动切过去）：
             提交门禁仍由 estimateSeen 把关，只是数字不在左边显示了。 -->
        <button class="clean-primary clean-block" :disabled="busy||!configReady||!estimateSeen" @click="submit">{{estimateSeen?'创建清洗任务':'先获取预估再提交'}}</button>
        <p v-if="llmFields.length&&!llm.sample_rows" class="clean-warn">启用了大模型字段但没限制生成行数，GB 级数据会很慢。建议在第 4 步设置「只对前 N 行生成」。</p>
          </div>
        </section>
      </aside>

      <section class="clean-stage">
        <div class="clean-tabs">
          <!-- 四个页签只在「任务结果 / 试跑预览」下渲染：源数据、函数试跑、处理预估各自只有一块内容，
               给它们摆一排页签只会让人以为还有别的东西可看。`<template>` 不产生节点，所以
               `.clean-tabs > button` 仍然只命中这四个页签。 -->
          <template v-if="showsTabs">
            <button v-for="tab in STAGE_TABS" :key="tab.id" :class="{active:stageTab===tab.id}" :disabled="tab.taskOnly&&viewingDry" :title="tab.taskOnly&&viewingDry?'试跑没有这个，等任务跑起来再看':''" @click="stageTab=tab.id">{{tab.label}}</button>
          </template>
          <div class="clean-stage-meta">
            <span v-if="shownSource==='raw'" class="clean-current">源数据（未提交）</span>
            <span v-else-if="shownSource==='fn'" class="clean-current">函数试跑 · {{stageFn?.name||'未命名'}}</span>
            <span v-else-if="shownSource==='estimate'" class="clean-current">处理预估（未提交）</span>
            <span v-else-if="viewingDry" class="clean-current">试跑预览（未提交）</span>
            <span v-else-if="current" class="clean-current">{{current.name||current.id}} · {{statusText(current.status)}} · {{progressOf(current)}}%</span>
            <!-- 五份数据长得像，差一个标明来源的开关就等于让用户自己猜在看哪一份。
                 只在有两个以上来源时出现：只有任务时，上面那行字已经说清了在看什么。
                 另外它只在「数据预览」这一档下露面，见 showSwitcher。 -->
            <div v-if="showSwitcher" class="clean-source">
              <button v-for="item in switchSources" :key="item.id" :class="{active:shownSource===item.id}" @click="showSource(item.id)">{{item.label}}</button>
            </div>
          </div>
        </div>

        <div v-if="!shownSource" class="clean-empty"><h3>这里显示数据</h3><p>左边把配置填完就会出现：探测表头后有源数据，试跑后有清洗前后的对比，提交后有实时日志与字段统计。</p></div>

        <template v-else>
          <!-- 源数据：文件开头几行的**真值**。映射错就错在「只看表头名猜内容」上，所以它和
               「按表头一键映射」是配套的 —— 左边改一行映射，这里那一列的表头标记立刻跟着变。 -->
          <div v-if="shownSource==='raw'" class="clean-panel">
            <div class="clean-inspect">
              <span>{{inspectResult.file}} · {{inspectResult.kind.toUpperCase()}} · 编码 {{inspectResult.encoding}} · 分隔符 {{inspectResult.delimiter||'—'}}</span>
              <span>{{inspectResult.row_estimate}} 行（估算）· 表头 {{inspectResult.headers.join(' / ')||'—'}}</span>
              <span v-for="message in inspectResult.warnings" :key="message" class="clean-warn">{{message}}</span>
              <span v-if="delimiterHint" class="clean-warn">{{delimiterHint}}</span>
            </div>
            <div v-if="sampleRows.length" class="clean-map-preview">
              <div class="clean-toolbar">
                <label>预览文件<select v-model="inspectFile" :disabled="inspectBusy" @change="doInspect(true)"><option v-for="item in inspectFiles" :key="item.key" :value="item.key">{{item.key}}</option></select></label>
                <label>预览行数<select v-model.number="inspectRows" :disabled="inspectBusy" @change="doInspect(true)"><option :value="20">20</option><option :value="50">50</option><option :value="200">200</option></select></label>
                <button type="button" :disabled="inspectBusy" @click="doInspect(true)">{{inspectBusy?'探测中…':'刷新预览'}}</button>
              </div>
              <div class="clean-pane" data-side="source">
                <table>
                  <thead><tr><th>源数据</th>
                    <th v-for="(name,column) in headers" :key="column" :class="mappedTo(name).length?'':'dropped'">{{name}}<i v-if="mappedTo(name).length" class="clean-col-tag mapped">→ {{mappedTo(name).join('、')}}</i><i v-else class="clean-col-tag">未映射</i></th>
                    <!-- 只填了目标字段的「新列」：源文件里根本没有这一列，所以上面那圈遍历
                         （headers）永远列不到它 —— 而「我加了哪几列」正是表头映射这一步最该看得见
                         的东西。与「清洗前」栏里给生成列标「新增」是同一套说法，这里的数据当然是空的。 -->
                    <th v-for="(name,at) in newColumns" :key="`new-${at}`" class="added" title="源文件里没有这一列，由字段配置新建">{{name}}<i class="clean-col-tag added">新增</i></th>
                  </tr></thead>
                  <tbody><tr v-for="(row,index) in sampleRows" :key="index"><td class="clean-index">{{index+1}}</td><td v-for="(value,column) in row" :key="column" :class="mappedTo(headers[column]).length?'':'dropped'">{{cellShown(value)}}</td><td v-for="(name,at) in newColumns" :key="`new-${at}`"></td></tr></tbody>
                </table>
              </div>
              <p class="clean-hint">这是文件开头 {{sampleRows.length}} 行的真实数据（只读文件开头，不加载整个文件）；标「未映射」的列没有字段引用它，不会进产物，末尾标「新增」的列源文件里没有、是字段配置新建的。改了第 2 步的分隔符/编码，这里会自己重探。</p>
            </div>
            <p v-else class="clean-hint">这个文件没取到样本行。</p>
          </div>

          <!-- 函数试跑：面板搬到了结果区，触发它的「试跑行数 + 用真实数据试跑这个函数」还留在
               左边的函数卡片里 —— 那是写代码的地方，点完抬头看这里。 -->
          <div v-else-if="shownSource==='fn'" class="clean-panel">
            <div class="clean-fn-test">
              <p class="clean-hint">
                样本：{{stageFn.test.sample.file||'（未取到文件）'}} 的前 {{stageFn.test.sample.rows}} 行 · {{stageFn.test.mode==='row'?'逐行':'整列'}}模式 ·
                耗时 {{stageFn.test.seconds}} 秒<template v-if="!stageFn.test.ran">（没有执行）</template>
              </p>
              <p v-if="stageFn.test.error" class="clean-warn">{{stageFn.test.error}}</p>
              <!-- 结果是「上一次跑的那版源码」跑出来的：源码一动就明说，别让旧结果冒充新的。 -->
              <p v-else-if="stageFn.test.source!==stageFn.source" class="clean-warn">源码在这之后被改过，下面这些结果是上一版的 —— 重新试跑一次。</p>
              <div v-if="stageFn.test.rows.length" class="clean-table-wrap clean-fn-table">
                <table class="clean-stats">
                  <thead><tr><th>#</th><th>输入</th><th>返回</th><th>备注</th></tr></thead>
                  <tbody>
                    <tr v-for="item in stageFn.test.rows" :key="item.index" :class="{bad:!!item.error}">
                      <td>{{item.index+1}}</td>
                      <td>{{cellText(item.input)}}</td>
                      <td>{{item.error?'':(item.output?cellText(item.output):'不改动')}}</td>
                      <td class="clean-fn-note">{{item.error||''}}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="stageFn.test.columns.length" class="clean-table-wrap clean-fn-table">
                <table class="clean-stats">
                  <thead><tr><th>字段</th><th>输入（前 5 个）</th><th>返回（前 5 个）</th></tr></thead>
                  <tbody>
                    <tr v-for="col in stageFn.test.columns" :key="col.name">
                      <td>{{col.name}}</td><td>{{cellText(col.input)}}</td><td>{{cellText(col.output)}}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="stageFn.test.printed.length" class="clean-logs clean-fn-print">
                <p v-for="(line,at) in stageFn.test.printed" :key="at"><span>{{line}}</span></p>
                <p v-if="stageFn.test.printed_omitted" class="lv-warn"><span>（另有 {{stageFn.test.printed_omitted}} 行输出已省略）</span></p>
              </div>
              <ul v-if="stageFn.test.notes.length" class="clean-list">
                <li v-for="(note,at) in stageFn.test.notes" :key="at">{{note}}</li>
              </ul>
            </div>
          </div>

          <!-- 处理预估：提交前的几个数字，与左侧第 7 步的「获取处理预估」配套。 -->
          <div v-else-if="shownSource==='estimate'" class="clean-panel">
            <div class="clean-estimate">
              <b>{{estimateText}}</b>
              <span>共 {{estimate.rows}} 行{{estimate.rows_exact?'（精确）':'（估算）'}}，大模型调用 {{estimate.estimate.calls}} 次<template v-if="estimate.estimate.calls_capped">（已按上限截断）</template><template v-if="estimate.estimate.rounds">，约 {{estimate.estimate.rounds}} 轮</template>，并发 {{estimate.estimate.concurrency}}<template v-if="estimateLatency">（按每次 {{estimateLatency}}的历史延迟估算）</template></span>
              <span v-for="note in estimate.notes" :key="note">{{note}}</span>
              <span v-for="endpoint in estimate.endpoints.selected" :key="endpoint.id">{{endpoint.name}}：并发 {{endpoint.max_concurrency}}{{endpoint.enabled?'':'（已停用）'}}</span>
              <span v-if="estimate.endpoints.missing.length" class="clean-warn">找不到端点：{{estimate.endpoints.missing.join('、')}}</span>
            </div>
            <p class="clean-hint">拿这个数确认能不能接受，再回左边点「创建清洗任务」。改了配置要重新获取一次。</p>
          </div>

          <div v-else-if="stageTab==='preview'" class="clean-panel">
            <div v-if="!viewingDry" class="clean-toolbar">
              <label>文件<select v-model="previewFile" @change="loadPreview"><option v-for="item in preview?.files||[]" :key="item.key" :value="item.key">{{item.key}}</option></select></label>
              <label>行数<select v-model.number="previewRows" @change="loadPreview"><option :value="20">20</option><option :value="50">50</option><option :value="200">200</option></select></label>
              <button :disabled="previewBusy" @click="loadPreview">刷新</button>
            </div>
            <p v-else class="clean-hint">试跑按前 {{dry?.rows}} 行的上限跑完（只跑第一个文件），这里显示清洗前后的前 {{dry.preview.rows}} 行。</p>
            <!-- 试跑的数字（读入/违规/回退/大模型调用）以前写在左侧第 6 步，现在跟对比表放在一起：
                 「会变成什么样」和「改动了多少」本来就是同一个问题的两面。 -->
            <div v-if="viewingDry&&dry?.ok" class="clean-dry">
              <span>读入 {{dry.rows}} 行 · 清洗前违规 {{sum(dry.violations)}} · 清洗后违规 {{sum(dry.after_violations)}} · 回退 {{sum(dry.fallbacks)}} 次</span>
              <span v-if="dry.llm?.budget">大模型调用 {{dry.llm.budget.calls}} 次（上限 {{dry.llm.budget.max_calls}}）</span>
              <span v-for="note in dry.notes" :key="note" class="clean-note">{{note}}</span>
            </div>
            <p v-if="shownPreview?.note" class="clean-warn">{{shownPreview.note}}</p>
            <!-- 试跑失败时后端照样回一份 preview（半截的数据），所以「试跑预览」这个来源仍然可选。
                 进来得说清为什么是半截的 —— 左侧第 6 步那份错误提示保留，那里才是点按钮的地方。 -->
            <p v-if="viewingDry&&dry&&!dry.ok" class="clean-warn">{{dry.error}}</p>
            <div v-if="shownPreview" class="clean-compare">
              <!-- 左栏表头用 `before_columns`（文件里的列名），右栏用 `columns`（目的字段名）：
                   重命名、新列、被丢弃的列，三者在这一行上各有个说法（见 shownBeforeColumns）。 -->
              <div class="clean-pane" data-side="before" @scroll="syncScroll($event,'after')">
                <table><thead><tr><th>清洗前</th>
                  <th v-for="(name,column) in shownBeforeColumns" :key="column"
                      :class="isDroppedColumn(column)?'dropped':(isGeneratedColumn(column)?'added':'')"
                      :title="!isDroppedColumn(column)&&name!==shownPreview.columns[column]?`→ ${shownPreview.columns[column]}`:''">{{name}}<i v-if="isDroppedColumn(column)" class="clean-col-tag">删除</i><i v-else-if="isGeneratedColumn(column)" class="clean-col-tag added">新增</i></th>
                </tr></thead>
                  <tbody><tr v-for="(row,index) in shownPreview.before" :key="index"><td class="clean-index">{{index+1}}</td><td v-for="(value,column) in row" :key="column" :class="isDroppedColumn(column)?'dropped':diffClass(value,shownPreview.after[index]?.[column])">{{value}}</td></tr></tbody></table>
              </div>
              <div class="clean-pane" data-side="after" @scroll="syncScroll($event,'before')">
                <table><thead><tr><th>清洗后</th>
                  <th v-for="(name,column) in shownPreview.columns" :key="name"
                      :class="isGeneratedColumn(column)?'added':''">{{name}}<i v-if="isGeneratedColumn(column)" class="clean-col-tag added">新增</i></th>
                </tr></thead>
                  <tbody><tr v-for="(row,index) in shownPreview.after" :key="index"><td class="clean-index">{{index+1}}</td><td v-for="(value,column) in row" :key="column" :class="diffClass(shownPreview.before[index]?.[column],value)">{{value}}</td></tr></tbody></table>
              </div>
            </div>
            <p v-else class="clean-hint">{{previewBusy?'加载中…':'选一个文件查看前后对比。'}}</p>
            <!-- 各字段违规/回退：原先写在左侧第 5 步每张字段卡的摘要里。这些数字属于**这次试跑**，
                 不属于字段配置，所以跟着试跑结果走。只列真出过数的字段。 -->
            <div v-if="viewingDry&&dryFieldRows.length" class="clean-table-wrap clean-dry-fields">
              <table class="clean-stats">
                <thead><tr><th>字段</th><th>清洗前违规</th><th>回退</th></tr></thead>
                <tbody><tr v-for="row in dryFieldRows" :key="row.name"><td>{{row.name}}</td><td>{{row.violations}}</td><td>{{row.fallbacks}}</td></tr></tbody>
              </table>
            </div>
            <p class="clean-legend"><span class="added">新增</span><span class="changed">修改</span><span class="removed">清空</span>左栏是文件里的列名，右栏是目的字段名<template v-if="droppedNames.length">；末尾标「删除」的 {{droppedNames.length}} 列（{{droppedNames.join('、')}}）没配来源，不会进产物</template>。生成列在「清洗前」是空的，这是正常的。</p>
          </div>

          <div v-else-if="stageTab==='logs'" class="clean-panel">
            <template v-if="viewingDry">
              <p class="clean-hint">试跑没有实时日志 —— 日志是任务跑起来之后才有的事件流。提交任务后这里会实时刷新。</p>
            </template>
            <template v-else>
              <div class="clean-toolbar">
                <label>级别<select v-model="logLevel" @change="loadLogs"><option v-for="item in LEVELS" :key="item.value" :value="item.value">{{item.label}}</option></select></label>
                <label class="clean-check"><input v-model="follow" type="checkbox"><span>自动滚到底部</span></label>
                <button @click="resyncLogs">重新同步</button>
              </div>
              <p v-if="gapNotice" class="clean-warn">{{gapNotice}}</p>
              <div ref="logBox" class="clean-logs">
                <p v-if="!visibleLogs.length" class="clean-hint">暂无日志。</p>
                <p v-for="item in visibleLogs" :key="item.seq" :class="'lv-'+item.level"><time>{{dateText(item.ts)}}</time><span>{{item.message}}</span></p>
              </div>
            </template>
          </div>

          <div v-else-if="stageTab==='report'" class="clean-panel">
            <div v-if="!viewingDry" class="clean-toolbar"><button :disabled="reportBusy" @click="loadReport">刷新报告</button><a :href="`/api/clean/tasks/${current.id}/download`">下载 ZIP</a></div>
            <p v-else class="clean-hint">这份报告来自刚跑完的试跑（还没提交，所以没有下载包）。报告里的数字只覆盖试跑处理过的那几行。</p>
            <div v-if="shownReport" class="clean-report" v-html="renderMarkdown(shownReport)"></div>
            <p v-else class="clean-hint">{{viewingDry?'试跑没有生成报告（试跑失败时不会有报告）。':(reportBusy?'加载中…':'任务还没跑完，暂时没有报告。')}}</p>
          </div>

          <div v-else class="clean-panel">
            <template v-if="viewingDry">
              <p class="clean-hint">字段统计要等任务跑起来才有。试跑的数字在「试跑预览」里。</p>
            </template>
            <template v-else>
              <div class="clean-toolbar"><button :disabled="statsBusy" @click="loadStats">刷新统计</button><span v-if="stats">清洗前 {{stats.rows_before}} 行 → 清洗后 {{stats.rows_after}} 行</span></div>
              <div v-if="stats" class="clean-table-wrap">
                <table class="clean-stats">
                  <thead><tr><th>字段</th><th>清洗前</th><th>清洗后</th><th>违规（前→后）</th><th>回退</th><th>常见值（前）</th></tr></thead>
                  <tbody>
                    <tr v-for="item in stats.fields" :key="item.field">
                      <td>{{item.field}}</td>
                      <td><span v-if="item.before">{{item.before.count}} 行 · 空 {{pct(item.before.empty_rate)}} · 长度 {{item.before.length_min}}–{{item.before.length_max}} · p50 {{item.before.p50??'—'}}{{item.before.approximate?'（采样）':''}}</span><span v-else>—</span></td>
                      <td><span v-if="item.after">{{item.after.count}} 行 · 空 {{pct(item.after.empty_rate)}} · 长度 {{item.after.length_min}}–{{item.after.length_max}} · p50 {{item.after.p50??'—'}}{{item.after.approximate?'（采样）':''}}</span><span v-else>—</span></td>
                      <td>{{sum(item.violations)}} → {{sum(item.after_violations)}}</td>
                      <td>{{sum(item.fallbacks)}}</td>
                      <td>{{(item.before?.top||[]).slice(0,3).map(pair=>pair[0]).join('、')||'—'}}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <p v-if="stats" class="clean-hint">{{stats.notes.join('；')}}</p>
              <p v-else class="clean-hint">{{statsBusy?'加载中…':'暂无统计。'}}</p>
            </template>
          </div>
        </template>
      </section>
    </div>

    <section class="queue clean-tasks">
      <div class="queue-head"><div><span class="eyebrow">CLEAN TASK QUEUE</span><h2>清洗任务</h2></div><button class="refresh" @click="loadTasks">刷新</button></div>
      <div v-if="!tasks.length" class="empty">还没有清洗任务。配置完成后点「创建清洗任务」。</div>
      <article v-for="task in sortedTasks" :key="task.id" class="task clean-task">
        <div class="task-main"><div><b>{{task.name||task.id}}</b><small>{{dateText(task.created_at)}} · {{task.message||statusText(task.status)}}<template v-if="task.files_total"> · 文件 {{task.files_done||0}}/{{task.files_total}}</template></small></div></div>
        <div class="progress"><div><span>{{etaText(task)||statusText(task.status)}}</span><strong>{{progressOf(task)}}%</strong></div><div class="track"><i :class="task.status" :style="{width:progressOf(task)+'%'}"></i></div><small v-if="task.error" class="error">{{task.error}}</small></div>
        <span class="badge" :class="task.status">{{task.status_text||statusText(task.status)}}</span>
        <div class="task-actions">
          <button @click="selectTask(task.id)">查看</button>
          <button v-if="['running','queued'].includes(task.status)" @click="control(task,'pause')">暂停</button>
          <button v-if="task.status==='paused'" @click="control(task,'resume')">继续</button>
          <!-- 暂停只是让工作线程停在检查点，进程还在；要让 GB 级任务真的停下来只有取消。
              取消保留已提交的产物，状态落到「已取消」，上面那个「续跑」按钮随即出现。 -->
          <button v-if="['running','queued','paused'].includes(task.status)" @click="control(task,'cancel')">取消</button>
          <button v-if="task.resume?.resumable" class="resume" @click="control(task,'resume')">续跑</button>
          <button v-if="['failed','cancelled','interrupted'].includes(task.status)&&!task.resume?.resumable" class="retry" @click="control(task,'retry')">重跑</button>
          <a v-if="['completed','cancelled','interrupted'].includes(task.status)" :href="`/api/clean/tasks/${task.id}/download`">下载</a>
          <button class="danger" :disabled="['running','queued'].includes(task.status)" @click="control(task,'delete')">删除</button>
        </div>
        <p v-if="task.resume&&!task.resume.resumable&&task.status!=='completed'" class="clean-note">不能续跑：{{task.resume.reason}}</p>
      </article>
    </section>
  </section>
</template>

<style scoped>
.clean-page{color:#30465f;font-size:var(--fs-13,13px);--clean-line:#dbe4f0;--clean-muted:#74869b;--clean-blue:#4169d8;--clean-config-w:calc(620px * var(--ui-scale));--clean-stage-min:calc(360px * var(--ui-scale));--clean-field-max:calc(820px * var(--ui-scale));--clean-row-max:calc(1280px * var(--ui-scale))}
.clean-header{display:flex;align-items:center;justify-content:space-between;gap:20px;padding-bottom:24px;margin-bottom:24px;border-bottom:1px solid var(--clean-line)}
.clean-header p{color:var(--clean-muted);margin:0;line-height:1.8}
.clean-badge{border:1px solid #cbdaf2;border-radius:20px;padding:9px 14px;background:#f3f7ff;color:#5576b7;font-size:var(--fs-11,11px);white-space:nowrap}
/* 清洗页自己声明几何，不再继承地图工具的工作台框架（400px 侧栏 + 580px 舞台，见
   ui-refinement.css）。那边的侧栏只放几个输入框配一块大画布；这里的配置栏要装七步表单，
   所以它宽了近一倍、不再自带内滚动条（表单跟着页面滚），而右侧舞台粘在视口上。
   两栏各自成卡片，外层不再套一个大边框 —— 那正是「表单被挤在一个小框里」的观感来源。

   配比是**两栏各半**（50%）。定宽 620px 在 2560 的屏上只占 38%，表单反而成了窄的那一栏，
   而舞台在任务跑完之前一直是空的。下限 620px 保证窄屏不被压扁（minmax 的 max 小于 min
   时按 min 算）。舞台留 360px 下限：再窄连四个 Tab 都摆不下。低于 1100px 视口叠成一栏。

   整页仍然留在 --ui-content-w 那个居中框里（1376 × --ui-frame）：两边留白是全局节奏，
   撑满窗口试过一版，观感反而更差。所以这里只调两栏的比例，不动页面宽度。 */
.clean-workspace{display:grid;grid-template-columns:minmax(var(--clean-config-w),50%) minmax(var(--clean-stage-min),1fr);gap:calc(24px * var(--ui-scale));align-items:start;height:auto;min-height:0;border:0;border-radius:0;background:none;box-shadow:none}
.clean-config{align-self:start;min-width:0;min-height:0;overflow:visible;padding:calc(20px * var(--ui-scale)) calc(22px * var(--ui-scale));border:1px solid var(--clean-line);border-radius:var(--ui-panel-radius);background:#fff;box-shadow:0 8px 28px rgba(48,75,112,.06)}
/* ---- 步骤导航与手风琴 ---- */
.clean-steps{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding-bottom:calc(14px * var(--ui-scale));margin-bottom:calc(16px * var(--ui-scale));border-bottom:1px solid var(--clean-line)}
.clean-step-pill{display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border:1px solid var(--clean-line);border-radius:999px;background:#fff;color:#5b7590;font-size:var(--fs-11,11px);font-weight:600;line-height:1.5}
.clean-step-pill i{display:grid;place-items:center;width:18px;height:18px;border-radius:50%;background:#eef2f8;color:#7d8fa5;font-size:var(--fs-10,10px);font-style:normal}
.clean-step-pill.done{border-color:#c6e2d6}
.clean-step-pill.done i{background:#e6f6ef;color:#2f7c63}
.clean-step-pill.active{border-color:#9db8e6;background:#eef3fc;color:#2f5bb0}
.clean-step-pill.active i{background:var(--clean-blue);color:#fff}
.clean-expand{margin:0 0 0 auto;padding:6px 10px;border-radius:999px;color:#5b7590;font-size:var(--fs-11,11px);font-weight:600}
.clean-step{border:1px solid var(--clean-line);border-radius:12px;background:#fff;margin-bottom:calc(10px * var(--ui-scale));overflow:hidden}
.clean-step.open{border-color:#c9d9f0;box-shadow:0 6px 18px rgba(48,75,112,.07)}
.clean-step-head{display:flex;align-items:center;gap:10px;width:100%;padding:calc(12px * var(--ui-scale)) calc(14px * var(--ui-scale));border:0;border-radius:0;background:transparent;text-align:left;cursor:pointer}
.clean-step-head:hover{background:#f6f9fe;border-color:transparent}
.clean-step-head b{font-size:var(--fs-14,14px);font-weight:650;color:#2f4a6b}
.clean-step-arrow{width:8px;height:8px;flex:none;border-right:2px solid #93a7bf;border-bottom:2px solid #93a7bf;transform:rotate(-45deg);transition:transform .16s;font-style:normal}
.clean-step.open .clean-step-arrow{transform:rotate(45deg)}
.clean-step-no{display:grid;place-items:center;width:24px;height:24px;flex:none;border-radius:50%;background:#eef2f8;color:#7d8fa5;font-size:var(--fs-11,11px);font-style:normal;font-weight:700}
.clean-step.done .clean-step-no{background:var(--clean-blue);color:#fff}
.clean-step-sum{margin-left:auto;color:var(--clean-muted);font-size:var(--fs-11,11px);font-weight:400;line-height:1.8;white-space:nowrap}
.clean-step-body{padding:calc(14px * var(--ui-scale)) calc(14px * var(--ui-scale)) calc(16px * var(--ui-scale));border-top:1px solid var(--clean-line)}
.clean-next{justify-content:flex-end;margin:calc(12px * var(--ui-scale)) 0 0}
.clean-hint,.clean-note{color:var(--clean-muted);line-height:1.8}
.clean-page button,.clean-page a{font:inherit;border:1px solid var(--clean-line);border-radius:8px;padding:8px 12px;color:#456184;background:#fff;cursor:pointer;text-decoration:none;white-space:nowrap;line-height:1.5}
.clean-page button:hover:not(:disabled),.clean-page a:hover{border-color:#9bb8e6;background:#f2f6ff}
.clean-page button:disabled{opacity:.45;cursor:not-allowed}
.clean-page .clean-primary{background:var(--clean-blue);color:#fff;border-color:var(--clean-blue)}
.clean-page .clean-danger{color:#b15168}
.clean-page label{display:flex;flex-direction:column;gap:7px;min-width:0;margin-bottom:14px;font-size:var(--fs-12,12px);font-weight:600}
.clean-page label small,.clean-page label span{font-size:var(--fs-11,11px);font-weight:400;color:var(--clean-muted);line-height:1.7}
.clean-page input,.clean-page select,.clean-page textarea{box-sizing:border-box;width:100%;min-width:0;padding:8px 10px;border:1px solid var(--clean-line);border-radius:8px;background:#fff;color:#354d6b;font-family:inherit;font-size:var(--fs-12,12px);font-weight:400;line-height:1.6}
.clean-page textarea{resize:vertical}
.clean-segmented{display:flex;gap:4px;padding:3px;border:1px solid var(--clean-line);border-radius:9px;background:#eef2f8;margin-bottom:16px}
.clean-segmented button{flex:1;border:0;background:transparent;color:#6a7c91;font-weight:600}
.clean-segmented button.active{background:#fff;color:#3562bd;box-shadow:0 1px 4px rgba(48,75,112,.12)}
.clean-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:12px}
/* 结果面板顶部的工具栏：里面有「标题在上、控件在下」的 label，也有裸按钮和复选框行。
   `align-items:center` 会让按钮按整块 label 居中，于是按钮中心比下拉框中心高几像素
   （用户报的就是这个「没持平」）。改成按底对齐，并把这个行里控件的高度统一钉到 36px：
   高度一致之后，底对齐 = 上下边缘都对齐。 */
.clean-toolbar{display:flex;align-items:flex-end;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.clean-toolbar>label{margin-bottom:0}
/* 高度要**钉死**而不是只给下限：`min-height` 挡不住更高的那个 —— 按钮自带上下各
   8px padding，天然比 select 高 2px，底对齐之后顶边就差这 2px（浏览器套件量过）。
   `a` 也要：报告页的「下载 ZIP」是个 `<a>`，`.clean-page a` 只给了它按钮的外观、
   没给高度；行内元素认不得 height，得先变成 inline-flex 才能对齐、才能垂直居中。 */
.clean-toolbar :is(select,button){height:calc(36px * var(--ui-scale));padding-top:0;padding-bottom:0}
.clean-toolbar>a{display:inline-flex;align-items:center;height:calc(36px * var(--ui-scale));padding-top:0;padding-bottom:0}
.clean-toolbar>.clean-check{align-items:center;min-height:calc(36px * var(--ui-scale))}
/* 整行等宽的按钮（第 7 步的「获取处理预估 / 创建清洗任务」）。
   不写进 `.clean-primary`：那样第 4 步模型池里的按钮也会被拉成整行。 */
.clean-page .clean-block{display:block;width:100%;margin-bottom:10px}
.clean-hint code{padding:1px 5px;border-radius:4px;background:#eef2fa;color:#405e91;font:var(--fs-11,11px)/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
/* auto-fit 而不是写死两列：这一行里经常只有一个可见的子项（其余被 v-if 藏掉），
   写死两列会让那个输入框只占半宽、右半边空一大块。 */
/* 字段宽度只由这一行的可用宽度决定：一个字段就占满一行，两个字段各占一半。
   配置列变宽之后（58% 配比，1920 下 958、2560 下 1133），这一条是「空间变大」的直接来源 ——
   固定成两条轨道反而会让单字段行比 620px 那版更窄（450 < 550），方向就反了。
   --clean-field-max 只是超宽屏的兜底，不让单行字段拉成千像素长条。 */
.clean-two{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:0 12px;max-width:var(--clean-row-max)}
.clean-config input:not([type=checkbox]):not([type=radio]):not([type=file]),.clean-config select{max-width:var(--clean-field-max)}
/* 复选框行的选择器必须比 `.clean-page label`（一个类 + 一个标签）更具体，否则
   `flex-direction:column` 会赢，复选框跑到文字上面去。 */
.clean-page .clean-check{flex-direction:row;align-items:center;gap:8px}
.clean-page .clean-check input{width:auto}
/* 多选字段的勾选块：原生 `<select multiple>` 是个蓝底列表框，样式改不动，
   也看不出「选了几个」。这里用能换行的一排 chip 表达多选。 */
.clean-field-group{min-width:0;margin-bottom:14px}
.clean-check-label{display:block;margin-bottom:6px;color:#60758e;font-size:var(--fs-12,12px);font-weight:600}
.clean-check-group{display:flex;flex-wrap:wrap;gap:6px;align-content:flex-start;min-height:calc(38px * var(--ui-scale));padding:8px 10px;border:1px solid var(--clean-line);border-radius:8px;background:#fbfcfe}
.clean-check-group .clean-check{margin:0;padding:4px 9px;border:1px solid #dfe7f2;border-radius:999px;background:#fff;font-size:var(--fs-11,11px);font-weight:500;cursor:pointer}
.clean-check-group .clean-check:hover{border-color:#a9bfdf}
.clean-check-group .clean-check.on{border-color:#9db8e6;background:#eef3fc;color:#2f5bb0;font-weight:600}
.clean-check-group .clean-check input{margin:0;accent-color:var(--clean-blue)}
.clean-file{border:1px dashed #c9d7ea;border-radius:10px;padding:12px;background:#f7faff}
/* 两个并排的按钮；两个原生 input 藏在后面 —— 它们的系统控件（「未选择文件」+ 系统按钮）
   既不可换语言也不可换样式。`font-size:0` 那套是仓库既有做法（style.css 的 .image-upload-grid），
   这里用绝对定位 + 透明，效果相同但不受字号影响。 */
.clean-file-actions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px}
.clean-file-actions input[type=file]{position:absolute;width:1px;height:1px;padding:0;border:0;opacity:0;pointer-events:none}
.clean-file-actions button{width:100%;min-height:calc(38px * var(--ui-scale));border-color:#b9cbe6;background:#f1f6ff;color:#3562bd;font-weight:650}
.clean-file-hint{margin:0 0 10px;color:var(--clean-muted);font-size:var(--fs-11,11px);line-height:1.7}
.clean-file .clean-summary{margin-bottom:0}
/* 数字框的上下箭头是浏览器自带的：既占宽度又容易误触。保留 `type=number`（移动端数字
   键盘、min/max 语义都还指望它），只把箭头藏掉。 */
.clean-config input[type=number]{appearance:textfield;-moz-appearance:textfield}
.clean-config input[type=number]::-webkit-outer-spin-button,
.clean-config input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
/* 焦点态自己画，不用浏览器默认的 outline。`:invalid` 一律不画红框：数字框输到一半
   （比如只敲了个负号）必然短暂非法，红框会闪。校验统一在提交时用提示告知。 */
.clean-page input:focus-visible,.clean-page select:focus-visible,.clean-page textarea:focus-visible{outline:0;border-color:#87a7df;box-shadow:0 0 0 3px rgba(65,111,209,.12)}
.clean-summary{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:10px 12px;border:1px solid #d9e4f4;border-radius:9px;background:#f2f6fd;margin-bottom:14px}
.clean-summary b{font-size:var(--fs-12,12px)}
.clean-summary span{flex:1;min-width:0;color:var(--clean-muted);font-size:var(--fs-11,11px);overflow-wrap:anywhere}
.clean-inspect{display:grid;gap:5px;padding:10px 12px;border:1px solid #e2e8f1;border-radius:9px;background:#f8fafd;margin-bottom:14px}
.clean-inspect span{font-size:var(--fs-11,11px);color:#5d758f;line-height:1.7;overflow-wrap:anywhere}
.clean-warn{padding:9px 11px;border:1px solid #f0dcc0;border-radius:8px;background:#fff8ed;color:#956330;font-size:var(--fs-11,11px);line-height:1.75}
.clean-ok{padding:9px 11px;border:1px solid #cbe7dc;border-radius:8px;background:#f1faf6;color:#2f7c63;font-size:var(--fs-11,11px)}
.clean-dry,.clean-estimate{display:grid;gap:6px;padding:11px 12px;border:1px solid #dbe4f0;border-radius:9px;background:#f7fafe;margin-bottom:14px}
.clean-dry span,.clean-estimate span{font-size:var(--fs-11,11px);color:#5e7791;line-height:1.75}
.clean-estimate b{font-size:var(--fs-13,13px);color:#30507e}
.clean-fields{display:grid;gap:8px;margin-bottom:14px}
.clean-field{display:grid;grid-template-columns:1fr auto 1fr auto;align-items:center;gap:8px}
.clean-field small{grid-column:1/-1;color:#8296ab;font-size:var(--fs-10,10px)}
.clean-arrow{color:#9aabc0}
.clean-op,.clean-rule{padding:11px 12px;border:1px solid var(--clean-line);border-radius:9px;background:#fbfcfe;margin-bottom:10px}
/* 多条规则长得一模一样时，用户分不清哪张卡是哪条规则。左侧一道强调色边框 + 头部序号
   徽章把每张卡的开头钉出来（约束卡片本轮不加，见计划的范围外）。 */
.clean-op{border-left:4px solid #9db8e6;background:#f9fbff;margin-bottom:16px}
/* 首行是「序号 + 控件 + ×」。原生 select 是 width:100%（全局 `.panel select`），而
   .clean-row 带 flex-wrap:wrap —— 放进 flex 里，width:100% 的 select 会先占满整行，
   把 × 挤到下一行单独站着（实测 y 差 45px）。显式网格轨道不会有这个问题：轨道是
   auto 时 select 按内容取宽，× 紧跟其后。第三列是摘要，用 1fr 吃掉剩余宽度后再截断，
   这样切换算子时长文本不会把 × 推出卡片。 */
.clean-op-head{display:grid;grid-template-columns:auto minmax(0,auto) minmax(0,1fr) auto;align-items:center;gap:8px;margin-bottom:12px}
.clean-op-no{display:grid;place-items:center;width:calc(20px * var(--ui-scale));height:calc(20px * var(--ui-scale));border-radius:50%;background:#e6eefc;color:#3562bd;font-size:var(--fs-11,11px);font-style:normal;font-weight:700}
.clean-op-head select{max-width:calc(200px * var(--ui-scale))}
.clean-op-sum{color:var(--clean-muted);font-size:var(--fs-11,11px);font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.clean-rule>.clean-row:first-child{display:grid;grid-template-columns:minmax(0,auto) minmax(0,auto) auto;justify-content:start;align-items:center;gap:8px}
.clean-gen{border:1px solid var(--clean-line);border-radius:9px;background:#fbfcfe;padding:10px 12px;margin-bottom:10px}
.clean-fn-head{display:flex;align-items:center;gap:10px;margin:18px 0 10px}
.clean-fn-head b{font-size:var(--fs-12,12px)}
.clean-fn-head small{color:var(--clean-muted);font-size:var(--fs-11,11px);margin-right:auto}
/* 源码框的等宽字体在 PyCodeEditor 里（:deep 命中 CM 生成的 DOM），这里只管卡片这一层。 */
.clean-fn-source{min-width:0;margin-bottom:14px}
.clean-fn-actions{margin:0 0 12px}
/* `.clean-page label` 一律是列式（文字在上、控件在下），工具条又是 `align-items:flex-end`
   —— 控件底边对齐。这一颗必须**定宽**：不定宽时输入框会被压成「刚好装下 3」那么窄的一条
   （65px），四个字比它还宽，看上去像飘在输入框外面。宽度照着第 6 步那条同名工具条给。 */
.clean-fn-actions>label{margin-bottom:0;flex:0 0 auto;width:calc(120px * var(--ui-scale))}
.clean-fn-actions input[type=number]{width:100%}
.clean-fn-test{padding:11px 12px;border:1px solid var(--clean-line);border-radius:9px;background:#f7fafe;margin-bottom:12px;min-width:0}
.clean-fn-test .clean-hint{margin:0 0 8px}
.clean-fn-test .clean-warn{margin:0 0 8px}
.clean-fn-table{max-height:calc(360px * var(--ui-scale));margin-bottom:8px}
.clean-fn-table td.clean-fn-note{white-space:normal;max-width:calc(320px * var(--ui-scale));color:#a8566a}
/* 出错的那一行整行标红（备注格子才有字，其余格子是空的）。选择器要压过斑马纹那条
   （`.clean-stats tbody tr:nth-child(even) td`），所以带上 `.clean-fn-test` 这一层。 */
.clean-fn-test .clean-stats tbody tr.bad td{background:#fdf0f1}
.clean-fn-test .clean-stats tbody tr.bad td:first-child{background:#fdf0f1}
.clean-fn-print{max-height:calc(200px * var(--ui-scale));margin-bottom:8px}
.clean-fn-test .clean-list{margin-bottom:0}
.clean-fn code{padding:1px 4px;border-radius:4px;color:#405e91;background:#edf2fa;font:var(--fs-10,10px)/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere}
.clean-gen summary{cursor:pointer;display:flex;align-items:center;justify-content:space-between;gap:10px;font-size:var(--fs-12,12px)}
.clean-gen summary small{color:var(--clean-muted);font-size:var(--fs-11,11px)}
.clean-gen>*:not(summary){margin-top:12px}
.clean-prompt{display:grid;gap:8px}
.clean-chips{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.clean-chips span{font-size:var(--fs-11,11px);color:var(--clean-muted)}
.clean-chips button{padding:4px 8px;font-size:var(--fs-11,11px);border-radius:999px}
.clean-chips button.active{border-color:#9db8e6;background:#eef3fc;color:#2f5bb0;font-weight:600}
.clean-locale{margin:7px 0 0}
.clean-guide{padding:12px 14px;border:1px solid var(--clean-line);border-radius:10px;background:#f5f8fd;margin-bottom:14px}
.clean-guide summary{cursor:pointer;font-size:var(--fs-12,12px);font-weight:600}
.clean-guide>*:not(summary){margin-top:12px}
/* 自定义函数的契约 4 条与可运行示例。默认源码骨架信息量太少（`return {}` 看不出能写
   什么），所以契约 + 例子都放在源码框**上面**：写之前先看得到，而不是写砸了才回头翻。 */
.clean-list{margin:0 0 10px;padding-left:20px;color:#5e7791;font-size:var(--fs-11,11px);line-height:1.85}
.clean-list li{margin-bottom:3px}
.clean-list b{color:#456184}
.clean-sample{padding:10px 12px;border:1px solid #dfe7f2;border-radius:9px;background:#fff;margin-bottom:10px}
.clean-sample header{display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap}
.clean-sample header b{font-size:var(--fs-12,12px)}
.clean-sample header small{flex:1;min-width:0;color:var(--clean-muted);font-size:var(--fs-11,11px);line-height:1.7}
.clean-sample pre{margin:0;padding:9px 11px;border-radius:8px;background:#f4f7fc;color:#3b5680;font:var(--fs-11,11px)/1.75 ui-monospace,SFMono-Regular,Menlo,monospace;overflow:auto;white-space:pre}
.clean-fn-guide{padding:11px 12px;border:1px solid #dfe7f2;border-radius:9px;background:#f5f8fd;margin-bottom:12px}
.clean-guide-toggle{padding:6px 10px;font-size:var(--fs-11,11px);border-color:#b9cbe6;background:#f1f6ff;color:#3562bd;font-weight:600}
.clean-browse{display:flex;align-items:center;gap:10px}
.clean-browse code{font-size:var(--fs-11,11px);color:#5a76a3;overflow-wrap:anywhere}
.clean-browse-list{display:flex;flex-wrap:wrap;gap:6px;max-height:220px;overflow:auto}
.clean-browse-list button{font-size:var(--fs-11,11px);padding:6px 9px}
.clean-pool{display:grid;gap:8px}
.clean-page .clean-pool label{align-items:flex-start;border:1px solid #e2e8f1;border-radius:9px;padding:9px 11px;background:#fff;margin-bottom:0}
.clean-pool label b{font-size:var(--fs-12,12px)}
.clean-pool label small{display:block;margin-top:4px}
.clean-endpoint{padding:12px;border:1px solid #cbdaf2;border-radius:10px;background:#f7faff;margin-top:12px}
/* 粘在视口上：配置栏很长，舞台不能跟着滚走。祖先链里不能有 overflow:hidden，
   否则 sticky 会静默失效 —— 这也是 .clean-workspace 被移出 ui-refinement.css 那几条
   共享规则的原因（那里带着 overflow:hidden）。
   高度按视口给（92dvh，上限 1200px）：表要看的行多，天花板压到 900px 时大屏上每屏白丢
   四五行。92dvh 加上 18px 的粘顶仍在视口内，不会把自己的下半截顶到屏幕外面去。 */
.clean-stage{position:sticky;top:calc(18px * var(--ui-scale));height:min(92dvh,1200px);min-height:520px;min-width:0;display:flex;flex-direction:column;border:1px solid var(--clean-line);border-radius:14px;background:#fff;overflow:hidden}
.clean-tabs{display:flex;align-items:center;gap:6px;padding:12px 14px;border-bottom:1px solid var(--clean-line);flex-wrap:wrap}
.clean-tabs button{border:0;background:transparent;color:#6a7c91;font-weight:600}
.clean-tabs button.active{background:#eef3fc;color:#3562bd}
.clean-stage-meta{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-left:auto;min-width:0}
.clean-current{font-size:var(--fs-11,11px);color:var(--clean-muted);overflow-wrap:anywhere}
/* 结果区的来源开关：源数据 / 函数试跑 / 试跑预览 / 处理预估 / 任务结果。几份展示长得像，
   所以必须有这个开关说明在看哪一份。5 颗按钮在 390 视口下比容器还宽，而按钮是
   `white-space:nowrap` —— flex 压不动文字就会溢出、整页跟着横向滚动，所以这里允许换行。 */
.clean-source{display:flex;flex-wrap:wrap;gap:4px;padding:3px;border:1px solid var(--clean-line);border-radius:9px;background:#eef2f8}
.clean-source button{border:0;background:transparent;padding:5px 10px;font-size:var(--fs-11,11px);color:#6a7c91;font-weight:600}
.clean-source button.active{background:#fff;color:#3562bd;box-shadow:0 1px 4px rgba(48,75,112,.12)}
.clean-panel{flex:1;min-height:0;overflow:auto;padding:14px 16px}
.clean-empty{flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;gap:8px;color:var(--clean-muted);padding:24px}
.clean-empty h3{color:#405a7b;margin:0;font-size:var(--fs-17,17px)}
.clean-compare{display:grid;grid-template-columns:1fr 1fr;gap:12px;align-items:start}
.clean-pane{max-height:min(60vh,640px);overflow:auto;border:1px solid var(--clean-line);border-radius:10px;overscroll-behavior:contain}
.clean-pane table{border-collapse:collapse;width:100%;font-size:var(--fs-11,11px);white-space:nowrap}
.clean-pane th,.clean-pane td{padding:7px 9px;border-bottom:1px solid #eef2f7;text-align:left;max-width:220px;overflow:hidden;text-overflow:ellipsis}
.clean-pane thead th{position:sticky;top:0;background:#f0f5fc;z-index:1}
.clean-pane .clean-index{color:#9aabc0;background:#fafcff}
/* 斑马纹必须把差异着色排除掉，否则偶数行会把「新增/修改/清空」的底色整片盖掉 ——
   那三种颜色正是这张表存在的理由。 */
.clean-pane tbody tr:nth-child(even) td:not(.added):not(.changed):not(.removed){background:#fafcff}
.clean-pane td.added{background:#eefaf3;color:#2f7c63}
.clean-pane td.changed{background:#fff6e8;color:#8a6224}
.clean-pane td.removed{background:#fdf0f1;color:#a8566a}
/* 会被丢弃的源列（只出现在左栏，右栏没有）：灰掉、划一道线，一眼看出它不进产物。
   斑马纹同样要排除它，否则偶数行会把灰底盖掉。 */
.clean-pane td.dropped,.clean-pane th.dropped{color:#9aa7b6;background:#f7f9fb}
.clean-pane td.dropped{text-decoration:line-through}
.clean-pane tbody tr:nth-child(even) td.dropped{background:#f7f9fb}
/* 表头上的小标记：删除 / 新增。 */
.clean-col-tag{margin-left:5px;padding:1px 5px;border-radius:5px;background:#fdf0f1;color:#a8566a;font-size:var(--fs-10,10px);font-style:normal;font-weight:600}
.clean-col-tag.added{background:#eefaf3;color:#2f7c63}
.clean-pane th.added{background:#e9f7f0}
/* 结果区「源数据」里的实时预览：每一列映射到哪个目的字段。与「新增」同色（都是「这一列有用」），
   未映射的列走上面 `.dropped` 那套（灰底 + 删除线）—— 与「删除」是同一个意思。 */
.clean-col-tag.mapped{background:#eefaf3;color:#2f7c63}
.clean-map-preview{margin:0}
/* 试跑的「各字段违规/回退」小表：通常只有几行，不必跟着统计表一起撑到 560px。 */
.clean-dry-fields{max-height:min(34vh,320px);margin-top:12px}
/* 窄屏（390）下这三个小标签会被压成竖排的两个字（容器不换行、标签又能被压缩）。
   标签本身不许压缩、整行允许折行，长句子自己占一行。 */
.clean-legend{display:flex;flex-wrap:wrap;gap:8px 12px;align-items:center;color:var(--clean-muted);font-size:var(--fs-11,11px);margin:12px 0 0}
.clean-legend span{padding:2px 8px;border-radius:6px;flex:none;white-space:nowrap}
.clean-legend .added{background:#eefaf3;color:#2f7c63}
.clean-legend .changed{background:#fff6e8;color:#8a6224}
.clean-legend .removed{background:#fdf0f1;color:#a8566a}
.clean-logs{max-height:min(66vh,700px);overflow:auto;border:1px solid var(--clean-line);border-radius:10px;background:#fbfcfe;padding:10px 12px;font:var(--fs-11,11px)/1.75 ui-monospace,SFMono-Regular,Menlo,monospace;overscroll-behavior:contain}
.clean-logs p{display:flex;gap:10px;margin:0 0 3px;overflow-wrap:anywhere}
.clean-logs time{color:#a3b2c4;flex-shrink:0}
.clean-logs .lv-warn span{color:#8a6224}
.clean-logs .lv-error span{color:#a8566a}
.clean-report{overflow-wrap:anywhere;line-height:1.85}
.clean-report :deep(h1){font-size:var(--fs-19,19px);margin:6px 0 14px}
.clean-report :deep(h2){font-size:var(--fs-15,15px);margin:22px 0 10px;padding-bottom:8px;border-bottom:1px solid var(--clean-line)}
.clean-report :deep(h3){font-size:var(--fs-13,13px);margin:16px 0 8px}
.clean-report :deep(table){border-collapse:collapse;width:100%;margin:10px 0;font-size:var(--fs-11,11px)}
.clean-report :deep(th),.clean-report :deep(td){padding:7px 9px;border:1px solid #e2e8f1;text-align:left;vertical-align:top}
.clean-report :deep(th){background:#f0f5fc}
.clean-report :deep(code){padding:1px 5px;border-radius:4px;background:#eef2fa;color:#405e91;font:var(--fs-11,11px)/1.5 ui-monospace,monospace}
.clean-report :deep(blockquote){margin:10px 0;padding:8px 12px;border-left:3px solid #cbdaf2;background:#f7fafe;color:#5e7791;font-size:var(--fs-11,11px)}
.clean-report :deep(.md-table-scroll){overflow:auto}
/* 字段统计：6 列里好几列是长句子，压到 100% 宽就是每格都在折行。照仓库既有做法
   （style.css 的 .database-table-wrap）给一层可横向滚动的壳：列不被挤压，需要时出滚动条。
   首列粘住左边 —— 横向滚到右边时还得知道这一行是哪个字段。 */
.clean-table-wrap{overflow:auto;max-height:min(58vh,560px);border:1px solid var(--clean-line);border-radius:10px;background:#fff;overscroll-behavior:contain}
.clean-stats{width:max-content;min-width:100%;border-collapse:collapse;font-size:var(--fs-11,11px)}
.clean-stats th,.clean-stats td{padding:8px 10px;border-bottom:1px solid #eef2f7;text-align:left;vertical-align:top;white-space:nowrap}
.clean-stats th{background:#f0f5fc;position:sticky;top:0;z-index:3}
.clean-stats th:first-child,.clean-stats td:first-child{position:sticky;left:0;background:#f0f5fc;z-index:2}
.clean-stats tbody tr:nth-child(even) td{background:#fafcff}
/* 斑马纹要跟着首列走，否则滚动时粘住的那一列是另一套底色，看起来像错位 */
.clean-stats tbody tr:nth-child(even) td:first-child{background:#fafcff}
.clean-tasks{margin-bottom:0}
.clean-task{padding:16px 18px;background:#fff;border:1px solid var(--clean-line);border-radius:12px;margin-bottom:12px}
/* 续跑原因的正文里会出现路径与根目录列表，那是不含空格的长串；不强制断词的话
   390px 视口下整页会横向溢出（实测 3px）。 */
.clean-task .clean-note{grid-column:1/-1;margin:8px 0 0;font-size:var(--fs-11,11px);overflow-wrap:anywhere}
/* 1100 而不是 1050：58% 配比的拐点在 1128px（620 + 24 + 360），再往下两栏就装不下了 ——
   硬留着会横向溢出。这里提前收成一栏，配置列还是 620 的下限宽度。
   单列之后舞台不再粘（一列布局里粘住等于挡住表单），但要给个下限高度，
   否则空态下它塌成一条 160px 的横条，看起来像坏了。 */
@media(max-width:1100px){.clean-workspace{grid-template-columns:minmax(0,1fr);gap:18px}.clean-compare{grid-template-columns:1fr}.clean-stage{position:static;height:auto;min-height:calc(420px * var(--ui-scale))}}
@media(max-width:800px){.clean-badge{display:none}.clean-two{grid-template-columns:1fr}
  /* 窄屏放不下「序号 + 下拉 + 摘要 + ×」一行：摘要换到第二行整行显示，而不是被截成
     「字段：…」——多张卡片可比的就是这一句摘要。 */
  .clean-op-head{grid-template-columns:auto minmax(0,1fr) auto}
  .clean-op-sum{grid-column:1/-1;white-space:normal}
  .clean-op-head select{max-width:100%}}
@media(max-width:480px){.clean-field{grid-template-columns:1fr}.clean-arrow{display:none}.clean-header h1{font-size:var(--fs-25,25px)}
  /* 5 颗来源按钮在这个宽度下换行后还是偏宽，把左右内边距收一点，尽量一行装下。 */
  .clean-source button{padding:5px 7px}}
</style>
