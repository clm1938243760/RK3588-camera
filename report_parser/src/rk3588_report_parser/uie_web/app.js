const FIELD_LABELS = {
  patient_name: '患者姓名', patient_id: '患者ID', sex: '性别', age: '年龄',
  birthday: '出生日期', his_exam_no: '检查号/申请单号', report_no: '报告号',
  exam_item: '检查项目', name_phonetic: '姓名拼音', xing: '姓', ming: '名',
  nian: '出生年', yue: '出生月', ri: '出生日',
};
const FIELD_ORDER = [
  'patient_name', 'patient_id', 'his_exam_no', 'report_no', 'exam_item', 'sex',
  'age', 'birthday', 'name_phonetic', 'xing', 'ming', 'nian', 'yue', 'ri',
];
const STATUS_LABELS = {
  accepted: '识别成功', review_required: '需要复核', rejected: '已拒绝',
  error: '识别错误', empty: '暂无结果',
};
const METHOD_LABELS = {
  uie: 'UIE',
  uie_typed_refinement: 'UIE收紧',
  label_inline_fallback: '同框标签补全',
  label_neighbor_fallback: '邻近标签补全',
  typed_unique_fallback: '唯一类型候选',
};
const REJECTION_LABELS = {
  invalid_sex_value: '候选不是男或女',
  invalid_age_value: '年龄格式或范围不合法',
  invalid_birthday_value: '出生日期不合法',
  invalid_identifier_value: '号码格式不合法',
  ambiguous_identifier_value: '候选中包含多个号码',
  invalid_exam_item: '候选不是检查项目文字',
  invalid_patient_name: '姓名格式不合法',
  invalid_name_phonetic: '姓名拼音格式不合法',
  probability_below_threshold: '低于配置置信度',
  text_not_from_ocr: '候选不属于OCR原文',
};

const fileInput = document.getElementById('fileInput');
const parseButton = document.getElementById('parseButton');
const latestButton = document.getElementById('latestButton');
const exportPatientButton = document.getElementById('exportPatientButton');
const exportEvidenceButton = document.getElementById('exportEvidenceButton');
const previewImage = document.getElementById('previewImage');
const imageWrap = document.getElementById('imageWrap');
const imageStage = document.getElementById('imageStage');
const overlay = document.getElementById('overlay');
const tokenInput = document.getElementById('tokenInput');
const schemaDialog = document.getElementById('schemaDialog');
const remoteListener = !['127.0.0.1', 'localhost', '::1'].includes(window.location.hostname);
let selectedFile = null;
let previewUrl = null;
let currentResult = null;
let currentSchema = null;

tokenInput.value = window.sessionStorage.getItem('rk3588UieAccessToken') || '';

function headers() {
  const token = tokenInput.value.trim();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

function tokenMissing() {
  return remoteListener && !tokenInput.value.trim();
}

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' }[ch]));
}

function setService(ok, text) {
  const state = document.getElementById('serviceState');
  state.textContent = ok ? '在线' : '离线';
  state.className = `service-state ${ok ? 'online' : 'offline'}`;
  document.getElementById('runtimeText').textContent = text;
}

function responseError(response, payload, fallback) {
  if (response.status === 401) return '访问令牌错误或未填写';
  return payload?.error || fallback;
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
    setService(true, `${runtime.model} · PP-OCR · ${runtime.field_count}个字段`);
  } catch (_) {
    setService(false, '本地服务未连接');
  }
}

fileInput.addEventListener('change', () => {
  selectedFile = fileInput.files[0] || null;
  document.getElementById('fileName').textContent = selectedFile ? selectedFile.name : '未选择文件';
  parseButton.disabled = !selectedFile;
  document.getElementById('saveAndParseSchemaButton').disabled = !selectedFile;
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  if (!selectedFile) return;
  previewUrl = URL.createObjectURL(selectedFile);
  previewImage.src = previewUrl;
  imageWrap.hidden = false;
  imageStage.classList.remove('empty');
  document.getElementById('emptyText').hidden = true;
  overlay.replaceChildren();
});

previewImage.addEventListener('load', () => {
  overlay.setAttribute('viewBox', `0 0 ${previewImage.naturalWidth || 1000} ${previewImage.naturalHeight || 1000}`);
});

