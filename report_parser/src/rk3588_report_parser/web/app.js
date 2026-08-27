const fileInput = document.getElementById('fileInput');
const parseButton = document.getElementById('parseButton');
const exportButton = document.getElementById('exportButton');
const previewImage = document.getElementById('previewImage');
const imageWrap = document.getElementById('imageWrap');
const imageStage = document.getElementById('imageStage');
const overlay = document.getElementById('overlay');
const tokenInput = document.getElementById('tokenInput');
const ruleButton = document.getElementById('ruleButton');
const ruleDialog = document.getElementById('ruleDialog');
const targetLength = document.getElementById('targetLength');
const remoteListener = !['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
let selectedFile = null;
let currentResult = null;
let previewUrl = null;
let currentRules = { enabled: false, profile: 'unconfigured', fields: [] };

tokenInput.value = window.sessionStorage.getItem('rk3588ReportAccessToken') || '';

function headers() {
  const token = tokenInput.value.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenMissing() {
  return remoteListener && !tokenInput.value.trim();
}

function responseError(response, payload, fallback) {
  if (response.status === 401) return '访问令牌错误或未填写';
  return payload?.error || fallback;
}

function setService(ok, text) {
  const state = document.getElementById('serviceState');
  state.textContent = ok ? '在线' : '离线';
  state.className = `service-state ${ok ? 'online' : 'offline'}`;
  document.getElementById('runtimeText').textContent = text;
}

async function loadRuntime() {
  try {
    if (tokenMissing()) {
      const health = await fetch('/api/v1/health');
      if (!health.ok) throw new Error('health unavailable');
      setService(true, '请输入访问令牌');
      return;
    }
    const response = await fetch('/api/v1/runtime', { headers: headers() });
    if (!response.ok) throw new Error('runtime unavailable');
    const runtime = await response.json();
    const ruleMode = runtime.identifier_rules?.enabled;
    const engineText = ruleMode ? 'OCR字数模式' : runtime.model;
    setService(true, `${runtime.profile} · ${engineText}`);
  } catch (_) {
    setService(false, '本地服务未连接');
  }
}

async function loadRules() {
  const response = await fetch('/api/v1/rules', { headers: headers() });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || '规则读取失败');
  currentRules = payload;
  document.getElementById('ruleProfile').value = currentRules.profile || 'unconfigured';
  document.getElementById('rulesEnabled').checked = Boolean(currentRules.enabled);
  const selectedRule = currentRules.fields.find(rule => rule.type === 'selected_identifier') || currentRules.fields[0];
  targetLength.value = selectedRule?.lengths?.[0] || 8;
}

function collectRules() {
  const length = Number(targetLength.value);
  if (!Number.isInteger(length) || length < 4 || length > 64) throw new Error('目标字符数必须在4到64之间');
  return {
    enabled: document.getElementById('rulesEnabled').checked,
    profile: document.getElementById('ruleProfile').value.trim(),
    fields: [{
      type: 'selected_identifier', lengths: [length], charset: 'alphanumeric', prefixes: [],
      allow_unlabeled: true, priority: 1000, enabled: true,
    }],
  };
}

ruleButton.addEventListener('click', async () => {
  const state = document.getElementById('ruleSaveState');
  state.textContent = '';
  state.className = 'rule-save-state';
  if (tokenMissing()) {
    state.textContent = '请先填写访问令牌';
    state.className = 'rule-save-state error';
    tokenInput.focus();
    return;
  }
  try {
    await loadRules();
    ruleDialog.showModal();
  } catch (error) {
    state.textContent = error.message;
    state.className = 'rule-save-state error';
  }
});

document.getElementById('closeRuleButton').addEventListener('click', () => ruleDialog.close());
document.getElementById('cancelRuleButton').addEventListener('click', () => ruleDialog.close());
document.getElementById('saveRuleButton').addEventListener('click', async () => {
  const state = document.getElementById('ruleSaveState');
  try {
    const payload = collectRules();
    const response = await fetch('/api/v1/rules', {
      method: 'PUT', headers: { ...headers(), 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(responseError(response, result, '规则保存失败'));
    currentRules = result;
    state.textContent = '已保存';
    state.className = 'rule-save-state';
    await loadRuntime();
    window.setTimeout(() => ruleDialog.close(), 350);
  } catch (error) {
    state.textContent = error.message;
    state.className = 'rule-save-state error';
  }
});

fileInput.addEventListener('change', () => {
  selectedFile = fileInput.files[0] || null;
  document.getElementById('fileName').textContent = selectedFile ? selectedFile.name : '未选择文件';
  parseButton.disabled = !selectedFile;
  currentResult = null;
  exportButton.disabled = true;
  overlay.replaceChildren();
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  if (!selectedFile) return;
  previewUrl = URL.createObjectURL(selectedFile);
  previewImage.src = previewUrl;
  imageWrap.hidden = false;
  imageStage.classList.remove('empty');
  document.getElementById('emptyText').hidden = true;
});

parseButton.addEventListener('click', async () => {
  if (!selectedFile) return;
  if (tokenMissing()) {
    document.getElementById('resultStatus').textContent = '请填写访问令牌';
    tokenInput.focus();
    return;
  }
  parseButton.disabled = true;
  parseButton.textContent = '识别中';
  document.getElementById('resultStatus').textContent = '处理中';
  const form = new FormData();
  form.append('image', selectedFile, selectedFile.name);
  try {
    const response = await fetch('/api/v1/parse', { method: 'POST', headers: headers(), body: form });
    const result = await response.json();
    if (!response.ok) throw new Error(responseError(response, result, '识别失败'));
    currentResult = result;
    renderResult(result);
    exportButton.disabled = false;
  } catch (error) {
    currentResult = null;
    document.getElementById('resultStatus').textContent = '错误';
    document.getElementById('identifierList').innerHTML = `<p class="muted">${escapeHtml(error.message)}</p>`;
  } finally {
    parseButton.disabled = false;
    parseButton.textContent = '开始识别';
  }
});

exportButton.addEventListener('click', () => {
  if (!currentResult) return;
  const blob = new Blob([JSON.stringify(currentResult, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = 'identifier-result.json';
  link.click();
  URL.revokeObjectURL(url);
});

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
}

function renderItems(target, items, review) {
  if (!items.length) {
    target.innerHTML = '<p class="muted">暂无项目</p>';
    return;
  }
  const sourceLabels = { configured_rule: 'OCR规则确认', ambiguous_rule_match: '多规则冲突', unmatched_rule: '未匹配规则', weak_rule_match: '仅格式匹配·待复核', model: '模型' };
  target.innerHTML = items.map(item => `
    <article class="identifier-item ${review ? 'review' : ''}">
      <div class="item-head"><span>${escapeHtml(item.type_label)}</span><span>${Math.round(item.ocr_confidence * 100)}%</span></div>
      <strong>${escapeHtml(item.value || '未通过校验')}</strong>
      <small>${escapeHtml([item.raw_label, item.evidence.relation, sourceLabels[item.decision_source] || item.decision_source].filter(Boolean).join(' · '))}</small>
    </article>`).join('');
}

function addRect(box, alternative) {
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('x', box[0]);
  rect.setAttribute('y', box[1]);
  rect.setAttribute('width', Math.max(1, box[2] - box[0]));
  rect.setAttribute('height', Math.max(1, box[3] - box[1]));
  if (alternative) rect.setAttribute('class', 'alternative');
  overlay.appendChild(rect);
}

function renderResult(result) {
  document.getElementById('resultStatus').textContent = ({ accepted: '已接受', review_required: '需要复核', rejected: '已拒绝' })[result.status] || result.status;
  document.getElementById('durationText').textContent = `${(result.timings.total_ms / 1000).toFixed(2)} s`;
  document.getElementById('primaryValue').textContent = result.primary_identifier?.value || '--';
  document.getElementById('primaryType').textContent = result.primary_identifier?.type_label || '--';
  renderItems(document.getElementById('identifierList'), result.identifiers || [], false);
  renderItems(document.getElementById('alternativeList'), result.alternatives || [], true);
  overlay.replaceChildren();
  (result.identifiers || []).forEach(item => {
    addRect(item.evidence.label_box, false);
    item.evidence.value_boxes.forEach(box => addRect(box, false));
  });
  (result.alternatives || []).forEach(item => {
    addRect(item.evidence.label_box, true);
    item.evidence.value_boxes.forEach(box => addRect(box, true));
  });
  const metrics = [
    ['OCR文本块', result.ocr_summary.item_count],
    ['OCR平均置信度', `${Math.round(result.ocr_summary.mean_score * 100)}%`],
    ['OCR耗时', `${result.timings.ocr_ms.toFixed(0)} ms`],
    ['分类耗时', `${result.timings.classification_ms.toFixed(0)} ms`],
    ['复核耗时', `${result.timings.verification_ms.toFixed(0)} ms`],
    ['运行配置', result.engine.profile],
  ];
  document.getElementById('metricsList').innerHTML = metrics.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join('');
}

tokenInput.addEventListener('input', () => {
  window.sessionStorage.setItem('rk3588ReportAccessToken', tokenInput.value.trim());
});
tokenInput.addEventListener('change', loadRuntime);
loadRuntime();
