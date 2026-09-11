# 工程工具门户

基于 Vue 3 与 FastAPI 的前后端分离工具集合，包含地图、数据迁移、离线交付与技术趋势工具。

## GitHub 每日热门报告

- 每天自动抓取 GitHub Trending 日榜并生成一页报告；
- 展示仓库名称、简介、语言、Star、Fork 和今日新增 Star；
- 点击仓库卡片可跳转至对应 GitHub 页面；
- 支持手动刷新今日报告和按日期查看历史报告；
- 本地最多保留最近 7 份日报，超出后自动清理最早报告。

## 1. 卫星地图下载

- 地图上连续框选多个矩形并批量创建任务；
- Google Satellite 瓦片下载、精确范围裁剪和大图分片；
- 支持导出 PNG 或带 EPSG:3857 空间参考的 GeoTIFF（`.tif`）；PNG 按 Zoom 分目录，GeoTIFF 在 ZIP 根目录平铺并在文件名保留 `zXX` 分辨率标识；
- 支持下载 DEM 高程数据并同步切换为分层设色高程底图；DEM 输出为米制单波段 Float32 GeoTIFF，最高层级 Z15；
- 配置起止层级，例如 Z1–Z17 会下载全部 17 个层级；
- 各层级输出到 `z01`、`z02` … `z17` 目录，文件名也包含层级；
- 搜索国家、州/省、市和区县，并按真实行政区边界裁切；
- 页面地图使用卫星影像底图，便于在框选前确认实际地表内容；
- 任务名称自动生成：行政区任务使用行政区名称，自由框选任务使用坐标范围与层级；
- 后台任务队列、本地状态持久化、进度轮询和失败信息；
- 任务暂停、继续、失败重试和删除；删除任务时同步清理影像目录及 ZIP；
- 每个任务的全部产物打包为 ZIP 导出。

## 启动

一键启动前后端：

```bash
cd script/maps_utils/web_service
./start.sh
```

按 `Ctrl+C` 会同时停止前后端服务。也可以分别启动：

后端（建议使用项目根目录已有的 `.venv`）：

```bash
cd script/maps_utils/web_service/backend
../../../../.venv/bin/pip install -r requirements.txt
../../../../.venv/bin/python run.py
```

前端另开一个终端：

```bash
cd script/maps_utils/web_service/frontend
npm install
npm run dev
```

浏览器打开 `http://localhost:5173`。FastAPI 文档位于 `http://localhost:8000/docs`。

Markdown 转 Word 与网页预览还需要以下本地转换组件：

```bash
brew install pandoc
npm install -g @mermaid-js/mermaid-cli
../../../.venv/bin/playwright install chromium
```

安装 LibreOffice 后会优先使用其生成 Word 页面预览；未安装时自动使用 Pandoc + Chromium 生成高质量 PDF 预览，不影响 DOCX 下载。

## API

- `POST /api/tasks`：创建单个任务
- `POST /api/tasks/batch`：批量创建任务
- `GET /api/regions/search?q=上海`：搜索行政区划及其 GeoJSON 边界
- `GET /api/tasks`：任务列表与进度
- `GET /api/tasks/{id}`：任务详情
- `POST /api/tasks/{id}/pause`：暂停任务
- `POST /api/tasks/{id}/resume`：继续任务
- `POST /api/tasks/{id}/retry`：清理失败残留并重新执行任务
- `DELETE /api/tasks/{id}`：取消并删除任务及文件
- `GET /api/tasks/{id}/export`：导出已完成任务 ZIP

任务元数据保存在 `backend/data/tasks.json`，下载产物保存在 `backend/data/{任务ID}/`。当前任务队列为单机实现，适合本地或单实例部署；生产多实例部署时建议将任务执行器替换为 Celery/RQ，并把状态迁移到 Redis 或数据库。

> 下载第三方地图瓦片及 OSM 数据时，请遵守对应服务的使用条款、授权与访问频率限制。

为防止误选大区域和高层级导致磁盘耗尽，单任务限制为 20,000 个瓦片。例如整个国家下载到 Z17 通常远超限制，需要拆分成州/省、市等多个批次。

## 2. Docker 离线包下载

Web 服务端受控调用 `script/shell/download_docker_offline.sh`，支持：

