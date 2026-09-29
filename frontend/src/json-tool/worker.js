import {processDocument} from './engine.js'
self.onmessage = ({data}) => {
  try { self.postMessage({id:data.id, result:processDocument(data)}) }
  catch (error) { self.postMessage({id:data.id, error:error.message}) }
}
