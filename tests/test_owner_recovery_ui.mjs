import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import path from 'node:path';
const dir=path.join(import.meta.dirname,'../agent-console');
const source=fs.readFileSync(path.join(dir,'cluster.js'),'utf8');
const components=fs.readFileSync(path.join(dir,'components.js'),'utf8');
class Node {
  constructor(tag,attrs={},...children){this.tag=tag;this.attrs=attrs;this.children=[];this.events={};this.append(...children);}
  append(...items){for(const item of items.flat(Infinity)){if(item===null||item===undefined)continue;this.children.push(item);if(item instanceof Node)item.parent=this;}}
  replaceChildren(...items){this.children=[];this.append(...items);}
  replaceWith(node){const i=this.parent.children.indexOf(this);this.parent.children[i]=node;node.parent=this.parent;}
  addEventListener(name,fn){this.events[name]=fn;}
  get text(){return this.children.map(x=>x instanceof Node?x.text:String(x)).join(' ');}
  find(tag){if(this.tag===tag)return this;for(const x of this.children){const n=x instanceof Node&&x.find(tag);if(n)return n;}}
}
const calls=[];
const scope={
  el:(...args)=>new Node(...args),
  action:(label,run)=>{const n=new Node('button',{},label);n.run=run;return n;},
  chip:()=>new Node('chip'),
  confirmDialog:async()=>true,
  fetcher:async(...args)=>{calls.push(args);return {};},
  ctx:{api:async(p)=>{calls.push([p]);return p.includes('/actions')?{tunnels:[]}:{config:{}};},
    render:async()=>{},errorBox:(err)=>new Node('error',{},err.message)},
  path:(name,rest='')=>'clusters/'+encodeURIComponent(name)+(rest?'/'+rest:''),
  tunnelUp:c=>c.tunnel?.state==='up',
  AGENT_PAGES:[['','Tunnel and setup']],subTabs:()=>new Node('nav'),href:()=>'',mcpNameCard:()=>new Node('mcp'),
};
vm.createContext(scope);
const recovery=components.slice(components.indexOf('export function ownerRecovery('),components.indexOf('/** 19.5 GB:'));
const agents=source.slice(source.indexOf('async function agents('),source.indexOf('// Tunnel actions (rest_actions.py'));
const card=source.slice(source.indexOf('function tunnelActionsCard('),source.indexOf('// What LLM clients call this cluster'));
vm.runInContext(recovery.replace('export function','function')+'\n'+agents+'\n'+card,scope);
const c={name:'me@production.example',set_up:true,has_owner_token:false,tunnel:{state:'up'}};
let n=scope.tunnelActionsCard(c);
assert.match(n.text,/connected for agents/);
assert.doesNotMatch(n.text,/Start the agent tunnel/);
assert.equal(calls.length,0);
assert.equal(n.find('button').text,'Recover owner access');
await n.find('button').run();
assert.equal(calls[0][0],'clusters/me%40production.example/setup');
assert.equal(JSON.stringify(calls[0][1].body),'{"repair_owner_token":true}');
calls.length=0;
n=scope.tunnelActionsCard({...c,tunnel:{state:'down'}});
assert.match(n.text,/Start the agent tunnel/);
assert.equal(calls.length,0);
n=scope.tunnelActionsCard({...c,tunnel:{state:'starting'}});
assert.match(n.text,/connecting/);
assert.doesNotMatch(n.text,/Start the agent tunnel/);
n=scope.tunnelActionsCard({...c,tunnel:{state:'error',error:'authentication failed'}});
assert.match(n.text,/authentication failed/);
assert.doesNotMatch(n.text,/Start the agent tunnel/);
n=scope.tunnelActionsCard({...c,has_owner_token:true,tunnel:{state:'up',owner_access:'rejected'}});
assert.match(n.text,/connected for agents/);
assert.match(n.text,/rejected/);
assert.equal(calls.length,0);
n=scope.tunnelActionsCard({...c,has_owner_token:true});
await new Promise(r=>setImmediate(r));
assert.equal(calls.length,2);
assert.ok(calls.some(([p])=>p.endsWith('/rest/admin/config')));
calls.length=0;
const page=await scope.agents({...c,tunnel:{state:'down'}},true,'');
assert.match(page.text,/Start the agent tunnel/);
const row=page.find('hpc-cluster-row');
row.events.update({detail:c});
assert.match(page.text,/connected for agents/);
assert.doesNotMatch(page.text,/Start the agent tunnel/);
row.events.update({detail:{...c,has_owner_token:true}});
await new Promise(r=>setImmediate(r));
assert.equal(calls.length,2);
assert.doesNotMatch(page.text,/connected for agents/);
scope.confirmDialog=async()=>false;
calls.length=0;
await scope.ownerRecovery(c).run();
assert.equal(calls.length,0);
console.log('PASS: owner-only recovery request, cancellation, connected/missing/rejected states, admin gating and live card refresh');
