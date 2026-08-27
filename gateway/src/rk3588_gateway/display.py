from __future__ import annotations

from html import escape


DISPLAY_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>RK3588 Gateway Status</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101418;
      --panel: #171d22;
      --line: #2b343c;
      --text: #eef3f7;
      --muted: #93a1ad;
      --ok: #3bd17f;
      --warn: #f3c74f;
      --bad: #f06464;
      --idle: #667481;
      font-family: Arial, "Microsoft YaHei", sans-serif;
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; background: #000; color: var(--text); }
    body { overflow: hidden; }
    .screen {
      width: 480px;
      height: 320px;
      overflow: hidden;
      background: var(--bg);
    }
    .topbar {
      height: 62px;
      display: grid;
      grid-template-columns: 1.2fr 0.7fr 0.8fr;
      align-items: center;
      border-bottom: 1px solid var(--line);
      background: #141a1f;
    }
    .topbar .cell:nth-child(4), .topbar .cell:nth-child(5) { display: none; }
    .cell {
      height: 100%;
      min-width: 0;
      padding: 8px 10px;
      border-right: 1px solid var(--line);
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 5px;
    }
    .cell:last-child { border-right: 0; }
    .label { color: var(--muted); font-size: 12px; line-height: 1; }
    .value { font-size: 17px; font-weight: 700; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .main {
      height: 258px;
      display: grid;
      grid-template-columns: 285px 195px;
      gap: 0;
    }
    .section {
      min-width: 0;
      padding: 10px;
      border-right: 1px solid var(--line);
    }
    .section:last-child { border-right: 0; }
    h1 { margin: 0 0 8px; font-size: 16px; line-height: 1.1; font-weight: 700; letter-spacing: 0; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; }
    .metric {
      min-height: 50px;
      padding: 8px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
    }
    .metric .label { font-size: 11px; margin-bottom: 5px; }
    .metric .value { font-size: 16px; }
    .events {
      height: 232px;
      overflow: hidden;
      border-top: 1px solid var(--line);
    }
    .event {
      display: grid;
      grid-template-columns: 56px 1fr;
      gap: 5px;
      min-height: 32px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      color: var(--muted);
      font-size: 11px;
    }
    .event span:last-child { grid-column: 1 / -1; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
    .event strong { color: var(--text); font-weight: 600; }
    .pill { display: inline-flex; align-items: center; gap: 8px; }
    .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--idle); flex: 0 0 auto; }
    .ok .dot { background: var(--ok); }
    .warn .dot { background: var(--warn); }
    .bad .dot { background: var(--bad); }
    .keys { display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }
    .key {
      height: 54px;
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 6px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-direction: column;
      gap: 4px;
      font-size: 13px;
    }
    .key.active { border-color: var(--ok); background: #183326; }
    .key .name { font-size: 15px; font-weight: 700; }
    .key .state { color: var(--muted); font-size: 10px; }
    @media (max-width: 360px) {
      .topbar { height: auto; grid-template-columns: 1fr; }
      .cell { min-height: 74px; border-right: 0; border-bottom: 1px solid var(--line); }
      .main { height: auto; grid-template-columns: 1fr; overflow: auto; }
      .section { border-right: 0; border-bottom: 1px solid var(--line); }
      .grid, .keys { grid-template-columns: 1fr 1fr; }
      .event { grid-template-columns: 1fr; gap: 4px; padding: 10px 0; }
    }
  </style>
</head>
<body>
  <div class="screen">
  <div class="topbar">
    <div class="cell"><div class="label">设备</div><div id="device" class="value">--</div></div>
    <div class="cell"><div class="label">服务</div><div id="service" class="value pill ok"><span class="dot"></span><span>运行中</span></div></div>
    <div class="cell"><div class="label">队列</div><div id="queue" class="value">0</div></div>
    <div class="cell"><div class="label">最近扫码</div><div id="scan" class="value">--</div></div>
    <div class="cell"><div class="label">时间</div><div id="clock" class="value">--</div></div>
  </div>
  <div class="main">
    <section class="section">
      <h1>实时状态</h1>
      <div class="grid">
        <div class="metric"><div class="label">打印文件</div><div id="prints" class="value">0</div></div>
        <div class="metric"><div class="label">U盘文件</div><div id="msc" class="value">0</div></div>
        <div class="metric"><div class="label">本地 API</div><div id="api" class="value">正常</div></div>
        <div class="metric"><div class="label">最近事件</div><div id="lastEvent" class="value">--</div></div>
      </div>
      <h1 style="margin-top:10px">按键</h1>
      <div id="keys" class="keys"></div>
    </section>
    <section class="section">
      <h1>事件</h1>
      <div id="events" class="events"></div>
    </section>
  </div>
  </div>
  <script>
    const keyLabels = { up: "UP", down: "DOWN", ok: "OK", back: "BACK" };
    function text(value) { return value === undefined || value === null || value === "" ? "--" : String(value); }
    function setText(id, value) { document.getElementById(id).textContent = text(value); }
    function eventSummary(event) {
      if (!event) return "--";
      if (event.type === "barcode.scan") return event.payload?.code || event.type;
      if (event.type === "print.captured") return "打印 " + (event.payload?.bytes || 0) + "B";
      if (event.type === "msc.file_copied") return event.payload?.path || event.type;
      return event.type;
    }
    function renderKeys(lines) {
      const host = document.getElementById("keys");
      host.innerHTML = "";
      for (const line of lines || []) {
        const div = document.createElement("div");
        div.className = "key" + (line.value ? " active" : "");
        div.innerHTML = `<div class="name">${keyLabels[line.name] || line.name}</div><div class="state">${line.value ? "按下" : "松开"}</div>`;
        host.appendChild(div);
      }
    }
    function renderEvents(events) {
      const host = document.getElementById("events");
      host.innerHTML = "";
      for (const event of events || []) {
        const row = document.createElement("div");
        row.className = "event";
        row.innerHTML = `<span>${new Date(event.created_at).toLocaleTimeString()}</span><strong>${event.type}</strong><span>${eventSummary(event)}</span>`;
        host.appendChild(row);
      }
    }
    async function refresh() {
      try {
        const res = await fetch("/display/state", { cache: "no-store" });
        if (!res.ok) throw new Error("HTTP " + res.status);
        const data = await res.json();
        setText("device", data.device_id + " / " + data.location);
        setText("queue", data.queued_events);
        setText("scan", data.last_scan || "--");
        setText("clock", new Date().toLocaleTimeString());
        setText("prints", data.print_jobs);
        setText("msc", data.msc_files);
        setText("api", "正常");
        setText("lastEvent", eventSummary(data.events?.[0]));
        renderKeys(data.gpio?.lines || []);
        renderEvents(data.events || []);
        document.getElementById("service").className = "value pill ok";
        document.querySelector("#service span:last-child").textContent = "运行中";
      } catch (err) {
        document.getElementById("service").className = "value pill bad";
        document.querySelector("#service span:last-child").textContent = "异常";
        setText("api", "异常");
      }
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


def html_response() -> str:
    return DISPLAY_HTML


def safe_text(value: object) -> str:
    return escape(str(value), quote=False)
