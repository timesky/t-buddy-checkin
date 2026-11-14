#!/usr/bin/env node
// 真实鼠标输入（trusted event）：CDP Input.dispatchMouseEvent。
//
// 为什么需要它：成长中心日历的补签格（.gs-cal-cell.is-makeup-clickable）只对
// **可信**点击有反应 —— 用 JS 的 element.click() 或手工 dispatchEvent 派发
// PointerEvent/MouseEvent，页面什么也不做（2026-11-14 实测：连点三次无任何
// 弹层；同样位置用 Input.dispatchMouseEvent 立刻弹出
// `.gs-cell-toast--makeup`「消耗一张补登卡 使用」）。
//
// 用法: node cdp_input.mjs <wsUrl> <x> <y>
// 输出: {"ok":true,"x":..,"y":..} | {"ok":false,"error":".."}
//
// 坐标是 target 视口坐标（页面内 getBoundingClientRect 的 left/top），
// 调用方必须先 scrollIntoView 把元素滚进视口，否则点不中。

const [wsUrl, xArg, yArg] = process.argv.slice(2);
const x = Number(xArg);
const y = Number(yArg);

if (!wsUrl || !Number.isFinite(x) || !Number.isFinite(y)) {
  console.log(JSON.stringify({ ok: false, error: 'usage: cdp_input.mjs <wsUrl> <x> <y>' }));
  process.exit(1);
}

const out = (obj) => {
  process.stdout.write(JSON.stringify(obj));
  process.exit(0);
};

const timer = setTimeout(() => out({ ok: false, error: 'timeout' }), 15000);
let ws;
try {
  ws = new WebSocket(wsUrl);
} catch (e) {
  clearTimeout(timer);
  out({ ok: false, error: `connect failed: ${e.message}` });
}

let id = 0;
const send = (method, params) => {
  ws.send(JSON.stringify({ id: ++id, method, params }));
};

ws.addEventListener('open', () => {
  send('Input.dispatchMouseEvent', { type: 'mouseMoved', x, y, button: 'none', clickCount: 0 });
  send('Input.dispatchMouseEvent', { type: 'mousePressed', x, y, button: 'left', clickCount: 1 });
  setTimeout(() => {
    send('Input.dispatchMouseEvent', { type: 'mouseReleased', x, y, button: 'left', clickCount: 1 });
    setTimeout(() => {
      clearTimeout(timer);
      out({ ok: true, x, y });
    }, 350);
  }, 60);
});

ws.addEventListener('error', (e) => {
  clearTimeout(timer);
  out({ ok: false, error: `ws error: ${e.message || 'unknown'}` });
});
