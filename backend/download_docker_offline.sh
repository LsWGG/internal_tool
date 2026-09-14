#!/bin/bash

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 默认参数
ARCH=""
DOCKER_VERSION=""
COMPOSE_VERSION=""
DOWNLOAD_DIR="docker-offline-packages"
SHOW_HELP=false

# 显示帮助信息
show_help() {
    echo -e "${GREEN}Docker 离线安装包下载工具${NC}"
    echo -e ""
    echo -e "${GREEN}使用方法：${NC}"
    echo -e "  $0 [-a <arch>] [-d <docker-version>] [-c <compose-version>] [-o <output-dir>] [-h]"
    echo -e ""
    echo -e "${GREEN}参数说明：${NC}"
    echo -e "  -a <arch>      指定架构 (x86_64, aarch64, armv7l)"
    echo -e "                 如不指定，将自动检测系统架构"
    echo -e "  -d <version>   指定 Docker 版本 (如: 24.0.7)"
    echo -e "                 如不指定，将下载最新版本"
    echo -e "  -c <version>   指定 docker-compose 版本 (如: 2.21.0)"
    echo -e "                 如不指定，将下载最新版本"
    echo -e "  -o <dir>       指定下载目录 (默认: docker-offline-packages)"
    echo -e "  -h             显示帮助信息"
    echo -e ""
    echo -e "${GREEN}示例：${NC}"
    echo -e "  $0                                    # 自动检测架构，下载最新版本"
    echo -e "  $0 -a x86_64                          # 指定 x86_64 架构，下载最新版本"
    echo -e "  $0 -a aarch64 -d 24.0.7 -c 2.40.3    # 指定版本下载"
    echo -e "  $0 -o /tmp/docker-packages           # 指定下载目录"
}

# 解析命令行参数
while getopts "a:d:c:o:h" opt; do
    case $opt in
        a)
            ARCH="$OPTARG"
            ;;
        d)
            DOCKER_VERSION="$OPTARG"
            ;;
        c)
            COMPOSE_VERSION="$OPTARG"
            ;;
        o)
            DOWNLOAD_DIR="$OPTARG"
            ;;
        h)
            SHOW_HELP=true
            ;;
        \?)
            echo -e "${RED}无效参数${NC}"
            show_help
            exit 1
            ;;
    esac
done

if $SHOW_HELP; then
    show_help
    exit 0
fi

# 检测系统架构
detect_arch() {
    local arch=$(uname -m)
    case $arch in
        x86_64|amd64)
            echo "x86_64"
            ;;
        aarch64|arm64)
            echo "aarch64"
            ;;
        armv7l|armv7)
            echo "armv7l"
            ;;
        *)
            echo "unknown"
            ;;
    esac
}

# 验证架构
validate_arch() {
    local arch=$1
    case $arch in
        x86_64|aarch64|armv7l)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# 获取架构显示名称
get_arch_display_name() {
    case $1 in
        x86_64)
            echo "x86_64 (Intel/AMD 64位)"
            ;;
        aarch64)
            echo "aarch64 (ARM 64位)"
            ;;
        armv7l)
            echo "armv7l (ARM 32位)"
            ;;
    esac
}

# 获取可用 Docker 版本列表
get_docker_versions() {
    local arch=$1
    case $arch in
        x86_64)
            local url="https://download.docker.com/linux/static/stable/x86_64/"
            ;;
        aarch64)
            local url="https://download.docker.com/linux/static/stable/aarch64/"
            ;;
        armv7l)
            local url="https://download.docker.com/linux/static/stable/armhf/"
            ;;
        *)
            echo ""
            return
            ;;
    esac
    
    curl -s "$url" | grep -o 'docker-[0-9.]*.tgz' | grep -v 'rootless\|ce' | sed 's/docker-//;s/.tgz//' | sort -Vr
}

# 获取最新 Docker 版本
get_latest_docker_version() {
    local arch=$1
    get_docker_versions "$arch" | head -1
}

# 获取可用 docker-compose 版本列表
get_compose_versions() {
    curl -s "https://api.github.com/repos/docker/compose/releases" | grep -o '"tag_name": "[^"]*"' | cut -d'"' -f4 | sed 's/v//' | sort -Vr
}