- `x86_64`、`aarch64`、`armv7l` 三种 Linux 架构；
- Docker 和 Docker Compose 指定版本，或留空自动下载最新版；
- 在线获取可用版本列表；
- 生成 Docker 静态二进制、Compose、`docker_install.sh` 和 `INSTALL.md`；
- 任务进度、暂停、继续、失败重试、删除及 ZIP 导出。

相关接口：

- `GET /api/docker/versions?arch=x86_64`
- `GET /api/docker/tasks`
- `POST /api/docker/tasks`
- `POST /api/docker/tasks/{id}/pause|resume|retry`
- `DELETE /api/docker/tasks/{id}`
- `GET /api/docker/tasks/{id}/export`

Docker 离线包任务保存在 `backend/docker_data/`。目标服务器需要是使用 systemd 的 Linux 系统，安装脚本需以 root 权限运行。

## 3. GJB5068 地图转 Shapefile

支持上传 `.SMS` 及 `.XMS/.XSX/.XTP/.XZB`（`X` 为图层组字母）的 GJB 地图目录，并输出标准 Shapefile；其中 XTP 属性指针表按图层实际清单选配，注记等图层可以不包含 XTP：

- 支持 A/B/C/D/E/F/I/J/K/L/R 图层组，可同时解析点（P）、线（L）、面（A）和注记（N）；
- 解析 SX/TP 属性和 SMS 图幅元数据，并自动应用 SMS 中的相对坐标原点及放大系数；
- 根据 SMS 带号自动生成 CGCS2000 高斯-克吕格坐标系，避免仅修改坐标系声明而造成坐标错位；
- 自动合并所有源图层，只按几何类型输出合并后的 `.shp/.shx/.dbf/.prj/.cpg`，不为每个源图层拆分文件；
- 通过 GeoJSON 在卫星底图上预览转换结果；
- 转换结果打包 ZIP 下载，支持失败重试和任务删除；
- 单次目录上传上限 200MB，页面预览上限 5,000 个要素，完整下载结果不受预览上限影响。

相关接口：

- `GET /api/gjb/tasks`
- `POST /api/gjb/tasks`（multipart 目录文件上传）
- `POST /api/gjb/tasks/{id}/retry`
- `DELETE /api/gjb/tasks/{id}`
- `GET /api/gjb/tasks/{id}/preview`
- `GET /api/gjb/tasks/{id}/export`

GJB 转换任务及上传文件保存在 `backend/gjb_data/`。

## 4. Elasticsearch 数据导入导出

将 `script/es_utils/es_data_util.py` 的索引 Mapping 与 JSON Lines 数据迁移能力整合为 Web 工具：

- 支持 ES 7/8 HTTP/HTTPS、用户名密码及证书校验配置；
- 在线测试连接并获取索引、文档数和存储大小；
- 多选索引，使用 Scroll 批量导出 Mapping 与文档并生成 ZIP；
- 上传本工具导出的 ZIP，或上传包含 `mapping/`、`data/` 的目录进行恢复；
- 支持保留原 `_id`、覆盖同名索引，并始终导入或导出所选索引的全部数据；
- 支持进度、暂停、继续、删除和导出包下载；
- 密码仅保留在当前后台执行对象中，不写入任务状态文件。

## 5. NebulaGraph 数据导入导出

将 `script/nebula_utils/nebula_data_util.py` 的 NebulaGraph 3.x 迁移能力整合为 Web 工具：

- 配置 Graph 服务地址、端口、用户名和密码并在线获取 Space；
- 多选 Space，完整导出 Space Schema、Tag、Edge、全部点和全部边；
- 导出结构为 `{space}_base.txt`、`{space}_node.csv`、`{space}_edge.csv` 并打包 ZIP；
- 支持上传导出 ZIP 或备份目录，按 Schema → 点 → 边的顺序恢复；
- 任务进度、结果下载和任务删除；
- 密码仅用于当前连接和任务执行，不写入持久化任务记录。

## 6. Markdown 转 Word

基于开源 Pandoc 与 Mermaid CLI 的高质量文档转换工具：

