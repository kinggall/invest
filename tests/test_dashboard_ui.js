// Execute the actual inline controller against a minimal DOM to exercise request feedback.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../dashboard.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
class Node {constructor(){this.style={};this.children=[];this.attrs={};this.listeners={};this.elements=[];this.value='';this.textContent='';this.disabled=false}append(n){this.children.push(n)}replaceChildren(){this.children=[]}setAttribute(k,v){this.attrs[k]=v}addEventListener(k,fn){this.listeners[k]=fn}}
const elements=new Map([...ids].map(id=>[id,new Node()]));
const document={getElementById:id=>elements.get(id)||null,createElement:()=>new Node(),createElementNS:()=>new Node()};
let status={csrf:'test-token',enabled:false,token:'준비',account_ready:true,strategy:{},orders:[],signals:[],snapshot:null,history:null,backtest:null,last_message:'중지됨',lookup_job:{state:'idle'}};
const context=vm.createContext({document,setInterval:()=>0,console,fetch:async(url)=>{
 if(url!='/api/status')status={...status,lookup_job:{id:'1',command:'historical',state:'running',message:'2026-11-13 · 1/5페이지 조회 중'}};
 return {ok:true,status:200,json:async()=>status};
}});
(async()=>{
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(elements.get('historyCandles').style.display,'none');
 await vm.runInContext("action('historical',{date:'2026-11-13'})",context);
 assert.equal(elements.get('historical').disabled,true);
 assert.match(elements.get('historyFeedback').textContent,/1\/5페이지/);
 status={...status,lookup_job:{id:'1',command:'historical',state:'failed',message:'기존 실전 조회 토큰 확인 필요 [SHARED_REAL_TOKEN_UNAVAILABLE]'}};
 await vm.runInContext('refresh()',context);
 assert.match(elements.get('historyFeedback').textContent,/SHARED_REAL_TOKEN_UNAVAILABLE/);
 assert.equal(elements.get('historyFeedback').style.display,'block');
 assert.equal(elements.get('historyFeedback').attrs.role,'alert');
 assert.equal(elements.get('historical').disabled,false);
 status={...status,lookup_job:{id:'2',command:'historical',state:'succeeded',message:'과거 3분봉 조회 완료'},history:{cutoff:'2026-11-13',chart:[{time:'2026-11-13T09:00:00+09:00',open:15000,high:15010,low:14990,close:15005,volume:100,macd:1,signal:.5,histogram:.5}],periods:[12,26,9],missing_bars:[],source:'검증 데이터',warmup_date:'2026-11-12'}};
 await vm.runInContext('refresh()',context);
 assert.equal(elements.get('historyCandles').style.display,'block');
 assert.ok(elements.get('historyCandles').children.length>0);
 assert.equal(elements.get('historyFeedback').textContent,'과거 3분봉 조회 완료');
 console.log('Dashboard controller: loading, inline failure, retry availability and chart render passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