# 获取最新 docker-compose 版本
get_latest_compose_version() {
    get_compose_versions | head -1
}

# 检查网络连接
check_network() {
    echo -e "${YELLOW}检查网络连接...${NC}"
    
    # 检查是否能访问 Docker 下载站点
    if ! curl -s --connect-timeout 5 "https://download.docker.com" > /dev/null; then
        echo -e "${RED}错误：无法访问 Docker 下载站点${NC}"
        echo -e "${YELLOW}请检查网络连接或使用代理${NC}"
        return 1
    fi
    
    # 检查是否能访问 GitHub
    if ! curl -s --connect-timeout 5 "https://github.com" > /dev/null; then
        echo -e "${RED}错误：无法访问 GitHub${NC}"
        echo -e "${YELLOW}请检查网络连接或使用代理${NC}"
        return 1
    fi
    
    echo -e "✓ 网络连接正常"
    return 0
}

# 下载文件并检查完整性
download_file() {
    local url=$1
    local output_file=$2
    local retry_count=3
    
    for i in $(seq 1 $retry_count); do
        echo -e "${BLUE}下载中... (尝试 $i/$retry_count)${NC}"
        
        if curl -L -o "$output_file" -C - "$url"; then
            # 检查文件是否下载完整
            if [[ -s "$output_file" ]]; then
                echo -e "✓ 下载完成: $(basename "$output_file") ($(du -h "$output_file" | cut -f1))"
                return 0
            else
                echo -e "${YELLOW}文件大小为0，重新下载...${NC}"
                rm -f "$output_file"
            fi
        else
            echo -e "${YELLOW}下载失败，重试...${NC}"
        fi
        
        if [[ $i -lt $retry_count ]]; then
            sleep 2
        fi
    done
    
    echo -e "${RED}错误：下载失败: $(basename "$output_file")${NC}"
    return 1
}

# 生成 docker_install.sh 安装脚本
generate_install_script() {
    local arch=$1
    local docker_version=$2
    local compose_version=$3
    local docker_file=$4
    local compose_file=$5
    
    cat > docker_install.sh << 'EOF'
#!/bin/bash

# Docker 和 docker-compose 离线安装脚本
# 该脚本应与以下文件在同一目录：
# - docker-<version>.tgz
# - docker-compose-linux-<arch>

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 默认参数
ARCH=""
DOCKER_VERSION=""
COMPOSE_VERSION=""
SHOW_HELP=false

# 显示帮助信息
show_help() {
    echo -e "${GREEN}Docker 离线安装脚本${NC}"
    echo -e ""
    echo -e "${GREEN}使用方法：${NC}"
    echo -e "  $0 [-a <arch>] [-d <docker-version>] [-c <compose-version>] [-h]"
    echo -e ""
    echo -e "${GREEN}参数说明：${NC}"
    echo -e "  -a <arch>      指定架构 (x86_64, aarch64, armv7l)"
    echo -e "                 如不指定，将自动检测系统架构"
    echo -e "  -d <version>   指定 Docker 版本"
    echo -e "  -c <version>   指定 docker-compose 版本"
    echo -e "  -h             显示帮助信息"
    echo -e ""
    echo -e "${GREEN}注意：${NC}"
    echo -e "  1. 请确保以下文件在脚本同目录："
    echo -e "     - docker-<version>.tgz"
    echo -e "     - docker-compose-linux-<arch>"
    echo -e "  2. 如果不指定参数，脚本将尝试自动检测"
}

# 解析命令行参数
while getopts "a:d:c:h" opt; do
    case $opt in
        a)
            ARCH="$OPTARG"
            ;;
        d)
            DOCKER_VERSION="$OPTARG"
            ;;
        c)
            COMPOSE_VERSION="$OPTARG"
            ;;
        h)
            SHOW_HELP=true
            ;;
        \?)
            echo -e "${RED}无效参数${NC}"
            show_help
            exit 1
            ;;
    esac
done

if $SHOW_HELP; then
    show_help
    exit 0
fi

# 检测系统架构
detect_arch() {
    local arch=$(uname -m)
    case $arch in
        x86_64|amd64)
            echo "x86_64"
            ;;
        aarch64|arm64)
            echo "aarch64"
            ;;
        armv7l|armv7)
            echo "armv7l"
            ;;
        *)
            echo "unknown"
            ;;
    esac
}

