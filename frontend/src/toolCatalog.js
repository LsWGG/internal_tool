export const toolGroups=[
  {id:'geo',name:'地理空间',english:'GEOSPATIAL',description:'地图下载、空间格式转换与图层预览。',icon:'geo',tools:[
    {id:'map',name:'卫星地图下载',tag:'影像与高程',description:'多数据源影像与 DEM，框选范围、批量下载和 GeoTIFF 导出。',keywords:'卫星 高德 Google 地图 瓦片 tif dem',icon:'map'},
    {id:'tile-viewer',name:'地图瓦片查看',tag:'XYZ 瓦片预览',description:'加载在线瓦片链接或上传 z/x/y 目录，检查层级、覆盖范围与图像质量。',keywords:'xyz tiles 瓦片 在线链接 目录 png jpg webp 预览',icon:'map'},
    {id:'propzone',name:'全美行政区域 SHP 下载',tag:'美国区域数据',description:'定位州、县和城市，选择下载颗粒度，获取边界与 zoning 数据。',keywords:'propzone 美国 行政区 shapefile',icon:'layers'},
    {id:'gjb',name:'GJB 地图转 SHP',tag:'空间格式转换',description:'转换 GJB 图幅，保留多图层、注记并预览转换成果。',keywords:'gjb shapefile 转换',icon:'layers'},
    {id:'shp',name:'SHP 文件预览',tag:'空间数据查看',description:'叠加多个 Shapefile，独立控制图层显隐、定位与删除。',keywords:'shapefile gis 预览',icon:'geo'},
  ]},
  {id:'database',name:'数据库',english:'DATABASES',description:'在线管理数据库，完成数据备份、恢复与迁移。',icon:'data',tools:[
    {id:'database',name:'数据库在线操作',tag:'查询与编辑',description:'连接 PostgreSQL、SQLite、Redis，浏览数据并进行 CRUD。',keywords:'pg postgres sqlite redis sql 导入 导出 迁移',icon:'data'},
    {id:'es',name:'Elasticsearch 数据迁移',tag:'搜索数据库',description:'导入导出索引结构及文档，管理备份和恢复任务。',keywords:'es elasticsearch 索引 mapping',icon:'data'},
    {id:'nebula',name:'NebulaGraph 数据迁移',tag:'图数据库',description:'迁移 Space、Tag、Edge，以及点和边数据。',keywords:'nebula 图数据库',icon:'graph'},
  ]},
  {id:'documents',name:'文档处理',english:'DOCUMENTS',description:'从网页、Markdown 和 Excel 制作可交付文档。',icon:'file',tools:[
    {id:'pdf',name:'网页转 PDF',tag:'网页归档',description:'输入网址或上传 Excel，批量转换、预览和下载 PDF。',keywords:'pdf 网页 excel',icon:'file'},
    {id:'word-batch',name:'Word 批量生成',tag:'模板填充',description:'自定义 Word 模板，按 Excel 数据批量填入文字和图片。',keywords:'docx excel 模板 图片 批量 word',icon:'word'},
    {id:'md-word',name:'Markdown 转 Word',tag:'技术文档',description:'保留标题、表格与代码，将 Mermaid 图形嵌入 Word。',keywords:'md markdown mermaid docx word',icon:'word'},
    {id:'mermaid-export',name:'Mermaid 图片导出',tag:'图表生成',description:'从 Markdown 提取 Mermaid 代码块，按标题命名并预览、导出 PNG。',keywords:'mermaid markdown png 图表 流程图',icon:'image'},
  ]},
  {id:'images',name:'图片处理',english:'IMAGES',description:'转换图片格式，查看和管理照片元数据。',icon:'image',tools:[
    {id:'image-convert',name:'图片格式转换',tag:'多格式转换',description:'批量转换 HEIC、JPEG、PNG 等格式，尽可能保留元数据。',keywords:'heic jpg jpeg png webp tiff 图片',icon:'image'},
    {id:'image-metadata',name:'图片元数据',tag:'照片信息管理',description:'查看和编辑 GPS、拍摄时间、设备与版权，支持 HEIC 预览。',keywords:'exif gps heic 元数据 位置 隐私',icon:'image'},
  ]},
  {id:'collection',name:'采集与资讯',english:'COLLECTION & INSIGHTS',description:'采集公开内容，跟踪开源项目动态。',icon:'trend',tools:[
    {id:'crawler',name:'智能网页采集',tag:'公开内容采集',description:'按数据源配置新闻、公众号、视频和社交内容采集任务。',keywords:'爬虫 新闻 微信 youtube 推特 twitter x',icon:'layers'},
    {id:'trending',name:'GitHub 每日热门榜单',tag:'技术趋势',description:'查看热门仓库、中文简介及近 7 天历史报告。',keywords:'github trending 热门 开源 历史 翻译',icon:'trend'},
  ]},
  {id:'delivery',name:'开发交付',english:'DEVELOPMENT & DELIVERY',description:'为离线部署准备软件安装包。',icon:'package',tools:[
    {id:'docker',name:'Docker 离线包',tag:'离线部署',description:'选择系统架构和版本，生成 Docker、Compose 与安装脚本。',keywords:'docker compose linux devops 离线',icon:'package'},
  ]},
]
