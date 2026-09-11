import {heicTo} from 'heic-to/next'

self.onmessage=async({data})=>{
  try{
    const blob=await heicTo({blob:data,type:'image/jpeg',quality:0.88})
    self.postMessage({blob})
  }catch(error){self.postMessage({error:error.message||'HEIC 解码失败'})}
}
