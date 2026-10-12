#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/dist/docker-offline}"
ARCHIVE_PATH="${2:-$ROOT_DIR/internal-tool-docker-offline.tar.gz}"
PLATFORM="${PLATFORM:-$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || echo linux/amd64)}"
if [[ "${1:-}" == "--platform" ]]; then
  PLATFORM="${2:?请指定平台，例如 linux/arm64}"
  OUTPUT_DIR="${3:-$ROOT_DIR/dist/docker-offline}"
  ARCHIVE_PATH="${4:-$ROOT_DIR/internal-tool-docker-offline.tar.gz}"
fi

if [[ ! -f "$ROOT_DIR/frontend/dist/index.html" ]]; then
  echo "未找到 frontend/dist，请先执行：cd frontend && npm ci && npm run build" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "未找到 docker，请先安装 Docker。" >&2
  exit 1
fi

HOST_PLATFORM="$(docker version --format '{{.Server.Os}}/{{.Server.Arch}}')"
if [[ "$HOST_PLATFORM" != "$PLATFORM" ]]; then
  echo "检测到跨平台构建：$HOST_PLATFORM -> $PLATFORM，正在准备 binfmt/QEMU……"
  docker run --privileged --rm tonistiigi/binfmt --install all
fi

rm -rf "$OUTPUT_DIR"
mkdir -p "$OUTPUT_DIR/frontend/dist" "$OUTPUT_DIR/docker"
cp -a "$ROOT_DIR/frontend/dist/." "$OUTPUT_DIR/frontend/dist/"
cp "$ROOT_DIR/docker-compose.yml" "$OUTPUT_DIR/"
cp -a "$ROOT_DIR/docker/." "$OUTPUT_DIR/docker/"
cp "$ROOT_DIR/.env.example" "$OUTPUT_DIR/.env.example"

# 在联网机器按目标平台构建后端镜像，再导出为离线 tar。需要 Docker Buildx。
docker buildx build --platform "$PLATFORM" --provenance=false --load \
  -t internal_tool-backend:offline \
  -f "$ROOT_DIR/docker/backend.Dockerfile" "$ROOT_DIR"
docker image save internal_tool-backend:offline -o "$OUTPUT_DIR/backend-image.tar"
docker pull --platform "$PLATFORM" nginx:1.27-alpine
docker image save nginx:1.27-alpine -o "$OUTPUT_DIR/nginx-image.tar"

cat > "$OUTPUT_DIR/install-offline.sh" <<'INSTALL'
#!/usr/bin/env bash
set -Eeuo pipefail
BASE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
command -v docker >/dev/null 2>&1 || { echo "未找到 Docker。" >&2; exit 1; }

docker load -i "$BASE_DIR/backend-image.tar"
docker load -i "$BASE_DIR/nginx-image.tar"
# 离线包中的 compose 文件固定使用已导入的镜像，不触发联网 build。
COMPOSE_PROJECT_NAME="${COMPOSE_PROJECT_NAME:-internal-tool}" docker compose -f "$BASE_DIR/docker-compose.yml" up -d --no-build

echo "部署完成：http://localhost"
INSTALL
chmod +x "$OUTPUT_DIR/install-offline.sh"

# 离线包内 compose 改为使用导入镜像，并禁用 build。
python3 - "$OUTPUT_DIR/docker-compose.yml" <<'PY'
from pathlib import Path
p = Path(__import__('sys').argv[1])
s = p.read_text()
s = s.replace('    build:\n      context: .\n      dockerfile: docker/backend.Dockerfile\n', '    image: internal_tool-backend:offline\n')
p.write_text(s)
PY

rm -f "$ARCHIVE_PATH"
tar -C "$(dirname "$OUTPUT_DIR")" -czf "$ARCHIVE_PATH" "$(basename "$OUTPUT_DIR")"
echo "离线包已生成：$ARCHIVE_PATH"
echo "目标平台：$PLATFORM"
