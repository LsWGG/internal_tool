const names={
  FileType:'文件格式',FileTypeExtension:'文件扩展名',MIMEType:'媒体类型',FileSize:'文件大小',ImageSize:'图像尺寸',Megapixels:'百万像素',
  Make:'设备品牌',Model:'设备型号',Artist:'作者',Copyright:'版权',ImageDescription:'图片描述',Software:'处理软件',
  DateTimeOriginal:'拍摄时间',CreateDate:'创建时间',ModifyDate:'修改时间',OffsetTime:'修改时间时区',OffsetTimeOriginal:'拍摄时间时区',OffsetTimeDigitized:'数字化时间时区',SubSecTimeOriginal:'拍摄时间小数秒',SubSecTime:'时间小数秒',SubSecTimeDigitized:'数字化时间小数秒',
  GPSLatitude:'纬度',GPSLongitude:'经度',GPSAltitude:'海拔',GPSLatitudeRef:'南北纬标识',GPSLongitudeRef:'东西经标识',GPSAltitudeRef:'海拔基准',GPSPosition:'地理坐标',GPSCoordinates:'位置坐标',GPSVersionID:'GPS 版本',GPSDateStamp:'GPS 日期',GPSTimeStamp:'GPS 时间',GPSDateTime:'GPS 日期时间',GPSSpeed:'移动速度',GPSSpeedRef:'速度单位',GPSImgDirection:'拍摄方向',GPSImgDirectionRef:'方向基准',GPSHPositioningError:'水平定位误差',GPSMapDatum:'坐标基准',GPSDestBearing:'目标方位角',GPSDestBearingRef:'目标方位基准',
  ImageWidth:'像素宽度',ImageHeight:'像素高度',ExifImageWidth:'EXIF 像素宽度',ExifImageHeight:'EXIF 像素高度',XResolution:'水平分辨率',YResolution:'垂直分辨率',ResolutionUnit:'分辨率单位',Orientation:'显示方向',Rotation:'旋转角度',
  ISO:'感光度',FNumber:'光圈值',Aperture:'光圈',ApertureValue:'光圈值（APEX）',ExposureTime:'曝光时间',ShutterSpeed:'快门速度',ShutterSpeedValue:'快门值（APEX）',ExposureProgram:'曝光程序',ExposureMode:'曝光模式',ExposureCompensation:'曝光补偿',BrightnessValue:'亮度值',MeteringMode:'测光模式',Flash:'闪光灯状态',FocalLength:'焦距',FocalLengthIn35mmFormat:'等效 35mm 焦距',LensModel:'镜头型号',LensMake:'镜头品牌',LensInfo:'镜头规格',WhiteBalance:'白平衡',SceneCaptureType:'场景类型',SceneType:'场景来源',SensingMethod:'感光方式',SubjectArea:'主体区域',DigitalZoomRatio:'数码变焦倍率',
  ColorSpace:'色彩空间',ColorType:'颜色类型',BitDepth:'位深',BitsPerSample:'通道位数',SamplesPerPixel:'像素通道数',Compression:'压缩方式',CompressionType:'压缩类型',Filter:'滤波方式',Interlace:'交错扫描',PhotometricInterpretation:'像素颜色解释',YCbCrPositioning:'色度采样位置',YCbCrSubSampling:'色度子采样',ComponentsConfiguration:'通道配置',
  ExifVersion:'EXIF 版本',FlashpixVersion:'Flashpix 版本',JFIFVersion:'JFIF 版本',XMPToolkit:'XMP 工具包',ProfileDescription:'色彩配置描述',ProfileCopyright:'色彩配置版权',ProfileVersion:'色彩配置版本',ProfileClass:'色彩配置类别',ColorSpaceData:'色彩模型',ProfileConnectionSpace:'配置连接空间',ProfileDateTime:'色彩配置时间',ProfileFileSignature:'配置文件签名',PrimaryPlatform:'主要平台',RenderingIntent:'色彩渲染意图',
  MajorBrand:'主要容器品牌',MinorVersion:'容器次版本',CompatibleBrands:'兼容容器品牌',HandlerType:'处理器类型',HandlerVendorID:'处理器厂商',HandlerDescription:'处理器描述',PrimaryItemReference:'主图像引用',MediaDataSize:'媒体数据大小',MediaDataOffset:'媒体数据偏移',MetaImageSize:'容器图像尺寸',ImageSpatialExtent:'图像空间尺寸',
  Title:'标题',Description:'描述',Subject:'关键词',Keywords:'关键词',Creator:'创作者',Author:'作者',Rating:'评分',Label:'标签',UserComment:'用户备注',Comment:'备注',DocumentName:'文档名称',BodySerialNumber:'机身序列号',LensSerialNumber:'镜头序列号',OwnerName:'所有者',CameraOwnerName:'相机所有者',ImageUniqueID:'图片唯一标识',
  StripOffsets:'图像条带偏移',StripByteCounts:'图像条带字节数',RowsPerStrip:'每条带行数',PlanarConfiguration:'通道存储布局',TileOffsets:'图像分块偏移',TileByteCounts:'图像分块字节数',ThumbnailImage:'缩略图二进制数据',PreviewImage:'预览图二进制数据',ThumbnailOffset:'缩略图偏移',ThumbnailLength:'缩略图长度',ExifByteOrder:'EXIF 字节顺序',
}
const explanations={
  XResolution:'水平方向的打印密度，不改变图片像素宽度；单位由分辨率单位字段指定。',YResolution:'垂直方向的打印密度，不改变图片像素高度；单位由分辨率单位字段指定。',ResolutionUnit:'1 = 无单位，2 = 每英寸（DPI），3 = 每厘米。',
  Orientation:'EXIF 显示方向：1 正常，3 旋转 180°，6 顺时针 90°，8 逆时针 90°；2、4、5、7 为镜像方向。',
  GPSLatitude:'纬度角度；GPS 分组中通常为绝对值，南北方向由南北纬标识指定。建议使用常用信息中的 GPS 编辑器。',GPSLongitude:'经度角度；GPS 分组中通常为绝对值，东西方向由东西经标识指定。建议使用 GPS 编辑器。',GPSAltitude:'相对海平面的高度（米）；GPS 原始字段通常为绝对值，正负由海拔基准决定。',GPSLatitudeRef:'N = 北纬，S = 南纬。',GPSLongitudeRef:'E = 东经，W = 西经。',GPSAltitudeRef:'0 = 海平面以上，1 = 海平面以下。',
  DateTimeOriginal:'拍摄时的本地时间，格式 YYYY:MM:DD HH:mm:ss；时区由拍摄时间时区字段指定。',CreateDate:'图像创建或数字化时间；EXIF 通常使用 YYYY:MM:DD HH:mm:ss 格式。',ModifyDate:'图像记录的修改时间，不等同于操作系统的文件修改时间。',OffsetTimeOriginal:'拍摄时的 UTC 偏移，例如北京时间 +08:00；不会自动改写拍摄时间。',
  ExposureTime:'快门开放时长，单位秒。原始数值可填写 0.008（即 1/125 秒）。',FNumber:'镜头光圈的 f 值，例如 1.8、2.8、5.6。',ISO:'拍摄时使用的感光度，例如 100、400、1600。',FocalLength:'镜头实际焦距，单位毫米，不是等效焦距。',ColorSpace:'EXIF 中 1 通常表示 sRGB，65535 表示未校准；色彩配置可能另存在 ICC 信息中。',
  ImageWidth:'图像实际像素宽度，属于结构信息；不能靠改写元数据实现图片缩放。',ImageHeight:'图像实际像素高度，属于结构信息；不能靠改写元数据实现图片缩放。',Rating:'图片评分；通常 0 为未评分，1～5 为星级，具体取值由格式决定。',
}
const groups={File:'文件基本信息',IFD0:'图像主目录',IFD1:'缩略图目录',ExifIFD:'拍摄信息',GPS:'地理位置',Composite:'计算结果',PNG:'PNG 图像结构',JFIF:'JPEG 容器',QuickTime:'HEIC / 媒体容器',ICC_Profile:'色彩配置',IPTC:'新闻与版权信息'}
export function fieldInfo(name,group,writable=false){
  const label=names[name]||'扩展字段'
  const area=groups[group]||(group.startsWith('XMP-')?'XMP 描述信息':'厂商或格式扩展信息')
  const description=explanations[name]||(names[name]?`${label}，属于${area}。`:`${area}中的扩展字段 ${name}，暂无准确的中文释义；保留原始字段名以便核对。`)
  return {label,description:description+(writable?' 按原始字段值编辑，保存时会进行回读校验。':' 此项只读展示，不直接改写。')}
}
