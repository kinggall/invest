// Exercise the actual dedicated-page controller with a small DOM, without a browser.
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync(require('node:path').join(__dirname,'../dashboard.html'),'utf8');
const ids=new Set([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]));
class Node {constructor(){this.style={};this.children=[];this.attrs={};this.listeners={};this.elements=[];this.value='';this.textContent='';this.disabled=false}append(n){this.children.push(n)}replaceChildren(){this.children=[]}setAttribute(k,v){this.attrs[k]=v}addEventListener(k,fn){this.listeners[k]=fn}focus(){this.focused=true}}
const elements=new Map([...ids].map(id=>[id,new Node()]));
for(const id of ['replayMacdFilter','replayVwapSizing','replayBreakoutAdd'])elements.get(id).checked=true;elements.get('replayReboundBars').value='1';
const document={getElementById:id=>elements.get(id)||null,createElement:()=>new Node(),createElementNS:()=>new Node()};
let status={api_version:'2026-11-demo2',restart_required:false,csrf:'test',account_ready:true,token:'준비',enabled:false,risk_active:false,
 strategy:{id:'kodex-macd-v1',symbol:'069500',mode:'macd',fast:12,slow:26,signal:9},lookup_job:{state:'idle'},snapshot:null,
 wall_paper:{enabled:false,quantity:0,average_entry:0,equity:10000000,net_pnl:0,message:'가상 시뮬레이션 중지',events:[],state:{run_id:'run',capital:10000000,cash:10000000,realized:0,fees:0,status:'STOPPED',position:null}},
 oi_job:{state:'idle'},oi_config:{expiry:'',radius_pct:5},future_job:{state:'idle'}};
