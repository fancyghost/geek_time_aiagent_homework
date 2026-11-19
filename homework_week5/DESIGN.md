# DESIGN.md · Trace Observatory 设计说明

## 1. 轨迹图承载的信息

一次完整 Agent 对话被建模为 `trace = { meta, turns[], calls[] }`。图上**同时**编码以下维度：

| 维度 | 视觉通道 | 位置 |
|---|---|---|
| 总运行时长 | 指标条 hero 数字 + 轴末端播放头标签 | 顶部 / 轴右端 |
| 对话轮次 | 指标 + 轮次分隔带「轮次 n」+ 转录分组 | 顶部 / 图内 / 下方 |
| 每次调用开始时间 | 横轴位置（T+ 相对毫秒）+ 左栏「T+x.xxs」+ 抽屉时钟时刻 | 条的 x / 左栏 / 抽屉 |
| 持续时长 | 条宽 + 条上时长标签 + 抽屉进度条 | 条 / 抽屉 |
| 内容 / 摘要 | 左栏第三行摘要 + 抽屉「摘要/输入/输出/思维链/错误」全文 | 左栏 / 抽屉 |
| 调用类型 | 颜色 + 形状图标 + 填充纹理（三重编码） | 条 / 左栏图标 / 图例 |
| 状态 | 成功/错误/待决 chips + 错误红描边⚠ | 工具条 / 条 |
| 父子关系 | 树形缩进 + 肘形连线 + 抽屉「↑父」跳转链接 | 图内 / 抽屉 |
| 错误信息 | 红描边 + ⚠ + 抽屉错误块（code+msg） | 条 / 抽屉 |
| 模型 / 工具名 | 左栏第二行「· model/tool」+ 抽屉调用信息 | 左栏 / 抽屉 |
| Token / 耗时分布 | 指标 + 「各类型耗时占比」横向条形 + Token 四宫格 | 顶部 / 下方 |

时间先后由 `start_ms` 单调映射到 x 保证正确；重叠调用（并行/父子）通过行分离 + 连线表达，不互相遮挡。

## 2. 视觉编码与色彩系统

**类型三重编码（色盲可辨）**：颜色之外叠加形状与纹理，任一通道缺失仍可区分。

| 类型 | 色 token | 形状 | 纹理 |
|---|---|---|---|
| llm 推理 | `--c-llm` 青 | diamond | solid |
| tool 工具 | `--c-tool` 绿 | square | hatch |
| governance 治理 | `--c-gov` 琥珀 | shield | dot |
| approval 审批 | `--c-approval` 品红 | hand | stripe |
| retrieval 检索 | `--c-retrieval` 紫 | circle | cross |
| 错误 | `--danger` 红描边 | ⚠ | — |

**三主题令牌**（`[data-theme]` 切换 CSS 变量）：
- `observatory` 深空：冷黑蓝底 + 青/品红主导，霓虹 glow；
- `blueprint` 蓝图：纸白底 + 网格纸纹 + 墨蓝，multiply 氛围；
- `ember` 熔金：暖棕底 + 橙/奶油字，暖中性色。
每主题含主导族 + 2–3 全饱和 accent + 语义 success/warn/danger/info + 暖/冷中性（bg/surface/raised/overlay/scrim）。
渲染时 `getComputedStyle` 把变量解析为具体 hex 注入 SVG，保证 **SVG/PNG 导出与屏幕一致**。
滚动条、边框、glow、chips、滑杆均取自同一调色板（自定义 `::-webkit-scrollbar`）。

## 3. 几何与渲染实现（chart.js）

