#!/usr/bin/env node
/**
 * cf_browser_fetch.js —— 真实浏览器抓取桥（**已移入 optional/，本机实测无效**）
 *
 * ⚠️ 不在主流程内，也没有任何脚本引用它。保留它只是留个记录。
 *
 * 为什么本机用不了（实测结论，2026-10-07）：
 *   Cloudflare 托管挑战的 JS 由 challenges.cloudflare.com 下发，而本机
 *   **直连与走 Clash 7897 对该域名都是 HTTP 000（0.46s 即失败）**。
 *   域名不可达 ⇒ 任何浏览器、任何 HTTP 客户端都拿不到挑战脚本 ⇒ 挑战必然
 *   解不开。同时 playwright-core 也未安装。
 *   结论：这不是脚本写得不对，而是网络环境限制，换实现也绕不过去。
 *
 * 因此本技能的 CF 兜底改成「人工/服务端抓取 + 回填」：
 *   1. 用 WebFetch（服务端出口，不受本机网络限制）打开搜索地址：
 *        https://www.rrdynb.com/plus/search.php?q=<关键词>&pagesize=11
 *   2. 拿到候选文章 URL 后一条命令回填：
 *        pansearch.py "<名称>" --rrdynb-articles "<url1>,<url2>"
 *
 * 什么情况下它还有用：换到能访问 challenges.cloudflare.com 的网络（并
 * `npm install playwright-core`）后，可用它把整条链路自动化。
 *
 * 用法（前提是上面两个条件都满足）：
 *   node cf_browser_fetch.js "<url>" [--out result.html] [--wait 60] [--headful]
 *
 * 输出：HTML 写到 --out（默认 stdout）。
 * 退出码：0 成功；4 挑战未通过（多半是 challenges.cloudflare.com 不可达）；1 其它错误。
 */

const fs = require('fs');
const os = require('os');
const path = require('path');

function findChrome() {
  const candidates = [];
  if (process.env.RRDY_CHROME) candidates.push(process.env.RRDY_CHROME);

  // agent-browser 下载的 Chrome
  const abDir = path.join(os.homedir(), '.agent-browser', 'browsers');
  if (fs.existsSync(abDir)) {
    for (const d of fs.readdirSync(abDir)) {
      candidates.push(path.join(abDir, d, 'chrome.exe'));
      candidates.push(path.join(abDir, d, 'chrome-win64', 'chrome.exe'));
    }
  }
  // ms-playwright
  const pwDir = path.join(os.homedir(), 'AppData', 'Local', 'ms-playwright');
  if (fs.existsSync(pwDir)) {
    for (const d of fs.readdirSync(pwDir)) {
      candidates.push(path.join(pwDir, d, 'chrome-win64', 'chrome.exe'));
      candidates.push(path.join(pwDir, d, 'chrome-win', 'chrome.exe'));
    }
  }
  // 系统 Chrome
  candidates.push('C:/Program Files/Google/Chrome/Application/chrome.exe');
  candidates.push('C:/Program Files (x86)/Google/Chrome/Application/chrome.exe');
  candidates.push('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome');
  candidates.push('/usr/bin/google-chrome');
  candidates.push('/usr/bin/chromium');

  for (const c of candidates) {
    try {
      if (c && fs.existsSync(c)) return c;
    } catch (_) {}
  }
  return null;
}

function arg(name, def) {
  const i = process.argv.indexOf(name);
  return i > -1 && process.argv[i + 1] ? process.argv[i + 1] : def;
}

(async () => {
  const url = process.argv[2];
  if (!url || url.startsWith('--')) {
    console.error('用法: node cf_browser_fetch.js "<url>" [--out file.html] [--wait 秒] [--headful]');
    process.exit(1);
  }
  const outFile = arg('--out', null);
  const maxWait = parseInt(arg('--wait', '60'), 10) * 1000;
  const headless = !process.argv.includes('--headful');

  let chromium;
  try {
    ({ chromium } = require('playwright-core'));
  } catch (e) {
    console.error('缺少依赖 playwright-core，请先执行:  npm install playwright-core');
    console.error('（若安装在别处，可设置 NODE_PATH 指向其 node_modules 目录）');
    process.exit(1);
  }

  const chromePath = findChrome();
  if (!chromePath) {
    console.error('未找到 Chrome/Chromium 可执行文件。可通过环境变量 RRDY_CHROME 指定路径。');
    process.exit(1);
  }
  console.error('[cf] chrome =', chromePath);

  const browser = await chromium.launch({
    executablePath: chromePath,
    headless,
    args: [
      '--disable-blink-features=AutomationControlled',
      '--no-sandbox',
      '--disable-dev-shm-usage',
      '--no-first-run',
      '--window-size=1366,900',
    ],
  });

  const ctx = await browser.newContext({
    userAgent:
      'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    viewport: { width: 1366, height: 900 },
  });

  await ctx.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
  });

  const page = await ctx.newPage();
  page.setDefaultTimeout(30000);

  try {
    await page.goto('https://www.rrdynb.com/index.html', {
      waitUntil: 'domcontentloaded',
      timeout: 40000,
    });
    await page.waitForTimeout(1500);
  } catch (_) {
    /* 预热失败不致命 */
  }

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  } catch (_) {}

  const RE = /Just a moment|请稍候|window\._cf_chl_opt|Enable JavaScript and cookies|正在进行安全验证/;
  const deadline = Date.now() + maxWait;
  let html = '';
  let ok = false;

  while (Date.now() < deadline) {
    await page.waitForTimeout(2000);
    try {
      html = await page.content();
    } catch (_) {
      continue;
    }
    if (!RE.test(html) && html.length > 3000) {
      ok = true;
      break;
    }
  }

  if (outFile) fs.writeFileSync(outFile, html, 'utf-8');
  else process.stdout.write(html);

  await browser.close();

  if (!ok) {
    console.error(
      '[cf] 挑战未通过。常见原因：本机无法访问 challenges.cloudflare.com（网络限制）。' +
        '请改用 WebFetch 兜底。'
    );
    process.exit(4);
  }
  console.error('[cf] OK, 取到 ' + html.length + ' 字节');
  process.exit(0);
})().catch((e) => {
  console.error('FATAL:', e.message);
  process.exit(1);
});
