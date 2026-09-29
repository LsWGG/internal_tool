<script setup>
import {computed,ref,watch} from 'vue'
import {firstChar,frameState,letterTone,linkHost} from './linkTools'

const props=defineProps({tool:{type:Object,default:null},offline:{type:Boolean,default:false}})

// 外壳是系统 UI，实际内容是这个地址的 iframe。换工具时重挂（key 而不是改 src）：某些
// 站点只有在文档重新创建时才会重新初始化，顺带把加载遮罩复位。
const nonce=ref(0),loaded=ref(false)
function restart(){nonce.value+=1;loaded.value=false}
watch(()=>props.tool?.id,restart)

// 后端保存时探到的结论。`blocked` 是「问过了，对方明确不让嵌」—— 这种就不放 iframe 了：
// 加载必然是空的，而空白框会让人以为是我们坏了或者网络不通。
const frame=computed(()=>frameState(props.tool))
const host=computed(()=>linkHost(props.tool?.url)||props.tool?.url||'')
</script>

<template>
  <section class="link-page">
    <header>
      <div>
        <span class="eyebrow">EXTERNAL TOOL</span>
        <h1>{{tool?tool.name:'外链工具'}}</h1>
        <p v-if="tool">{{tool.description||'在系统界面里内嵌打开这个在线工具。'}}</p>
        <p v-else-if="offline">外链工具列表暂时读不到，请确认后端服务是否可用；也可能这个工具已经被删除。</p>
        <p v-else>这个外链工具不存在，可能已经被删除。</p>
      </div>
      <span v-if="tool" class="link-logo" :style="letterTone(tool.id)" aria-hidden="true">{{firstChar(tool.name)}}</span>
    </header>

    <template v-if="tool">
      <!-- 明确不让嵌的：不挂 iframe。这里说的「不让嵌」是后端保存时读响应头读到的，
           不是猜的 —— 所以页面能把对方的原话（frame-ancestors 'self'）摆出来，
           用户才知道自己撞的是对方的策略，而不是本系统坏了。 -->
      <div v-if="frame.blocked" class="link-blocked">
        <b>{{host}} 不允许被嵌入</b>
        <p>这个地址的响应头里有 <code>{{frame.policy||'X-Frame-Options / frame-ancestors'}}</code>，浏览器因此不允许任何其它站点把它放进 iframe —— 不是加载失败，是对方明确拒绝，前端没有绕过去的办法。<br>想在系统界面里用它，只能自建一套（同一套工具的自己部署版），再把这里的地址换成你的实例。</p>
        <a class="link-open" :href="tool.url" target="_blank" rel="noopener noreferrer">在新窗口打开 {{host}} ↗</a>
      </div>

      <template v-else>
        <div class="link-frame-wrap">
          <!-- 外部 iframe 是跨源内容，前端不能重写其 target=_blank。保留弹窗权限，
               以免第三方工具的链接被浏览器静默拦截；页面自身使用 _top 的跳转仍由当前页承接。 -->
          <iframe :key="nonce" class="link-frame" :src="tool.url" :title="tool.name" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-top-navigation-by-user-activation" @load="loaded=true"></iframe>
          <div v-if="!loaded" class="link-loading" aria-live="polite">正在加载 {{tool.url}} …</div>
        </div>
      </template>
    </template>

    <div v-else class="link-empty">
      <b>没有可显示的内容</b>
      <p>点左上角的「← 返回工具门户」回到首页；如果这个工具还在设置里，删掉再重新添加一次即可。</p>
    </div>
  </section>
</template>

<style scoped>
/* 头部交给 ui-refinement.css 的 .ui-shell>section>header 统一，这里只写这块内容自己的东西。 */
.link-page{color:#30465f}
.link-logo{display:grid;place-items:center;flex:none;width:calc(52px * var(--ui-scale));height:calc(52px * var(--ui-scale));border:1px solid #e2ebf9;border-radius:14px;font-size:calc(23px * var(--ui-scale));font-weight:650;line-height:1;box-shadow:var(--ds-inner-hi,inset 0 1px 0 #fff)}
.link-frame-wrap{position:relative}
/* iframe 不给高度就是 0。这一页现在只有「页头 + 框」两行东西，所以 100dvh 减掉上面那几行
   就是框能占满的一屏。280 / 244 是量出来的（框上沿 232 + main 下内边距 48；窄屏 216 + 28），
   不是拍的 —— 减法多一 px 就多一条滚动条，少一 px 就多一截空白。 */
.link-frame{display:block;width:100%;height:clamp(420px,calc(100dvh - 280px),1200px);border:1px solid var(--workbench-line,#dde5f0);border-radius:var(--workbench-radius,12px);background:#fff;box-shadow:0 5px 22px #405b7c09}
.link-loading{position:absolute;inset:0;display:grid;place-items:center;border:1px solid var(--workbench-line,#dde5f0);border-radius:var(--workbench-radius,12px);background:#f8fafd;color:#8290a3;font-size:var(--fs-12,12px)}
/* 能嵌的地址这一页只有工具栏 + iframe，不再挂说明段落：进来就是这个页面。
   「框里一片空白」那种情况已经在上面按结论处理掉了（不让嵌的压根不挂 iframe）。 */
/* 被拒嵌入的落地页：这块要看起来像「一个说明」，不像「一个报错」—— 错不在我们，
   也不在用户的网络。所以走系统里那套浅蓝面板，不用红色。 */
.link-blocked{padding:34px 24px;border:1px solid #dbe5f2;border-radius:14px;background:#f7fafe;text-align:center}
.link-blocked b{display:block;color:#2d4564;font-size:var(--fs-16,16px)}
.link-blocked p{max-width:660px;margin:11px auto 0;color:#74869b;font-size:var(--fs-12,12px);line-height:1.9;overflow-wrap:anywhere}
.link-blocked code{padding:1px 6px;border-radius:5px;background:#eaf0f9;color:#4a6ea8;font:11px ui-monospace,SFMono-Regular,monospace}
.link-open{display:inline-flex;align-items:center;min-height:40px;margin-top:20px;padding:9px 18px;border:1px solid #3d63b7;border-radius:10px;background:var(--ui-primary,#416fd1);color:#fff;font-size:var(--fs-12,12px);font-weight:600;text-decoration:none}
.link-open:hover{background:var(--ui-primary-hover,#315fbe)}
.link-empty{margin-top:8px;padding:44px 24px;border:1px dashed #d7e1ef;border-radius:14px;background:#fbfdff;text-align:center}
.link-empty b{color:#3c526d;font-size:var(--fs-15,15px)}
.link-empty p{max-width:520px;margin:9px auto 0;color:#8290a3;font-size:var(--fs-12,12px);line-height:1.8}
@media(max-width:760px){.link-logo{width:44px;height:44px;font-size:20px}.link-frame{height:clamp(380px,calc(100dvh - 244px),1200px)}}
</style>
