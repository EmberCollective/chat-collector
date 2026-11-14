'use strict';
// The three source fragments are the only overview implementation; the preview stays one file.
const fs=require('node:fs'),path=require('node:path');
const target=path.join(__dirname,'../console-redesign/index.html');
const before=fs.readFileSync(target,'utf8').replace(/\r\n/g,'\n');
let after=before;
for(const [kind,file,wrap]of [
 ['style','overview.css',body=>`<style>\n${body.trim()}\n</style>`],
 ['markup','overview.html',body=>{const at=body.indexOf('<dialog');if(at<0)throw Error('Missing shared run dialog');return ` <section id="view-overview" class="view" aria-labelledby="overview-title" hidden>\n${body.slice(0,at).trim()}\n </section>\n${body.slice(at).trim()}`;}],
 ['script','overview.js',body=>`<script>\n${body.trim()}\n</script>`]
]){
 const start=`<!-- overview:${kind}:start -->`,end=`<!-- overview:${kind}:end -->`;
 const begin=after.indexOf(start),finish=after.indexOf(end);
 if(begin<0||finish<begin)throw Error('Missing overview build markers: '+kind);
 const content=fs.readFileSync(path.join(__dirname,'source',file),'utf8').replace(/\r\n/g,'\n');
 after=after.slice(0,begin+start.length)+'\n'+wrap(content)+'\n'+after.slice(finish);
}
if(process.argv.includes('--check')){
 if(after!==before){console.error('Overview preview is stale. Run node docs/design/overview-dashboard/build.cjs');process.exitCode=1;}
 else console.log('PASS overview generated content matches its source');
}else if(after!==before){fs.writeFileSync(target,after);console.log('Updated the shared overview in console-redesign/index.html');}
