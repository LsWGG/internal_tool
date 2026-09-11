import PreviewWorker from './metadataPreview.worker.js?worker'

const cache=new Map()
let queue=Promise.resolve()
export function releaseImagePreview(id){const entry=cache.get(id);if(entry?.url)URL.revokeObjectURL(entry.url);cache.delete(id)}
export function decodeImagePreview(record){
  const prior=cache.get(record.id)
  if(prior?.revision===record.revision)return prior.promise
  releaseImagePreview(record.id)
  const entry={revision:record.revision}
  cache.set(record.id,entry)
  entry.promise=queue.then(()=>{
    if(cache.get(record.id)!==entry)throw new Error('图片已删除或更新')
    return new Promise((resolve,reject)=>{
      const worker=new PreviewWorker()
      const finish=(error,blob)=>{clearTimeout(timer);worker.terminate();error?reject(new Error(error)):resolve(blob)}
      const timer=setTimeout(()=>finish('HEIC 预览超时，可继续编辑元数据'),90000)
      worker.onmessage=({data})=>finish(data.error,data.blob)
      worker.onerror=()=>finish('HEIC 解码失败，可继续编辑元数据')
      worker.postMessage(record.file)
    })
  }).then(blob=>{
    if(cache.get(record.id)!==entry)throw new Error('图片已删除或更新')
    entry.url=URL.createObjectURL(blob)
    return entry.url
  })
  queue=entry.promise.catch(()=>{})
  return entry.promise
}