# 验证架构
validate_arch() {
    local arch=$1
    case $arch in
        x86_64|aarch64|armv7l)
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

# 自动检测文件
auto_detect_files() {
    local arch=$1
    
    # 查找 Docker 安装包
    local docker_files=(docker-*.tgz)
    if [[ ${#docker_files[@]} -gt 0 ]]; then
        DOCKER_FILE="${docker_files[0]}"
        # 从文件名提取版本号
        if [[ $DOCKER_FILE =~ docker-([0-9.]+)\.tgz ]]; then
            DOCKER_VERSION="${BASH_REMATCH[1]}"
        fi
    fi
    
    # 查找 docker-compose 文件
    local compose_patterns=(
        "docker-compose-*"
    )
    
    for pattern in "${compose_patterns[@]}"; do
        local files=($pattern)
        if [[ ${#files[@]} -gt 0 ]]; then
            COMPOSE_FILE="${files[0]}"
            # 从文件名提取版本号
            if [[ $COMPOSE_FILE =~ docker-compose-([0-9.]+)-linux- ]]; then
                COMPOSE_VERSION="${BASH_REMATCH[1]}"
            elif [[ $COMPOSE_FILE =~ docker-compose-linux- ]]; then
                COMPOSE_VERSION="unknown"
            fi
            break
        fi
    done
}

# 检查必要文件
check_required_files() {
    if [[ ! -f "$DOCKER_FILE" ]]; then
        echo -e "${RED}错误：未找到 Docker 安装包${NC}"
        echo -e "${YELLOW}请确保文件存在: $DOCKER_FILE${NC}"
        exit 1
    fi
    
    if [[ ! -f "$COMPOSE_FILE" ]]; then
        echo -e "${RED}错误：未找到 docker-compose 文件${NC}"
        echo -e "${YELLOW}请确保文件存在: $COMPOSE_FILE${NC}"
        exit 1
    fi
}

# 验证用户权限
check_permissions() {
    if [[ $EUID -ne 0 ]]; then
        echo -e "${RED}错误：该脚本需要以 root 用户运行${NC}"
        echo -e "${YELLOW}请使用 sudo 或切换到 root 用户${NC}"
        exit 1
    fi
}

# 检查是否已安装 Docker
check_existing_installation() {
    if command -v docker &> /dev/null; then
        echo -e "${YELLOW}检测到已安装 Docker，是否继续安装？${NC}"
        read -p "继续安装将覆盖现有版本 [y/N]: " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            echo -e "${YELLOW}安装已取消${NC}"
            exit 0
        fi
    fi
}

# 安装 Docker
install_docker() {
    echo -e "${YELLOW}开始安装 Docker...${NC}"
    
    # 停止正在运行的 Docker 服务
    if systemctl is-active --quiet docker 2>/dev/null; then
        echo -e "${BLUE}停止正在运行的 Docker 服务...${NC}"
        systemctl stop docker
    fi
    
    # 解压 Docker 安装包
    echo -e "${BLUE}解压 Docker 安装包...${NC}"
    tar -xzf "$DOCKER_FILE"
    
    # 检查解压后的文件
    if [[ ! -d "docker" ]]; then
        # 尝试查找解压出的目录或文件
        if ls -d */ 2>/dev/null | grep -q docker; then
            mv $(ls -d */ | grep docker | head -1) docker
        else
            echo -e "${RED}错误：Docker 安装包解压后未找到 docker 目录${NC}"
            exit 1
        fi
    fi
    
    # 检查是否存在必要的二进制文件
    if [[ ! -f "docker/dockerd" ]] && [[ ! -f "docker/docker" ]]; then
        echo -e "${RED}错误：Docker 安装包中未找到必要的二进制文件${NC}"
        exit 1
    fi
    
    # 复制 Docker 二进制文件
    echo -e "${BLUE}安装 Docker 二进制文件...${NC}"
    cp -f docker/* /usr/bin/ 2>/dev/null || true
    
    # 复制子目录中的文件
    if ls docker/*/ 2>/dev/null; then
        for dir in docker/*/; do
            cp -f "$dir"* /usr/bin/ 2>/dev/null || true
        done
    fi
    
    # 创建 Docker 配置目录
    mkdir -p /etc/docker
    
    # 创建 Docker 服务文件
    echo -e "${BLUE}配置 Docker 服务...${NC}"
    cat > /etc/systemd/system/docker.service << DOCKER_SERVICE_EOF
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com
After=network-online.target firewalld.service
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd
ExecReload=/bin/kill -s HUP \$MAINPID
TimeoutSec=0
RestartSec=2
Restart=always
StartLimitBurst=3
StartLimitInterval=60s
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
DOCKER_SERVICE_EOF

    # 创建 docker 用户组
    if ! getent group docker > /dev/null; then
        groupadd docker
    fi
    
    # 清理临时文件
    echo -e "${BLUE}清理临时文件...${NC}"
    rm -rf docker
    
    echo -e "${GREEN}✓ Docker 安装完成${NC}"
}

# 安装 docker-compose
install_docker_compose() {
    echo -e "${YELLOW}开始安装 docker-compose...${NC}"
    
    # 添加执行权限
    if [[ ! -x "$COMPOSE_FILE" ]]; then
        chmod +x "$COMPOSE_FILE"
    fi
    
    # 安装到系统目录
    cp -f "$COMPOSE_FILE" /usr/local/bin/docker-compose
    chmod +x /usr/local/bin/docker-compose
    
    # 创建符号链接
    if [[ ! -f /usr/bin/docker-compose ]]; then
        ln -sf /usr/local/bin/docker-compose /usr/bin/docker-compose 2>/dev/null || true
    fi
    
    echo -e "${GREEN}✓ docker-compose 安装完成${NC}"
}

# 配置和启动服务
configure_and_start_service() {
    echo -e "${YELLOW}配置 Docker 服务...${NC}"
    
    # 重新加载 systemd 配置
    echo -e "${BLUE}重新加载 systemd 配置...${NC}"
    systemctl daemon-reload
    
    # 启动 Docker 服务
    echo -e "${BLUE}启动 Docker 服务...${NC}"
    systemctl start docker
    
    # 设置开机自启
    echo -e "${BLUE}设置 Docker 开机自启...${NC}"
    systemctl enable docker
    
    echo -e "${GREEN}✓ Docker 服务配置完成${NC}"
}

# 验证安装
verify_installation() {
    echo -e "${YELLOW}验证安装...${NC}"
    
    local success=true
    
    # 检查 Docker 版本
    if docker --version 2>&1; then
        echo -e "${GREEN}✓ Docker 命令可用${NC}"
    else
        echo -e "${RED}✗ Docker 命令不可用${NC}"
        success=false
    fi
    
    # 检查 docker-compose 版本
    if docker-compose --version 2>&1; then
        echo -e "${GREEN}✓ docker-compose 命令可用${NC}"
    else
        echo -e "${RED}✗ docker-compose 命令不可用${NC}"
        success=false
    fi
    
    # 检查 Docker 服务状态
    if systemctl is-active --quiet docker; then
        echo -e "${GREEN}✓ Docker 服务正在运行${NC}"
    else
        echo -e "${RED}✗ Docker 服务未运行${NC}"
        success=false
    fi
    
    if $success; then
        return 0
    else
        return 1
    fi
}

# 显示安装完成信息
show_completion_info() {
    echo -e ""
    echo -e "${GREEN}=== 安装完成 ===${NC}"
    echo -e ""
    echo -e "${YELLOW}使用说明：${NC}"
    echo -e "1. 将用户添加到 docker 组（可选）："
    echo -e "   sudo usermod -aG docker \$USER"
    echo -e "   然后重新登录生效"
    echo -e ""
    echo -e "2. 测试命令："
    echo -e "   docker run hello-world"
    echo -e "   docker-compose --version"
    echo -e ""
    echo -e "3. 查看服务状态："
    echo -e "   systemctl status docker"
    echo -e ""
    echo -e "4. 卸载 Docker（如果需要）："
    echo -e "   systemctl stop docker"
    echo -e "   rm -f /usr/bin/docker*"
    echo -e "   rm -f /usr/local/bin/docker-compose"
    echo -e "   rm -f /etc/systemd/system/docker.service"
    echo -e "   rm -f /etc/systemd/system/docker.socket"
    echo -e ""
    echo -e "${GREEN}安装成功！${NC}"
}

# 主安装函数
main_install() {
    echo -e "${GREEN}=== Docker 离线安装 ===${NC}"
    
    # 检查权限
    check_permissions
    
    # 检查已安装的 Docker
    check_existing_installation
    
    # 自动检测架构
    if [[ -z "$ARCH" ]]; then
        echo -e "${YELLOW}正在自动检测系统架构...${NC}"
        ARCH=$(detect_arch)
        if [[ "$ARCH" == "unknown" ]]; then
            echo -e "${RED}无法检测系统架构${NC}"
            exit 1
        fi
        echo -e "检测到架构: $ARCH"
    else
        if ! validate_arch "$ARCH"; then
            echo -e "${RED}不支持的架构: $ARCH${NC}"
            exit 1
        fi
    fi
    
    # 自动检测文件
    auto_detect_files "$ARCH"
    
    # 检查文件
    check_required_files
    
    echo -e "${GREEN}安装配置：${NC}"
    echo -e "架构: $ARCH"
    echo -e "Docker 文件: $DOCKER_FILE"
    echo -e "docker-compose 文件: $COMPOSE_FILE"
    echo -e ""
    
    # 确认安装
    read -p "是否继续安装？ [Y/n]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Nn]$ ]]; then
        echo -e "${YELLOW}安装已取消${NC}"
        exit 0
    fi
    
    echo ""
    
    # 执行安装步骤
    install_docker
    echo ""
    
    install_docker_compose
    echo ""
    
    configure_and_start_service
    echo ""
    
    sleep 2  # 等待服务启动
    
    if verify_installation; then
        show_completion_info
    else
        echo -e "${RED}安装验证失败，请检查日志${NC}"
        exit 1
    fi
}

# 执行安装
main_install
EOF
    
    # 设置执行权限
    chmod +x docker_install.sh
    
    echo -e "${GREEN}✓ 已生成 docker_install.sh 安装脚本${NC}"
}

# 主函数
main() {
    echo -e "${GREEN}=== Docker 离线安装包下载工具 ===${NC}"
    
    # 检查网络连接
    if ! check_network; then
        exit 1
    fi
    
    # 处理架构
    if [[ -z "$ARCH" ]]; then
        echo -e "${YELLOW}未指定架构，正在自动检测...${NC}"
        ARCH=$(detect_arch)
        if [[ "$ARCH" == "unknown" ]]; then
            echo -e "${RED}无法自动检测系统架构${NC}"
            echo -e "${YELLOW}请使用 -a 参数手动指定架构：${NC}"
            echo -e "  x86_64    (Intel/AMD 64位)"
            echo -e "  aarch64   (ARM 64位)"
            echo -e "  armv7l    (ARM 32位)"
            exit 1
        fi
    else
        if ! validate_arch "$ARCH"; then
            echo -e "${RED}错误：不支持的架构: $ARCH${NC}"
            echo -e "${YELLOW}支持的架构：x86_64, aarch64, armv7l${NC}"
            exit 1
        fi
    fi
    
    echo -e "${GREEN}使用架构: $(get_arch_display_name "$ARCH")${NC}"
    
    # 处理 Docker 版本
    if [[ -z "$DOCKER_VERSION" ]]; then
        echo -e "${YELLOW}获取 Docker 最新版本信息...${NC}"
        DOCKER_VERSION=$(get_latest_docker_version "$ARCH")
        if [[ -z "$DOCKER_VERSION" ]]; then
            echo -e "${RED}错误：无法获取 Docker 版本信息${NC}"
            exit 1
        fi
        echo -e "将下载 Docker 最新版本: ${GREEN}$DOCKER_VERSION${NC}"
    else
        echo -e "${YELLOW}检查 Docker 版本 $DOCKER_VERSION 是否可用...${NC}"
        if ! get_docker_versions "$ARCH" | grep -q "^$DOCKER_VERSION$"; then
            echo -e "${RED}错误：Docker 版本 $DOCKER_VERSION 不可用${NC}"
            echo -e "${YELLOW}可用版本：${NC}"
            get_docker_versions "$ARCH" | head -10
            exit 1
        fi
        echo -e "将下载指定 Docker 版本: ${GREEN}$DOCKER_VERSION${NC}"
    fi
    
    # 处理 docker-compose 版本
    if [[ -z "$COMPOSE_VERSION" ]]; then
        echo -e "${YELLOW}获取 docker-compose 最新版本信息...${NC}"
        COMPOSE_VERSION=$(get_latest_compose_version)
        if [[ -z "$COMPOSE_VERSION" ]]; then
            echo -e "${RED}错误：无法获取 docker-compose 版本信息${NC}"
            exit 1
        fi
        echo -e "将下载 docker-compose 最新版本: ${GREEN}$COMPOSE_VERSION${NC}"
    else
        echo -e "${YELLOW}检查 docker-compose 版本 $COMPOSE_VERSION 是否可用...${NC}"
        if ! get_compose_versions | grep -q "^$COMPOSE_VERSION$"; then
            echo -e "${RED}错误：docker-compose 版本 $COMPOSE_VERSION 不可用${NC}"
            echo -e "${YELLOW}可用版本：${NC}"
            get_compose_versions | head -10
            exit 1
        fi
        echo -e "将下载指定 docker-compose 版本: ${GREEN}$COMPOSE_VERSION${NC}"
    fi
    
    # 创建下载目录
    echo -e "${YELLOW}创建下载目录: $DOWNLOAD_DIR${NC}"
    mkdir -p "$DOWNLOAD_DIR"
    
    # 获取绝对路径
    DOWNLOAD_DIR=$(realpath "$DOWNLOAD_DIR")
    cd "$DOWNLOAD_DIR"
    
    echo -e "${GREEN}下载目录: $DOWNLOAD_DIR${NC}"
    echo ""
    
    # 下载 Docker
    echo -e "${YELLOW}开始下载 Docker...${NC}"
    
    # 构建 Docker 下载 URL
    case $ARCH in
        x86_64)
            DOCKER_URL="https://download.docker.com/linux/static/stable/x86_64/docker-$DOCKER_VERSION.tgz"
            ;;
        aarch64)
            DOCKER_URL="https://download.docker.com/linux/static/stable/aarch64/docker-$DOCKER_VERSION.tgz"
            ;;
        armv7l)
            DOCKER_URL="https://download.docker.com/linux/static/stable/armhf/docker-$DOCKER_VERSION.tgz"
            ;;
    esac
    
    DOCKER_FILE="docker-$DOCKER_VERSION-$ARCH.tgz"
    
    if download_file "$DOCKER_URL" "$DOCKER_FILE"; then
        echo -e "${GREEN}✓ Docker 下载成功${NC}"
    else
        echo -e "${RED}Docker 下载失败${NC}"
        exit 1
    fi
    
    echo ""
    
    # 下载 docker-compose
    echo -e "${YELLOW}开始下载 docker-compose...${NC}"
    
    # 构建 docker-compose 下载 URL
    case $ARCH in
        x86_64)
            COMPOSE_URL="https://github.com/docker/compose/releases/download/v$COMPOSE_VERSION/docker-compose-linux-x86_64"
            COMPOSE_FILE="docker-compose-$COMPOSE_VERSION-linux-x86_64"
            ;;
        aarch64)
            COMPOSE_URL="https://github.com/docker/compose/releases/download/v$COMPOSE_VERSION/docker-compose-linux-aarch64"
            COMPOSE_FILE="docker-compose-$COMPOSE_VERSION-linux-aarch64"
            ;;
        armv7l)
            COMPOSE_URL="https://github.com/docker/compose/releases/download/v$COMPOSE_VERSION/docker-compose-linux-armv7l"
            COMPOSE_FILE="docker-compose-$COMPOSE_VERSION-linux-armv7l"
            ;;
    esac
    
    if download_file "$COMPOSE_URL" "$COMPOSE_FILE"; then
        echo -e "${GREEN}✓ docker-compose 下载成功${NC}"
    else
        echo -e "${RED}docker-compose 下载失败${NC}"
        exit 1
    fi
    
    # 添加执行权限
    chmod +x "$COMPOSE_FILE"
    
    echo ""
    
    # 重命名为安装脚本期望的格式
    # mv "$DOCKER_FILE" "docker-$DOCKER_VERSION.tgz"
    # mv "$COMPOSE_FILE" "docker-compose-linux-$ARCH"
    
    # 生成安装脚本
    generate_install_script "$ARCH" "$DOCKER_VERSION" "$COMPOSE_VERSION" "docker-$DOCKER_VERSION.tgz" "docker-compose-linux-$ARCH"
    
    # 创建安装说明文件
    cat > INSTALL.md << EOF
# Docker 离线安装说明

## 下载信息
- 架构: $ARCH ($(get_arch_display_name "$ARCH"))
- Docker 版本: $DOCKER_VERSION
- docker-compose 版本: $COMPOSE_VERSION
- 下载时间: $(date)

## 文件说明
1. \`docker-$DOCKER_VERSION.tgz\` - Docker 安装包
2. \`docker-compose-linux-$ARCH\` - docker-compose 二进制文件
3. \`docker_install.sh\` - 安装脚本

## 安装方法

### 步骤1：传输文件到目标服务器
将整个目录复制到目标服务器：
\`\`\`bash
# 从本机复制到远程服务器
scp -r "$DOWNLOAD_DIR" user@remote-server:/tmp/

# 或者使用其他传输方式（rsync, sftp等）
\`\`\`

### 步骤2：在目标服务器上安装
\`\`\`bash
# 进入下载目录
cd /tmp/docker-offline-packages

# 方法1：使用安装脚本（推荐）
sudo ./docker_install.sh

# 方法2：指定参数安装
sudo ./docker_install.sh -a $ARCH -d $DOCKER_VERSION -c $COMPOSE_VERSION
\`\`\`

### 步骤3：验证安装
\`\`\`bash
# 验证 Docker
docker --version
docker run hello-world

# 验证 docker-compose
docker-compose --version

# 查看服务状态
systemctl status docker
\`\`\`

## 注意事项
1. 安装脚本需要 root 权限运行
2. 如果已安装 Docker，安装脚本会提示覆盖
3. 安装完成后建议将用户添加到 docker 组
   \`\`\`bash
   sudo usermod -aG docker \$USER
   # 然后重新登录
   \`\`\`

## 卸载方法
如果需要卸载：
\`\`\`bash
# 停止服务
systemctl stop docker
systemctl disable docker

# 删除二进制文件
rm -f /usr/bin/docker*
rm -f /usr/local/bin/docker-compose

# 删除配置文件
rm -f /etc/systemd/system/docker.service
rm -f /etc/systemd/system/docker.socket

# 删除数据目录（谨慎操作）
# rm -rf /var/lib/docker
\`\`\`
EOF
    echo -e "${GREEN}=== 下载完成 ===${NC}"
    echo -e "文件保存在: $DOWNLOAD_DIR"
    echo ""
    
    # 显示文件列表
    echo -e "${YELLOW}文件列表：${NC}"
    ls -lh "docker-$DOCKER_VERSION.tgz" "docker-compose-linux-$ARCH" "docker_install.sh" 2>/dev/null || true
    echo ""
    
    # 显示摘要信息
    echo -e "${GREEN}摘要：${NC}"
    echo -e "架构: $(get_arch_display_name "$ARCH")"
    echo -e "Docker 版本: $DOCKER_VERSION"
    echo -e "docker-compose 版本: $COMPOSE_VERSION"
    echo -e "总大小: $(du -sh . | cut -f1)"
    echo ""
    
    # 显示下一步操作
    echo -e "1. ${GREEN}传输到目标服务器${NC}:"
    echo -e "   scp -r \"$DOWNLOAD_DIR\" user@target-server:/path/to/"
    echo ""
    echo -e "2. ${GREEN}在目标服务器上安装${NC}:"
    echo -e "   cd /path/to/docker-offline-packages"
    echo -e "   sudo ./docker_install.sh"
    echo ""
    echo -e "3. ${GREEN}查看详细安装说明${NC}:"
    echo -e "   cat INSTALL.md"
    echo ""
    echo -e "${GREEN}完成！安装脚本已生成。${NC}"
}

# 异常处理
trap 'echo -e "${RED}程序被中断${NC}"; exit 1' INT TERM

main "$@"