parseButton.addEventListener('click', async () => {
  if (!selectedFile) return;
  if (tokenMissing()) {
    document.getElementById('resultStatus').textContent = '请填写访问令牌';
    tokenInput.focus();
    return;
  }
  setBusy(true);
  const form = new FormData();
  form.append('image', selectedFile, selectedFile.name);
  try {
    const response = await fetch('/api/v1/parse', { method: 'POST', headers: headers(), body: form });
    const payload = await response.json();
    if (!response.ok) throw new Error(responseError(response, payload, '识别失败'));
    renderResult(payload);
  } catch (error) {
    renderError(error.message);
  } finally {
    setBusy(false);
  }
});

latestButton.addEventListener('click', async () => {
  try {
    const response = await fetch('/api/v1/result', { headers: headers() });
    const payload = await response.json();
    if (!response.ok) throw new Error(responseError(response, payload, '读取失败'));
    if (payload.status === 'empty') throw new Error('暂无摄像头结构化结果');
    renderResult(payload);
  } catch (error) {
    renderError(error.message);
  }
});

function setBusy(busy) {
  parseButton.disabled = busy || !selectedFile;
  parseButton.textContent = busy ? '识别中' : '开始识别';
  if (busy) document.getElementById('resultStatus').textContent = 'OCR与字段分类中';
}

function renderError(message) {
  currentResult = null;
  document.getElementById('resultStatus').textContent = '错误';
  document.getElementById('fieldList').innerHTML = `<p class="muted error-text">${escapeHtml(message)}</p>`;
  exportPatientButton.disabled = true;
  exportEvidenceButton.disabled = true;
  document.getElementById('reviewSection').hidden = true;
}

function addRect(box, className, fieldKey) {
  if (!Array.isArray(box) || box.length !== 4) return;
  const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  rect.setAttribute('x', box[0]);
  rect.setAttribute('y', box[1]);
  rect.setAttribute('width', Math.max(1, box[2] - box[0]));
  rect.setAttribute('height', Math.max(1, box[3] - box[1]));
  rect.setAttribute('class', className);
  rect.dataset.field = fieldKey;
  overlay.appendChild(rect);
}

function drawEvidence(selectedKey = '') {
  overlay.replaceChildren();
  if (!currentResult?.fields) return;
  Object.entries(currentResult.fields).forEach(([key, evidence]) => {
    const selected = !selectedKey || key === selectedKey;
    const conflict = (currentResult.conflict_fields || []).includes(key);
    const className = `${conflict ? 'conflict' : 'evidence'}${selected ? '' : ' dimmed'}`;
    (evidence.boxes || []).forEach(box => addRect(box, className, key));
    (evidence.alternatives || []).forEach(item => (item.boxes || []).forEach(box => addRect(box, 'alternative', key)));
  });
}

function renderResult(result) {
  currentResult = result;
  const patient = result.patient_response?.data?.[0] || {};
  document.getElementById('resultStatus').textContent = STATUS_LABELS[result.status] || result.status;
  document.getElementById('resultStatus').className = `status-${result.status || 'empty'}`;
  document.getElementById('durationText').textContent = formatDuration(result.timings?.total_ms);
  document.getElementById('patientName').textContent = patient.patient_name || '--';
  document.getElementById('patientIdentity').textContent = patient.patient_id || patient.his_exam_no || patient.report_no || '--';
  renderFields(result, patient);
  renderReview(result);
  document.getElementById('patientJson').textContent = JSON.stringify(result.patient_response, null, 2);
  document.getElementById('ocrText').textContent = result.document?.full_text || '暂无内容';
  document.getElementById('blockCount').textContent = `${result.document?.blocks?.length || 0} 个文本块`;
  renderMetrics(result);
  drawEvidence();
  exportPatientButton.disabled = false;
  exportEvidenceButton.disabled = false;
}

