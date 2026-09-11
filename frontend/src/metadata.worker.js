import {dispose, parseMetadata, writeMetadata} from '@uswriting/exiftool'
import wasmUrl from '@6over3/zeroperl-ts/zeroperl.wasm?url'
import {installMetadataUuid} from './metadataUuid'

const options={fetch:()=>fetch(wasmUrl)}
// ZeroPerl 1.0.10 detects browsers via window/document, excluding Web Workers.
// Supply detection-only markers so its loader uses our bundled WASM fetch.
self.window=self
self.document={}
let queue=Promise.resolve()
self.addEventListener('unhandledrejection',event=>event.preventDefault())

async function processRequest(data){
  const file=new File([data.file], 'image'+(data.extension||'.jpg'))
  if(data.action==='catalog')return parseMetadata(file,{...options,args:['-lang','en','-f','-listx']})
  // Some valid PNG files store ancillary text/eXIf chunks after IDAT.
  // ExifTool repairs their order while writing but reports a minor warning
  // on stderr; the WASM wrapper otherwise mistakes that successful repair
  // for a failed write and discards the output. `-m` ignores only ExifTool
  // minor errors, while the client still re-reads and verifies every tag,
  // format and pixel dimension before replacing the edited copy.
  if(data.action==='write')return writeMetadata(file,data.tags,{...options,args:['-n','-m']})
  return parseMetadata(file,{...options,args:['-j','-G1','-s','-n','-a','-struct']})
}

self.onmessage=({data})=>{
  queue=queue.then(async()=>{
    try{
      installMetadataUuid()
      let result,retried=false
      try{result=await processRequest(data)}
      catch(error){
        // ZeroPerl may retain a fragmented linear-memory layout after many
        // large images. Recreate the engine and retry only this recoverable
        // allocation error; semantic/file errors must still reach the user.
        if(!/offset is out of bounds|out of bounds offset/i.test(String(error?.message||error)))throw error
        retried=true;try{await dispose()}catch{}installMetadataUuid();result=await processRequest(data)
      }
      if(!retried&&!result.success&&/offset is out of bounds|out of bounds offset/i.test(String(result.error||''))){
        try{await dispose()}catch{}installMetadataUuid();result=await processRequest(data)
      }
      if(!result.success)throw new Error(result.error||'元数据处理失败')
      self.postMessage({id:data.id,data:result.data})
    }catch(error){console.error(error);self.postMessage({id:data.id,error:error.message})}
  })
}
