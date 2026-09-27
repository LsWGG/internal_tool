<script setup>
/* Ambient dust layer. A fixed, full-viewport canvas that sits at z-index 0, behind the app
   shell (which is z-index 1), so it only shows through the transparent parts of a page —
   gutters, the header band, gaps between cards — on every route without any per-page work.

   Teleported to <body> on purpose: `main.ui-shell` is position:relative + z-index:1, which
   creates a stacking context, so a z-index:0 canvas placed inside it would paint *above*
   the static white panels instead of behind them.

   Cost is bounded at every step rather than tuned by eye: density, backing-store pixels,
   frame rate, link count and pause conditions all have hard ceilings, which is what makes a
   full-viewport animated layer viable at 3840x2160. On top of the ceilings the layer watches
   its own cadence and parks itself on the last painted frame if the browser cannot keep up —
   see backOff(). */
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { setAmbientPaused, subscribeAmbient } from './ambientState'

const canvas = ref(null)

const PIXEL_BUDGET = 2600000      // ≈1920x1350; a 4K backing store would otherwise be 8.3M px
const AREA_PER_PARTICLE = 26000
const MIN_PARTICLES = 14
const MAX_PARTICLES = 140
const FRAME_MS = 33               // ~30fps: these drift slowly, nobody can tell
const LINK_DIST = 118
const LINKS_PER_PARTICLE = 4
const LINKS_TOTAL = 260
const POINTER_PAUSE_MS = 200
// The bar is deliberately high: this is decoration, and a page dragged down to 15fps to
// animate some dust is a bad trade. Measured over a window rather than as a run of slow
// frames, because one fast frame in a jittery stream would otherwise reset a streak forever.
const MIN_FPS = 20
const SAMPLE_MS = 1500
const RETRY_MS = 30000

// Two brand-adjacent tints; a pre-rendered sprite avoids per-particle shadowBlur, which is
// 10-100x more expensive at 4K.
const TINTS = [[79, 127, 224], [122, 92, 224]]

let ctx = null
let cssW = 0
let cssH = 0
let particles = []
let sprites = []
let linkDist = LINK_DIST
let raf = 0
let lastDraw = 0
let resizeTimer = 0
let pointerTimer = 0
let externalPaused = false
let reducedQuery = null
let disposeAmbient = null
let parked = false
let backoffs = 0
let sampleMs = 0
let sampleFrames = 0
let lastTick = 0
let retryTimer = 0
let pointerHeld = false

function makeSprite(rgb) {
  const size = 64
  const off = document.createElement('canvas')
  off.width = size
  off.height = size
  const c = off.getContext('2d')
  if (!c) return off
  const grad = c.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2)
  const [r, g, b] = rgb
  grad.addColorStop(0, `rgba(${r},${g},${b},.95)`)
  grad.addColorStop(0.45, `rgba(${r},${g},${b},.32)`)
  grad.addColorStop(1, `rgba(${r},${g},${b},0)`)
  c.fillStyle = grad
  c.fillRect(0, 0, size, size)
  return off
}

function seed(count) {
  particles = []
  for (let i = 0; i < count; i++) {
    particles.push({
      x: Math.random() * cssW,
      y: Math.random() * cssH,
      vx: (Math.random() - 0.5) * 0.14,
      vy: (Math.random() - 0.5) * 0.14,
      r: 1.1 + Math.random() * 2.6,
      a: 0.18 + Math.random() * 0.42,
      tint: Math.random() < 0.5 ? 0 : 1,
    })
  }
}

// Keep particles where they were relative to the viewport so a resize does not visibly
// re-scatter the field.
function reseed(oldW, oldH, count) {
  if (!oldW || !oldH) {
    seed(count)
    return
  }
  const next = []
  for (let i = 0; i < count; i++) {
    const p = particles[i % Math.max(1, particles.length)]
    if (!p) {
      next.push({
        x: Math.random() * cssW, y: Math.random() * cssH,
        vx: (Math.random() - 0.5) * 0.14, vy: (Math.random() - 0.5) * 0.14,
        r: 1.1 + Math.random() * 2.6, a: 0.18 + Math.random() * 0.42,
        tint: Math.random() < 0.5 ? 0 : 1,
      })
      continue
    }
    next.push({
      ...p,
      x: Math.max(0, Math.min(cssW, p.x * (cssW / oldW))),
      y: Math.max(0, Math.min(cssH, p.y * (cssH / oldH))),
    })
  }
  particles = next
}

