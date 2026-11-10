// 真浏览器验证 dragdrag 删除密码交互
// 流程：上传两个测试文件（临时组 / 任一正式组）→ 点删除按钮 → 看要不要密码
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = process.env.DRAGDRAG_BASE || "http://10.10.16.166/";
// 口令只从环境 / 不入库的 .admin_password 读，仓库里不写死
const PW = process.env.DRAGDRAG_ADMIN_PASSWORD
  || (fs.existsSync(".admin_password") ? fs.readFileSync(".admin_password", "utf8").trim() : "");
if (!PW) { console.error("请先设置 DRAGDRAG_ADMIN_PASSWORD"); process.exit(2); }
const _sections = await (await fetch(BASE + "/sections")).json();
const _formal = _sections.find((s) => s.id !== "default");
const FORMAL = _formal?.id;
const FORMAL_NAME = _formal?.name || "";
if (!FORMAL) { console.error("找不到非临时分区，无法测试"); process.exit(2); }
const PORT = 9351;
const OUTDIR = os.tmpdir();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let pass = 0, fail = 0;
function ok(msg, cond = true) {
  if (cond) { pass++; console.log("  ✅ " + msg); }
  else { fail++; console.log("  ❌ " + msg); }
}
function head(t) { console.log("\n" + t); }

const profile = fs.mkdtempSync(path.join(os.tmpdir(), "pwdprobe-"));
const proc = spawn(EDGE, [
  "--headless=new", `--remote-debugging-port=${PORT}`, `--user-data-dir=${profile}`,
  "--no-first-run", "--no-default-browser-check", "--disable-extensions",
  "--window-size=1400,1000", BASE,
], { stdio: "ignore" });

