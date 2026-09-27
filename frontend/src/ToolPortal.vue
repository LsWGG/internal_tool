<script setup>
import {computed,nextTick,onBeforeUnmount,onMounted,ref,watch} from 'vue'
import {mergeGroups} from './catalogState'
import {toCatalogTool} from './linkTools'
const props=defineProps({iconComponent:[Object,Function],links:{type:Array,default:()=>[]}})
const emit=defineEmits(['open','settings'])
// active 空着开局：分类可以被改名、重排、搬空，写死 'geo' 会在重排后留下一个匹配不到的高亮，
// onMounted 的 updateActive() 立刻就会填上真实值。
const query=ref(''),active=ref(''),scroller=ref(),pageEl=ref()
// Pointer wash: coalesced into one rAF and written as two CSS variables, so the compositor
// moves the glow with translate3d instead of the main thread repainting a 4K radial gradient.
let glowFrame=0
function syncGlow(event){if(glowFrame)return;const x=event.clientX,y=event.clientY;glowFrame=requestAnimationFrame(()=>{glowFrame=0;const box=pageEl.value?.getBoundingClientRect();if(!box)return;pageEl.value.style.setProperty('--ds-pointer-x',`${x-box.left}px`);pageEl.value.style.setProperty('--ds-pointer-y',`${y-box.top}px`)})}
onBeforeUnmount(()=>cancelAnimationFrame(glowFrame))
// 分组来自 catalogState 的合并视图（用户在设置里改过的顺序/名字/落点都在里面），
// 外链工具挂到各自选的分类下，没选分类的都落在最后的「外链工具」组。
// 后端读不到时合并视图就是代码里那 6 组 18 卡 —— test_ui_catalog.py 把 /api/** 全 abort，
// 它守的就是这条。
const visibleGroups=computed(()=>{const words=query.value.toLowerCase().trim().split(/\s+/).filter(Boolean);const groups=mergeGroups(props.links.map(toCatalogTool));return groups.map(group=>({...group,tools:group.tools.filter(tool=>words.every(word=>`${group.name} ${tool.name} ${tool.description} ${tool.keywords}`.toLowerCase().includes(word)))})).filter(group=>group.tools.length)})
const matchCount=computed(()=>visibleGroups.value.reduce((count,group)=>count+group.tools.length,0))
function jump(id){const box=scroller.value,target=box?.querySelector(`[data-group="${id}"]`);if(target){active.value=id;box.scrollTo({top:target.getBoundingClientRect().top-box.getBoundingClientRect().top+box.scrollTop-parseFloat(getComputedStyle(box).paddingTop),behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'})}}
function updateActive(){if(!scroller.value)return;const sections=[...scroller.value.querySelectorAll('[data-group]')];const top=scroller.value.getBoundingClientRect().top;active.value=(sections.filter(el=>el.getBoundingClientRect().top<=top+60).at(-1)||sections[0])?.dataset.group||''}
// 站点明确不让嵌的工具（后端保存时探到的）：卡片直接把浏览器送去新窗口。这里放行
// 原生跳转是有意的 —— a[target=_blank] 的中键、⌘/Ctrl+点击、「复制链接地址」全都照常，
// 而进外壳页只会得到一片空白（对方的 X-Frame-Options / CSP 由浏览器执行，挡不住）。
function openCard(tool,event){
  if(tool.blocked)return
  event.preventDefault()
  emit('open',tool.id)
}
function blockedTip(tool){
  return `${tool.framePolicy?`${tool.framePolicy}，`:''}该站点不允许被嵌入，点开直接在新窗口打开`
}
watch(query,async()=>{await nextTick();scroller.value?.scrollTo({top:0});updateActive()})
onMounted(updateActive)
</script>
<template>
  <section ref="pageEl" class="catalog-page" @pointermove="syncGlow">
    <div class="catalog-head">
      <div class="catalog-intro"><div class="catalog-identity"><img src="/tool-portal-icon.svg?v=2" alt=""><div><span class="eyebrow">INTERNAL TOOL COLLECTION</span><h1>工程工具门户</h1><p>按工作场景找到工具，从数据处理到成果交付。</p></div></div><div class="catalog-tools"><div class="catalog-search"><label for="tool-search">查找工具</label><div><input id="tool-search" v-model="query" type="search" placeholder="搜索工具、格式或关键词" autocomplete="off"><button v-if="query" @click="query=''" aria-label="清空搜索">×</button></div></div><button class="catalog-settings" type="button" aria-label="设置" title="设置" @click="emit('settings')"><component :is="iconComponent" name="gear" /></button></div></div>
      <nav class="catalog-nav" aria-label="工具分类目录"><span>工具目录</span><div><button v-for="group in visibleGroups" :key="group.id" :class="{active:active===group.id}" :aria-current="active===group.id?'location':undefined" @click="jump(group.id)">{{group.name}}<small>{{group.tools.length}}</small></button></div></nav>
    </div>
    <div ref="scroller" class="catalog-scroll" @scroll.passive="updateActive" tabindex="0" aria-label="工具分组">
      <p v-if="query" class="catalog-search-status" role="status">找到 {{matchCount}} 个工具</p>
      <section v-for="group in visibleGroups" :key="group.id" :data-group="group.id" class="catalog-group">
        <div class="catalog-group-heading"><span class="catalog-group-icon"><component :is="iconComponent" :name="group.icon" /></span><div><h2>{{group.name}}</h2><p>{{group.description}}</p></div></div>
        <div class="catalog-cards"><a v-for="tool in group.tools" :key="tool.id" :href="tool.blocked?tool.url:'#'+tool.id" :target="tool.blocked?'_blank':undefined" :rel="tool.blocked?'noopener noreferrer':undefined" class="catalog-card" @click="openCard(tool,$event)"><span class="catalog-tool-icon" :class="{'is-letter':tool.letter}" :style="tool.tone||null"><template v-if="tool.letter">{{tool.letter}}</template><component v-else :is="iconComponent" :name="tool.icon" /></span><div><small>{{tool.tag}}</small><span v-if="tool.blocked" class="catalog-chip" :title="blockedTip(tool)">↗ 新窗口</span><h3>{{tool.name}}</h3><p>{{tool.description}}</p></div><span class="catalog-arrow" aria-hidden="true">↗</span></a></div>
      </section>
      <div v-if="!visibleGroups.length" class="catalog-empty"><h2>没有找到匹配工具</h2><p>试试“Word”“图片”“数据库”或文件格式名称。</p><button @click="query=''">查看全部工具</button></div>
    </div>
  </section>
</template>
<style scoped>
.catalog-page{height:100dvh;display:flex;flex-direction:column;color:#273b55;overflow:hidden;max-width:none;margin:0;background:rgba(246,248,252,.72)}.catalog-head{flex:none;padding:30px var(--ui-catalog-gutter) 0;background:inherit}.catalog-intro{display:flex;align-items:center;justify-content:space-between;gap:calc(32px * var(--ui-scale))}.catalog-identity{display:flex;gap:18px;align-items:center;min-width:0}.catalog-identity img{width:calc(58px * var(--ui-scale));height:calc(58px * var(--ui-scale));flex:none}.catalog-identity h1{font-family:inherit;font-size:var(--fs-30,30px);line-height:1.4;letter-spacing:-.6px;margin:5px 0 7px;color:#20344f}.catalog-identity p{color:#72829a;font-size:var(--fs-13,13px);line-height:1.7}.catalog-identity .eyebrow{font-size:var(--fs-10,10px);letter-spacing:1.8px;text-shadow:none}.catalog-tools{display:flex;align-items:flex-end;gap:10px;flex:none}.catalog-settings{display:grid;place-items:center;flex:none;width:calc(42px * var(--ui-scale));height:calc(42px * var(--ui-scale));padding:0;border:1px solid #dae3f0;border-radius:10px;color:#61748d;background:#fff;cursor:pointer}.catalog-settings:hover{border-color:#afc6ec;color:#4266b9;box-shadow:0 6px 16px #46679817}.catalog-settings :deep(svg){width:calc(19px * var(--ui-scale));height:calc(19px * var(--ui-scale))}.catalog-tool-icon.is-letter{font-size:calc(17px * var(--ui-scale));font-weight:650;line-height:1}.catalog-search{width:calc(290px * var(--ui-scale));flex:none}.catalog-search label{font-size:var(--fs-11,11px);color:#73849b;margin:0 0 7px;font-weight:500}.catalog-search>div{position:relative}.catalog-search input{width:100%;padding:11px 36px 11px 13px;background:white;border:1px solid #dae3f0;border-radius:10px;color:#304963;font-size:var(--fs-12,12px);outline:none}.catalog-search button{position:absolute;right:5px;top:4px;width:30px;height:30px;border:0;background:transparent;color:#71829a;font-size:var(--fs-20,20px);cursor:pointer}.catalog-nav{display:flex;align-items:center;gap:calc(20px * var(--ui-scale));padding:calc(24px * var(--ui-scale)) 0 calc(22px * var(--ui-scale));border-bottom:1px solid #dce5f0;margin-top:6px}.catalog-nav>span{font-size:var(--fs-11,11px);font-weight:600;color:#8895a7;white-space:nowrap}.catalog-nav>div{display:flex;gap:7px;overflow-x:auto;scrollbar-width:thin}.catalog-nav button{display:flex;align-items:center;gap:9px;padding:9px 13px;border:1px solid transparent;border-radius:9px;color:#61748d;background:transparent;font-family:inherit;font-size:var(--fs-12,12px);font-weight:500;line-height:1.5;white-space:nowrap;cursor:pointer}.catalog-nav button.active{background:#eaf0fd;border-color:#d4e0f7;color:#4266b9}.catalog-nav small{font-size:var(--fs-10,10px);color:inherit;opacity:.75}.catalog-scroll{flex:1;min-height:0;overflow-y:auto;position:relative;scrollbar-gutter:stable;padding:28px var(--ui-catalog-gutter) 60px;overscroll-behavior:contain;outline:none}.catalog-group{padding:0;margin-bottom:calc(32px * var(--ui-scale));scroll-margin-top:0}.catalog-group:last-child{margin-bottom:0;padding-bottom:18px}.catalog-group-heading{display:flex;align-items:center;gap:calc(12px * var(--ui-scale));margin:0 0 calc(16px * var(--ui-scale))}.catalog-group-icon{display:grid;place-items:center;color:#6381b5;width:calc(34px * var(--ui-scale));height:calc(34px * var(--ui-scale));background:#ebf0f8;border-radius:9px;flex:none}.catalog-group-icon :deep(svg){width:calc(19px * var(--ui-scale));height:calc(19px * var(--ui-scale))}.catalog-group-heading h2{font-family:inherit;font-size:var(--fs-17,17px);font-weight:650;line-height:1.5;color:#2f4663;letter-spacing:0;margin:0}.catalog-group-heading p{font-size:var(--fs-12,12px);color:#8592a5;line-height:1.7;margin:3px 0 0}.catalog-cards{display:grid;grid-template-columns:repeat(var(--ui-catalog-cols,3),minmax(0,1fr));gap:calc(14px * var(--ui-scale))}.catalog-card{display:flex;align-items:flex-start;gap:calc(14px * var(--ui-scale));position:relative;min-height:calc(156px * var(--ui-scale));padding:calc(21px * var(--ui-scale)) calc(20px * var(--ui-scale));background:white;border:1px solid #e0e7f1;border-radius:calc(13px * var(--ui-scale));box-shadow:0 3px 12px #3c5c8610;text-decoration:none;transition:border-color .15s,box-shadow .15s,transform .15s;min-width:0}.catalog-card:hover{border-color:#afc6ec;box-shadow:0 8px 24px #46679817;transform:translateY(-2px)}.catalog-tool-icon{display:grid;place-items:center;width:calc(38px * var(--ui-scale));height:calc(38px * var(--ui-scale));background:#f0f5fe;border:1px solid #e2ebf9;border-radius:10px;flex:none;color:#527bc7}.catalog-tool-icon :deep(svg){width:calc(21px * var(--ui-scale));height:calc(21px * var(--ui-scale))}.catalog-card>div{min-width:0;padding-right:8px}.catalog-card small{font-size:var(--fs-10,10px);color:#8795a8;letter-spacing:.4px}.catalog-card h3{font-family:inherit;font-size:var(--fs-15,15px);line-height:1.55;font-weight:650;color:#2d4564;margin:6px 0 9px}
/* 「点开是新窗口」这件事必须写在卡片上：不然用户点完发现「门户没了」只会以为是坏了。
   形状跟着 .catalog-tool-icon 那套浅蓝走，和它旁边的工具名同一行。 */
.catalog-chip{display:inline-block;margin-left:7px;padding:1px 6px;border:1px solid #dbe6f6;border-radius:6px;background:#f2f6fd;color:#4a6ea8;font-size:var(--fs-10,10px);line-height:1.7;white-space:nowrap;vertical-align:1px}.catalog-card p{font-size:var(--fs-12,12px);line-height:1.8;color:#7a8ba0;overflow-wrap:anywhere}.catalog-arrow{position:absolute;right:13px;top:14px;color:#b3c1d5;font-size:var(--fs-17,17px)}.catalog-card:hover .catalog-arrow{color:#527bc7}.catalog-empty{text-align:center;padding:60px 16px}.catalog-empty h2{font-size:var(--fs-20,20px)}.catalog-empty p{font-size:var(--fs-13,13px);margin:12px}.catalog-empty button{border:1px solid #d7e2f2;background:#fff;border-radius:8px;padding:10px 18px;color:#4169d8;cursor:pointer}.catalog-search-status{font-size:var(--fs-12,12px);color:#74869f;margin:0 0 20px}.catalog-page :is(a,button,input):focus-visible,.catalog-scroll:focus-visible{outline:2px solid #7298de;outline-offset:3px}
@media(max-width:1100px){.catalog-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.catalog-search{width:240px}}
@media(max-width:760px){.catalog-head{padding:20px 18px 0}.catalog-intro{gap:16px;align-items:stretch;flex-direction:column}.catalog-identity img{width:44px;height:44px}.catalog-identity h1{font-size:var(--fs-24,24px)}.catalog-identity p{font-size:var(--fs-12,12px)}.catalog-tools{width:100%}.catalog-search{width:auto;flex:1;min-width:0}.catalog-search label{display:none}.catalog-nav{padding:14px 0 17px;gap:8px}.catalog-nav>span{display:none}.catalog-nav button{padding:8px 10px}.catalog-scroll{padding:22px 18px 40px}.catalog-card{padding:17px 15px;min-height:152px;gap:10px}.catalog-group-heading p{font-size:var(--fs-11,11px)}}
@media(max-width:520px){.catalog-cards{grid-template-columns:minmax(0,1fr)}.catalog-card{min-height:130px}.catalog-group{margin-bottom:28px}.catalog-identity{gap:12px}.catalog-identity .eyebrow{font-size:var(--fs-9,9px)}.catalog-card h3{font-size:var(--fs-15,15px)}.catalog-tool-icon{width:35px;height:35px}}
@media(prefers-reduced-motion:reduce){.catalog-card{transition:none}.catalog-card:hover{transform:none}}
</style>
<style scoped>
.catalog-group:last-child{min-height:calc(100% + 2px)}
</style>
