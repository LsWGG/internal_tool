<script setup>
import ThemeToggle from './ThemeToggle.vue'
import {computed,nextTick,onMounted,ref,watch} from 'vue'
import {mergeGroups} from './catalogState'
import {letterTone,toCatalogTool} from './linkTools'
const props=defineProps({iconComponent:[Object,Function],links:{type:Array,default:()=>[]}})
const emit=defineEmits(['open','settings'])
// active 空着开局：分类可以被改名、重排、搬空，写死 'geo' 会在重排后留下一个匹配不到的高亮，
// onMounted 的 updateActive() 立刻就会填上真实值。
const query=ref(''),active=ref(''),scroller=ref()
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
  <section class="catalog-page">
    <div class="catalog-head">
      <div class="pop-effects" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i><span class="pop-spark">✦</span><span class="pop-orbit"></span></div>
      <div class="catalog-topline"><span class="catalog-brand"><img class="brand-mark" src="/tool-portal-icon.svg?v=3" alt="" width="40" height="40"> INTERNAL TOOLS <small>工程工具门户</small></span><div class="catalog-header-actions"><ThemeToggle/><button class="catalog-settings" type="button" aria-label="设置" title="设置" @click="emit('settings')"><component :is="iconComponent" name="gear" /><span>管理工具</span></button></div></div>
      <div class="catalog-intro">
        <div class="catalog-identity"><h1>好工具，<span>让工作出彩。</span></h1><p>从数据处理到成果交付，把复杂任务变成简单行动。</p></div>
        <div class="catalog-tools"><div class="catalog-search"><div><span aria-hidden="true">⌕</span><input id="tool-search" aria-label="查找工具" v-model="query" type="search" placeholder="搜索工具、格式或关键词" autocomplete="off"><button v-if="query" @click="query=''" aria-label="清空搜索">×</button><span v-else aria-hidden="true">↵</span></div></div></div>
      </div>
      <nav class="catalog-nav" aria-label="工具分类目录"><div><button v-for="group in visibleGroups" :key="group.id" :class="{active:active===group.id}" :aria-current="active===group.id?'location':undefined" @click="jump(group.id)">{{group.name}}<small>{{group.tools.length}}</small></button></div></nav>
    </div>
    <div ref="scroller" class="catalog-scroll" @scroll.passive="updateActive" tabindex="0" aria-label="工具分组">
      <p v-if="query" class="catalog-search-status" role="status">找到 {{matchCount}} 个工具</p>
      <section v-for="(group,index) in visibleGroups" :key="group.id" :data-group="group.id" class="catalog-group">
        <div class="catalog-group-heading"><span class="catalog-group-number">{{String(index+1).padStart(2,'0')}}</span><span class="catalog-group-icon"><component :is="iconComponent" :name="group.icon" /></span><div><h2>{{group.name}}</h2><p>{{group.description}}</p></div><span class="group-count">{{group.tools.length}} TOOLS</span></div>
        <div class="catalog-cards"><a v-for="tool in group.tools" :key="tool.id" :href="tool.blocked?tool.url:'#'+tool.id" :target="tool.blocked?'_blank':undefined" :rel="tool.blocked?'noopener noreferrer':undefined" class="catalog-card" @click="openCard(tool,$event)"><span class="catalog-tool-icon" :class="{'is-letter':tool.letter}" :style="tool.tone||letterTone(tool.id)"><template v-if="tool.letter">{{tool.letter}}</template><component v-else :is="iconComponent" :name="tool.icon" /></span><div><small>{{tool.tag}}</small><span v-if="tool.blocked" class="catalog-chip" :title="blockedTip(tool)">↗ 新窗口</span><h3>{{tool.name}}</h3><p>{{tool.description}}</p></div><span class="catalog-arrow" aria-hidden="true">↗</span></a></div>
      </section>
      <div v-if="!visibleGroups.length" class="catalog-empty"><h2>没有找到匹配工具</h2><p>试试“Word”“图片”“数据库”或文件格式名称。</p><button @click="query=''">查看全部工具</button></div>
    </div>
  </section>
