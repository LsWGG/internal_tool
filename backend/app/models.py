from pydantic import BaseModel, Field, model_validator


class Bounds(BaseModel):
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-85.05112878, le=85.05112878)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-85.05112878, le=85.05112878)

    @model_validator(mode="after")
    def validate_order(self):
        if self.west >= self.east or self.south >= self.north:
            raise ValueError("范围坐标顺序无效")
        return self


class TaskOptions(BaseModel):
    data_type: str = Field(default="satellite", pattern="^(satellite|dem)$")
    zoom_min: int = Field(default=1, ge=0, le=20)
    zoom_max: int = Field(default=17, ge=0, le=20)
    output_name: str = Field(default="", max_length=120)
    tile_source: str = Field(default="google_satellite", pattern="^(google_satellite|google_hybrid|bing_aerial|amap_satellite|amap_hybrid|amap_standard|osm_standard|opentopomap|aws_terrarium)$")
    max_workers: int = Field(default=8, ge=1, le=16)
    split_size: int = Field(default=2048, ge=256, le=8192)
    output_format: str = Field(default="png", pattern="^(png|tif)$")

    @model_validator(mode="after")
    def validate_zoom_range(self):
        if self.zoom_min > self.zoom_max:
            raise ValueError("起始层级不能大于结束层级")
        if self.data_type == "dem" and self.zoom_max > 15:
            raise ValueError("DEM 高程数据最高支持 Z15")
        if self.data_type == "dem" and self.output_format != "tif":
            raise ValueError("DEM 高程数据仅支持 GeoTIFF 输出")
        if self.data_type == "dem" and self.tile_source != "aws_terrarium":
            raise ValueError("DEM 高程数据当前仅支持 AWS Terrarium 数据源")
        if self.data_type == "satellite" and self.tile_source == "aws_terrarium":
            raise ValueError("AWS Terrarium 仅用于 DEM 高程数据")
        source_limits={"google_satellite":(0,20),"google_hybrid":(0,20),"bing_aerial":(1,20),"amap_satellite":(1,18),"amap_hybrid":(1,18),"amap_standard":(1,18),"osm_standard":(0,19),"opentopomap":(0,17),"aws_terrarium":(0,15)}
        source_min,source_max=source_limits[self.tile_source]
        if self.zoom_min < source_min or self.zoom_max > source_max:
            raise ValueError(f"所选数据源仅支持 Z{source_min}–Z{source_max}")
        return self


class CreateTask(BaseModel):
    bounds: Bounds
    geometry: dict | None = None
    options: TaskOptions = Field(default_factory=TaskOptions)


class BatchCreate(BaseModel):
    tasks: list[CreateTask] = Field(min_length=1, max_length=100)


class DockerCreateTask(BaseModel):
    arch: str = Field(pattern="^(x86_64|aarch64|armv7l)$")
    docker_version: str = Field(default="", pattern=r"^$|^[0-9]+(?:\.[0-9]+){1,3}$")
    compose_version: str = Field(default="", pattern=r"^$|^[0-9]+(?:\.[0-9]+){1,3}$")

    @model_validator(mode="after")
    def validate_docker_compose_compatibility(self):
        if self.docker_version:
            parts = tuple(int(part) for part in self.docker_version.split("."))
            if parts < (20, 10):
                raise ValueError("Docker Compose V2 要求 Docker Engine 20.10 或更高版本")
        if self.compose_version and int(self.compose_version.split(".")[0]) < 2:
            raise ValueError("当前离线包工具仅支持 Docker Compose V2")
        return self


class ESConnection(BaseModel):
    host: str = Field(default="http://127.0.0.1:9200", pattern=r"^https?://.+")
    username: str = ""
    password: str = ""
    verify_certs: bool = True


class ESExportTask(BaseModel):
    connection: ESConnection
    indexes: list[str] = Field(min_length=1, max_length=100)


class NebulaConnection(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=9669, ge=1, le=65535)
    username: str = "root"
    password: str = "nebula"


class NebulaExportTask(BaseModel):
    connection: NebulaConnection
    spaces: list[str] = Field(min_length=1, max_length=100)
