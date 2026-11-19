# Trace Observatory · Agent 对话轨迹可视化

> 第五周作业：把一次完整的 Agent 对话渲染成可交互的**调用瀑布甘特图**，
> 一眼看清「总时长 / 轮次 / 每次调用的开始时间 / 持续时长 / 内容」，
> 并支持类型泳道、父子关系、错误定位、回放与多主题分析。

纯前端实现（原生 JS + SVG），**零第三方依赖**；示例数据由 `build_trace.py`（仅标准库）生成，
其延迟分位数校准自仓库真实观测库 `logs/observability.db`（只读）。**不含任何 API Key / Token。**

---

## 1. 快速运行

任选其一：

**方式 A · 双击打开（无需服务器）**
```
直接双击 index.html
```
数据通过 `<script src="data/traces.js">` 注入 `window.TRACES`，`file://` 协议下即可完整运行。

**方式 B · 本地服务器（推荐，便于导出/截图）**
```powershell
python serve.py --port 8770 --no-open
# 浏览器打开 http://127.0.0.1:8770/index.html
```
`serve.py` 仅用标准库 `http.server`，无缓存头，改动即刷新。

> 依赖：浏览器（Chrome / Edge 任一现代版本）。Python 仅在使用 `serve.py` / `build_trace.py` 时需要（3.8+，标准库即可）。

## 2. 重新生成示例数据（可选）

```powershell
python build_trace.py
```
- 只读打开 `../logs/observability.db`（`sqlite3` URI `mode=ro`），统计真实延迟分位数与模型名写入 `meta.calibration`；
- 生成 3 条轨迹 → `data/traces.js`（`window.TRACES`）与 `data/traces.json`（同源 JSON）。
- 若数据库不存在，自动退化为内置校准常量，仍可运行。

## 3. 目录结构

```
homework_week5/
├── index.html            页面结构（顶栏 / 指标 / 工具条 / 回放 / 图表 / 转录 / 侧栏控制 / 抽屉）
├── assets/
│   ├── app.css           三主题色彩令牌、自定义滚动条、响应式、reduced-motion
│   ├── chart.js          SVG 瀑布甘特渲染器 + 播放头 + SVG/PNG 导出
│   └── app.js            状态 / 控制 / 回放 / 搜索过滤 / 抽屉 / 保存视图 / 撤销
├── data/
│   ├── traces.js         示例数据（window.TRACES，供 file:// 直接加载）
│   └── traces.json       同源 JSON（便于程序化消费）
├── build_trace.py        示例数据生成 + 真实 DB 只读校准
├── serve.py              标准库静态服务器
├── screenshots/          运行截图（01–09 为交付图）
├── _diag.html            [开发工具] 同源 iframe 视口框，用于复现截图
└── _capture_shots.py     [开发工具]  headless Chrome 批量复现 screenshots/01–09
```

## 4. 示例对话（3 条）

| 轨迹 | 轮次 | 调用 | 错误 | 总时长 | 看点 |
|---|---|---|---|---|---|
| 订单查询 → 取消 → 转账 | 3 | 12 | 1 | 14.96s | 多轮、父子调用、人工审批、限流重试 |
| RAG 知识问答 · 混合检索 | 1 | 5 | 0 | 3.77s | 检索召回 + 生成带引用回答 |
| 风控拦截 · 转账被拒 | 1 | 4 | 2 | 7.76s | 治理预检拒绝、错误链路 |

## 5. 使用说明

**读图**
- 横轴 = 相对开始时间（T+），条宽 = 持续时长；左栏 = 类型图标 + 调用名 + 「T+开始 · 时长 · 模型/工具」+ 内容摘要。
- 缩进 / 肘形连线 = 父子调用层级；轮次分隔带标注「轮次 n」。
- 错误调用 = 红描边 + ⚠ 标记；点击任意条打开**详情抽屉**（时间四宫格、进度条、工具/轮次、父链接、摘要/输入/输出/思维链/错误）。

**常驻控制区（侧栏，实时生效）**
- 滑杆：时间缩放、行高密度、发光强度；回放速度在运输条。
- 选择：分组方式（时间树 / 类型泳道 / 按轮次）、排序依据（开始 / 时长降序 / 名称）、配色主题（Observatory 深空 / Blueprint 蓝图 / Ember 熔金）。
- 开关：网格、父子连线、内容摘要、回放幽灵态。
- 按钮：重置视图、随机轨迹、保存视图（localStorage，删除可**撤销**）。

**回放**：播放 / 暂停、回到起点、拖动进度条或播放头；未到达的调用呈幽灵态，当前调用脉冲高亮。
**过滤**：搜索框（名称/模型/工具/内容）+ 类型 chips + 状态 chips + 点击转录轮次过滤；右上显示「显示 x / y 次调用」。
**导出**：顶栏 JSON / SVG / PNG（PNG 为 2x 带背景位图）。
**键盘**：`Space` 播放/暂停 · `Esc` 关闭抽屉 · `R` 回到起点。

## 6. 运行截图

| 文件 | 内容 |
|---|---|
| 01-desktop-default.png | 桌面默认全景（指标 + 瀑布 + 转录 + 耗时占比 + 控制区） |
| 02-viewport-top.png | 桌面首屏 |
| 03-detail-drawer.png | 点击调用后的详情抽屉 |
| 04-type-lanes.png | 调用类型泳道分组 + 耗时占比 |
| 05-ember-theme.png | Ember · 熔金主题 |
| 06-rag-trace.png | RAG 轨迹（检索召回类型） |
| 07-mobile-390.png / 07b-mobile-390-tall.png | 移动 390px 首屏 / 长页 |
| 08-tablet-820.png | 平板 820px |
| 09-blueprint-theme.png | Blueprint · 蓝图主题 |

复现截图（可选，需 Chrome + 已起服务）：`python _capture_shots.py`。

## 7. 设计说明

见 [DESIGN.md](DESIGN.md)：轨迹图承载的信息、视觉编码、几何与回放实现、色彩系统、数据模型与校准、响应式与可访问性。