function resize() {
  if (!canvas.value || !ctx) return
  const oldW = cssW
  const oldH = cssH
  cssW = window.innerWidth
  cssH = window.innerHeight
  const area = Math.max(1, cssW * cssH)
  // Bound the backing store instead of the ratio: at 3840x2160 this renders at 0.56x and is
  // stretched back up. The dots are soft radial gradients, so downsampling is invisible —
  // and it is the difference between a free layer and a 16.6M px one.
  const scale = Math.min(window.devicePixelRatio || 1, 1.5, Math.sqrt(PIXEL_BUDGET / area))
  canvas.value.width = Math.max(1, Math.round(cssW * scale))
  canvas.value.height = Math.max(1, Math.round(cssH * scale))
  ctx.setTransform(scale, 0, 0, scale, 0, 0)
  const count = Math.max(MIN_PARTICLES, Math.min(MAX_PARTICLES, Math.round(area / AREA_PER_PARTICLE)))
  reseed(oldW, oldH, count)
  const uiScale = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ui-scale')) || 1
  linkDist = LINK_DIST * uiScale
  if (reducedQuery?.matches || parked) draw()
}

function step(dt) {
  const k = dt / 16.67
  for (const p of particles) {
    p.x += p.vx * k
    p.y += p.vy * k
    if (p.x < -20) p.x = cssW + 20
    else if (p.x > cssW + 20) p.x = -20
    if (p.y < -20) p.y = cssH + 20
    else if (p.y > cssH + 20) p.y = -20
  }
}

