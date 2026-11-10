// CDP 真浏览器验证：页面最上方的共享剪贴板
// 用法：node probe_clip.mjs
import { spawn } from "child_process";
import fs from "fs";
import os from "os";
import path from "path";

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const BASE = process.env.DRAGDRAG_BASE || "http://10.10.16.166";
const OUTDIR = os.tmpdir();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let pass = 0, fail = 0;
const ok = (m, c = true) => { if (c) { pass++; console.log("  ✅ " + m); } else { fail++; console.log("  ❌ " + m); } };

async function openPage(url, port, { width = 1280, height = 1400 } = {}) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "clip-"));
  const p = spawn(EDGE, [
    "--headless=new", `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
    "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    `--window-size=${width},${height}`, url,
  ], { stdio: "ignore" });
  let wsUrl = null;
  for (let i = 0; i < 60 && !wsUrl; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl && (t.url || "").startsWith(url));
      if (page) wsUrl = page.webSocketDebuggerUrl;
    } catch (e) {}
    if (!wsUrl) await sleep(300);
  }
  if (!wsUrl) throw new Error("CDP 未就绪: " + url);
  const ws = new WebSocket(wsUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let id = 0;
  const pend = new Map();
  const errs = [];
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && pend.has(m.id)) { const { res, rej } = pend.get(m.id); pend.delete(m.id); m.error ? rej(new Error(JSON.stringify(m.error))) : res(m.result); }
    else if (m.method === "Runtime.exceptionThrown") errs.push(m.params.exceptionDetails.text + " " + (m.params.exceptionDetails.exception?.description ?? ""));
    else if (m.method === "Runtime.consoleAPICalled" && m.params.type === "error") errs.push(m.params.args.map((a) => a.value ?? a.description).join(" "));
  };
  const send = (method, params) => new Promise((res, rej) => { const i = ++id; pend.set(i, { res, rej }); ws.send(JSON.stringify({ id: i, method, params })); });
  await send("Runtime.enable");
  await send("Page.enable");
  await sleep(2500);
  return { send, errs, close: () => { try { ws.close(); } catch (e) {} try { p.kill(); } catch (e) {} } };
}

const evaluate = async (cdp, expr, awaitPromise = false) =>
  (await cdp.send("Runtime.evaluate", { returnByValue: true, awaitPromise, expression: expr })).result.value;

async function shot(cdp, file, clip) {
  const r = await cdp.send("Page.captureScreenshot", { format: "png", captureBeyondViewport: true, ...(clip ? { clip } : {}) });
  fs.writeFileSync(file, Buffer.from(r.data, "base64"));
  console.log("  📷 " + file);
}

async function main() {
  const cdp = await openPage(BASE + "/", 9391);
  const tag = "探针" + Date.now().toString().slice(-5);

  console.log("\n[1] 位置：必须在最上方");
  const layout = await evaluate(cdp, `(() => {
    const card = document.getElementById('clip-card');
    const h2 = document.querySelector('h2');
    const drop = document.getElementById('drop-area');
    if (!card) return null;
    return {
      cardTop: Math.round(card.getBoundingClientRect().top + scrollY),
      h2Top: Math.round(h2.getBoundingClientRect().top + scrollY),
      dropTop: Math.round(drop.getBoundingClientRect().top + scrollY),
      w: Math.round(card.getBoundingClientRect().width),
      visible: card.getBoundingClientRect().height > 0,
      title: card.querySelector('.clip-title strong').textContent.trim(),
      firstBlock: document.body.firstElementChild === card,
    };
  })()`);
  ok("卡片存在且可见", layout && layout.visible);
  ok("是 body 的第一个块", layout?.firstBlock === true);
  ok(`在 h2 之上（card ${layout?.cardTop} < h2 ${layout?.h2Top}）`, layout.cardTop < layout.h2Top);
  ok(`也在上传区之上（< ${layout?.dropTop}）`, layout.cardTop < layout.dropTop);
  ok(`标题是「${layout?.title}」`, /共享剪贴板/.test(layout.title || ""));

  console.log("\n[2] 通过界面新增");
  await evaluate(cdp, `(() => {
    const t = document.getElementById('clip-new');
    t.value = ${JSON.stringify(tag + " abc")};
    document.getElementById('clip-add').click();
    return true;
  })()`);
  await sleep(1200);
  const afterAdd = await evaluate(cdp, `(() => {
    const rows = [...document.querySelectorAll('#clip-list .clip-item')];
    return { n: rows.length, first: rows[0]?.querySelector('.clip-text')?.textContent || '',
             empty: document.getElementById('clip-empty').hidden,
             count: document.getElementById('clip-count').textContent,
             composeEmpty: document.getElementById('clip-new').value === '' };
  })()`);
  ok(`列表第一条就是刚加的（${afterAdd.first.slice(0, 24)}）`, afterAdd.first.startsWith(tag));
  ok("输入框已清空", afterAdd.composeEmpty);
  ok(`空提示已隐藏、计数显示「${afterAdd.count}」`, afterAdd.empty === true && /\d+ 条/.test(afterAdd.count));
  const srvAfterAdd = await evaluate(cdp, `fetch('/api/clipboard',{cache:'no-store'}).then(r=>r.json()).then(d=>d.items[0])`, true);
  ok("服务端确实存下了", (srvAfterAdd?.text || "").startsWith(tag));

  console.log("\n[3] 编辑");
  await evaluate(cdp, `(() => {
    document.querySelector('#clip-list .clip-item .clip-item-actions .clip-btn:nth-child(2)').click();
    return true;
  })()`);
  await sleep(300);
  const editing = await evaluate(cdp, `(() => {
    const a = document.querySelector('.clip-edit-area');
    return { has: !!a, val: a?.value || '', bar: !!document.querySelector('.clip-edit-bar') };
  })()`);
  ok("出现编辑框并带入原文", editing.has && editing.val.startsWith(tag));
  await evaluate(cdp, `(() => {
    const a = document.querySelector('.clip-edit-area');
    a.value = ${JSON.stringify(tag + " 改过了")};
    document.querySelector('.clip-edit-bar .clip-btn:last-child').click();
    return true;
  })()`);
  await sleep(1200);
  const afterEdit = await evaluate(cdp, `(() => ({
    text: document.querySelector('#clip-list .clip-item .clip-text')?.textContent || '',
    stillEditing: !!document.querySelector('.clip-edit-area'),
  }))()`);
  ok("保存后显示新内容", afterEdit.text === tag + " 改过了");
  ok("编辑态已退出", afterEdit.stillEditing === false);
  const srvAfterEdit = await evaluate(cdp, `fetch('/api/clipboard',{cache:'no-store'}).then(r=>r.json()).then(d=>d.items.find(x=>x.text.indexOf('${tag}')===0))`, true);
  ok("服务端也改了", (srvAfterEdit?.text || "").endsWith("改过了"));

  console.log("\n[4] 复制按钮（用真鼠标事件点，才有用户手势）");
  const env = await evaluate(cdp, `({secure: window.isSecureContext, hasApi: !!navigator.clipboard})`);
  const btnPos = await evaluate(cdp, `(() => {
    const b = document.querySelector('#clip-list .clip-item .clip-item-actions .clip-btn');
    const r = b.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  })()`);
  await cdp.send("Input.dispatchMouseEvent", { type: "mousePressed", x: btnPos.x, y: btnPos.y, button: "left", clickCount: 1 });
  await cdp.send("Input.dispatchMouseEvent", { type: "mouseReleased", x: btnPos.x, y: btnPos.y, button: "left", clickCount: 1 });
  await sleep(500);
  const copyRes = await evaluate(cdp, `(() => {
    const btn = document.querySelector('#clip-list .clip-item .clip-item-actions .clip-btn');
    const sel = String(window.getSelection());
    return { label: btn.textContent.trim(), selected: sel.slice(0, 40) };
  })()`);
  ok(`http 环境没有 clipboard API（secure=${env.secure}, hasApi=${env.hasApi}）`, env.secure === false && env.hasApi === false);
  ok(`真点击后给了明确反馈（${copyRes.label}）`, /已复制|已选中/.test(copyRes.label));
  ok("万一复制不成也会把文本选中，让用户 Ctrl+C",
     /已复制/.test(copyRes.label) || copyRes.selected.startsWith(tag));

  console.log("\n[5] 富文本注入只会当文本");
  await evaluate(cdp, `(() => {
    const t = document.getElementById('clip-new');
    t.value = '<img src=x onerror="window.__xss=1"><b>粗</b>';
    document.getElementById('clip-add').click();
    return true;
  })()`);
  await sleep(1200);
  const xss = await evaluate(cdp, `(() => {
    const row = document.querySelector('#clip-list .clip-item');
    const box = row.querySelector('.clip-text');
    return { hasImg: !!box.querySelector('img'), hasB: !!box.querySelector('b'),
             xss: window.__xss, text: box.textContent.slice(0, 40),
             rows: document.querySelectorAll('#clip-list .clip-item').length };
  })()`);
  ok("没生成 img/b 元素（按纯文本渲染）", xss.hasImg === false && xss.hasB === false);
  ok("onerror 没执行", xss.xss === undefined);
  ok(`原文照旧可见：${xss.text}`, xss.text.startsWith("<img"));

  console.log("\n[6] 别人加的东西会自己出现（6 秒轮询）");
  const remote = tag + " 来自别人";
  await evaluate(cdp, `fetch('/api/clipboard',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:${JSON.stringify(remote)}})}).then(r=>r.json())`, true);
  await sleep(900);
  const beforePoll = await evaluate(cdp, `[...document.querySelectorAll('#clip-list .clip-text')].some(e=>e.textContent===${JSON.stringify(remote)})`);
  await sleep(7000);
  const afterPoll = await evaluate(cdp, `[...document.querySelectorAll('#clip-list .clip-text')].some(e=>e.textContent===${JSON.stringify(remote)})`);
  ok("轮询前没渲染（证明是真轮询刷新）", beforePoll === false);
  ok("6 秒后自动出现", afterPoll === true);

  console.log("\n[7] 删除要点两次");
  const del1 = await evaluate(cdp, `(() => {
    const rows = [...document.querySelectorAll('#clip-list .clip-item')];
    const row = rows.find(r => r.querySelector('.clip-text')?.textContent === ${JSON.stringify(remote)});
    const btn = row.querySelector('.clip-item-actions .clip-btn.danger');
    btn.click();
    return { label: btn.textContent, n: rows.length };
  })()`);
  await sleep(500);
  const del1b = await evaluate(cdp, `document.querySelectorAll('#clip-list .clip-item').length`);
  ok(`第一次点击只进入警戒（「${del1.label}」）`, del1.label === "再点一次" && del1b === del1.n);
  await evaluate(cdp, `(() => {
    const rows = [...document.querySelectorAll('#clip-list .clip-item')];
    const row = rows.find(r => r.querySelector('.clip-text')?.textContent === ${JSON.stringify(remote)});
    row.querySelector('.clip-item-actions .clip-btn.danger').click();
    return true;
  })()`);
  await sleep(1200);
  const del2 = await evaluate(cdp, `({ n: [...document.querySelectorAll('#clip-list .clip-text')].filter(e=>e.textContent===${JSON.stringify(remote)}).length })`);
  const srvGone = await evaluate(cdp, `fetch('/api/clipboard',{cache:'no-store'}).then(r=>r.json()).then(d=>d.items.filter(x=>x.text===${JSON.stringify(remote)}).length)`, true);
  ok("第二次点击才真删", del2.n === 0 && srvGone === 0);

  console.log("\n[8] Ctrl+Enter 也能放");
  await evaluate(cdp, `(() => {
    const t = document.getElementById('clip-new');
    t.value = ${JSON.stringify(tag + " 快捷键")};
    t.focus();
    t.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', ctrlKey:true, bubbles:true, cancelable:true}));
    return true;
  })()`);
  await sleep(1200);
  const shortcut = await evaluate(cdp, `[...document.querySelectorAll('#clip-list .clip-text')].some(e=>e.textContent===${JSON.stringify(tag + " 快捷键")})`);
  ok("Ctrl+Enter 生效", shortcut === true);

  console.log("\n[9] 原有功能没坏");
  const legacy = await evaluate(cdp, `(() => ({
    drop: !!document.getElementById('drop-area'),
    sections: document.querySelectorAll('#sections-container .desktop-section').length,
    pwdHidden: document.getElementById('pwd-mask').hidden,
    pwdInputType: document.getElementById('pwd-input').type,
    h2: document.querySelector('h2').textContent.trim(),
  }))()`);
  ok("上传区还在", legacy.drop);
  ok(`分区正常渲染（${legacy.sections} 个）`, legacy.sections > 0);
  ok("密码弹窗默认隐藏、输入框是 password", legacy.pwdHidden === true && legacy.pwdInputType === "password");
  ok("标题仍是「局域网文件传输」", legacy.h2 === "局域网文件传输");

  await shot(cdp, path.join(OUTDIR, "clip-card-desktop.png"),
    { x: 0, y: 0, width: 1280, height: 520, scale: 1 });
  ok("桌面端无 JS 异常", cdp.errs.length === 0);
  if (cdp.errs.length) console.log("     " + cdp.errs.join("\n     "));
  cdp.close();

  console.log("\n[10] 手机宽度 390px");
  const m = await openPage(BASE + "/", 9392, { width: 390, height: 900 });
  const mob = await evaluate(m, `(() => {
    const card = document.getElementById('clip-card');
    const r = card.getBoundingClientRect();
    return {
      overflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      cardW: Math.round(r.width), vw: window.innerWidth,
      addW: Math.round(document.getElementById('clip-add').getBoundingClientRect().width),
      stacked: getComputedStyle(document.querySelector('.clip-compose')).flexDirection,
    };
  })()`);
  ok(`没有横向溢出（${mob.overflowX}px）`, mob.overflowX <= 0);
  ok(`卡片不超屏（${mob.cardW} ≤ ${mob.vw}）`, mob.cardW <= mob.vw);
  ok(`按钮改成纵向排列（flex-direction=${mob.stacked}）`, mob.stacked === "column");
  await shot(m, path.join(OUTDIR, "clip-card-mobile.png"), { x: 0, y: 0, width: 390, height: 620, scale: 2 });
  ok("移动端无 JS 异常", m.errs.length === 0);
  m.close();

  // 收尾：删掉本次探针造的所有条目
  const list = await (await fetch(BASE + "/api/clipboard")).json();
  let removed = 0;
  for (const it of list.items) {
    if (it.text.startsWith(tag) || it.text.startsWith("<img src=x")) {
      await fetch(BASE + "/api/clipboard/" + it.id, { method: "DELETE" });
      removed++;
    }
  }
  const left = (await (await fetch(BASE + "/api/clipboard")).json()).items;
  ok(`探针数据已清理（删 ${removed} 条，剩 ${left.length} 条他人内容）`,
     left.every((x) => !x.text.startsWith(tag) && !x.text.startsWith("<img src=x")));

  console.log(`\n===== ${pass} 通过 / ${fail} 失败 =====`);
  process.exit(fail ? 1 : 0);
}

main();
