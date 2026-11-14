---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: fdb00e3af3e9d364d870c4a019017ca0_2ecfb237dfe111f189bc525400248c00
    ReservedCode1: XT2KoFjOOutEwzAqvg9+sX1q3aHmUgZ/RsMB4hGEaI874BYQ9OPc0AaR6Kjme23iz2z5IUCHrQHJY2lNQsH/zrKTl+JgPttvQLwhBdwzq9TnMSDMZm2Ab33i0QGki0C4QPe+JOdzytKDqXtOCWa7NAmgElJnP2vlcLriOCA498jhNmuYUxXRxsH9CTY=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: fdb00e3af3e9d364d870c4a019017ca0_2ecfb237dfe111f189bc525400248c00
    ReservedCode2: XT2KoFjOOutEwzAqvg9+sX1q3aHmUgZ/RsMB4hGEaI874BYQ9OPc0AaR6Kjme23iz2z5IUCHrQHJY2lNQsH/zrKTl+JgPttvQLwhBdwzq9TnMSDMZm2Ab33i0QGki0C4QPe+JOdzytKDqXtOCWa7NAmgElJnP2vlcLriOCA498jhNmuYUxXRxsH9CTY=
---

# 更新日志

版本号遵循语义化版本，格式参考 Keep a Changelog。

## [未发布]

- v1.3.0：高级设置页（高级截图选项与输出控制）

## [1.2.3] - 2026-11-12

### 新增

- 设置面板新增左侧页签导航：常规 / 保存与输出 / 快捷键 / 关于
- 新增控件：滑块开关 `ToggleSwitch`、水平滑块 `HSlider`、分段选择器 `SegBar`
- 删除截图时预览卡片播放弹出特效（回弹放大后收缩淡出）

### 变更

- 设置面板配色改为深灰底（`#313131` 系）+ 白色文字，按钮使用无衬线字体
- 布尔选项统一为滑块开关，按「截图行为 / 悬浮预览 / 保存位置 / 图片 / 全局快捷键 / 版本」分组，1px 分隔线
- 图片格式由下拉框改为分段选择器，压缩质量与停留时长改为滑块
- 悬浮窗三按钮改为纯图标并浮于截图内部，信息条与按钮背景半透明模糊
- 复制按钮点击后切换为打勾图标，三按钮具备悬停 / 按下 / 点击后反馈
- 应用图标与窗口图标统一为 ShareX 官方图标改配色版本（`assets/sharexflash.ico`，含 16–256px 七档），打包脚本以 `--add-data` 将 ico 一并打入，运行时窗口图标可正常加载

### 移除

- 删除截图不再弹出确认框（`ConfirmDialog` 保留但停用）

### 修复

- 修复设置项排版拥挤、分类不清
- 修复删除确认流程打断操作节奏

## [1.2.2] - 2026-11-12

### 新增

- 悬浮窗改纯 2D 风格：去背景模糊、投影与圆角，改为不透明纯色块 + 1px 实线分层
- 悬浮条固定于图片下沿，高 34px；按钮为纯图标（复制为两张纸样式，点击变打勾；删除为打叉）

### 变更

- 按钮与信息条叠加在截图内部，不占图片外空间
- 按钮补悬停（描边变白）与按下（反色白底黑图标）反馈

### 移除

- 移除 V6 版底部磨砂托盘与圆角裁剪

## [1.2.1] - 2026-11-09

### 修复

- 倒计时进度条时长偏差约 1.8 秒：改用 `time.monotonic()` 单调时钟计时
- 进度条闪断：改为 Canvas 独立图元绘制
- 误暂停：仅指针位于按钮上时暂停，移出恢复

## [1.2.0] - 2026-11-09

### 新增

- 悬浮窗 V6 版式：底部磨砂托盘内嵌截图，三按钮浮层叠放在图片内部
- 支持任意多键组合全局热键，修复 PrintScreen、Win、小键盘与媒体键无法录制

### 变更

- 项目由 QuickShot 更名为 Share X Flash，替换图标配色（主色 `#2B9BFF`）
- 配置目录由 `%APPDATA%\QuickShot` 迁移至 `%APPDATA%\ShareXFlash`，旧配置自动复制

## [1.1.0] - 2026-11-08

### 新增

- 右下角悬浮预览窗：打开图片 / 复制到剪贴板 / 删除图片
- 代码合成快门音效
- PNG / JPG / BMP / WEBP / GIF 多格式输出与质量控制
- 文件名模板（`{yyyy}{MM}{dd}{HH}{mm}{ss}` 等变量）

## [1.0.0] - 2026-11-08

### 新增

- 全屏 / 区域 / 当前窗口三种截图方式
- 自定义多键全局快捷键
- 自定义保存目录与自动复制到剪贴板
*（内容由AI生成，仅供参考）*