- 上传 UTF-8 编码的 `.md` 或 `.markdown` 文件并异步生成 `.docx`；
- 支持 GFM 标题、目录、列表、任务列表、表格、引用和代码高亮；
- 自动识别 `mermaid` 代码块，渲染为 3 倍分辨率透明 PNG 后嵌入 Word；
- 根据图片宽高和 Word 正文区域自动等比缩放，超宽、超高 Mermaid 图均限制在单页可用范围内；
- 默认使用适合商务文档和黑白打印的“商务黑”主题，也可选择清晰蓝紫或森林绿；
- Mermaid 及其他文档图片在 Word 与 PDF 预览中统一居中；
- 使用内置 A4 Word 参考模板统一页边距、标题层级、正文、代码和表格样式；
- 支持任务进度、PDF 页面预览、Word 下载、失败重试和删除；
- 删除任务时递归清理原始 Markdown、Mermaid 图片、DOCX、PDF 及中间文件。

相关接口：

- `GET /api/md-word/tasks`
- `POST /api/md-word/tasks`（multipart Markdown 文件上传）
- `POST /api/md-word/tasks/{id}/retry`
- `DELETE /api/md-word/tasks/{id}`
- `GET /api/md-word/tasks/{id}/preview`
- `GET /api/md-word/tasks/{id}/export`

转换任务保存在 `backend/md_word_data/`，任务状态服务重启后可恢复读取；运行中的任务若被中断，会标记为失败并允许重新转换。

## 7. 图片格式转换

面向照片整理与图片交付的异步批量转换工具：

- 必须支持并已接入 HEIC/HEIF 解码与编码，同时支持 JPEG、PNG、WebP、TIFF、BMP 等输入；
- 可输出 HEIC、JPEG、PNG、WebP、TIFF，并为 HEIC、JPEG、WebP 配置图片质量；
- 支持选择单张、多张图片或上传整个目录，结果 ZIP 保留原相对目录结构；
- 跨格式保留 EXIF/GPS、拍摄时间、ICC 色彩配置和 XMP，按 EXIF 方向校正像素后写入标准方向；
- 单张失败不会中断整批转换，任务中展示成功和失败数量；全部失败的任务可直接重试；
- 支持任务进度、ZIP 下载、失败重试和删除，删除时递归清理原图、转换结果及 ZIP。

相关接口：

- `GET /api/image-convert/tasks`
- `POST /api/image-convert/tasks`（multipart 多图片/目录上传）
- `POST /api/image-convert/tasks/{id}/retry`
- `DELETE /api/image-convert/tasks/{id}`
- `GET /api/image-convert/tasks/{id}/export`

转换任务保存在 `backend/image_convert_data/`。单次最多 5000 张图片，单文件不超过 500MB，总上传不超过 2GB。

## 8. 数据库在线操作

支持 SQLite、PostgreSQL 和 Redis 的常用在线操作：

- 测试连接、浏览表/键、查看字段和分页数据；
- 对关系型数据库执行新增、修改、删除，对 Redis 执行键值读写和删除；
- 将全部或指定表/键导出为 ZIP JSON 备份，并可导入到另一实例完成基础迁移；
- 导入导出均为异步任务，支持进度、结果下载、失败重试和删除；
- 数据库密码只保留在当前进程任务上下文，不写入 `tasks.json`。

相关接口：

- `POST /api/database/test`
- `POST /api/database/schema`
- `POST /api/database/rows`
- `POST /api/database/crud`
- `GET /api/database/tasks`
- `POST /api/database/tasks/export`
- `POST /api/database/tasks/import`
- `POST /api/database/tasks/{id}/retry`
- `DELETE /api/database/tasks/{id}`
- `GET /api/database/tasks/{id}/export`

## 9. 智能网页采集

面向无需登录的公开网页，支持通用网页、新闻网站、Twitter/X、YouTube 和微信公众号文章模板：

- 内置标题、摘要、作者、发布时间、正文、图片、互动数据等字段，可用 CSS 选择器手动调整；
- 输入示例页面后自动分析 HTML、OpenGraph 和 JSON-LD，生成字段建议；配置 `CRAWLER_LLM_URL` 与 `CRAWLER_LLM_API_KEY` 时可调用兼容 OpenAI 的模型优化选择器；
- 支持请求间隔、失败记录、代理轮换、robots.txt 校验和可选 Playwright 动态页面渲染；
- 支持一次性或每小时/每 6 小时/每天定时采集，导出 JSON、JSONL、CSV、Excel；
- 支持任务进度、暂停、继续、失败重试、删除和结果下载。

