// Exercise the actual dedicated-page controller with a small DOM, without a browser.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../dashboard.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
class Node {constructor(){this.style={};this.children=[];this.attrs={};this.listeners={};this.elements=[];this.value='';this.textContent='';this.disabled=false}append(n){this.children.push(n)}replaceChildren(){this.children=[]}setAttribute(k,v){this.attrs[k]=v}addEventListener(k,fn){this.listeners[k]=fn}}
const elements=new Map([...ids].map(id=>[id,new Node()]));
const fields=elements.get('indicatorForm').elements;for(const match of html.matchAll(/<input\b[^>]*name="([^"]+)"[^>]*>/g)){const field=new Node();field.name=match[1];field.type='number';fields.push(field);fields[field.name]=field}
const document={getElementById:id=>elements.get(id)||null,createElement:()=>new Node(),createElementNS:()=>new Node()};
let status={api_version:'2026-11-focus1',restart_required:false,csrf:'test',account_ready:true,token:'준비',enabled:false,risk_active:false,
 strategy:{id:'kodex-macd-v1',symbol:'069500',mode:'macd',fast:12,slow:26,signal:9},lookup_job:{state:'idle'},snapshot:null,
 wall_paper:{enabled:false,quantity:0,average_entry:0,equity:10000000,net_pnl:0,message:'가상 시뮬레이션 중지',events:[],state:{run_id:'run',capital:10000000,cash:10000000,realized:0,fees:0,status:'STOPPED',position:null}},
 oi_job:{state:'idle'},oi_config:{expiry:'',radius_pct:5},future_job:{state:'idle'}};
