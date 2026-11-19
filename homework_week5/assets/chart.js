/* ===========================================================================
   chart.js · SVG 瀑布甘特图渲染器（无第三方依赖）
   左侧栏：类型图标 + 调用名 + 「T+开始 · 时长」+ 内容摘要（高密度时）
   右侧时间轴：按 start_ms 定位、duration_ms 定宽的调用条；类型用 颜色+纹理+图标 区分。
   支持父子连线、轮次分隔、泳道分组（带标题行）、可拖动播放头。
   颜色在渲染时解析为具体色值，保证 SVG/PNG 导出保真。
   =========================================================================== */
(function () {
  "use strict";

  const TYPES = {
    llm:        { label: "LLM 推理", key: "llm",       shape: "diamond", pattern: "solid"  },
    tool:       { label: "工具调用", key: "tool",      shape: "square",  pattern: "hatch"  },
    governance: { label: "治理预检", key: "gov",       shape: "shield",  pattern: "dot"    },
    approval:   { label: "人工审批", key: "approval",  shape: "hand",    pattern: "stripe" },
    retrieval:  { label: "检索召回", key: "retrieval", shape: "circle",  pattern: "cross"  },
  };
  const ORDER = ["llm", "tool", "governance", "approval", "retrieval"];
  const HEADER_H = 22;

  const esc = (s) => String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  const trunc = (s, n) => { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
  const fmtMs = (ms) => ms >= 1000 ? (ms / 1000).toFixed(ms >= 10000 ? 1 : 2) + "s" : Math.round(ms) + "ms";
  const fmtS = (ms) => (ms / 1000).toFixed(2) + "s";

  function glyph(shape, cx, cy, r, color) {
    const c = esc(color);
    switch (shape) {
      case "diamond": return `<path d="M${cx} ${cy - r} L${cx + r} ${cy} L${cx} ${cy + r} L${cx - r} ${cy} Z" fill="${c}"/>`;
      case "square": return `<rect x="${cx - r + 1}" y="${cy - r + 1}" width="${2 * r - 2}" height="${2 * r - 2}" rx="2" fill="${c}"/>`;
      case "circle": return `<circle cx="${cx}" cy="${cy}" r="${r - 1}" fill="${c}"/>`;
      case "shield": return `<path d="M${cx} ${cy - r} L${cx + r - 1} ${cy - r + 2} V${cy + 1} Q${cx + r - 1} ${cy + r} ${cx} ${cy + r} Q${cx - r + 1} ${cy + r} ${cx - r + 1} ${cy + 1} V${cy - r + 2} Z" fill="${c}"/>`;
      case "hand": return `<path d="M${cx - r + 1} ${cy + r - 1} V${cy - 1} q0 ${-r + 1} ${r - 1} ${-r + 1} q${r - 1} 0 ${r - 1} ${r - 1} V${cy + r - 1} Z" fill="${c}"/>`;
      default: return `<circle cx="${cx}" cy="${cy}" r="${r - 1}" fill="${c}"/>`;
    }
  }

  function defs(pal, glow) {
    const g = (glow / 100) * 6 + 0.4, op = (glow / 100) * 0.85;
    let filters = "";
    ORDER.forEach((t) => {
      filters += `<filter id="glow-${t}" x="-60%" y="-160%" width="220%" height="420%">
        <feDropShadow dx="0" dy="0" stdDeviation="${g.toFixed(2)}" flood-color="${esc(pal[TYPES[t].key])}" flood-opacity="${op.toFixed(2)}"/></filter>`;
    });
    const dark = "rgba(0,0,0,.30)";
    return `<defs>${filters}
      <pattern id="pat-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="6" stroke="${dark}" stroke-width="2.2"/></pattern>
      <pattern id="pat-dot" width="5" height="5" patternUnits="userSpaceOnUse"><circle cx="2.5" cy="2.5" r="1.05" fill="${dark}"/></pattern>
      <pattern id="pat-stripe" width="6" height="6" patternUnits="userSpaceOnUse"><line x1="1.5" y1="0" x2="1.5" y2="6" stroke="${dark}" stroke-width="2.4"/></pattern>
      <pattern id="pat-cross" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="7" stroke="${dark}" stroke-width="1.5"/><line x1="0" y1="3.5" x2="7" y2="3.5" stroke="${dark}" stroke-width="1.5"/></pattern>
    </defs>`;
  }

  function niceStep(totalMs, targetTicks) {
    const raw = totalMs / targetTicks;
    const steps = [50, 100, 200, 250, 500, 1000, 2000, 2500, 5000, 10000, 15000, 30000, 60000];
    for (const s of steps) if (s >= raw) return s;
    return 60000;
  }

  let geom = null;

  function render(svg, m) {
    const pal = m.palette, st = m.state, rows = m.rows;
    const padLeft = 232, padRight = 26, padTop = 8, axisH = 26, padBottom = 16;
    const rowH = st.density;
    const containerW = Math.max(360, m.containerW);
    const basePx = (containerW - padLeft - padRight) / Math.max(1, m.totalMs);
    const pxPerMs = basePx * st.zoom;
    const innerW = Math.max(containerW - padLeft - padRight, m.totalMs * pxPerMs);
    const W = Math.round(padLeft + innerW + padRight);
    const X = (ms) => padLeft + ms * pxPerMs;

    // 累积行几何（泳道标题行更矮）
    const heights = rows.map((r) => r.header ? HEADER_H : rowH);
    const ys = []; let acc = padTop + axisH;
    rows.forEach((r, i) => { ys[i] = acc; acc += heights[i]; });
    const H = Math.round(acc + padBottom);
    const bodyH = acc - (padTop + axisH);
    const center = (i) => ys[i] + heights[i] / 2;
    const barH = Math.max(9, Math.min(rowH * 0.5, 26));
    geom = { padLeft, padRight, padTop, axisH, rowH, pxPerMs, totalMs: m.totalMs, W, H, rows, X, ys, heights };

    const rowIndex = new Map();
    rows.forEach((r, i) => { if (!r.header) rowIndex.set(r.call.id, i); });

    let s = `<rect x="0" y="0" width="${W}" height="${H}" fill="${esc(pal.surface)}"/>` + defs(pal, st.glow);

    // 轮次分隔带
    if (m.turns && st.group === "time") {
      m.turns.forEach((tn, i) => {
        const x0 = X(tn.start_ms);
        const x1 = i + 1 < m.turns.length ? X(m.turns[i + 1].start_ms) : padLeft + innerW;
        if (i % 2 === 1) s += `<rect class="wf-turn-band" x="${x0}" y="${padTop + axisH}" width="${Math.max(0, x1 - x0)}" height="${bodyH}"/>`;
        s += `<line x1="${x0}" y1="${padTop + axisH}" x2="${x0}" y2="${H - padBottom}" stroke="${esc(pal.accent)}" stroke-width="1" stroke-dasharray="3 4" opacity=".26"/>`;
        s += `<text class="wf-turn-label" x="${x0 + 5}" y="${padTop + axisH + 12}">轮次 ${tn.index}</text>`;
      });
    }

    // 时间轴 + 网格
    const step = niceStep(m.totalMs, Math.max(5, Math.round(innerW / 110)));
    s += `<line x1="${padLeft}" y1="${padTop + axisH}" x2="${padLeft + innerW}" y2="${padTop + axisH}" stroke="${esc(pal.line)}" stroke-width="1.4"/>`;
    for (let t = 0; t <= m.totalMs + 1; t += step) {
      const x = X(t); if (x > padLeft + innerW + 1) break;
      const major = (t / step) % 2 === 0;
      if (st.showGrid) s += `<line class="wf-grid${major ? " major" : ""}" x1="${x}" y1="${padTop + axisH}" x2="${x}" y2="${H - padBottom}"/>`;
      s += `<line x1="${x}" y1="${padTop + axisH - 5}" x2="${x}" y2="${padTop + axisH}" stroke="${esc(pal.muted)}" stroke-width="1.2"/>`;
      s += `<text class="wf-axis-label" x="${x}" y="${padTop + axisH - 9}" text-anchor="middle">${t >= 1000 ? (t / 1000) + "s" : t + "ms"}</text>`;
    }

    // 行底纹 / 泳道标题
    rows.forEach((r, i) => {
      if (r.header) {
        s += `<rect x="0" y="${ys[i]}" width="${W}" height="${HEADER_H}" fill="${esc(r.color)}" opacity=".10"/>`;
        s += `<rect x="0" y="${ys[i]}" width="3" height="${HEADER_H}" fill="${esc(r.color)}"/>`;
        s += `<text class="wf-lane-label" x="11" y="${ys[i] + 15}" fill="${esc(r.color)}">${esc(r.label)}</text>`;
        s += `<line x1="0" y1="${ys[i]}" x2="${W}" y2="${ys[i]}" stroke="${esc(pal.line)}" stroke-width="1"/>`;
      } else if (i % 2 === 0) {
        s += `<rect class="wf-rowband" x="0" y="${ys[i]}" width="${W}" height="${rowH}"/>`;
      }
    });

    // 父子连线
    if (st.showConnectors) {
      rows.forEach((r) => {
        if (r.header) return;
        const c = r.call; if (!c.parent_id || !rowIndex.has(c.parent_id) || !rowIndex.has(c.id)) return;
        const p = m.byId.get(c.parent_id); if (!p) return;
        const pi = rowIndex.get(c.parent_id), ci = rowIndex.get(c.id);
        const px = X(p.start_ms), py = center(pi), cx = X(c.start_ms), cy = center(ci);
        const midY = py + (cy - py) / 2;
        s += `<path class="wf-connector" d="M${px} ${py} V${midY} H${cx} V${cy}"/>`;
        s += `<circle cx="${cx}" cy="${cy}" r="2" fill="${esc(pal.dim)}"/>`;
      });
    }

    // 调用条 + 左栏
    rows.forEach((r, i) => {
      if (r.header) return;
      const c = r.call, ty = TYPES[c.type] || TYPES.llm, col = pal[ty.key];
      const y = ys[i], cy = center(i), rh = heights[i];
      const bx = X(c.start_ms), bw = Math.max(3, c.duration_ms * pxPerMs), by = cy - barH / 2;
      const isErr = c.status === "error";
      const dimmed = m.dimmedIds.has(c.id);
      const ghosted = st.ghost && m.playheadMs < m.totalMs - 1 && c.start_ms > m.playheadMs;
      const active = m.activeId === c.id, sel = m.selectedId === c.id;
      let cls = "wf-bar";
      if (dimmed) cls += " dimmed"; else if (ghosted) cls += " ghosted"; else cls += " reached";
      if (sel) cls += " selected";
      if (active && !dimmed && !ghosted) cls += " active";
      const filt = (!dimmed && !ghosted && st.glow > 4) ? ` filter="url(#glow-${c.type})"` : "";
      const stroke = isErr ? ` stroke="${esc(pal.error)}" stroke-width="1.8"` : (sel ? ` stroke="${esc(pal.text)}" stroke-width="2"` : "");

      const indent = 12 + r.depth * 15, gx = indent;
      s += `<g data-id="${esc(c.id)}" class="wf-row" style="cursor:pointer">`;
      s += `<rect x="0" y="${y}" width="${padLeft - 6}" height="${rh}" fill="transparent"/>`;
      if (r.depth > 0 && st.group === "time") s += `<path d="M${indent - 9} ${y} V${cy} H${indent - 3}" stroke="${esc(pal.dim)}" stroke-width="1.1" fill="none" opacity=".5"/>`;
      const threeLine = rh >= 38 && st.showLabels && c.summary;
      s += glyph(ty.shape, gx + 5, cy - (threeLine ? 7 : (rh >= 28 ? 4 : 0)), 5.2, isErr ? pal.error : col);
      const nameX = gx + 16, nameAvail = padLeft - 12 - nameX;
      const nameY = threeLine ? y + 13 : (rh >= 28 ? y + 14 : y + rh / 2 + 4);
      s += `<text class="wf-label" x="${nameX}" y="${nameY}">${esc(trunc(c.name, Math.max(6, Math.floor(nameAvail / 7.2))))}</text>`;
      if (rh >= 28) {
        const sub = `T+${fmtS(c.start_ms)} · ${fmtMs(c.duration_ms)}` + (c.model ? " · " + c.model : (c.tool ? " · " + c.tool : ""));
        s += `<text class="wf-sub" x="${nameX}" y="${y + (threeLine ? 25 : 26)}">${esc(trunc(sub, Math.max(8, Math.floor(nameAvail / 5.6))))}</text>`;
      }
      if (threeLine) s += `<text class="wf-sub" x="${nameX}" y="${y + 36}" opacity=".78">${esc(trunc(c.summary, Math.max(8, Math.floor(nameAvail / 5.6))))}</text>`;

      s += `<g class="${cls}"${filt} data-id="${esc(c.id)}">`;
      s += `<rect x="${bx}" y="${by}" width="${bw}" height="${barH}" rx="4" fill="${esc(isErr ? pal.error : col)}"${stroke}/>`;
      if (ty.pattern !== "solid" && bw > 6) s += `<rect x="${bx}" y="${by}" width="${bw}" height="${barH}" rx="4" fill="url(#pat-${ty.pattern})" pointer-events="none"/>`;
      const durTxt = fmtMs(c.duration_ms);
      if (bw > 46) s += `<text class="wf-dur" x="${bx + bw / 2}" y="${cy + 3.6}" text-anchor="middle" pointer-events="none">${durTxt}</text>`;
      else if (!dimmed && !ghosted) s += `<text class="wf-sub" x="${bx + bw + 5}" y="${cy + 3.4}" pointer-events="none">${durTxt}</text>`;
      if (isErr) {
        const ex = bx + bw + (bw > 46 ? 5 : 32);
        s += `<path d="M${ex} ${cy - 6} l6 11 h-12 Z" fill="${esc(pal.error)}" pointer-events="none"/>`;
        s += `<text x="${ex}" y="${cy + 3.5}" text-anchor="middle" font-size="8" font-weight="800" fill="${esc(pal.barText)}" pointer-events="none">!</text>`;
      }
      s += `</g></g>`;
    });

    // 播放头（整组 transform 定位）
    const phx = X(Math.min(m.playheadMs, m.totalMs));
    s += `<g data-playhead="1" transform="translate(${phx},0)">`;
    s += `<line class="wf-playhead-glow" x1="0" y1="${padTop + axisH - 4}" x2="0" y2="${H - padBottom}"/>`;
    s += `<line class="wf-playhead" x1="0" y1="${padTop + axisH - 4}" x2="0" y2="${H - padBottom}"/>`;
    s += `<path class="wf-ph-handle" d="M-7 ${padTop + axisH - 12} h14 l-7 9 Z"/>`;
    s += `<rect x="-26" y="${padTop - 2}" width="52" height="15" rx="4" fill="${esc(pal.accent2)}"/>`;
    s += `<text data-ph-text="1" x="0" y="${padTop + 9}" text-anchor="middle" font-size="9.5" font-weight="700" fill="${esc(pal.barText)}">${fmtS(m.playheadMs)}</text>`;
    s += `<rect x="-9" y="${padTop + axisH - 14}" width="18" height="${H - padBottom - padTop - axisH + 14}" fill="transparent" style="cursor:ew-resize"/>`;
    s += `</g>`;

    svg.setAttribute("width", W); svg.setAttribute("height", H); svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.innerHTML = s;
    wire(svg, m);
    return geom;
  }

  function wire(svg, m) {
    if (svg._wired) { svg._cb = m; return; }
    svg._wired = true; svg._cb = m;
    const msFromEvent = (evt) => {
      const rect = svg.getBoundingClientRect(), scale = geom.W / rect.width;
      const x = (evt.clientX - rect.left) * scale;
      return Math.max(0, Math.min(geom.totalMs, (x - geom.padLeft) / geom.pxPerMs));
    };
    svg.addEventListener("mousemove", (evt) => {
      const cb = svg._cb;
      if (svg._drag) { cb.onScrub(msFromEvent(evt)); return; }
      const g = evt.target.closest("[data-id]");
      if (g) { const c = cb.byId.get(g.getAttribute("data-id")); if (c) cb.onHover(c, evt); } else cb.onLeave();
    });
    svg.addEventListener("mouseleave", () => { if (!svg._drag) svg._cb.onLeave(); });
    svg.addEventListener("click", (evt) => {
      if (svg._dragMoved) { svg._dragMoved = false; return; }
      const g = evt.target.closest("[data-id]");
      if (g) svg._cb.onSelect(g.getAttribute("data-id"));
    });
    svg.addEventListener("pointerdown", (evt) => {
      if (evt.target.closest("[data-playhead]")) { svg._drag = true; svg._dragMoved = false; try { svg.setPointerCapture(evt.pointerId); } catch (e) {} svg._cb.onScrub(msFromEvent(evt)); evt.preventDefault(); }
    });
    svg.addEventListener("pointermove", (evt) => { if (svg._drag) { svg._dragMoved = true; svg._cb.onScrub(msFromEvent(evt)); } });
    const endDrag = (evt) => { if (svg._drag) { svg._drag = false; try { svg.releasePointerCapture(evt.pointerId); } catch (e) {} } };
    svg.addEventListener("pointerup", endDrag);
    svg.addEventListener("pointercancel", endDrag);
  }

  function updatePlayhead(svg, ms, opts) {
    if (!geom) return null;
    const x = geom.X(Math.min(ms, geom.totalMs));
    const ph = svg.querySelector("[data-playhead]"); if (ph) ph.setAttribute("transform", `translate(${x},0)`);
    const txt = svg.querySelector("[data-ph-text]"); if (txt) txt.textContent = fmtS(ms);
    let active = null;
    svg.querySelectorAll("g.wf-bar").forEach((n) => {
      const c = opts.byId.get(n.getAttribute("data-id")); if (!c) return;
      const dimmed = opts.dimmedIds.has(c.id);
      const ghosted = opts.ghost && ms < geom.totalMs - 1 && c.start_ms > ms;
      const isActive = !dimmed && !ghosted && c.start_ms <= ms && ms < c.start_ms + c.duration_ms;
      n.classList.toggle("dimmed", dimmed);
      n.classList.toggle("ghosted", !dimmed && ghosted);
      n.classList.toggle("reached", !dimmed && !ghosted);
      n.classList.toggle("active", isActive);
      if (isActive) active = c;
    });
    return active;
  }

  function serialize(svg) {
    const clone = svg.cloneNode(true);
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    const style = document.createElementNS("http://www.w3.org/2000/svg", "style");
    style.textContent = `text{font-family:'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif}
      .wf-axis-label{font-size:10.5px}.wf-label{font-size:11.5px;font-weight:600}.wf-sub{font-size:10px}
      .wf-dur{font-size:10.5px;font-weight:700}.wf-turn-label{font-size:10px;font-weight:700}.wf-lane-label{font-size:10.5px;font-weight:700}
      .wf-connector{stroke-width:1.4;fill:none;opacity:.6}.wf-grid{stroke-width:1}.wf-rowband{opacity:.32}
      .wf-bar.dimmed{opacity:.16}.wf-bar.ghosted{opacity:.2}.wf-turn-band{opacity:.05}`;
    clone.insertBefore(style, clone.firstChild);
    return new XMLSerializer().serializeToString(clone);
  }
  function download(filename, blob) {
    const url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = filename; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(url); a.remove(); }, 400);
  }
  function exportSVG(svg, name) { download(name + ".svg", new Blob([serialize(svg)], { type: "image/svg+xml;charset=utf-8" })); }
  function exportPNG(svg, name, scale, bg) {
    const str = serialize(svg), w = +svg.getAttribute("width"), h = +svg.getAttribute("height");
    const img = new Image(), url = URL.createObjectURL(new Blob([str], { type: "image/svg+xml;charset=utf-8" }));
    img.onload = function () {
      const cv = document.createElement("canvas"); cv.width = Math.round(w * scale); cv.height = Math.round(h * scale);
      const ctx = cv.getContext("2d"); ctx.fillStyle = bg || "#111a2e"; ctx.fillRect(0, 0, cv.width, cv.height);
      ctx.drawImage(img, 0, 0, cv.width, cv.height); URL.revokeObjectURL(url);
      cv.toBlob((b) => { if (b) download(name + ".png", b); else exportSVG(svg, name); }, "image/png");
    };
    img.onerror = function () { URL.revokeObjectURL(url); exportSVG(svg, name); };
    img.src = url;
  }

  window.WaterfallChart = { TYPES, ORDER, render, updatePlayhead, exportSVG, exportPNG, serialize, getGeom: () => geom, fmtMs, fmtS };
})();