Twitter/X 采集不需要配置 `X_BEARER_TOKEN`：系统从公开网页索引或官方公开账号时间线发现推文，
再通过 X 官方 oEmbed 接口读取正文。支持普通关键词、`@账号`、`from:账号 关键词` 和已知推文链接；
其中账号查询更稳定，普通关键词的实时性取决于公开搜索引擎的收录情况。
页面提供用户信息、用户历史推文和关键词推文三种模式；用户信息模式可导出用户名、显示名称、
简介、所在地、粉丝数、关注数、推文数、注册时间、认证状态、头像和主页地址。

TikTok 采集无需 API Key，支持关键词视频搜索、指定视频评论、账号公开资料和账号公开视频四种模式。
视频与账号元数据由 `yt-dlp` 解析，公开评论通过 Playwright 渲染并滚动读取；受地区网络限制时可在
高级设置中填写代理。登录、验证码或平台访问控制出现时会停止采集并给出提示。

Telegram 采集支持公开频道、公开群组消息、群组成员资料和全平台消息搜索。公开频道与公开群组
直接读取 `t.me` 页面；群组成员、私有内容和全平台搜索使用 Telegram 官方 API，并需要在页面填写
API ID、API Hash 与 Telethon StringSession，或通过 `TELEGRAM_API_ID`、`TELEGRAM_API_HASH`、
`TELEGRAM_SESSION` 环境变量统一配置。系统只读取当前授权账号本来有权访问的内容。

工具不会绕过验证码、登录墙或其他访问控制，仅处理用户有权访问的公开内容。

YouTube 下载依赖 `yt-dlp[default,deno]`（包含 JavaScript 解析组件和 Deno），
并使用 FFmpeg 合并音视频。安装后端依赖后重启后端，确保运行进程加载新版组件。
遇到播放地址失效或临时网络错误时，任务会重新解析地址，最多追加两次重试。
部分失败时，任务显示成功与失败数量，ZIP 中保留成功视频和 `download_errors.json`。
点击“重新采集”会复用本次视频列表和下载断点；定时任务仍重新搜索新内容。

相关接口：

- `GET /api/crawler/fields?source=news`
- `POST /api/crawler/analyze`
- `GET /api/crawler/tasks`
- `POST /api/crawler/tasks`
- `POST /api/crawler/tasks/{id}/{pause|resume|retry}`
- `DELETE /api/crawler/tasks/{id}`
- `GET /api/crawler/tasks/{id}/export`

## Word 模板批量生成

