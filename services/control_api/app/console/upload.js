import {requestJSON, uploadDocument} from './upload-client.mjs';

const $ = id => document.getElementById(id);
let documentId = null;
let runId = null;
let busy = false;
const statusNames = {queued:'排队中', validating:'校验中', parsing:'解析中', extracting:'提取中', validating_output:'校验提取结果', awaiting_review:'等待人工审核', importing:'导入中', completed:'处理完成', failed:'提取失败', canceled:'已取消', validated:'文件校验通过'};
function message(text, success = false) {
  $('notice').hidden = !text;
  $('notice').classList.toggle('success', success);
  $('notice').textContent = text;
}
function lock(value) {
  busy = value;
  document.querySelectorAll('button, input, select').forEach(el => {el.disabled = value;});
}
function saveLocation() {
  const params = new URLSearchParams({document: String(documentId)});
  if (runId) params.set('run', String(runId));
  history.replaceState(null, '', `/upload?${params}`);
}
function showDocument(doc) {
  documentId = doc.id; runId = null;
  $('uploaded').hidden = false;
  $('document-summary').textContent = `${doc.original_file_name} · ${(doc.file_size_bytes / 1024).toFixed(1)} KB · 文档 ID ${doc.id}`;
  $('document-validation').textContent = statusNames[doc.validation_status] || doc.validation_status;
  $('review-link').href = `/admin/documents/${doc.id}`;
  $('extract').hidden = false;
  $('run-result').textContent = '';
  $('refresh-run').hidden = true;
  saveLocation();
}
function showRun(run) {
  runId = run.id;
  $('extract').hidden = true;
  $('refresh-run').hidden = false;
  $('run-result').textContent = `提取任务 #${run.id} · ${statusNames[run.status] || run.status}${run.error_code ? `\n错误：${run.error_code}\n${JSON.stringify(run.error_detail)}` : ''}`;
  saveLocation();
}
$('document-file').addEventListener('change', () => {
  const file = $('document-file').files[0];
  $('file-info').textContent = file ? `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB` : '请选择一个 PDF 或 DOCX 文件';
});
$('upload-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  const file = $('document-file').files[0];
  if (!file || !/\.(pdf|docx)$/i.test(file.name)) {message('请选择 PDF 或 DOCX 文件。'); return;}
  lock(true); message('正在上传并校验文档，请稍候…');
  try {
    showDocument(await uploadDocument(file, $('device-model').value));
    $('upload-form').reset(); $('file-info').textContent = '请选择一个 PDF 或 DOCX 文件';
    message('文档上传成功。可启动 AI 提取，或前往文档审核查看。', true);
  } catch(error) {message(`上传未确认成功：${error.message} 如请求中断，请先到“查看全部文档”确认记录，再决定是否重新上传。`);}
  finally {lock(false);}
});
$('extract').addEventListener('click', async () => {
  if (busy || !documentId) return;
  lock(true); message('正在创建提取任务…');
  try {
    showRun(await requestJSON(`/api/v1/documents/${documentId}/extraction-runs`, {method:'POST'}));
    message('提取任务已提交，请点击“刷新处理状态”查看进度。', true);
  } catch(error) {
    $('extract').hidden = true;
    message(`任务提交未确认：${error.message} 文档已经上传，请前往“查看文档与审核”确认任务，避免重复提交。`);
  } finally {lock(false);}
});
$('refresh-run').addEventListener('click', async () => {
  if (busy || !runId) return;
  lock(true);
  try {showRun(await requestJSON(`/api/v1/extraction-runs/${runId}`)); message();}
  catch(error) {message(error.message);}
  finally {lock(false);}
});
$('lookup-form').addEventListener('submit', async event => {
  event.preventDefault(); if (busy) return;
  lock(true);
  try {showDocument(await requestJSON(`/api/v1/documents/${Number($('document-id').value)}`)); message();}
  catch(error) {message(error.message);}
  finally {lock(false);}
});
async function initialize() {
  try {
    const models = await requestJSON('/api/v1/device-models');
    models.forEach(model => $('device-model').add(new Option(`${model.model_name} · ${model.model_code}`, String(model.id))));
  } catch { $('model-status').textContent = '设备型号暂时无法加载；仍可选择不关联型号上传。'; }
  const params = new URLSearchParams(location.search);
  if (/^[1-9]\d*$/.test(params.get('document') || '')) {
    lock(true);
    try {
      showDocument(await requestJSON(`/api/v1/documents/${params.get('document')}`));
      if (/^[1-9]\d*$/.test(params.get('run') || '')) {
        const run = await requestJSON(`/api/v1/extraction-runs/${params.get('run')}`);
        if (run.source_document_id !== documentId) throw new Error('提取任务与文档不匹配。');
        showRun(run);
      }
    } catch(error) {message(error.message);}
    finally {lock(false);}
  }
}
initialize();
