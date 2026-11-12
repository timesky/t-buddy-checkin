#!/usr/bin/env node
// 极简 CDP 客户端：连一个 target 的 WebSocket，执行一段 JS，输出 JSON。
// 用法: node cdp_client.mjs <wsUrl> <expression> [timeoutMs]
// 输出: {"ok":true,"value":<返回值>} | {"ok":false,"error":"..."}
//
// 用途：agent-browser 的 eval 不支持 iframe（跨源），成长中心是 OOPIF，
// 必须直连 iframe 的 target 才能读到 DOM / 点击按钮。

const [wsUrl, expr, timeoutArg] = process.argv.slice(2);
const timeoutMs = Number(timeoutArg || 20000);

if (!wsUrl || !expr) {
  console.log(JSON.stringify({ ok: false, error: 'usage: cdp_client.mjs <wsUrl> <expr> [timeoutMs]' }));
  process.exit(1);
}

const out = (obj) => {
  process.stdout.write(JSON.stringify(obj));
  process.exit(0);
};

const timer = setTimeout(() => out({ ok: false, error: 'timeout' }), timeoutMs);
let ws;
try {
  ws = new WebSocket(wsUrl);
} catch (e) {
  clearTimeout(timer);
  out({ ok: false, error: `connect failed: ${e.message}` });
}

ws.addEventListener('open', () => {
  ws.send(JSON.stringify({
    id: 1,
    method: 'Runtime.evaluate',
    params: { expression: expr, returnByValue: true, awaitPromise: true },
  }));
});

ws.addEventListener('message', (ev) => {
  let msg;
  try { msg = JSON.parse(ev.data); } catch { return; }
  if (msg.id !== 1) return;
  clearTimeout(timer);
  const r = msg.result || {};
  if (r.exceptionDetails) {
    const d = r.exceptionDetails;
    out({ ok: false, error: (d.exception && d.exception.description) || d.text || 'exception' });
    return;
  }
  const v = r.result ? r.result.value : undefined;
  out({ ok: true, value: v === undefined ? null : v });
});

ws.addEventListener('error', (e) => {
  clearTimeout(timer);
  out({ ok: false, error: `ws error: ${e.message || 'unknown'}` });
});

ws.addEventListener('close', () => {
  clearTimeout(timer);
});
