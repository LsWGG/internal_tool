<script setup>
import {computed, nextTick, onBeforeUnmount, onMounted, ref, watch} from 'vue'
import axios from 'axios'

const emit = defineEmits(['notify'])
const file = ref(null), result = ref(null), selected = ref(null)
const busy = ref(false), exporting = ref(false), syncing = ref(false), error = ref('')
const progress = ref(0), progressText = ref(''), downloadUrl = ref('')
const exportPhase = ref('idle'), exportNotice = ref('')
const exportTaskVisible = ref(false)
const canvas = ref(null), previewImage = ref(null)
const zoom = ref(1), pan = ref({x:0,y:0}), dragging = ref(false), fullscreen = ref(false)
const theme = ref('neutral')
const custom = ref({primaryColor:'#4f6fd8', lineColor:'#6b7fa3', textColor:'#20324d', mainBkg:'#f7faff'})
const themes = [['neutral','商务黑'], ['default','清晰蓝紫'], ['forest','森林绿'], ['monochrome','黑白'], ['custom','自定义']]
const selectedImage = computed(() => result.value?.images.find(image => image.index === selected.value))
const readyCount = computed(() => result.value?.images.filter(image => image.status === 'ready').length || 0)
const settings = computed(() => JSON.stringify({theme:theme.value, custom:theme.value === 'custom' ? custom.value : {}}))
const statusText = {idle:'点击渲染', queued:'等待渲染…', rendering:'正在渲染…', ready:'已预览', failed:'渲染失败 · 点击重试'}
let generation = 0, pollTimer, themeTimer, disposed = false
let themeRequest = Promise.resolve()
let downloadController = null
const message = e => e.response?.data?.detail || e.message || '请求失败，请重试'
const zoomText = computed(() => `${Math.round(zoom.value * 100)}%`)
const exportPhaseText = computed(() => ({
  rendering:'正在生成高清图片', packaging:'正在打包图片', downloading:'正在下载 ZIP',
  completed:'导出已完成', waiting:'后台仍在生成', failed:'导出失败'
}[exportPhase.value] || '等待导出'))
const exportStatus = computed(() => ({
  rendering:'running', packaging:'running', downloading:'running', completed:'completed', waiting:'paused', failed:'failed'
}[exportPhase.value] || 'queued'))
const exportStatusText = computed(() => ({running:'进行中', completed:'已完成', paused:'后台生成', failed:'失败', queued:'等待中'}[exportStatus.value]))
const exportButtonText = computed(() => exportPhase.value === 'waiting' ? '继续等待导出' : '导出全部图片 ZIP')
const archiveUrl = computed(() => result.value?.id && exportPhase.value === 'completed' ? `/api/mermaid/tasks/${result.value.id}/export` : '')
let dragOrigin = null

function resetView() {
  zoom.value = 1
  pan.value = {x:0, y:0}
}
function setZoom(value, center = null) {
  const next = Math.min(5, Math.max(.25, Number(value.toFixed(2))))
  if (next === zoom.value) return
  if (center && canvas.value) {
    const box = canvas.value.getBoundingClientRect()
    const point = {x:center.clientX - box.left - box.width / 2, y:center.clientY - box.top - box.height / 2}
    const ratio = next / zoom.value
    pan.value = {x:point.x - (point.x - pan.value.x) * ratio, y:point.y - (point.y - pan.value.y) * ratio}
  }
  zoom.value = next
  if (next === 1) pan.value = {x:0,y:0}
}
function zoomIn() { setZoom(zoom.value + (zoom.value < 1 ? .25 : .5)) }
function zoomOut() { setZoom(zoom.value - (zoom.value <= 1 ? .25 : .5)) }
function onWheel(event) { setZoom(zoom.value * (event.deltaY < 0 ? 1.12 : .89), event) }
function startDrag(event) {
  if (!selectedImage.value?.url || event.button !== 0) return
  dragging.value = true
  dragOrigin = {x:event.clientX - pan.value.x, y:event.clientY - pan.value.y}
  event.currentTarget.setPointerCapture?.(event.pointerId)
}
function dragView(event) {
  if (!dragging.value || !dragOrigin) return
  pan.value = {x:event.clientX - dragOrigin.x, y:event.clientY - dragOrigin.y}
}
function endDrag(event) {
  dragging.value = false
  dragOrigin = null
  event.currentTarget?.releasePointerCapture?.(event.pointerId)
}
async function toggleFullscreen() {
  if (!canvas.value) return
  try {
    if (!document.fullscreenElement) await canvas.value.requestFullscreen()
    else await document.exitFullscreen()
  } catch (e) { error.value = `无法进入全屏：${message(e)}` }
}
function syncFullscreen() {
  fullscreen.value = document.fullscreenElement === canvas.value
  nextTick(resetView)
}

