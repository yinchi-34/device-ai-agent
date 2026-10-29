import {test} from 'node:test';
import assert from 'node:assert/strict';
import {uploadDocument} from '../../app/console/upload-client.mjs';

test('uploads multipart with optional model and lets the browser set its boundary', async () => {
  const result = await uploadDocument(new Blob(['pdf']), '42', async (path, options) => {
    assert.equal(path, '/api/v1/documents');
    assert.equal(options.method, 'POST');
    assert.ok(options.body instanceof FormData);
    assert.equal(options.body.get('device_model_id'), '42');
    assert.equal(await options.body.get('file').text(), 'pdf');
    assert.equal(options.headers['Content-Type'], undefined);
    return Response.json({id: 12}, {status: 201});
  });
  assert.equal(result.id, 12);
});

test('omits an unselected model and surfaces server validation failures', async () => {
  await assert.rejects(uploadDocument(new Blob(['bad']), '', async (path, options) => {
    assert.equal(options.body.has('device_model_id'), false);
    return Response.json({message: '文件内容与类型不匹配'}, {status: 422});
  }), /文件内容与类型不匹配/);
});

test('explains non-JSON errors without exposing raw server output', async () => {
  await assert.rejects(uploadDocument(new Blob(['pdf']), '', async () =>
    new Response('<html>proxy error</html>', {status: 502})
  ), /502/);
});
