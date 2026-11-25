/* Run with NODE_PATH pointing to an installed Playwright package. Synthetic data only. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { chromium } = require('playwright');
const root = path.resolve(__dirname, '..');
const artifacts = fs.mkdtempSync(path.join(os.tmpdir(), 'loci-desk-qa-'));
const text = '😀 原告：星河测试有限公司。\n受托人：陈思远。\n陈思远提交材料。\n陈\n思远确认收到。\nAnn met ANN, not Joanne.\n' +
  '复核用虚构材料，不涉及真实案件。\n'.repeat(55);
const errors = [];

async function main() {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1140, height: 740 } });
    page.on('pageerror', e => errors.push(String(e)));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const p = url.pathname;
      let data = { ok: true };
      if (p === '/') return route.fulfill({contentType:'text/html', body:fs.readFileSync(path.join(root,'loci/templates/index.html'))});
      if (p.startsWith('/static/')) return route.fulfill({contentType:p.endsWith('.css')?'text/css':'application/javascript',body:fs.readFileSync(path.join(root,'loci',p))});
      if (p === '/api/cases') data = {cases:[{key:'demo',name:'虚构测试案件'}]};
      else if (p === '/api/status') data = {daemon:{running:false},inbox_count:0,case_count:1,paths:{inbox:'/demo/inbox',cases:'/demo/cases'}};
      else if (p === '/api/desensitize/files') data = {ok:true,files:[{rel:'sample.txt',name:'虚构测试材料.txt',ext:'txt',dir:'',size:3200}]};
      else if (p === '/api/desensitize/analyze') data = {ok:true,text,spans:[],ner:false,source_name:'虚构测试材料.txt'};
      else if (p === '/api/desensitize/apply') {
        const proc = spawnSync(path.join(root,'.venv/bin/python'), [path.join(__dirname,'desk_apply_fixture.py')], {input:route.request().postData(),encoding:'utf8'});
        assert.equal(proc.status,0,proc.stderr);
        data = JSON.parse(proc.stdout);
      } else if (p === '/api/desensitize/pick-output') data = {ok:true,output_id:'test-output',location:'/demo/output'};
      else if (p === '/api/log') data = {lines:[]};
      else if (p === '/api/review') data = {items:[]};
      await route.fulfill({json:data});
    });
    await page.goto('http://127.0.0.1:5081/');
    await page.waitForLoadState('networkidle');
    assert.equal(await page.locator('.dashboard-intro').count(), 0);
    await page.screenshot({path:path.join(artifacts,'dashboard.png')});
    await page.locator('[data-sec="desk"]').click();
    await page.locator('#desk-case').selectOption('demo');
    await page.locator('#desk-file').selectOption('sample.txt');
    await page.locator('#desk-policy').selectOption('custom');
    const labels = await page.locator('[data-scope-label]').allTextContents();
    assert.equal(labels.length,26);
    for (const label of ['人名','公司','身份证','港澳台证件','社保公积金']) assert.ok(labels.includes(label));
    await page.locator('#desk-scope-all').uncheck();
    assert.equal(await page.locator('[data-scope-cat]:checked').count(),0);
    await page.locator('[data-scope-group="identity"]').check();
    assert.equal(await page.locator('[data-scope-cat]:checked').count(),10);
    await page.screenshot({path:path.join(artifacts,'settings.png')});
    await page.locator('#desk-settings-close').click();
    await page.locator('#desk-analyze').click();
    await page.locator('#desk-src').getByText('😀 原告', {exact:false}).waitFor();
    assert.equal(await page.locator('#desk-settings').isVisible(),false);

    // A real Selection/Range followed by pointerup drives the production picker.
    await page.evaluate(() => {
      const node = document.getElementById('desk-src').firstChild;
      const start = node.textContent.indexOf('陈思远');
      const range = document.createRange(); range.setStart(node,start); range.setEnd(node,start+3);
      const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
      document.getElementById('desk-src').dispatchEvent(new MouseEvent('mouseup',{bubbles:true}));
    });
    await page.locator('#desk-picker [data-label="PERSON"]').click();
    assert.equal(await page.locator('#desk-src mark').count(),1);
    await page.locator('#desk-match-terms').click();
    assert.equal(await page.locator('#desk-src mark').count(),3);
    await page.locator('#desk-match-terms').click();
    assert.equal(await page.locator('#desk-src mark').count(),3);
    await page.locator('#desk-src mark').nth(1).click();
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-match-terms').click();
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-analyze').click();
    await page.waitForFunction(() => !DESK.busy && DESK.src.length > 0);
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-pick-output').click();
    assert.equal(await page.locator('#desk-export').isEnabled(),false);
    await page.locator('#desk-settings-toggle').click();
    await page.locator('input[name="desk-mode"][value="asterisk"]').check();
    await page.locator('#desk-settings-close').click();
    await page.locator('#desk-apply').click();
    await page.waitForFunction(() => document.getElementById('desk-out').dataset.has === '1');
    const masked = await page.locator('#desk-out').textContent();
    assert.ok(masked.startsWith('😀 原告：星河测试有限公司。'),masked);
    assert.equal((masked.match(/陈思远/g)||[]).length,1,'only the explicitly excluded occurrence remains');
    assert.ok(!masked.includes('陈\n思远'));
    assert.equal(await page.locator('#desk-export').isEnabled(),true);
    await page.screenshot({path:path.join(artifacts,'review.png')});
    const offsets = await page.evaluate(() => {
      const original = [{start:2,end:5,label:'PERSON'}];
      const source = '😀 陈思远';
      const converted = deskSpansFromApi(source, original);
      return {selected:source.slice(converted[0].start,converted[0].end),
        roundtrip:deskSpansToApi(source,converted).map(s => [s.start,s.end])};
    });
    assert.deepEqual(offsets,{selected:'陈思远',roundtrip:[[2,5]]});

    // Explicit edits invalidate the old preview. Undo restores positions and rules.
    await page.locator('#desk-src mark').first().click();
    assert.equal(await page.locator('#desk-export').isEnabled(),false);
    await page.locator('#desk-undo').click();
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-settings-toggle').click();
    await page.locator('#desk-term-text').fill('Ann');
    await page.locator('#desk-term-label').selectOption('PERSON');
    await page.locator('#desk-term-form button').click();
    await page.locator('#desk-settings-close').click();
    await page.locator('#desk-match-terms').click();
    assert.equal(await page.locator('#desk-src mark').count(),4);
    assert.equal(await page.locator('#desk-src mark').filter({hasText:'Joanne'}).count(),0);
    await page.locator('#desk-settings-toggle').click();
    await page.locator('#desk-term-list .desk-term').filter({hasText:'Ann'}).getByRole('button').click();
    await page.locator('#desk-settings-close').click();
    assert.equal(await page.locator('#desk-src mark').count(),2);
    for (const size of [{width:860,height:580},{width:1440,height:900}]) {
      await page.setViewportSize(size);
      const geometry = await page.evaluate(() => {
        const src = document.getElementById('desk-src');
        const main = document.getElementById('sec-desk');
        return {height:src.clientHeight,view:innerHeight,overflow:main.scrollWidth-main.clientWidth};
      });
      assert.ok(geometry.height / geometry.view > .60,JSON.stringify(geometry));
      assert.ok(geometry.overflow <= 1,JSON.stringify(geometry));
      await page.screenshot({path:path.join(artifacts,`review-${size.width}.png`)});
    }
    // Leaving and returning to the workbench keeps current manual work.
    await page.locator('[data-sec="dashboard"]').click();
    await page.locator('[data-sec="desk"]').click();
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-settings-toggle').click();
    await page.locator('#desk-policy').selectOption('strict');
    await page.locator('#desk-settings-close').click();
    assert.equal(await page.locator('#desk-apply').isEnabled(),false);
    await page.locator('#desk-analyze').click();
    await page.waitForFunction(() => !DESK.busy && !DESK.scopeDirty);
    assert.equal(await page.locator('#desk-src mark').count(),2);
    await page.locator('#desk-settings-toggle').click();
    await page.locator('[data-desk-route="standalone"]').click();
    assert.equal(await page.locator('#desk-term-count').textContent(),'0');
    assert.equal(await page.locator('#desk-src mark').count(),0);
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({ok:true,artifacts,checks:'layout, 26 category labels, select all/groups, manual selection, full-text round, exclusion, reanalysis, real apply endpoint, Unicode offsets, stale preview, undo, source reset'}));
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e); process.exitCode = 1; });