const requests=[];
const context=vm.createContext({document,setInterval:()=>0,console,fetch:async(url,options)=>{if(options?.body)requests.push({url,value:JSON.parse(options.body)});return {ok:true,json:async()=>status}}});
(async()=>{
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);await new Promise(r=>setImmediate(r));
 for(const id of ['start','stop','strategyForm','orders','signals','backtest','riskPanel'])assert.equal(ids.has(id),false,id+' should be removed');
 assert.equal(elements.get('wallStart').disabled,false);
 assert.equal(fields.fast.value,12);
 elements.get('wallStart').listeners.click();await new Promise(r=>setImmediate(r));
 assert.equal(requests.at(-1).url,'/api/wall_start');
 status={...status,wall_paper:{...status.wall_paper,enabled:true,message:'진입 대기'}};
 await vm.runInContext('refresh()',context);
 assert.equal(elements.get('wallStart').disabled,true);assert.equal(elements.get('wallStop').disabled,false);assert.equal(fields.fast.disabled,true);
 elements.get('wallStop').listeners.click();await new Promise(r=>setImmediate(r));assert.equal(requests.at(-1).url,'/api/wall_stop');
 status={...status,wall_paper:{...status.wall_paper,enabled:false}};await vm.runInContext('refresh()',context);
 fields.fast.value='8';fields.slow.value='21';fields.signal.value='5';elements.get('indicatorForm').listeners.submit({preventDefault(){}});await new Promise(r=>setImmediate(r));
 assert.equal(requests.at(-1).url,'/api/strategy');assert.equal(requests.at(-1).value.fast,8);assert.equal(requests.at(-1).value.symbol,'069500');
 status={...status,lookup_job:{state:'running',command:'preview',message:'ETF 조회 중'}};await vm.runInContext('refresh()',context);
 assert.equal(elements.get('preview').disabled,true);
 status={...status,lookup_job:{state:'failed',command:'preview',message:'조회 실패 · 다시 시도'}};await vm.runInContext('refresh()',context);
 assert.equal(elements.get('preview').disabled,false);assert.match(elements.get('previewFeedback').textContent,/조회 실패/);
 const point={time:'2026-11-17T10:30:00+09:00',end:'2026-11-17T10:33:00+09:00',open:79000,high:79100,low:78900,close:79050,volume:100,macd:5,signal:3,histogram:2,vwap:79000,vwap_distance_pct:.06};
 const put={side:'put',strike:780,oi:1107,code:'P',received_at:'2026-11-17T10:33:00+09:00',projected_future_level:779},call={side:'call',strike:800,oi:2537,code:'C',received_at:put.received_at,projected_future_level:799};
 status={...status,snapshot:{observed_at:'2026-11-17T10:33:10+09:00',symbol:'069500',bars:40,required_bars:35,periods:[12,26,9],chart:[point],last_bar:point,flow:{foreign:-100,institution:20,foreign_change:5,foreign_change_3m:10,institution_change:1,institution_change_3m:2,history:[]}},
 oi_snapshot:{observed_at:put.received_at,expiry:'202612',expiries:[{code:'202612',label:'202612'}],spot:786.17,radius_pct:5,calls:[call],puts:[put],put_wall:put,call_wall:call,put_major_wall:{...put,strike:750,oi:4783},history:[],warnings:[]},
 confirmation:{message:'보조조건 혼재',basis:'KODEX 200 ETF 확정 3분봉',walls:{state:'범위 확인',put:780,call:800,spot:786.17},macd:{state:'상방 우위',improving:true,histogram:2,previous_histogram:1},vwap:{state:'VWAP 위',value:79000,rising:true},flow:{state:'합산 순매수 증가',foreign:10,institution:2,interval_seconds:180,unit:'API 원단위'}}};
 await vm.runInContext('refresh()',context);
 assert.match(elements.get('oiPutInfo').textContent,/전체 주요 벽 750pt/);assert.match(elements.get('confirmMacd').textContent,/개선/);assert.match(elements.get('foreignChange').textContent,/약3분 10/);assert.match(elements.get('vwapDiagnostic').textContent,/79,000/);
 const night={id:'night:2026-11-16',mode:'night',start:'2026-11-16T18:00:00+09:00',scheduled_open:true};
 status={...status,future_context:night,future_job:{state:'succeeded',message:'야간 조회 완료'},future_snapshot:{session:night,code:'A01612',source:'CM 실제 분봉',observed_at:'2026-11-17T01:03:10+09:00',last_bar_end:'2026-11-17T01:03:00+09:00',bars:[{...point,time:'2026-11-17T01:00:00+09:00',end:'2026-11-17T01:03:00+09:00',open:799,high:801,low:798,close:800}],warnings:[]}};
 await vm.runInContext('refresh()',context);assert.match(elements.get('futureTitle').textContent,/야간선물/);assert.match(elements.get('futureWarnings').textContent,/환산선을 숨깁니다/);assert.equal(elements.get('oiFuture').textContent,'800pt / —pt');
 status={...status,wall_paper:{...status.wall_paper,enabled:true,quantity:10,average_entry:79005,events:[{time:'2026-11-17T10:33:10+09:00',side:'buy',reason:'REBOUND_ENTRY',quantity:10,price:79005,reference_price:79000,fee:118.51}],state:{...status.wall_paper.state,updated_at:'2026-11-17T10:33:10+09:00',status:'ENTERED',position:{put:780,call:800,target1:792,target2:796,put_stop:777.66,stop_price:78214.95,breakout_count:0,added:false}}}};
 await vm.runInContext('refresh()',context);assert.equal(elements.get('wallTrades').children.length,1);assert.match(elements.get('wallBox').textContent,/792/);assert.ok(elements.get('wallCandles').children.some(n=>n.attrs.points));
 status={...status,restart_required:true};await vm.runInContext('refresh()',context);assert.equal(elements.get('wallStart').disabled,true);assert.equal(elements.get('preview').disabled,true);const before=requests.length;await vm.runInContext("action('wall_start')",context);assert.equal(requests.length,before);
 assert.ok(requests.every(r=>r.url!='/api/start'));
 console.log('Dedicated paper page: legacy controls absent, indicator settings, OI, futures, flow, ledger and chart markers, loading/retry and version guard passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
