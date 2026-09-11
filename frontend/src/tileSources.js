export const tileSources=[
  {id:'google_satellite',type:'satellite',category:'imagery',name:'Google 卫星影像',minZoom:0,maxZoom:20,url:'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',attribution:'Imagery © Google'},
  {id:'google_hybrid',type:'satellite',category:'imagery',name:'Google 混合影像',minZoom:0,maxZoom:20,url:'https://mt1.google.com/vt/lyrs=y&x={x}&y={y}&z={z}',attribution:'Imagery © Google'},
  {id:'bing_aerial',type:'satellite',category:'imagery',name:'Bing 航拍影像',minZoom:1,maxZoom:20,url:'https://ecn.t3.tiles.virtualearth.net/tiles/a{q}.jpeg?g=1',attribution:'Imagery © Microsoft Bing',quadkey:true},
  {id:'amap_satellite',type:'satellite',category:'imagery',name:'高德卫星影像',minZoom:1,maxZoom:18,url:'https://webst02.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',attribution:'地图 © 高德'},
  {id:'amap_hybrid',type:'satellite',category:'imagery',name:'高德卫星混合标注',minZoom:1,maxZoom:18,url:'https://webst02.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}',attribution:'地图 © 高德'},
  {id:'amap_standard',type:'satellite',category:'standard',name:'高德标准地图',minZoom:1,maxZoom:18,url:'https://webrd02.is.autonavi.com/appmaptile?style=7&x={x}&y={y}&z={z}',attribution:'地图 © 高德'},
  {id:'osm_standard',type:'satellite',category:'standard',name:'OpenStreetMap 标准地图',minZoom:0,maxZoom:19,url:'https://tile.openstreetmap.org/{z}/{x}/{y}.png',attribution:'© OpenStreetMap contributors'},
  {id:'opentopomap',type:'satellite',category:'terrain',name:'OpenTopoMap 地形图',minZoom:0,maxZoom:17,url:'https://tile.opentopomap.org/{z}/{x}/{y}.png',attribution:'© OpenTopoMap contributors'},
  {id:'aws_terrarium',type:'dem',category:'dem',name:'AWS Terrarium 高程',minZoom:0,maxZoom:15,url:'https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png',attribution:'Elevation © AWS Open Data / Mapzen'},
]