function stopPolling() { clearTimeout(pollTimer) }
function revokeDownload() {
  if (downloadUrl.value) URL.revokeObjectURL(downloadUrl.value)
  downloadUrl.value = ''
}
function resetExportState() {
  exportPhase.value = 'idle'
  exportNotice.value = ''
  progress.value = 0
  progressText.value = ''
}
function stopExportWait() {
  if (exportPhase.value === 'downloading') {
    downloadController?.abort()
    return
  }
  stopPolling()
  exporting.value = false
  exportPhase.value = 'waiting'
  exportNotice.value = '已停止等待；图片仍在后台生成，可稍后再次点击“导出图片 ZIP”。'
}
function choose(event) {
  generation++
  stopPolling()
  clearTimeout(themeTimer)
  file.value = event.target.files[0] || null
  result.value = null
  selected.value = null
  syncing.value = false
  error.value = ''
  resetExportState()
  revokeDownload()
}
function themeData() {
  const data = new FormData()
  data.append('theme', theme.value)
  if (theme.value === 'custom') data.append('custom_theme', JSON.stringify(custom.value))
  return data
}
async function previewFile() {
  if (!file.value || busy.value || exporting.value) return
  const current = ++generation
  stopPolling()
  clearTimeout(themeTimer)
  syncing.value = false
  busy.value = true
  error.value = ''
  resetExportState()
  revokeDownload()
  try {
    const data = themeData()
    data.append('file', file.value, file.value.name)
    const response = await axios.post('/api/mermaid/preview', data)
    if (disposed || current !== generation) return
    result.value = response.data
    selected.value = null
    emit('notify', `已识别 ${result.value.count} 张图片，点击列表中的图片生成预览。`, 'success')
  } catch (e) { if (current === generation) error.value = message(e) }
  finally { busy.value = false }
}
function schedulePoll(current) {
  stopPolling()
  pollTimer = setTimeout(() => poll(current), 500)
}
async function poll(current) {
  if (disposed || current !== generation || !result.value) return
  try {
    const {data} = await axios.get(`/api/mermaid/tasks/${result.value.id}`)
    if (disposed || current !== generation) return
    result.value = data
    if (exporting.value) {
      progress.value = data.progress
      progressText.value = data.message
      if (data.status === 'failed') {
        exporting.value = false
        exportPhase.value = 'failed'
        error.value = data.error || '导出失败，请重试'
      } else if (data.status === 'completed') {
        exporting.value = false
        progress.value = 100
        progressText.value = `已生成 ${data.count} 张高清图片，点击“导出”下载 ZIP`
        exportPhase.value = 'completed'
        return
      } else {
        exportPhase.value = data.progress >= 95 ? 'packaging' : 'rendering'
      }
    }
    if (exporting.value || data.images.some(image => ['queued','rendering'].includes(image.status))) schedulePoll(current)
  } catch (e) {
    if (current !== generation || disposed) return
    error.value = `${message(e)}；正在重试获取进度…`
    schedulePoll(current)
  }
}
async function selectImage(index) {
  if (syncing.value || busy.value || exporting.value) return
  selected.value = index
  resetView()
  const image = selectedImage.value
  if (['ready','queued','rendering'].includes(image.status)) return
  const current = generation
  error.value = ''
  image.status = 'queued'
  try {
    const {data} = await axios.post(`/api/mermaid/tasks/${result.value.id}/images/${index}/render`)
    if (disposed || current !== generation) return
    result.value = data
    schedulePoll(current)
  } catch (e) {
    if (current !== generation) return
    image.status = 'failed'
    image.error = message(e)
    error.value = message(e)
  }
}
async function applyTheme(current) {
  if (!result.value || disposed || current !== generation) return
  try {
    // Serialize updates so rapid color changes cannot arrive at the server out of order.
    const taskId = result.value.id
    const payload = themeData()
    const request = themeRequest.catch(() => {}).then(() => {
      if (disposed || current !== generation) return null
      return axios.post(`/api/mermaid/tasks/${taskId}/theme`, payload)
    })
    themeRequest = request
    const response = await request
    if (disposed || current !== generation) return
    result.value = response.data
    syncing.value = false
    // Only the currently selected image is refreshed; other images remain lazy.
    if (selected.value !== null) await selectImage(selected.value)
  } catch (e) {
    if (current === generation) error.value = `主题更新失败：${message(e)}`
  }
}
watch(settings, () => {
  if (!result.value || exporting.value) return
  const current = ++generation
  stopPolling()
  clearTimeout(themeTimer)
  revokeDownload()
  syncing.value = true
  error.value = ''
  resetExportState()
  result.value.images = result.value.images.map(({index,title,name}) => ({index,title,name,status:'idle'}))
  delete result.value.archive
  themeTimer = setTimeout(() => applyTheme(current), 300)
})
async function renderFile() {
  if (!result.value || exporting.value || syncing.value) return
  const current = generation
  exporting.value = true
  exportTaskVisible.value = true
  error.value = ''
  exportNotice.value = ''
  exportPhase.value = 'rendering'
  progress.value = 1
  progressText.value = '正在准备导出…'
  try {
    const {data} = await axios.post(`/api/mermaid/tasks/${result.value.id}/render`)
    if (disposed || current !== generation) return
    result.value = data
    if (data.status === 'completed') {
      exporting.value = false
      progress.value = 100
      progressText.value = `已生成 ${data.count} 张高清图片，点击“导出”下载 ZIP`
      exportPhase.value = 'completed'
    } else schedulePoll(current)
  } catch (e) { exportPhase.value = 'failed'; error.value = message(e); exporting.value = false }
}
async function deleteExportTask() {
  if (!result.value?.id) return
  const taskId = result.value.id
  generation++
  stopPolling()
  try {
    await axios.delete(`/api/mermaid/tasks/${taskId}`)
    result.value = null
    selected.value = null
    exportTaskVisible.value = false
    resetExportState()
    revokeDownload()
    emit('notify', '下载任务已删除。', 'success')
  } catch (e) { error.value = `删除下载任务失败：${message(e)}` }
}
onBeforeUnmount(() => {
  disposed = true
  generation++
  stopPolling()
  clearTimeout(themeTimer)
  downloadController?.abort()
  revokeDownload()
  document.removeEventListener('fullscreenchange', syncFullscreen)
})
onMounted(() => document.addEventListener('fullscreenchange', syncFullscreen))
</script>

