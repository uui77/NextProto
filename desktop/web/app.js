(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const STATE = { status: null };
  let connected = false;
  let currentView = "device";
  // 转写结果状态
  const TX = {
    source: null,     // 源音频文件名
    audioUrl: null,   // 音频 URL
    segments: [],     // 片段列表
    fullText: "",     // 完整文本
    activeIdx: -1,    // 当前激活的片段索引
    editMode: false,
    origText: "",     // 编辑模式进入时的原始文本（用于判定是否有改动）
    spk_mode: "off",  // "off" | "campplus" | "fallback"
  };
  // 搜索状态
  const SEARCH = {
    q: "",
    hits: [],        // [{segIdx, from, to}]
    cursor: -1,
  };
  // 转写中文件名集合（用于卡片 busy 标记，单文件和批量都走这里）
  const BUSY_NAMES = new Set();
  // 下载进行中标记（用于禁用删除等互斥操作）
  let _dlActive = false;
  // 进度条拖动状态（只绑定一次 document 事件）
  const AUDIO_DRAG = { dragging: false, seekFn: null };
  document.addEventListener("mousemove", (e) => { if (AUDIO_DRAG.dragging && AUDIO_DRAG.seekFn) AUDIO_DRAG.seekFn(e); });
  document.addEventListener("mouseup",   () => { AUDIO_DRAG.dragging = false; });

  // ============ Toast ============
  function toast(msg, kind = "info", ttl = 2600) {
    const stack = $("toastStack");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast t-${kind}`;
    const icoSvg = {
      info:    `<svg viewBox="0 0 24 24" class="t-ico" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>`,
      success: `<svg viewBox="0 0 24 24" class="t-ico" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>`,
      warn:    `<svg viewBox="0 0 24 24" class="t-ico" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`,
      error:   `<svg viewBox="0 0 24 24" class="t-ico" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`,
    };
    el.innerHTML = (icoSvg[kind] || icoSvg.info) + `<div>${escapeHtml(msg)}</div>`;
    stack.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateY(6px)";
      el.style.transition = "all .2s ease";
      setTimeout(() => el.remove(), 220);
    }, ttl);
  }

  // ============ Protocol helpers (for self-test) ============
  function crc16Xmodem(data) {
    let crc = 0;
    for (const b of data) {
      crc ^= b << 8;
      for (let i = 0; i < 8; i++) {
        crc = (crc & 0x8000) ? ((crc << 1) ^ 0x1021) & 0xffff : (crc << 1) & 0xffff;
      }
    }
    return crc;
  }

  function strBytes(str, padTo) {
    const arr = [];
    for (let i = 0; i < str.length; i++) arr.push(str.charCodeAt(i) & 0xff);
    while (padTo && arr.length < padTo) arr.push(0);
    return arr;
  }

  function buildFrame(seq, data) {
    const len = data.length;
    const crcInput = new Uint8Array(2 + len);
    crcInput[0] = len & 0xff;
    crcInput[1] = (len >> 8) & 0xff;
    crcInput.set(data, 2);
    const crc = crc16Xmodem(crcInput);
    const frame = new Uint8Array(6 + len);
    frame[0] = 0x5a;
    frame[1] = seq & 0xff;
    frame[2] = crc & 0xff;
    frame[3] = (crc >> 8) & 0xff;
    frame[4] = len & 0xff;
    frame[5] = (len >> 8) & 0xff;
    frame.set(data, 6);
    return frame;
  }

  function bytesToHex(arr) {
    return Array.from(arr).map(b => b.toString(16).padStart(2, "0")).join(" ");
  }

  function parseFileList(body) {
    const dv = new DataView(body.buffer);
    const count = dv.getUint32(0, false);
    const files = [];
    let off = 4;
    for (let i = 0; i < count && off + 28 <= body.length; i++) {
      const time = dv.getUint32(off, false);
      const size = dv.getUint32(off + 4, false);
      let name = "";
      for (let j = off + 8; j < off + 28; j++) {
        if (body[j] === 0) break;
        name += String.fromCharCode(body[j]);
      }
      files.push({ time, size, name });
      off += 28;
    }
    return { count, files };
  }

  function inspectWav(buf) {
    if (buf.length < 44) return { ok: false };
    const dv = new DataView(buf.buffer);
    const riff = String.fromCharCode(buf[0], buf[1], buf[2], buf[3]);
    if (riff !== "RIFF") return { ok: false };
    const declared = dv.getUint32(4, true) + 8;
    const channels = dv.getUint16(22, true);
    const sampleRate = dv.getUint32(24, true);
    const bitsPerSample = dv.getUint16(34, true);
    return { ok: true, declared, channels, sampleRate, bitsPerSample };
  }

  // Page title mapping
  const pageMeta = {
    overview: { title: "概览", desc: "管理你的录音笔，查看状态，下载与转写文件" },
    files: { title: "文件管理", desc: "录音文件下载、转写与本地管理" },
    realtime: { title: "实时转写", desc: "实时音频流接收与转写" },
    control: { title: "录音控制", desc: "远程控制设备录音及增益设置" },
    tools: { title: "开发工具", desc: "原始命令发送与协议调试" },
  };

  // ============ Theme ============
  function initTheme() {
    const saved = localStorage.getItem("recorder-theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const theme = saved || (prefersDark ? "dark" : "light");
    document.documentElement.setAttribute("data-theme", theme);

    $("themeToggle").onclick = () => {
      const current = document.documentElement.getAttribute("data-theme");
      const next = current === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("recorder-theme", next);
    };
  }

  // ============ Navigation ============
  function initNav() {
    const navItems = document.querySelectorAll(".nav-item");
    const pages = document.querySelectorAll(".page");

    navItems.forEach((item) => {
      item.onclick = () => {
        const section = item.dataset.section;
        navItems.forEach((n) => n.classList.remove("active"));
        item.classList.add("active");
        pages.forEach((p) => p.classList.add("hidden"));
        const page = document.getElementById(`page-${section}`);
        if (page) page.classList.remove("hidden");
        const meta = pageMeta[section];
        if (meta) {
          $("pageTitle").textContent = meta.title;
          $("pageDesc").textContent = meta.desc;
        }
        // Auto-load local files when switching to files tab
        if (section === "files" && currentView === "local") loadLocal();
      };
    });

    // Segmented view toggle
    document.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.onclick = () => {
        currentView = btn.dataset.view;
        document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        $("deviceTableWrap").classList.toggle("hidden", currentView !== "device");
        $("localWrap").classList.toggle("hidden", currentView !== "local");
        // 切换视图后重新计算按钮可用状态
        refreshNeedConn();
        if (currentView === "local") loadLocal();
      };
    });

    // Gain buttons
    document.querySelectorAll(".gain-btn").forEach((btn) => {
      btn.onclick = () => {
        const level = Number(btn.dataset.gain);
        document.querySelectorAll(".gain-btn").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        const labels = { 1: "低", 2: "中", 3: "高" };
        $("gainCurrent").textContent = labels[level];
        run(btn, async () => {
          const r = await api("/api/gain", { level });
          log("OK", r.message);
        });
      };
    });
  }

  // ============ Logging (with buffer limit) ============
  const MAX_LOG_LINES = 1200;
  const logBuffer = [];

  // 提示音：用 Web Audio API 生成短促"叮"声，无需外部文件
  let _audioCtx = null;
  function playDing(type = "ok") {
    try {
      if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const ctx = _audioCtx;
      if (ctx.state === "suspended") ctx.resume();
      // ok: 两音上行（完成）; info: 单音; error: 两音下行
      const notes = type === "error" ? [440, 330] : type === "info" ? [660] : [660, 880];
      let t = ctx.currentTime;
      notes.forEach(freq => {
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.frequency.value = freq;
        osc.type = "sine";
        gain.gain.setValueAtTime(0, t);
        gain.gain.linearRampToValueAtTime(0.3, t + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.001, t + 0.3);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t);
        osc.stop(t + 0.3);
        t += 0.15;
      });
    } catch (e) { /* 忽略音频错误 */ }
  }

  function log(level, text) {
    const ts = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    const line = `[${ts}] [${level}] ${text}`;
    logBuffer.push(line);
    if (logBuffer.length > MAX_LOG_LINES) logBuffer.shift();
    const div = document.createElement("div");
    div.className = `line ${level.toLowerCase()}`;
    div.textContent = line;
    $("log").appendChild(div);
    // 限制 DOM 节点数量
    const logEl = $("log");
    while (logEl.children.length > MAX_LOG_LINES) logEl.removeChild(logEl.firstChild);
    logEl.scrollTop = logEl.scrollHeight;
  }

  function exportLog() {
    const blob = new Blob([logBuffer.join("\n")], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    const ts = new Date().toISOString().replace(/[-:]/g, "").replace(/\..+/, "").replace("T", "-");
    a.download = `qs668-log-${ts}.txt`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  }

  // ============ API ============
  async function api(path, body, method) {
    const opts = { method: method || (body === undefined ? "GET" : "POST"), cache: "no-store" };
    if (body !== undefined) {
      opts.headers = { "Content-Type": "application/json", "Cache-Control": "no-store" };
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(path, opts);
    let data = null;
    try { data = await resp.json(); } catch (e) { /* empty */ }
    if (!resp.ok) {
      const detail = (data && data.error) ? data.error : `HTTP ${resp.status}`;
      // 注意：5xx 的"后端错误"日志不再在这里额外打一遍。
      // 友好错误（蓝牙未开启等）会通过下面的 throw 传到 run() 的 catch 只打一次；
      // 未知错误后端 WebSocket 仍会推一条 ERR 提示。这样避免 3 遍重复。
      throw new Error(detail);
    }
    return data;
  }

  async function run(btn, fn) {
    if (btn) { btn.disabled = true; btn.classList.add("busy"); }
    try { await fn(); }
    catch (err) { log("ERR", err.message); }
    finally {
      if (btn) { btn.disabled = false; btn.classList.remove("busy"); }
      refreshNeedConn();
    }
  }

  function setConnected(on, mtu, payload) {
    connected = on;
    const badge = $("connBadge");
    if (on) {
      badge.classList.add("connected");
      badge.querySelector(".status-text").textContent = "已连接";
      playDing("info");
    } else {
      badge.classList.remove("connected");
      badge.querySelector(".status-text").textContent = "未连接";
    }
    $("disconnectBtn").disabled = !on;
    if (on) log("INFO", `已连接，MTU=${mtu}，单写上限=${payload}B`);
    else log("INFO", "已断开连接");
    refreshNeedConn();
  }

  function refreshNeedConn() {
    document.querySelectorAll("[data-need-conn]").forEach((b) => {
      if (b.classList.contains("busy")) return;
      // 特殊：刷新按钮在「本地」视图下不需要连设备（刷新 downloads 目录）
      if (b.id === "filesBtn" && currentView === "local") {
        b.disabled = false;
      } else {
        b.disabled = !connected;
      }
    });
  }

  // ============ WebSocket ============
  function openWs() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "log") {
        log(msg.level, msg.text);
      } else if (msg.type === "download_start") {
        _dlActive = true;
        showProgress(0, msg.expected || 0, msg.filename);
        toast(`开始下载：${msg.filename}`, "info", 2000);
        log("INFO", `开始下载：${msg.filename}`);
        const pw = $("progressWrap");
        if (pw) pw.scrollIntoView({ behavior: "smooth", block: "center" });
        updateDlButtons();
      } else if (msg.type === "progress") {
        showProgress(msg.received, msg.expected);
      } else if (msg.type === "realtime") {
        if (msg.event === "bytes") $("rtBytes").textContent = msg.text;
        if (msg.event === "filename") { log("INFO", `实时录音文件名：${msg.value}`); $("rtFileName").textContent = msg.value; }
        if (msg.event === "state") { log("INFO", `实时状态：${msg.value}`); $("rtStatus").textContent = msg.value; }
      } else if (msg.type === "device_event") {
        log("EVENT", `设备事件：${msg.desc} body=${msg.body}`);
      }
    };
    ws.onclose = () => setTimeout(openWs, 2000);
  }

  let _dlFilename = "";
  function showProgress(received, expected, filename) {
    if (filename) _dlFilename = filename;
    $("progressWrap").classList.remove("hidden");
    const pct = expected > 0 ? Math.min(received / expected * 100, 100) : 0;
    $("progressBar").style.width = `${pct}%`;
    const nameTag = _dlFilename ? `正在下载：${_dlFilename}  ` : "";
    $("progressText").textContent = expected > 0
      ? `${nameTag}${fmtSize(received)} / ~${fmtSize(expected)} (${pct.toFixed(0)}%)`
      : `${nameTag}${fmtSize(received)}`;
  }

  function fmtSize(n) {
    if (n >= 1048576) return `${(n / 1048576).toFixed(1)} MB`;
    if (n >= 1024) return `${(n / 1024).toFixed(1)} KB`;
    return `${n} B`;
  }

  // 根据下载状态启用/禁用设备文件列表中的删除按钮
  function updateDlButtons() {
    const tbody = $("fileRows");
    if (!tbody) return;
    tbody.querySelectorAll("tr").forEach(tr => {
      const delBtn = tr.querySelector("button.danger");
      if (!delBtn) return;
      if (_dlActive) {
        delBtn.disabled = true;
        delBtn.title = "下载中，请稍候";
      } else {
        delBtn.disabled = false;
        delBtn.title = "";
      }
    });
  }

  // ============ Connection ============
  $("scanBtn").onclick = () => run($("scanBtn"), async () => {
    log("INFO", "扫描中（6 秒）...");
    const devices = await api("/api/scan",
      { timeout: 6, compat: $("compatChk").checked });
    const list = $("deviceList");
    list.innerHTML = "";
    if (!devices.length) {
      log("WARN", "未发现录音笔；可勾选兼容扫描重试");
      const hint = document.createElement("div");
      hint.className = "empty-state";
      hint.style.cssText = "padding:16px; gap:8px; text-align:center; justify-content:flex-start;";
      hint.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12.55a11 11 0 0 1 14.08 0"/><path d="M1.42 9a16 16 0 0 1 21.16 0"/><path d="M8.53 16.11a6 6 0 0 1 6.95 0"/><line x1="12" y1="20" x2="12.01" y2="20"/></svg>
        <p class="es-title">没找到录音笔</p>
        <p class="es-desc">
          请确认录音笔已开机并开启「录音设备」模式 / USB 调试。<br>
          也可以勾选 <b>兼容扫描</b> 再试一次。
        </p>
      `;
      list.appendChild(hint);
      return;
    }
    devices.forEach((d) => {
      const wrap = document.createElement("div");
      wrap.className = "device-row";
      wrap.style.cssText = "display:flex;gap:8px;align-items:center;";
      const btn = document.createElement("button");
      btn.className = "device-item";
      btn.textContent = `${d.name}  ${d.address}`;
      btn.style.flex = "1";
      btn.onclick = () => run(btn, async () => {
        log("INFO", "连接中...");
        const autoPair = !!(document.getElementById("autoPairChk") || {}).checked;
        const r = await api("/api/connect", { target: d.index, auto_pair: autoPair });
        setConnected(true, r.mtu, r.payload);
        // 连接成功后自动巡检
        log("INFO", "自动巡检中...");
        try {
          const rows = await api("/api/smoke", {});
          const panel = $("infoPanel");
          panel.innerHTML = "";
          rows.forEach((row) => {
            const div = document.createElement("div");
            if (row.ok === false) div.className = "bad";
            div.innerHTML = `<b>${row.label}</b>${row.value}`;
            panel.appendChild(div);
            log(row.ok ? "OK" : "WARN", `${row.label}：${row.value}`);
          });
          $("infoCard").classList.remove("hidden");
          rows.forEach((row) => {
            const map = { "电量": "statBattery", "容量": "statCapacity", "固件": "statFirmware", "录音状态": "statRecording" };
            const elId = map[row.label];
            if (elId) $(elId).textContent = row.value;
          });
          await loadFiles();
        } catch (e) {
          log("WARN", `自动巡检未完成：${e.message}`);
        }
      });
      const pairBtn = document.createElement("button");
      pairBtn.className = "device-pair-btn";
      pairBtn.title = "仅配对不连接（在系统弹窗里确认配对码，录音笔常见 0000 / 1234）";
      pairBtn.textContent = "🔗 配对";
      pairBtn.onclick = () => run(pairBtn, async () => {
        log("INFO", `开始配对 ${d.name || d.address}（20 秒内会弹出系统配对弹窗 / 自动打开 Windows 添加设备向导；录音笔配对码一般是 0000 / 1234）`);
        try {
          const r = await api("/api/pair", { target: d.index });
          if (r && r.paired) {
            log("OK", `配对成功：${d.address}，现在直接点左边的"设备名 / 地址"就能秒连接`);
          } else {
            // paired=false → 后端已帮用户打开 Windows「添加设备」设置页
            log("INFO", `已帮你打开 Windows「添加蓝牙设备」向导（如果没弹出，请按 Win+I → 蓝牙和其他设备 → 添加设备）。`
              + ` 在列表里选中"${d.name || "CB08 / QS668"}"，配对码输入 0000 或 1234，配对完成后回到本页面点左边的"设备名 / 地址"即可连接。`);
          }
        } catch (e) {
          // 配对失败友好提示：同时给两个路径
          const msg = (e && e.message) ? e.message : String(e);
          log("ERR", `${msg}`
            + ` 仍不成功的话直接按 Win+I → 蓝牙和其他设备 → 添加设备 → 选"${d.name || "CB08"}"，配对码 0000 或 1234，手动配对完成后再回来点连接。`);
        }
      });
      wrap.appendChild(btn);
      wrap.appendChild(pairBtn);
      list.appendChild(wrap);
    });
  });

  $("disconnectBtn").onclick = () => run($("disconnectBtn"), async () => {
    await api("/api/disconnect", {});
    setConnected(false);
  });

  // ============ Device Info ============
  $("infoBtn").onclick = () => run($("infoBtn"), async () => {
    const r = await api("/api/info");
    $("statBattery").textContent = r.battery || "--";
    $("statCapacity").textContent = r.capacity || "--";
    $("statFirmware").textContent = r.version || "--";
    log("OK", `电量: ${r.battery}，容量: ${r.capacity}，固件: ${r.version}`);
  });

  $("smokeBtn").onclick = () => run($("smokeBtn"), async () => {
    log("INFO", "开始只读巡检...");
    const rows = await api("/api/smoke", {});
    const panel = $("infoPanel");
    panel.innerHTML = "";
    rows.forEach((r) => {
      const div = document.createElement("div");
      if (r.ok === false) div.className = "bad";
      div.innerHTML = `<b>${r.label}</b>${r.value}`;
      panel.appendChild(div);
      log(r.ok ? "OK" : "WARN", `${r.label}：${r.value}`);
    });
    $("infoCard").classList.remove("hidden");
    // Update status cards
    rows.forEach((r) => {
      const map = { "电量": "statBattery", "容量": "statCapacity", "固件": "statFirmware", "录音状态": "statRecording" };
      const elId = map[r.label];
      if (elId) $(elId).textContent = r.value;
    });
    await loadFiles();
  });

  $("synctimeBtn").onclick = () => run($("synctimeBtn"), async () => {
    const r = await api("/api/synctime", {});
    log("OK", r.message);
  });

  // ============ Files ============
  async function loadFiles() {
    const files = await api("/api/status").then((s) => s.files);
    renderFiles(files);
  }

  function renderFiles(files) {
    const tbody = $("fileRows");
    tbody.innerHTML = "";
    const tableWrap = $("deviceTableWrap");
    if (!files.length) {
      tableWrap.classList.add("empty");
      return;
    }
    tableWrap.classList.remove("empty");
    files.forEach((f) => {
      const tr = document.createElement("tr");
      [String(f.index), f.name, f.time_text, f.size_text].forEach((v, i) => {
        const td = document.createElement("td");
        td.textContent = v;
        if (i === 1) td.className = "mono";
        tr.appendChild(td);
      });
      const td = document.createElement("td");
      td.style.whiteSpace = "nowrap";
      const mkBtn = (text, onclick, cls) => {
        const b = document.createElement("button");
        b.textContent = text;
        b.className = `btn btn-sm btn-ghost ${cls || ""}`;
        b.style.marginRight = "6px";
        b.onclick = () => onclick(b);
        return b;
      };
      td.append(
        mkBtn("下载", (btn) => run(btn, () => download(f.index, 0))),
        mkBtn("转写", (btn) => run(btn, () => transcribe({ index: f.index }))),
        mkBtn("删除", (btn) => run(btn, () => deleteOne(f)), "danger")
      );
      tr.appendChild(td);
      tbody.appendChild(tr);
    });
    updateDlButtons();
  }

  $("filesBtn").onclick = () => run($("filesBtn"), async () => {
    if (currentView === "local") {
      // 本地视图：刷新 downloads 目录
      await loadLocal();
      const locals = await api("/api/local");
      log("OK", `本地文件：共 ${locals.length} 个`);
    } else {
      // 设备视图：刷新录音笔上的文件列表（需要连接）
      log("INFO", "拉取设备文件列表...");
      const files = await api("/api/files");
      renderFiles(files);
      log("OK", `共 ${files.length} 个文件`);
    }
  });

  async function download(index, offset) {
    _dlFilename = "";
    showProgress(0, 0);
    let r;
    try {
      r = await api("/api/download", { index, offset: offset || 0 });
    } catch (err) {
      _dlActive = false;
      $("progressWrap").classList.add("hidden");
      updateDlButtons();
      throw err;
    }
    _dlActive = false;
    updateDlButtons();
    // 下载完成，进度条显示 100% 后淡出
    showProgress(1, 1);
    _dlFilename = "";
    setTimeout(() => $("progressWrap").classList.add("hidden"), 1500);
    let kind = "原始码流";
    if (r.is_wav && r.wav) {
      kind = `WAV ${r.wav.sample_rate}Hz ${r.wav.bits}bit ${r.wav.channels}ch` +
        (r.wav.ok ? "（声明长度一致）" : "（声明长度不一致！）");
    }
    const convTag = r.converted_from ? `  [Opus→WAV 转换完成]` : "";
    log("OK", `下载完成：${r.local_name}  ${r.size_text}  ${kind}${convTag}`);
    playDing("ok");
    await loadLocal();

    // ============ 下载后自动转写 ============
    const autoT = $("autoTranscribeToggle").checked;
    const savedName = r.local_name;
    if (autoT && savedName) {
      // 用 toast 告知，避免用户以为卡住了
      toast(`已下载，开始自动转写：${savedName}`, "info", 2200);
      try {
        await transcribe({ name: savedName });
      } catch (err) {
        toast(`自动转写失败：${err.message || err}`, "error", 4000);
      }
    }
  }

  async function deleteOne(f) {
    if (_dlActive) {
      toast("下载进行中，无法删除文件，请等待下载完成", "warn", 3000);
      return;
    }
    if (!confirm(`确认删除设备上的 ${f.name}？此操作不可恢复`)) return;
    let r;
    try {
      r = await api("/api/delete", { index: f.index });
    } catch (err) {
      log("ERR", `删除失败：${err.message || err}`);
      toast(`删除失败：${err.message || err}`, "error", 3000);
      return;
    }
    log("OK", r.message);
    playDing(r.message.includes("失败") ? "error" : "ok");
    try {
      const files = await api("/api/files");
      renderFiles(files);
    } catch (e) { /* 列表刷新失败不阻塞 */ }
  }

  $("deleteAllBtn").onclick = () => run($("deleteAllBtn"), async () => {
    if (_dlActive) {
      toast("下载进行中，无法删除文件，请等待下载完成", "warn", 3000);
      return;
    }
    if (!confirm("确认删除设备上的全部录音？此操作不可恢复")) return;
    const r = await api("/api/deleteall", {});
    log("OK", r.message);
    renderFiles([]);
  });

  $("abortBtn") && ($("abortBtn").onclick = () => run($("abortBtn"), async () => {
    const r = await api("/api/abort", {});
    log("OK", r.message);
  }));

  // ============ Transcribe ============
  function currentTranscribeOptions() {
    return {
      model: $("modelSel").value,
      spk: $("spkToggle").checked,
      language: $("langSel").value,
      volume_gain: $("gainSel").value,
    };
  }

  function setLanguagesForModel(modelKey) {
    const status = STATE.status || {};
    const asrOpts = status.asr_options || {};
    const models = asrOpts.models || {};
    const spec = models[modelKey];
    const allowed = (spec && spec.languages) || ["auto", "zh", "en", "yue", "ja", "ko"];
    const langSel = $("langSel");
    const current = langSel.value;
    // 只保留允许的选项
    Array.from(langSel.options).forEach((opt) => {
      opt.disabled = allowed.indexOf(opt.value) < 0;
    });
    if (allowed.indexOf(current) < 0) langSel.value = "auto";
  }

  $("modelSel").onchange = () => {
    setLanguagesForModel($("modelSel").value);
  };

  async function transcribe(target) {
    const opts = currentTranscribeOptions();
    const payload = Object.assign({}, opts, target);
    const modelLabel = $("modelSel")
      .selectedOptions[0].textContent.trim()
      .replace(/[()（）]/g, "")
      .slice(0, 28);
    log("INFO", `转写（${modelLabel}｜${opts.spk ? "说话人分离·" : ""}lang=${opts.language}）`
          + " — 首次使用会自动下载并加载模型，请耐心等待...");
    const targetName = target && target.name;
    if (targetName) { BUSY_NAMES.add(targetName); loadLocal(); }
    try {
      const r = await api("/api/transcribe", payload);
      applyTranscribeResponse(r, opts);
      log("OK", `转写完成，文本已保存：${r.txt}`);
      playDing("ok");
    } catch (err) {
      throw err;
    } finally {
      if (targetName) { BUSY_NAMES.delete(targetName); }
      await loadLocal();
    }
  }

  // 把 /api/transcribe 或 /api/transcript 的响应应用到 UI
  function applyTranscribeResponse(r, opts) {
    opts = opts || { model: "", language: "auto" };
    $("transcribeModal").classList.remove("hidden");
    const metaParts = [];
    metaParts.push(`模型：${r.model || opts.model || "-"}`);
    metaParts.push(`语言：${r.language || opts.language || "-"}`);
    if (r.spk) {
      const mode = r.spk_mode;
      if (mode === "campplus")      metaParts.push("说话人：开（Campplus 真实分离）");
      else if (mode === "fallback") metaParts.push("说话人：开（角色 A/B，按段落交替）");
      else                           metaParts.push("说话人：开");
    } else {
      metaParts.push("说话人：关");
    }
    metaParts.push(`来源：${r.source || "-"}`);
    $("transcribeMeta").textContent = metaParts.join("  ·  ");

    TX.source = r.source;
    TX.segments = r.segments || [];
    TX.fullText = r.text || "";
    TX.activeIdx = -1;
    TX.editMode = false;
    TX.origText = TX.fullText;
    TX.spk_mode = r.spk_mode || (r.spk ? "fallback" : "off");

    // 配置音频播放器
    const audioWrap = $("transcribeAudioWrap");
    const audioEl = $("transcribeAudio");
    if (r.source) {
      TX.audioUrl = `/downloads/${encodeURIComponent(r.source)}`;
      audioEl.src = TX.audioUrl;
      audioWrap.classList.remove("hidden");
    } else {
      audioWrap.classList.add("hidden");
      TX.audioUrl = null;
    }
    // 重置倍速为 1x
    setSpeed(1);

    // 渲染分段
    exitEditMode(false);
    renderSegmentsView();
    bindAudioSync();
    bindSearch();
    // 清空搜索框
    const s = $("segSearchInput");
    if (s) s.value = "";
    const sc = $("segSearchCount");
    if (sc) sc.classList.add("hidden");
  }

  function renderSegmentsView() {
    const view = $("segmentsView");
    view.innerHTML = "";
    if (!TX.segments.length) {
      // 无分段数据：单段显示
      const seg = document.createElement("div");
      seg.className = "seg single";
      const text = TX.fullText || "（未识别到语音）";
      seg.innerHTML = `<div class="seg-text">${escapeHtml(text)}</div>`;
      view.appendChild(seg);
      return;
    }
    const speakersSeen = new Set();
    TX.segments.forEach((s, idx) => {
      const segEl = document.createElement("div");
      segEl.className = "seg";
      segEl.dataset.idx = idx;
      const hasSpeaker = typeof s.speaker === "number";
      if (hasSpeaker) speakersSeen.add(s.speaker);
      const speakerBadge = hasSpeaker
        ? `<span class="seg-spk spk-${s.speaker % 6}" title="${speakerTitle(s.speaker)}">${speakerLabel(s.speaker)}</span>`
        : "";
      segEl.innerHTML = `
        <div class="seg-time" data-idx="${idx}" title="点击跳转到此处">${formatMs(s.start)} → ${formatMs(s.end)}</div>
        <div style="flex:1; display:flex; flex-direction:column; gap:6px;">
          <div style="display:flex; gap:8px; align-items:center;">
            ${speakerBadge}
          </div>
          <div class="seg-text">${escapeHtml(s.text || "")}</div>
        </div>
      `;
      segEl.addEventListener("click", () => seekToSegment(idx));
      view.appendChild(segEl);
    });
  }

  function speakerLabel(n) {
    const spkMode = TX.spk_mode || "off";
    if (spkMode === "fallback") {
      // 退化方案：按段交替，用"角色 A/B/C"标注（非声纹识别，仅供参考）
      const letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ";
      return `🗣 角色 ${letters[n % letters.length]}`;
    }
    // campplus / 默认：数字编号 说话人 1/2...
    return `🗣 说话人 ${n + 1}`;
  }
  function speakerTitle(n) {
    const spkMode = TX.spk_mode || "off";
    if (spkMode === "fallback") return `角色 ${n + 1}（按段落交替分配，非声纹识别，仅供参考）`;
    return `说话人 ${n + 1}`;
  }

  // ============ 音频播放控件绑定 ============
  function bindAudioSync() {
    const audioEl = $("transcribeAudio");
    const posEl = $("audioPos");
    const playBtn = $("audioPlayBtn");
    const stopBtn = $("audioStopBtn");
    const progressWrap = $("audioProgress");
    const progressFill = $("audioProgressFill");
    if (!audioEl) return;

    // —— 播放 / 暂停按钮 ——
    if (playBtn) {
      playBtn.onclick = () => {
        if (audioEl.paused) { audioEl.play().catch(() => {}); }
        else                { audioEl.pause(); }
      };
    }
    // 播放/暂停时切换图标；播放时确保倍速生效
    audioEl.onplay  = () => {
      updatePlayIcon(false);
      try { audioEl.playbackRate = _pendingRate; } catch (e) {}
    };
    audioEl.onpause = () => updatePlayIcon(true);
    audioEl.onended = () => { updatePlayIcon(true); setActiveSegment(-1); };

    // —— 停止按钮 ——
    if (stopBtn) {
      stopBtn.onclick = () => {
        audioEl.pause();
        try { audioEl.currentTime = 0; } catch (e) {}
        updatePlayIcon(true);
        setActiveSegment(-1);
      };
    }

    // —— 进度条点击/拖动 seek ——
    if (progressWrap) {
      function seekFromEvent(e) {
        const rect = progressWrap.getBoundingClientRect();
        const x = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
        const pct = Math.max(0, Math.min(1, x / rect.width));
        if (Number.isFinite(audioEl.duration)) {
          audioEl.currentTime = pct * audioEl.duration;
        }
        if (progressFill) progressFill.style.width = (pct * 100) + "%";
      }
      progressWrap.onmousedown = (e) => { AUDIO_DRAG.dragging = true; AUDIO_DRAG.seekFn = seekFromEvent; seekFromEvent(e); };
      progressWrap.ontouchstart = (e) => { AUDIO_DRAG.dragging = true; AUDIO_DRAG.seekFn = seekFromEvent; seekFromEvent(e); };
      progressWrap.ontouchmove  = (e) => { if (AUDIO_DRAG.dragging) { e.preventDefault(); seekFromEvent(e); } };
      progressWrap.ontouchend   = () => { AUDIO_DRAG.dragging = false; };
    }

    // —— 时间 / 进度刷新 ——
    function refreshPos() {
      if (posEl) {
        const dur = Number.isFinite(audioEl.duration) ? audioEl.duration * 1000 : 0;
        const cur = audioEl.currentTime * 1000;
        posEl.textContent = `${formatMs(cur)} / ${formatMs(dur)}`;
      }
      if (progressFill && Number.isFinite(audioEl.duration) && audioEl.duration > 0) {
        progressFill.style.width = (audioEl.currentTime / audioEl.duration * 100) + "%";
      }
    }

    // —— 分段高亮同步 ——
    audioEl.ontimeupdate = () => {
      refreshPos();
      if (!TX.segments || !TX.segments.length) return;
      const t = audioEl.currentTime * 1000;
      let newIdx = -1;
      for (let i = 0; i < TX.segments.length; i++) {
        const s = TX.segments[i];
        if (t >= s.start && t <= s.end) { newIdx = i; break; }
      }
      if (newIdx !== TX.activeIdx) setActiveSegment(newIdx);
    };
    audioEl.onloadedmetadata = () => refreshPos();

    refreshPos();
  }

  function updatePlayIcon(showPlay) {
    const playIcon = document.querySelector("#audioPlayBtn .icon-play");
    const pauseIcon = document.querySelector("#audioPlayBtn .icon-pause");
    if (playIcon)  playIcon.classList.toggle("hidden", !showPlay);
    if (pauseIcon) pauseIcon.classList.toggle("hidden",  showPlay);
  }

  // 滚动节流：200ms 内最多一次 smooth scroll，避免频繁跳转、抖动
  let _lastSegScrollAt = 0;
  function setActiveSegment(idx) {
    TX.activeIdx = idx;
    const view = $("segmentsView");
    if (!view) return;
    const els = view.querySelectorAll(".seg");
    let targetEl = null;
    els.forEach((el) => {
      const i = Number(el.dataset.idx);
      const isActive = i === idx;
      el.classList.toggle("active", isActive);
      if (isActive) targetEl = el;
    });
    if (!targetEl) return;
    // 计算元素在滚动容器内的相对位置（不用 offsetTop：它是相对 offsetParent，不一定是 view）
    const vRect = view.getBoundingClientRect();
    const eRect = targetEl.getBoundingClientRect();
    const relTop = (eRect.top - vRect.top) + view.scrollTop;
    const centerTarget = Math.max(0, relTop - view.clientHeight / 2 + eRect.height / 2);
    const now = Date.now();
    const within = (eRect.top >= vRect.top + 6) && (eRect.bottom <= vRect.bottom - 6);
    if (within) return; // 元素已经完全在可视区，不用滚
    // 600ms 内用 smooth，超过 600ms 用 auto（快速 seek/跳段时不晃）
    const smooth = (now - _lastSegScrollAt) < 600 ? "smooth" : "smooth";
    try {
      view.scrollTo({ top: centerTarget, behavior: smooth });
    } catch (_) {
      view.scrollTop = centerTarget;
    }
    _lastSegScrollAt = now;
  }

  function seekToSegment(idx) {
    const audioEl = $("transcribeAudio");
    if (!audioEl || !TX.segments[idx]) return;
    const startSec = TX.segments[idx].start / 1000;
    try {
      audioEl.currentTime = Math.max(0, startSec);
      if (audioEl.paused) audioEl.play().catch(() => {});
    } catch (e) {}
  }

  // ============ 倍速控制（直接绑定到按钮） ============
  let _pendingRate = 1;
  function setSpeed(rate) {
    _pendingRate = rate;
    const audioEl = $("transcribeAudio");
    if (audioEl) {
      try {
        audioEl.playbackRate = Number(rate) || 1;
        // 部分浏览器需要 preservesPitch 也设一下
        if (audioEl.preservesPitch !== undefined) audioEl.preservesPitch = true;
      } catch (e) {}
      // 如果还没加载完，在 canplay 时再设一次
      audioEl.addEventListener("canplay", function applyRate() {
        try { audioEl.playbackRate = Number(rate) || 1; } catch (e) {}
        audioEl.removeEventListener("canplay", applyRate);
      });
    }
    const btns = document.querySelectorAll("#speedGroup .seg-speed");
    btns.forEach(b => { b.classList.toggle("active", Number(b.dataset.rate) === Number(rate)); });
  }
  (function bindSpeedButtons() {
    const btns = document.querySelectorAll("#speedGroup .seg-speed");
    if (!btns.length) {
      setTimeout(bindSpeedButtons, 200);
      return;
    }
    btns.forEach((btn) => {
      btn.onclick = () => setSpeed(Number(btn.dataset.rate));
    });
  })();

  // ============ 复制下拉 ============
  (function bindCopyMenu() {
    const dd = $("copyDropdown");
    const menu = $("copyMenu");
    if (!dd || !menu) return;
    const btn = $("copyBtn");
    btn.onclick = (e) => { e.stopPropagation(); menu.classList.toggle("show"); };
    document.addEventListener("click", () => menu.classList.remove("show"));
    menu.addEventListener("click", (e) => { e.stopPropagation(); });
    menu.querySelectorAll(".dd-item").forEach((it) => {
      it.onclick = async () => {
        const kind = it.dataset.copy || "plain";
        const text = buildCopyText(kind);
        try {
          await copyText(text);
          toast(`已复制${copyKindLabel(kind)}`, "success", 1500);
        } catch (err) {
          toast("复制失败：" + (err.message || err), "error");
        }
        menu.classList.remove("show");
      };
    });
  })();
  function copyKindLabel(k) {
    return ({
      plain: "纯文本",
      spk:   "（带说话人）",
      ts:    "（带时间戳）",
      full:  "（说话人+时间戳）",
    })[k] || "文本";
  }
  function buildCopyText(kind) {
    const src = TX.editMode ? $("transcribeEdit").value : TX.fullText;
    if (!kind || kind === "plain") return src || "";
    if (!TX.segments || !TX.segments.length) return src || "";
    return TX.segments.map((s, i) => {
      const spk = (typeof s.speaker === "number") ? (speakerLabel(s.speaker) + " ") : "";
      const ts = `[${formatMs(s.start)}]`;
      const body = (s.text || "").trim();
      switch (kind) {
        case "spk":  return `${spk}${body}`.trim();
        case "ts":   return `${ts} ${body}`.trim();
        case "full": return `${ts} ${spk}${body}`.trim();
        default:     return body;
      }
    }).join("\n");
  }
  async function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      try { return await navigator.clipboard.writeText(text || ""); }
      catch (e) { /* fallthrough */ }
    }
    const ta = document.createElement("textarea");
    ta.value = text || ""; ta.style.position = "fixed";
    ta.style.left = "-9999px"; document.body.appendChild(ta);
    ta.select(); document.execCommand("copy"); document.body.removeChild(ta);
  }

  // ============ 重新转写按钮 ============
  $("reTranscribeBtn").onclick = async () => {
    if (!TX.source) { toast("没有正在查看的文件", "warn"); return; }
    if (!confirm(`用当前模型重新转写「${TX.source}」？\n之前的转写结果会被覆盖。`)) return;
    try {
      BUSY_NAMES.add(TX.source);
      loadLocal();
      await transcribe({ name: TX.source });
    } catch (err) {
      toast("重新转写失败：" + (err.message || err), "error");
    } finally {
      BUSY_NAMES.delete(TX.source);
      loadLocal();
    }
  };

  // ============ 关闭转写结果卡片 ============
  $("transcribeFullscreenBtn").onclick = () => {
    $("transcribeModal").classList.toggle("is-fullscreen");
  };
  $("closeTranscribeBtn").onclick = async () => {
    $("transcribeModal").classList.add("hidden");
    $("transcribeModal").classList.remove("is-fullscreen");
    // 停止音频播放
    const audioEl = $("transcribeAudio");
    if (audioEl) { audioEl.pause(); audioEl.currentTime = 0; }
    // 清理该文件的 BUSY 标记（说明转写已经结束/被用户关闭）
    if (TX.source) { BUSY_NAMES.delete(TX.source); }
    TX.source = null; TX.fullText = ""; TX.segments = []; TX.activeIdx = -1;
    await loadLocal();
  };

  // ============ 结果搜索 + 高亮 + 定位 ============
  function bindSearch() {
    const input = $("segSearchInput");
    const count = $("segSearchCount");
    const view = $("segmentsView");
    if (!input || !view) return;
    let curIdx = 0, hits = [];
    function runSearch() {
      const q = (input.value || "").trim().toLowerCase();
      clearHits();
      hits = []; curIdx = 0;
      if (!q) { if (count) count.classList.add("hidden"); return; }
      const nodes = view.querySelectorAll(".seg-text");
      nodes.forEach((el, i) => {
        const raw = el.dataset.orig || el.textContent;
        if (!el.dataset.orig) el.dataset.orig = raw;
        const lower = raw.toLowerCase();
        let p = lower.indexOf(q);
        if (p < 0) { el.innerHTML = escapeHtml(raw); return; }
        let out = "", from = 0;
        while (p >= 0) {
          out += escapeHtml(raw.slice(from, p)) + `<mark class="hit" data-hi="${hits.length}">`
               + escapeHtml(raw.slice(p, p + q.length)) + `</mark>`;
          hits.push({ el, idx: i, pos: p });
          from = p + q.length;
          p = lower.indexOf(q, from);
        }
        out += escapeHtml(raw.slice(from));
        el.innerHTML = out;
      });
      if (count) {
        if (!hits.length) { count.textContent = "0 结果"; count.className = "search-count zero"; }
        else              { count.textContent = `${1}/${hits.length}`; count.className = "search-count"; }
        count.classList.remove("hidden");
      }
      if (hits.length) focusHit(0, true);
    }
    function clearHits() {
      view.querySelectorAll(".seg-text").forEach((el) => {
        if (el.dataset.orig) el.innerHTML = escapeHtml(el.dataset.orig);
      });
    }
    function focusHit(i, smooth) {
      if (!hits.length) return;
      i = (i + hits.length) % hits.length;
      curIdx = i;
      hits.forEach(h => h.el.querySelectorAll("mark.hit").forEach(m => m.classList.remove("cur")));
      const m = hits[i].el.querySelectorAll("mark.hit")[countIndexAt(hits[i], i)];
      if (m) {
        m.classList.add("cur");
        m.scrollIntoView({ block: "center", behavior: smooth ? "smooth" : "auto" });
      }
      if (count) count.textContent = `${i + 1}/${hits.length}`;
    }
    // 计算某个 hits 条目对应命中在同元素内的第几个 <mark>
    function countIndexAt(hit, globalIdx) {
      let local = 0;
      for (let k = 0; k < globalIdx; k++) {
        if (hits[k].el === hit.el) local++;
      }
      return local;
    }
    input.oninput = debounce(runSearch, 120);
    input.onkeydown = (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (!hits.length) { runSearch(); return; }
        focusHit(curIdx + (e.shiftKey ? -1 : 1), true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        input.value = "";
        runSearch();
      }
    };
  }
  function debounce(fn, ms) {
    let t; return function (...args) { clearTimeout(t); t = setTimeout(() => fn.apply(this, args), ms); };
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function formatMs(ms) {
    if (ms == null || isNaN(ms)) return "--:--";
    const totalSec = Math.floor(ms / 1000);
    const m = Math.floor(totalSec / 60);
    const s = totalSec % 60;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  }
  function formatMs1(ms) {
    if (ms == null || isNaN(ms)) return "00:00.0";
    const totalSec = ms / 1000;
    const m = Math.floor(totalSec / 60);
    const s = totalSec - m * 60;
    return `${String(m).padStart(2, "0")}:${s.toFixed(1).padStart(4, "0")}`;
  }

  // ============ 音频剪辑编辑器 ============
  const CLIP = { name: "", duration: 0, start: 0, end: 0, rate: 1 };

  function openClipEditor(name) {
    CLIP.name = name;
    CLIP.start = 0;
    CLIP.end = 0;
    CLIP.rate = 1;
    const modal = $("clipModal");
    const audio = $("clipAudio");
    const info = $("clipInfo");
    const nameInput = $("clipNameInput");
    nameInput.value = "";
    modal.classList.remove("hidden");
    info.textContent = "加载中…";
    // 重置 region 显示
    $("clipRegion").style.left = "0%";
    $("clipRegion").style.width = "100%";
    // 倍速按钮重置为 1×
    document.querySelectorAll(".clip-speed").forEach(b => {
      b.classList.toggle("active", Number(b.dataset.rate) === 1);
    });
    audio.src = `/downloads/${encodeURIComponent(name)}`;
    audio.playbackRate = CLIP.rate;
    audio.onloadedmetadata = () => {
      CLIP.duration = audio.duration * 1000;
      CLIP.start = 0;
      CLIP.end = CLIP.duration;
      info.textContent = `${name}  ·  总时长 ${formatMs1(CLIP.duration)}`;
      updateClipDisplay();
    };
    audio.ontimeupdate = () => {
      const cur = audio.currentTime * 1000;
      if (cur >= CLIP.end) { audio.pause(); updateClipPlayIcon(true); }
      updateClipProgress();
    };
    audio.onended = () => updateClipPlayIcon(true);
  }

  function updateClipDisplay() {
    $("clipStartVal").textContent = formatMs1(CLIP.start);
    $("clipEndVal").textContent = formatMs1(CLIP.end);
    $("clipDurVal").textContent = formatMs1(CLIP.end - CLIP.start);
    const dur = CLIP.duration || 1;
    $("clipRegion").style.left  = (CLIP.start / dur * 100) + "%";
    $("clipRegion").style.width = ((CLIP.end - CLIP.start) / dur * 100) + "%";
  }

  function updateClipProgress() {
    const dur = CLIP.duration || 1;
    const cur = $("clipAudio").currentTime * 1000;
    // 在 timeline 上显示播放头
    let head = document.getElementById("clipPlayhead");
    if (!head) {
      head = document.createElement("div");
      head.id = "clipPlayhead";
      head.className = "clip-playhead";
      $("clipTimeline").appendChild(head);
    }
    head.style.left = (cur / dur * 100) + "%";
  }

  function updateClipPlayIcon(showPlay) {
    const playBtn = $("clipPlayBtn");
    if (showPlay) {
      playBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg> 试听片段`;
    } else {
      playBtn.innerHTML = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg> 暂停`;
    }
  }

  // 拖动选区手柄
  (function bindClipHandles() {
    const timeline = $("clipTimeline");
    const region = $("clipRegion");
    const handleL = $("clipHandleL");
    const handleR = $("clipHandleR");
    if (!timeline) { setTimeout(bindClipHandles, 300); return; }

    function msFromPct(pct) {
      return Math.max(0, Math.min(CLIP.duration, pct / 100 * CLIP.duration));
    }
    function pctFromX(clientX) {
      const rect = timeline.getBoundingClientRect();
      return Math.max(0, Math.min(100, (clientX - rect.left) / rect.width * 100));
    }

    // 拖动左/右手柄
    function bindHandle(handle, isLeft) {
      handle.onmousedown = (e) => { e.stopPropagation(); startDrag(e, isLeft); };
      handle.ontouchstart = (e) => { e.stopPropagation(); startDrag(e.touches[0], isLeft); };
    }
    function startDrag(e, isLeft) {
      const onMove = (ev) => {
        const cx = (ev.touches ? ev.touches[0].clientX : ev.clientX);
        const pct = pctFromX(cx);
        const ms = msFromPct(pct);
        if (isLeft) {
          CLIP.start = Math.min(ms, CLIP.end - 100);
        } else {
          CLIP.end = Math.max(ms, CLIP.start + 100);
        }
        updateClipDisplay();
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        document.removeEventListener("touchmove", onMove);
        document.removeEventListener("touchend", onUp);
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onUp);
    }
    bindHandle(handleL, true);
    bindHandle(handleR, false);

    // 点击/拖动 timeline 空白处：移动播放头（seek）
    function seekToX(clientX) {
      const pct = pctFromX(clientX);
      const ms = msFromPct(pct);
      const audio = $("clipAudio");
      if (audio && CLIP.duration) {
        audio.currentTime = ms / 1000;
        updateClipProgress();
      }
      // 显示时间提示气泡
      const tl = $("clipTimeline");
      let tip = document.getElementById("clipSeekTip");
      if (!tip) {
        tip = document.createElement("div");
        tip.id = "clipSeekTip";
        tip.className = "clip-seek-tip";
        tl.appendChild(tip);
      }
      tip.textContent = formatMs1(ms);
      tip.style.left = pct + "%";
      tip.classList.add("visible");
    }
    let seeking = false;
    timeline.onmousedown = (e) => {
      if (e.target === handleL || e.target === handleR ||
          handleL.contains(e.target) || handleR.contains(e.target)) return;
      seeking = true;
      seekToX(e.clientX);
      e.preventDefault();
    };
    document.addEventListener("mousemove", (e) => {
      if (seeking) seekToX(e.clientX);
    });
    document.addEventListener("mouseup", () => {
      seeking = false;
      const tip = document.getElementById("clipSeekTip");
      if (tip) tip.classList.remove("visible");
    });
    // 触摸
    timeline.ontouchstart = (e) => {
      if (e.target === handleL || e.target === handleR ||
          handleL.contains(e.target) || handleR.contains(e.target)) return;
      seeking = true;
      seekToX(e.touches[0].clientX);
    };
    document.addEventListener("touchmove", (e) => {
      if (seeking) { e.preventDefault(); seekToX(e.touches[0].clientX); }
    }, { passive: false });
    document.addEventListener("touchend", () => { seeking = false; });
  })();

  // 试听 / 暂停
  $("clipPlayBtn").onclick = () => {
    const audio = $("clipAudio");
    if (!audio.src || !CLIP.duration) return;
    if (audio.paused) {
      // 如果当前位置不在选区内，跳到起点
      if (audio.currentTime * 1000 < CLIP.start || audio.currentTime * 1000 >= CLIP.end) {
        audio.currentTime = CLIP.start / 1000;
      }
      audio.play().catch(() => {});
      updateClipPlayIcon(false);
    } else {
      audio.pause();
      updateClipPlayIcon(true);
    }
  };
  $("clipStopBtn").onclick = () => {
    const audio = $("clipAudio");
    audio.pause();
    audio.currentTime = CLIP.start / 1000;
    updateClipPlayIcon(true);
  };

  // 倍速
  document.querySelectorAll(".clip-speed").forEach((btn) => {
    btn.onclick = () => {
      CLIP.rate = Number(btn.dataset.rate);
      $("clipAudio").playbackRate = CLIP.rate;
      document.querySelectorAll(".clip-speed").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
    };
  });

  // 关闭
  $("clipCloseBtn").onclick = () => {
    $("clipAudio").pause();
    $("clipModal").classList.add("hidden");
  };

  // 导出
  $("clipExportBtn").onclick = () => run($("clipExportBtn"), async () => {
    if (!CLIP.name || !CLIP.duration) { toast("音频未加载", "warn"); return; }
    const customName = $("clipNameInput").value.trim();
    const payload = {
      name: CLIP.name,
      start_ms: Math.round(CLIP.start),
      end_ms: Math.round(CLIP.end),
    };
    if (customName) payload.out_name = customName;
    try {
      const r = await api("/api/clip_audio", payload);
      toast(`已导出：${r.out_name}（${formatMs1(r.duration_ms)}）`, "success");
      await loadLocal();
    } catch (err) {
      toast("导出失败：" + (err.message || err), "error");
    }
  });

  // ============ Edit mode ============
  $("editBtn").onclick = () => {
    TX.editMode = true;
    $("segmentsView").classList.add("hidden");
    $("editView").classList.remove("hidden");
    $("editBtn").classList.add("hidden");
    $("copyBtn").classList.add("hidden");
    $("downloadTxtBtn").classList.add("hidden");
    $("saveBtn").classList.remove("hidden");
    $("cancelBtn").classList.remove("hidden");
    $("transcribeEdit").value = TX.fullText;
    TX.origText = TX.fullText; // snapshot for diff check
    $("transcribeEdit").focus();
  };

  function exitEditMode(discard) {
    TX.editMode = false;
    $("editView").classList.add("hidden");
    $("segmentsView").classList.remove("hidden");
    $("editBtn").classList.remove("hidden");
    $("copyBtn").classList.remove("hidden");
    $("downloadTxtBtn").classList.remove("hidden");
    $("saveBtn").classList.add("hidden");
    $("cancelBtn").classList.add("hidden");
    if (!discard) {
      renderSegmentsView();
    }
  }

  $("cancelBtn").onclick = () => {
    exitEditMode(true);
  };

  $("saveBtn").onclick = () => run($("saveBtn"), async () => {
    const text = $("transcribeEdit").value;
    if (text === TX.origText) {
      toast("内容未变化，无需保存", "info", 1600);
      exitEditMode(false);
      return;
    }
    if (!TX.source) { log("ERR", "缺少源文件名，无法保存"); toast("缺少源文件，无法保存", "error"); return; }
    const r = await api("/api/save_transcript", { name: TX.source, text });
    TX.fullText = text;
    TX.origText = text;
    const delta = text.length - (TX._origLen || 0);
    TX._origLen = text.length;
    toast(`已保存修改（${r.length} 字，${delta >= 0 ? "+" : ""}${delta} 字）`, "success");
    log("OK", `已保存到 ${r.txt}（${r.length} 字）`);
    if (TX.segments.length) {
      const paragraphs = text.split(/\n+/).filter(Boolean);
      if (paragraphs.length === TX.segments.length) {
        TX.segments = TX.segments.map((s, i) => Object.assign({}, s, { text: paragraphs[i] }));
      } else {
        const cleaned = text.trim();
        const pieces = splitIntoN(cleaned, TX.segments.length);
        TX.segments = TX.segments.map((s, i) => Object.assign({}, s, { text: pieces[i] || "" }));
      }
    }
    renderSegmentsView();
    exitEditMode(false);
    await loadLocal();
  });

  function splitIntoN(text, n) {
    if (!text) return new Array(n).fill("");
    if (n <= 1) return [text];
    // 按句号/问号/感叹号/换行切分后均匀分配（兼容旧浏览器，不用 lookbehind）
    const sentences = text.split(/[。！？!?.\n]/).map(s => s.trim()).filter(Boolean);
    if (sentences.length <= n) {
      const total = text.length;
      const per = Math.ceil(total / n);
      const out = [];
      for (let i = 0; i < n; i++) {
        out.push(text.slice(i * per, (i + 1) * per));
      }
      return out;
    }
    const out = [];
    const base = Math.floor(sentences.length / n);
    let extra = sentences.length % n;
    let idx = 0;
    for (let i = 0; i < n; i++) {
      const take = base + (extra-- > 0 ? 1 : 0);
      out.push(sentences.slice(idx, idx + take).join(""));
      idx += take;
    }
    return out;
  }

  // ============ Copy & Download ============
  $("copyBtn").onclick = () => {
    const text = TX.editMode ? $("transcribeEdit").value : TX.fullText;
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      const btn = $("copyBtn");
      const orig = btn.textContent;
      btn.textContent = "已复制";
      setTimeout(() => btn.textContent = orig, 1500);
    });
  };

  $("downloadTxtBtn").onclick = () => {
    if (!TX.source) return;
    const safe = TX.source.replace(/\.[^.]+$/, "") + ".txt";
    const url = `/downloads/${encodeURIComponent(safe)}`;
    const a = document.createElement("a");
    a.href = url;
    a.download = safe;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  $("bulkTranscribeBtn").onclick = () => run($("bulkTranscribeBtn"), async () => {
    const locals = await api("/api/local");
    const wavs = locals.filter((f) => ["wav", "opus", "mp3", "m4a", "flac"]
      .indexOf(f.kind) >= 0);
    if (!wavs.length) {
      log("WARN", "没有可转写的本地音频文件，请先下载录音");
      return;
    }
    if (!confirm(`对 ${wavs.length} 个音频执行批量转写？\n`
               + "（模型切换会触发重新加载，首次请耐心等待）")) return;
    const opts = currentTranscribeOptions();
    let ok = 0, fail = 0;
    let combinedText = "";
    for (const f of wavs) {
      try {
        log("INFO", `[${ok + fail + 1}/${wavs.length}] 转写 ${f.name} ...`);
        const r = await api("/api/transcribe", Object.assign({}, opts, { name: f.name }));
        combinedText += `===== ${f.name} =====\n${r.text || ""}\n\n`;
        ok++;
        log("OK", `  ✓ 已保存：${r.txt}`);
      } catch (e) {
        fail++;
        log("ERR", `  ✗ ${f.name} 失败：${e.message || e}`);
      }
    }
    if (combinedText) {
      $("transcribeModal").classList.remove("hidden");
      $("transcribeMeta").textContent = `批量转写结果：成功 ${ok}，失败 ${fail}｜模型：${opts.model}`;
      // 批量结果：用单个 seg 显示
      TX.source = null;
      TX.audioUrl = null;
      TX.segments = [{ start: 0, end: 0, text: combinedText.trim() }];
      TX.fullText = combinedText.trim();
      TX.activeIdx = -1;
      TX.editMode = false;
      renderSegmentsView();
      const audioWrap = $("transcribeAudioWrap");
      audioWrap.classList.add("hidden");
    }
    await loadLocal();
  });

  // ============ Local Files ============
  const AUDIO_KINDS = ["wav", "opus", "mp3", "m4a", "flac"];
  async function loadLocal() {
    const files = await api("/api/local");
    const list = $("localList");
    list.innerHTML = "";
    const empty = $("localEmpty");
    // 只渲染音频主文件。
    // 附属文件（.txt / .transcript.json / .summary.json / .md）永远不做独立卡片，
    // 只通过徽章 ✓ 已转写 / 🤖 已摘要 / 📄 已导 MD 体现。
    const audioFiles = files.filter(f => AUDIO_KINDS.indexOf(f.kind) >= 0);
    const renderFiles = audioFiles;
    if (!renderFiles.length) {
      list.classList.add("hidden");
      empty.classList.remove("hidden");
      empty.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1" stroke-linecap="round" stroke-linejoin="round" width="64" height="64"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <p class="es-title">本地暂无文件</p>
        <p class="es-desc">在「设备」tab 选择录音文件下载，或
          <a onclick="document.querySelector('[data-view=device]').click()" style="color:var(--accent);cursor:pointer;text-decoration:underline dotted;">点此切到设备视图</a>
        </p>
      `;
      return;
    }
    list.classList.remove("hidden");
    empty.classList.add("hidden");
    renderFiles.forEach((f) => {
      const div = document.createElement("div");
      div.className = "local-card";
      div.dataset.name = f.name;
      if (BUSY_NAMES.has(f.name)) div.classList.add("transcribing");
      const url = `/downloads/${encodeURIComponent(f.name)}`;
      const isAudio = AUDIO_KINDS.indexOf(f.kind) >= 0;
      // 后端新字段：has_transcript / has_summary / has_md；兼容旧字段 has_txt
      const hasTranscript = !!f.has_transcript || !!f.has_txt;
      const hasSummary    = !!f.has_summary;
      const hasMd         = !!f.has_md;
      // 兼容 folder 字段：有 folder 时，显示名取 basename，folder 放 chip
      const displayName = f.display_name || (f.name.includes("/") || f.name.includes("\\")
        ? f.name.split(/[\\/]/).pop()
        : f.name);
      const folder = f.folder || "";
      const kindIcon = {
        wav: "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
        mp3: "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z M21 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
        opus: "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
        m4a: "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
        flac: "M9 18V5l12-2v13 M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0z",
        txt: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6 M8 13h8 M8 17h8 M8 9h2",
      };
      const svgPath = kindIcon[f.kind] || "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z";
      const head = document.createElement("div");
      head.className = "local-item-head";
      head.innerHTML = `
        <div class="file-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="${svgPath}"/></svg></div>
        <div style="flex:1; min-width:0;">
          <div class="local-item-title name-el" title="${escapeHtml(f.name)}">${escapeHtml(displayName)}</div>
          <div class="local-item-meta">
            <span>${f.size_text || ""}</span>
            <span class="badge">${(f.kind || "file").toUpperCase()}</span>
            ${folder ? `<span class="folder-chip" title="归档到：${escapeHtml(folder)}">📁 ${escapeHtml(folder)}</span>` : ""}
            ${BUSY_NAMES.has(f.name) ? `<span class="busy-tag">转写中…</span>` : ""}
            ${isAudio && hasTranscript ? `<span class="badge has-txt" title="已有转写结果，可直接查看">✓ 已转写</span>` : ""}
            ${isAudio && hasSummary    ? `<span class="badge has-summary" title="已有 AI 摘要">🤖 已摘要</span>` : ""}
            ${isAudio && hasMd         ? `<span class="badge has-md" title="已导出 Markdown">📄 已导 MD</span>` : ""}
          </div>
        </div>
        <div class="local-card-actions card-action-menu">
          <button class="btn-icon" title="归档到日期文件夹" data-act="archive" ${folder ? "disabled style=\"opacity:.35;cursor:not-allowed;\"" : ""}>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/></svg>
          </button>
          <button class="btn-icon" title="重命名" data-act="rename">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
          </button>
          <button class="btn-icon" title="在文件夹中显示" data-act="open">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          </button>
          <button class="btn-icon danger" title="删除文件（连同转写结果）" data-act="delete">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a2 2 0 0 1 2-2h2a2 2 0 0 1 2 2v2"/></svg>
          </button>
        </div>
      `;
      div.appendChild(head);

      // 音频卡片：加播放器 + 主要按钮（已转写优先展示"查看"，否则"转写"）
      if (isAudio) {
        const actions = document.createElement("div");
        actions.className = "local-item-actions";
        const audio = document.createElement("audio");
        audio.controls = true;
        audio.preload = "metadata";
        audio.src = url;
        audio.title = f.name;
        const btn = document.createElement("button");
        btn.className = "btn btn-sm " + (hasTranscript ? "btn-primary" : "btn-ghost");
        btn.textContent = hasTranscript ? "查看结果" : "立即转写";
        btn.title = hasTranscript ? `查看已有的转写结果（不重新跑模型）`
                                  : `用当前选择的模型转写 ${f.name}`;
        btn.onclick = () => {
          if (BUSY_NAMES.has(f.name)) {
            // 不硬拦——给提示但仍允许尝试查看（用户手动关闭卡片后 BUSY 可能没清干净）
            toast("该文件可能仍在转写中，已尝试载入结果", "warn", 1800);
          }
          run(btn, () => openTranscribeOrView(f.name));
        };
        const clipBtn = document.createElement("button");
        clipBtn.className = "btn btn-sm btn-ghost";
        clipBtn.textContent = "✂ 剪辑";
        clipBtn.title = `剪辑 ${f.name}`;
        clipBtn.onclick = () => openClipEditor(f.name);

        // 新按钮：AI 摘要
        const sumBtn = document.createElement("button");
        sumBtn.className = "btn btn-sm summary-btn";
        sumBtn.textContent = "🤖 摘要";
        sumBtn.title = hasSummary ? "查看已有 AI 摘要（可重新生成）" : "调用 AI 生成结构化摘要 + 思维导图";
        sumBtn.onclick = () => openSummaryModal(f.name, false);

        // 新按钮：归档
        const archBtn = document.createElement("button");
        archBtn.className = "btn btn-sm archive-btn";
        archBtn.textContent = "📦 归档";
        archBtn.title = folder ? `已归档到 ${folder}` : "归档到今天日期文件夹（YYYY-MM-DD）";
        if (folder) {
          archBtn.disabled = true;
          archBtn.style.opacity = ".55";
          archBtn.style.cursor = "not-allowed";
        }
        archBtn.onclick = () => run(archBtn, () => doArchive(f.name));

        // 新按钮：导出 Markdown
        const mdBtn = document.createElement("button");
        mdBtn.className = "btn btn-sm export-md-btn";
        mdBtn.textContent = "📄 导出 MD";
        mdBtn.title = "导出 Markdown（转写全文 + AI 摘要）到下载目录";
        mdBtn.onclick = () => run(mdBtn, () => doExportMarkdown(f.name));

        actions.append(audio, btn, clipBtn, sumBtn, archBtn, mdBtn);
        div.appendChild(actions);
      }

      // 绑定：归档 / 重命名 / 打开文件夹 / 删除
      const archIcon = head.querySelector('[data-act="archive"]');
      if (archIcon && !archIcon.disabled) {
        archIcon.onclick = (e) => {
          e.stopPropagation();
          doArchive(f.name);
        };
      }
      head.querySelector('[data-act="rename"]').onclick = (e) => {
        e.stopPropagation();
        startRename(div, f.name, displayName);
      };
      head.querySelector('[data-act="open"]').onclick = async (e) => {
        e.stopPropagation();
        try { await api("/api/open_folder", { name: f.name }); }
        catch (err) { toast("打开文件夹失败：" + (err.message || err), "error"); }
      };
      head.querySelector('[data-act="delete"]').onclick = async (e) => {
        e.stopPropagation();
        if (!confirm(`删除 ${f.name}？\n（同时会删除对应的 转写 / 摘要 / Markdown）`)) return;
        try {
          const r = await api("/api/delete_local", { name: f.name });
          toast(`已删除：${(r.deleted || [f.name]).join("、")}`, "success");
          if (TX.source === f.name) {
            $("transcribeModal").classList.add("hidden");
            TX.source = null; TX.fullText = ""; TX.segments = [];
          }
          await loadLocal();
        } catch (err) {
          toast("删除失败：" + (err.message || err), "error");
        }
      };

      list.appendChild(div);
    });
  }

  // ============ 本地卡片重命名 inline ============
  function startRename(cardEl, oldName, displayNameHint) {
    const titleEl = cardEl.querySelector(".name-el");
    if (!titleEl) return;
    // oldName 可能带 folder：重命名只改 basename（不含日期目录前缀）
    const baseName = oldName.includes("/") || oldName.includes("\\")
      ? oldName.split(/[\\/]/).pop()
      : oldName;
    const stemIdx = baseName.lastIndexOf(".");
    const initVal = stemIdx > 0 ? baseName.slice(0, stemIdx) : baseName;
    const input = document.createElement("input");
    input.className = "name-edit";
    input.type = "text";
    input.value = initVal;
    input.title = "输入新文件名（不需要写后缀），回车确认，Esc 取消";
    titleEl.replaceWith(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (ok) => {
      if (done) return;
      done = true;
      if (!ok) { input.replaceWith(titleEl); return; }
      const newStem = input.value.trim();
      if (!newStem) { input.replaceWith(titleEl); return; }
      try {
        const r = await api("/api/rename_local", { name: oldName, new_name: newStem });
        toast(`已重命名：${baseName} → ${r.new_name}`, "success");
        if (TX.source === oldName) $("transcribeModal").classList.add("hidden");
        await loadLocal();
      } catch (err) {
        toast("重命名失败：" + (err.message || err), "error");
        input.replaceWith(titleEl);
      }
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter")       { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  // ============ openTranscribeOrView：根据是否有缓存决定走"查看"还是"转写"
  async function openTranscribeOrView(name) {
    const opts = currentTranscribeOptions();
    const locals = await api("/api/local");
    const f = locals.find(x => x.name === name);
    if (!f) { log("ERR", `找不到 ${name}`); return; }
    if (f.has_meta || f.has_txt) {
      try {
        const r = await api("/api/transcript", { name });
        applyTranscribeResponse(r, { model: opts.model, language: opts.language });
        toast("已载入转写结果（从缓存，不重新转写）", "info", 1800);
      } catch (err) {
        const msg = (err && err.message) ? err.message : String(err);
        log("WARN", `查看缓存转写失败：${msg}。请手动点「转写」按钮重新生成。`);
        toast(
          "载入缓存失败：" + msg + "。未自动转写，请确认无误后手动点击「转写」。",
          "warn", 5000
        );
      }
    } else {
      return transcribe({ name });
    }
  }

  // ============ Realtime ============
  document.querySelectorAll("[data-rt]").forEach((btn) => {
    btn.onclick = () => run(btn, async () => {
      if (btn.dataset.rt === "start") {
        $("rtBytes").textContent = "0 B";
        $("rtStatus").textContent = "进行中";
      }
      const r = await api("/api/rt", { action: btn.dataset.rt });
      log("OK", r.message);
      if (btn.dataset.rt === "pause") $("rtStatus").textContent = "已暂停";
      if (btn.dataset.rt === "resume") $("rtStatus").textContent = "进行中";
      if (btn.dataset.rt === "stop") { $("rtStatus").textContent = "待机"; await loadLocal(); }
    });
  });

  // ============ Recording ============
  document.querySelectorAll("[data-rec]").forEach((btn) => {
    btn.onclick = () => run(btn, async () => {
      const r = await api("/api/rec", { action: btn.dataset.rec });
      log("OK", r.message);
      const act = btn.dataset.rec;
      if (act === "state") $("recState").textContent = r.message;
      if (act === "time") $("recTime").textContent = r.message;
      if (act === "name") $("recName").textContent = r.message;
    });
  });

  // ============ Raw commands ============
  $("rawBtn").onclick = () => run($("rawBtn"), async () => {
    const r = await api("/api/raw", {
      type: Number($("rawType").value),
      cmd: Number($("rawCmd").value),
      params: $("rawParams").value.trim(),
    });
    log("OK", r.message);
  });

  $("rawFrameBtn").onclick = () => run($("rawFrameBtn"), async () => {
    const r = await api("/api/rawframe", { hex: $("rawFrame").value.trim() });
    log("OK", r.message);
  });

  $("clearLogBtn").onclick = () => { $("log").innerHTML = ""; logBuffer.length = 0; };
  $("exportLogBtn").onclick = exportLog;

  // ============ Protocol Self-Test ============
  $("selfTestBtn").onclick = () => {
    const lines = [];
    const check = (label, ok, detail) =>
      lines.push(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? `  ${detail}` : ""}`);

    // 1. CRC-16/XMODEM 标准向量
    const crc = crc16Xmodem([0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37, 0x38, 0x39]);
    check("CRC-16/XMODEM 标准向量 \"123456789\"", crc === 0x31c3, `actual=0x${crc.toString(16).padStart(4, "0")}`);

    // 2. 帧构造验证（协议 7.3 节示例）
    const data = new Uint8Array([2, 2, 0, 0, 0, 0, ...strBytes("note20260710-162938.wav", 24)]);
    const frame = buildFrame(3, data);
    const expected = "5a 03 9e 20 1e 00 02 02 00 00 00 00 6e 6f 74 65 32 30 32 36 30 37 31 30 2d 31 36 32 39 33 38 2e 77 61 76 00";
    check("2-2 下载请求帧（协议 7.3 节）", bytesToHex(frame) === expected, bytesToHex(frame));

    // 3. 帧头校验
    check("帧头 0x5A", frame[0] === 0x5a);
    check("SEQ 字段", frame[1] === 3);
    check("LEN 字段 LE", frame[4] === 0x1e && frame[5] === 0x00, `len=${frame[4] | (frame[5] << 8)}`);

    // 4. CRC 校验范围（LEN + DATA）
    const crcInput = new Uint8Array([frame[4], frame[5], ...data]);
    const recalc = crc16Xmodem(crcInput);
    const frameCrc = frame[2] | (frame[3] << 8);
    check("CRC 计算范围 = LEN + DATA", recalc === frameCrc, `calc=0x${recalc.toString(16).padStart(4, "0")} frame=0x${frameCrc.toString(16).padStart(4, "0")}`);

    // 5. 文件列表大端解码
    const listBody = new Uint8Array([
      0, 0, 0, 1,        // count = 1 (BE)
      0, 0, 0, 12,       // time = 12 (BE)
      0, 0, 13, 128,     // size = 3456 (BE)
      ...strBytes("note20260710-162938.", 20),
    ]);
    const parsed = parseFileList(listBody);
    check("文件列表 BE 解析 count=1", parsed.count === 1);
    check("文件列表 time=12", parsed.files[0]?.time === 12);
    check("文件列表 size=3456", parsed.files[0]?.size === 3456);

    // 6. WAV 头检查（严格符合 RIFF：size = 总长度 - 8）
    const wavHeader = new Uint8Array([
      0x52, 0x49, 0x46, 0x46, // RIFF
      0x30, 0x00, 0x00, 0x00, // size = 48（declared = 48 + 8 = 56 = 实际长度）
      0x57, 0x41, 0x56, 0x45, // WAVE
      0x66, 0x6d, 0x74, 0x20, // fmt
      0x10, 0x00, 0x00, 0x00, // fmt chunk size = 16
      0x01, 0x00,             // audio format = 1 (PCM)
      0x01, 0x00,             // channels = 1
      0x80, 0x3e, 0x00, 0x00, // sample rate = 16000
      0x00, 0x7d, 0x00, 0x00, // byte rate = 16000 * 2 = 32000
      0x02, 0x00,             // block align = 2
      0x10, 0x00,             // bits per sample = 16
      0x64, 0x61, 0x74, 0x61, // data
      0x0c, 0x00, 0x00, 0x00, // data size = 12
      0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, // 12 bytes of silence
    ]);
    const wav = inspectWav(wavHeader);
    check("WAV 头 RIFF 识别", wav.ok);
    check("WAV 采样率 16000Hz", wav.sampleRate === 16000);
    check("WAV 16bit", wav.bitsPerSample === 16);
    check("WAV 单声道", wav.channels === 1);
    check("WAV 声明长度一致", wav.declared === wavHeader.length, `declared=${wav.declared} actual=${wavHeader.length}`);

    const passed = lines.filter(l => l.startsWith("PASS")).length;
    const total = lines.length;
    lines.unshift(`协议自检结果：${passed}/${total} 通过`, "");
    lines.push("", passed === total ? "全部通过" : `${total - passed} 项未通过，请检查协议实现`);

    $("selfTestOutput").textContent = lines.join("\n");
    log("INFO", `协议自检完成：${passed}/${total} 通过。`);
  };

  // ============ Init ============
  initTheme();
  initNav();
  openWs();

  api("/api/status").then((s) => {
    STATE.status = s;
    setConnected(s.connected, s.mtu, s.payload);
    setLanguagesForModel($("modelSel").value);
    if (s.files && s.files.length) renderFiles(s.files);
    return loadLocal();
  }).catch((err) => log("ERR", err.message));

  log("INFO", "页面已加载。请先扫描并连接录音笔。");

  // ============================================================
  // ============ 新增：AI 摘要设置 / 摘要模态框 / 归档 / 导出MD
  // ============================================================

  // ---------- Provider 默认配置 ----------
  const PROVIDER_PRESETS = {
    deepseek: {
      base_url:   "https://api.deepseek.com",
      model_name: "deepseek-chat",
      hint:       "DeepSeek 官方 API，中文能力优秀，约 1元/百万 tokens。API Key 需要在 platform.deepseek.com 申请。",
    },
    ollama: {
      base_url:   "http://127.0.0.1:11434/v1",
      model_name: "qwen2.5:7b",
      hint:       "本地 Ollama 服务，完全离线零成本。推荐 16G 内存以上机器。使用前请 `ollama run qwen2.5:7b`。",
    },
    relay: {
      base_url:   "https://api.openai.com/v1",
      model_name: "gpt-4o-mini",
      hint:       "API 中转站/任意兼容 OpenAI 格式的网关（比如 OneAPI / NewAPI / siliconflow 等）。",
    },
  };

  // ---------- DOM 引用缓存 ----------
  const LLM = {
    modal:        () => $("llmConfigModal"),
    providerCards:() => document.querySelectorAll(".provider-card"),
    radios:       () => document.querySelectorAll('input[name="llm_provider"]'),
    baseUrl:      () => $("llm_base_url"),
    modelName:    () => $("llm_model_name"),
    apiKey:       () => $("llm_api_key"),
    temp:         () => $("llm_temperature"),
    tempVal:      () => $("llmTempVal"),
    timeout:      () => $("llm_timeout"),
    timeoutVal:   () => $("llmTimeoutVal"),
    enabled:      () => $("llm_enabled"),
    urlHint:      () => $("llmUrlHint"),
    statusLine:   () => $("llmStatusLine"),
  };

  const SUM = {
    modal:   () => $("summaryModal"),
    loading: () => $("summaryLoading"),
    loadingText: () => $("summaryLoadingText"),
    subtitle: () => $("summarySubtitle"),
    content: () => $("summaryContent"),
    tabs:    () => document.querySelectorAll(".s-tab"),
    panes:   () => document.querySelectorAll(".summary-pane"),
    svg:     () => $("markmapSvg"),
    empty:   () => $("mindmapEmpty"),
    refresh: () => $("summaryRefreshBtn"),
    copy:    () => $("summaryCopyBtn"),
    downloadBtn:  () => $("summaryDownloadBtn"),
    downloadGroup:() => $("summaryDownloadGroup"),
    downloadMenu: () => $("summaryDownloadMenu"),
    fullscreen:() => $("summaryFullscreenBtn"),
    mmToolEdit:   () => $("mmEditBtn"),
    mmToolReload: () => $("mmReloadBtn"),
    mmDownloadGroup:() => $("mmDownloadGroup"),
    mmEditModal:() => $("mmEditModal"),
    mmEditText: () => $("mmEditTextarea"),
    mmEditApply:() => $("mmEditApplyBtn"),
    mmEditReset:() => $("mmEditResetBtn"),
    mmEditClose:() => $("mmEditCloseBtn"),
  };

  // 摘要模态框状态
  let _summaryCtx = {
    name: null,        // 正在查看的源文件名（相对路径）
    lastData: null,    // 上一次拿到的 summary 对象（可被用户编辑后覆盖）
    markmapMM: null,   // markmap instance
    lastMd: null,      // 思维导图最后一次渲染用的 Markdown（编辑后保留）
    originalData: null,// 最近一次"已保存/已加载"的结构化摘要快照（用于 dirty 判定）
    originalMd: null,  // 最近一次"已保存/已加载"的思维导图 Markdown（用于 dirty 判定）
    dirty: false,      // 是否有未保存的编辑
    isFullscreen: false,
  };

  function _sumSnapshotEqual(a, b) {
    if (a === b) return true;
    if (!a || !b) return false;
    try {
      // 忽略 edited / edited_at / mindmap_md 这些辅助字段
      const strip = (o) => {
        if (!o || typeof o !== "object") return o;
        const out = Array.isArray(o) ? [] : {};
        for (const [k, v] of Object.entries(o)) {
          if (k === "edited" || k === "edited_at" || k === "mindmap_md") continue;
          out[k] = strip(v);
        }
        return out;
      };
      return JSON.stringify(strip(a)) === JSON.stringify(strip(b));
    } catch { return false; }
  }
  function _recomputeSummaryDirty() {
    const dataDirty = !_sumSnapshotEqual(_summaryCtx.lastData, _summaryCtx.originalData);
    const mdDirty = _summaryCtx.lastMd !== _summaryCtx.originalMd;
    _summaryCtx.dirty = dataDirty || mdDirty;
    const btn = document.getElementById("summarySaveBtn");
    if (btn) {
      btn.disabled = !_summaryCtx.dirty;
      btn.classList.toggle("btn-primary", !!_summaryCtx.dirty);
      btn.classList.toggle("btn-ghost", !_summaryCtx.dirty);
      btn.textContent = _summaryCtx.dirty ? "💾 保存 *" : "💾 保存";
    }
    const sub = document.getElementById("summarySubtitle");
    if (sub) {
      const editTag = _summaryCtx.dirty
        ? `<span style="margin-left:8px;padding:2px 8px;background:#fff7ed;color:#b36b00;border:1px solid #ffd2a1;border-radius:999px;font-size:11px;">● 未保存</span>`
        : "";
      const base = _summaryCtx.name ? `来源：${_summaryCtx.name}` : "";
      if (_summaryCtx._subHtml !== (base + editTag)) {
        sub.innerHTML = base + editTag;
        _summaryCtx._subHtml = base + editTag;
      }
    }
  }

  // ---------- LLM 设置：打开面板 ----------
  function openLLMConfig() {
    LLM.statusLine().textContent = "";
    LLM.statusLine().className = "llm-status-line";
    api("/api/llm_config").then((cfg) => {
      // provider
      const p = (cfg.provider || "deepseek").toLowerCase();
      LLM.radios().forEach((r) => {
        r.checked = (r.value === p);
        r.closest(".provider-card").classList.toggle("active", r.value === p);
      });
      LLM.baseUrl().value   = cfg.base_url   || "";
      LLM.modelName().value = cfg.model_name || "";
      // api_key 后端返回的是脱敏后的（如果是 '***' 则保持空，让用户重填；否则可能是真实值——后端不存明文以外的情况，这里直接显示）
      const key = cfg.api_key || "";
      LLM.apiKey().value    = (/^\*+$/.test(key) ? "" : key);
      LLM.temp().value      = typeof cfg.temperature === "number" ? cfg.temperature : 0.3;
      LLM.tempVal().textContent = LLM.temp().value;
      LLM.timeout().value      = typeof cfg.timeout === "number" ? cfg.timeout : 120;
      LLM.timeoutVal().textContent = LLM.timeout().value;
      LLM.enabled().checked   = !!cfg.enabled;
      _updateProviderHint(p);
      LLM.modal().classList.remove("hidden");
    }).catch((err) => {
      toast("读取 LLM 配置失败：" + (err.message || err), "error");
    });
  }
  function closeLLMConfig() { LLM.modal().classList.add("hidden"); }

  function _updateProviderHint(provider) {
    const preset = PROVIDER_PRESETS[provider] || PROVIDER_PRESETS.deepseek;
    LLM.urlHint().textContent = preset.hint;
  }

  // Provider 卡片：点击切换时联动默认值（只在用户没改过内容时才自动填，避免覆盖）
  function _bindProviderCards() {
    LLM.providerCards().forEach((card) => {
      const radio = card.querySelector('input[type="radio"]');
      if (!radio) return;
      card.addEventListener("click", (e) => {
        // 让 radio 先被选中
        const p = radio.value;
        setTimeout(() => {
          LLM.providerCards().forEach((c) => c.classList.toggle("active", c === card));
          _updateProviderHint(p);
          // 自动建议默认 base_url/model（仅当当前是空值或等于旧 preset 时才覆盖）
          const preset = PROVIDER_PRESETS[p];
          if (!LLM.baseUrl().value.trim() || Object.values(PROVIDER_PRESETS).some(pp => pp.base_url === LLM.baseUrl().value.trim())) {
            LLM.baseUrl().value = preset.base_url;
          }
          if (!LLM.modelName().value.trim() || Object.values(PROVIDER_PRESETS).some(pp => pp.model_name === LLM.modelName().value.trim())) {
            LLM.modelName().value = preset.model_name;
          }
        }, 0);
      });
    });
    // 温度 & 超时滑块
    LLM.temp().addEventListener("input", () => { LLM.tempVal().textContent = LLM.temp().value; });
    LLM.timeout().addEventListener("input", () => { LLM.timeoutVal().textContent = LLM.timeout().value; });
  }

  function _collectLLMForm() {
    let provider = "deepseek";
    LLM.radios().forEach(r => { if (r.checked) provider = r.value; });
    return {
      provider:   provider,
      base_url:   LLM.baseUrl().value.trim(),
      model_name: LLM.modelName().value.trim(),
      api_key:    LLM.apiKey().value,
      temperature: parseFloat(LLM.temp().value),
      timeout:    parseInt(LLM.timeout().value, 10),
      enabled:    !!LLM.enabled().checked,
    };
  }
  function _setStatus(cls, msg) {
    const el = LLM.statusLine();
    el.className = "llm-status-line " + (cls || "");
    el.textContent = msg || "";
  }

  async function _saveLLMConfig() {
    const body = _collectLLMForm();
    if (!body.base_url)   { _setStatus("err", "Base URL 不能为空"); return; }
    if (!body.model_name) { _setStatus("err", "模型名称不能为空"); return; }
    _setStatus("info", "正在保存…");
    try {
      const r = await api("/api/llm_config", body);
      _setStatus("ok", `✅ 已保存（${r.provider || body.provider} · ${r.model_name || body.model_name}）。提示：配置文件在 downloads/.llm_config.json`);
    } catch (err) {
      _setStatus("err", "❌ 保存失败：" + (err.message || err));
    }
  }

  async function _testLLMConnection() {
    const body = _collectLLMForm();
    if (!body.base_url)   { _setStatus("err", "Base URL 不能为空"); return; }
    if (!body.model_name) { _setStatus("err", "模型名称不能为空"); return; }
    _setStatus("info", "正在测试连接…（发送一条 'hi' 看看模型是否能回复）");
    try {
      const r = await api("/api/llm_test", body);
      _setStatus("ok", `✅ 连接成功！模型返回：${r.reply || "（空）"}`);
    } catch (err) {
      _setStatus("err", "❌ 连接失败：" + (err.message || err));
    }
  }

  function _resetLLMForm() {
    if (!confirm("恢复为 DeepSeek 默认配置？（清空当前表单内容）")) return;
    const p = "deepseek";
    const preset = PROVIDER_PRESETS[p];
    LLM.radios().forEach((r) => {
      r.checked = (r.value === p);
      r.closest(".provider-card").classList.toggle("active", r.value === p);
    });
    LLM.baseUrl().value   = preset.base_url;
    LLM.modelName().value = preset.model_name;
    LLM.apiKey().value    = "";
    LLM.temp().value      = 0.3;   LLM.tempVal().textContent    = "0.3";
    LLM.timeout().value   = 120;   LLM.timeoutVal().textContent = "120";
    LLM.enabled().checked = true;
    _updateProviderHint(p);
    _setStatus("", "");
  }

  // 绑定 LLM 设置按钮
  function _bindLLMConfigUI() {
    const setupBtn = $("aiSummarySetupBtn");
    if (setupBtn) setupBtn.onclick = openLLMConfig;
    const closeBtn = $("llmConfigCloseBtn");
    if (closeBtn) closeBtn.onclick = closeLLMConfig;
    const cancel   = $("llmCancelBtn");
    if (cancel) cancel.onclick = closeLLMConfig;
    const saveBtn  = $("llmSaveBtn");
    if (saveBtn)  saveBtn.onclick  = () => _saveLLMConfig();
    const testBtn  = $("llmTestBtn");
    if (testBtn)  testBtn.onclick  = () => _testLLMConnection();
    const resetBtn = $("llmResetBtn");
    if (resetBtn) resetBtn.onclick = () => _resetLLMForm();
    // 点击遮罩关闭
    LLM.modal().addEventListener("click", (e) => {
      if (e.target === LLM.modal()) closeLLMConfig();
    });
    _bindProviderCards();
  }

  // ---------- 摘要模态框 ----------
  function openSummaryModal(name, force) {
    if (!name) { toast("没有指定文件", "warn"); return; }
    _summaryCtx.name = name;
    SUM.subtitle().textContent = `来源：${name}`;
    // 默认切到结构化 Tab
    switchSummaryTab("structured");
    SUM.modal().classList.remove("hidden");
    loadSummary(name, !!force);
  }
  function closeSummaryModal() {
    if (_summaryCtx.dirty) {
      const pick = window.confirm("摘要或思维导图还有未保存的修改。\n\n点击「确定」——直接放弃并关闭；\n点击「取消」——回到窗口点 💾 保存后再关。");
      if (!pick) return;
    }
    SUM.modal().classList.add("hidden");
    // 清理 loading / 内容
    _summaryCtx.name = null;
    _summaryCtx.lastData = null;
    _summaryCtx.originalData = null;
    _summaryCtx.originalMd = null;
    _summaryCtx.lastMd = null;
    _summaryCtx.dirty = false;
    if (_summaryCtx.markmapMM) {
      try { _summaryCtx.markmapMM.destroy(); } catch (e) {}
      _summaryCtx.markmapMM = null;
    }
    // 离开模态时重置一下保存按钮视觉
    const btn = document.getElementById("summarySaveBtn");
    if (btn) { btn.disabled = true; btn.classList.remove("btn-primary"); btn.classList.add("btn-ghost"); btn.textContent = "💾 保存"; }
    const sub = document.getElementById("summarySubtitle");
    if (sub) sub.textContent = "";
  }
  function switchSummaryTab(tab) {
    SUM.tabs().forEach(t => t.classList.toggle("active", t.dataset.tab === tab));
    SUM.panes().forEach(p => p.classList.toggle("active", p.id === `pane-${tab}`));
    // J2: 头部「⬇ 下载」只在结构化视图显示
    const dlWrap = document.getElementById("summaryDownloadGroup");
    if (dlWrap) {
      if (tab === "structured") dlWrap.style.display = "";
      else                      dlWrap.style.display = "none";
    }
    // 切换到思维导图时渲染
    if (tab === "mindmap" && _summaryCtx.lastData) {
      renderMindmap(_summaryCtx.lastData, _summaryCtx.lastMd);
    }
  }

  async function saveSummary() {
    if (!_summaryCtx.name) { toast("没有目标文件", "warn"); return; }
    if (!_summaryCtx.lastData) { toast("暂无可保存的摘要内容", "warn"); return; }
    try {
      await api("/api/save_summary", {
        name: _summaryCtx.name,
        summary: _summaryCtx.lastData,
        mindmap_md: typeof _summaryCtx.lastMd === "string" ? _summaryCtx.lastMd : null,
      });
      // 更新"原始快照"为当前内容，dirty→false
      try { _summaryCtx.originalData = JSON.parse(JSON.stringify(_summaryCtx.lastData)); } catch { _summaryCtx.originalData = _summaryCtx.lastData; }
      _summaryCtx.originalMd = _summaryCtx.lastMd;
      _recomputeSummaryDirty();
      toast("已保存到本地（下次打开仍会保留你的改动）", "success", 2000);
    } catch (err) {
      let msg = (err && err.message) ? err.message : String(err);
      toast("保存失败：" + msg, "error", 3500);
    }
  }

  async function loadSummary(name, force) {
    SUM.loading().classList.remove("hidden");
    SUM.loadingText().textContent = force
      ? "AI 正在重新阅读转写内容，请稍候…（忽略缓存）"
      : "AI 正在阅读转写内容，请稍候…";
    SUM.content().innerHTML = "";
    SUM.svg().style.display = "none";
    SUM.empty().style.display = "";
    try {
      const r = await api("/api/summarize", { name, force: !!force });
      _summaryCtx.lastData = r.summary || null;
      // 优先回填用户上次手动保存的思维导图 Markdown（若有）
      if (_summaryCtx.lastData && typeof _summaryCtx.lastData.mindmap_md === "string" && _summaryCtx.lastData.mindmap_md.trim()) {
        _summaryCtx.lastMd = _summaryCtx.lastData.mindmap_md.trim();
      } else {
        _summaryCtx.lastMd = null;
      }
      // 生成"已保存快照"（用于 dirty 判定）
      try { _summaryCtx.originalData = _summaryCtx.lastData ? JSON.parse(JSON.stringify(_summaryCtx.lastData)) : null; } catch { _summaryCtx.originalData = _summaryCtx.lastData; }
      _summaryCtx.originalMd = _summaryCtx.lastMd;
      _recomputeSummaryDirty();
      if (_summaryCtx.lastData) {
        SUM.content().innerHTML = renderSummaryContent(_summaryCtx.lastData, r.from_cache);
        // 结构化视图挂 contenteditable（失焦自动写回 lastData，供下载/复制/重建导图时使用）
        try { _attachStructuredEditor(SUM.content()); } catch (e) { console.warn("attach editor failed", e); }
      } else {
        SUM.content().innerHTML = `<div class="empty-state" style="padding:24px;"><p class="es-title">无摘要内容</p><p class="es-desc">AI 返回了空结果，请尝试"重新生成"。</p></div>`;
      }
      // 当前就在思维导图 Tab 的话直接渲染
      const active = document.querySelector(".s-tab.active");
      if (active && active.dataset.tab === "mindmap" && _summaryCtx.lastData) {
        renderMindmap(_summaryCtx.lastData, _summaryCtx.lastMd);
      }
      if (r.from_cache) {
        if (_summaryCtx.lastData && _summaryCtx.lastData.edited) toast("已加载你上次手动保存的摘要版本", "info", 2200);
        else toast("已从缓存加载摘要", "info", 1800);
      } else {
        toast("摘要生成完成", "success", 1500);
      }
    } catch (err) {
      let msg = (err && err.message) ? err.message : String(err);
      // 如果是未配置提示，给出引导去设置
      if (/未配置|no llm|llm.*not.*config/i.test(msg)) {
        SUM.content().innerHTML = `
          <div class="s-block" style="border-color:#ffd2a1;background:#fffaf0;">
            <p class="s-block-title" style="color:#b36b00;">⚠️ 还没配置 AI 摘要</p>
            <p style="margin:0;line-height:1.75;">请先点击顶部工具栏的 <b>🤖 摘要设置</b>，选择 DeepSeek API / 本地 Ollama / 中转站之一并保存，再回来生成摘要。</p>
            <p style="margin-top:10px;">
              <button class="btn btn-primary" onclick="document.getElementById('aiSummarySetupBtn').click(); closeSummaryModal();">去设置 →</button>
            </p>
            <p style="margin-top:8px;font-size:12px;color:var(--fg-tertiary);">错误详情：${escapeHtml(msg)}</p>
          </div>
        `;
      } else {
        SUM.content().innerHTML = `
          <div class="s-block" style="border-color:#fecaca;background:#fef2f2;">
            <p class="s-block-title" style="color:#dc2626;">❌ 生成失败</p>
            <p style="margin:0;line-height:1.75;">${escapeHtml(msg)}</p>
            <p style="margin-top:10px;">
              <button class="btn btn-ghost" onclick="document.getElementById('summaryRefreshBtn').click();">↻ 重试</button>
            </p>
          </div>
        `;
      }
      toast(msg, "error", 4000);
    } finally {
      SUM.loading().classList.add("hidden");
    }
  }

  // 把摘要对象渲染成结构化 HTML
  function renderSummaryContent(s, fromCache) {
    const parts = [];
    const cacheTag = fromCache
      ? `<span style="font-size:11px;color:var(--fg-tertiary);font-weight:500;margin-left:8px;padding:2px 8px;background:var(--bg);border-radius:999px;">（来自缓存）</span>`
      : "";
    if (s.title) parts.push(`
      <div class="s-block">
        <p class="s-block-title">📌 主题</p>
        <div class="s-title-text">${escapeHtml(s.title)}${cacheTag}</div>
      </div>
    `);
    if (s.summary) parts.push(`
      <div class="s-block">
        <p class="s-block-title">📝 内容摘要</p>
        <div class="s-summary-text">${escapeHtml(String(s.summary))}</div>
      </div>
    `);
    if (Array.isArray(s.key_points) && s.key_points.length) {
      const lis = s.key_points.map(k => `<li>${escapeHtml(typeof k === "string" ? k : (k.point || JSON.stringify(k)))}</li>`).join("");
      parts.push(`
        <div class="s-block">
          <p class="s-block-title">💡 关键观点 (${s.key_points.length})</p>
          <ul class="s-list">${lis}</ul>
        </div>
      `);
    }
    if (Array.isArray(s.decisions) && s.decisions.length) {
      const lis = s.decisions.map(d => `<li>${escapeHtml(typeof d === "string" ? d : (d.item || JSON.stringify(d)))}</li>`).join("");
      parts.push(`
        <div class="s-block">
          <p class="s-block-title">✅ 会议决定 / 已达成共识</p>
          <ul class="s-list">${lis}</ul>
        </div>
      `);
    }
    if (Array.isArray(s.todos) && s.todos.length) {
      let rows = "";
      s.todos.forEach((t, i) => {
        if (typeof t === "string") {
          rows += `<tr><td>${i+1}.</td><td>${escapeHtml(t)}</td><td></td></tr>`;
        } else {
          let due = t.due || t.deadline || "";
          let who = t.owner || t.assignee || t.who || "";
          let content = t.content || t.item || t.task || JSON.stringify(t);
          rows += `<tr><td>${i+1}.</td><td>${escapeHtml(content)}${t.status ? ` <span style="color:var(--fg-tertiary);font-size:12px;">【${escapeHtml(t.status)}】</span>` : ""}</td><td>${who ? escapeHtml(who) : ""}${due ? `<br><span style="color:#b36b00;font-size:12px;">📅 ${escapeHtml(due)}</span>` : ""}</td></tr>`;
        }
      });
      parts.push(`
        <div class="s-block">
          <p class="s-block-title">📋 待办事项 (${s.todos.length})</p>
          <table class="s-todo-table">
            <thead><tr><th>#</th><th>任务</th><th>负责人 / 截止</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `);
    }
    // 风险/问题/疑问
    const extras = [
      ["risks",     "⚠️ 风险 / 注意点"],
      ["questions", "❓ 遗留问题 / 待确认"],
      ["next",      "➡️ 下一步建议"],
    ];
    extras.forEach(([k, title]) => {
      const arr = s[k];
      if (Array.isArray(arr) && arr.length) {
        const lis = arr.map(x => `<li>${escapeHtml(typeof x === "string" ? x : (x.item || JSON.stringify(x)))}</li>`).join("");
        parts.push(`<div class="s-block"><p class="s-block-title">${title}</p><ul class="s-list">${lis}</ul></div>`);
      }
    });
    if (s.tags && Array.isArray(s.tags) && s.tags.length) {
      parts.push(`
        <div class="s-block">
          <p class="s-block-title">🏷 标签</p>
          <div style="display:flex;flex-wrap:wrap;gap:6px;">
            ${s.tags.map(t => `<span style="background:rgba(10,132,255,0.08);color:#0a84ff;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;">#${escapeHtml(String(t))}</span>`).join("")}
          </div>
        </div>
      `);
    }
    if (!parts.length) {
      parts.push(`
        <div class="s-block">
          <p class="s-empty">AI 返回了没有可识别字段的摘要内容。</p>
          <pre style="background:var(--bg);padding:12px;border-radius:8px;margin-top:10px;overflow:auto;">${escapeHtml(JSON.stringify(s, null, 2))}</pre>
        </div>
      `);
    }
    return parts.join("");
  }

  // ---------- 思维导图 (markmap-autoloader: Transformer + Markmap) ----------
  // 说明：优先走官方推荐路径：Markdown -> Transformer.transform(md) -> root -> Markmap.create(svg, opts, root)
  // 该路径是 markmap 所有公开 Demo、官方文档使用的路径，兼容性最好，不会出现 JSON 字段名猜不对导致的空白 SVG。
  // 如果 Transformer/Markmap 任一缺失（网络真的断了、vendor 没下载、所有 CDN 404），
  // 则走 _renderMarkmapFallback()：把同样的内容渲染成可读的"文本大纲"，绝不再给用户一张空白。
  function _buildMarkdown(summary) {
    const lines = [];
    const title = (summary.title || summary.topic || "AI 摘要").toString().replace(/[#\n]/g, " ").trim();
    lines.push(`# ${title}`);

    function pushList(heading, arr) {
      if (!Array.isArray(arr) || !arr.length) return;
      lines.push("");
      lines.push(`${heading}`);
      arr.forEach((item) => {
        const text = (typeof item === "string"
          ? item
          : (item.content || item.item || item.point || item.task || JSON.stringify(item))
        ).toString().replace(/[\n]/g, " / ");
        lines.push(`- ${text}`);
      });
    }
    function pushTodos(heading, arr) {
      if (!Array.isArray(arr) || !arr.length) return;
      lines.push("");
      lines.push(`${heading}`);
      arr.forEach((t, i) => {
        if (typeof t === "string") {
          lines.push(`- ${i+1}. ${t.replace(/[\n]/g, " / ")}`);
          return;
        }
        const content = (t.content || t.item || t.task || JSON.stringify(t)).toString();
        const who = t.owner || t.assignee || t.who || "";
        const due = t.due || t.deadline || "";
        const status = t.status || "";
        const parts = [];
        if (who) parts.push(`👤${who}`);
        if (due) parts.push(`📅${due}`);
        if (status) parts.push(`[${status}]`);
        lines.push(`- ${i+1}. ${content.replace(/[\n]/g, " / ")}${parts.length ? "  _(" + parts.join(" ") + ")_" : ""}`);
      });
    }
    if (summary.summary) {
      lines.push("");
      lines.push(`## 📝 内容摘要`);
      const paras = summary.summary.toString().split(/\n+/).filter(Boolean).slice(0, 5);
      paras.forEach(p => lines.push(`- ${p.replace(/[-*]/g, "·")}`));
    }
    pushList("## 💡 关键观点",  summary.key_points);
    pushList("## ✅ 会议决定",  summary.decisions);
    pushTodos("## 📋 待办事项", summary.todos);
    pushList("## ⚠️ 风险注意",  summary.risks);
    pushList("## ❓ 遗留问题",  summary.questions);
    pushList("## ➡️ 下一步",    summary.next);
    if (Array.isArray(summary.tags) && summary.tags.length) {
      lines.push("");
      lines.push("## 🏷 标签");
      summary.tags.forEach(t => lines.push(`- #${String(t).replace(/[\n]/g, " ")}`));
    }
    // 保证至少有一个分支，避免空图
    if (lines.length <= 1) {
      lines.push("");
      lines.push("## （暂无分支内容）");
      lines.push("- 请点击「重新生成」，或确认 AI 摘要返回包含关键观点/待办。");
    }
    return lines.join("\n");
  }

  function _renderMarkmapFallback(md) {
    // 终极兜底：当 markmap 模块任何一个不可用时，把 Markdown 转成纯 HTML 文本大纲显示，
    // 用户不会再看到"完全空白"的面板。
    const svg = SUM.svg();
    svg.style.display = "none";
    const container = SUM.empty();
    container.style.display = "";
    const html = md.split("\n").map(line => {
      if (!line.trim()) return "";
      if (/^# /.test(line)) return `<h3 style="margin:0 0 10px 0;color:var(--accent);">${escapeHtml(line.slice(2))}</h3>`;
      if (/^## /.test(line)) return `<h4 style="margin:16px 0 6px 0;color:var(--fg);">${escapeHtml(line.slice(3))}</h4>`;
      if (/^- /.test(line)) return `<li style="margin:3px 0;line-height:1.7;">${escapeHtml(line.slice(2))}</li>`;
      return `<div style="line-height:1.7;">${escapeHtml(line)}</div>`;
    }).filter(Boolean).join("\n");
    container.innerHTML = `
      <div style="padding:24px;max-width:760px;">
        <div style="color:#b36b00;font-weight:600;margin-bottom:8px;">⚠️ markmap 渲染组件未加载，已切换为文本大纲视图</div>
        <div style="color:var(--fg-tertiary);font-size:12px;margin-bottom:18px;">
          请运行 <code>python web/vendor/download_vendor.py</code> 下载本地依赖，或刷新页面重试加载 CDN 版本。
        </div>
        <ul style="list-style:disc;padding-left:20px;">
          ${html}
        </ul>
      </div>
    `;
  }

  function renderMindmap(summary, customMd) {
    // 优先使用用户编辑过的 Markdown（customMd）；否则从 summary 对象构造
    let md = (typeof customMd === "string") ? customMd : _buildMarkdown(summary);
    _summaryCtx.lastMd = md;

    // 准备容器 / 清旧
    const svg = SUM.svg();
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (_summaryCtx.markmapMM) {
      try { _summaryCtx.markmapMM.destroy(); } catch (e) {}
      _summaryCtx.markmapMM = null;
    }

    const markmapNs = window.markmap;
    const hasMarkmap = markmapNs && typeof markmapNs.Markmap === "function";
    const hasTransformer = markmapNs && typeof markmapNs.Transformer === "function";

    if (!hasMarkmap || !hasTransformer) {
      _renderMarkmapFallback(md);
      return;
    }
    const { Markmap, Transformer, loadCSS, loadJS } = markmapNs;

    svg.style.display = "block";
    SUM.empty().style.display = "none";

    try {
      // 官方推荐：Transformer.transform(md) -> {root, features} -> Markmap.create
      const transformer = new Transformer();
      const { root, features } = transformer.transform(md);
      if (window.console) {
        console.debug("[markmap] md length=", md.length, "root=", root, "features=", features);
      }
      const assets = transformer.getUsedAssets(features);
      if (assets && assets.styles && Array.isArray(assets.styles)) {
        assets.styles.forEach(s => { try { loadCSS(s); } catch (_) {} });
      }
      if (assets && assets.scripts && Array.isArray(assets.scripts)) {
        assets.scripts.forEach(s => { try { loadJS(s, { getMarkmap: () => window.markmap }); } catch (_) {} });
      }
      const instance = Markmap.create(svg, {
        autoFit: true,
        duration: 350,
        initialExpandLevel: -1, // 全部展开
      }, root);
      // 已知：某些浏览器下 Tab 切过去时 SVG 获取不到真实高度，导致 markmap 把图画在 0x0 视口里 -> 看起来空白
      // 解决：延迟 60ms 后调用一次 fit() / 主动重新渲染。
      try {
        setTimeout(() => {
          if (svg.getBoundingClientRect && svg.getBoundingClientRect().width < 10) return;
          if (typeof instance.fit === "function") instance.fit();
          else if (typeof instance.setData === "function") instance.setData(root);
        }, 60);
      } catch (_) {}
      _summaryCtx.markmapMM = instance;
    } catch (e) {
      console.error("[markmap] 渲染异常", e);
      // 渲染异常也走兜底，绝不展示空白
      _renderMarkmapFallback(md);
    }
  }

  // ========== 摘要工具函数：下载 / 全屏 / 编辑 ==========

  // ---- 文件下载工具 ----
  function _saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename || "download";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => {
      try { URL.revokeObjectURL(url); a.remove(); } catch (_) {}
    }, 500);
  }
  function _downloadText(text, filename, mime) {
    const blob = new Blob([text], { type: mime || "text/plain;charset=utf-8" });
    _saveBlob(blob, filename);
  }

  // ---- 把 summary 对象序列化成各种格式 ----
  function _summaryToStructuredMarkdown(s) {
    if (!s) return "";
    const L = [];
    L.push(`# ${s.title || "AI 摘要"}`);
    L.push("");
    if (s.summary) {
      L.push("## 📝 内容摘要");
      L.push("");
      L.push(String(s.summary));
      L.push("");
    }
    function listMd(h, arr, prefix) {
      if (!Array.isArray(arr) || !arr.length) return;
      L.push(h);
      L.push("");
      arr.forEach((k, i) => {
        const line = typeof k === "string" ? k : (k.point || k.item || k.task || JSON.stringify(k));
        L.push(`${prefix} ${i+1}. ${line}`);
      });
      L.push("");
    }
    listMd("## 💡 关键观点",  s.key_points, "-");
    listMd("## ✅ 会议决定",  s.decisions,  "-");
    if (Array.isArray(s.todos) && s.todos.length) {
      L.push("## 📋 待办事项");
      L.push("");
      s.todos.forEach((t, i) => {
        if (typeof t === "string") { L.push(`- [ ] ${i+1}. ${t}`); return; }
        const content = t.content || t.item || t.task || JSON.stringify(t);
        const who = t.owner || t.assignee || t.who || "";
        const due = t.due || t.deadline || "";
        const st  = t.status || "";
        const suffix = [];
        if (who) suffix.push(`👤${who}`);
        if (due) suffix.push(`📅${due}`);
        if (st)  suffix.push(`[${st}]`);
        L.push(`- [ ] ${i+1}. ${content}${suffix.length ? "  (" + suffix.join(" ") + ")" : ""}`);
      });
      L.push("");
    }
    listMd("## ⚠️ 风险注意",  s.risks,     "-");
    listMd("## ❓ 遗留问题",  s.questions, "-");
    listMd("## ➡️ 下一步",    s.next,      "-");
    if (Array.isArray(s.tags) && s.tags.length) {
      L.push("## 🏷 标签");
      L.push("");
      L.push(s.tags.map(t => `#${String(t)}`).join(" "));
      L.push("");
    }
    return L.join("\n");
  }
  function _summaryToOutlineText(s) {
    if (!s) return "";
    const L = [];
    if (s.title) L.push(`${s.title}`);
    if (s.summary) { L.push("", s.summary); }
    function push(h, arr) {
      if (!Array.isArray(arr) || !arr.length) return;
      L.push("", h);
      arr.forEach((k, i) => {
        const line = typeof k === "string" ? k : (k.point || k.item || k.task || JSON.stringify(k));
        L.push(`  ${i+1}. ${line}`);
      });
    }
    push("【关键观点】", s.key_points);
    push("【会议决定】", s.decisions);
    push("【待办事项】", s.todos && s.todos.map(t => (typeof t === "string" ? t : (t.content || t.item || t.task || JSON.stringify(t)))));
    push("【风险/注意】", s.risks);
    push("【遗留问题】", s.questions);
    push("【下一步】",   s.next);
    if (Array.isArray(s.tags) && s.tags.length) L.push("", `标签：${s.tags.join(", ")}`);
    return L.join("\n");
  }

  // ---- 摘要对话框的"下载"按钮 ----
  function _doSummaryDownload(kind) {
    if (!_summaryCtx.lastData) { toast("暂无可下载的摘要", "warn"); return; }
    const base = (_summaryCtx.name || "summary").replace(/\.[^.]+$/, "");
    const stamp = new Date().toISOString().slice(0,10);
    if (kind === "md-structured") {
      const md = _summaryToStructuredMarkdown(_summaryCtx.lastData);
      _downloadText(md, `${base}_摘要_${stamp}.md`, "text/markdown;charset=utf-8");
      toast("已下载 Markdown（结构化）", "success", 1800);
    } else if (kind === "json") {
      const json = JSON.stringify(_summaryCtx.lastData, null, 2);
      _downloadText(json, `${base}_摘要_${stamp}.json`, "application/json;charset=utf-8");
      toast("已下载 JSON", "success", 1800);
    } else if (kind === "md-outline") {
      const txt = _summaryToOutlineText(_summaryCtx.lastData);
      _downloadText(txt, `${base}_摘要_${stamp}.txt`, "text/plain;charset=utf-8");
      toast("已下载纯文本大纲", "success", 1800);
    }
  }

  // ---- 思维导图的"下载"按钮 ----
  function _doMindmapDownload(kind) {
    const svg = SUM.svg();
    const base = (_summaryCtx.name || "mindmap").replace(/\.[^.]+$/, "");
    const stamp = new Date().toISOString().slice(0,10);
    if (kind === "md") {
      if (!_summaryCtx.lastMd) { toast("暂无可下载的思维导图大纲", "warn"); return; }
      _downloadText(_summaryCtx.lastMd, `${base}_思维导图_${stamp}.md`, "text/markdown;charset=utf-8");
      toast("已下载思维导图 Markdown", "success", 1800);
      return;
    }
    if (!svg || !svg.childNodes.length) { toast("请先渲染思维导图再下载", "warn"); return; }
    if (kind === "svg") {
      // 深克隆一份，确保带上 xmlns / 尺寸
      const clone = svg.cloneNode(true);
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
      const rect = svg.getBoundingClientRect();
      const w = clone.getAttribute("width")  || String(Math.max(1200, rect.width));
      const h = clone.getAttribute("height") || String(Math.max(800,  rect.height));
      if (!clone.getAttribute("width"))  clone.setAttribute("width",  w);
      if (!clone.getAttribute("height")) clone.setAttribute("height", h);
      clone.setAttribute("viewBox", clone.getAttribute("viewBox") || `0 0 ${w} ${h}`);
      const src = new XMLSerializer().serializeToString(clone);
      _downloadText('<?xml version="1.0" encoding="UTF-8"?>\n' + src, `${base}_思维导图_${stamp}.svg`, "image/svg+xml;charset=utf-8");
      toast("已下载 SVG", "success", 1800);
    } else if (kind === "png") {
      // PNG 导出：把 SVG 转成图片再画到 canvas 上。
      // 关键：必须把所有计算样式内联到 SVG 元素上，移除外部 CSS 引用，
      // 否则浏览器渲染图片时会跨域拉取 CDN 上的 CSS，导致 canvas 被"污染"（tainted），toBlob 报错。
      const rect = svg.getBoundingClientRect();
      const w = Math.max(1200, Math.round(rect.width));
      const h = Math.max(800,  Math.round(rect.height));

      const clone = svg.cloneNode(true);
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
      clone.setAttribute("xmlns:xlink", "http://www.w3.org/1999/xlink");
      clone.setAttribute("width",  w);
      clone.setAttribute("height", h);
      clone.setAttribute("viewBox", clone.getAttribute("viewBox") || `0 0 ${w} ${h}`);

      // 1) 把原始 SVG 中每个元素的计算样式内联到 clone 对应元素上
      //    （foreignObject 里的 div/span 靠外部 CSS 渲染，不内联的话图片里文字样式全丢）
      const srcEls = svg.querySelectorAll("*");
      const dstEls = clone.querySelectorAll("*");
      const STYLE_PROPS = [
        "fill","fill-opacity","stroke","stroke-width","stroke-opacity","stroke-linecap","stroke-linejoin",
        "font-size","font-family","font-weight","color","text-anchor","text-decoration",
        "opacity","display","visibility","overflow","box-sizing",
        "background","background-color","border","border-color","border-radius","border-width","border-style",
        "padding","margin","line-height","white-space","word-break","text-overflow",
        "width","height","max-width","max-height","position","left","top","transform",
        "flex","flex-direction","align-items","justify-content","gap","text-align","vertical-align",
        "list-style","list-style-type","cursor","pointer-events"
      ];
      for (let i = 0; i < srcEls.length && i < dstEls.length; i++) {
        try {
          const cs = window.getComputedStyle(srcEls[i]);
          let styleStr = "";
          for (const prop of STYLE_PROPS) {
            const val = cs.getPropertyValue(prop);
            if (val && val !== "none" && val !== "normal" && val !== "auto" && val !== "0s" && val !== "0") {
              styleStr += prop + ":" + val + ";";
            }
          }
          if (styleStr) {
            const existing = dstEls[i].getAttribute("style") || "";
            dstEls[i].setAttribute("style", existing + styleStr);
          }
        } catch (_) {}
      }

      // 2) 移除所有外部资源引用（<image href="http..."> 、style 里的 url(http...)）
      clone.querySelectorAll("image").forEach((imgEl) => {
        const href = imgEl.getAttribute("href") || imgEl.getAttribute("xlink:href") || "";
        if (/^https?:\/\//.test(href)) imgEl.remove();
      });
      // 清除 style 属性中的 url(http...) 引用
      clone.querySelectorAll("*[style]").forEach((el) => {
        const s = el.getAttribute("style") || "";
        if (s.includes("url(http") || s.includes("url('http") || s.includes('url("http')) {
          el.setAttribute("style", s.replace(/url\(['"]?https?:\/\/[^)]+\)['"]?/g, "none"));
        }
      });

      // 3) 收集文档里和 markmap 相关的 CSS 规则，嵌入 <style> 标签
      let cssText = "";
      try {
        for (const sheet of document.styleSheets) {
          try {
            for (const rule of sheet.cssRules) {
              const sel = rule.selectorText || "";
              if (sel && (
                sel.includes("markmap") || sel.includes("foreignObject") ||
                sel.includes(".mm") || sel === "div" || sel === "span" ||
                sel === "tspan" || sel === "circle" || sel === "line" ||
                sel === "path" || sel === "g" || sel === "text" || sel === "rect" ||
                sel.startsWith(".markmap") || sel.startsWith("#markmap")
              )) {
                cssText += rule.cssText + "\n";
              }
            }
          } catch (_) { /* 跨域样式表跳过 */ }
        }
      } catch (_) {}
      if (cssText) {
        const styleEl = document.createElementNS("http://www.w3.org/2000/svg", "style");
        styleEl.setAttribute("type", "text/css");
        // 用 CDATA 包裹防止特殊字符破坏 XML
        styleEl.textContent = "<![CDATA[" + cssText + "]]>";
        clone.insertBefore(styleEl, clone.firstChild);
      }

      // 4) 序列化为 data URL（不用 blob: URL，data: URL 永远同源，不会污染 canvas）
      const svgString = new XMLSerializer().serializeToString(clone);
      const dataUrl = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svgString);

      const img = new Image();
      img.crossOrigin = "anonymous";
      img.onload = () => {
        try {
          const scale = 2;
          const canvas = document.createElement("canvas");
          canvas.width  = w * scale;
          canvas.height = h * scale;
          const ctx = canvas.getContext("2d");
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, canvas.width, canvas.height);
          ctx.scale(scale, scale);
          ctx.drawImage(img, 0, 0, w, h);
          canvas.toBlob((blob) => {
            if (!blob) { toast("PNG 导出失败：canvas.toBlob 无结果（可能浏览器不支持）", "error"); return; }
            _saveBlob(blob, `${base}_思维导图_${stamp}.png`);
            toast("已下载 PNG", "success", 1800);
          }, "image/png");
        } catch (e) {
          toast("PNG 导出失败：" + (e.message || e), "error");
        }
      };
      img.onerror = () => {
        toast("SVG 转图片失败：可能是 SVG 内容过大或格式问题，请尝试下载 SVG 格式", "error", 4000);
      };
      img.src = dataUrl;
    }
  }

  // ---- 摘要对话框全屏切换 ----
  function _toggleSummaryFullscreen() {
    const modal = SUM.modal();
    const btn = SUM.fullscreen();
    if (!modal || !btn) return;
    const will = !_summaryCtx.isFullscreen;
    modal.classList.toggle("is-fullscreen", will);
    btn.setAttribute("aria-pressed", String(will));
    _summaryCtx.isFullscreen = will;
    // 切全屏后 markmap 的容器尺寸变化很大，强制重新 fit / 重建
    if (will && _summaryCtx.markmapMM) {
      setTimeout(() => {
        try {
          if (typeof _summaryCtx.markmapMM.fit === "function") _summaryCtx.markmapMM.fit();
        } catch (_) {
          // 若 fit 不生效，重建一次
          if (_summaryCtx.lastData) renderMindmap(_summaryCtx.lastData, _summaryCtx.lastMd);
        }
      }, 120);
    }
  }

  // ---- 下拉菜单点击外部自动关 ----
  function _bindCloseOutside() {
    document.addEventListener("click", (e) => {
      document.querySelectorAll(".btn-split-group.open").forEach((g) => {
        if (!g.contains(e.target)) g.classList.remove("open");
      });
    });
  }

  // ---- 结构化摘要的编辑：可编辑字段 -> 同步回 lastData ----
  // renderSummaryContent 时给每个 block 加 data-*，绑定保存。更省事做法：每次 loadSummary 后在 DOM 里做一次编辑区"可编辑 + 失焦保存"
  function _attachStructuredEditor(rootEl) {
    if (!rootEl || !_summaryCtx.lastData) return;
    const s = _summaryCtx.lastData;

    // --- 标题 & 摘要（最常改的两块用 contenteditable） ---
    const titleEl = rootEl.querySelector(".s-title-text");
    if (titleEl) {
      titleEl.setAttribute("contenteditable", "true");
      titleEl.setAttribute("spellcheck", "false");
      titleEl.style.outline = "none";
      titleEl.title = "点击可编辑主题（失焦自动保存）";
      titleEl.addEventListener("blur", () => {
        s.title = titleEl.innerText.trim();
        _recomputeSummaryDirty();
      });
    }
    const sumEl = rootEl.querySelector(".s-summary-text");
    if (sumEl) {
      sumEl.setAttribute("contenteditable", "true");
      sumEl.setAttribute("spellcheck", "false");
      sumEl.style.outline = "none";
      sumEl.title = "点击可编辑内容摘要（失焦自动保存）";
      sumEl.addEventListener("blur", () => {
        s.summary = sumEl.innerText.trim();
        _recomputeSummaryDirty();
      });
    }

    // --- 列表 / 待办表：直接让每个 <li> 和 <td> 可编辑，并按 DOM 顺序对应到数组 ---
    const lists = [
      ["💡 关键观点",           "key_points", "item"],
      ["✅ 会议决定",           "decisions",  "item"],
      ["⚠️ 风险 / 注意点",     "risks",      "item"],
      ["❓ 遗留问题 / 待确认", "questions",  "item"],
      ["➡️ 下一步",            "next",       "item"],
    ];
    rootEl.querySelectorAll(".s-block").forEach((blk) => {
      const title = (blk.querySelector(".s-block-title") || {}).innerText || "";
      for (const [prefix, key, field] of lists) {
        if (!title.startsWith(prefix)) continue;
        const lis = blk.querySelectorAll(".s-list > li");
        lis.forEach((li, idx) => {
          if (!Array.isArray(s[key])) return;
          li.setAttribute("contenteditable", "true");
          li.setAttribute("spellcheck", "false");
          li.title = "点击编辑（失焦保存）";
          li.addEventListener("blur", () => {
            const val = li.innerText.trim();
            const existing = s[key][idx];
            if (typeof existing === "string") s[key][idx] = val;
            else if (existing && typeof existing === "object") existing.item = val;
            else s[key][idx] = val;
            _recomputeSummaryDirty();
          });
        });
        break;
      }
      // 待办表独立处理
      if (title.startsWith("📋 待办事项")) {
        const rows = blk.querySelectorAll("tbody > tr");
        rows.forEach((tr, idx) => {
          if (!Array.isArray(s.todos)) return;
          const tds = tr.querySelectorAll("td");
          if (tds.length < 3) return;
          // 任务（第 2 列）
          const tdTask = tds[1];
          tdTask.setAttribute("contenteditable", "true");
          tdTask.setAttribute("spellcheck", "false");
          tdTask.addEventListener("blur", () => {
            const text = tdTask.innerText.replace(/【.+?】/g, "").trim();
            const todo = s.todos[idx];
            if (typeof todo === "string") s.todos[idx] = text;
            else if (todo && typeof todo === "object") todo.content = text;
            else s.todos[idx] = text;
            _recomputeSummaryDirty();
          });
          // 负责人 / 截止（第 3 列）
          const tdWho = tds[2];
          tdWho.setAttribute("contenteditable", "true");
          tdWho.setAttribute("spellcheck", "false");
          tdWho.addEventListener("blur", () => {
            const raw = tdWho.innerText.trim();
            const todo = s.todos[idx] || {};
            // 解析可选的 📅日期 部分
            const dueMatch = raw.match(/📅([^\n]+)/);
            const due   = dueMatch ? dueMatch[1].trim() : "";
            const whoRaw = raw.replace(/📅[^\n]*/g, "").replace(/\n+/g, " ").trim();
            if (typeof todo !== "object") s.todos[idx] = { content: todo || "" };
            const t = s.todos[idx];
            if (whoRaw) (t.owner = whoRaw);
            if (due)    (t.due = due);
            _recomputeSummaryDirty();
          });
        });
      }
    });

    // --- 标签单独找 ---
    // 当前 tags block 没有通过 selector 暴露出来，但结构是 div.s-block > ul.s-list
    rootEl.querySelectorAll(".s-block").forEach((blk) => {
      const titleEl = blk.querySelector(".s-block-title");
      if (!titleEl || !/标签/.test(titleEl.innerText || "")) return;
      const lis = blk.querySelectorAll(".s-list > li");
      lis.forEach((li, idx) => {
        if (!Array.isArray(s.tags)) return;
        li.setAttribute("contenteditable", "true");
        li.addEventListener("blur", () => {
          const val = li.innerText.trim().replace(/^#/, "");
          s.tags[idx] = val;
          _recomputeSummaryDirty();
        });
      });
    });
  }

  // ---- 思维导图 Markdown 编辑弹窗 ----
  function openMmEditModal() {
    const m = SUM.mmEditModal();
    if (!m) return;
    // 默认值：优先用上次实际渲染的 lastMd；否则从 summary 构造；都没有就占位
    const placeholderMd = `# ${(_summaryCtx.lastData && _summaryCtx.lastData.title) || "我的思维导图"}\n\n` +
      "## 一级分支\n- 要点 1\n- 要点 2\n\n## 二级分支\n### 子分类\n- 细节";
    SUM.mmEditText().value = _summaryCtx.lastMd ||
      (_summaryCtx.lastData ? _buildMarkdown(_summaryCtx.lastData) : placeholderMd);
    m.classList.remove("hidden");
    setTimeout(() => SUM.mmEditText().focus(), 60);
  }
  function closeMmEditModal() {
    const m = SUM.mmEditModal();
    if (m) m.classList.add("hidden");
  }
  function applyMmEdit() {
    const md = (SUM.mmEditText().value || "").trim();
    if (!md) { toast("大纲内容不能为空", "warn"); return; }
    // 用用户编辑过的 Markdown 直接渲染（不从 summary 构造）
    const placeholder = { title: (md.match(/^#\s+(.+)$/m) || [,"思维导图"])[1].trim() };
    renderMindmap(_summaryCtx.lastData || placeholder, md);
    closeMmEditModal();
    _recomputeSummaryDirty();
    toast("思维导图已更新", "success", 1500);
  }

  function _bindSummaryModalUI() {
    // 关闭
    const cb = $("summaryCloseBtn");
    if (cb) cb.onclick = closeSummaryModal;
    SUM.modal().addEventListener("click", (e) => {
      if (e.target === SUM.modal()) closeSummaryModal();
    });
    // 重新生成
    const rf = SUM.refresh();
    if (rf) rf.onclick = () => {
      if (!_summaryCtx.name) return;
      if (_summaryCtx.dirty) {
        const ok = window.confirm("重新生成会覆盖当前编辑但未保存的内容，确定要继续吗？");
        if (!ok) return;
      }
      loadSummary(_summaryCtx.name, true);
    };
    // 手动保存
    const sv = document.getElementById("summarySaveBtn");
    if (sv) sv.onclick = saveSummary;
    // 复制
    const cp = SUM.copy();
    if (cp) cp.onclick = async () => {
      if (!_summaryCtx.lastData) { toast("暂无可复制的摘要", "warn"); return; }
      const s = _summaryCtx.lastData;
      // 结构化文本
      const lines = [];
      if (s.title)   lines.push(`【主题】${s.title}`);
      if (s.summary) lines.push("【摘要】", s.summary, "");
      if (Array.isArray(s.key_points) && s.key_points.length) {
        lines.push("【关键观点】");
        s.key_points.forEach((k, i) => lines.push(`${i+1}. ${typeof k === "string" ? k : (k.point || JSON.stringify(k))}`));
        lines.push("");
      }
      if (Array.isArray(s.decisions) && s.decisions.length) {
        lines.push("【会议决定】");
        s.decisions.forEach((d, i) => lines.push(`${i+1}. ${typeof d === "string" ? d : (d.item || JSON.stringify(d))}`));
        lines.push("");
      }
      if (Array.isArray(s.todos) && s.todos.length) {
        lines.push("【待办】");
        s.todos.forEach((t, i) => {
          if (typeof t === "string") lines.push(`- [ ] ${i+1}. ${t}`);
          else {
            const content = t.content || t.item || t.task || JSON.stringify(t);
            const who = t.owner || t.assignee || t.who || "";
            const due = t.due || t.deadline || "";
            lines.push(`- [ ] ${i+1}. ${content}${who ? "  👤"+who : ""}${due ? "  📅"+due : ""}`);
          }
        });
        lines.push("");
      }
      if (!lines.length) lines.push(JSON.stringify(s, null, 2));
      try {
        await copyText(lines.join("\n"));
        toast("已复制摘要", "success", 1500);
      } catch (e) {
        toast("复制失败：" + (e.message || e), "error");
      }
    };
    // Tab 切换
    SUM.tabs().forEach(t => {
      t.addEventListener("click", () => switchSummaryTab(t.dataset.tab));
    });
    // 下载下拉（摘要对话框头部）
    const sumDlGrp = SUM.downloadGroup();
    if (sumDlGrp) {
      sumDlGrp.addEventListener("click", (e) => {
        const trigger = e.target.closest("#summaryDownloadBtn");
        const item = e.target.closest(".dd-item");
        if (trigger) { e.stopPropagation(); sumDlGrp.classList.toggle("open"); }
        else if (item) {
          e.stopPropagation();
          sumDlGrp.classList.remove("open");
          _doSummaryDownload(item.dataset.dl);
        }
      });
    }
    // 全屏切换
    const fs = SUM.fullscreen();
    if (fs) fs.addEventListener("click", _toggleSummaryFullscreen);

    // === 思维导图 Tab 工具栏 ===
    if (SUM.mmToolEdit()) SUM.mmToolEdit().addEventListener("click", openMmEditModal);
    if (SUM.mmToolReload()) SUM.mmToolReload().addEventListener("click", () => {
      if (!_summaryCtx.lastData) { toast("请先生成摘要", "warn"); return; }
      renderMindmap(_summaryCtx.lastData);
      toast("思维导图已重建", "success", 1300);
    });
    const mmDlGrp = SUM.mmDownloadGroup();
    if (mmDlGrp) {
      mmDlGrp.addEventListener("click", (e) => {
        const trigger = e.target.closest(".btn-split-group > .btn");
        const item = e.target.closest(".dd-item");
        if (trigger) { e.stopPropagation(); mmDlGrp.classList.toggle("open"); }
        else if (item) {
          e.stopPropagation();
          mmDlGrp.classList.remove("open");
          _doMindmapDownload(item.dataset.mm);
        }
      });
    }
    // === 思维导图编辑弹窗 ===
    const mmM = SUM.mmEditModal();
    if (mmM) mmM.addEventListener("click", (e) => { if (e.target === mmM) closeMmEditModal(); });
    if (SUM.mmEditClose()) SUM.mmEditClose().onclick = closeMmEditModal;
    if (SUM.mmEditApply()) SUM.mmEditApply().onclick = applyMmEdit;
    if (SUM.mmEditReset()) SUM.mmEditReset().onclick = () => {
      if (!_summaryCtx.lastData) { toast("请先生成摘要", "warn"); return; }
      SUM.mmEditText().value = _buildMarkdown(_summaryCtx.lastData);
    };
    // Ctrl+Enter 应用
    if (SUM.mmEditText()) SUM.mmEditText().addEventListener("keydown", (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); applyMmEdit(); }
      if (e.key === "Escape") { e.preventDefault(); closeMmEditModal(); }
    });

    // 顶部摘要按钮
    const topSumBtn = $("summaryBtn");
    if (topSumBtn) topSumBtn.onclick = () => {
      if (!TX.source) { toast("请先打开一份转写结果", "warn"); return; }
      openSummaryModal(TX.source, false);
    };
  }

  // ---------- 归档 ----------
  async function doArchive(name) {
    if (!name) return;
    const dateStr = new Date();
    const y = dateStr.getFullYear();
    const m = String(dateStr.getMonth()+1).padStart(2,"0");
    const d = String(dateStr.getDate()).padStart(2,"0");
    const suggested = `${y}-${m}-${d}`;
    const folder = prompt(`归档到哪个日期文件夹？\n（只支持 YYYY-MM-DD 格式；留空则使用今天：${suggested}）`, suggested);
    if (folder === null) return; // 取消
    const f = (folder || "").trim() || suggested;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(f)) { toast("文件夹名必须是 YYYY-MM-DD 格式", "warn"); return; }
    try {
      const r = await api("/api/archive", { name, folder: f });
      toast(`✅ 已归档到：${r.target_folder || f}（移动 ${(r.moved || []).length} 个文件）`, "success", 2200);
      if (TX.source === name) TX.source = r.new_name || TX.source;
      await loadLocal();
    } catch (err) {
      toast("归档失败：" + (err.message || err), "error");
    }
  }

  // ---------- 导出 Markdown ----------
  async function doExportMarkdown(name) {
    if (!name) { toast("没有指定文件", "warn"); return; }
    try {
      const r = await api("/api/export_markdown", { name });
      toast(`✅ Markdown 已导出：${r.output_path || r.md_path || "（已保存）"}`, "success", 2500);
      // 尝试给用户弹个下载链接
      if (r.md_path) {
        try {
          const link = document.createElement("a");
          link.href = `/downloads/${encodeURIComponent(r.md_path)}`;
          link.target = "_blank";
          link.download = r.md_path.split(/[\\/]/).pop();
          link.rel = "noopener";
          document.body.appendChild(link);
          link.click();
          setTimeout(() => link.remove(), 100);
        } catch (e) {}
      }
      await loadLocal();
    } catch (err) {
      let msg = (err && err.message) ? err.message : String(err);
      if (/no transcript|转写|empty/i.test(msg)) {
        toast(`导出失败：${msg}。请先对该文件执行转写，再导出。`, "error", 5000);
      } else {
        toast("导出失败：" + msg, "error");
      }
    }
  }

  function _bindtranscribeModalButtons() {
    // 转写结果卡片：导出 MD 按钮
    const exMd = $("exportMdBtn");
    if (exMd) exMd.onclick = () => {
      if (!TX.source) { toast("请先打开一份转写结果", "warn"); return; }
      run(exMd, () => doExportMarkdown(TX.source));
    };
  }

  // ---------- 统一挂载 ----------
  _bindLLMConfigUI();
  _bindSummaryModalUI();
  _bindtranscribeModalButtons();
  _bindCloseOutside();

  // ESC 关闭最上层的模态框
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    // 思维导图编辑弹窗在最上层 -> 先关它
    const mmEdit = SUM.mmEditModal && SUM.mmEditModal();
    if (mmEdit && !mmEdit.classList.contains("hidden")) { closeMmEditModal(); e.preventDefault(); return; }
    // 如果摘要是全屏 -> ESC 只退全屏不退弹窗
    if (!SUM.modal().classList.contains("hidden") && _summaryCtx.isFullscreen) {
      _toggleSummaryFullscreen();
      e.preventDefault();
      return;
    }
    if (!SUM.modal().classList.contains("hidden"))      { closeSummaryModal(); e.preventDefault(); return; }
    if (!LLM.modal().classList.contains("hidden"))      { closeLLMConfig();   e.preventDefault(); return; }
  });

})();

