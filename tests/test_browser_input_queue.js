const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('admin_static/app.js', 'utf8');
const command = source.slice(source.indexOf('async function browserCommand('), source.indexOf("document.addEventListener('click',async event=>{\n const button=event.target.closest('button');"));
const calls = [], pending = [];
const flow = {id:'test',epoch:1,busy:false,queue:[],dragging:false};
const context = {serverBrowser:flow, authEpoch:1,
 request:(url,data)=>{calls.push({url,data});return new Promise(resolve=>pending.push(resolve));},
 $:()=>({open:true}), clearTimeout:()=>{}, setTimeout:()=>1, browserFrame:()=>{},toast:()=>{}};
vm.createContext(context);vm.runInContext(command, context);
(async()=>{
 const first=context.browserCommand('status');
 await context.browserCommand('input',{kind:'pointer-events',events:[{type:'down',x:20,y:30}]});
 for(let x=21;x<100;x++)await context.browserCommand('input',{kind:'pointer-events',events:[{type:'move',x,y:30}]});
 await context.browserCommand('input',{kind:'pointer-events',events:[{type:'up',x:100,y:30}]});
 assert.equal(calls.length,1,'input must wait for an in-flight frame');
 assert.equal(flow.queue.length,1,'adjacent pointer inputs should be batched');
 assert.deepEqual(flow.queue[0].data.events.map(e=>e.type),['down','move','up']);
 assert.equal(flow.queue[0].data.events[1].x,99,'coalesce only move events');
 pending.shift()({status:'ready'});await first;
 assert.equal(calls.length,2,'queued input must be sent immediately after frame completes');
 assert.equal(calls[1].url,'/api/browser-input');
 assert.equal(calls[1].data.events[0].type,'down');
 assert.equal(calls[1].data.events[2].type,'up');
 pending.shift()({status:'ready'});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(flow.busy,false);
 console.log('PASS: dragging during frame refresh preserves down/up and coalesces moves');
})().catch(error=>{console.error(error);process.exitCode=1;});
