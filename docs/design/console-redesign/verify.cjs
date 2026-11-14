/* Prototype-only checks. Uses an existing Playwright installation; no app dependencies. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const executablePath = process.env.CHROME_BIN || [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'
  ].find(p => fs.existsSync(p));
  const browser = await chromium.launch({headless:true, executablePath});
  try {
    const context = await browser.newContext({viewport:{width:1600,height:1000}, timezoneId:'Asia/Shanghai'});
    const page = await context.newPage();
    const errors = [], network = [];
    page.on('pageerror', error => errors.push(error.message));
    await context.route('**/*', route => {
      if (/^https?:/.test(route.request().url())) {network.push(route.request().url()); return route.abort();}
      return route.continue();
    });
    await page.goto(pathToFileURL(path.join(__dirname,'index.html')).href);
    assert.equal(await page.locator('#message-list .message-row').count(),8);
    assert.equal(await page.locator('#message-inspector h4').textContent(),'产品协作群');
    const ids = await page.locator('[id]').evaluateAll(nodes=>nodes.map(n=>n.id));
    assert.equal(new Set(ids).size,ids.length,'duplicate IDs');
    assert.equal(await page.locator('script[src],img[src],link[href]:not([href^="data:"])').count(),0,'external assets');
    const projectReadme=fs.readFileSync(path.resolve(__dirname,'../../..','README.md'),'utf8');
    const productName=projectReadme.match(/<h1[^>]*>([^<]+)<\/h1>/)[1];
    assert.equal(await page.locator('.brand strong').textContent(),productName);
    assert.ok((await page.title()).startsWith(productName));
    const readmeLogo=fs.readFileSync(path.resolve(__dirname,'../../..','assets/readme/logo.svg'),'utf8');
    const expectedPaths=[...readmeLogo.matchAll(/<path d="([^"]+)"/g)].map(match=>match[1]);
    assert.deepEqual(await page.locator('#brand-logo path').evaluateAll(nodes=>nodes.map(n=>n.getAttribute('d'))),expectedPaths);
    assert.equal(await page.locator('#brand-logo rect').getAttribute('width'),'128');
    assert.equal(await page.locator('#brand-logo rect').getAttribute('height'),'128');
    const previewRect=await page.locator('#preview-button').boundingBox();
    assert.ok(previewRect.y+previewRect.height<1000,'preview action below desktop viewport');

    const screenshots = process.argv.includes('--screenshots');
    const output = process.env.SCREENSHOT_DIR || path.join(__dirname,'previews');
    if (screenshots) {fs.mkdirSync(output,{recursive:true}); await page.screenshot({path:path.join(output,'desktop.png')});}

    await page.locator('#tab-filter').focus();
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.locator('#tab-browse').getAttribute('aria-selected'),'true');
    await page.keyboard.press('Home');
    assert.equal(await page.locator('#tab-filter').getAttribute('aria-selected'),'true');

    await page.locator('#range-start').fill('2026-10-01T00:00');
    await page.locator('#preview-button').click();
    assert.match(await page.locator('#filter-error').textContent(),/7 天/);
    // Historical replay has no seven-day cap, unlike preview.
    await page.locator('[data-action="replay"]').click();
    assert.equal(await page.locator('#dialog').evaluate(n=>n.open),true);
    assert.match(await page.locator('#dialog-body').textContent(),/不受 7 天/);
    await page.keyboard.press('Escape');
    await page.locator('[data-range="today"]').click();
    await page.locator('#preview-button').click();
    await page.locator('#cancel-preview').click();
    await page.waitForTimeout(1000);
    assert.match(await page.locator('#preview-status').textContent(),/已取消/);
    assert.equal(await page.locator('#results-panel').getAttribute('aria-busy'),'false');
    await page.locator('#preview-button').click();
    await page.waitForFunction(()=>document.getElementById('preview-status').textContent.includes('演示预览完成'));

    await page.locator('[data-action="rules-edit"]').click();
    await page.locator('#rules-editor').fill('invalid JSON');
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.locator('#dialog').evaluate(n=>n.open),true);
    assert.ok((await page.locator('#rules-error').textContent()).length);
    await page.locator('#rules-editor').fill(JSON.stringify([{file:'../rulesets/context.sql',enabled:true,recent_count:5}]));
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.locator('#rule-list .rule').count(),1);
    assert.match(await page.locator('#filter-state').textContent(),/未保存/);

    // The same automation forms are reachable from one navigation entry and old shortcuts.
    assert.equal(await page.locator('.nav-item[data-view="automation"]').count(),1);
    await page.evaluate(()=>openSettings('service'));
    assert.equal(await page.locator('#view-automation').isVisible(),true);
    assert.equal(await page.locator('#view-automation [role="tab"]').count(),2);
    await page.locator('[data-automation-task="delivery"]').uncheck();
    await page.evaluate(()=>openSettings('delivery'));
    assert.equal(await page.locator('#channel-auto-delivery').isChecked(),false);
    await page.locator('#channel-auto-delivery').check();
    await page.evaluate(()=>openSettings('service'));
    assert.equal(await page.locator('[data-automation-task="delivery"]').isChecked(),true);
    assert.match(await page.locator('#automation-task-fields').textContent(),/处理正式增量.*投递正式待办.*应用操作回写.*监听渠道操作.*检查到期提醒/s);
    await page.locator('[data-background-source="wechat-main"]').uncheck();
    await page.evaluate(()=>{switchSource('qq-main');switchSource('wechat-main');});
    assert.equal(await page.locator('[data-background-source="wechat-main"]').isChecked(),false);
    assert.equal(await page.evaluate(()=>currentSource().ui.schedule),false);
    await page.locator('[data-background-source="wechat-main"]').check();
    await page.locator('[data-automation-interval="feedback"]').fill('15');
    await page.locator('[data-automation-interval="feedback"]').dispatchEvent('change');
    assert.equal(await page.evaluate(()=>automationTasks.find(task=>task.id==='feedback').interval),15);

    // Discover files from the source's catalog; a name unknown to the UI stays disabled.
    await page.locator('[data-action="automation-source-rules"][data-source-id="wechat-main"]').click();
    assert.equal(await page.locator('#source-sql-settings').isVisible(),true);
    assert.equal(await page.locator('#sql-catalog .setting-row').filter({hasText:'project-notes.sql'}).locator('input[type="checkbox"]').isChecked(),false);
    await page.evaluate(()=>{
      openSettings('source');
      currentSource().sqlFiles.push({file:'../rulesets/future-source-rule.sql'});
    });
    await page.locator('[data-action="sql-scan"]').click();
    assert.match(await page.locator('#sql-directory').textContent(),/\.\.\/rulesets\/\*\.sql/);
    const discovered=page.locator('#sql-catalog .setting-row').filter({hasText:'future-source-rule.sql'});
    assert.equal(await discovered.locator('input[type="checkbox"]').isChecked(),false);
    await discovered.locator('[data-action="sql-params"]').click();
    await page.locator('#sql-params-json').fill('[]');
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.locator('#dialog').evaluate(node=>node.open),true);
    assert.match(await page.locator('#sql-params-error').textContent(),/JSON 对象/);
    await page.locator('#sql-recent-count').fill('12');
    await page.locator('#sql-params-json').fill('{"project":"release","minimum_count":3}');
    await page.locator('#dialog-confirm').click();
    await discovered.locator('input[type="checkbox"]').check();
    assert.deepEqual(await page.evaluate(()=>JSON.parse(currentSource().ui.savedRulesText).find(rule=>rule.file.includes('future-source-rule'))),{file:'../rulesets/future-source-rule.sql',enabled:true,params:{project:'release',minimum_count:3},recent_count:12});
    await page.evaluate(()=>switchSource('qq-main'));
    assert.equal(await page.locator('#sql-catalog').getByText('future-source-rule.sql',{exact:true}).count(),0);
    await page.evaluate(()=>switchSource('wechat-main'));
    assert.equal(await discovered.locator('input[type="checkbox"]').isChecked(),true);
    await page.evaluate(()=>{navigate('collect');activateTab(document.getElementById('tab-filter'));});

    await page.locator('#result-search').fill('没有这样的消息');
    assert.equal(await page.locator('#message-list .message-row').count(),0);
    assert.match(await page.locator('#message-list').textContent(),/仍保留/);
    await page.locator('#result-search').fill('');
    await page.locator('#attachment-filter').selectOption('missing');
    assert.equal(await page.locator('#message-list .message-row').count(),1);
    await page.locator('#message-inspector [data-action="attachment"]').click();
    assert.match(await page.locator('#dialog-body').textContent(),/不能作为原图/);
    await page.keyboard.press('Escape');
    await page.locator('#attachment-filter').selectOption('all');
    await page.locator('#source-picker').selectOption('qq-main');
    assert.equal(await page.locator('#message-list .message-row').count(),4);
    assert.equal(await page.locator('#source-name').textContent(),'QQ');
    await page.locator('#source-picker').selectOption('wechat-main');
    assert.equal(await page.locator('#rule-list .rule').count(),1,'source draft leaked or was lost');
    await page.locator('#source-picker').selectOption('external-example');
    assert.equal(await page.locator('#tab-filter').isVisible(),false);
    assert.equal(await page.locator('[data-action="collect-run"]').isVisible(),false);
    assert.equal(await page.locator('#schedule-button').isVisible(),false);
    await page.locator('[data-action="browse"]').click();
    assert.equal(await page.locator('#browse-output .message-row').count(),2);
    // A new kind and a second instance of the same kind must work without a UI branch.
    await page.evaluate(()=>{
      catalog.sources.push({id:'custom-feed',kind:'new-provider',name:'订阅信息源',mark:'订',count:1,health:'已发布',snapshot:'11-14 10:00:00',capabilities:{query:true,configure:true},fields:[{key:'feed_name',label:'订阅名称',type:'string'}],values:{feed_name:'项目动态'},readonly:{'来源':'接入描述'},messages:[{group:'订阅动态',sender:'发布者',time:'10:00:00',text:'新的统一消息',tags:[],conversation:'new-conversation',rule:'来源规则'}],savedRules:[],boundary:'仅提供来源声明的查询能力'});
      renderSourceUI();
    });
    await page.locator('#source-picker').selectOption('custom-feed');
    assert.equal(await page.locator('#source-name').textContent(),'订阅信息源');
    assert.equal(await page.locator('#preview-sql-rules').isVisible(),false);
    assert.equal(await page.locator('[data-action="replay"]').isVisible(),false);
    await page.evaluate(()=>openSettings('source'));
    assert.equal(await page.locator('[data-source-field="feed_name"]').inputValue(),'项目动态');
    await page.locator('[data-source-field="feed_name"]').fill('已编辑订阅');
    await page.locator('[data-source-field="feed_name"]').dispatchEvent('change');
    await page.evaluate(()=>{
      const second=structuredClone(catalog.sources.find(source=>source.id==='custom-feed'));
      second.id='custom-feed-2';second.name='同类型的另一账号';second.values.feed_name='另一个订阅';delete second.ui;
      catalog.sources.push(second);renderSourceUI();
    });
    await page.locator('[aria-label="设置信息源"]').selectOption('custom-feed-2');
    assert.equal(await page.locator('[data-source-field="feed_name"]').inputValue(),'另一个订阅');
    await page.locator('[aria-label="设置信息源"]').selectOption('custom-feed');
    assert.equal(await page.locator('[data-source-field="feed_name"]').inputValue(),'已编辑订阅');
    await page.evaluate(()=>{switchSource('wechat-main');navigate('collect');activateTab(document.getElementById('tab-filter'));});

    await page.locator('.nav-item[data-view="process"]').click();
    await page.locator('[data-process-mode="debug"]').click();
    assert.equal(await page.locator('#process-dates').isVisible(),true);
    await page.locator('#process-run').click();
    assert.match(await page.locator('#dialog-body').textContent(),/不写入正式待办/);
    assert.match(await page.locator('#dialog-body').textContent(),/发送到已配置模型/);
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.locator('#tab-tasks').getAttribute('aria-selected'),'true');
    await page.locator('#tab-tools').click();
    assert.equal(await page.locator('#workflow-events>.workflow-event').count(),10);
    await page.locator('#workflow-filter').selectOption('tool');
    assert.equal(await page.locator('#workflow-events>.workflow-event').count(),3);
    await page.locator('#workflow-events>.workflow-event').first().locator('summary').first().click();
    assert.match(await page.locator('#workflow-events>.workflow-event').first().textContent(),/工具参数/);
    assert.match(await page.locator('#workflow-events>.workflow-event').first().textContent(),/工具返回/);
    await page.locator('#workflow-filter').selectOption('all');
    await page.locator('#workflow-run-picker').selectOption('pi-demo-running');
    assert.match(await page.locator('#workflow-wait').textContent(),/等待模型/);
    assert.match(await page.locator('#workflow-events').textContent(),/正文未记录/);
    await page.locator('#workflow-run-picker').selectOption('pi-demo-failed');
    assert.match(await page.locator('#workflow-events').textContent(),/压缩完成/);
    assert.match(await page.locator('#workflow-events').textContent(),/原生失败重试/);
    await page.locator('#workflow-search').fill('不存在的事件');
    assert.equal(await page.locator('#workflow-events>.workflow-event').count(),0);
    await page.locator('#workflow-search').fill('');
    await page.evaluate(()=>{
      workflowRuns[0].events.push({seq:11,kind:'tool',title:'future_tool',time:'10:28:13',input:{item:'demo'},output:{ok:true}});
    });
    await page.locator('#workflow-run-picker').selectOption('pi-demo-01');
    await page.locator('#workflow-search').fill('future_tool');
    assert.equal(await page.locator('#workflow-events>.workflow-event').count(),1,'new tool needed a UI branch');
    await page.locator('#workflow-search').fill('');

    await page.evaluate(()=>{
      catalog.channelTypes.push({kind:'custom-notifier',name:'新通知渠道',capabilities:{test:true,files:false,feedback:false},fields:[{key:'destination_env',label:'目标环境变量名',type:'env'}]});
      openSettings('delivery');
    });
    await page.locator('[data-action="channel-add"]').click();
    await page.locator('#new-channel-id').fill('custom-notification');
    await page.locator('#new-channel-kind').selectOption('custom-notifier');
    await page.locator('#new-channel-name').fill('项目订阅通知');
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.locator('[data-target-id="custom-notification"][data-target-field="destination_env"]').count(),1);
    assert.equal(await page.locator('[data-target-enabled="custom-notification"]').isChecked(),false);
    await page.locator('[data-action="channel-test"][data-target-id="custom-notification"]').click();
    assert.match(await page.locator('#dialog-body').textContent(),/项目订阅通知/);
    await page.keyboard.press('Escape');
    await page.evaluate(()=>{catalog.targets.push({id:'unavailable-target',kind:'unavailable-type',name:'描述不可用的目标',enabled:false,values:{untouched:'keep'}});renderChannels();});
    assert.match(await page.locator('#channel-cards').textContent(),/暂不提供编辑/);
    assert.equal(await page.evaluate(()=>catalog.targets.find(target=>target.id==='unavailable-target').values.untouched),'keep');

    await page.locator('.nav-item[data-view="delivery"]').click();
    await page.locator('[data-feedback="complete"]').first().click();
    assert.match(await page.locator('#delivery-task-state-0').textContent(),/等待处理层回写/);
    await page.locator('#tab-deliveries').click();
    await page.locator('[data-action="delivery-retry"]').click();
    assert.match(await page.locator('#dialog-body').textContent(),/核实没有收到/);
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(()=>catalog.deliveries[0].status),'unknown','cancel changed delivery state');
    await page.locator('[data-action="delivery-received"]').click();
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.evaluate(()=>catalog.deliveries[0].status),'sent');
    assert.equal(await page.locator('[data-action="delivery-received"]').count(),0);
    assert.equal(await page.locator('.nav-count').textContent(),'');
    await page.evaluate(()=>{catalog.deliveries[0].status='unknown';renderDeliveries();renderOverview();});
    await page.locator('[data-action="delivery-retry"]').click();
    await page.locator('#dialog-confirm').click();
    assert.equal(await page.evaluate(()=>catalog.deliveries[0].status),'queued');
    assert.match(await page.locator('#delivery-rows tr').first().textContent(),/等待重试（演示）/);
    assert.doesNotMatch(await page.locator('#delivery-rows tr').first().textContent(),/已确认送达/);

    await page.locator('.nav-item[data-view="history"]').click();
    await page.locator('#history-filter').selectOption('failed');
    assert.equal(await page.locator('#history-rows tr').count(),1);
    await page.locator('#history-rows [data-job]').click();
    assert.match(await page.locator('#dialog-body').textContent(),/未推进断点/);
    await page.keyboard.press('Escape');

    const views=['overview','collect','process','delivery','automation','history','settings'];
    for (const width of [1600,1440,1280,1024,800,390,320]) {
      await page.setViewportSize({width,height:900});
      for (const view of views) {
        await page.evaluate(v=>navigate(v),view);
        if(view==='process')await page.evaluate(()=>activateTab(document.getElementById('tab-tools')));
        const sizes=await page.evaluate(()=>({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth}));
        assert.ok(sizes.scroll<=sizes.client,`${view} overflows at ${width}: ${JSON.stringify(sizes)}`);
        if(view==='settings')for(const tab of ['source','model','delivery','service']){
          await page.evaluate(tab=>openSettings(tab),tab);
          assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),`settings ${tab} overflows at ${width}`);
        }
        if(view==='automation')for(const tab of ['service','delivery']){
          await page.evaluate(tab=>openSettings(tab),tab);
          assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),`automation ${tab} overflows at ${width}`);
        }
      }
    }
    await page.setViewportSize({width:390,height:844});
    await page.evaluate(()=>navigate('collect'));
    assert.equal(await page.locator('.sidebar').evaluate(n=>n.inert),true);
    await page.locator('[data-action="menu"]').click();
    assert.equal(await page.locator('.shell').evaluate(n=>n.inert),true);
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.shell').evaluate(n=>n.inert),false);
    await page.locator('#message-list .message-row').first().click();
    assert.equal(await page.locator('#dialog').evaluate(n=>n.open),true);
    await page.keyboard.press('Escape');
    if (screenshots) {
      await page.reload();
      await page.screenshot({path:path.join(output,'mobile.png'),fullPage:true});
      await page.setViewportSize({width:1600,height:1000});
      await page.evaluate(()=>{navigate('process');activateTab(document.getElementById('tab-tools'));});
      await page.locator('#workflow-filter').selectOption('tool');
      await page.locator('#workflow-events>.workflow-event').filter({has:page.locator('summary strong',{hasText:/^read_attachment$/})}).locator('summary').first().click();
      await page.locator('.workflow-layout').screenshot({path:path.join(output,'pi-workflow.png')});
      await page.evaluate(()=>openSettings('service'));
      await page.screenshot({path:path.join(output,'automation.png'),fullPage:true});
      await page.evaluate(()=>openSettings('source'));
      await page.evaluate(()=>window.scrollTo(0,document.getElementById('source-sql-settings').getBoundingClientRect().top+scrollY-80));
      await page.locator('#source-sql-settings').screenshot({path:path.join(output,'sql-rules.png')});
    }
    await page.goto(pathToFileURL(path.join(__dirname,'index.html')).href+'#automation');
    assert.equal(await page.locator('#view-automation').isVisible(),true);
    assert.deepEqual(errors,[],'browser JavaScript errors');
    assert.deepEqual(network,[],'unexpected network requests');
    console.log('PASS: branding, extensible catalogs, SQL discovery and parameters, unified automation, safe delivery simulation, complete Pi workflow; seven views and settings at seven widths; no network or browser errors');
    if (screenshots) console.log('Previews:',output);
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
