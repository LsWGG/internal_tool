# Internal Tool Collection（内部工具集合）

> 面向工程场景的开源工具集合，提供地图下载、数据迁移、格式转换与内容采集的一体化 Web 平台。

## 项目简介

Internal Tool Collection 是一个前后端分离的实用工具平台，采用 Vue 3 + FastAPI 构建，覆盖日常工程中常见的高频任务：

- 地图数据下载与预览
- Docker 离线包下载
- GJB5068 地图转 Shapefile
- Elasticsearch / NebulaGraph 数据迁移
- Markdown 转 Word
- 图片格式转换
- 数据库在线操作
- 智能网页采集
- GitHub 每日趋势报告

平台采用任务化流程：创建任务 → 执行进度监控 → 结果预览 → 导出下载 → 清理资源，强调可复用、可追踪、可维护。

## 功能模块

### 1. 卫星地图下载

- 在线地图上框选多个范围并批量创建任务
- 支持按层级范围下载（如 Z1–Z17）
- 支持 PNG、GeoTIFF 输出
- 支持 DEM（高程）下载与高程图层渲染
- 支持按国家/州/城市等行政区边界下载
- 任务支持暂停、继续、重试、删除
- 支持 ZIP 结果导出与失败清理

### 2. Docker 离线包下载

- 支持常见架构：`x86_64`、`aarch64`、`armv7l`
- 支持指定 Docker 与 Docker Compose 版本
- 支持在线版本列表查询
- 生成离线安装包并提供安装说明
- 支持任务生命周期管理（暂停/继续/重试/删除）
- 支持 ZIP 导出

### 3. GJB5068 地图转 Shapefile

- 支持上传 GJB 地图目录（含 `.SMS`、`.XMS/.XSX/.XTP/.XZB`）
- 解析点线面与注记图层
- 一次任务支持多图层统一输出
- 地图页预览转换结果
- 支持失败重试与任务删除

### 4. Elasticsearch 迁移

- 支持 ES 7/8 集群连接测试
- 支持索引级别的导入导出
- 支持保留 `_id` 的导出与恢复
- 支持导入导出任务异步执行

### 5. NebulaGraph 迁移

- 支持 Space 级别配置与 Schema 导出
- 支持点、边及关联数据导出恢复
- 支持任务生命周期管理

### 6. Markdown 转 Word

- 支持上传 Markdown 文件并转换为 DOCX
- 内置 Mermaid 渲染并嵌入 Word
- 提供转换结果预览和导出
- 支持任务重试与删除

### 7. 图片格式转换

- 支持 HEIC/HEIF 相关流程与常见图片格式转换
- 支持单文件、多文件与目录上传
- 支持批量 ZIP 打包导出
- 支持元数据保留和失败项重试

### 8. 数据库在线操作

- 支持 SQLite、PostgreSQL、Redis 的基础操作
- 支持连接测试、表/键查看、分页查询
- 支持数据导入导出与任务管理

### 9. 智能网页采集

- 支持通用网页、新闻站、微博/公众号、X（Twitter）、YouTube、TikTok（按能力开放）等场景
- 支持字段抽取规则与自定义字段
- 支持一次性任务与定时任务
- 支持结果预览与导出

### 10. GitHub 每日热门报告

- 定时抓取并生成每日热门仓库报告
- 支持按日期查看历史记录
- 支持收藏与报告快速跳转

## 项目结构

- `backend/`：FastAPI 后端服务、任务调度与接口
- `frontend/`：Vue 前端应用
- `start.sh`：一键启动脚本
- `tmp/`：运行时临时文件目录

## 快速开始

### 1. 依赖环境

- Python 3.11+
- Node.js 18+
- uv（推荐）或可用的 pip 环境

### 2. 安装

```bash
cd /Users/shunli/Documents/Home/Python/samples/projects/internal_tool
uv venv .venv
source .venv/bin/activate
cd backend
pip install -r requirements.txt
playwright install chromium
# Mermaid 图片导出与 Markdown 转 Word 需要 Mermaid CLI
npm install -g @mermaid-js/mermaid-cli
cd ../frontend
npm install
```

### 3. 启动

```bash
cd /Users/shunli/Documents/Home/Python/samples/projects/internal_tool
./start.sh
```

启动后访问：

- 前端：http://localhost:5173
- API 文档：http://localhost:8000/docs

按 `Ctrl + C` 可同时停止前后端。

## API 说明

各模块统一使用 `/api/<模块>/...` 风格提供任务化接口，通常包含：

- 创建任务
- 查询任务列表/详情
- 暂停
- 继续
- 重试
- 删除
- 导出

完整接口定义请参考服务运行时 `http://localhost:8000/docs`。

## 部署与数据

- 任务数据与结果文件按模块保存在后端运行目录内
- 删除任务时会清理关联产物文件，避免长期堆积
- 建议单机测试部署；如需多实例生产化，请结合外部队列/数据库改造任务调度

## 贡献指南

欢迎提交 Issue 与 Pull Request。

1. Fork 本仓库
2. 新建特性分支
3. 提交实现与必要的使用说明
4. 发起 PR，并在说明中补充页面截图或示例任务结果

## 安全与合规

- 严禁提交 `.env`、Token、API Key 等敏感信息
- 地图与爬取任务请遵循目标站点的 robots 与服务条款
- 建议在正式环境启用鉴权与访问限速

## 许可说明

本仓库当前以开源项目形式维护，请根据你的发布需求补充具体许可证文件（如 Apache-2.0、MIT 等）。
