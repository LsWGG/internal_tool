<script setup>
import {ref} from 'vue'
const dark=ref(document.documentElement.dataset.theme==='dark')
function toggle(){dark.value=!dark.value;const theme=dark.value?'dark':'light';document.documentElement.dataset.theme=theme;try{localStorage.setItem('internal-tools.theme',theme)}catch{}document.querySelector('meta[name="theme-color"]')?.setAttribute('content',dark.value?'#171b23':'#ffe45c')}
</script>
<template><button class="theme-toggle" type="button" :aria-pressed="dark" :aria-label="dark?'切换到亮色主题':'切换到暗色主题'" @click="toggle"><svg viewBox="0 0 24 24" aria-hidden="true"><path v-if="dark" d="M12 3v2m0 14v2M3 12h2m14 0h2M5.6 5.6 7 7m10 10 1.4 1.4M5.6 18.4 7 17M17 7l1.4-1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z"/><path v-else d="M20 14.5A8.5 8.5 0 0 1 9.5 4a8.5 8.5 0 1 0 10.5 10.5Z"/></svg><span>{{dark?'亮色':'暗色'}}</span></button></template>
<style scoped>.theme-toggle{display:inline-flex;align-items:center;justify-content:center;gap:6px;border:2px solid #252526;border-radius:3px;padding:7px 10px;background:var(--ui-surface);color:var(--ui-text);box-shadow:2px 2px #252526;font:700 12px inherit;cursor:pointer}.theme-toggle svg{width:18px;height:18px;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}.theme-toggle:focus-visible{outline:3px solid #83d9e7;outline-offset:3px}</style>