<template>
  <section class="mermaid-page">
    <header><div><span class="eyebrow">MERMAID EXPORT</span><h1>Markdown 转 Mermaid 图片</h1><p>提取图表、按需预览，按当前主题导出高清 PNG 压缩包。</p></div></header>
    <div class="mermaid-workspace">
      <aside class="panel mermaid-config">
        <div class="panel-title"><b>导出设置</b><span>PNG / ZIP</span></div>
        <label>Markdown 文件<input type="file" accept=".md,.markdown,text/markdown" :disabled="busy || exporting" @change="choose"></label>
        <div class="file-summary">{{ file ? `已选择 ${file.name}` : '请选择 UTF-8 编码的 Markdown 文件' }}</div>
        <label>图片主题<select v-model="theme" :disabled="busy || exporting"><option v-for="item in themes" :key="item[0]" :value="item[0]">{{ item[1] }}</option></select></label>
        <div v-if="theme === 'custom'" class="mermaid-custom">
          <label>节点主色<input v-model="custom.primaryColor" type="color" :disabled="exporting || busy"></label>
          <label>连线颜色<input v-model="custom.lineColor" type="color" :disabled="exporting || busy"></label>
          <label>文字颜色<input v-model="custom.textColor" type="color" :disabled="exporting || busy"></label>
          <label>背景颜色<input v-model="custom.mainBkg" type="color" :disabled="exporting || busy"></label>
        </div>
        <div class="mermaid-guide"><b>{{result ? '第 2 步：按需检查' : '第 1 步：读取图表列表'}}</b><span>{{result ? '点选任意图表开始渲染；不必逐张预览。' : '不会立即渲染图片，先确认图表标题与数量。'}}</span></div>
        <button class="primary mermaid-submit" :disabled="busy || exporting || syncing || !file" @click="previewFile">{{ busy ? '正在读取图表…' : result ? '重新读取图表列表' : '读取图表列表' }}</button>
        <button v-if="result" class="mermaid-render-button" :disabled="exporting || syncing || busy" @click="renderFile">{{ exporting ? `${exportPhaseText} ${progress}%` : exportButtonText }}</button>
        <div v-if="syncing" class="mermaid-sync" role="status">正在应用主题… <button v-if="error" @click="applyTheme(generation)">重试</button></div>
        <p v-if="error" class="error mermaid-error" role="alert">{{ error }}</p>
      </aside>
      <section class="mermaid-preview">
        <div class="pdf-preview-head"><div><b>导出前图片预览</b><small>{{ result ? `共 ${result.count} 张图片 · 已预览 ${readyCount} 张` : '读取 Markdown 后，这里会显示图表列表' }}</small></div><span v-if="result" class="mermaid-preview-count">按需渲染</span></div>
        <div v-if="result" class="mermaid-preview-body">
          <nav aria-label="Mermaid 图片列表">
            <button v-for="image in result.images" :key="image.index" :class="{active:image.index === selected}" :aria-current="image.index === selected ? 'true' : undefined" :title="image.title" :disabled="syncing || exporting" @click="selectImage(image.index)">
              <span class="mermaid-index">{{ String(image.index).padStart(2,'0') }}</span>
              <span class="mermaid-list-copy"><b>{{ image.title }}</b><small :class="{'error':image.status === 'failed'}">{{ statusText[image.status] }}</small></span>
            </button>
          </nav>
          <div ref="canvas" class="mermaid-canvas" :class="{dragging,fullscreen}" aria-live="polite" @wheel.prevent="onWheel" @pointerdown="startDrag" @pointermove="dragView" @pointerup="endDrag" @pointercancel="endDrag">
            <div v-if="selectedImage?.url" class="mermaid-view-controls" @pointerdown.stop>
              <button type="button" title="缩小" aria-label="缩小图片" :disabled="zoom <= .25" @click="zoomOut">−</button>
              <button type="button" class="mermaid-zoom-value" title="恢复适应画布" @click="resetView">{{ zoomText }}</button>
              <button type="button" title="放大" aria-label="放大图片" :disabled="zoom >= 5" @click="zoomIn">＋</button>
              <i></i>
              <button type="button" :title="fullscreen ? '退出全屏' : '全屏查看'" :aria-label="fullscreen ? '退出全屏' : '全屏查看'" @click="toggleFullscreen">{{ fullscreen ? '↙' : '⛶' }}</button>
            </div>
            <template v-if="selectedImage">
              <div v-if="selectedImage.url" class="mermaid-image-stage">
                <img ref="previewImage" :key="selectedImage.url" :src="selectedImage.url" :alt="selectedImage.title" draggable="false" :style="{transform:`translate3d(${pan.x}px,${pan.y}px,0) scale(${zoom})`}" @load="resetView">
              </div>
              <div v-else class="mermaid-canvas-state"><b>{{ syncing ? '正在更新主题…' : statusText[selectedImage.status] }}</b><p v-if="selectedImage.error" class="error">{{ selectedImage.error }}</p><button v-if="selectedImage.status === 'failed'" @click="selectImage(selected)">重试渲染</button></div>
              <div v-if="selectedImage.url" class="mermaid-caption"><b>{{ selectedImage.title }}</b></div>
            </template>
            <div v-else class="mermaid-canvas-state"><b>选择图片开始预览</b><p>图片列表已就绪，点击左侧任意图片即可渲染。</p><small>无需逐张预览，也可以直接导出全部图片。</small></div>
          </div>
        </div>
        <div v-else class="pdf-help-empty"><div class="md-word-empty-icon">⌁</div><b>Mermaid 图片预览</b><p>上传 Markdown 后，在这里查看图表列表。</p></div>
      </section>
    </div>
    <section class="queue" aria-live="polite">
      <div class="queue-head"><div><span class="eyebrow">TASK QUEUE</span><h2>下载任务</h2></div><button class="refresh" @click="poll(generation)">刷新</button></div>
      <div v-if="!exportTaskVisible" class="empty">暂无下载任务。完成图表检查后，点击“导出全部图片 ZIP”创建任务。</div>
      <article v-else class="task">
        <div class="task-main"><div class="task-icon" aria-hidden="true"><svg class="ai-icon" viewBox="0 0 24 24"><path d="M4 19V5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2m4-4 3-3 2 2 3-4"/></svg></div><div><b>{{ result?.name || 'Mermaid 图片' }}</b><small>{{ result?.count || 0 }} 张图片 · {{ themes.find(item=>item[0]===theme)?.[1] || theme }}主题 · ZIP</small></div></div>
        <div class="progress"><div><span>{{ progressText || exportPhaseText }}</span><strong>{{ progress }}%</strong></div><div class="track"><i :class="exportStatus" :style="{width:progress+'%'}"></i></div><small v-if="exportNotice">{{ exportNotice }}</small></div>
        <span class="badge" :class="exportStatus">{{ exportStatusText }}</span>
        <div class="task-actions"><button v-if="exporting" @click="stopExportWait">暂停</button><button v-if="exportStatus==='failed'" class="retry" @click="renderFile">重试</button><button v-if="exportStatus==='paused'" class="resume" @click="renderFile">继续</button><a v-if="archiveUrl" class="export" :href="archiveUrl">导出</a><button class="danger" @click="deleteExportTask">删除</button></div>
      </article>
    </section>
  </section>
