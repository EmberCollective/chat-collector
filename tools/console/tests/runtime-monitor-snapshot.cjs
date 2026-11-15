'use strict';
// Synthetic probe response for isolated browser checks; never imported by the application.
module.exports=function runtimeSnapshot(now=Date.now()/1000){
  const cpu=[12,14,16,20,28,32,21,18,22,34,44,41,38],shared=[1.2,1.4,2.1,2.4,3.2,4.2,3.1,2.4,2.6,6.8,21.2,19.1,16.8];
  const busy=[18,23,20,26,32,24,21,29,33,58,82,78,74],reads=[1.8,2.4,2.1,3.8,4.2,3,2.4,4.1,6.2,14.6,27.4,25.1,23.8],writes=[0.4,0.6,0.5,0.8,1.2,0.7,0.8,1.4,2.1,5.2,9.4,8.8,8.2];
  const modules=[['wechat','微信'],['qq','QQ'],['processor','待办处理'],['messaging','消息投递'],['feedback','状态回写']].map(([id,name])=>({id,name,group_ids:['shared'],attribution:'shared'}));
  modules.push({id:'listener',name:'渠道监听',group_ids:['listener'],attribution:'independent'},{id:'console',name:'控制台',group_ids:['console'],attribution:'independent'},{id:'external-example',name:'外部信息源 · 接入示例',group_ids:[],attribution:'unassigned'});
  return {generated_at:now,interval_seconds:5,status:'ok',error:null,sample_duration_ms:1.7,modules,
    disks:[{id:'physical-disk-0',name:'磁盘 0',mounts:['D:','E:'],kind:'hdd'},{id:'physical-disk-2',name:'磁盘 2',mounts:['F:'],kind:'hdd'}],
    samples:cpu.map((value,i)=>({at:now-60+i*5,cpu:value,active_jobs:i>=10?[{module:'qq',source_id:'qq',name:'QQ',started_at:now-12}]:i>=5&&i<=7?[{module:'processor',source_id:null,name:'待办处理',started_at:now-35}]:[],disks:[{id:'physical-disk-0',busy:busy[i],read_mbps:reads[i],write_mbps:writes[i]},{id:'physical-disk-2',busy:8,read_mbps:1.4,write_mbps:0.3}],processes:[{id:'shared',name:'共享后台',module_ids:['wechat','qq','processor','messaging','feedback'],cpu:shared[i],read_mbps:22.4,write_mbps:7.6,status:'ok'},{id:'listener',name:'渠道监听',module_ids:['listener'],cpu:0.2,read_mbps:0,write_mbps:0,status:'ok'},{id:'console',name:'控制台',module_ids:['console'],cpu:0.5,read_mbps:0.1,write_mbps:0,status:'ok'}]}))};
};
