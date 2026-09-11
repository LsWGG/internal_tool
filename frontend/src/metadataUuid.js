// randomUUID requires a secure context; getRandomValues also works on LAN HTTP.
function fallbackUuid(){
  const crypto=globalThis.crypto
  if(typeof crypto?.getRandomValues!=='function')throw new Error('浏览器不支持安全随机数，请升级浏览器或使用 HTTPS / localhost 访问')
  const bytes=crypto.getRandomValues(new Uint8Array(16))
  bytes[6]=(bytes[6]&0x0f)|0x40
  bytes[8]=(bytes[8]&0x3f)|0x80
  const hex=Array.from(bytes,b=>b.toString(16).padStart(2,'0')).join('')
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`
}

export function metadataUuid(){
  return typeof globalThis.crypto?.randomUUID==='function'?globalThis.crypto.randomUUID():fallbackUuid()
}

// The WASM wrapper also calls crypto.randomUUID for temporary output files.
export function installMetadataUuid(){
  if(typeof globalThis.crypto?.randomUUID!=='function'){
    if(typeof globalThis.crypto?.getRandomValues!=='function')throw new Error('浏览器不支持安全随机数')
    Object.defineProperty(globalThis.crypto,'randomUUID',{configurable:true,value:fallbackUuid})
  }
}
