# Internal Tool Collection

> An open-source-friendly toolbox for engineering operations: map data acquisition, data migration, format conversion, and content collection.

## Overview

This repository provides a unified web workspace for commonly used engineering utilities.  
It is built with:

- Frontend: Vue 3 + Vite
- Backend: FastAPI + async task management

Key goals:

- Keep tasks clear and reproducible
- Provide end-to-end operations (create -> run -> monitor -> export -> cleanup)
- Make multi-tool work convenient for daily use
- Keep user experience consistent across modules

## Project Modules

### 1) Satellite Map Download

- Draw multiple rectangle areas on map for batch tasks
- Download map tiles by zoom range (PNG) or GeoTIFF
- Download DEM tiles and render elevation map style
- Download by administrative region (country / state / city / district)
- Pause, resume, retry on failure, delete
- Export task artifacts as ZIP

### 2) Docker Offline Package

- Support common Linux architectures (`x86_64`, `aarch64`, `armv7l`)
- Get available Docker and Docker Compose versions
- Generate offline install bundles and scripts
- Lifecycle controls: pause, resume, retry, delete
- ZIP export

### 3) GJB5068 Map to Shapefile

- Upload GJB map directories (for example `.SMS`, `.XMS/.XSX/.XTP/.XZB`)
- Convert to standard Shapefile outputs
- Preview converted geometry on map
- Retry and delete tasks, ZIP export

### 4) Elasticsearch Migration

- Connect to ES clusters and run import/export workflows
- Export selected indices and restore in target clusters
- Preserve selected data scope and index structure

### 5) NebulaGraph Migration

- Discover and connect to Nebula spaces
- Export schema and data
- Import backup packages into target spaces

### 6) Markdown to Word

- Convert Markdown documents to DOCX
- Support Mermaid chart rendering and embed into Word output
- Preview and export converted results

### 7) Image Format Conversion

- Batch conversion for image formats (including HEIC/HEIF related flows)
- Multi-file and directory upload
- Keep metadata where feasible
- Export converted files by ZIP

### 8) Database Operations

- SQLite / PostgreSQL / Redis operation page
- Connection test, schema/key inspection, basic CRUD
- Data export / import with task lifecycle

### 9) Intelligent Web Crawler

- Universal page/news/X/YouTube/WeChat flows
- Field extraction and selector helper
- Scheduled collection and result management
- Export to common data formats

### 10) GitHub Trending Report

- Daily GitHub trend report generation
- Repository list and metadata with repository links
- Historical report retention and viewing

## Repository Layout

- `backend/`: FastAPI service and task APIs
- `frontend/`: Vue client
- `tmp/`: Runtime output and temporary files
- `start.sh`: one-command startup script

All task records and tool outputs are stored in backend runtime directories.

## Local Development

### Prerequisites

- Python 3.11+
- Node.js 18+
- `uv` (recommended) or `pip` + virtualenv

### Install

```bash
cd /Users/shunli/Documents/Home/Python/samples/projects/internal_tool
uv venv .venv
source .venv/bin/activate
cd backend
pip install -r requirements.txt
cd ../frontend
npm install
```

### Run

```bash
cd /Users/shunli/Documents/Home/Python/samples/projects/internal_tool
./start.sh
```

Default endpoints:

- Frontend: `http://localhost:5173`
- Backend docs: `http://localhost:8000/docs`

Press `Ctrl + C` to stop both services.

## API Notes

Tool APIs are exposed under `/api/<module>/...`, typically including:

- task creation
- task list and detail
- pause / resume
- retry
- delete
- export

Use `/docs` for full generated API schema.

## Configuration

Create environment variables according to your deployment need (do not commit secrets):

- crawler related LLM endpoints and keys (optional)
- database connection parameters (when using DB tool)
- any third-party credentials required by your target data source

> All credentials should be configured at runtime or local `.env` files and excluded from source control.

## Data and Cleanup

Task artifacts and temporary uploads are stored in backend data directories.

When a task is deleted, related artifacts are cleared together with task records.

## Contributing

Contributions are welcome. To contribute:

- Fork the repository
- Create a feature branch
- Keep API and UI behavior backward compatible
- Add clear usage notes for every tool change
- Submit a PR with usage screenshots or sample task outputs

## Security and Compliance

- Keep `.env`, API keys, and tokens out of git history
- Respect robots policies and website terms when using crawler workflows
- Use reasonable rate limits for third-party map and content sources

## License

Add a license file before public redistribution if a specific license is required.