</template>

<style scoped>
.mermaid-render-button{width:100%;margin-top:10px;padding:11px;border:1px solid #4169d8;border-radius:9px;color:#315fc8;background:#edf4ff;font-weight:700;cursor:pointer}
button:disabled{opacity:.5;cursor:wait}
.mermaid-guide{display:grid;gap:5px;margin:17px 0 0;padding:12px;border:1px solid #dce6f2;border-radius:9px;background:#f7faff;color:#687d96;font-size:11px;line-height:1.6}.mermaid-guide b{color:#42638b;font-size:11px}.mermaid-guide span{display:block}
.mermaid-preview-count{padding:5px 8px;border-radius:6px;color:#5778a9;background:#edf4ff;font-size:10px;font-weight:700;white-space:nowrap}
.mermaid-preview-body{grid-template-columns:250px minmax(0,1fr)}
.mermaid-preview-body nav{display:flex;align-content:flex-start;flex-direction:column;gap:2px;max-height:650px;overflow-y:auto}
.mermaid-preview-body nav button{display:flex;align-items:center;gap:9px;flex:0 0 44px;box-sizing:border-box;min-height:44px;height:44px;max-height:44px;padding:4px 8px;white-space:normal;margin:0;border:1px solid transparent;background:transparent;overflow:hidden;transition:background .15s,border-color .15s}
.mermaid-preview-body nav button:hover{background:#edf3fb}
.mermaid-preview-body nav button.active{border-color:#b6cbf3;background:#e7f0ff;box-shadow:inset 3px 0 #4169d8}
.mermaid-preview-body nav button:focus-visible{outline:2px solid #4169d8;outline-offset:2px}
.mermaid-index{display:flex;width:28px;height:24px;margin:0;align-items:center;justify-content:center;flex:0 0 auto;border-radius:6px;background:#e8eef6;color:#7c91ac;font:600 11px ui-monospace,monospace}
.active .mermaid-index{background:#4169d8;color:#fff}
.mermaid-list-copy{display:block;min-width:0;margin:0;font:inherit;color:inherit;line-height:1}
nav b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px;line-height:1.2;margin:0;color:#38516f}
nav small{display:block;margin-top:1px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:10px;line-height:1.2;font-weight:400;color:#5273a4}
nav small.error{color:#c74545}
.mermaid-canvas{position:relative;display:flex;min-height:450px;padding:0!important;overflow:hidden!important;background:#f7f9fc!important;touch-action:none;user-select:none}
.mermaid-image-stage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:72px 40px 64px;overflow:hidden}
.mermaid-image-stage img{display:block;max-width:100%!important;max-height:100%!important;width:auto;height:auto;object-fit:contain;background:#fff;box-shadow:0 8px 30px #28466418!important;transform-origin:center center;transition:transform .12s ease;will-change:transform;cursor:grab}
.mermaid-canvas.dragging .mermaid-image-stage img{cursor:grabbing;transition:none}
.mermaid-view-controls{position:absolute;z-index:3;top:16px;right:16px;display:flex;align-items:center;gap:4px;padding:5px;border:1px solid #d7e0ec;border-radius:10px;background:#fffffff2;box-shadow:0 5px 18px #294a701a;backdrop-filter:blur(8px)}
.mermaid-view-controls button{display:grid;place-items:center;min-width:32px;height:30px;padding:0 8px;border:0;border-radius:6px;background:transparent;color:#385575;font-size:17px;line-height:1;cursor:pointer}
.mermaid-view-controls button:hover{background:#eaf1fb;color:#315fc8}
.mermaid-view-controls button:disabled{opacity:.35;cursor:default}
.mermaid-view-controls .mermaid-zoom-value{min-width:54px;font-size:11px;font-weight:700}
.mermaid-view-controls i{width:1px;height:20px;margin:0 2px;background:#dce5ef}
.mermaid-caption{position:absolute;z-index:2;left:50%;bottom:18px;transform:translateX(-50%);max-width:calc(100% - 80px);padding:7px 13px;border:1px solid #dce5ef;border-radius:8px;background:#fffffff0;color:#344f70;text-align:center;box-shadow:0 4px 14px #294a7012;pointer-events:none}
.mermaid-caption b{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.mermaid-canvas:fullscreen{width:100vw;height:100vh;min-height:100vh;background:#f7f9fc!important}
.mermaid-canvas:fullscreen .mermaid-image-stage{padding:72px 56px 70px}
.mermaid-canvas:fullscreen .mermaid-image-stage img{max-width:calc(100vw - 112px)!important;max-height:calc(100vh - 142px)!important}
.mermaid-canvas-state{text-align:center;color:#5d7290;max-width:100%;overflow-wrap:anywhere}
.mermaid-canvas-state p{font-size:13px;line-height:1.8}
.mermaid-sync{margin-top:12px;font-size:12px;color:#526986}
@media(max-width:850px){.mermaid-preview-body{grid-template-columns:180px minmax(0,1fr)}.mermaid-preview-body nav{max-height:650px;padding:8px}.mermaid-preview-body nav button{flex-basis:44px;min-height:44px;height:44px;max-height:44px;gap:7px;padding:4px 7px}.mermaid-image-stage{padding:62px 18px 58px}.mermaid-view-controls{top:10px;right:10px}}
</style>
