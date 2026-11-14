'use strict';
const ruleCatalogs = new Map();
function placeRules(view){
  const editor=document.getElementById('rulesets-editor');
  document.getElementById(view==='automation'?'automation-rules-slot':'source-rules-slot').append(editor);
  if(view==='automation'&&location.hash==='#automation/rules')document.getElementById('automation-rules').hidden=false;
  let scope=document.getElementById('automation-rules-save-scope');
  if(!scope){scope=document.createElement('p');scope.id='automation-rules-save-scope';scope.className='scope';scope.textContent='保存包含此来源的完整配置，以及设置页尚未保存的字段';editor.prepend(scope);}
  scope.hidden=view!=='automation';
  document.getElementById('rules-save').textContent=view==='automation'?'保存此来源完整配置…':'保存此来源规则…';
}
async function discoverRules(generation=sourceGeneration){
  const id=selected();
  if(!sourceInfo()?.capabilities.sql){$('rules-discovery').textContent='此来源未提供 SQL 规则配置。';return;}
  $('rules-discovery').textContent='正在读取规则目录…';
  try{
    const data=await api(sourcePath(id)+'/rulesets');
    if(generation!==sourceGeneration||id!==selected())return;
    const invalid=[...$('rule-list').querySelectorAll('input')].filter(input=>!input.validity.valid).map(input=>({id:input.id,value:input.value}));
    ruleCatalogs.set(id,data);renderRuleSummary();
    for(const field of invalid)if($(field.id))$(field.id).value=field.value;
  }catch(error){if(generation===sourceGeneration&&id===selected()){$('rules-discovery').textContent='目录读取失败：'+error.message+'；现有编辑已保留，可重试。';}}
}
function writeRules(rules,render=false){
  $('filter-rulesets').value=JSON.stringify(rules,null,2);markQueryChanged();
  if(render)renderRuleSummary();
}
function renderRules(){
  const list=$('rule-list'),summary=$('rule-summary');list.replaceChildren();summary.replaceChildren();
  const supported=!!sourceInfo()?.capabilities.sql;
  $('rules-refresh').hidden=!supported;$('rules-save').hidden=!sourceInfo()?.capabilities.configure;
  if(!supported){$('rules-discovery').textContent='此来源未提供 SQL 规则配置。';return;}
  const catalog=ruleCatalogs.get(selected());
  if(catalog){
    const problems=(catalog.directory_details||[]).filter(entry=>entry.error).map(entry=>entry.path+'：'+entry.error);
    $('rules-discovery').textContent='发现目录：'+catalog.directories.join('；')+([...problems,...(catalog.warnings||[])].length?' · '+[...problems,...(catalog.warnings||[])].join('；'):'');
    const details=el('details');details.append(el('summary','目录状态与发现限制'),el('pre',JSON.stringify({directories:catalog.directory_details,omitted:catalog.omitted,limits:catalog.limits},null,2)));summary.append(details);
  }
  let rules;try{rules=ruleValues();}catch(error){summary.append(el('p',error.message+'；请修正下方 JSON，编辑内容已保留。','scope'));return;}
  if(rules.some(rule=>!rule||typeof rule!=='object'||Array.isArray(rule)||typeof rule.file!=='string'||(rule.params!=null&&(typeof rule.params!=='object'||Array.isArray(rule.params))))){summary.append(el('p','注册表中有无法用表单展示的条目，请在高级 JSON 中修正；原内容已保留。','scope'));return;}
  const available=catalog?.rules||[],enabled=rules.filter(rule=>rule.enabled!==false).length;
  summary.append(el('p',`已启用 ${enabled} 条规则${enabled?'':' · 当前不采集，也不推进断点'}`,'scope'));
  const metadata=rule=>available.find(item=>item.file===rule.file||item.registered_files?.includes(rule.file));
  const displayed=rules.map((rule,index)=>({rule,index,info:metadata(rule)}));
  for(const info of available)if(!rules.some(rule=>metadata(rule)===info))displayed.push({rule:{file:info.file,enabled:false},index:-1,info});
  for(const {rule,index,info}of displayed){
    const card=el('article',undefined,'rule-card'),label=el('label',undefined,'rule-toggle'),toggle=el('input');toggle.type='checkbox';toggle.checked=index>=0&&rule.enabled!==false;toggle.setAttribute('aria-label','启用 '+rule.file);label.append(toggle,el('strong',rule.file.split(/[\\/]/).pop()));card.append(label,el('small',rule.file,'muted path'));
    toggle.addEventListener('change',()=>{const entries=ruleValues();if(index<0)entries.push({file:rule.file,enabled:true});else entries[index].enabled=toggle.checked;writeRules(entries,true);});
    if(info?.error||info?.exists===false)card.append(el('p',info.error||'文件不存在；可取消启用或修正注册路径。','error'));
    if(!info&&catalog)card.append(el('p','此条目不在已发现列表中，保留原配置；可刷新目录或检查路径。','scope'));
    const params=rule.params||{},keys=[...new Set([...(info?.parameters||[]),...Object.keys(params)])];
    if(index>=0&&(keys.length||info?.recent_count||Object.hasOwn(rule,'recent_count'))){
      const fields=el('div',undefined,'rule-params');card.append(fields);
      keys.forEach((key,parameterIndex)=>{
        const value=params[key],numeric=typeof value==='number',field=el('label',key+(numeric?'（数字）':'')),input=el('input');input.id=`rule-param-${index}-${parameterIndex}`;input.dataset.ruleParameter=key;input.type=numeric?'number':'text';if(numeric)input.step='any';input.value=value??'';input.placeholder=value===null?'空值 null':'填写 SQL 命名参数';input.disabled=!toggle.checked;input.required=toggle.checked&&value===undefined;field.append(input);fields.append(field);
        input.addEventListener('input',()=>{
          input.setCustomValidity(numeric&&(!input.value.trim()||!Number.isFinite(Number(input.value)))?'请填写有效数字':'');
          if(!input.validity.valid)return;
          const entries=ruleValues();entries[index].params??={};entries[index].params[key]=numeric?Number(input.value):value===null&&!input.value?null:input.value;writeRules(entries);
        });
      });
      if(info?.recent_count||Object.hasOwn(rule,'recent_count')){
        const field=el('label','前后上下文条数（0–1000）'),input=el('input');input.id='rule-recent-'+index;input.type='number';input.min=0;input.max=1000;input.step=1;input.value=rule.recent_count??0;input.disabled=!toggle.checked;input.required=true;field.append(input);fields.append(field);
        input.addEventListener('input',()=>{if(input.validity.valid){const entries=ruleValues();entries[index].recent_count=Number(input.value);writeRules(entries);}});
      }
    }
    if(info?.sql){const details=el('details');details.append(el('summary','查看 SQL 正文'),el('pre',info.sql));card.append(details);}
    list.append(card);
  }
  if(!displayed.length)list.append(el('p','未发现 SQL 文件。将 .sql 文件放入上方目录后重新发现。','empty'));
}
function validateRuleInputs(){
  for(const input of $('rule-list').querySelectorAll('input'))if(!input.checkValidity()){
    location.hash='automation/rules';input.reportValidity();throw Error('请修正标出的规则参数，再预览或保存。');
  }
}
