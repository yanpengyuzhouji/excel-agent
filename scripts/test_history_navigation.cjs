// Exercise the real navigation code with delayed network responses; no server writes.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const source=fs.readFileSync('examples/frontend/index.html','utf8');
const elements=new Map(),pending=[];
const state={models:[],conversations:[{conv_id:'a',title:'A'},{conv_id:'b',title:'B'}]};
const context=vm.createContext({globalAgent:state,Map,AbortController,structuredClone,
 $:key=>{if(!elements.has(key))elements.set(key,{value:'',innerHTML:'',textContent:'',setAttribute(){},removeAttribute(){}});return elements.get(key);},
 api:(path,opts={})=>new Promise((resolve,reject)=>pending.push({path,opts,resolve,reject})),
 stopExecutionTimer(){},renderAttachments(){},renderSidebar(){},renderGlobalExecution(){},syncControls(){},renderMessage:()=>'',notice:()=>{}});
const helperStart=source.indexOf('const conversationCache=');
if(helperStart>=0)vm.runInContext(source.slice(helperStart,source.indexOf('let executionTimer=',helperStart)),context);
vm.runInContext(source.slice(source.indexOf('async function loadGlobalConversation('),source.indexOf('function handleEvent(')),context);
const doc=(id)=>({title:id,scope:{attachments:[],workbook_ids:[]},messages:[{role:'ai',content:id,round_index:1}]});
(async()=>{
 const first=context.loadGlobalConversation('a');
 pending.shift().resolve(doc('a'));await first;
 await context.loadGlobalConversation('');
 const cached=context.loadGlobalConversation('a');
 assert.equal(state.answer,'a','cached conversation must appear before the network finishes');
 assert.equal(state.loading,true,'mutations must wait for revalidation');
 const old=pending.shift();
 const latest=context.loadGlobalConversation('b');
 assert.equal(old.opts.signal.aborted,true,'obsolete request must be cancelled');
 pending.shift().resolve(doc('b'));await latest;
 old.resolve(doc('stale-a'));await cached;
 assert.equal(state.answer,'b','late response must not replace current conversation');
 assert.equal(state.loading,false);
 const deleted=context.loadGlobalConversation('a');
 const error=Object.assign(new Error('deleted'),{status:404});pending.shift().reject(error);await deleted;
 assert.equal(state.answer,'','deleted cached transcript must be cleared');
 assert.equal(state.selectedConversationId,'');
 const failing=context.loadGlobalConversation('b');
 pending.shift().reject(new Error('offline'));await failing;
 assert.equal(state.loadFailed,true,'do not allow questions against an unverified stale attachment scope');
 await context.loadGlobalConversation('');assert.equal(state.loadFailed,false);
 const cancelled=context.loadGlobalConversation('b');const request=pending.shift();
 await context.loadGlobalConversation('');request.resolve(doc('late'));await cancelled;
 assert.equal(state.selectedConversationId,'');assert.equal(state.answer,'');
 console.log('History: instant cache, cancellation, stale guard, deleted cache cleanup passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