function renderFields(result, patient) {
  const target = document.getElementById('fieldList');
  target.innerHTML = FIELD_ORDER.map(key => {
    const value = patient[key];
    const evidence = result.fields?.[key];
    const conflict = (result.conflict_fields || []).includes(key);
    const needsReview = (result.review_fields || []).includes(key);
    const derived = value && !evidence;
    const method = METHOD_LABELS[evidence?.resolution_method || 'uie'] || 'OCR证据';
    const confidence = evidence
      ? `${method} · ${Math.round(evidence.probability * 100)}% / OCR ${Math.round(evidence.ocr_confidence * 100)}%`
      : derived ? '派生' : '--';
    const alternatives = (evidence?.alternatives || [])
      .map((item, index) => ({ ...item, candidateIndex: index + 1 }))
      .filter(item => item?.value && item.value !== evidence.value).slice(0, 3);
    const alternativeButtons = alternatives.map(item => `<button class="alternative-choice" type="button" data-field="${key}" data-index="${item.candidateIndex}">
      <span>${escapeHtml(item.value)}</span><small>${escapeHtml(item.matched_prompt || '备选')} · ${Math.round(item.probability * 100)}%</small>
    </button>`).join('');
    const approvalButton = needsReview && evidence ? `<button class="alternative-choice confirm-choice" type="button" data-field="${key}" data-index="0">
      <span>确认当前值</span><small>保留OCR证据</small>
    </button>` : '';
    const correction = result.manual_corrections?.[key] ? '<b>已人工复核</b>' : '';
    return `<div class="field-row${conflict || needsReview ? ' conflict' : ''}${value ? '' : ' empty-value'}">
      <button class="field-main" type="button" data-field="${key}" ${evidence ? '' : 'disabled'}>
        <span>${escapeHtml(FIELD_LABELS[key] || key)}</span>
        <small title="${escapeHtml(evidence?.matched_prompt || '')}">${escapeHtml(confidence)}</small>
        <strong>${escapeHtml(value ?? '--')}</strong>
        ${correction}
      </button>
      ${approvalButton || alternativeButtons ? `<div class="alternative-options">${approvalButton}${alternativeButtons}</div>` : ''}
    </div>`;
  }).join('');
  target.querySelectorAll('.field-main[data-field]').forEach(button => {
    button.addEventListener('click', () => drawEvidence(button.dataset.field));
  });
  target.querySelectorAll('.alternative-choice').forEach(button => {
    button.addEventListener('click', () => selectCandidate(button.dataset.field, Number(button.dataset.index)));
  });
}

function renderReview(result) {
  const section = document.getElementById('reviewSection');
  const target = document.getElementById('reviewList');
  const rows = [];
  (result.review_fields || []).forEach(key => rows.push(`${FIELD_LABELS[key] || key}：自动补全结果需要人工确认`));
  (result.conflict_fields || []).forEach(key => rows.push(`${FIELD_LABELS[key] || key}：存在多个不同候选`));
  (result.missing_fields || []).forEach(key => rows.push(`${FIELD_LABELS[key] || key}：必填字段缺失`));
  (result.rejected_predictions || []).forEach(item => {
    if (item.reason === 'probability_below_threshold') return;
    const field = FIELD_LABELS[item.field_key] || item.field_key;
    const reason = REJECTION_LABELS[item.reason] || item.reason;
    rows.push(`${field}：${reason}，候选未写入患者JSON`);
  });
  const uniqueRows = [...new Set(rows)];
  section.hidden = uniqueRows.length === 0;
  target.innerHTML = uniqueRows.map(value => `<li>${escapeHtml(value)}</li>`).join('');
}

