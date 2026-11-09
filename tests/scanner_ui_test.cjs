// Exercise the actual scanner UI script with a simulated camera and gate API.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('templates/scanner.html', 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
const flush = async () => { for (let i = 0; i < 15; i++) await Promise.resolve(); };
function setup(failCamera = false) {
  const nodes = {}, handlers = {}, calls = [];
  let reader, attempts = 0;
  const node = id => nodes[id] ||= {
    value: '', disabled: false, style: {}, hidden: false, textContent: '', events: {},
    classList: { flags: new Set(), contains(v) { return this.flags.has(v); }, add(v) { this.flags.add(v); }, remove(v) { this.flags.delete(v); } },
    addEventListener(type, fn) { this.events[type] = fn; }, focus() {}
  };
  class Reader {
    constructor() { reader = this; }
    async start(camera, config, callback) {
      attempts++;
      if (failCamera && attempts === 1) { const e = new Error('denied'); e.name = 'NotAllowedError'; throw e; }
      this.callback = callback;
    }
    async stop() { this.stopped = true; }
  }
  const document = { hidden: false, getElementById: node, addEventListener: (type, fn) => { handlers[type] = fn; } };
  const context = {
    document, navigator: { mediaDevices: { getUserMedia() {} } },
    Html5Qrcode: Reader, Html5QrcodeSupportedFormats: { QR_CODE: 0 },
    location: {}, setTimeout() { return 1; }, clearTimeout() {}, Date,
    async fetch(path, options) {
      calls.push({path, body: JSON.parse(options.body)});
      return { status: 200, async json() { return path.endsWith('lookup')
        ? {status: 'ok', guest: 'Guest', ticket_type: 'VIP'}
        : {status: 'granted', guest: 'Guest', inside: 1, total: 1}; } };
    }
  };
  context.window = { Html5Qrcode: Reader, addEventListener() {} };
  vm.runInNewContext(script, context);
  return {node, calls, document, handlers, reader: () => reader, attempts: () => attempts};
}
(async () => {
  const ui = setup();
  await flush();
  ui.reader().callback('https://example.com/gate/ABC-123');
  await flush();
  assert.equal(ui.calls[0].body.code, 'ABC-123');
  assert.equal(ui.node('sheet').classList.contains('open'), true);
  ui.reader().callback('OTHER');
  await flush();
  assert.equal(ui.calls.length, 1, 'camera must pause while the PIN is requested');
  ui.node('pin').value = '1234';
  const pending = ui.node('confirm').events.click();
  ui.node('confirm').events.click();
  await pending;
  await flush();
  assert.equal(ui.calls.filter(c => c.path.endsWith('verify')).length, 1, 'ignore repeated confirmation');
  assert.equal(ui.node('inside').textContent, 1);
  await ui.handlers.visibilitychange.call(null, ui.document.hidden = true);
  assert.equal(ui.reader().stopped, true);

  const denied = setup(true);
  await flush();
  assert.match(denied.node('camText').textContent, /permission was blocked/);
  denied.node('manual').value = 'MANUAL-123';
  denied.node('manualForm').events.submit({preventDefault() {}});
  await flush();
  assert.equal(denied.calls[0].body.code, 'MANUAL-123', 'manual entry works without camera permission');
  denied.node('cancel').events.click();
  await flush();
  assert.equal(denied.attempts(), 2, 'retry camera after cancellation');
  console.log('Scanner UI checks passed: camera callbacks, PIN, duplicate submission, visibility, denial and manual entry.');
})().catch(e => { console.error(e); process.exitCode = 1; });
