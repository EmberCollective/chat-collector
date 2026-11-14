const assert=require('node:assert/strict');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const base=path.resolve(__dirname,'../../..');
const evidence={};
(async()=>{
 const browser=await chromium.launch({executablePath:process.env.CHROME_PATH||'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1600,height:1000}}),errors=[],network=[],downloads=[];
  page.on('pageerror',error=>errors.push(error.message));
  page.on('download',download=>downloads.push(download.suggestedFilename()));
  await page.route('**/*',route=>{if(/^https?:/.test(route.request().url())){network.push(route.request().url());return route.abort();}return route.continue();});
  await page.addInitScript(()=>{
   window.__diskAttempts=[];
   const deny=name=>(...args)=>{window.__diskAttempts.push(name);throw Error('Unexpected browser persistence: '+name);};
   Storage.prototype.setItem=deny('Storage.setItem');
   if(window.indexedDB)window.indexedDB.open=deny('indexedDB.open');
   for(const name of ['showSaveFilePicker','showDirectoryPicker','showOpenFilePicker'])if(name in window)window[name]=deny(name);
  });
  await page.goto(pathToFileURL(path.join(base,'docs/design/console-redesign/index.html')).href+'#automation');
  assert.equal(await page.locator('.nav-item').count(),7);
  assert.equal(await page.locator('.nav-item[aria-current="page"]').count(),1);
  assert.equal(await page.locator('.nav-item[aria-current="page"]').getAttribute('data-view'),'automation');
  const sourceCount=await page.evaluate(()=>catalog.sources.filter(source=>source.capabilities.sql).length);
  const sqlEvidence=[];
  for(const id of await page.evaluate(()=>catalog.sources.filter(source=>source.capabilities.sql).map(source=>source.id))){
   await page.evaluate(id=>{switchSource(id);openSettings('source');const source=currentSource();source.sqlFiles.push({file:'../custom/weekly-summary-v2.sql',enabled:true});},id);
   await page.locator('[data-action="sql-scan"]').click();
   const row=page.locator('#sql-catalog .setting-row').filter({hasText:'weekly-summary-v2.sql'});
   assert.equal(await row.locator('input').isChecked(),false,'discovery must ignore an enabled hint');
   await row.locator('button').click();
   await page.locator('#sql-recent-count').fill('17');
   await page.locator('#sql-params-json').fill('{"new_parameter":{"nested":true},"group_ids":["ops","product"]}');
   await page.locator('#dialog-confirm').click();
   const saved=await page.evaluate(()=>JSON.parse(currentSource().ui.savedRulesText).find(rule=>rule.file.endsWith('weekly-summary-v2.sql')));
   assert.equal(saved.enabled,false);assert.equal(saved.recent_count,17);assert.deepEqual(saved.params,{new_parameter:{nested:true},group_ids:['ops','product']});
   sqlEvidence.push({source:id,file:saved.file,enabled:saved.enabled,recent_count:saved.recent_count});
  }
  assert.equal(sqlEvidence.length,sourceCount);
  await page.locator('#source-sql-settings summary').click();
  await page.locator('#saved-rules').fill('{broken draft');
  await page.locator('[data-action="sql-scan"]').click();
  assert.equal(await page.locator('#saved-rules').inputValue(),'{broken draft');
  assert.match(await page.locator('#sql-catalog').textContent(),/当前编辑保留/);
  await page.evaluate(()=>{currentSource().ui.savedRulesText=JSON.stringify(currentSource().savedRules);renderSourceUI();openSettings('service');});
  for(const action of ['service-save','startup']){
   await page.locator(`[data-action="${action}"]`).click();
   assert.match(await page.locator('#dialog-foot').textContent(),/仅演示，不执行真实操作/);
   await page.locator('#dialog-confirm').click();
  }
  await page.evaluate(()=>{navigate('collect');activateTab(document.getElementById('tab-filter'));});
  await page.locator('[data-action="collect-run"]').click();await page.locator('#dialog-confirm').click();
  await page.evaluate(()=>openSettings('delivery'));
  await page.locator('[data-action="channel-test"]').first().click();await page.locator('#dialog-confirm').click();
  assert.deepEqual(await page.evaluate(()=>window.__diskAttempts),[]);
  evidence.console={navigation:7,sqlSources:sqlEvidence,malformedDraft:'preserved',demonstrationActions:['service-save','startup','collect-run','channel-test']};
  const parameterLayouts=[];
  for(const width of [320,390,1440,1600]){
   await page.setViewportSize({width,height:width<800?844:1000});
   await page.evaluate(()=>{switchSource('wechat-main');openSettings('source');});
   await page.locator('[data-action="sql-params"]').first().click();
   await page.locator('#sql-params-json').fill(JSON.stringify({arbitrary_parameter:'长参数正文用于验证编辑区内部滚动。'.repeat(500)}));
   const sizes=await page.evaluate(()=>{const dialog=document.getElementById('dialog'),input=document.getElementById('sql-params-json');return {width:innerWidth,page:document.documentElement.scrollWidth,dialog:dialog.clientWidth,content:dialog.scrollWidth,inputHeight:input.clientHeight,inputContent:input.scrollHeight};});
   assert.ok(sizes.page<=width+1&&sizes.content<=sizes.dialog+1,JSON.stringify(sizes));
   assert.ok(sizes.inputContent>sizes.inputHeight);
   await page.locator('#dialog-confirm').scrollIntoViewIfNeeded();
   const confirm=await page.locator('#dialog-confirm').boundingBox();
   assert.ok(confirm.x>=0&&confirm.x+confirm.width<=width&&confirm.y>=0&&confirm.y+confirm.height<=(width<800?844:1000),'parameter save is unreachable');
   if(width===390)await page.screenshot({path:path.join(base,'docs/design/console-redesign/previews/mobile-sql-parameters.png')});
   parameterLayouts.push(sizes);await page.keyboard.press('Escape');
  }
  evidence.parameterLayouts=parameterLayouts;

  await page.goto(pathToFileURL(path.join(base,'docs/design/overview-dashboard/index.html')).href);
  const keyboardEntries=['.problem-row button','#rate-chart [role="button"]','.run-ticks .failed','[data-service-problem="collect"]','[data-source-problem="work-chat"]'];
  for(let index=0;index<keyboardEntries.length;index++){
   const target=page.locator(keyboardEntries[index]).first();await target.focus();await page.keyboard.press(index%2?'Space':'Enter');
   assert.equal(await page.locator('#run-detail').isVisible(),true,keyboardEntries[index]);
   const text=await page.locator('#run-detail').textContent();
   for(const expected of ['操作','模块','触发方式','开始时间','耗时','本轮上下文','查看日志与原始记录'])assert.ok(text.includes(expected));
   await page.locator('#run-detail details summary').click();
   assert.match(await page.locator('#run-detail details').textContent(),/数据库释放/);
   const scope=await page.locator('#run-scope').textContent();
   await page.locator('#back-to-runs').focus();await page.keyboard.press('Enter');
   assert.equal(await page.locator('#run-scope').textContent(),scope);await page.keyboard.press('Escape');
  }
  evidence.keyboardEntries=keyboardEntries;
  await page.evaluate(()=>{const run=runs.find(run=>run.id==='delivery');run.context.large_context='上下文示例，保持当前来源边界。'.repeat(500);run.logs=Array.from({length:300},(_,index)=>`日志 ${index+1}：等待目标确认；未自动重发。`);});
  const layout=[];
  for(const width of [320,390,1440,1600]){
   await page.setViewportSize({width,height:width<800?844:1000});
   await page.locator('[data-service-problem="delivery"]').click();
   await page.locator('#run-detail details summary').click();
   const logBox=await page.locator('#run-detail details pre').first().evaluate(node=>({client:node.clientHeight,scroll:node.scrollHeight,wrap:getComputedStyle(node).whiteSpace}));
   assert.ok(logBox.scroll>logBox.client&&logBox.client<=240,'long logs should scroll internally');
   assert.equal(logBox.wrap,'pre-wrap');
   await page.locator('#verify-delivery').click();
   const result=await page.evaluate(()=>{const dialog=document.getElementById('run-dialog');return {width:innerWidth,page:document.documentElement.scrollWidth,dialog:dialog.clientWidth,content:dialog.scrollWidth};});
   assert.ok(result.page<=width+1&&result.content<=result.dialog+1,JSON.stringify(result));
   await page.locator('#delivery-checked').check();
   await page.locator('#confirm-delivery-retry').scrollIntoViewIfNeeded();
   const rect=await page.locator('#confirm-delivery-retry').boundingBox();
   assert.ok(rect.y>=0&&rect.y+rect.height<=844+(width>=800?156:0),'retry action is unreachable');
   if(width===390)await page.screenshot({path:path.join(base,'docs/design/overview-dashboard/previews/mobile-run-detail.png')});
   await page.locator('#close-dialog').scrollIntoViewIfNeeded();
   assert.equal(await page.locator('#close-dialog').isVisible(),true);
   layout.push({...result,logBox});await page.keyboard.press('Escape');
  }
  await page.locator('[data-service-problem="delivery"]').click();await page.locator('#verify-delivery').click();
  assert.equal(await page.locator('#confirm-delivery-retry').isDisabled(),true);
  await page.locator('#delivery-checked').check();await page.locator('#confirm-delivery-retry').click();
  assert.match(await page.locator('#run-detail').textContent(),/等待重试（演示）/);
  assert.equal(await page.locator('#confirm-delivery-retry').count(),0);
  assert.deepEqual(await page.evaluate(()=>window.__diskAttempts),[]);
  assert.deepEqual(errors,[]);assert.deepEqual(network,[]);assert.deepEqual(downloads,[]);
  evidence.layout=layout;evidence.sideEffects={http:network,browserPersistence:[],downloads,errors};
  console.log(JSON.stringify(evidence,null,2));
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
