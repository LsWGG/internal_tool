// 外链工具（用户自己挂上来的在线工具）在首页、设置弹窗和外链工具页之间共用的展示逻辑：
// 首字 LOGO、色相、分类落点、目录卡片数据。放在一份里，是为了让三处对同一个工具算出
// 同一张脸，而不是各写一遍「看起来差不多」的取字和取色。
import {externalGroup} from './toolCatalog'
import {hasCategory} from './catalogState'

const FALLBACK_LETTER='链'

/** 名称首字。按码点取，别把 emoji / 组合字符切成半个；拉丁字母转大写。 */
export function firstChar(name){
  const text=String(name??'').trim()
  if(!text)return FALLBACK_LETTER
  const [char]=Array.from(text)
  return /[a-z]/.test(char)?char.toUpperCase():char
}

/** 「符合项目主题的 LOGO」= 名称首字 + 品牌蓝附近的底色。
 *
 * 色相由 id 稳定散在 205–265°（设计系统那套浅蓝渐变所在的区间），同一个工具每次算出来
 * 都一样，一组卡片摆在一起仍是一家人。返回的样式里同时给 background 和 color，
 * 所以它写在元素的行内样式上就能盖掉 .catalog-tool-icon 的默认蓝。
 */
export function letterTone(id){
  const text=String(id??'')
  let hash=0
  for(let index=0;index<text.length;index+=1)hash=(hash*31+text.charCodeAt(index))>>>0
  const hue=205+(hash%61)
  return {background:`linear-gradient(160deg,hsl(${hue} 72% 97%),hsl(${hue} 68% 93%))`,color:`hsl(${hue} 52% 46%)`}
}

/** 卡片副标题用主机名，比整条地址短，也比「外链工具」四个字更能说明这是什么。 */
export function linkHost(url){
  try{return new URL(String(url||'')).host}catch{return''}
}

/** 分类落点：认得出的分类挂到那一组，认不出的（含空值）归到「外链工具」组。
 *
 * 服务端不校验分类（分类表在前端），所以这里是「未知值长不出空分组」的那道闸。
 * 合法集合问的是**合并后**的分类（用户能新建分类，所以它比代码里那张表大）——
 * 每次现算而不是取一份快照，否则用户在设置里新建的分类要等下次刷新才认得出。
 */
export function linkGroupId(category){
  const id=String(category??'').trim()
  return hasCategory(id)?id:externalGroup.id
}

/** 后端保存时探测到的嵌入结论。
 *
 * `known=false` 是「还没问过」（功能上线前存的老数据，或上次没问到）—— 它跟「问过说可以」
 * 不是一回事，界面上不能显示成同一个结论，行为上则沿用老样子（照嵌，页面上留着说明）。
 */
export function frameState(link){
  const value=link?.embeddable
  return {blocked:value===false,known:value===true||value===false,policy:link?.frame_policy||''}
}

/** 目录卡片数据。id 加前缀是为了和内置工具同处一个命名空间而不撞车（内置 id 里没有冒号）。 */
export function toCatalogTool(link){
  const frame=frameState(link)
  return {
    id:`link:${link.id}`,
    name:link.name,
    url:link.url,
    tag:linkHost(link.url)||externalGroup.name,
    description:link.description||link.url,
    keywords:`${link.name} ${link.description||''} ${link.url}`,
    icon:'link',
    letter:firstChar(link.name),
    tone:letterTone(link.id),
    group:linkGroupId(link.category),
    blocked:frame.blocked,
    framePolicy:frame.policy,
  }
}

/** 地址校验（前端先判，后端兜底）。返回空串表示没问题。
 *
 * 与后端 ExternalLink 同一条规则：只收 http/https，因为地址会被写进 iframe 的 src。
 */
export function linkUrlError(url){
  const text=String(url??'').trim()
  if(!text)return '请填写外链工具地址'
  let parsed
  try{parsed=new URL(text)}catch{return '地址必须以 http:// 或 https:// 开头'}
  return ['http:','https:'].includes(parsed.protocol)?'':'地址必须以 http:// 或 https:// 开头'
}