async function selectCandidate(fieldKey, candidateIndex) {
  document.getElementById('resultStatus').textContent = '保存复核结果';
  try {
    const response = await fetch('/api/v1/result/select', {
      method: 'POST', headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ field_key: fieldKey, candidate_index: candidateIndex }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(responseError(response, payload, '复核结果保存失败'));
    renderResult(payload);
    drawEvidence(fieldKey);
  } catch (error) {
    renderError(error.message);
  }
}

function renderMetrics(result) {
  const quality = result.quality || {};
  const metrics = [
    ['OCR文本块', result.document?.blocks?.length || result.source?.block_count || 0],
    ['OCR平均置信度', quality.ocr_mean_score == null ? '--' : `${Math.round(quality.ocr_mean_score * 100)}%`],
    ['OCR耗时', formatDuration(result.timings?.ocr_ms)],
    ['字段分类', formatDuration(result.timings?.uie_ms)],
    ['模型', result.model || '--'],
    ['捕获编号', result.capture_id ? result.capture_id.slice(0, 16) : '--'],
  ];
  document.getElementById('metricsList').innerHTML = metrics.map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${escapeHtml(value)}</dd>`).join('');
}

function formatDuration(value) {
  const milliseconds = Number(value);
  if (!Number.isFinite(milliseconds)) return '--';
  return milliseconds >= 1000 ? `${(milliseconds / 1000).toFixed(2)} s` : `${milliseconds.toFixed(0)} ms`;
}

function downloadJson(filename, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

exportPatientButton.addEventListener('click', () => {
  if (currentResult) downloadJson('patient.json', currentResult.patient_response);
});
exportEvidenceButton.addEventListener('click', () => {
  if (currentResult) downloadJson('patient-evidence.json', currentResult);
});

document.getElementById('schemaButton').addEventListener('click', async () => {
  const state = document.getElementById('schemaState');
  state.textContent = '';
  try {
    const response = await fetch('/api/v1/schema', { headers: headers() });
    const payload = await response.json();
    if (!response.ok) throw new Error(responseError(response, payload, '配置读取失败'));
    currentSchema = payload;
    document.getElementById('schemaModel').textContent = payload.model;
    renderSchemaRows(payload.fields || []);
    schemaDialog.showModal();
  } catch (error) {
    renderError(error.message);
  }
});

function renderSchemaRows(fields) {
  document.getElementById('schemaRows').innerHTML = fields.map((field, index) => `<tr data-index="${index}" data-key="${escapeHtml(field.field_key)}">
    <td><strong>${escapeHtml(FIELD_LABELS[field.field_key] || field.field_key)}</strong><small>${escapeHtml(field.field_key)}</small></td>
    <td><input class="schema-prompt" value="${escapeHtml(field.prompt)}" maxlength="80"></td>
    <td><textarea class="schema-aliases" rows="2" maxlength="400">${escapeHtml((field.prompt_aliases || []).join('\n'))}</textarea></td>
    <td><input class="schema-probability" type="number" min="0.05" max="1" step="0.05" value="${Number(field.minimum_probability ?? 0.5).toFixed(2)}"></td>
    <td><input class="schema-required" type="checkbox" ${field.required ? 'checked' : ''}></td>
  </tr>`).join('');
}

document.getElementById('closeSchemaButton').addEventListener('click', () => schemaDialog.close());
document.getElementById('cancelSchemaButton').addEventListener('click', () => schemaDialog.close());
document.getElementById('saveSchemaButton').addEventListener('click', event => saveSchema(event, false));
document.getElementById('saveAndParseSchemaButton').addEventListener('click', event => saveSchema(event, true));

async function saveSchema(event, reparse) {
  event.preventDefault();
  const state = document.getElementById('schemaState');
  try {
    const fields = Array.from(document.querySelectorAll('#schemaRows tr')).map(row => ({
      field_key: row.dataset.key,
      prompt: row.querySelector('.schema-prompt').value.trim(),
      prompt_aliases: row.querySelector('.schema-aliases').value.split(/[,，;；\n]/).map(value => value.trim()).filter(Boolean),
      minimum_probability: Number(row.querySelector('.schema-probability').value),
      required: row.querySelector('.schema-required').checked,
    }));
    if (fields.some(field => !field.prompt)) throw new Error('提示词不能为空');
    const response = await fetch('/api/v1/schema', {
      method: 'PUT', headers: { ...headers(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ schema_version: 1, fields }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(responseError(response, payload, '配置保存失败'));
    currentSchema = payload;
    state.textContent = '已保存';
    await loadRuntime();
    window.setTimeout(() => {
      schemaDialog.close();
      if (reparse && selectedFile) parseButton.click();
    }, 250);
  } catch (error) {
    state.textContent = error.message;
    state.className = 'error-text';
  }
}

tokenInput.addEventListener('input', () => {
  window.sessionStorage.setItem('rk3588UieAccessToken', tokenInput.value.trim());
});
tokenInput.addEventListener('change', loadRuntime);
loadRuntime();
