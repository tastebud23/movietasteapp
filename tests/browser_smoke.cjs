// Requires a running app, Node, Playwright and Chromium.
const { chromium } = require('playwright');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const output = process.env.MOVIETASTE_SCREENSHOTS || '/tmp/movietaste-screenshots';
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium',
    headless: true,
    args: ['--no-sandbox'],
  });
  try {
    for (const [name, viewport] of [
      ['desktop', { width: 1440, height: 1000 }],
      ['mobile', { width: 390, height: 844 }],
    ]) {
      const context = await browser.newContext({ viewport });
      const page = await context.newPage();
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      await page.goto(process.env.MOVIETASTE_BASE_URL || 'http://127.0.0.1:8501/', { waitUntil: 'networkidle' });
      await page.getByRole('heading', { name: 'Two tastes. One movie.', exact: true }).waitFor();
      await page.getByRole('textbox', { name: 'Your favourite films', exact: true }).waitFor();
      await page.getByRole('textbox', { name: 'Their favourite films', exact: true }).waitFor();
      await page.screenshot({ path: path.join(output, `${name}-landing.png`) });
      await page.getByRole('button', { name: 'See an example', exact: true }).click();
      const result = page.getByRole('heading', { name: '🎯 This is the one' });
      await result.waitFor();
      await result.evaluate(element => element.scrollIntoView({ block: 'start' }));
      await page.screenshot({ path: path.join(output, `${name}-results.png`) });
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
        throw new Error(`Horizontal overflow at ${name} width`);
      }
      if (errors.length) throw new Error(errors.join('\n'));
      await context.close();
    }
  } finally {
    await browser.close();
  }
  console.log(`PASS: desktop/mobile landing, example results and browser errors. Screenshots: ${output}`);
})().catch(error => {
  console.error(error);
  process.exitCode = 1;
});
