'use strict';
// Synthetic API response used by browser checks; never served by the application.
module.exports=function overviewSnapshot(now=1794637926){
  const refs=[{run_id:'00000000000000000000000000000001',run_source:'console',source_id:'qq'}];
  const sources=[
    {id:'wechat',name:'微信',kind:'wechat',metrics_available:true,status:'waiting',last_success:now-24,emitted:947,duplicates:240,emitted_complete:true,success:36,failed:0,skipped:5,unknown:0,running:0,retrying:0,valid_count:36,success_rate:1,avg_seconds:18.4,p95_seconds:38.1,sample_count:36,recent:Array.from({length:20},(_,i)=>({...refs[0],source_id:'wechat',started_at:now-60*(20-i),finished_at:now-60*(20-i)+18.4,outcome:'succeeded'}))},
    {id:'qq',name:'QQ',kind:'qq',metrics_available:true,status:'retry',last_success:now-236,emitted:337,duplicates:72,emitted_complete:true,success:33,failed:1,skipped:3,unknown:0,running:0,retrying:1,valid_count:34,success_rate:33/34,avg_seconds:42.6,p95_seconds:68.3,sample_count:34,recent:Array.from({length:20},(_,i)=>({...refs[0],started_at:now-60*(20-i),finished_at:now-60*(20-i)+14,outcome:i===19?'failed':'succeeded'}))},
    {id:'external-example',name:'外部信息源 · 接入示例',kind:'external',metrics_available:false,status:'unsupported',emitted:null,recent:[]}
  ];
  return {window:{start:now-86400,end:now,hours:24,basis:'episode_started_at'},generated_at:now,coverage:{},truncation:{history:false,resources:false},warnings:[],sources,
    summary:{success:69,failed:1,valid_count:70,success_rate:69/70,emitted:1284,duplicates:312,emitted_complete:true,sources_measured:2,sources_total:3},
    success_bins:[5,6,6,5,6,6,6,5,6,6,6,6].map((success,i)=>({start:now-86400+i*7200,end:now-86400+(i+1)*7200,success,failed:i===11?1:0,success_rate:success/(success+(i===11?1:0)),sample_count:success+(i===11?1:0),run_refs:i===11?refs:[]})),
    issues:[{key:'qq-failure',kind:'job_error',module:'qq',source_id:'qq',run_id:refs[0].run_id,run_source:'console',last_at:now-182,error:'数据库被占用；上次成功 14:28，下轮等待重试。已保存断点未推进。',next_action:'检查数据库占用后再运行',config_target:'settings/source',detail_target:{kind:'run',id:refs[0].run_id,source:'console'}},{key:'delivery-unknown',kind:'delivery_unknown',module:'messaging',last_at:now-114,error:'先核对目标群是否收到，再决定是否重试，避免重复发送。',next_action:'先核对目标端结果',config_target:'automation/channels',delivery_id:'fixture-unknown',detail_target:{kind:'delivery',id:'fixture-unknown'}}],
    service:{state:'running',running:true,verified:true,enabled:true,pid:1234},jobs:[{module:'wechat',source_id:'wechat',enabled:true,state:'idle'},{module:'qq',source_id:'qq',enabled:true,state:'retry'},{module:'processor',enabled:true,state:'running'},{module:'messaging',enabled:true,state:'idle'},{module:'feedback',enabled:true,state:'idle'}],
    queues:{processor:{pending_groups:8},messaging:{counts:{pending:0,retry:0,unknown:1}},feedback:{counts:{queued:0,failed:0,conflict:0}}},
    resources:{basis:'logical_bytes',physical_bytes:null,measured_at:now,complete:true,total_logical_bytes:14e9,known_logical_bytes:14e9,sources:[{source_id:'wechat',logical_bytes:4.6e9,status:'measured'},{source_id:'qq',logical_bytes:8.1e9,status:'measured'},{source_id:'external-example',logical_bytes:null,status:'unsupported'}],shared:{logical_bytes:1.3e9,status:'measured'}}};
};
