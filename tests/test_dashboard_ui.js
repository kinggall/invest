// Execute the actual inline controller against a minimal DOM to exercise request feedback.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../dashboard.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
class Node {constructor(){this.style={};this.children=[];this.attrs={};this.listeners={};this.elements=[];this.value='';this.textContent='';this.disabled=false}append(n){this.children.push(n)}replaceChildren(){this.children=[]}setAttribute(k,v){this.attrs[k]=v}addEventListener(k,fn){this.listeners[k]=fn}}
const elements=new Map([...ids].map(id=>[id,new Node()]));
const fields=elements.get('strategyForm').elements;for(const match of html.matchAll(/<input\b[^>]*name="([^"]+)"[^>]*>/g)){const field=new Node();field.name=match[1];field.type=match[0].match(/type="([^"]+)"/)?.[1]||'text';field.checked=false;fields.push(field);fields[field.name]=field}
const document={getElementById:id=>elements.get(id)||null,createElement:()=>new Node(),createElementNS:()=>new Node()};
let status={csrf:'test-token',enabled:false,token:'준비',account_ready:true,strategy:{id:'tiger-macd-v1',symbol:'472150',fast:12,slow:26,signal:9,quantity:1,max_notional:100000,limit_offset_bps:10,max_daily_buys:10,vwap_filter:false},orders:[],signals:[],snapshot:null,history:null,backtest:null,last_message:'중지됨',lookup_job:{state:'idle'}};
const requests=[];class FormDataMock {constructor(form){this.entries=[...form.elements].filter(e=>e.type!=='checkbox'||e.checked).map(e=>[e.name,e.value])}[Symbol.iterator](){return this.entries[Symbol.iterator]()}}
const context=vm.createContext({document,FormData:FormDataMock,setInterval:()=>0,console,fetch:async(url,options)=>{
 if(options?.body)requests.push(JSON.parse(options.body));
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
 status={...status,lookup_job:{id:'2',command:'historical',state:'succeeded',message:'과거 3분봉 조회 완료'},history:{cutoff:'2026-11-13',chart:[{time:'2026-11-13T09:00:00+09:00',open:15000,high:15010,low:14990,close:15005,volume:100,macd:1,signal:.5,histogram:.5,vwap:14999,vwap_distance_pct:.04,vwap_change:1}],periods:[12,26,9],missing_bars:[],source:'검증 데이터',warmup_date:'2026-11-12'}};
 await vm.runInContext('refresh()',context);
 assert.equal(elements.get('historyCandles').style.display,'block');
 assert.ok(elements.get('historyCandles').children.length>0);
 assert.equal(elements.get('historyFeedback').textContent,'과거 3분봉 조회 완료');
 assert.ok(elements.get('historyCandles').children.some(node=>node.attrs.stroke==='#8c69bb'&&node.attrs.d));
 const point=status.history.chart[0];
 status={...status,snapshot:{position:0,status:'NO_SIGNAL',bars:35,required_bars:35,chart:[point],last_bar:point,flow:{history:[{observed_at:'2026-11-16T10:01:00+09:00',foreign:123,foreign_change:-5,foreign_change_3m:-10,institution:50,institution_change:2,institution_change_3m:3}],foreign:123,foreign_change:-5,foreign_change_3m:-10,institution:50,institution_change:2,institution_change_3m:3}}};
 await vm.runInContext('refresh()',context);
 assert.match(elements.get('foreignChange').textContent,/-5/);
 assert.equal(elements.get('flowSamples').children.length,1);
 const summary={net_pnl:-5000,return_pct:-.05,max_drawdown_pct:.1,buy_count:1,sell_count:1};
 status={...status,backtest:{date:'2026-11-13',initial_capital:10000000,final_equity:9995000,net_pnl:-5000,return_pct:-.05,buy_count:1,sell_count:1,round_trips:1,win_rate_pct:0,max_drawdown_pct:.1,fees:3000,tax:0,realized_pnl:-5000,unrealized_pnl:0,ending_shares:0,trades:[],equity:[{value:10000000},{value:9995000}],assumptions:{vwap_filter:true,execution:'다음 봉',mark:'종가평가',liquidity:'가정'},comparison:{selected:'vwap',baseline:{...summary,net_pnl:-10000},vwap:summary}}};
 await vm.runInContext('refresh()',context);
 assert.equal(elements.get('btComparison').children.length,2);
 assert.match(elements.get('btHeading').textContent,/VWAP/);
 assert.match(elements.get('btComparison').children[1].children[0].textContent,/아래 상세/);
 assert.equal(fields.vwap_filter.checked,false);
 fields.vwap_filter.checked=true;
 elements.get('strategyForm').listeners.submit({preventDefault(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(requests.at(-1).vwap_filter,true);
 fields.vwap_filter.checked=false;
 elements.get('strategyForm').listeners.submit({preventDefault(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(requests.at(-1).vwap_filter,false);
 console.log('Dashboard controller: loading, inline failure, retry availability and chart render, VWAP overlay, checkbox serialization and time-series flow passed.');
})().catch(error=>{console.error(error);process.exitCode=1});
