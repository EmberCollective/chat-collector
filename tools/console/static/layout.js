'use strict';
const viewLabels = {overview:'运行概览',sources:'信息源调试',processor:'待办处理',delivery:'行动与投递',history:'运行记录',settings:'设置'};
function activatePanel(id, focus=false) {
  const tab=document.querySelector(`[data-panel="${id}"]`); if(!tab)return;
  const tabs=tab.parentElement;
  for(const other of tabs.querySelectorAll('[data-panel]')){
    const active=tab===other;other.setAttribute('aria-selected',String(active));other.tabIndex=active?0:-1;
    document.getElementById(other.dataset.panel).hidden=!active;
  }
  if(focus)tab.focus();
  if(id==='query'||id==='messages'){
    document.getElementById('preview-output').hidden=id!=='query';
    document.getElementById('message-output').hidden=id!=='messages';
    document.querySelector('.filter-footer').hidden=id!=='query';
    document.getElementById('result-scope').textContent=id==='query'?'现有快照 · 只读预览':'已采集消息 · 最新修订';
  }
  if(id==='pi-workflow'&&document.getElementById('workflow-matches')&&typeof refreshWorkflow==='function')refreshWorkflow().catch(e=>notice(e.message,true));
}
function setMenu(open){
  document.body.classList.toggle('menu-open',open);
  document.getElementById('menu-toggle').setAttribute('aria-expanded',String(open));
  document.getElementById('nav-shade').hidden=!open;
  document.getElementById('app-body').inert=open;
  document.getElementById('sidebar').inert=!open&&matchMedia('(max-width:800px)').matches;
  if(open)document.querySelector('#sidebar [aria-current=page]').focus();
}
function showView(){
  const [candidate,panel]=location.hash.slice(1).split('/'),view=candidate==='pi-workflow'?'processor':viewLabels[candidate]?candidate:'overview';
  for(const section of document.querySelectorAll('main>.view'))section.hidden=section.id!=='view-'+view;
  for(const link of document.querySelectorAll('[data-view]')){
    if(link.dataset.view===view)link.setAttribute('aria-current','page');else link.removeAttribute('aria-current');
  }
  document.getElementById('view-label').textContent=viewLabels[view];
  document.title='拾序 · Loose Ends · '+viewLabels[view];
  if(view==='settings'&&panel)activatePanel('settings-'+panel);
  if(candidate==='pi-workflow')activatePanel('pi-workflow');
  setMenu(false);
  document.getElementById('main').focus({preventScroll:true});
}
document.querySelectorAll('[data-panel]').forEach(tab=>{
  tab.addEventListener('click',()=>activatePanel(tab.dataset.panel));
  tab.addEventListener('keydown',event=>{
    const tabs=[...tab.parentElement.querySelectorAll('[data-panel]')];let index=tabs.indexOf(tab);
    if(event.key==='ArrowRight')index=(index+1)%tabs.length;
    else if(event.key==='ArrowLeft')index=(index+tabs.length-1)%tabs.length;
    else if(event.key==='Home')index=0;else if(event.key==='End')index=tabs.length-1;else return;
    event.preventDefault();activatePanel(tabs[index].dataset.panel,true);
  });
});
document.getElementById('menu-toggle').addEventListener('click',()=>setMenu(true));
document.getElementById('nav-shade').addEventListener('click',()=>{setMenu(false);document.getElementById('menu-toggle').focus();});
document.addEventListener('keydown',event=>{if(event.key==='Escape'&&document.body.classList.contains('menu-open')){setMenu(false);document.getElementById('menu-toggle').focus();}});
matchMedia('(max-width:800px)').addEventListener('change',()=>setMenu(false));
window.addEventListener('hashchange',showView);
showView();
document.querySelectorAll('#sidebar a').forEach(link=>link.addEventListener('click',()=>{setMenu(false);document.getElementById('main').focus({preventScroll:true});}));
