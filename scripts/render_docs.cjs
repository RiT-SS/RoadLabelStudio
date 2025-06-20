// Capture the real UI against scripts/demo.py, with synthetic data only.
// Install Playwright separately; optionally set PLAYWRIGHT_PACKAGE / CHROMIUM_PATH.
const {chromium} = require(process.env.PLAYWRIGHT_PACKAGE || 'playwright');
const fs = require('fs');
const path = require('path');
const root = path.resolve(__dirname, '..');

(async () => {
  const launch = {headless: true};
  if (process.env.CHROMIUM_PATH) launch.executablePath = process.env.CHROMIUM_PATH;
  const browser = await chromium.launch(launch);
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1040}, deviceScaleFactor: 1});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto('http://127.0.0.1:18780/');
    await page.locator('.video-card').first().waitFor();
    const names = await page.locator('.video-card h3').allTextContents();
    if (names.length !== 3 || names.some(name => !name.startsWith('演示 '))) {
      throw Error('Refusing screenshots: expected only the three synthetic demo videos.');
    }
    const output = path.join(root, 'docs', 'images');
    fs.mkdirSync(output, {recursive: true});
    await page.evaluate(() => {
      const label = document.createElement('div');
      label.id = 'docs-demo-label';
      label.textContent = '界面演示 · 合成视频 / 预设标签 · 非真实模型输出';
      Object.assign(label.style, {position: 'fixed', bottom: '12px', left: '224px', zIndex: '10000', background: '#153b32', color: '#fff', padding: '8px 14px', borderRadius: '8px', fontSize: '12px', pointerEvents: 'none'});
      document.body.appendChild(label);
    });
    async function capture(name, fullPage = false) {
      await page.waitForFunction(() => [...document.images].filter(image => image.getBoundingClientRect().width > 0).every(image => image.complete && image.naturalWidth > 0));
      await page.evaluate(() => {document.activeElement?.blur(); window.scrollTo(0, 0);});
      await page.mouse.move(1430, 5);
      await page.screenshot({path: path.join(output, name + '.png'), fullPage, animations: 'disabled'});
    }
    if (process.argv.includes('--queue-only')) {
      await page.locator('nav [data-view="jobs"]').click();
      await page.locator('.runtime-strip').waitFor();
      await page.locator('.job-card').first().waitFor();
      await capture('queue');
      if (errors.length) throw Error(errors.join('\n'));
      console.log('Captured task queue using synthetic data. No browser errors.');
      return;
    }
    await capture('library');
    await page.locator('[data-video="demo-1"]').click();
    await page.locator('#frame-number').fill('32');
    await page.locator('#frame-number').press('Tab');
    await page.waitForFunction(() => document.querySelector('[data-score="crack"]')?.value === '0.93');
    await page.setViewportSize({width: 1440, height: 1120});
    await capture('review');
    await page.locator('.review-layout > div > .panel').nth(1).screenshot({path: path.join(output, 'filters.png'), animations: 'disabled', style: '#docs-demo-label {visibility: hidden}'});
    await page.setViewportSize({width: 1440, height: 1040});
    await page.locator('#new-job').click();
    await page.locator('#job-dialog').waitFor({state: 'visible'});
    await capture('ai-task');
    await page.locator('[data-close="job-dialog"]').first().click();
    await page.locator('nav [data-view="skills"]').click();
    await page.locator('.skill-card').first().waitFor();
    await capture('skills');
    await page.locator('nav [data-view="jobs"]').click();
    await page.locator('.runtime-strip').waitFor();
    await capture('queue');
    if (errors.length) throw Error(errors.join('\n'));
    console.log('Captured 6 UI views using synthetic data. No browser errors.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