function draw() {
  if (!ctx) return
  ctx.clearRect(0, 0, cssW, cssH)
  const max = linkDist * linkDist
  let drawn = 0
  ctx.lineWidth = 1
  ctx.strokeStyle = 'rgba(97,133,214,.12)'
  ctx.beginPath()
  for (let i = 0; i < particles.length; i++) {
    const a = particles[i]
    let links = 0
    for (let j = i + 1; j < particles.length && drawn < LINKS_TOTAL; j++) {
      if (links >= LINKS_PER_PARTICLE) break
      const b = particles[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      if (dx * dx + dy * dy > max) continue
      ctx.moveTo(a.x, a.y)
      ctx.lineTo(b.x, b.y)
      links++
      drawn++
    }
  }
  // One stroke for every link: a single path is far cheaper than per-pair strokes.
  ctx.stroke()
  for (const p of particles) {
    ctx.globalAlpha = p.a
    ctx.drawImage(sprites[p.tint], p.x - p.r * 3, p.y - p.r * 3, p.r * 6, p.r * 6)
  }
  ctx.globalAlpha = 1
}

function frame(now) {
  raf = requestAnimationFrame(frame)
  if (reducedQuery?.matches || document.hidden || externalPaused) {
    lastTick = 0
    return
  }
  const delta = lastTick ? now - lastTick : 0
  lastTick = now
  if (delta) {
    sampleMs += delta
    sampleFrames++
    if (sampleMs >= SAMPLE_MS) {
      if (sampleFrames / (sampleMs / 1000) < MIN_FPS) {
        backOff()
        return
      }
      sampleMs = 0
      sampleFrames = 0
    }
  }
  if (now - lastDraw < FRAME_MS) return
  const dt = Math.min(64, now - lastDraw)
  lastDraw = now
  step(dt)
  draw()
}

/* A full-viewport animated overlay is close to free on a compositing GPU and unaffordable
   without one. Measured here under software rendering at 3840x2160: one repaint costs ~110ms
   whether the canvas is 2150x1209 or 720x405 and whether it has 140 particles or none —
   because the whole viewport has to be recomposited, not because of anything this file draws.
   No amount of tuning fixes that, so the layer measures its own frame interval instead and,
   if the browser cannot sustain it, parks on the last painted frame. The field stays visible;
   only the drift stops. It retries once, in case the stall was transient (a page load, a
   batch job), and then stays parked. */
function backOff() {
  stop()
  parked = true
  clearTimeout(retryTimer)
  if (backoffs++ >= 1) return
  retryTimer = setTimeout(() => {
    parked = false
    sampleMs = 0
    sampleFrames = 0
    lastDraw = 0
    if (!document.hidden && !externalPaused) start()
  }, RETRY_MS)
}

function start() {
  if (raf || !ctx || reducedQuery?.matches || parked) return
  lastTick = 0
  sampleMs = 0
  sampleFrames = 0
  raf = requestAnimationFrame(frame)
}

function stop() {
  if (raf) cancelAnimationFrame(raf)
  raf = 0
}

function release() {
  // A timed pause must never end a held one: a press on a map fires a stray scroll (Leaflet
  // reflowing its panes) whose 200ms tail would otherwise cancel the drag pause 200ms in.
  if (pointerHeld) return
  externalPaused = false
  if (!document.hidden) start()
}

// ms = 0 means "until told otherwise"; anything else is a tail that lets a gesture settle
// before the layer comes back.
function pauseFor(ms) {
  externalPaused = true
  stop()
  clearTimeout(pointerTimer)
  pointerTimer = ms > 0 ? setTimeout(release, ms) : 0
}

// Held, not timed. A drag that pauses for a moment mid-gesture fires no pointermove at all,
// and a renewal-on-move scheme lets the layer creep back in while the button is still down.
function onPointerDown() {
  pointerHeld = true
  pauseFor(0)
}

function onPointerMove(event) {
  if (pointerHeld || event.buttons) {
    pointerHeld = true
    pauseFor(0)
  }
}

function onPointerUp() {
  pointerHeld = false
  pauseFor(POINTER_PAUSE_MS)
}

function onWheel() {
  pauseFor(POINTER_PAUSE_MS)
}

// Chromium hands the gesture over to native scrolling or text selection mid-drag and fires
// pointercancel, which would otherwise release the pause while the page is still moving.
function onScroll() {
  pauseFor(POINTER_PAUSE_MS)
}

function onVisibility() {
  if (document.hidden) stop()
  else if (!externalPaused) start()
}

function onReducedChange() {
  if (reducedQuery.matches) {
    stop()
    draw()
  } else {
    start()
  }
}

let observer = null

onMounted(() => {
  const el = canvas.value
  if (!el) return
  ctx = el.getContext('2d')
  if (!ctx) return // some headless configurations return null; stay silent rather than throw
  sprites = TINTS.map(makeSprite)
  reducedQuery = window.matchMedia('(prefers-reduced-motion: reduce)')
  resize()
  if (reducedQuery.matches) draw()
  else start()
  reducedQuery.addEventListener('change', onReducedChange)
  document.addEventListener('visibilitychange', onVisibility)
  // Covers every Leaflet drag, wheel zoom and list scroll at once, instead of wiring a pause
  // into each of the six maps. Capture phase is required, not stylistic, and measured:
  // Leaflet stops propagation on the map container so `wheel` never bubbles to document, and
  // `scroll` does not bubble at all — both are invisible to a bubble-phase listener here.
  const capture = { passive: true, capture: true }
  document.addEventListener('pointerdown', onPointerDown, capture)
  document.addEventListener('pointermove', onPointerMove, capture)
  document.addEventListener('pointerup', onPointerUp, capture)
  document.addEventListener('pointercancel', onPointerUp, capture)
  document.addEventListener('wheel', onWheel, capture)
  document.addEventListener('scroll', onScroll, capture)
  window.addEventListener('resize', onResize, { passive: true })
  disposeAmbient = subscribeAmbient((paused) => {
    externalPaused = paused
    if (paused) stop()
    else if (!document.hidden) start()
  })
  if (typeof ResizeObserver !== 'undefined') {
    observer = new ResizeObserver(onResize)
    observer.observe(document.documentElement)
  }
})

function onResize() {
  clearTimeout(resizeTimer)
  resizeTimer = setTimeout(resize, 120)
}

onBeforeUnmount(() => {
  stop()
  clearTimeout(resizeTimer)
  clearTimeout(pointerTimer)
  clearTimeout(retryTimer)
  observer?.disconnect()
  disposeAmbient?.()
  reducedQuery?.removeEventListener('change', onReducedChange)
  document.removeEventListener('visibilitychange', onVisibility)
  document.removeEventListener('pointerdown', onPointerDown, true)
  document.removeEventListener('pointermove', onPointerMove, true)
  document.removeEventListener('pointerup', onPointerUp, true)
  document.removeEventListener('pointercancel', onPointerUp, true)
  document.removeEventListener('wheel', onWheel, true)
  document.removeEventListener('scroll', onScroll, true)
  window.removeEventListener('resize', onResize)
  setAmbientPaused('ambient-unmounted', true)
})
</script>

<template>
  <Teleport to="body">
    <canvas ref="canvas" class="ambient-canvas" data-ambient aria-hidden="true"></canvas>
  </Teleport>
</template>

<style>
.ambient-canvas{position:fixed;inset:0;width:100%;height:100%;z-index:0;pointer-events:none;display:block}
</style>
