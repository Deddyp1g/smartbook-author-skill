#!/usr/bin/env node
// Run the same explicit interaction scenario at two viewport sizes.
const { createHash } = require('node:crypto');
const { chromium } = require('playwright');

async function main() {
  const [url, expectedHash, rawSteps, executablePath] = process.argv.slice(2);
  const steps = JSON.parse(rawSteps);
  const checks = { http: true, desktop: true, mobile: true, interaction: true, console: true };
  const views = [];
  const browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
  try {
    for (const [name, viewport] of [['desktop', { width: 1440, height: 900 }], ['mobile', { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport, isMobile: name === 'mobile', hasTouch: name === 'mobile' });
      const page = await context.newPage();
      const errors = [];
      const failedRequests = [];
      const stepResults = [];
      page.on('pageerror', error => errors.push(error.message));
      page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
      page.on('requestfailed', request => failedRequests.push(`${request.method()} ${request.url()}`));
      page.on('response', response => { if (response.status() >= 400) failedRequests.push(`HTTP ${response.status()} ${response.url()}`); });
      let pageOk = false;
      try {
        const response = await page.goto(url, { waitUntil: 'load', timeout: 30000 });
        const index = response && await response.body();
        const httpOk = Boolean(response && response.ok() && createHash('sha256').update(index).digest('hex') === expectedHash);
        checks.http = checks.http && httpOk;
        await page.locator('body').waitFor({ state: 'visible', timeout: 10000 });
        const dimensions = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: window.innerWidth }));
        pageOk = dimensions.width <= dimensions.viewport + 1;
        for (const step of steps) {
          try {
            if (await page.locator(step.expect_visible).first().isVisible()) {
              throw new Error('Expected result is already visible before the click');
            }
            await page.locator(step.click).first().click({ timeout: 10000 });
            await page.locator(step.expect_visible).first().waitFor({ state: 'visible', timeout: 10000 });
            stepResults.push({ name: step.name, ok: true });
          } catch (error) {
            stepResults.push({ name: step.name, ok: false, error: String(error.message).slice(0, 400) });
            checks.interaction = false;
          }
        }
      } catch (error) {
        errors.push(String(error.message).slice(0, 400));
        checks.http = false;
        checks.interaction = false;
      }
      checks[name] = pageOk;
      checks.console = checks.console && !errors.length && !failedRequests.length;
      views.push({ name, viewport, layout_ok: pageOk, steps: stepResults, errors, failed_requests: failedRequests });
      await context.close();
    }
  } finally {
    await browser.close();
  }
  return { ok: Object.values(checks).every(Boolean), checks, views,
    limitations: 'Checks the supplied scenario and static preview only; platform reader compatibility requires separate verification.' };
}

main().then(result => console.log(JSON.stringify(result))).catch(error => {
  console.error(String(error.stack || error));
  process.exitCode = 1;
});