let ws, send, errs = [];
try {
  let wsUrl = null;
  for (let i = 0; i < 50 && !wsUrl; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
      wsUrl = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl)?.webSocketDebuggerUrl;
    } catch (e) {}
    if (!wsUrl) await sleep(300);
  }
  if (!wsUrl) throw new Error("CDP 未就绪");

  ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pend = new Map();
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) {
      const { res, rej } = pend.get(m.id); pend.delete(m.id);
      m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result);
    } else if (m.method === "Runtime.exceptionThrown") {
      errs.push(m.params.exceptionDetails.text + " " + (m.params.exceptionDetails.exception?.description ?? ""));
    } else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") {
      errs.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
    }
  };
  send = (method, params) => new Promise((res, rej) => {
    const i = ++id; pend.set(i, { res, rej });
    ws.send(JSON.stringify({ id: i, method, params }));
  });

  await send("Runtime.enable");
  await send("Page.enable");
  await sleep(2500);

  const evaluate = async (expr, awaitPromise = false) =>
    (await send("Runtime.evaluate", { returnByValue: true, expression: expr, awaitPromise })).result.value;

  // ---------- 0. 页面结构
  head("[0] 页面与弹窗结构");
  const dom = await evaluate(`(() => {
    const mask = document.getElementById('pwd-mask');
    const input = document.getElementById('pwd-input');
    return {
      maskExists: !!mask,
      maskHidden: mask ? mask.hidden : null,
      inputType: input ? input.type : null,
      hasCancel: !!document.getElementById('pwd-cancel'),
      hasOk: !!document.getElementById('pwd-ok'),
      plaintextPwd: document.documentElement.outerHTML.includes(${JSON.stringify(PW)}),
      pageText: document.body.innerText.length,
    };
  })()`);
  ok("密码弹窗已在页面里", dom.maskExists);
  ok("弹窗默认隐藏", dom.maskHidden === true);
  ok("输入框是 password 类型（不明文显示）", dom.inputType === "password");
  ok("有取消 / 确定按钮", dom.hasCancel && dom.hasOk);
  ok("页面源码里没有明文管理员口令", dom.plaintextPwd === false);

  // ---------- 1. 准备测试文件
  head("[1] 上传测试文件");
  const TS = Date.now();
  const A = `pwd-ui-temp-${TS}.txt`;
  const B = `pwd-ui-sec-${TS}.txt`;
  const prep = await evaluate(`(async () => {
    async function up(name, sec) {
      const fd = new FormData();
      fd.append('file', new File(['hello'], name, { type: 'text/plain' }));
      fd.append('targetSection', sec);
      const r = await fetch('/upload', { method: 'POST', body: fd });
      return r.status;
    }
    const a = await up(${JSON.stringify(A)}, 'default');
    const b = await up(${JSON.stringify(B)}, ${JSON.stringify(FORMAL)});
    await loadSectionsFromServer();
    await new Promise(r => setTimeout(r, 500));
    return { a, b };
  })()`, true);
  ok(`临时组测试文件上传成功（HTTP ${prep.a}）`, prep.a === 200);
  ok(`正式组「${FORMAL_NAME}」测试文件上传成功（HTTP ${prep.b}）`, prep.b === 200);

  // stub 掉原生确认框，拦住 alert 以便断言
  await evaluate(`(() => {
    window.__alerts = [];
    window.confirm = () => true;
    window.alert = (m) => window.__alerts.push(String(m));
    // 记录弹窗出现的时刻
    window.__maskLog = [];
    const mask = document.getElementById('pwd-mask');
    new MutationObserver(() => {
      window.__maskLog.push({ hidden: mask.hidden, t: Date.now() });
    }).observe(mask, { attributes: true, attributeFilter: ['hidden'] });
    return true;
  })()`);

  const clickDelete = (fname) => evaluate(`(() => {
    const tiles = [...document.querySelectorAll('.file-tile')];
    const t = tiles.find(x => x.querySelector('.file-name')?.textContent?.trim() === ${JSON.stringify(fname)});
    if (!t) return 'tile-not-found';
    const btns = t.querySelectorAll('.file-actions button');
    if (btns.length < 2) return 'no-delete-btn';
    btns[btns.length - 1].click();
    return 'clicked';
  })()`);

  const state = () => evaluate(`(() => ({
    maskHidden: document.getElementById('pwd-mask').hidden,
    alerts: window.__alerts.slice(),
    hasA: [...document.querySelectorAll('.file-name')].some(e => e.textContent.trim() === ${JSON.stringify(A)}),
    hasB: [...document.querySelectorAll('.file-name')].some(e => e.textContent.trim() === ${JSON.stringify(B)}),
  }))()`);

  // ---------- 2. 临时组：不该弹密码
  head("[2] 临时组删除：不应弹密码框");
  const r2 = await clickDelete(A);
  ok(`找到临时组文件并点了删除（${r2}）`, r2 === "clicked");
  await sleep(1200);
  const s2 = await state();
  ok("没有弹密码框", s2.maskHidden === true);
  ok("文件已从页面消失", s2.hasA === false);
  ok("没有任何 alert 报错", s2.alerts.length === 0);

  // ---------- 3. 正式组：必须弹密码
  head("[3] 正式组删除：必须弹密码框");
  const r3 = await clickDelete(B);
  ok(`找到正式组文件并点了删除（${r3}）`, r3 === "clicked");
  await sleep(600);
  const s3 = await state();
  ok("密码框弹出来了", s3.maskHidden === false);
  ok("文件还在（还没删）", s3.hasB === true);
  const hint = await evaluate(`document.getElementById('pwd-hint').textContent`);
  ok(`提示里说清了是哪个组：${hint}`, /密码/.test(hint) && hint.includes(FORMAL_NAME));

  // 取消
  await evaluate(`document.getElementById('pwd-cancel').click()`);
  await sleep(400);
  const s4 = await state();
  ok("点取消后弹窗关闭", s4.maskHidden === true);
  ok("取消后文件仍在", s4.hasB === true);

  await send("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }).then((r) => {
    // 重新弹一次，为了截图
  });

  // ---------- 4. 错误密码
  head("[4] 输入错误密码");
  await clickDelete(B);
  await sleep(400);
  await send("Page.captureScreenshot", { format: "png" }).then(async (r) => {
    fs.writeFileSync(path.join(OUTDIR, "pwd-dialog.png"), Buffer.from(r.data, "base64"));
  });
  await evaluate(`(() => {
    const i = document.getElementById('pwd-input');
    i.value = 'wrong-password';
    document.getElementById('pwd-ok').click();
    return true;
  })()`);
  await sleep(1500);
  const s5 = await state();
  ok("密码框已关闭", s5.maskHidden === true);
  ok("弹出了错误提示", s5.alerts.length === 1);
  ok(`提示内容合理：${s5.alerts[0] || ""}`, /密码|临时组/.test(s5.alerts[0] || ""));
  ok("文件仍在（错误密码没能删掉）", s5.hasB === true);

  // ---------- 5. 正确密码
  head("[5] 输入正确密码");
  await clickDelete(B);
  await sleep(400);
  await evaluate(`(() => {
    const i = document.getElementById('pwd-input');
    i.value = ${JSON.stringify(PW)};
    document.getElementById('pwd-ok').click();
    return true;
  })()`);
  await sleep(1800);
  const s6 = await state();
  ok("密码正确后文件被删除", s6.hasB === false);

  // ---------- 6. 回车提交
  head("[6] 回车也能提交");
  const C = `pwd-ui-enter-${TS}.txt`;
  await evaluate(`(async () => {
    const fd = new FormData();
    fd.append('file', new File(['hi'], ${JSON.stringify(C)}, { type: 'text/plain' }));
    fd.append('targetSection', ${JSON.stringify(FORMAL)});
    await fetch('/upload', { method: 'POST', body: fd });
    await loadSectionsFromServer();
    await new Promise(r => setTimeout(r, 400));
  })()`, true);
  await clickDelete(C);
  await sleep(400);
  await evaluate(`(() => {
    const i = document.getElementById('pwd-input');
    i.value = ${JSON.stringify(PW)};
    i.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    return true;
  })()`);
  await sleep(1800);
  const s7 = await evaluate(`(() => ({
    hasC: [...document.querySelectorAll('.file-name')].some(e => e.textContent.trim() === ${JSON.stringify(C)}),
  }))()`);
  ok("回车提交后文件被删除", s7.hasC === false);

  // ---------- 7. 收尾检查
  head("[7] 收尾");
  const final = await evaluate(`(() => ({
    maskHidden: document.getElementById('pwd-mask').hidden,
    inputCleared: document.getElementById('pwd-input').value === '',
  }))()`);
  ok("弹窗最终处于关闭状态", final.maskHidden === true);
  ok("密码输入框已清空（不残留）", final.inputCleared === true);

  // 清理：确认三个测试文件都不在了
  const leftover = await evaluate(`(async () => {
    const r = await fetch('/files');
    const list = await r.json();
    return list.filter(n => n.includes('pwd-ui-'));
  })()`, true);
  ok(`磁盘上没有残留测试文件（${JSON.stringify(leftover)}）`, leftover.length === 0);

  ok(`全程 JS 异常 ${errs.length} 条`, errs.length === 0);
  if (errs.length) errs.slice(0, 5).forEach((e) => console.log("     " + e));

  console.log(`\n=== ${pass} 通过 / ${fail} 失败 ===`);
  console.log("截图:", path.join(OUTDIR, "pwd-dialog.png"));
} finally {
  try { ws?.close(); } catch (e) {}
  proc.kill();
  try { fs.rmSync(profile, { recursive: true, force: true }); } catch (e) {}
  process.exit(fail > 0 ? 1 : 0);
}