const requests=[];
const context=vm.createContext({document,setInterval:()=>0,console,fetch:async(url,options)=>{if(options?.body)requests.push({url,value:JSON.parse(options.body)});return {ok:true,json:async()=>status}}});
(async()=>{
 vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1],context);await new Promise(r=>setImmediate(r));
 for(const id of ['indicatorForm','flowSamples','oiHistoryChart','start','stop','strategyForm','orders','signals','backtest','riskPanel'])assert.equal(ids.has(id),false,id+' should be removed');
 assert.equal(elements.get('wallStart').disabled,false);
 assert.equal((html.match(/role="tab"/g)||[]).length,3);
 // Quarter-hour ticks follow KST across missing bars and midnight.
 const axis=vm.runInContext("(()=>{const svg=document.createElementNS('','svg');timeAxis(svg,[{time:'2026-11-17T09:00:00+09:00'},{time:'2026-11-17T10:00:00+09:00'}],i=>30+i*740,200);return svg})()",context);
 const ticks=axis.children.filter(n=>n.attrs['data-time-tick']);assert.deepEqual(ticks.map(n=>n.textContent),['9시','15','30','45','10시']);assert.equal(ticks[2].attrs.x,400);
 const nightAxis=vm.runInContext("(()=>{const svg=document.createElementNS('','svg');timeAxis(svg,[{time:'2026-11-17T23:45:00+09:00'},{time:'2026-11-18T00:15:00+09:00'}],i=>30+i*740,200);return svg})()",context);
 assert.deepEqual(nightAxis.children.filter(n=>n.attrs['data-time-tick']).map(n=>n.textContent),['45','0시','15']);

 elements.get('tabOI').listeners.click();assert.equal(elements.get('panelPaper').hidden,true);assert.equal(elements.get('panelOI').hidden,false);assert.equal(elements.get('tabOI').attrs['aria-selected'],'true');
 elements.get('tabOI').listeners.keydown({key:'ArrowRight',preventDefault(){}});assert.equal(elements.get('panelReplay').hidden,false);assert.equal(elements.get('tabReplay').focused,true);
 elements.get('tabPaper').listeners.click();assert.equal(elements.get('panelPaper').hidden,false);assert.equal(elements.get('panelReplay').hidden,true);

 elements.get('wallStart').listeners.click();await new Promise(r=>setImmediate(r));
 assert.equal(requests.at(-1).url,'/api/wall_start');
 status={...status,wall_paper:{...status.wall_paper,enabled:true,message:'진입 대기'}};
 await vm.runInContext('refresh()',context);
 assert.equal(elements.get('wallStart').disabled,true);assert.equal(elements.get('wallStop').disabled,false);
 elements.get('wallStop').listeners.click();await new Promise(r=>setImmediate(r));assert.equal(requests.at(-1).url,'/api/wall_stop');
 status={...status,wall_paper:{...status.wall_paper,enabled:false}};await vm.runInContext('refresh()',context);
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
 assert.match(elements.get('oiPutInfo').textContent,/전체 주요 벽 750pt/);assert.match(elements.get('confirmMacd').textContent,/개선/);
 const night={id:'night:2026-11-16',mode:'night',start:'2026-11-16T18:00:00+09:00',scheduled_open:true};
 status={...status,future_context:night,future_job:{state:'succeeded',message:'야간 조회 완료'},future_snapshot:{session:night,code:'A01612',source:'CM 실제 분봉',observed_at:'2026-11-17T01:03:10+09:00',last_bar_end:'2026-11-17T01:03:00+09:00',bars:[{...point,time:'2026-11-17T01:00:00+09:00',end:'2026-11-17T01:03:00+09:00',open:799,high:801,low:798,close:800}],warnings:[]}};
 await vm.runInContext('refresh()',context);assert.match(elements.get('futureTitle').textContent,/야간선물/);assert.match(elements.get('futureWarnings').textContent,/환산선을 숨깁니다/);assert.equal(elements.get('oiFuture').textContent,'800pt / —pt');
 status={...status,wall_paper:{...status.wall_paper,enabled:true,quantity:10,average_entry:79005,events:[{time:'2026-11-17T10:33:10+09:00',side:'buy',reason:'REBOUND_ENTRY',quantity:10,price:79005,reference_price:79000,fee:118.51}],state:{...status.wall_paper.state,updated_at:'2026-11-17T10:33:10+09:00',status:'ENTERED',position:{put:780,call:800,target1:792,target2:796,put_stop:777.66,stop_price:78214.95,breakout_count:0,added:false}}}};
 await vm.runInContext('refresh()',context);assert.equal(elements.get('wallTrades').children.length,1);assert.match(elements.get('wallBox').textContent,/792/);assert.ok(elements.get('wallCandles').children.some(n=>n.attrs.points));
 status={...status,wall_paper:{...status.wall_paper,retry_after_seconds:25}};await vm.runInContext('refresh()',context);assert.match(elements.get('wallRunning').textContent,/호출 한도 대기 · 25초/);assert.match(elements.get('running').textContent,/호출 한도 대기/);
 status={...status,replay_dates:['2026-11-16'],replay_job:{state:'running',message:'100개 표본 재생 중'}};await vm.runInContext('refresh()',context);assert.equal(elements.get('replayRun').disabled,true);assert.equal(elements.get('replayDate').value,'2026-11-16');
 status={...status,replay_job:{state:'failed',message:'선택일 과거 OI 기록이 없습니다'}};await vm.runInContext('refresh()',context);assert.equal(elements.get('replayRun').disabled,false);assert.match(elements.get('replayFeedback').textContent,/OI 기록/);
 elements.get('replayCapital').value='10000000';elements.get('replayMode').value='minute';elements.get('replayMacdFilter').checked=false;elements.get('replayReboundBars').value='3';elements.get('replayRun').listeners.click();await new Promise(r=>setImmediate(r));assert.equal(requests.at(-1).url,'/api/replay');assert.equal(requests.at(-1).value.options.macd_filter,false);assert.equal(requests.at(-1).value.options.rebound_bars,3);assert.equal(requests.at(-1).value.mode,'minute');
 status={...status,replay_job:{state:'succeeded',message:'재생 완료 · 성과 판단 보류'},replay_result:{date:'2026-11-16',mode:'minute',capital:10000000,first:'2026-11-16T12:00:00+09:00',last:'2026-11-16T15:00:00+09:00',samples:10,ready_samples:0,final_equity:10000000,net_pnl:0,return_pct:0,buy_count:0,sell_count:0,round_trips:0,win_rate_pct:null,max_drawdown_pct:0,ending_quantity:0,fees:0,warnings:['분봉 시가 가정 · 자료 부족'],curve:[{time:'2026-11-16T12:00:00+09:00',value:10000000}],events:[],chart:[]}};await vm.runInContext('refresh()',context);assert.equal(elements.get('replayResult').hidden,false);assert.match(elements.get('replayPnl').textContent,/성과 판단 보류/);assert.match(elements.get('replayWarnings').textContent,/시가 가정/);
 status={...status,wall_paper:{...status.wall_paper,pending_count:1,orders:[{time:'2026-11-17T10:33:00+09:00',order_no:'123',status:'PARTIAL',side:'buy',quantity:25,filled:7,reason:'REBOUND_ENTRY'}],account_balance:{quantity:7,deposit:9000000,observed_at:'2026-11-17T10:34:00+09:00'}}};await vm.runInContext('refresh()',context);assert.equal(elements.get('brokerOrders').children.length,1);assert.match(elements.get('brokerBalance').textContent,/7주/);assert.equal(elements.get('wallReset').disabled,true);
 const rangeNode=vm.runInContext("(()=>{drawOIBars({spot:770,radius_pct:5,coverage_complete:false,puts:[{side:'put',strike:750,oi:100,stale:true}],calls:[{side:'call',strike:800,oi:200,stale:true},{side:'call',strike:1595,oi:10000,stale:true}],call_major_wall:{side:'call',strike:1595,oi:10000}});return document.getElementById('oiRows')})()",context);assert.equal(rangeNode.children.length,2);assert.match(elements.get('oiRangeNote').textContent,/700~850pt/);
 vm.runInContext("drawOIBars({spot:770,radius_pct:5,puts:[],calls:[{side:'call',strike:1595,oi:10000}]})",context);assert.equal(elements.get('oiRows').children.length,0);
 vm.runInContext("drawOIBars({spot:null,radius_pct:5,puts:[],calls:[{side:'call',strike:1595,oi:10000}]})",context);assert.equal(elements.get('oiRows').children.length,0);assert.match(elements.get('oiRangeNote').textContent,/현재 지수 미수신/);
 status={...status,wall_paper:{...status.wall_paper,enabled:false,day_exit_armed:true,message:'당일 청산 미완료',state:{...status.wall_paper.state,status:'CLOSE_FAILED'}}};await vm.runInContext('refresh()',context);assert.equal(elements.get('wallFeedback').className,'error');assert.match(elements.get('wallRunning').textContent,/당일 청산 확인 필요/);
 status={...status,oi_job:{state:'running',message:'OI 전체 새 수신 4/64'}};await vm.runInContext('refresh()',context);assert.equal(elements.get('oiContent').hidden,true);assert.match(elements.get('oiObserved').textContent,/수신 중/);
 status={...status,oi_job:{state:'succeeded',message:'OI 전체 수신 완료'}};await vm.runInContext('refresh()',context);assert.equal(elements.get('oiContent').hidden,false);
 status={...status,restart_required:true};await vm.runInContext('refresh()',context);assert.equal(elements.get('wallStart').disabled,true);assert.equal(elements.get('preview').disabled,true);const before=requests.length;await vm.runInContext("action('wall_start')",context);assert.equal(requests.length,before);
 assert.ok(requests.every(r=>r.url!='/api/start'));
 console.log('Dedicated paper page: three tabs, legacy controls absent, OI, futures, confirmation, ledger and chart markers, loading/retry and version guard passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
