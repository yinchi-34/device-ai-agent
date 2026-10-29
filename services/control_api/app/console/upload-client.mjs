export async function requestJSON(path, options = {}, request = fetch) {
  const response = await request(path, {
    ...options,
    signal: AbortSignal.timeout(120000),
    headers: {Accept: 'application/json', ...options.headers},
  });
  let body;
  try { body = await response.json(); } catch { /* Use the status if not JSON. */ }
  if (!response.ok) {
    const detail = body?.message || (typeof body?.detail === 'string' ? body.detail : '');
    throw new Error(detail || `请求失败（${response.status}），请检查服务、数据库及任务队列。`);
  }
  if (!body) throw new Error('服务未返回有效结果，请刷新状态确认，避免重复提交。');
  return body;
}

export function uploadDocument(file, modelId, request = fetch) {
  const body = new FormData();
  body.append('file', file);
  if (modelId) body.append('device_model_id', modelId);
  return requestJSON('/api/v1/documents', {method: 'POST', body}, request);
}