入口：门户「文档与媒体工具」→「Word 批量生成」（`/#word-batch`）。参考 [Selfboot 批量生成 Word](https://gallery.selfboot.cn/zh/tools/gendocx) 的双大括号占位符及逐行生成方式，新增图片、字段映射、持久化任务与结果预览。

使用步骤：

1. 在自定义 `.docx` 模板中填写 `{{姓名}}`、`{{部门}}` 等占位符，图片使用 `{{%照片}}`。支持正文、表格、页眉页脚及被 Word 拆为多个文字片段的占位符。
2. 上传 `.xlsx`，第一行是唯一且非空的表头，每一行是一份文档。可选择工作表、调整字段映射。普通占位符也可以指定为图片类型。
3. 图片可以是 Excel 数据单元格锚定的内嵌图片，或 Excel 中的图片文件名 / ZIP 内相对路径，另行上传图片或 ZIP。支持 PNG、JPEG、WebP、BMP、TIFF、HEIC/HEIF，处理后以 PNG 插入。重复文件名须写完整相对路径。不支持 `IMAGE()` / `DISPIMG()` 等公式式图片、本地绝对路径或远程图片 URL。
4. 设置每个图片字段的最大宽高（mm），等比例缩放，不裁剪；模板本身控制段落、表格、图片对齐方式。图片占位符建议独占段落，宽高不要大于所在单元格。
5. 点击「试生成首行」确认内容，随后批量生成。支持 `{{姓名}}_通知_{{序号}}` 命名，默认第一列，重名自动编号；结果每页 20 项，可预览、单独下载或 ZIP 下载。

任务在后端 `word_batch_data` 保存，刷新页面可继续查看进度。逐行失败不影响其他行，ZIP 包含成功文件和失败明细；失败 / 部分失败任务支持重新生成整批。删除任务同时删除模板副本、数据、素材、结果和 ZIP；运行中不允许删除。未提交工作区在重置或离开工具时清理，异常关闭遗留的工作区在服务启动时清理超过 24 小时的部分。

依赖：安装后端 `requirements.txt`（新增 `python-docx`、`openpyxl`），前端执行 `npm install`（新增 `docx-preview`）。生成无需本机 Microsoft Word 或 LibreOffice。浏览器预览不执行超链接，保留模板样式，但字体、复杂版式和分页以 Word 打开文件为准。

限制：模板 20MB、Excel 30MB；每张工作表最多 5000 行 / 200 列；上传图片及解码后的素材各不超过 200MB、最多 1000 张；单任务生成结果上限 1GB。DOCX 不接受宏、嵌入式程序及外链图片；ZIP 检查解压总量和路径。占位符仅做字面替换，不执行表达式、循环、条件或代码。Excel 公式使用已缓存值，缺少缓存会提示在 Excel 重新计算保存；日期、零值、简单前导零 / 百分比 / 小数格式做常用格式转换，复杂自定义格式建议先转为文本。

测试：`PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_word_batch.py -v`；启动前端 `127.0.0.1:5174` 后运行 `frontend/test_word_batch_browser.py`，它会使用独立临时 API（8001）验证上传、嵌入图片、预览、生成、下载、表单重置和删除清理。

## 图片元数据工具

门户入口：图片元数据（`/#image-metadata`）。参考 Selfboot 图片元数据工具的使用方式，采用浏览器内 WebAssembly 读写引擎，而非只支持 JPEG 的 EXIF 写入方式。

- 无需安装本地 ExifTool 命令、Perl 或后端依赖。前端执行 `npm install` 后，WASM 随 Vite 打包部署，不从第三方 CDN 加载。
- 多图片导入，浏览可识别的 EXIF / GPS / IPTC / XMP 等字段，支持标准可写字段的新增、修改、删除和批量应用，以及 GPS 地图定位、单图下载、JSON 导出、ZIP 打包。
- 原图与编辑副本保存在当前浏览器的 IndexedDB，不上传服务器。恢复原图还原原始文件字节；删除会清除本工具在浏览器保存的原图和副本。清理浏览器数据也会清除图片，请及时下载结果。GPS 地图底图会向 OpenStreetMap 请求瓦片。
- 安全策略：仅接受引擎目录中非危险、非二进制、非结构的可写字段；先在副本写入，再校验格式、像素尺寸和修改字段的回读值，校验失败不替换当前文件。批量任务按图片分别成功或失败，不隐瞒部分失败。
- “全部字段”指全部可识别字段，不代表每个字段都能任意写入。实际像素宽高、压缩编码、偏移、计算字段和厂商私有二进制块只读；格式不支持的写入会明确报错。分辨率是打印密度，不改变像素数。日期不擅自转换时区。
- JPEG、PNG、WebP、TIFF、HEIC、AVIF 提供多格式编辑；BMP/GIF 可以读取，但不保证支持所有标准元数据组。HEIC/HEIF 使用 `heic-to`（libheif，LGPL-3.0）在独立浏览器 Worker 内解码为临时 JPEG 预览，缩略图与大图共用缓存，不修改原图或导出格式。TIFF 是否可预览仍取决于浏览器原生支持。
- 图片列表支持逐张删除，工具栏支持批量删除，窄屏也保留删除入口；删除同时清除 IndexedDB 中的原图、副本和内存预览缓存。右侧字段独立滚动，保存栏固定；字段显示中文名称与说明，无法准确解释的厂商扩展字段明确标注并保留原名。
- 引擎约 25MB，首次载入需等待；建议使用桌面 Chrome/Edge。每批最多 100 张、单张 100MB，总计 1GB，实际容量受浏览器存储和内存限制。

浏览器回归测试：启动前端到 `127.0.0.1:5174` 后，使用具备 Pillow、pillow-heif 和 Playwright Chromium 的 Python 环境执行 `frontend/test_metadata_browser.py`。测试生成图片，不需要真实照片。
