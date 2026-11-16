from datetime import datetime,timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from futures_chart import FuturesChart,session_at,stamp_for,closed_bars
from trader import KST,TradingError,FUTURES_MINUTES

class Quotes:
    def __init__(self,minutes,mode='day'):
        self.minutes=minutes;self.mode=mode;self.calls=[];self.code='A01612'
    def request(self,path,tr,params,post=False):
        self.calls.append((path,tr,params,post))
        self.assert_route(path,tr,post)
        base=datetime.strptime(params['FID_INPUT_DATE_1'],'%Y%m%d').replace(tzinfo=KST)
        hour=params['FID_INPUT_HOUR_1'];cursor=base+timedelta(hours=int(hour[:2]),minutes=int(hour[2:4]))
        rows=[]
        for row in reversed(self.minutes):
            stamp=datetime.fromisoformat(row['time'])
            if stamp>cursor:continue
            if self.mode=='night':
                anchor=stamp.replace(hour=0,minute=0,second=0,microsecond=0)-(timedelta(days=1) if stamp.hour<6 else timedelta())
                h=stamp.hour+(24 if stamp.hour<6 else 0)
                date=anchor.strftime('%Y%m%d')
            else:h=stamp.hour;date=stamp.strftime('%Y%m%d')
            rows.append(dict(stck_bsop_date=date,stck_cntg_hour=f'{h:02d}{stamp.minute:02d}00',futs_oprc=str(row['open']),futs_hgpr=str(row['high']),futs_lwpr=str(row['low']),futs_prpr=str(row['close']),cntg_vol=str(row['volume'])))
        return dict(output1=dict(futs_shrn_iscd=self.code),output2=rows[:102])
    def assert_route(self,path,tr,post):
        if path!=FUTURES_MINUTES or tr!='FHKIF03020200' or post:raise AssertionError('Not a read-only futures minute route')


def minutes(start,count):
    return [dict(time=(start+timedelta(minutes=i)).isoformat(),open=790+i*.05,high=791+i*.05,low=789+i*.05,close=790.5+i*.05,volume=i+1) for i in range(count)]


class FuturesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.metadata=patch('futures_chart.load_future_master',return_value=[dict(code='A01612',name='F 202612',expiry='202612')]);self.metadata.start();self.addCleanup(self.metadata.stop)
    def service(self,broker):
        result=FuturesChart(Path(self.tmp.name),Path(self.tmp.name)/'state.db',broker)
        self.addCleanup(result.close);return result

    def test_day_night_midnight_and_weekend_session_selection(self):
        cases=[((2026,11,16,9,0),'day',True,'2026-11-16'),((2026,11,16,19,0),'night',True,'2026-11-16'),
               ((2026,11,17,1,0),'night',True,'2026-11-16'),((2026,11,17,7,0),'night',False,'2026-11-16'),
               ((2026,11,17,16,0),'day',False,'2026-11-17'),((2026,11,21,1,0),'night',True,'2026-11-20'),
               ((2026,11,22,19,0),'night',False,'2026-11-20'),((2026,11,23,1,0),'night',False,'2026-11-20')]
        for args,mode,opened,date in cases:
            ctx=session_at(datetime(*args,tzinfo=KST))
            self.assertEqual((ctx['mode'],ctx['scheduled_open'],ctx['start'][:10]),(mode,opened,date))

    def test_extended_night_hours_map_to_next_calendar_date(self):
        ctx=session_at(datetime(2026,11,17,1,0,tzinfo=KST))
        self.assertEqual(stamp_for('20261116','245900',ctx),datetime(2026,11,17,0,59,tzinfo=KST))
        self.assertEqual(stamp_for('20261117','005900',ctx),datetime(2026,11,17,0,59,tzinfo=KST))
        self.assertIsNone(stamp_for('20261116','140000',ctx))
        self.assertIsNone(stamp_for('20261115','245900',ctx))
        with self.assertRaises(TradingError):stamp_for('20261116','306000',ctx)

    def test_real_day_ohlcv_is_aggregated_to_closed_three_minute_bars(self):
        start=datetime(2026,11,16,9,0,tzinfo=KST);broker=Quotes(minutes(start,12))
        result=self.service(broker).collect(start+timedelta(minutes=12,seconds=5))
        self.assertEqual(len(result['bars']),4)
        self.assertEqual(result['bars'][0]['volume'],6)
        self.assertEqual(result['bars'][0]['open'],790)
        self.assertEqual(result['bars'][0]['close'],790.6)
        self.assertEqual(broker.calls[0][2]['FID_COND_MRKT_DIV_CODE'],'F')

    def test_night_uses_cm_extended_query_time_and_actual_night_bars(self):
        start=datetime(2026,11,17,0,54,tzinfo=KST);broker=Quotes(minutes(start,12),'night')
        result=self.service(broker).collect(start+timedelta(minutes=12,seconds=5))
        params=broker.calls[0][2]
        self.assertEqual((params['FID_COND_MRKT_DIV_CODE'],params['FID_INPUT_DATE_1'],params['FID_INPUT_HOUR_1']),('CM','20261116','250600'))
        self.assertEqual(len(result['bars']),4)
        self.assertFalse(result['stale'])
        self.assertTrue(all(r['time'].startswith('2026-11-17') for r in result['bars']))

    def test_day_response_is_never_relabelled_as_night(self):
        start=datetime(2026,11,16,14,0,tzinfo=KST);broker=Quotes(minutes(start,12))
        with self.assertRaisesRegex(TradingError,'FUTURES_SESSION_EMPTY'):
            self.service(broker).collect(datetime(2026,11,17,1,0,tzinfo=KST))
        self.assertTrue(all(c[2]['FID_COND_MRKT_DIV_CODE']=='CM' for c in broker.calls))

    def test_incomplete_and_missing_minutes_never_create_synthetic_candles(self):
        start=datetime(2026,11,16,18,0,tzinfo=KST);ctx=session_at(start)
        rows=minutes(start,6)
        self.assertEqual(len(closed_bars(rows,ctx,start+timedelta(minutes=4))),1)
        self.assertEqual(len(closed_bars(rows[:1]+rows[2:],ctx,start+timedelta(minutes=6))),1)

    def test_initial_download_is_bounded_and_later_reads_reuse_cache(self):
        start=datetime(2026,11,16,8,45,tzinfo=KST);broker=Quotes(minutes(start,360));service=self.service(broker)
        first=service.collect(start+timedelta(minutes=360,seconds=5))
        self.assertEqual(len(broker.calls),3)
        self.assertTrue(any('최근 구간' in w for w in first['warnings']))
        service.collect(start+timedelta(minutes=361,seconds=5))
        self.assertEqual(len(broker.calls),4)

    def test_contract_mismatch_and_invalid_ohlc_are_rejected(self):
        start=datetime(2026,11,16,9,0,tzinfo=KST);broker=Quotes(minutes(start,12));broker.code='A01703'
        with self.assertRaisesRegex(TradingError,'FUTURES_SYMBOL_MISMATCH'):self.service(broker).collect(start+timedelta(minutes=12))
        broker.code='A01612';broker.minutes[0]['high']=1
        with self.assertRaisesRegex(TradingError,'FUTURES_MINUTE_OHLC_INVALID'):self.service(broker).collect(start+timedelta(minutes=12))