</template>
<style scoped>
.catalog-page{height:100dvh;display:flex;flex-direction:column;color:#202022;overflow:hidden;background:#fffdf4;position:relative}
.catalog-head{flex:none;padding:0 var(--ui-catalog-gutter);background:#83d9e7;background-image:linear-gradient(#20202212 1px,transparent 1px),linear-gradient(90deg,#20202212 1px,transparent 1px);background-size:32px 32px;border-bottom:3px solid #202022}
.catalog-topline{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:14px 0;border-bottom:1px solid #20202240}
.catalog-header-actions{display:flex;align-items:center;gap:12px}.catalog-brand{display:flex;align-items:center;gap:12px;font-size:var(--fs-14);font-weight:900;letter-spacing:1px}.catalog-brand small{font-size:var(--fs-11);font-weight:500;letter-spacing:0}.brand-mark{display:block;flex:none;width:40px;height:40px;object-fit:contain}
.catalog-settings{display:flex;align-items:center;justify-content:center;gap:8px;padding:8px 12px;background:#fffdf4;border:2px solid #202022;border-radius:0;box-shadow:3px 3px #202022;color:#202022;cursor:pointer;font-size:var(--fs-11);font-weight:700}.catalog-settings :deep(svg){width:17px;height:17px}
.catalog-intro{display:flex;align-items:center;justify-content:space-between;gap:40px;padding:22px 0}.catalog-identity{min-width:0}.catalog-identity h1{font-family:inherit;font-weight:900;font-size:clamp(25px,2vw,36px);letter-spacing:-1px;line-height:1.35;margin:0 0 8px}.catalog-identity h1 span{display:inline-block;background:#ffe250;padding:0 8px;border:2px solid #202022;box-shadow:4px 4px #202022;transform:rotate(-2deg)}.catalog-identity p{font-size:var(--fs-12);color:#30383a;line-height:1.8}
.catalog-tools{width:calc(340px * var(--ui-scale));flex:none;position:relative}.catalog-search>div{display:flex;align-items:center;gap:8px;background:white;border:2px solid #202022;padding:0 10px;box-shadow:3px 3px #202022}.catalog-search input{width:100%;min-width:0;padding:12px 0;background:transparent;border:0;box-shadow:none;border-radius:0;color:#202022;font-size:var(--fs-12);outline:none}.catalog-search button{border:0;background:none;font-size:22px;cursor:pointer;color:#202022}.catalog-search>div:focus-within{background:#fffbea;box-shadow:3px 3px #202022,0 0 0 3px #ffe250}
.catalog-nav{display:flex;align-items:center;gap:20px;padding:4px 0 14px}.catalog-nav>span{font-size:var(--fs-10);font-weight:800;white-space:nowrap}.catalog-nav>div{display:flex;gap:12px;overflow-x:auto;padding:3px 4px 5px;scrollbar-width:thin}.catalog-nav button{display:flex;align-items:center;gap:12px;padding:8px 13px;border:2px solid transparent;background:#ffffff40;border-radius:0;color:#202022;font-family:inherit;font-size:var(--fs-11);font-weight:700;line-height:1.5;white-space:nowrap;cursor:pointer}.catalog-nav button:hover{background:#ffffff90}.catalog-nav button.active{border-color:#202022;background:#ffe250;box-shadow:3px 3px #202022}.catalog-nav small{font-size:var(--fs-9)}
.catalog-scroll{flex:1;min-height:0;overflow-y:auto;scrollbar-gutter:stable;padding:20px var(--ui-catalog-gutter) 60px;overscroll-behavior:contain;outline:none}.catalog-group{margin-bottom:34px}.catalog-group:last-child{min-height:calc(100% + 2px)}.catalog-group-heading{display:flex;align-items:center;gap:12px;margin-bottom:18px}.catalog-group-number{font-size:var(--fs-28);font-weight:900;letter-spacing:-1px;color:#202022}.catalog-group-icon{display:grid;place-items:center;flex:none;width:32px;height:32px;border:2px solid #202022;background:#ffe250;color:#202022;box-shadow:2px 2px #202022}.catalog-group-icon :deep(svg){width:19px;height:19px}.catalog-group-heading h2{font-family:inherit;font-size:var(--fs-17);font-weight:800;line-height:1.5;margin:0;color:#202022}.catalog-group-heading p{font-size:var(--fs-11);color:#646463;line-height:1.7;margin-top:3px}.group-count{margin-left:auto;font-size:var(--fs-9);letter-spacing:1px;font-weight:800;border-bottom:2px solid #202022;padding-bottom:5px;white-space:nowrap}
.catalog-cards{display:grid;grid-template-columns:repeat(var(--ui-catalog-cols,3),minmax(0,1fr));gap:18px}.catalog-card{--card-color:#ffe250;display:flex;align-items:flex-start;gap:15px;position:relative;min-height:calc(155px * var(--ui-scale));padding:calc(23px * var(--ui-scale)) calc(20px * var(--ui-scale));background:#fff;border:2px solid #202022;border-radius:0;box-shadow:4px 4px #202022;text-decoration:none;transition:transform .16s,box-shadow .16s;min-width:0;animation:none}.catalog-card:nth-child(3n+2){--card-color:#ff93ba}.catalog-card:nth-child(3n){--card-color:#83d9e7}.catalog-card:hover{background:#fffef6;transform:translate(-2px,-3px);box-shadow:7px 7px #202022}.catalog-tool-icon{display:grid;place-items:center;width:calc(42px * var(--ui-scale));height:calc(42px * var(--ui-scale));background:var(--card-color)!important;border:2px solid #202022;border-radius:0;flex:none;color:#202022!important;box-shadow:2px 2px #202022}.catalog-tool-icon :deep(svg){width:calc(23px * var(--ui-scale));height:calc(23px * var(--ui-scale))}.catalog-tool-icon.is-letter{font-size:var(--fs-18);font-weight:900}.catalog-card>div{min-width:0;padding-right:10px}.catalog-card small{font-size:var(--fs-9);font-weight:700;letter-spacing:1px;color:#686864}.catalog-card h3{font-family:inherit;font-size:var(--fs-15);line-height:1.55;font-weight:800;color:#202022;margin:7px 0}.catalog-card p{font-size:var(--fs-11);line-height:1.8;color:#5f5f5c;overflow-wrap:anywhere}.catalog-arrow{position:absolute;right:12px;top:12px;color:#202022;font-size:var(--fs-19);font-weight:800}.catalog-chip{display:inline-block;margin-left:5px;border:1px solid #202022;padding:1px 4px;background:#ffe250;color:#202022;font-size:var(--fs-9)}.catalog-empty{text-align:center;padding:60px 16px}.catalog-empty h2{font-family:inherit;font-size:var(--fs-20)}.catalog-empty p{font-size:var(--fs-13);margin:12px}.catalog-empty button{border:2px solid #202022;background:#ffe250;box-shadow:3px 3px #202022;padding:10px 18px;color:#202022;cursor:pointer}.catalog-search-status{font-size:var(--fs-12);color:#454541;margin-bottom:20px}.catalog-page :is(a,button,input):focus-visible,.catalog-scroll:focus-visible{outline:3px solid #d81b62;outline-offset:4px}
@media(min-width:1800px){.catalog-intro{padding:20px 0}.catalog-topline{padding:10px 0}}
@media(max-width:1100px){.catalog-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.catalog-intro{gap:25px}.catalog-tools{width:300px}.catalog-identity h1{font-size:26px}.catalog-brand small{display:none}}
@media(max-width:760px){.catalog-head{padding:0 18px}.catalog-topline{padding:12px 0}.catalog-header-actions{display:flex;align-items:center;gap:12px}.catalog-brand{font-size:11px;gap:8px}.catalog-settings{padding:7px}.catalog-settings span{display:none}.catalog-intro{padding:18px 0;display:block}.catalog-identity h1{font-size:24px;letter-spacing:-1px;margin:0}.catalog-identity p{display:none}.catalog-tools{width:100%;padding:0;border:0;background:transparent;box-shadow:none;margin-top:18px}.catalog-search input{padding:10px 0;font-size:12px}.catalog-nav{gap:0;padding-bottom:10px}.catalog-nav>span{display:none}.catalog-nav button{padding:6px 10px;font-size:11px}.catalog-scroll{padding:22px 18px 40px}.catalog-group-heading{gap:10px}.catalog-group-heading p{font-size:10px}.catalog-group-number{font-size:24px}.group-count{font-size:8px}.catalog-card{padding:19px 16px}}
@media(max-width:520px){.catalog-cards{grid-template-columns:1fr;gap:14px}.catalog-card{min-height:135px}.catalog-identity h1{font-size:24px}.catalog-group-heading h2{font-size:16px}}
@media(prefers-reduced-motion:reduce){.catalog-card{transition:none}.catalog-card:hover{transform:none}}
/* Neon accents animate only small decorative layers; all controls stay above them. */
.catalog-head{position:relative;isolation:isolate}.catalog-topline,.catalog-intro,.catalog-nav{position:relative;z-index:1}
.pop-effects{position:absolute;inset:0;overflow:hidden;pointer-events:none;z-index:0}
.pop-effects::before{content:'';position:absolute;inset:-32px;background:linear-gradient(#fff5 1px,transparent 1px),linear-gradient(90deg,#fff5 1px,transparent 1px);background-size:64px 64px;opacity:.3;animation:pop-grid 16s linear infinite}
.pop-effects i{position:absolute;width:12px;height:12px;background:#ff4ab9;border-radius:50%;box-shadow:0 0 9px #ff4ab9,0 0 24px #ff4ab980;animation:pop-float 7s ease-in-out infinite alternate;left:4%;top:38%}
.pop-effects i:nth-child(2){left:43%;top:15%;width:8px;height:8px;animation-delay:-3s;background:#fff458;box-shadow:0 0 12px #fff458}
.pop-effects i:nth-child(3){left:60%;top:72%;animation-delay:-5s;background:#3df8ff;box-shadow:0 0 14px #00edff,0 0 28px #00edff}
.pop-effects i:nth-child(4){left:96%;top:35%;animation-delay:-2s;width:18px;height:18px}.pop-effects i:nth-child(5){left:53%;top:43%;animation-delay:-4s;width:6px;height:6px}.pop-effects i:nth-child(6){left:91%;top:85%;animation-delay:-6s;background:#fff458;box-shadow:0 0 12px #fff458}
.pop-spark{position:absolute;left:55%;top:27%;font-size:38px;color:#fff456;text-shadow:2px 2px #252526,0 0 20px #fff456;animation:pop-twinkle 5s ease-in-out infinite}
.pop-orbit{position:absolute;right:-35px;top:72px;width:120px;height:120px;border:9px dotted #ff62b0;filter:drop-shadow(0 0 5px #ff62b0);animation:pop-spin 40s linear infinite;opacity:.65;border-radius:50%}
.catalog-identity h1 span{animation:pop-neon 4s ease-in-out infinite}.catalog-card:hover{box-shadow:6px 6px #202022,0 0 17px #ff4ab968}.catalog-card:hover .catalog-tool-icon{box-shadow:2px 2px #202022,0 0 14px var(--card-color)}
@keyframes pop-grid{to{transform:translate(32px,32px)}}
@keyframes pop-float{to{transform:translate3d(15px,-24px,0);opacity:.45}}
@keyframes pop-twinkle{50%{transform:rotate(30deg) scale(.75);opacity:.5}}
@keyframes pop-spin{to{transform:rotate(360deg)}}
@keyframes pop-neon{0%,100%{box-shadow:4px 4px #202022,0 0 10px #ff49ab55}50%{box-shadow:4px 4px #202022,0 0 24px #ff49abaa}}
@media(prefers-reduced-motion:reduce){.pop-effects *,.pop-effects::before,.catalog-identity h1 span{animation:none}.pop-effects{opacity:.5}}
/* The search wrapper supplies a single, consistent focus indicator. */
.ui-shell .catalog-page .catalog-search input:focus-visible{outline:none;box-shadow:none}
</style>
