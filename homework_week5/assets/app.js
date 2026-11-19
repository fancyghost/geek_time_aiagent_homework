/* ===========================================================================
   app.js · Trace Observatory 应用逻辑
   状态管理 / 实时控制 / 搜索·过滤·排序·分组 / 指标 / 转录 / 耗时分布 /
   详情抽屉 / 平滑回放 / 保存视图(含撤销) / 导出 JSON·SVG·PNG / 主题切换。
   =========================================================================== */
(function () {
  "use strict";
  const TRACES = window.TRACES || [];
  const WC = window.WaterfallChart;
  const $ = (id) => document.getElementById(id);
  const svg = $("waterfall");
  const TYPES = WC.TYPES, ORDER = WC.ORDER;
  const STATUS = { ok: { label: "成功", color: "var(--ok)" }, error: { label: "错误", color: "var(--danger)" }, pending: { label: "待决", color: "var(--warn)" } };
  const esc = (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  const fmtS = WC.fmtS, fmtMs = WC.fmtMs;
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

  if (!TRACES.length) { document.body.innerHTML = "<p style='padding:40px'>未找到轨迹数据，请先运行 python build_trace.py</p>"; return; }

  // ---------- 状态 ----------
  const DEFAULTS = { group: "time", sort: "start", zoom: 1, density: 40, glow: 55, showGrid: true, showConnectors: true, showLabels: true, ghost: true, theme: "observatory", speed: 1 };
  const state = Object.assign({
    traceId: TRACES[0].id, search: "", types: new Set(ORDER), statuses: new Set(Object.keys(STATUS)),
    turnFilter: null, playing: false, playheadMs: 0, selectedId: null,
  }, DEFAULTS);

  let trace = null, byId = new Map(), rows = [], dimmedIds = new Set(), palette = null, containerW = 900;
  let raf = null, lastT = 0, lastDeleted = null;

  // ---------- 工具 ----------
  function readPalette() {
    const cs = getComputedStyle(document.documentElement), g = (n) => cs.getPropertyValue(n).trim();
    palette = { llm: g("--c-llm"), tool: g("--c-tool"), gov: g("--c-gov"), approval: g("--c-approval"), retrieval: g("--c-retrieval"), error: g("--c-error"), text: g("--text"), warm: g("--warm"), muted: g("--muted"), dim: g("--dim"), line: g("--line"), lineSoft: g("--line-soft"), raised: g("--raised"), surface: g("--surface"), bg: g("--bg"), void: g("--void"), accent: g("--accent"), accent2: g("--accent-2"), barText: g("--bar-text") };
  }
  function loadTrace(id) {
    const src = TRACES.find((t) => t.id === id) || TRACES[0];
    trace = JSON.parse(JSON.stringify(src)); // 克隆，随机抖动不污染源数据
    byId = new Map(trace.calls.map((c) => [c.id, c]));
    state.traceId = trace.id; state.playheadMs = trace.total_ms; state.selectedId = null; state.turnFilter = null;
    pause();
  }
  function depthOf(c) { let d = 0, p = c.parent_id; while (p && byId.has(p)) { d++; p = byId.get(p).parent_id; } return d; }
  function matches(c) {
    if (!state.types.has(c.type) || !state.statuses.has(c.status)) return false;
    if (state.turnFilter != null && c.turn !== state.turnFilter) return false;
    if (state.search) {
      const q = state.search.toLowerCase();
      const hay = [c.name, c.model, c.tool, c.template_ref, c.summary, c.error_code, JSON.stringify(c.content)].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  }
  function matched() { return trace.calls.filter(matches); }
  function wall(ms) { const d = new Date(new Date(trace.started_at).getTime() + ms); return d.toTimeString().slice(0, 8) + "." + String(d.getMilliseconds()).padStart(3, "0"); }

  function computeRows() {
    const all = trace.calls.slice();
    dimmedIds = new Set(all.filter((c) => !matches(c)).map((c) => c.id));
    const out = [];
    if (state.group === "type") {
      ORDER.forEach((t) => {
        const lane = all.filter((c) => c.type === t).sort((a, b) => a.start_ms - b.start_ms);
        if (!lane.length) return;
        out.push({ header: true, label: TYPES[t].label + " · " + lane.length, color: palette[TYPES[t].key] });
        lane.forEach((c) => out.push({ call: c, depth: 0 }));
      });
    } else if (state.group === "turn") {
      trace.turns.forEach((tn) => {
        const lane = all.filter((c) => c.turn === tn.index).sort((a, b) => a.start_ms - b.start_ms);
        if (!lane.length) return;
        out.push({ header: true, label: "轮次 " + tn.index + " · " + lane.length + " 调用", color: palette.accent });
        lane.forEach((c) => out.push({ call: c, depth: 0 }));
      });
    } else {
      const ordered = all.slice();
      if (state.sort === "duration") ordered.sort((a, b) => b.duration_ms - a.duration_ms);
      else if (state.sort === "name") ordered.sort((a, b) => a.name.localeCompare(b.name, "zh"));
      else ordered.sort((a, b) => a.start_ms - b.start_ms || depthOf(a) - depthOf(b));
      ordered.forEach((c) => out.push({ call: c, depth: depthOf(c) }));
    }
    rows = out;
  }

  // ---------- 渲染：图表 ----------
  function activeIdNow() {
    const ms = state.playheadMs;
    const a = trace.calls.find((c) => matches(c) && c.start_ms <= ms && ms < c.start_ms + c.duration_ms);
    return a ? a.id : null;
  }
  function renderChart() {
    computeRows();
    containerW = Math.max($("chartScroll").clientWidth - 12, 640);
    WC.render(svg, {
      rows, totalMs: trace.total_ms, palette, state, turns: trace.turns, byId,
      dimmedIds, selectedId: state.selectedId, activeId: activeIdNow(),
      playheadMs: state.playheadMs, containerW,
      onSelect: selectCall, onHover: showTip, onLeave: hideTip, onScrub: scrubTo,
    });
    syncPlayhead();
    $("matchCount").textContent = `显示 ${matched().length} / ${trace.calls.length} 次调用`;
    $("chartHint").textContent = state.group === "time" ? "点击调用查看详情 · 拖动播放头回放" : (state.group === "type" ? "按调用类型泳道分组" : "按对话轮次泳道分组");
  }
  function syncPlayhead() {
    const active = WC.updatePlayhead(svg, state.playheadMs, { byId, dimmedIds, ghost: state.ghost });
    $("scrub").value = Math.round(state.playheadMs / trace.total_ms * 1000);
    $("elapsedNow").textContent = "T+" + fmtS(state.playheadMs);
    $("elapsedTotal").textContent = fmtS(trace.total_ms);
    $("activeCall").innerHTML = active ? `进行中：<b>${esc(active.name)}</b>` : (state.playheadMs >= trace.total_ms - 1 ? "回放完成 · 全部调用已展示" : "就绪");
  }

  // ---------- 渲染：指标 ----------
  function renderStats() {
    const m = matched(), all = trace.calls;
    const tok = m.reduce((a, c) => ({ i: a.i + (c.tokens?.input || 0), o: a.o + (c.tokens?.output || 0) }), { i: 0, o: 0 });
    const errs = m.filter((c) => c.status === "error").length;
    const avg = m.length ? m.reduce((a, c) => a + c.duration_ms, 0) / m.length : 0;
    const slow = m.slice().sort((a, b) => b.duration_ms - a.duration_ms)[0];
    const llmN = m.filter((c) => c.type === "llm").length, toolN = m.filter((c) => c.type === "tool").length;
    const tiles = [
      { hero: true, label: "总运行时长", value: fmtS(trace.total_ms), sub: `端到端 · ${trace.started_at.slice(11, 19)} 起`, color: "var(--accent)" },
      { label: "对话轮次", value: trace.turn_count, sub: "user ↔ assistant", color: "var(--c-retrieval)" },
      { label: "调用次数", value: m.length, sub: `LLM ${llmN} · 工具 ${toolN}`, color: "var(--c-llm)" },
      { label: "错误", value: errs, sub: errs ? "含失败/拒绝" : "全部成功", color: errs ? "var(--danger)" : "var(--ok)" },
      { label: "Token 入/出", value: (tok.i + tok.o).toLocaleString(), sub: `入 ${tok.i} · 出 ${tok.o}`, color: "var(--c-gov)" },
      { label: "平均调用时长", value: fmtMs(avg), sub: `共 ${m.length} 次`, color: "var(--c-tool)" },
      { label: "最慢调用", value: slow ? fmtMs(slow.duration_ms) : "—", sub: slow ? slow.name : "无", color: "var(--c-approval)" },
    ];
    $("stats").innerHTML = tiles.map((t, i) => `<div class="stat${t.hero ? " hero" : ""}" style="--sc:${t.color};animation-delay:${i * 45}ms">
      <div class="s-label">${esc(t.label)}</div><div class="s-value">${t.value}</div><div class="s-sub" title="${esc(t.sub)}">${esc(t.sub)}</div></div>`).join("");
  }

  // ---------- 渲染：过滤 chips ----------
  function renderChips() {
    $("typeChips").innerHTML = ORDER.map((t) => {
      const n = trace.calls.filter((c) => c.type === t).length; if (!n) return "";
      const on = state.types.has(t);
      return `<button class="chip ${on ? "on" : "off"}" data-type="${t}" style="--cc:${palette[TYPES[t].key]}" title="${TYPES[t].label}（${n}）"><span class="dot"></span>${TYPES[t].label} ${n}</button>`;
    }).join("");
    $("statusChips").innerHTML = Object.keys(STATUS).map((s) => {
      const n = trace.calls.filter((c) => c.status === s).length; if (!n) return "";
      const on = state.statuses.has(s);
      return `<button class="chip ${on ? "on" : "off"}" data-status="${s}" style="--cc:${STATUS[s].color}"><span class="dot"></span>${STATUS[s].label} ${n}</button>`;
    }).join("");
  }

  // ---------- 渲染：图例 ----------
  function renderLegend() {
    const pat = { solid: "", hatch: "hatch", dot: "dot", stripe: "stripe", cross: "hatch" };
    $("legend").innerHTML = ORDER.map((t) => `<span class="legend-item"><span class="legend-swatch ${pat[TYPES[t].pattern]}" style="--cc:${palette[TYPES[t].key]}"></span>${TYPES[t].label}</span>`).join("")
      + `<span class="legend-item"><span class="legend-swatch" style="--cc:var(--danger)"></span>错误/拒绝</span>`
      + `<span class="legend-item" style="margin-left:auto;color:var(--dim)">条宽=时长 · 横轴=开始时间 · 缩进=父子层级</span>`;
  }

  // ---------- 渲染：转录 ----------
  function renderTranscript() {
    $("turnCountLabel").textContent = `${trace.turn_count} 轮 · ${trace.agent}`;
    $("transcript").innerHTML = trace.turns.map((tn) => {
      const n = trace.calls.filter((c) => c.turn === tn.index).length;
      const act = state.turnFilter === tn.index;
      return `<div class="turn${act ? " active" : ""}" data-turn="${tn.index}">
        <div class="turn-head"><span class="turn-idx">轮次 ${tn.index}</span><span>T+${fmtS(tn.start_ms)}</span><span>${n} 次调用</span>${act ? '<span style="color:var(--accent)">· 已过滤</span>' : ""}</div>
        <div class="bubble user"><span class="who">用户</span>${esc(tn.user)}</div>
        <div class="bubble assistant"><span class="who">助手</span>${esc(tn.assistant)}</div></div>`;
    }).join("");
  }

  // ---------- 渲染：耗时分布 + Token ----------
  function renderLatency() {
    const m = matched();
    const agg = ORDER.map((t) => ({ t, ms: m.filter((c) => c.type === t).reduce((a, c) => a + c.duration_ms, 0), n: m.filter((c) => c.type === t).length })).filter((x) => x.n);
    const total = agg.reduce((a, x) => a + x.ms, 0) || 1;
    const max = Math.max(...agg.map((x) => x.ms), 1);
    const pat = { solid: "", hatch: "hatch", dot: "hatch", stripe: "hatch", cross: "hatch" };
    $("latencyChart").innerHTML = agg.map((x) => `<div class="lat-row" style="--cc:${palette[TYPES[x.t].key]}">
      <span class="lat-name"><span class="dot"></span>${TYPES[x.t].label}</span>
      <span class="lat-track"><span class="lat-fill ${pat[TYPES[x.t].pattern]}" style="width:${(x.ms / max * 100).toFixed(1)}%"></span></span>
      <span class="lat-val">${fmtMs(x.ms)}<small style="color:var(--dim);font-weight:500"> ${(x.ms / total * 100).toFixed(0)}%</small></span></div>`).join("")
      || `<p style="color:var(--dim);font-size:12px">无匹配调用</p>`;
    const tk = m.reduce((a, c) => ({ input: a.input + (c.tokens?.input || 0), output: a.output + (c.tokens?.output || 0), cached: a.cached + (c.tokens?.cached || 0), reasoning: a.reasoning + (c.tokens?.reasoning || 0) }), { input: 0, output: 0, cached: 0, reasoning: 0 });
    $("tokenSummary").innerHTML = [["输入", tk.input], ["输出", tk.output], ["缓存", tk.cached], ["思维链", tk.reasoning]].map(([l, v]) => `<div class="tok"><div class="t-l">${l}</div><div class="t-v">${v.toLocaleString()}</div></div>`).join("");
  }

  // ---------- 详情抽屉 ----------
  function selectCall(id) {
    state.selectedId = (state.selectedId === id) ? null : id;
    renderChart(); renderDrawer();
    if (state.selectedId) { const el = svg.querySelector(`g.wf-bar[data-id="${id}"]`); if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" }); }
  }
  function contentBlock(k, v) {
    if (v == null) return "";
    const txt = typeof v === "string" ? v : JSON.stringify(v, null, 2);
    return `<div class="dw-block"><div class="bk">${esc(k)}</div><div class="dw-content">${esc(txt)}</div></div>`;
  }
  function renderDrawer() {
    const c = state.selectedId ? byId.get(state.selectedId) : null;
    const empty = $("drawerEmpty"), body = $("drawerBody");
    if (!c) { empty.hidden = false; body.hidden = true; body.innerHTML = ""; return; }
    empty.hidden = true; body.hidden = false;
    const ty = TYPES[c.type] || TYPES.llm, col = palette[ty.key];
    const st = STATUS[c.status] || STATUS.ok;
    const kids = trace.calls.filter((x) => x.parent_id === c.id);
    const parent = c.parent_id ? byId.get(c.parent_id) : null;
    const ttft = c.ttft_ms || 0, gen = Math.max(0, c.duration_ms - ttft);
    const tok = c.tokens || {};
    const tokTotal = (tok.input || 0) + (tok.output || 0) || 1;
    const timingBar = c.type === "llm" && ttft
      ? `<div class="timing-bar"><span class="tb-ttft" style="width:${ttft / c.duration_ms * 100}%"></span><span class="tb-gen" style="width:${gen / c.duration_ms * 100}%"></span></div>
         <div class="timing-legend"><span><i style="background:var(--warn)"></i>首Token ${fmtMs(ttft)}</span><span><i style="background:var(--c-llm)"></i>生成 ${fmtMs(gen)}</span></div>`
      : `<div class="timing-bar"><span class="tb-gen" style="width:100%;background:${col}"></span></div><div class="timing-legend"><span><i style="background:${col}"></i>执行 ${fmtMs(c.duration_ms)}</span></div>`;
    body.innerHTML = `
      <div class="dw-head" style="--cc:${col}">
        <button class="dw-close" id="dwClose" title="关闭">×</button>
        <span class="dw-type">${esc(ty.label)}</span>
        <div class="dw-title">${esc(c.name)}</div>
        <span class="dw-status ${c.status}">● ${st.label}${c.retries ? ` · 重试 ${c.retries} 次` : ""}</span>
      </div>
      <div class="dw-section"><h3>时间</h3>
        <div class="dw-grid">
          <div class="dw-cell"><div class="k">开始（相对）</div><div class="v accent">T+${fmtS(c.start_ms)}</div></div>
          <div class="dw-cell"><div class="k">持续时长</div><div class="v">${fmtMs(c.duration_ms)}</div></div>
          <div class="dw-cell"><div class="k">开始（时钟）</div><div class="v" style="font-size:12px">${wall(c.start_ms)}</div></div>
          <div class="dw-cell"><div class="k">结束（相对）</div><div class="v" style="font-size:12px">T+${fmtS(c.start_ms + c.duration_ms)}</div></div>
        </div>${timingBar}
      </div>
      <div class="dw-section"><h3>调用信息</h3>
        <div class="dw-grid">
          ${c.model ? `<div class="dw-cell"><div class="k">模型</div><div class="v" style="font-size:12px">${esc(c.model)}</div></div>` : ""}
          ${c.template_ref ? `<div class="dw-cell"><div class="k">提示词模板</div><div class="v" style="font-size:12px">${esc(c.template_ref)}</div></div>` : ""}
          ${c.tool ? `<div class="dw-cell"><div class="k">工具</div><div class="v" style="font-size:12px">${esc(c.tool)}</div></div>` : ""}
          <div class="dw-cell"><div class="k">所属轮次</div><div class="v">第 ${c.turn} 轮</div></div>
        </div>
      </div>
      ${(tok.input || tok.output) ? `<div class="dw-section"><h3>Token 消耗</h3>
        <div class="tok-bar">
          <span style="width:${(tok.input || 0) / tokTotal * 100}%;background:var(--c-llm)"></span>
          <span style="width:${(tok.output || 0) / tokTotal * 100}%;background:var(--c-tool)"></span>
        </div>
        <div class="timing-legend"><span><i style="background:var(--c-llm)"></i>输入 ${tok.input || 0}</span><span><i style="background:var(--c-tool)"></i>输出 ${tok.output || 0}</span>${tok.cached ? `<span>缓存 ${tok.cached}</span>` : ""}${tok.reasoning ? `<span>思维链 ${tok.reasoning}</span>` : ""}</div>
      </div>` : ""}
      ${c.status === "error" ? `<div class="dw-section"><h3>错误</h3><div class="dw-error"><div class="code">${esc(c.error_code || "ERROR")}</div><div class="msg">${esc(c.error_message || "")}</div></div></div>` : ""}
      ${(parent || kids.length) ? `<div class="dw-section"><h3>调用关系</h3><div class="dw-links">
        ${parent ? `<button class="dw-link" data-goto="${parent.id}">↑ 父：${esc(parent.name)}</button>` : ""}
        ${kids.map((k) => `<button class="dw-link" data-goto="${k.id}">↓ 子：${esc(k.name)}</button>`).join("")}
      </div></div>` : ""}
      <div class="dw-section"><h3>内容</h3>
        ${c.summary ? `<div class="dw-block"><div class="bk">摘要</div><div class="dw-content">${esc(c.summary)}</div></div>` : ""}
        ${contentBlock("输入 / 参数", c.content.input ?? c.content.args)}
        ${contentBlock("输出 / 结果", c.content.output ?? c.content.result)}
        ${contentBlock("思维链", c.content.reasoning)}
        ${contentBlock("错误详情", c.content.error)}
      </div>`;
    $("dwClose").onclick = () => { state.selectedId = null; renderChart(); renderDrawer(); };
    body.querySelectorAll("[data-goto]").forEach((b) => b.onclick = () => selectCall(b.getAttribute("data-goto")));
  }

  // ---------- 提示浮层 ----------
  function showTip(c, evt) {
    const tip = $("tooltip"), ty = TYPES[c.type] || TYPES.llm;
    tip.style.setProperty("--cc", palette[ty.key]);
    tip.innerHTML = `<div class="tt-name"><span class="dot"></span>${esc(c.name)}</div>
      <div class="tt-row"><span>${ty.label} · ${STATUS[c.status].label}</span><b>${fmtMs(c.duration_ms)}</b></div>
      <div class="tt-row"><span>开始 T+${fmtS(c.start_ms)}</span><b>${wall(c.start_ms)}</b></div>
      ${c.model ? `<div class="tt-row"><span>模型</span><b>${esc(c.model)}</b></div>` : ""}${c.tool ? `<div class="tt-row"><span>工具</span><b>${esc(c.tool)}</b></div>` : ""}
      ${c.summary ? `<div class="tt-sum">${esc(c.summary)}</div>` : ""}`;
    tip.classList.add("show");
    const pad = 16, w = tip.offsetWidth, h = tip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
    if (y + h > innerHeight - 8) y = evt.clientY - h - pad;
    tip.style.left = clamp(x, 8, innerWidth - w - 8) + "px"; tip.style.top = clamp(y, 8, innerHeight - h - 8) + "px";
  }
  function hideTip() { $("tooltip").classList.remove("show"); }

  // ---------- 回放 ----------
  function play() {
    if (state.playing) return;
    if (state.playheadMs >= trace.total_ms - 1) state.playheadMs = 0;
    state.playing = true; lastT = 0; $("playBtn").classList.add("playing"); $("playBtn").setAttribute("aria-label", "暂停回放");
    raf = requestAnimationFrame(tick);
  }
  function pause() { state.playing = false; $("playBtn").classList.remove("playing"); $("playBtn").setAttribute("aria-label", "播放回放"); if (raf) cancelAnimationFrame(raf); raf = null; }
  function tick(t) {
    if (!state.playing) return;
    if (!lastT) lastT = t;
    const dt = Math.min(64, t - lastT); lastT = t;
    state.playheadMs = Math.min(trace.total_ms, state.playheadMs + dt * state.speed);
    syncPlayhead();
    if (state.playheadMs >= trace.total_ms) pause(); else raf = requestAnimationFrame(tick);
  }
  function scrubTo(ms) { pause(); state.playheadMs = clamp(ms, 0, trace.total_ms); syncPlayhead(); }

  // ---------- Toast ----------
  let toastTimer = null;
  function toast(msg, actionLabel, actionFn) {
    const el = $("toast");
    el.innerHTML = esc(msg) + (actionLabel ? ` <button class="btn btn-ghost" style="margin-left:10px;padding:3px 10px" id="toastAct">${esc(actionLabel)}</button>` : "");
    el.classList.add("show");
    if (actionLabel && actionFn) $("toastAct").onclick = () => { actionFn(); el.classList.remove("show"); };
    clearTimeout(toastTimer); toastTimer = setTimeout(() => el.classList.remove("show"), actionLabel ? 5200 : 2400);
  }

  // ---------- 保存视图 ----------
  const SV_KEY = "trace-observatory-views";
  function getViews() { try { return JSON.parse(localStorage.getItem(SV_KEY) || "[]"); } catch (e) { return []; } }
  function setViews(v) { try { localStorage.setItem(SV_KEY, JSON.stringify(v)); } catch (e) {} }
  function snapshot() { return { traceId: state.traceId, group: state.group, sort: state.sort, zoom: state.zoom, density: state.density, glow: state.glow, theme: state.theme, search: state.search, types: [...state.types], statuses: [...state.statuses], showGrid: state.showGrid, showConnectors: state.showConnectors, showLabels: state.showLabels, ghost: state.ghost }; }
  function applySnapshot(s) {
    loadTrace(s.traceId);
    Object.assign(state, { group: s.group, sort: s.sort, zoom: s.zoom, density: s.density, glow: s.glow, search: s.search || "", showGrid: s.showGrid, showConnectors: s.showConnectors, showLabels: s.showLabels, ghost: s.ghost });
    state.types = new Set(s.types); state.statuses = new Set(s.statuses);
    setTheme(s.theme); syncControls(); fullRefresh();
  }
  function renderSavedViews() {
    const v = getViews();
    $("savedViews").innerHTML = v.length ? v.map((x, i) => `<span class="saved-view" data-sv="${i}" title="应用视图">${esc(x.name)}<span class="x" data-del="${i}" title="删除">×</span></span>`).join("") : `<span class="sv-empty">暂无保存的视图 · 调好参数后点「保存视图」</span>`;
    $("savedViews").querySelectorAll("[data-sv]").forEach((el) => el.onclick = (e) => { if (e.target.hasAttribute("data-del")) return; applySnapshot(getViews()[+el.getAttribute("data-sv")].snap); toast("已应用视图"); });
    $("savedViews").querySelectorAll("[data-del]").forEach((el) => el.onclick = (e) => {
      e.stopPropagation(); const i = +el.getAttribute("data-del"), views = getViews(), removed = views.splice(i, 1)[0];
      setViews(views); renderSavedViews(); lastDeleted = removed;
      toast(`已删除视图「${removed.name}」`, "撤销", () => { const vv = getViews(); vv.splice(i, 0, lastDeleted); setViews(vv); renderSavedViews(); toast("已恢复视图"); });
    });
  }

  // ---------- 主题 ----------
  function setTheme(t) { state.theme = t; document.documentElement.setAttribute("data-theme", t); $("theme").value = t; readPalette(); }

  // ---------- 控件同步 ----------
  function syncControls() {
    $("zoom").value = state.zoom; $("zoomOut").textContent = state.zoom.toFixed(1) + "×";
    $("density").value = state.density; $("densityOut").textContent = state.density + "px";
    $("glow").value = state.glow; $("glowOut").textContent = state.glow;
    $("speed").value = state.speed; $("speedOut").textContent = state.speed.toFixed(2).replace(/0$/, "") + "×";
    $("group").value = state.group; $("sort").value = state.sort; $("theme").value = state.theme;
    $("grid").checked = state.showGrid; $("connectors").checked = state.showConnectors; $("labels").checked = state.showLabels; $("ghost").checked = state.ghost;
    $("search").value = state.search; $("searchClear").classList.toggle("show", !!state.search);
  }
  function fullRefresh() { renderChart(); renderStats(); renderChips(); renderTranscript(); renderLatency(); renderDrawer(); }

  // ---------- 轨迹切换 ----------
  function renderPills() {
    $("traceSwitch").innerHTML = TRACES.map((t) => `<button class="trace-pill${t.id === state.traceId ? " active" : ""}" data-trace="${t.id}">
      <span class="tp-title">${esc(t.title)}</span><span class="tp-meta">${t.turn_count} 轮 · ${t.call_count} 调用 · ${fmtS(t.total_ms)}</span></button>`).join("");
    $("traceSwitch").querySelectorAll("[data-trace]").forEach((b) => b.onclick = () => { loadTrace(b.getAttribute("data-trace")); renderPills(); fullRefresh(); toast("已切换：" + trace.title); });
  }

  // ---------- 导出 ----------
  function downloadJSON() {
    const blob = new Blob([JSON.stringify(trace, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = trace.id + ".json"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 400);
    toast("已导出轨迹 JSON");
  }

  // ---------- 绑定 ----------
  function bind() {
    const on = (id, ev, fn) => $(id).addEventListener(ev, fn);
    on("zoom", "input", (e) => { state.zoom = +e.target.value; $("zoomOut").textContent = state.zoom.toFixed(1) + "×"; renderChart(); });
    on("density", "input", (e) => { state.density = +e.target.value; $("densityOut").textContent = state.density + "px"; renderChart(); });
    on("glow", "input", (e) => { state.glow = +e.target.value; $("glowOut").textContent = state.glow; renderChart(); });
    on("speed", "input", (e) => { state.speed = +e.target.value; $("speedOut").textContent = state.speed.toFixed(2).replace(/0$/, "") + "×"; });
    on("group", "change", (e) => { state.group = e.target.value; fullRefresh(); });
    on("sort", "change", (e) => { state.sort = e.target.value; fullRefresh(); });
    on("theme", "change", (e) => { setTheme(e.target.value); renderChart(); renderChips(); renderLatency(); renderLegend(); renderDrawer(); toast("主题：" + e.target.selectedOptions[0].textContent); });
    ["grid", "connectors", "labels", "ghost"].forEach((k) => on(k, "change", (e) => { state[k === "grid" ? "showGrid" : k === "connectors" ? "showConnectors" : k === "labels" ? "showLabels" : "ghost"] = e.target.checked; renderChart(); }));
    on("search", "input", (e) => { state.search = e.target.value.trim(); $("searchClear").classList.toggle("show", !!state.search); fullRefresh(); });
    on("searchClear", "click", () => { state.search = ""; $("search").value = ""; $("searchClear").classList.remove("show"); fullRefresh(); });
    on("typeChips", "click", (e) => { const b = e.target.closest("[data-type]"); if (!b) return; const t = b.getAttribute("data-type"); state.types.has(t) ? state.types.delete(t) : state.types.add(t); if (!state.types.size) state.types = new Set(ORDER); fullRefresh(); });
    on("statusChips", "click", (e) => { const b = e.target.closest("[data-status]"); if (!b) return; const s = b.getAttribute("data-status"); state.statuses.has(s) ? state.statuses.delete(s) : state.statuses.add(s); if (!state.statuses.size) state.statuses = new Set(Object.keys(STATUS)); fullRefresh(); });
    on("transcript", "click", (e) => { const t = e.target.closest("[data-turn]"); if (!t) return; const i = +t.getAttribute("data-turn"); state.turnFilter = state.turnFilter === i ? null : i; fullRefresh(); });
    on("playBtn", "click", () => state.playing ? pause() : play());
    on("rewindBtn", "click", () => { pause(); state.playheadMs = 0; syncPlayhead(); });
    on("scrub", "input", (e) => scrubTo(+e.target.value / 1000 * trace.total_ms));
    on("resetView", "click", () => { Object.assign(state, DEFAULTS); state.traceId = TRACES[0].id; state.types = new Set(ORDER); state.statuses = new Set(Object.keys(STATUS)); state.search = ""; state.turnFilter = null; setTheme(DEFAULTS.theme); loadTrace(state.traceId); syncControls(); fullRefresh(); toast("视图已重置"); });
    on("randomize", "click", () => {
      const pick = TRACES[Math.floor(Math.random() * TRACES.length)];
      loadTrace(pick.id);
      trace.calls.forEach((c) => { c.duration_ms = Math.max(20, Math.round(c.duration_ms * (0.85 + Math.random() * 0.3))); });
      trace.total_ms = Math.max(...trace.calls.map((c) => c.start_ms + c.duration_ms));
      state.playheadMs = trace.total_ms; state.glow = 30 + Math.floor(Math.random() * 60); state.density = 32 + Math.floor(Math.random() * 24);
      syncControls(); renderPills(); fullRefresh(); toast("已随机生成轨迹变体：" + trace.title);
    });
    on("saveView", "click", () => {
      const views = getViews(); const name = "视图 " + (views.length + 1) + " · " + trace.title.slice(0, 6);
      views.push({ name, snap: snapshot() }); setViews(views); renderSavedViews(); toast("已保存当前视图");
    });
    on("exportJson", "click", downloadJSON);
    on("exportSvg", "click", () => { WC.exportSVG(svg, trace.id + "-waterfall"); toast("已导出 SVG"); });
    on("exportPng", "click", () => { WC.exportPNG(svg, trace.id + "-waterfall", 2, palette.surface); toast("正在导出 PNG…"); });
    addEventListener("keydown", (e) => {
      if (e.target.matches("input,select,textarea")) return;
      if (e.code === "Space") { e.preventDefault(); state.playing ? pause() : play(); }
      else if (e.key === "Escape" && state.selectedId) { state.selectedId = null; renderChart(); renderDrawer(); }
      else if (e.key === "r" || e.key === "R") { pause(); state.playheadMs = 0; syncPlayhead(); }
    });
    let rt = null;
    const ro = new ResizeObserver(() => { clearTimeout(rt); rt = setTimeout(renderChart, 120); });
    ro.observe($("chartScroll"));
  }

  // ---------- 启动 ----------
  function init() {
    readPalette(); loadTrace(state.traceId); syncControls();
    renderPills(); renderLegend(); fullRefresh(); renderSavedViews(); bind();
    requestAnimationFrame(() => { containerW = Math.max($("chartScroll").clientWidth - 12, 640); renderChart(); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})();