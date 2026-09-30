<script setup>
import {computed,ref} from 'vue'
const props=defineProps({disabled:Boolean,available:Boolean,deployed:Boolean})
const items=computed(()=>[{id:'edit',text:'编辑配置'},{id:'copy',text:'复制为新项目'},{id:'export',text:'导出 Compose'},...(props.deployed?[{id:'down',text:'移除容器'}]:[]),{id:'delete',text:'删除记录'}])
defineEmits(['choose'])
const menu=ref(),position=ref({})
function toggle(event){const r=event.currentTarget.getBoundingClientRect();position.value={left:`${Math.max(8,Math.min(r.right-180,window.innerWidth-188))}px`,top:`${Math.max(8,Math.min(r.bottom+4,window.innerHeight-240))}px`};menu.value.togglePopover()}
</script>
<template><button class="menu-trigger" :disabled="disabled" aria-label="更多项目操作" @click="toggle">更多 ▾</button><Teleport to="body"><div ref="menu" popover class="project-menu-items" :style="position"><button v-for="item in items" :key="item.id" :disabled="disabled||(item.id==='down'&&!available)" @click="$emit('choose',item.id);menu.hidePopover()">{{item.text}}</button></div></Teleport></template>
<style scoped>
.menu-trigger{min-height:34px;padding:6px 12px;border:1px solid var(--compose-line);border-radius:4px;background:var(--compose-paper);color:var(--compose-text);font:inherit;font-size:var(--fs-12);white-space:nowrap;cursor:pointer}.project-menu-items{position:fixed;inset:auto;margin:0;width:180px;padding:6px;background:var(--compose-paper);color:var(--compose-text);border:1px solid var(--compose-line);border-radius:6px;box-shadow:0 5px 20px #0002}.project-menu-items button{display:block;width:100%;padding:10px;border:0;background:transparent;color:var(--compose-text);text-align:left;cursor:pointer}.project-menu-items button:hover{background:var(--compose-tint)}button:disabled{opacity:.5;cursor:default}
</style>
