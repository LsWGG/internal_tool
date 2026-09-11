"""Local browser integration check; no uploaded image leaves the isolated browser."""
import base64
import io
import json
import os
from PIL import Image
from pillow_heif import register_heif_opener
from playwright.sync_api import sync_playwright

register_heif_opener()
fixtures = []
for extension, format_name in [('jpg', 'JPEG'), ('png', 'PNG'), ('webp', 'WEBP'), ('tif', 'TIFF'), ('heic', 'HEIF'), ('avif', 'AVIF')]:
    output = io.BytesIO()
    image = Image.new('RGB', (320, 240), '#6b92ca')
    image.save(output, format=format_name)
    fixtures.append({'extension': extension, 'base64': base64.b64encode(output.getvalue()).decode()})

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={'width': 1500, 'height': 1100})
    page.on('pageerror', lambda error: print('PAGE ERROR', str(error), flush=True))
    origin='http://127.0.0.1:5174'
    if os.environ.get('METADATA_TEST_INSECURE'):
        origin='http://metadata.test:5174'
        page.route(origin+'/**', lambda route: route.fulfill(response=route.fetch(url=route.request.url.replace(origin, 'http://127.0.0.1:5174'))))
    # The portal initializes unrelated tools; metadata itself has no backend calls.
    page.route('**/api/**', lambda route: route.abort())
    page.goto(origin+'/#image-metadata')
    if os.environ.get('METADATA_TEST_INSECURE'):
        assert page.evaluate('!isSecureContext && typeof crypto.randomUUID === "undefined"'), 'Not an insecure HTTP test'
    results = page.evaluate('''async fixtures => {
      const {default:client,getImagePreview}=await import('/src/imageMetadataClient.js');
      const results=[];
      for(const fixture of fixtures){
        const bytes=Uint8Array.from(atob(fixture.base64),c=>c.charCodeAt(0));
        const file=new File([bytes],'test.'+fixture.extension);
        const body=new FormData();body.append('files',file);
        const upload=(await client.post('/api/image-metadata/images',body)).data;
        if(upload.errors.length){results.push({format:fixture.extension,upload:upload.errors});continue}
        const id=upload.images[0].id;
        if(fixture.extension==='heic'){
          const preview=await getImagePreview(id);
          const blob=await (await fetch(preview)).blob();
          const bitmap=await createImageBitmap(blob);
          if(blob.type!=='image/jpeg'||bitmap.width!==320||bitmap.height!==240)throw new Error('HEIC preview failed');
          bitmap.close();
        }
        const update=(await client.post('/api/image-metadata/batch',{ids:[id],updates:{'IFD0:Artist':'测试作者','XMP-dc:Title':'图片标题','IFD0:XResolution':300,'IFD0:YResolution':300},gps:{latitude:-33.12345,longitude:-79.23456,altitude:-12}})).data;
        if(update.errors.length){results.push({format:fixture.extension,update:update.errors});continue}
        const detail=update.images[0];
        if(Math.abs(detail.latitude+33.12345)>0.00001)throw new Error('GPS latitude mismatch');
        if(detail.width!==320||detail.height!==240)throw new Error('Dimensions changed');
        const cleared=(await client.post('/api/image-metadata/batch',{ids:[id],clear_gps:true})).data;
        if(cleared.errors.length||cleared.images[0].latitude!==null)throw new Error('Clear GPS failed');
        const list=(await client.post('/api/image-metadata/batch',{ids:[id],updates:{'XMP-dc:Subject':['自然','旅行']},deletes:['IFD0:Artist']})).data;
        if(list.errors.length)throw new Error(JSON.stringify(list.errors));
        const revision=list.images[0].revision;
        const rejected=(await client.post('/api/image-metadata/batch',{ids:[id],updates:{'IFD0:ImageWidth':999}})).data;
        if(!rejected.errors.length||(await client.get('/api/image-metadata/images/'+id)).data.revision!==revision)throw new Error('Unsafe write was not atomic');
        const removed=(await client.post('/api/image-metadata/batch',{ids:[id],clear_all:true})).data;
        if(removed.errors.length)throw new Error(JSON.stringify(removed.errors));
        if(removed.images[0].fields.some(f=>['IFD0:Artist','XMP-dc:Title','XMP-dc:Subject'].includes(f.key)))throw new Error('Metadata not removed');
        const restored=(await client.post('/api/image-metadata/batch',{ids:[id],restore:true})).data;
        if(restored.errors.length)throw new Error('Restore failed');
        const restoredBytes=new Uint8Array(await (await fetch(restored.images[0].preview_url)).arrayBuffer());
        if(bytes.length!==restoredBytes.length||!bytes.every((b,i)=>b===restoredBytes[i]))throw new Error('Original bytes changed');
        results.push({format:fixture.extension,fields:detail.fields.length,gps:detail.latitude,cleared:cleared.errors,restored:restored.errors});
      }
      return results;
    }''', fixtures)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)
    assert all('fields' in result for result in results), 'Some formats failed'
    page.reload()
    page.locator('.metadata-preview-heading b').filter(has_text='test.avif').wait_for(timeout=60000)
    page.set_viewport_size({'width': 1280, 'height': 800})
    page.get_by_role('button',name='常用信息',exact=True).click()
    assert page.locator('.metadata-form-grid').first.evaluate('''grid=>{
      const fields=[...grid.children];
      return fields.every((field,i)=>{
        const control=field.querySelector('input,select').getBoundingClientRect();
        const help=field.querySelector('.metadata-field-help').getBoundingClientRect();
        return help.top>=control.bottom && (i%2===0||Math.abs(control.top-fields[i-1].querySelector('input,select').getBoundingClientRect().top)<1);
      });
    }'''), 'Descriptions disrupted form alignment'
    page.screenshot(path='/private/tmp/image-metadata-common-desktop.png', full_page=True)
    page.set_viewport_size({'width':390,'height':844})
    page.screenshot(path='/private/tmp/image-metadata-common-mobile.png', full_page=True)
    page.set_viewport_size({'width':1280,'height':800})
    page.get_by_role('button', name='全部字段', exact=False).click()
    page.wait_for_function('document.querySelectorAll(".metadata-field").length>0')
    assert page.locator('.metadata-field-help').first.inner_text(), 'Missing field explanation'
    assert page.locator('.metadata-inspector-body').evaluate('(el)=>{el.scrollTop=200;return el.scrollHeight>el.clientHeight&&el.scrollTop>0}'), 'Inspector does not scroll'
    page.locator('.metadata-file').filter(has_text='test.heic').locator('button').first.click()
    page.wait_for_function('document.querySelector(".metadata-preview-heading b")?.textContent === "test.heic" && document.querySelector(".metadata-canvas img")?.naturalWidth>0', timeout=90000)
    page.screenshot(path='/private/tmp/image-metadata-workbench.png', full_page=True)
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile horizontal overflow'
    page.screenshot(path='/private/tmp/image-metadata-mobile.png', full_page=True)
    page.get_by_role('button',name='删除 test.heic',exact=True).click()
    page.get_by_role('button',name='确认删除',exact=True).click()
    page.wait_for_function('!Array.from(document.querySelectorAll(".metadata-file b")).some(e=>e.textContent==="test.heic")')
    page.evaluate('''async()=>{
      const {default:client}=await import('/src/imageMetadataClient.js');
      const ids=(await client.get('/api/image-metadata/images')).data.map(x=>x.id);
      const archive=(await client.post('/api/image-metadata/export',{ids})).data;
      const {unzipSync}=await import('/node_modules/.vite/deps/fflate.js');
      if(Object.keys(unzipSync(new Uint8Array(await archive.arrayBuffer()))).length!==ids.length*2)throw new Error('Incomplete archive');
      const metadataArchive=(await client.post('/api/image-metadata/metadata-export',{ids})).data;
      const metadataFiles=unzipSync(new Uint8Array(await metadataArchive.arrayBuffer()));
      if(Object.keys(metadataFiles).length!==ids.length||Object.keys(metadataFiles).some(name=>!name.endsWith('.metadata.json')))throw new Error('Incomplete metadata archive');
      const deleted=(await client.post('/api/image-metadata/batch',{ids,action:'delete'})).data;
      if(deleted.errors.length||(await client.get('/api/image-metadata/images')).data.length)throw new Error('Delete failed');
    }''')
    browser.close()