- 布局常量：`padLeft=232`（左栏）、`padRight=26`、`padTop=8`、`axisH=26`、`padBottom=16`、泳道标题行 `HEADER_H=22`。
- 行高 = `density` 滑杆（22–64px）；`heights[]` 区分标题行/调用行，累积 `ys[]` 得每行 y；`X(ms)=padLeft+ms*pxPerMs`，`pxPerMs=basePx*zoom`。
- 分组：`time` 树形缩进（depth）；`type`/`turn` 插入 `{header:true}` 泳道标题行（色带 + 左色条 + 标签）。
- 父子连线：肘形 polyline（父行中心 → 子行左缩进），`showConnectors` 开关。
- 播放头：独立 `<g data-playhead transform="translate(x,0)">`；`updatePlayhead()` **只改 transform + 切换 bar 的 dimmed/ghosted/reached/active 类**，不重建 SVG → 回放 60fps 无重排。
- 回放循环：单一 `requestAnimationFrame`，`dt` clamp 64ms × `speed`；到端自动暂停；重复播放不叠加 loop/listener。
- 导出：`serialize()` clone + 内联 `<style>` → SVG blob；PNG 经 `Image+canvas` 2x scale + 背景填充，`onerror` 降级 SVG。

## 4. 状态与交互（app.js）

- 单一 `state`（DEFAULTS + traceId/search/types/statuses/turnFilter/playing/playheadMs/selectedId/zoom/density/glow/…）。
- `loadTrace()` JSON 深克隆，**randomize 抖动不污染 `window.TRACES`**；`resetView` 恢复 DEFAULTS **并回到默认轨迹**。
- 过滤 = `matched()` 统一供 chart/stats/chips/latency/transcript，保证各处计数一致（「显示 x / y」）。
- 详情抽屉：点击条 / 抽屉父链接跳转；`Esc` 关闭；侧栏限高 flex 列以 `.sidebar>*{flex-shrink:0}` 保证展开抽屉不被压扁、整体滚动。
- 保存视图：localStorage `trace-observatory-views`，snapshot/apply；删除后 toast 带**撤销**（恢复插入原位）。
- 键盘：Space 播放/暂停、Esc 关抽屉、R 回起点；输入框聚焦时不劫持。
- `ResizeObserver(chartScroll)` 防抖重渲染，窗口变化自适应。

## 5. 数据模型与真实性校准

`call = { id, turn, type, name, model|tool, start_ms, duration_ms, status, parent_id, content{summary,input,output,reasoning,error}, tokens{...} }`。
`build_trace.py` 只读 `logs/observability.db`（`mode=ro`）取真实延迟分位数与模型名写入 `meta.calibration`
（实测 samples=122、p50≈1837.8ms、p95≈2929.0ms），示例调用的时长据此标定，模型/模板/工具名沿用仓库真实资产
（llama-local、deepseek-v4-flash、db_query@v1、search_orders/cancel_order/transfer、order:read 预检、人工审批、限流重试）。
**不写入任何密钥**；DB 不存在时退化内置常量。

## 6. 响应式与可访问性

- 断点 1180 / 980 / 620：双栏 → 窄侧栏 → 单栏（侧栏移底部）→ 统计 2 列、品牌副标题隐藏。
- 关键防溢出：网格轨道一律 `minmax(0,1fr)`（避免宽 SVG 撑破页面）；`.stage/.sidebar/.chart-scroll{min-width:0}`。
- 移动端图表最小宽 640px，在 `.chart-scroll` 内**横向内滚**（页面不溢出），左栏标签保持可读。
- 实测 390/820/1440 三档 `body.scrollWidth == clientWidth`，无横向溢出、无裁切、控件可达。
- `prefers-reduced-motion` 关闭动画/过渡；语义化 button/slider/combobox + aria-label；焦点可见。

## 7. 工程质量与已修复缺陷

- 零依赖、file:// 可运行；stdlib 生成数据与服务器；截图可复现（`_capture_shots.py` + `_diag.html`）。
- 实测修复：① 侧栏 flex 压扁抽屉 → `flex-shrink:0`；② 耗时占比条 inline span 高度失效 → `display:block`；
  ③ 单栏 `1fr` 被宽 SVG 撑破 → `minmax(0,1fr)`；④ reset 未回到默认轨迹 → 显式恢复 `TRACES[0]`。
- 所有 SVG/HTML 插值经 `esc()` 转义；数据为本地自产，无网络/用户输入注入面。