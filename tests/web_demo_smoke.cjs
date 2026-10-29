// Optional browser checks. Reuse an installed Playwright; no dependency installation.
// Start the deterministic server on 127.0.0.1:8765 before running this script.
const { chromium } = require(process.env.MEALPILOT_PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  let checks = 0;
  try {
    const context = await browser.newContext({viewport: {width: 1440, height: 1100}});
    await context.route('**/*', route => new URL(route.request().url()).origin === 'http://127.0.0.1:8765' ? route.continue() : route.abort());
    const page = await context.newPage();
    const scriptErrors = [];
    page.on('pageerror', error => scriptErrors.push(error.message));
    await page.goto('http://127.0.0.1:8765/');
    await page.screenshot({path: 'build/demo-refined-initial.png', fullPage: true});
    const input = page.getByLabel('Describe your meal');
    const submit = page.locator('#submit');
    const requestMeal = async text => {
      await input.fill(text);
      await submit.click();
      await page.waitForFunction(() => !document.getElementById('submit').disabled);
    };
    await page.getByRole('button', {name: 'Paneer, please'}).click();
    assert.match(await input.inputValue(), /with paneer without chicken/); checks++;
    await submit.click();
    await page.getByRole('heading', {name: '1. Paneer Rice Bowl', exact: true}).waitFor();
    assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'parsed'); checks++;
    assert.match(await page.locator('#output').innerText(), /180[\s\S]*Matches your preferred cuisine/);
    assert.equal(await page.locator('.meal').count(), 1); checks++;
    await requestMeal('without chicken and spicy budget 220');
    assert.match(await page.locator('#output').innerText(), /Clarify the exclusion scope/);
    assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'clarification'); checks++;
    assert.equal(await page.locator('.meal').count(), 0); checks++;
    await requestMeal('vegan budget 220');
    assert.match(await page.locator('#response-title').innerText(), /different approach/); checks++;
    assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'unsupported'); checks++;
    await requestMeal('budget 100');
    assert.match(await page.locator('#output').innerText(), /No matches[\s\S]*constraints were kept/); checks++;
    assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'empty'); checks++;
    await requestMeal('   ');
    assert.match(await page.locator('#error').innerText(), /Enter a meal request/); checks++;
    assert.equal(await input.getAttribute('aria-invalid'), 'true');
    assert.equal(await input.getAttribute('aria-errormessage'), 'error'); checks++;
    for (const code of [422, 500, 502, 503, 504]) {
      await page.route('**/recommend/text', route => route.fulfill({status: code, contentType: 'application/json', body: '{"detail":"private body never display"}'}));
      await requestMeal('budget 220');
      assert.equal(await page.locator('#error').isVisible(), true);
      assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'error');
      assert.doesNotMatch(await page.locator('body').innerText(), /private body/);
      await page.unroute('**/recommend/text'); checks++;
    }
    await page.route('**/recommend/text', route => route.abort());
    await requestMeal('budget 220');
    assert.match(await page.locator('#error').innerText(), /Check your connection/); checks++;
    await page.unroute('**/recommend/text');
    await page.route('**/recommend/text', route => route.fulfill({status: 200, contentType: 'application/json', body: 'not json'}));
    await requestMeal('budget 220');
    assert.equal(await page.locator('#error').isVisible(), true); checks++;
    await page.unroute('**/recommend/text');
    let releaseResponse;
    const pendingResponse = new Promise(resolve => { releaseResponse = resolve; });
    await page.route('**/recommend/text', async route => { await pendingResponse; await route.continue(); });
    await input.fill('budget 220');
    await submit.evaluate(node => node.click());
    assert.equal(await submit.isDisabled(), true);
    assert.equal(await page.locator('#response-panel').getAttribute('aria-busy'), 'true');
    assert.equal(await page.locator('#response-panel').getAttribute('data-state'), 'loading');
    assert.equal(await page.locator('#status').evaluate(node => !document.getElementById('response-panel').contains(node)), true); checks++;
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await page.locator('.loading-indicator span').first().evaluate(node => getComputedStyle(node).animationName), 'none'); checks++;
    await page.emulateMedia({reducedMotion: 'no-preference'});
    releaseResponse();
    await page.waitForFunction(() => !document.getElementById('submit').disabled); checks++;
    await page.unroute('**/recommend/text');
    let stalledRoute;
    await page.route('**/recommend/text', route => { stalledRoute = route; });
    await requestMeal('budget 220');
    assert.match(await page.locator('#error').innerText(), /request took too long/);
    assert.equal(await input.isEnabled(), true); checks++;
    await stalledRoute.abort().catch(() => {});
    await page.unroute('**/recommend/text');
    await page.route('**/recommend/text', async route => {
      const response = await route.fetch();
      const data = await response.json();
      data.result.recommendations[0].dish = '<img src=x onerror="window.injected=true">';
      data.result.recommendations[0].restaurant = 'LongCatalogName'.repeat(18);
      await route.fulfill({json: data});
    });
    await requestMeal('budget 220');
    assert.equal(await page.locator('#output img').count(), 0);
    assert.equal(await page.evaluate(() => window.injected), undefined); checks++;
    await page.setViewportSize({width: 320, height: 850});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true); checks++;
    await page.setViewportSize({width: 1440, height: 1100});
    await page.unroute('**/recommend/text');
    await requestMeal('Indian meal at most 220 with paneer without chicken');
    fs.mkdirSync('build', {recursive: true});
    await page.screenshot({path: 'build/demo-desktop.png', fullPage: true});
    for (const width of [320, 375, 768, 1440]) {
      await page.setViewportSize({width, height: 850});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true); checks++;
      for (const text of ['without chicken and spicy budget 220', 'vegan budget 220', 'budget 100']) {
        await requestMeal(text);
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true); checks++;
      }
      await requestMeal('Indian meal at most 220 with paneer without chicken');
      await page.route('**/recommend/text', route => route.fulfill({status: 503}));
      await requestMeal('budget 220');
      assert.equal(await page.locator('#error').isVisible(), true);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true); checks++;
      await page.unroute('**/recommend/text');
      await requestMeal('Indian meal at most 220 with paneer without chicken');
    }
    await page.setViewportSize({width: 375, height: 850});
    await page.screenshot({path: 'build/demo-mobile.png', fullPage: true});
    await input.focus();
    assert.equal(await input.evaluate(node => getComputedStyle(node).outlineStyle), 'solid');
    await page.keyboard.press('Tab');
    assert.equal(await page.getByRole('button', {name: 'Paneer, please'}).evaluate(node => node === document.activeElement), true); checks++;
    await submit.focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => !document.getElementById('submit').disabled);
    assert.equal(await page.locator('.meal').count(), 1); checks++;
    assert.deepEqual(scriptErrors, []); checks++;
    await input.focus();
    assert.equal(await input.evaluate(node => getComputedStyle(node).outlineWidth), '3px'); checks++;
    assert.equal(await page.locator('.ingredients').first().evaluate(node => parseFloat(getComputedStyle(node).fontSize) >= 14), true); checks++;
    const contrastChecks = await page.evaluate(() => {
      const luminance = color => {
        const values = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => value / 255).map(value => value <= .04045 ? value / 12.92 : ((value + .055) / 1.055) ** 2.4);
        return .2126 * values[0] + .7152 * values[1] + .0722 * values[2];
      };
      const ratio = (first, second) => { const a = luminance(first), b = luminance(second); return (Math.max(a, b) + .05) / (Math.min(a, b) + .05); };
      const style = selector => getComputedStyle(document.querySelector(selector));
      return [
        ['button text', ratio(style('#submit').color, style('#submit').backgroundColor), 4.5],
        ['muted text', ratio(style('.lead').color, style(':root').backgroundColor), 4.5],
        ['input placeholder', ratio(getComputedStyle(document.querySelector('textarea'), '::placeholder').color, style('textarea').backgroundColor), 4.5],
        ['input boundary', ratio(style('textarea').borderTopColor, style('textarea').backgroundColor), 3],
        ['intent heading', ratio(style('.intent h3').color, style('.intent').backgroundColor), 4.5],
        ['trust text', ratio(style('.trust-panel p').color, style('.trust-panel').backgroundColor), 4.5],
        ['input focus', ratio(style('textarea').outlineColor, style('.request-panel').backgroundColor), 3],
      ];
    });
    for (const [label, ratio, minimum] of contrastChecks) {
      assert.ok(ratio >= minimum, `${label}: ${ratio.toFixed(2)} < ${minimum}`); checks++;
    }
    console.log(`${checks} browser checks passed (real local API; failures/XSS use injected fixtures).`);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
