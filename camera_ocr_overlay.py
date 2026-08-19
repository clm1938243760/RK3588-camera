#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


DISPLAY_BASE_ROTATION = 90
OCR_BASE_ROTATION = 90
PATIENT_RESPONSE_FIELDS = (
    "birthday",
    "exam_item",
    "ming",
    "sex",
    "yue",
    "his_exam_no",
    "xing",
    "patient_id",
    "ri",
    "patient_name",
    "name_phonetic",
    "nian",
    "report_no",
    "age",
)


PAGE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RK3588 报告单采集监看</title>
<style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:#050607;color:#f8fafc;font-family:Arial,"Microsoft YaHei",sans-serif}
#stage{position:relative;width:100%;height:100%;display:flex;align-items:center;justify-content:center;background:#050607}
#viewport{position:relative;width:min(100vw,calc(100vh * 16 / 9));aspect-ratio:16/9;overflow:hidden;background:#000}
#video,#overlay{position:absolute;inset:0;width:100%;height:100%;object-fit:contain}
#video{opacity:0;transition:opacity 120ms linear}
#video.playing{opacity:1}
#overlay{pointer-events:none}
body.rotation-90 #viewport,body.rotation-270 #viewport{width:min(100vw,calc(100vh * 9 / 16));aspect-ratio:9/16}
body.rotation-90 #video,body.rotation-90 #overlay,body.rotation-270 #video,body.rotation-270 #overlay{inset:auto;left:-38.8889%;top:21.875%;width:177.7778%;height:56.25%;object-fit:fill}
body.rotation-90 #video,body.rotation-90 #overlay{transform:rotate(-90deg)}
body.rotation-180 #video,body.rotation-180 #overlay{transform:rotate(180deg)}
body.rotation-270 #video,body.rotation-270 #overlay{transform:rotate(90deg)}
#panel{position:fixed;left:14px;top:14px;z-index:4;width:min(340px,calc(100vw - 28px));padding:12px;background:rgba(5,6,7,.78);border:1px solid rgba(255,255,255,.3);border-radius:6px;backdrop-filter:blur(5px)}
#headline{display:flex;align-items:center;gap:9px;min-height:22px;font-size:14px;font-weight:600}
#settingsButton{margin-left:auto;height:26px;border:1px solid rgba(255,255,255,.28);border-radius:4px;background:rgba(255,255,255,.08);color:#f8fafc;padding:0 9px;font-size:11px;cursor:pointer}
#settingsButton:hover{background:rgba(255,255,255,.16)}
#dot{flex:0 0 auto;width:9px;height:9px;border-radius:50%;background:#f59e0b;box-shadow:0 0 0 3px rgba(245,158,11,.14)}
#panel.ready #dot{background:#22c55e;box-shadow:0 0 0 3px rgba(34,197,94,.14)}
#panel.error #dot{background:#ef4444;box-shadow:0 0 0 3px rgba(239,68,68,.14)}
#detail{margin-top:7px;color:#cbd5e1;font-size:12px;line-height:1.5}
#progress{height:4px;margin-top:9px;overflow:hidden;background:rgba(255,255,255,.18);border-radius:2px}
#progressBar{width:0;height:100%;background:#22d3ee;transition:width 150ms linear}
#metrics{display:grid;grid-template-columns:1fr 1fr;gap:5px 12px;margin-top:9px;color:#aeb8c3;font-size:11px}
#metrics span:nth-child(even){color:#f1f5f9;text-align:right}
#resultBand{margin-top:11px;padding-top:10px;border-top:1px solid rgba(255,255,255,.2)}
#resultHeader{display:flex;align-items:center;justify-content:space-between;gap:10px;color:#aeb8c3;font-size:11px}
#identifier{margin-top:5px;min-height:28px;color:#f8fafc;font-family:Consolas,"Courier New",monospace;font-size:22px;font-weight:700;line-height:1.25;overflow-wrap:anywhere}
#identifier.waiting{color:#94a3b8;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:13px;font-weight:400;line-height:28px}
#resultHint{min-height:17px;margin-top:4px;color:#94a3b8;font-size:10px;line-height:1.45}
#patientActions{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:7px}
#patientSummary{min-width:0;color:#94a3b8;font-size:10px;overflow-wrap:anywhere}
#settings{display:none;margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,255,255,.2)}
#settings.open{display:block}
.settingRow{display:grid;grid-template-columns:96px minmax(0,1fr);align-items:center;gap:9px;margin-bottom:7px;color:#cbd5e1;font-size:11px}
.settingRow select,.settingRow input{width:100%;height:30px;box-sizing:border-box;border:1px solid rgba(255,255,255,.28);border-radius:4px;background:#111418;color:#f8fafc;padding:0 8px;font-size:12px}
.settingRow select:focus,.settingRow input:focus{outline:2px solid #22d3ee;outline-offset:1px}
.settingToggle{display:flex;align-items:center;justify-content:flex-end;gap:8px;color:#f8fafc}
.settingToggle input{width:38px;height:20px;margin:0;accent-color:#22d3ee;cursor:pointer}
#saveSettings{width:100%;height:32px;margin-top:3px;border:0;border-radius:4px;background:#22d3ee;color:#06202a;font-size:12px;font-weight:700;cursor:pointer}
#saveSettings:disabled{cursor:wait;opacity:.55}
#settingsMessage{min-height:17px;margin-top:5px;color:#94a3b8;font-size:10px;line-height:1.45}
#ocrPanel{position:fixed;right:14px;top:14px;bottom:14px;z-index:4;display:flex;flex-direction:column;width:min(430px,calc(100vw - 396px));min-width:300px;overflow:hidden;background:rgba(5,6,7,.9);border:1px solid rgba(255,255,255,.3);border-radius:6px;backdrop-filter:blur(5px)}
#ocrHeader{display:flex;align-items:center;gap:8px;min-height:42px;padding:0 12px;border-bottom:1px solid rgba(255,255,255,.2)}
#ocrTitle{font-size:13px;font-weight:700}
#ocrSummary{margin-left:auto;color:#94a3b8;font-size:10px}
.ocrAction{height:26px;border:1px solid rgba(255,255,255,.28);border-radius:4px;background:rgba(255,255,255,.08);color:#f8fafc;padding:0 8px;font-size:11px;cursor:pointer}
.ocrAction:disabled{cursor:default;opacity:.35}
#ocrText{flex:1;margin:0;padding:12px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;color:#e5e7eb;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:13px;line-height:1.75;user-select:text}
#ocrText.waiting{color:#94a3b8}
@media(max-width:900px){#ocrPanel{left:14px;right:14px;top:auto;bottom:14px;width:auto;min-width:0;height:min(34vh,320px)}#panel{max-height:calc(66vh - 42px);overflow:auto}}
</style>
</head>
<body class="__ROTATION_CLASS__">
<div id="stage"><div id="viewport"><video id="video" autoplay muted playsinline disablepictureinpicture></video><canvas id="overlay"></canvas></div></div>
<section id="panel" aria-live="polite">
  <div id="headline"><span id="dot"></span><span id="statusText">正在连接</span><button id="settingsButton" type="button" aria-expanded="false">配置</button></div>
  <div id="detail">等待检测服务状态</div>
  <div id="progress"><div id="progressBar"></div></div>
  <div id="metrics"><span>视频</span><span id="videoState">连接中</span><span>纸张置信度</span><span id="confidence">--</span><span>检测耗时</span><span id="inference">--</span><span>A/B 采集</span><span id="burst">--</span><span>识别规则</span><span id="ruleText">读取中</span><span>患者查询</span><span id="patientQueryText">读取中</span><span>自动录入</span><span id="autoEntryText">读取中</span></div>
  <div id="resultBand">
    <div id="resultHeader"><span>最终号码</span><span id="verificationText">等待 A/B 验证</span></div>
    <div id="identifier" class="waiting">尚未生成</div>
    <div id="resultHint">验证通过后自动显示；取走纸张后立即隐藏</div>
    <div id="patientActions"><span id="patientSummary">等待患者查询</span><button id="exportPatient" class="ocrAction" type="button" disabled>导出患者JSON</button></div>
  </div>
  <div id="settings">
    <label class="settingRow"><span>网页视角</span><select id="displayRotation"><option value="0">0°（当前方向）</option><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select></label>
    <label class="settingRow"><span>OCR 视角</span><select id="ocrRotation"><option value="0">0°（当前方向）</option><option value="90">90°</option><option value="180">180°</option><option value="270">270°</option></select></label>
    <label class="settingRow"><span>目标字符数</span><input id="matchLength" type="number" min="1" max="64" step="1" inputmode="numeric"></label>
    <label class="settingRow"><span>字符类型</span><select id="matchCharset"><option value="alphanumeric">字母 + 数字</option><option value="digits">纯数字</option></select></label>
    <label class="settingRow"><span>患者查询</span><span class="settingToggle"><input id="patientQueryEnabled" type="checkbox"><span>生成JSON</span></span></label>
    <label class="settingRow"><span>自动录入</span><span class="settingToggle"><input id="autoEntryEnabled" type="checkbox"><span>启动HID</span></span></label>
    <button id="saveSettings" type="button">保存配置</button>
    <div id="settingsMessage">0°代表当前正确方向；OCR配置从下一张纸生效</div>
  </div>
</section>
<aside id="ocrPanel" aria-live="polite">
  <div id="ocrHeader"><span id="ocrTitle">全文识别</span><span id="ocrSummary">等待报告单</span><button id="copyText" class="ocrAction" type="button" disabled>复制</button><button id="exportText" class="ocrAction" type="button" disabled>导出</button></div>
  <pre id="ocrText" class="waiting">识别验证通过后显示全部文字</pre>
</aside>
<script>
const video=document.getElementById("video");
const canvas=document.getElementById("overlay");
const ctx=canvas.getContext("2d");
const panel=document.getElementById("panel");
const statusText=document.getElementById("statusText");
const detail=document.getElementById("detail");
const progressBar=document.getElementById("progressBar");
const videoState=document.getElementById("videoState");
const ruleText=document.getElementById("ruleText");
const identifier=document.getElementById("identifier");
const verificationText=document.getElementById("verificationText");
const resultHint=document.getElementById("resultHint");
const settingsButton=document.getElementById("settingsButton");
const settings=document.getElementById("settings");
const displayRotation=document.getElementById("displayRotation");
const ocrRotation=document.getElementById("ocrRotation");
const matchLength=document.getElementById("matchLength");
const matchCharset=document.getElementById("matchCharset");
const patientQueryEnabled=document.getElementById("patientQueryEnabled");
const autoEntryEnabled=document.getElementById("autoEntryEnabled");
const patientQueryText=document.getElementById("patientQueryText");
const autoEntryText=document.getElementById("autoEntryText");
const saveSettings=document.getElementById("saveSettings");
const settingsMessage=document.getElementById("settingsMessage");
const ocrSummary=document.getElementById("ocrSummary");
const ocrText=document.getElementById("ocrText");
const copyText=document.getElementById("copyText");
const exportText=document.getElementById("exportText");
const patientSummary=document.getElementById("patientSummary");
const exportPatient=document.getElementById("exportPatient");
let reader=null;
let generation=-1;
let currentStage="absent";
let currentDocument=null;
let currentPatient=null;
let lastStatus=null;

function applyDisplayRotation(value){
  const offset=[0,90,180,270].includes(Number(value))?Number(value):0;
  const rotation=(90+offset)%360;
  document.body.classList.remove("rotation-0","rotation-90","rotation-180","rotation-270");
  document.body.classList.add("rotation-"+rotation);
}

function fillConfig(config){
  displayRotation.value=String(config.display_rotation??0);
  ocrRotation.value=String(config.ocr_rotation??0);
  matchLength.value=String(config.match?.length??16);
  matchCharset.value=config.match?.charset||"alphanumeric";
  patientQueryEnabled.checked=Boolean(config.patient_query_enabled);
  autoEntryEnabled.checked=Boolean(config.auto_entry_enabled);
  applyDisplayRotation(displayRotation.value);
}

async function loadConfig(){
  try{
    const response=await fetch("/api/config",{cache:"no-store"});
    if(!response.ok)throw new Error("config request failed");
    fillConfig(await response.json());
  }catch(error){settingsMessage.textContent="无法读取当前配置";}
}

async function persistConfig(){
  const length=Number(matchLength.value);
  if(!Number.isInteger(length)||length<1||length>64){settingsMessage.textContent="目标字符数必须是 1 至 64 的整数";return;}
  saveSettings.disabled=true;
  settingsMessage.textContent="正在保存并重启识别服务";
  try{
    const response=await fetch("/api/config",{
      method:"POST",
      cache:"no-store",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({
        display_rotation:Number(displayRotation.value),
        ocr_rotation:Number(ocrRotation.value),
        match:{length:length,charset:matchCharset.value},
        patient_query_enabled:patientQueryEnabled.checked,
        auto_entry_enabled:autoEntryEnabled.checked
      })
    });
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||"save failed");
    fillConfig(result.config);
    renderRule(result.rule);
    settingsMessage.textContent="已保存；网页视角已更新，OCR 配置从下一张纸生效";
  }catch(error){settingsMessage.textContent="保存失败："+String(error.message||error);}
  finally{saveSettings.disabled=false;}
}

function setStatus(kind,title,message){panel.className=kind;statusText.textContent=title;detail.textContent=message;}
function connectVideo(){
  const base="http://"+location.hostname+":8891/camera/";
  const script=document.createElement("script");
  script.src=base+"reader.js";
  script.onload=()=>{
    reader=new MediaMTXWebRTCReader({
      url:base+"whep",
      onError:()=>{videoState.textContent="连接失败";},
      onTrack:ev=>{
        try{ev.receiver.playoutDelayHint=0;}catch(e){}
        try{ev.receiver.jitterBufferTarget=0;}catch(e){}
        const stream=ev.streams&&ev.streams[0]?ev.streams[0]:new MediaStream([ev.track]);
        video.srcObject=stream;
        video.play().catch(()=>{videoState.textContent="播放失败";});
      },
      onDataChannel:()=>{}
    });
  };
  script.onerror=()=>{videoState.textContent="组件失败";};
  document.head.appendChild(script);
}

function resizeCanvas(width,height){
  if(!width||!height)return;
  if(canvas.width!==width||canvas.height!==height){canvas.width=width;canvas.height=height;}
}

function drawPaper(result){
  const size=result.frame_size||{width:1920,height:1080};
  resizeCanvas(size.width,size.height);
  ctx.clearRect(0,0,canvas.width,canvas.height);
  const points=result.active&&result.paper_detected&&Array.isArray(result.paper_corners)?result.paper_corners:[];
  if(points.length===4){
    const accepted=result.capture_stage==="verified";
    const rejected=result.capture_stage==="verification_rejected"||result.capture_stage==="ocr_error";
    ctx.lineWidth=Math.max(3,canvas.width/640);
    ctx.strokeStyle=accepted?"#22c55e":rejected?"#ef4444":"#22d3ee";
    ctx.fillStyle=accepted?"rgba(34,197,94,.08)":rejected?"rgba(239,68,68,.08)":"rgba(34,211,238,.07)";
    ctx.beginPath();ctx.moveTo(points[0][0],points[0][1]);
    for(let i=1;i<points.length;i++)ctx.lineTo(points[i][0],points[i][1]);
    ctx.closePath();ctx.fill();ctx.stroke();
    for(const point of points){ctx.beginPath();ctx.arc(point[0],point[1],Math.max(5,canvas.width/260),0,Math.PI*2);ctx.fillStyle=ctx.strokeStyle;ctx.fill();}
  }
  drawOcrBlocks(currentDocument);
}

function unrotatePoint(x,y,rotation){
  if(rotation===90)return [1-y,x];
  if(rotation===180)return [1-x,1-y];
  if(rotation===270)return [y,1-x];
  return [x,y];
}

function paperPoint(x,y,source){
  const corners=source.paper_corners;
  const [u,v]=unrotatePoint(x,y,source.ocr_rotation||0);
  const weights=[(1-u)*(1-v),u*(1-v),u*v,(1-u)*v];
  const sx=canvas.width/(source.frame_size?.width||canvas.width);
  const sy=canvas.height/(source.frame_size?.height||canvas.height);
  return [
    corners.reduce((sum,point,index)=>sum+point[0]*weights[index],0)*sx,
    corners.reduce((sum,point,index)=>sum+point[1]*weights[index],0)*sy
  ];
}

function drawOcrBlocks(documentResult){
  if(!documentResult||!documentResult.available)return;
  const source=documentResult.source||{};
  const corners=source.paper_corners;
  if(!Array.isArray(corners)||corners.length!==4)return;
  ctx.lineWidth=Math.max(1.5,canvas.width/1100);
  ctx.strokeStyle="#fbbf24";
  ctx.fillStyle="rgba(251,191,36,.055)";
  for(const block of documentResult.blocks||[]){
    const box=block.normalized_box;
    if(!Array.isArray(box)||box.length!==4)continue;
    const polygon=[
      paperPoint(box[0]/1000,box[1]/1000,source),
      paperPoint(box[2]/1000,box[1]/1000,source),
      paperPoint(box[2]/1000,box[3]/1000,source),
      paperPoint(box[0]/1000,box[3]/1000,source)
    ];
    ctx.beginPath();ctx.moveTo(polygon[0][0],polygon[0][1]);
    for(let i=1;i<polygon.length;i++)ctx.lineTo(polygon[i][0],polygon[i][1]);
    ctx.closePath();ctx.fill();ctx.stroke();
  }
}

function clearDocument(message="识别验证通过后显示全部文字"){
  currentDocument=null;
  ocrSummary.textContent="等待报告单";
  ocrText.textContent=message;
  ocrText.className="waiting";
  copyText.disabled=true;
  exportText.disabled=true;
  if(lastStatus)drawPaper(lastStatus);
}

function renderDocument(documentResult){
  if(!documentResult||!documentResult.available){clearDocument("全文结果尚未就绪");return;}
  currentDocument=documentResult;
  ocrSummary.textContent=`${documentResult.line_count||0} 行 · ${documentResult.item_count||0} 块 · ${Math.round((documentResult.mean_confidence||0)*100)}%`;
  ocrText.textContent=documentResult.full_text||"";
  ocrText.className="";
  copyText.disabled=false;
  exportText.disabled=false;
  if(lastStatus)drawPaper(lastStatus);
}

function burstText(result){
  const current=result.burst||{};
  const a=result.burst_a||{};
  const b=result.burst_b||{};
  if(result.capture_stage==="collecting_a")return `A ${current.collected_frames||0}/${current.target_frames||3}`;
  if(result.capture_stage==="collecting_b")return `A 完成 · B ${current.collected_frames||0}/${current.target_frames||3}`;
  if(result.capture_stage==="verified")return "A = B";
  if(result.capture_stage==="verification_rejected")return "A ≠ B";
  if(a.ready&&b.ready)return "A、B 完成";
  if(a.ready)return "A 完成";
  return "--";
}

function charsetLabel(value){return value==="digits"?"纯数字":value==="alphanumeric"?"字母+数字":"字符";}
function renderRule(rule){
  const fields=rule&&Array.isArray(rule.fields)?rule.fields:[];
  const field=fields[0]||{};
  const lengths=Array.isArray(field.lengths)?field.lengths.join("/"):"--";
  ruleText.textContent=lengths==="--"?"未配置":`唯一 ${lengths} 位 · ${charsetLabel(field.charset)}`;
}

function clearIdentifier(message){
  identifier.textContent=message;
  identifier.className="waiting";
}

function clearPatient(message="等待患者查询"){
  currentPatient=null;
  patientSummary.textContent=message;
  exportPatient.disabled=true;
}

async function pollPatient(){
  if(["absent","tracking","inactive"].includes(currentStage)){
    if(currentPatient)clearPatient();
    window.setTimeout(pollPatient,500);
    return;
  }
  try{
    const response=await fetch("/api/patient",{cache:"no-store"});
    const result=await response.json();
    if(result.code==="PENDING"||result.code==="WAITING"){
      clearPatient(result.msg||"患者信息查询中");
    }else{
      currentPatient=result;
      exportPatient.disabled=false;
      const count=Array.isArray(result.data)?result.data.length:0;
      patientSummary.textContent=result.success?`已查询 ${count} 条记录`:(result.msg||"患者查询失败");
    }
  }catch(error){clearPatient("患者结果接口暂不可用");}
  window.setTimeout(pollPatient,500);
}

async function pollResult(){
  if(["absent","tracking","inactive"].includes(currentStage)){
    verificationText.textContent="等待 A/B 验证";
    clearIdentifier("尚未生成");
    resultHint.textContent="单号验证与全文识别独立运行";
    if(currentDocument)clearDocument();
    window.setTimeout(pollResult,400);
    return;
  }
  try{
    const response=await fetch("/api/result",{cache:"no-store"});
    const result=await response.json();
    renderRule(result.rule);
    if(result.identifier_available&&result.identifier){
      verificationText.textContent="A/B 完全一致";
      identifier.textContent=result.identifier;
      identifier.className="";
      resultHint.textContent="当前纸张已通过两次独立识别；取走后自动隐藏";
    }else{
      const verification=result.verification||{};
      verificationText.textContent=verification.status==="rejected"?"单号未通过":"等待 A/B 验证";
      clearIdentifier("单号尚未验证");
      resultHint.textContent="全文识别不受单号规则影响";
    }
    if(result.document&&result.document.available)renderDocument(result.document);
    else if(currentDocument)clearDocument("正在生成全文结果");
  }catch(error){clearIdentifier("结果接口暂不可用");clearDocument("全文结果接口暂不可用");resultHint.textContent="无法读取识别结果";}
  window.setTimeout(pollResult,400);
}

function renderStatus(result){
  lastStatus=result;
  const serviceState=result.service_state||("active" in result?(result.active?"active":"offline"):"waiting");
  const detectorService=result.detector_service||{};
  const detectorUnitState=detectorService.state||"unknown";
  currentStage=(result.active||serviceState==="busy")?(result.capture_stage||result.state):"inactive";
  if(["absent","tracking","inactive"].includes(currentStage)&&currentDocument)clearDocument();
  drawPaper(result);
  renderRule(result.rule);
  document.getElementById("confidence").textContent=result.paper_detected?`${Math.round((result.paper_confidence||0)*100)}%`:"--";
  document.getElementById("inference").textContent=result.paper_inference_ms!=null?`${result.paper_inference_ms.toFixed(1)} ms`:"--";
  document.getElementById("burst").textContent=burstText(result);
  const patientQuery=result.patient_query||{};
  const autoEntry=result.auto_entry||result.forwarding||{};
  const actionLabels={disabled:"关闭",clear_required:"请先取走当前纸张",waiting:"等待号码",armed:"下一张生效",sending:"处理中",sent:"已完成",error:"失败重试"};
  patientQueryText.textContent=patientQuery.state==="sent"?`已查询 ${patientQuery.record_count||0} 条`:(actionLabels[patientQuery.state]||(patientQuery.enabled?"等待号码":"关闭"));
  autoEntryText.textContent=actionLabels[autoEntry.state]||(autoEntry.enabled?"等待号码":"关闭");
  const target=Math.max(.1,result.stable_target_seconds||.8);
  const progress=Math.max(0,Math.min(100,(result.stable_for||0)/target*100));
  progressBar.style.width=`${progress}%`;
  if(!result.active){
    if(serviceState==="busy"){
      setStatus("","正在处理识别","OCR 运行时状态更新会短暂停顿，请保持纸张不动");
    }else if(detectorUnitState==="activating"){
      setStatus("","检测服务正在启动","初始化相机和模型后会自动开始检测");
    }else if(detectorUnitState==="active"){
      setStatus("","等待检测状态更新","检测服务正在运行，当前识别任务可能仍在处理中");
    }else if(["inactive","failed","not-found"].includes(detectorUnitState)){
      setStatus("error","检测服务未运行","服务状态："+detectorUnitState+"，请检查 rk3588-report-camera-trigger");
    }else if(serviceState==="waiting"){
      setStatus("","正在等待首次检测状态","页面会在检测服务完成初始化后自动更新");
    }else{
      setStatus("error","检测状态长时间未更新","无法确认服务是否运行，请查看服务状态和日志");
    }
    return;
  }
  const stage=result.capture_stage||result.state;
  const labels={
    absent:["等待报告单","把带号码的纸张放入画面"],
    tracking:["已检测到纸张","请保持不动，正在确认稳定"],
    locked:["纸张已锁定","正在检查画面中的文字"],
    collecting_a:["采集第一组图像","正在从三帧中选择最清晰画面"],
    field_a_ready:["第一组识别完成","准备进行第二次独立识别"],
    waiting_b:["等待第二次识别","短暂等待后重新采集三帧"],
    retry_waiting_a:["正在重新验证","两次结果不一致，准备重试"],
    collecting_b:["采集第二组图像","正在从三帧中选择最清晰画面"],
    reposition_required:["未检测到文字","请移动纸张，使带号码区域清晰可见"],
    burst_rejected:["图像质量不足","请减少反光并保持纸张稳定"],
    ocr_error:["OCR 暂不可用","请检查本地 OCR 服务"],
    verified:["识别验证通过","两次独立识别结果一致，可以取走纸张"],
    verification_rejected:["单号验证未通过","全文结果不受单号规则影响"]
  };
  const selected=labels[stage]||["正在检测",String(stage||"")];
  setStatus(stage==="verified"?"ready":stage==="verification_rejected"||stage==="ocr_error"?"error":"",selected[0],selected[1]);
}

async function pollStatus(){
  try{
    const response=await fetch("/api/status?generation="+generation,{cache:"no-store"});
    const result=await response.json();
    generation=result.generation;
    renderStatus(result);
  }catch(error){setStatus("error","状态服务断开","无法读取 DocAligner 检测状态");}
  window.setTimeout(pollStatus,200);
}

video.addEventListener("playing",()=>{video.classList.add("playing");videoState.textContent="实时";});
settingsButton.addEventListener("click",()=>{
  const open=!settings.classList.contains("open");
  settings.classList.toggle("open",open);
  settingsButton.setAttribute("aria-expanded",String(open));
  settingsButton.textContent=open?"收起":"配置";
});
displayRotation.addEventListener("change",()=>applyDisplayRotation(displayRotation.value));
saveSettings.addEventListener("click",persistConfig);
copyText.addEventListener("click",async()=>{
  if(!currentDocument)return;
  try{await navigator.clipboard.writeText(currentDocument.full_text||"");ocrSummary.textContent="已复制全文";}
  catch(error){ocrSummary.textContent="复制失败";}
});
exportText.addEventListener("click",()=>{
  if(!currentDocument)return;
  const blob=new Blob([JSON.stringify(currentDocument,null,2)],{type:"application/json"});
  const url=URL.createObjectURL(blob);
  const link=document.createElement("a");link.href=url;link.download="rk3588-ocr-result.json";link.click();
  window.setTimeout(()=>URL.revokeObjectURL(url),1000);
});
exportPatient.addEventListener("click",()=>{
  if(!currentPatient)return;
  const blob=new Blob([JSON.stringify(currentPatient,null,2)],{type:"application/json"});
  const url=URL.createObjectURL(blob);
  const link=document.createElement("a");link.href=url;link.download="rk3588-patient-result.json";link.click();
  window.setTimeout(()=>URL.revokeObjectURL(url),1000);
});
window.addEventListener("beforeunload",()=>{if(reader)reader.close();});
connectVideo();loadConfig();pollStatus();pollResult();pollPatient();
</script>
</body>
</html>""".encode("utf-8")


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _safe_burst(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in (
            "ready",
            "accepted",
            "collected_frames",
            "target_frames",
            "rejected_frames",
            "quality_failures",
            "best_frame_index",
        )
        if key in value and isinstance(value[key], (bool, int, float))
    }


def normalize_trigger_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    size = payload.get("frame_size") if isinstance(payload.get("frame_size"), dict) else {}
    width = max(1, int(_number(size.get("width"), 1920)))
    height = max(1, int(_number(size.get("height"), 1080)))
    corners = []
    for point in payload.get("paper_corners") or []:
        if not isinstance(point, list) or len(point) != 2:
            continue
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in point):
            continue
        corners.append([
            max(0.0, min(float(width), float(point[0]))),
            max(0.0, min(float(height), float(point[1]))),
        ])
    if len(corners) != 4:
        corners = []
    verification = payload.get("verification") if isinstance(payload.get("verification"), dict) else {}
    capture_id = payload.get("capture_id")
    if not isinstance(capture_id, str) or re.fullmatch(r"[0-9a-f]{32}", capture_id) is None:
        capture_id = ""
    full_text = payload.get("full_text") if isinstance(payload.get("full_text"), dict) else {}
    return {
        "paper_detected": bool(payload.get("paper_detected")) and len(corners) == 4,
        "paper_confidence": round(_number(payload.get("paper_confidence")), 4),
        "paper_inference_ms": round(_number(payload.get("paper_inference_ms")), 2),
        "frame_size": {"width": width, "height": height},
        "paper_corners": corners,
        "state": str(payload.get("state") or "absent")[:48],
        "reason": str(payload.get("reason") or "")[:96],
        "stable_for": round(max(0.0, _number(payload.get("stable_for"))), 3),
        "stable_target_seconds": round(max(0.1, _number(payload.get("stable_target_seconds"), 0.8)), 3),
        "capture_stage": str(payload.get("capture_stage") or payload.get("state") or "absent")[:64],
        "ocr_available": bool(payload.get("ocr_available")),
        "text_detected": bool(payload.get("text_detected")),
        "capture_id": capture_id,
        "full_text": {
            "available": bool(full_text.get("available")),
            "status": str(full_text.get("status") or "waiting")[:32],
            "line_count": max(0, int(_number(full_text.get("line_count")))),
            "item_count": max(0, int(_number(full_text.get("item_count")))),
            "mean_confidence": round(max(0.0, min(1.0, _number(full_text.get("mean_confidence")))), 4),
            "elapsed_ms": round(max(0.0, _number(full_text.get("elapsed_ms"))), 2),
        },
        "burst": _safe_burst(payload.get("burst")),
        "burst_a": _safe_burst(payload.get("burst_a")),
        "burst_b": _safe_burst(payload.get("burst_b")),
        "verification": {
            key: verification[key]
            for key in ("status", "reason", "attempt")
            if key in verification and isinstance(verification[key], (str, int))
        },
    }


def normalize_rule_summary(payload: Dict[str, Any]) -> Dict[str, Any]:
    fields = []
    for raw in payload.get("fields") or []:
        if not isinstance(raw, dict) or raw.get("enabled", True) is False:
            continue
        lengths = sorted(
            {
                int(value)
                for value in raw.get("lengths") or []
                if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 128
            }
        )
        charset = str(raw.get("charset") or "")
        if not lengths or charset not in {"digits", "alphanumeric"}:
            continue
        fields.append(
            {
                "lengths": lengths,
                "charset": charset,
                "allow_unlabeled": bool(raw.get("allow_unlabeled")),
            }
        )
    return {
        "enabled": bool(payload.get("enabled", True)),
        "profile": str(payload.get("profile") or "")[:96],
        "selection": "unique_character_count",
        "fields": fields,
    }


def load_rule_summary(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("rules must be a JSON object")
        return normalize_rule_summary(payload)
    except (OSError, ValueError, json.JSONDecodeError):
        return normalize_rule_summary({})


def _identifier_matches_rule(identifier: str, rule: Dict[str, Any]) -> bool:
    for field in rule.get("fields") or []:
        if len(identifier) not in field.get("lengths", []):
            continue
        charset = field.get("charset")
        if charset == "digits" and identifier.isascii() and identifier.isdigit():
            return True
        if charset == "alphanumeric" and re.fullmatch(r"[A-Za-z0-9]+", identifier):
            return True
    return False


def _full_text_document(payload: Any, expected_capture_id: str) -> Dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("status") != "accepted":
        return {"available": False, "status": "invalid"}
    capture_id = payload.get("capture_id")
    if not isinstance(capture_id, str) or not capture_id or capture_id != expected_capture_id:
        return {"available": False, "status": "stale"}

    source = payload.get("source")
    document = payload.get("document")
    if not isinstance(source, dict) or not isinstance(document, dict):
        return {"available": False, "status": "invalid"}

    source_size = source.get("frame_size")
    if not isinstance(source_size, dict):
        return {"available": False, "status": "invalid"}
    source_width = int(_number(source_size.get("width")))
    source_height = int(_number(source_size.get("height")))
    rotation = source.get("ocr_rotation")
    if (
        source_width < 1
        or source_height < 1
        or isinstance(rotation, bool)
        or rotation not in {0, 90, 180, 270}
    ):
        return {"available": False, "status": "invalid"}
    corners = []
    for point in source.get("paper_corners") or []:
        if not isinstance(point, list) or len(point) != 2:
            continue
        x, y = _number(point[0], -1.0), _number(point[1], -1.0)
        if not 0 <= x <= source_width or not 0 <= y <= source_height:
            continue
        corners.append([round(x, 3), round(y, 3)])
    if len(corners) != 4:
        return {"available": False, "status": "invalid"}

    image_size = document.get("image_size")
    if not isinstance(image_size, list) or len(image_size) != 2:
        return {"available": False, "status": "invalid"}
    image_width = int(_number(image_size[0]))
    image_height = int(_number(image_size[1]))
    if image_width < 1 or image_height < 1:
        return {"available": False, "status": "invalid"}

    blocks = []
    for raw in (document.get("blocks") or [])[:2048]:
        if not isinstance(raw, dict):
            continue
        text = raw.get("text")
        normalized_box = raw.get("normalized_box")
        if not isinstance(text, str) or not text or len(text) > 4096:
            continue
        if not isinstance(normalized_box, list) or len(normalized_box) != 4:
            continue
        box = [int(_number(value, -1.0)) for value in normalized_box]
        if any(value < 0 or value > 1000 for value in box):
            continue
        span_id = int(_number(raw.get("id")))
        line_id = int(_number(raw.get("line_id")))
        if span_id < 1 or line_id < 1:
            continue
        blocks.append(
            {
                "id": span_id,
                "line_id": line_id,
                "text": text,
                "normalized_box": box,
                "score": round(max(0.0, min(1.0, _number(raw.get("score")))), 4),
            }
        )
    blocks.sort(key=lambda item: (item["line_id"], item["normalized_box"][0], item["id"]))
    if not blocks:
        return {"available": False, "status": "invalid"}

    grouped: Dict[int, list[Dict[str, Any]]] = {}
    for block in blocks:
        grouped.setdefault(block["line_id"], []).append(block)
    lines = [
        {
            "line_id": line_id,
            "text": " ".join(block["text"] for block in line_blocks),
            "span_ids": [block["id"] for block in line_blocks],
        }
        for line_id, line_blocks in grouped.items()
    ]
    mean_confidence = sum(block["score"] for block in blocks) / len(blocks)
    return {
        "available": True,
        "status": "accepted",
        "capture_id": capture_id,
        "source": {
            "frame_size": {"width": source_width, "height": source_height},
            "paper_corners": corners,
            "ocr_rotation": rotation,
        },
        "image_size": {"width": image_width, "height": image_height},
        "full_text": "\n".join(line["text"] for line in lines),
        "lines": lines,
        "blocks": blocks,
        "line_count": len(lines),
        "item_count": len(blocks),
        "mean_confidence": round(mean_confidence, 4),
    }


class VerifiedResultStore:
    def __init__(
        self,
        result_path: Path,
        rules_path: Path,
        full_text_path: Optional[Path] = None,
    ) -> None:
        self.result_path = result_path
        self.rules_path = rules_path
        self.full_text_path = full_text_path

    def rule_summary(self) -> Dict[str, Any]:
        return load_rule_summary(self.rules_path)

    def snapshot(self, live_status: Dict[str, Any]) -> Dict[str, Any]:
        rule = self.rule_summary()
        verification = live_status.get("verification") or {}
        live_capture_id = live_status.get("capture_id")
        if not isinstance(live_capture_id, str) or not live_capture_id:
            live_capture_id = ""
        document = {"available": False, "status": "waiting"}
        if live_status.get("active") is True and live_capture_id and self.full_text_path is not None:
            try:
                full_text_payload = json.loads(self.full_text_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
            else:
                document = _full_text_document(full_text_payload, live_capture_id)

        verified = (
            live_status.get("active") is True
            and live_status.get("capture_stage") == "verified"
            and verification.get("status") == "accepted"
        )
        if not verified:
            return {
                "available": bool(document.get("available")),
                "status": "accepted" if document.get("available") else "waiting",
                "identifier_available": False,
                "document": document,
                "verification": {
                    key: verification[key]
                    for key in ("status", "reason", "attempt")
                    if key in verification
                },
                "rule": rule,
            }
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
            identifier = payload.get("identifier") if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError):
            identifier = None
            payload = {}
        if (
            not isinstance(identifier, str)
            or payload.get("status") != "accepted"
            or not _identifier_matches_rule(identifier, rule)
            or (live_capture_id and payload.get("capture_id") != live_capture_id)
        ):
            return {
                "available": bool(document.get("available")),
                "status": "accepted" if document.get("available") else "invalid",
                "identifier_available": False,
                "document": document,
                "rule": rule,
            }
        result = {
            "available": True,
            "status": "accepted",
            "identifier_available": True,
            "identifier": identifier,
            "verification": {
                key: verification[key]
                for key in ("status", "reason", "attempt")
                if key in verification
            },
            "rule": rule,
            "document": document,
        }
        return result

    def forwarding_candidate(self, live_status: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        public = self.snapshot(live_status)
        identifier = public.get("identifier")
        if not public.get("available") or not isinstance(identifier, str):
            return None
        try:
            payload = json.loads(self.result_path.read_text(encoding="utf-8"))
            created_at = float(payload.get("created_at"))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
        event_key = hashlib.sha256(
            (identifier + "\0" + format(created_at, ".6f")).encode("utf-8")
        ).hexdigest()
        capture_id = payload.get("capture_id")
        if not isinstance(capture_id, str) or not capture_id:
            capture_id = event_key
        return {
            "identifier": identifier,
            "created_at": created_at,
            "event_key": event_key,
            "capture_id": capture_id,
        }


def _validated_rotation(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 90, 180, 270}:
        raise ValueError("%s must be one of 0, 90, 180, 270" % field)
    return value


def _write_text_atomic(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    try:
        temporary.chmod(mode)
    except OSError:
        pass
    temporary.replace(path)


def _patient_envelope(
    code: str,
    message: str,
    success: bool,
    data: Optional[list[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "code": code,
        "data": list(data or []),
        "msg": message,
        "success": success,
    }


def _canonical_patient_payload(payload: Any) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("patient response must be an object")
    raw_data = payload.get("data")
    if raw_data is None:
        raw_data = []
    if not isinstance(raw_data, list) or any(not isinstance(item, dict) for item in raw_data):
        raise ValueError("patient response data must be an object array")
    success = payload.get("success") is True
    records = [
        {field: item.get(field) for field in PATIENT_RESPONSE_FIELDS}
        for item in raw_data
    ]
    return _patient_envelope(
        str(payload.get("code") or ("SUCCESS" if success else "FAIL")),
        str(payload.get("msg") or ("成功" if success else "患者查询失败")),
        success,
        records,
    )


class VerifiedPatientResultStore:
    def __init__(self, result_path: Path, metadata_path: Path) -> None:
        self.result_path = result_path
        self.metadata_path = metadata_path
        self.lock = threading.Lock()

    def write(
        self,
        capture_id: str,
        event_key: str,
        payload: Dict[str, Any],
        created_at: Optional[float] = None,
    ) -> Dict[str, Any]:
        canonical = _canonical_patient_payload(payload)
        metadata = {
            "capture_id": capture_id,
            "event_key": event_key,
            "created_at": time.time() if created_at is None else float(created_at),
            "success": canonical["success"],
            "record_count": len(canonical["data"]),
        }
        with self.lock:
            _write_text_atomic(
                self.result_path,
                json.dumps(canonical, ensure_ascii=False, indent=2) + "\n",
                mode=0o600,
            )
            _write_text_atomic(
                self.metadata_path,
                json.dumps(metadata, ensure_ascii=True, indent=2) + "\n",
                mode=0o600,
            )
        return canonical

    def snapshot(self, live_status: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        capture_id = live_status.get("capture_id")
        if live_status.get("active") is not True or not isinstance(capture_id, str) or not capture_id:
            return HTTPStatus.NOT_FOUND, _patient_envelope(
                "WAITING", "等待当前报告单", False
            )
        try:
            with self.lock:
                metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                payload = json.loads(self.result_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict) or metadata.get("capture_id") != capture_id:
                return HTTPStatus.ACCEPTED, _patient_envelope(
                    "PENDING", "患者信息查询中", False
                )
            canonical = _canonical_patient_payload(payload)
        except (OSError, ValueError, json.JSONDecodeError):
            return HTTPStatus.ACCEPTED, _patient_envelope(
                "PENDING", "患者信息查询中", False
            )
        if canonical["code"] == "PENDING":
            return HTTPStatus.ACCEPTED, canonical
        return (HTTPStatus.OK if canonical["success"] else HTTPStatus.BAD_GATEWAY, canonical)

    def metadata(self, capture_id: str) -> Dict[str, Any]:
        try:
            with self.lock:
                payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or payload.get("capture_id") != capture_id:
            return {}
        return payload


class CaptureConfigurationStore:
    def __init__(
        self,
        settings_path: Path,
        rules_path: Path,
        trigger_environment_path: Path,
        default_display_rotation: int = 0,
        default_ocr_rotation: int = 0,
        restart_trigger: Optional[Callable[[], None]] = None,
    ) -> None:
        self.settings_path = settings_path
        self.rules_path = rules_path
        self.trigger_environment_path = trigger_environment_path
        self.default_display_rotation = _validated_rotation(
            default_display_rotation, "default_display_rotation"
        )
        self.default_ocr_rotation = _validated_rotation(default_ocr_rotation, "default_ocr_rotation")
        self.restart_trigger = restart_trigger
        self.lock = threading.Lock()

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> Dict[str, Any]:
        display_rotation = self.default_display_rotation
        ocr_rotation = self.default_ocr_rotation
        patient_query_enabled = True
        patient_query_enabled_at = 0.0
        auto_entry_enabled = False
        auto_entry_enabled_at = 0.0
        try:
            payload = json.loads(self.settings_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                stored_display_rotation = _validated_rotation(
                    payload.get("display_rotation"), "display_rotation"
                )
                stored_ocr_rotation = _validated_rotation(
                    payload.get("ocr_rotation"), "ocr_rotation"
                )
                if payload.get("version") in {2, 3}:
                    display_rotation = stored_display_rotation
                    ocr_rotation = stored_ocr_rotation
                else:
                    display_rotation = (stored_display_rotation - DISPLAY_BASE_ROTATION) % 360
                    ocr_rotation = (stored_ocr_rotation - OCR_BASE_ROTATION) % 360
                legacy_auto_entry = bool(payload.get("forward_to_gateway", False))
                patient_query_enabled = bool(payload.get("patient_query_enabled", True))
                patient_query_enabled_at = max(
                    0.0,
                    _number(
                        payload.get(
                            "patient_query_enabled_at",
                            payload.get("updated_at", 0.0),
                        )
                    ),
                )
                auto_entry_enabled = bool(
                    payload.get("auto_entry_enabled", legacy_auto_entry)
                )
                auto_entry_enabled_at = max(
                    0.0,
                    _number(
                        payload.get(
                            "auto_entry_enabled_at",
                            payload.get("forwarding_enabled_at", 0.0),
                        )
                    ),
                )
        except (OSError, ValueError, json.JSONDecodeError):
            pass

        rule = load_rule_summary(self.rules_path)
        fields = rule.get("fields") or []
        field = fields[0] if fields else {"lengths": [16], "charset": "alphanumeric"}
        lengths = field.get("lengths") or [16]
        return {
            "display_rotation": display_rotation,
            "ocr_rotation": ocr_rotation,
            "match": {
                "length": int(lengths[0]),
                "charset": field.get("charset") or "alphanumeric",
            },
            "patient_query_enabled": patient_query_enabled,
            "patient_query_enabled_at": patient_query_enabled_at,
            "auto_entry_enabled": auto_entry_enabled,
            "auto_entry_enabled_at": auto_entry_enabled_at,
            "forward_to_gateway": auto_entry_enabled,
            "forwarding_enabled_at": auto_entry_enabled_at,
        }

    def update(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("configuration must be a JSON object")
        display_rotation = _validated_rotation(payload.get("display_rotation"), "display_rotation")
        ocr_rotation = _validated_rotation(payload.get("ocr_rotation"), "ocr_rotation")
        match = payload.get("match")
        if not isinstance(match, dict):
            raise ValueError("match must be a JSON object")
        length = match.get("length")
        if isinstance(length, bool) or not isinstance(length, int) or not 1 <= length <= 64:
            raise ValueError("match.length must be an integer from 1 to 64")
        charset = match.get("charset")
        if charset not in {"digits", "alphanumeric"}:
            raise ValueError("match.charset must be digits or alphanumeric")
        patient_query_enabled = payload.get("patient_query_enabled", True)
        if not isinstance(patient_query_enabled, bool):
            raise ValueError("patient_query_enabled must be a boolean")
        auto_entry_enabled = payload.get(
            "auto_entry_enabled",
            payload.get("forward_to_gateway", False),
        )
        if not isinstance(auto_entry_enabled, bool):
            raise ValueError("auto_entry_enabled must be a boolean")

        rules_payload = {
            "enabled": True,
            "profile": "single-length-%d" % length,
            "fields": [
                {
                    "type": "selected_identifier",
                    "lengths": [length],
                    "charset": charset,
                    "prefixes": [],
                    "priority": 1000,
                    "enabled": True,
                    "allow_unlabeled": True,
                }
            ],
        }
        with self.lock:
            current = self._snapshot_unlocked()
            now = time.time()
            patient_query_enabled_at = float(current.get("patient_query_enabled_at") or 0.0)
            if patient_query_enabled and not current.get("patient_query_enabled"):
                patient_query_enabled_at = now
            elif not patient_query_enabled:
                patient_query_enabled_at = 0.0
            auto_entry_enabled_at = float(current.get("auto_entry_enabled_at") or 0.0)
            if auto_entry_enabled and not current.get("auto_entry_enabled"):
                auto_entry_enabled_at = now
            elif not auto_entry_enabled:
                auto_entry_enabled_at = 0.0
            settings_payload = {
                "version": 3,
                "rotation_reference": "current_orientation_zero",
                "display_rotation": display_rotation,
                "ocr_rotation": ocr_rotation,
                "patient_query_enabled": patient_query_enabled,
                "patient_query_enabled_at": patient_query_enabled_at,
                "auto_entry_enabled": auto_entry_enabled,
                "auto_entry_enabled_at": auto_entry_enabled_at,
                "forward_to_gateway": auto_entry_enabled,
                "forwarding_enabled_at": auto_entry_enabled_at,
                "updated_at": now,
            }
            restart_required = (
                current.get("ocr_rotation") != ocr_rotation
                or current.get("match") != {"length": length, "charset": charset}
            )
            _write_text_atomic(
                self.settings_path,
                json.dumps(settings_payload, ensure_ascii=False, indent=2) + "\n",
            )
            _write_text_atomic(
                self.rules_path,
                json.dumps(rules_payload, ensure_ascii=False, indent=2) + "\n",
            )
            _write_text_atomic(
                self.trigger_environment_path,
                "OCR_ROTATION=%d\n" % ((OCR_BASE_ROTATION + ocr_rotation) % 360),
            )
            if self.restart_trigger is not None and restart_required:
                self.restart_trigger()
            return self._snapshot_unlocked()


class TriggerStatusCache:
    def __init__(self, status_path: Path, stale_seconds: float, offline_seconds: float = 30.0) -> None:
        self.status_path = status_path
        self.stale_seconds = max(0.5, stale_seconds)
        self.offline_seconds = max(self.stale_seconds + 0.5, offline_seconds)
        self.lock = threading.Lock()
        self.last_mtime_ns = -1
        self.generation = 0
        self.updated_at = 0.0
        self.status = normalize_trigger_status({})
        self.error = None

    def snapshot(self) -> Dict[str, Any]:
        self._refresh()
        with self.lock:
            status = dict(self.status)
            updated_at = self.updated_at
            generation = self.generation
            error = self.error
        age_ms = None if not updated_at else max(0, int(round((time.time() - updated_at) * 1000)))
        active = age_ms is not None and age_ms <= int(self.stale_seconds * 1000) and error is None
        if error is not None:
            service_state = "offline"
        elif age_ms is None:
            service_state = "waiting"
        elif active:
            service_state = "active"
        elif age_ms <= int(self.offline_seconds * 1000):
            service_state = "busy"
        else:
            service_state = "offline"
        return {
            "ok": error is None,
            "active": active,
            "service_state": service_state,
            "generation": generation,
            "status_age_ms": age_ms,
            "error": error,
            **status,
        }

    def _refresh(self) -> None:
        try:
            stat = self.status_path.stat()
        except OSError:
            with self.lock:
                self.error = None
            return
        if stat.st_mtime_ns == self.last_mtime_ns:
            return
        try:
            payload = json.loads(self.status_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("trigger status must be a JSON object")
            status = normalize_trigger_status(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            with self.lock:
                self.error = type(exc).__name__
            return
        with self.lock:
            self.last_mtime_ns = stat.st_mtime_ns
            self.generation += 1
            self.updated_at = stat.st_mtime
            self.status = status
            self.error = None


class SystemdServiceProbe:
    def __init__(self, unit: str, refresh_seconds: float = 2.0) -> None:
        self.unit = unit
        self.refresh_seconds = max(0.5, refresh_seconds)
        self.lock = threading.Lock()
        self.checked_at = 0.0
        self.state = "unknown"

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        current_time = time.time() if now is None else now
        with self.lock:
            if current_time - self.checked_at >= self.refresh_seconds:
                self.state = self._query()
                self.checked_at = current_time
            return {
                "unit": self.unit,
                "state": self.state,
                "checked_at": self.checked_at,
            }

    def _query(self) -> str:
        try:
            completed = subprocess.run(
                ["systemctl", "is-active", self.unit],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            return "unknown"
        state = completed.stdout.strip()
        return state if state in {"active", "activating", "deactivating", "inactive", "failed"} else "not-found"


def _post_gateway_scan(endpoint: str, identifier: str, timeout_seconds: float) -> None:
    request = Request(
        endpoint,
        data=json.dumps({"code": identifier}, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "RK3588-Camera-Identifier"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("gateway scan request failed: %s" % type(exc).__name__) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise RuntimeError("gateway scan request was not accepted")


class PatientQueryRequestError(RuntimeError):
    def __init__(self, reason: str, payload: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(reason)
        self.payload = payload or _patient_envelope("FAIL", "患者查询失败", False)


def _post_gateway_patient_query(
    endpoint: str,
    identifier: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    request = Request(
        endpoint,
        data=json.dumps({"code": identifier}, ensure_ascii=True).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "RK3588-Camera-Patient"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
    except HTTPError as exc:
        try:
            payload = _canonical_patient_payload(json.loads(exc.read().decode("utf-8")))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
            payload = _patient_envelope("FAIL", "患者查询失败", False)
        raise PatientQueryRequestError("gateway patient query returned HTTP error", payload) from exc
    except (URLError, OSError) as exc:
        raise PatientQueryRequestError(
            "gateway patient query transport failed",
            _patient_envelope("FAIL", "患者查询失败", False),
        ) from exc
    try:
        payload = _canonical_patient_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise PatientQueryRequestError("gateway patient query returned invalid JSON") from exc
    if payload["success"] is not True:
        raise PatientQueryRequestError("gateway patient query was not successful", payload)
    return payload


class VerifiedPatientQuery:
    def __init__(
        self,
        cache: TriggerStatusCache,
        result_store: VerifiedResultStore,
        config_store: CaptureConfigurationStore,
        patient_store: VerifiedPatientResultStore,
        state_path: Path,
        endpoint: str,
        timeout_seconds: float = 3.0,
        retry_seconds: float = 5.0,
        sender: Callable[[str, str, float], Dict[str, Any]] = _post_gateway_patient_query,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("gateway patient endpoint must be loopback HTTP")
        self.cache = cache
        self.result_store = result_store
        self.config_store = config_store
        self.patient_store = patient_store
        self.state_path = state_path
        self.endpoint = endpoint
        self.timeout_seconds = max(0.5, timeout_seconds)
        self.retry_seconds = max(0.5, retry_seconds)
        self.sender = sender
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        persisted_state = self._load_state()
        self.last_success_key = str(persisted_state.get("last_success_key") or "")
        self.clear_seen_enabled_at = _number(persisted_state.get("clear_seen_enabled_at"))
        self.next_retry_at = 0.0
        self.status: Dict[str, Any] = {
            "enabled": False,
            "state": "disabled",
            "last_success_at": None,
            "last_error": None,
            "record_count": 0,
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            key = payload.get("last_success_key")
            if not isinstance(key, str) or len(key) != 64:
                payload["last_success_key"] = ""
            return payload
        except (OSError, json.JSONDecodeError):
            return {}

    def _persist_state(self, success_at: Any = None) -> None:
        payload: Dict[str, Any] = {
            "last_success_key": self.last_success_key,
            "clear_seen_enabled_at": self.clear_seen_enabled_at,
        }
        if success_at is not None:
            payload["last_success_at"] = success_at
        _write_text_atomic(
            self.state_path,
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
            mode=0o600,
        )

    def _set_status(
        self,
        state: str,
        error: Optional[str] = None,
        success_at: Any = None,
        record_count: int = 0,
    ) -> None:
        with self.lock:
            self.status = {
                "enabled": state != "disabled",
                "state": state,
                "last_success_at": success_at,
                "last_error": error,
                "record_count": max(0, int(record_count)),
            }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.status)

    def poll_once(self, now: Optional[float] = None) -> None:
        current_time = time.time() if now is None else now
        config = self.config_store.snapshot()
        if not config.get("patient_query_enabled"):
            self.next_retry_at = 0.0
            self._set_status("disabled")
            return

        enabled_at = float(config.get("patient_query_enabled_at") or 0.0)
        live_status = self.cache.snapshot()
        if abs(self.clear_seen_enabled_at - enabled_at) > 0.000001:
            clear_frame_seen = (
                live_status.get("active") is True
                and live_status.get("paper_detected") is False
                and live_status.get("capture_stage") == "absent"
            )
            if not clear_frame_seen:
                self._set_status("clear_required")
                return
            self.clear_seen_enabled_at = enabled_at
            self._persist_state()

        candidate = self.result_store.forwarding_candidate(live_status)
        if candidate is None:
            self._set_status("waiting")
            return
        if float(candidate["created_at"]) <= enabled_at:
            self._set_status("armed")
            return
        event_key = str(candidate["event_key"])
        capture_id = str(candidate["capture_id"])
        if event_key == self.last_success_key:
            metadata = self.patient_store.metadata(capture_id)
            self._set_status("sent", record_count=int(metadata.get("record_count") or 0))
            return
        if current_time < self.next_retry_at:
            return

        self._set_status("sending")
        self.patient_store.write(
            capture_id,
            event_key,
            _patient_envelope("PENDING", "患者信息查询中", False),
            created_at=current_time,
        )
        try:
            payload = self.sender(
                self.endpoint,
                str(candidate["identifier"]),
                self.timeout_seconds,
            )
        except PatientQueryRequestError as exc:
            self.patient_store.write(capture_id, event_key, exc.payload, created_at=current_time)
            self.next_retry_at = current_time + self.retry_seconds
            self._set_status("error", type(exc).__name__)
            return
        except Exception as exc:
            self.patient_store.write(
                capture_id,
                event_key,
                _patient_envelope("FAIL", "患者查询失败", False),
                created_at=current_time,
            )
            self.next_retry_at = current_time + self.retry_seconds
            self._set_status("error", type(exc).__name__)
            return

        canonical = self.patient_store.write(
            capture_id,
            event_key,
            payload,
            created_at=current_time,
        )
        self.last_success_key = event_key
        self.next_retry_at = 0.0
        self._persist_state(success_at=current_time)
        self._set_status(
            "sent",
            success_at=current_time,
            record_count=len(canonical["data"]),
        )
        print("verified identifier patient query completed", flush=True)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="patient-query", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.timeout_seconds + 2.0)

    def _run(self) -> None:
        while not self.stop_event.wait(0.25):
            try:
                self.poll_once()
            except Exception as exc:
                self.next_retry_at = time.time() + self.retry_seconds
                self._set_status("error", type(exc).__name__)


class VerifiedIdentifierForwarder:
    def __init__(
        self,
        cache: TriggerStatusCache,
        result_store: VerifiedResultStore,
        config_store: CaptureConfigurationStore,
        state_path: Path,
        endpoint: str,
        timeout_seconds: float = 3.0,
        retry_seconds: float = 5.0,
        sender: Callable[[str, str, float], None] = _post_gateway_scan,
    ) -> None:
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            raise ValueError("gateway scan endpoint must be loopback HTTP")
        self.cache = cache
        self.result_store = result_store
        self.config_store = config_store
        self.state_path = state_path
        self.endpoint = endpoint
        self.timeout_seconds = max(0.5, timeout_seconds)
        self.retry_seconds = max(0.5, retry_seconds)
        self.sender = sender
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        persisted_state = self._load_state()
        self.last_success_key = str(persisted_state.get("last_success_key") or "")
        self.clear_seen_enabled_at = _number(persisted_state.get("clear_seen_enabled_at"))
        self.next_retry_at = 0.0
        self.status: Dict[str, Any] = {
            "enabled": False,
            "state": "disabled",
            "last_success_at": None,
            "last_error": None,
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            key = payload.get("last_success_key")
            if not isinstance(key, str) or len(key) != 64:
                payload["last_success_key"] = ""
            return payload
        except (OSError, json.JSONDecodeError):
            return {}

    def _persist_state(self, success_at: Any = None) -> None:
        payload: Dict[str, Any] = {
            "last_success_key": self.last_success_key,
            "clear_seen_enabled_at": self.clear_seen_enabled_at,
        }
        if success_at is not None:
            payload["last_success_at"] = success_at
        _write_text_atomic(
            self.state_path,
            json.dumps(payload, ensure_ascii=True, indent=2) + "\n",
        )

    def _set_status(self, state: str, error: Optional[str] = None, success_at: Any = None) -> None:
        with self.lock:
            self.status = {
                "enabled": state != "disabled",
                "state": state,
                "last_success_at": success_at,
                "last_error": error,
            }

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return dict(self.status)

    def poll_once(self, now: Optional[float] = None) -> None:
        current_time = time.time() if now is None else now
        config = self.config_store.snapshot()
        if not config.get("auto_entry_enabled"):
            self.next_retry_at = 0.0
            self._set_status("disabled")
            return

        enabled_at = float(config.get("auto_entry_enabled_at") or 0.0)
        live_status = self.cache.snapshot()
        if abs(self.clear_seen_enabled_at - enabled_at) > 0.000001:
            clear_frame_seen = (
                live_status.get("active") is True
                and live_status.get("paper_detected") is False
                and live_status.get("capture_stage") == "absent"
            )
            if not clear_frame_seen:
                self._set_status("clear_required")
                return
            self.clear_seen_enabled_at = enabled_at
            self._persist_state()

        candidate = self.result_store.forwarding_candidate(live_status)
        if candidate is None:
            self._set_status("waiting")
            return
        if float(candidate["created_at"]) <= enabled_at:
            self._set_status("armed")
            return
        event_key = str(candidate["event_key"])
        if event_key == self.last_success_key:
            self._set_status("sent")
            return
        if current_time < self.next_retry_at:
            return

        self._set_status("sending")
        try:
            self.sender(self.endpoint, str(candidate["identifier"]), self.timeout_seconds)
        except Exception as exc:
            self.next_retry_at = current_time + self.retry_seconds
            self._set_status("error", type(exc).__name__)
            return

        self.last_success_key = event_key
        self.next_retry_at = 0.0
        self._persist_state(success_at=current_time)
        self._set_status("sent", success_at=current_time)
        print("verified identifier forwarded to gateway scan endpoint", flush=True)

    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._run, name="identifier-forwarder", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=self.timeout_seconds + 2.0)

    def _run(self) -> None:
        while not self.stop_event.wait(0.25):
            try:
                self.poll_once()
            except Exception as exc:
                self.next_retry_at = time.time() + self.retry_seconds
                self._set_status("error", type(exc).__name__)


class Handler(BaseHTTPRequestHandler):
    server_version = "RK3588CaptureMonitor/0.5"

    @property
    def cache(self) -> TriggerStatusCache:
        return self.server.cache  # type: ignore[attr-defined]

    @property
    def result_store(self) -> VerifiedResultStore:
        return self.server.result_store  # type: ignore[attr-defined]

    @property
    def config_store(self) -> CaptureConfigurationStore:
        return self.server.config_store  # type: ignore[attr-defined]

    @property
    def forwarder(self) -> Optional[VerifiedIdentifierForwarder]:
        return self.server.forwarder  # type: ignore[attr-defined]

    @property
    def patient_query(self) -> Optional[VerifiedPatientQuery]:
        return self.server.patient_query  # type: ignore[attr-defined]

    @property
    def patient_store(self) -> Optional[VerifiedPatientResultStore]:
        return self.server.patient_store  # type: ignore[attr-defined]

    @property
    def detector_probe(self) -> Optional[SystemdServiceProbe]:
        return self.server.detector_probe  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        if urlsplit(self.path).path not in (
            "/api/status",
            "/api/result",
            "/api/patient",
            "/favicon.ico",
        ):
            print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self._send(self.server.page, "text/html; charset=utf-8")  # type: ignore[attr-defined]
            return
        if path in ("/api/status", "/api/health"):
            payload = self.cache.snapshot()
            payload["rule"] = self.result_store.rule_summary()
            auto_entry = (
                self.forwarder.snapshot()
                if self.forwarder is not None
                else {"enabled": False, "state": "disabled", "last_error": None}
            )
            payload["auto_entry"] = auto_entry
            payload["forwarding"] = auto_entry
            payload["patient_query"] = (
                self.patient_query.snapshot()
                if self.patient_query is not None
                else {
                    "enabled": False,
                    "state": "disabled",
                    "last_error": None,
                    "record_count": 0,
                }
            )
            if self.detector_probe is not None:
                payload["detector_service"] = self.detector_probe.snapshot()
            self._json(payload)
            return
        if path == "/api/result":
            self._json(self.result_store.snapshot(self.cache.snapshot()))
            return
        if path == "/api/patient":
            if self.patient_store is None:
                self._json(
                    _patient_envelope("WAITING", "患者查询未启用", False),
                    status=HTTPStatus.NOT_FOUND,
                )
                return
            status, payload = self.patient_store.snapshot(self.cache.snapshot())
            self._json(payload, status=status)
            return
        if path == "/api/config":
            self._json(self.config_store.snapshot())
            return
        if path == "/favicon.ico":
            self._send(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path != "/api/config":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            raw_length = self.headers.get("Content-Length", "")
            length = int(raw_length)
            if length < 1:
                raise ValueError("request body is empty")
            if length > 8192:
                self._json({"error": "request_too_large"}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            config = self.config_store.update(payload)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            self._json(
                {"error": "configuration_saved_but_trigger_restart_failed:%s" % type(exc).__name__},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
            return
        self._json(
            {
                "saved": True,
                "config": config,
                "rule": self.result_store.rule_summary(),
            }
        )

    def _json(self, payload: Dict[str, Any], status: int = HTTPStatus.OK) -> None:
        self._send(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            status=status,
        )

    def _send(self, body: bytes, content_type: str, status: int = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        cache: TriggerStatusCache,
        result_store: VerifiedResultStore,
        config_store: CaptureConfigurationStore,
        forwarder: Optional[VerifiedIdentifierForwarder] = None,
        patient_query: Optional[VerifiedPatientQuery] = None,
        patient_store: Optional[VerifiedPatientResultStore] = None,
        detector_probe: Optional[SystemdServiceProbe] = None,
        page: bytes = PAGE,
    ) -> None:
        super().__init__(address, Handler)
        self.cache = cache
        self.result_store = result_store
        self.config_store = config_store
        self.forwarder = forwarder
        self.patient_query = patient_query
        self.patient_store = patient_store
        self.detector_probe = detector_probe
        self.page = page


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RK3588 report capture monitor")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8893)
    parser.add_argument(
        "--status-file",
        type=Path,
        default=Path("/run/rk3588-report-parser/camera-trigger.json"),
    )
    parser.add_argument("--stale-seconds", type=float, default=12.0)
    parser.add_argument("--offline-seconds", type=float, default=30.0)
    parser.add_argument("--display-rotation", type=int, choices=(0, 90), default=0)
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path("/run/rk3588-report-parser/verified-identifier.json"),
    )
    parser.add_argument(
        "--full-text-result-file",
        type=Path,
        default=Path("/run/rk3588-report-parser/verified-full-text.json"),
    )
    parser.add_argument(
        "--rules-file",
        type=Path,
        default=Path("/var/lib/rk3588-report-parser/active_identifier_rules.json"),
    )
    parser.add_argument(
        "--capture-settings-file",
        type=Path,
        default=Path("/var/lib/rk3588-report-parser/camera_capture_settings.json"),
    )
    parser.add_argument(
        "--trigger-environment-file",
        type=Path,
        default=Path("/var/lib/rk3588-report-parser/camera-capture.env"),
    )
    parser.add_argument("--trigger-service", default="rk3588-report-camera-trigger.service")
    parser.add_argument("--no-trigger-restart", action="store_true")
    parser.add_argument("--gateway-scan-endpoint", default="http://127.0.0.1:8080/scan")
    parser.add_argument(
        "--gateway-patient-endpoint",
        default="http://127.0.0.1:8080/patient/query",
    )
    parser.add_argument(
        "--forward-state-file",
        type=Path,
        default=Path("/var/lib/rk3588-report-parser/identifier_forward_state.json"),
    )
    parser.add_argument("--forward-timeout-seconds", type=float, default=3.0)
    parser.add_argument("--forward-retry-seconds", type=float, default=5.0)
    parser.add_argument(
        "--patient-result-file",
        type=Path,
        default=Path("/run/rk3588-report-parser/verified-patient.json"),
    )
    parser.add_argument(
        "--patient-metadata-file",
        type=Path,
        default=Path("/run/rk3588-report-parser/verified-patient.meta.json"),
    )
    parser.add_argument(
        "--patient-query-state-file",
        type=Path,
        default=Path("/var/lib/rk3588-report-parser/patient_query_state.json"),
    )
    parser.add_argument("--patient-query-timeout-seconds", type=float, default=12.0)
    parser.add_argument("--patient-query-retry-seconds", type=float, default=5.0)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cache = TriggerStatusCache(args.status_file, args.stale_seconds, args.offline_seconds)
    result_store = VerifiedResultStore(
        args.result_file,
        args.rules_file,
        args.full_text_result_file,
    )
    restart_trigger = None
    if not args.no_trigger_restart:
        def restart_trigger() -> None:
            completed = subprocess.run(
                ["systemctl", "restart", args.trigger_service],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=15,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError("trigger service restart failed")

    config_store = CaptureConfigurationStore(
        args.capture_settings_file,
        args.rules_file,
        args.trigger_environment_file,
        default_display_rotation=0,
        default_ocr_rotation=0,
        restart_trigger=restart_trigger,
    )
    patient_store = VerifiedPatientResultStore(
        args.patient_result_file,
        args.patient_metadata_file,
    )
    patient_query = VerifiedPatientQuery(
        cache,
        result_store,
        config_store,
        patient_store,
        args.patient_query_state_file,
        args.gateway_patient_endpoint,
        timeout_seconds=args.patient_query_timeout_seconds,
        retry_seconds=args.patient_query_retry_seconds,
    )
    forwarder = VerifiedIdentifierForwarder(
        cache,
        result_store,
        config_store,
        args.forward_state_file,
        args.gateway_scan_endpoint,
        timeout_seconds=args.forward_timeout_seconds,
        retry_seconds=args.forward_retry_seconds,
    )
    detector_probe = SystemdServiceProbe(args.trigger_service)
    page = PAGE.replace(
        b"__ROTATION_CLASS__",
        ("rotation-%d" % args.display_rotation).encode("ascii"),
    )
    server = Server(
        (args.host, args.port),
        cache,
        result_store,
        config_store,
        forwarder=forwarder,
        patient_query=patient_query,
        patient_store=patient_store,
        detector_probe=detector_probe,
        page=page,
    )
    print("RK3588 capture monitor listening on http://%s:%d" % (args.host, args.port), flush=True)
    patient_query.start()
    forwarder.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        patient_query.stop()
        forwarder.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
