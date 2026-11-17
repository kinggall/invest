"""Compare KIS historical option bars and OI without issuing tokens or orders."""
import argparse
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re

from trader import (KST, SharedToken, TradingError, ReadOnlyDerivativesClient,
                    FUTURES_MINUTES, DERIVATIVE_READS)
from oi import load_index_master


OI_FIELDS = ('hts_otst_stpl_qty', 'otst_stpl_qty_icdc')
BAR_FIELDS = ('stck_bsop_date', 'stck_cntg_hour', 'futs_prpr',
              'futs_oprc', 'futs_hgpr', 'futs_lwpr', 'cntg_vol') + OI_FIELDS


def summarize(data, day, hour):
    header = data.get('output1') or {}
    rows = data.get('output2') or []
    if not isinstance(header, dict) or not isinstance(rows, list):
        raise TradingError('PROBE_RESPONSE_INVALID')
    rows = [r for r in rows if isinstance(r, dict)]
    def select(row):
        return {k: row[k] for k in BAR_FIELDS if k in row}
    return dict(request_date=day, request_time=hour,
                header_oi={k: header.get(k) for k in OI_FIELDS},
                bar_count=len(rows),
                first_bar=select(rows[0]) if rows else None,
                last_bar=select(rows[-1]) if rows else None,
                returned_dates=sorted({str(r.get('stck_bsop_date', '')) for r in rows}),
                bars_with_oi=sum(any(r.get(k) not in (None, '') for k in OI_FIELDS) for r in rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--domino-home', type=Path)
    parser.add_argument('--date', default='2026-11-16')
    parser.add_argument('--compare-date', default='2026-11-17')
    parser.add_argument('--symbol', help='Optional 9-character monthly option code')
    args = parser.parse_args()
    for day in (args.date, args.compare_date):
        datetime.strptime(day, '%Y-%m-%d')
    home = args.domino_home or os.environ.get('DOMINO_HOME')
    if not home and os.environ.get('LOCALAPPDATA'):
        settings = Path(os.environ['LOCALAPPDATA']) / 'invest' / 'settings.json'
        if settings.exists():
            home = json.loads(settings.read_text(encoding='utf-8-sig')).get('domino_home')
    if not home:
        raise TradingError('DOMINO_HOME_REQUIRED: --domino-home 경로를 지정하세요.')
    # Validate before public metadata downloads; never renew or alter the cache.
    SharedToken(home, 'real').read()
    symbol = args.symbol
    if not symbol:
        contracts = load_index_master(datetime.now(KST).date().isoformat())
        eligible = [r for r in contracts if r['side'] == 'call'
                    and r['expiry'] >= max(args.date, args.compare_date).replace('-', '')[:6]]
        if not eligible:
            raise TradingError('PROBE_OPTION_CODE_REQUIRED')
        expiry = min(r['expiry'] for r in eligible)
        symbol = min((r for r in eligible if r['expiry'] == expiry),
                     key=lambda r: abs(r['strike'] - 800))['code']
    if not re.fullmatch(r'[BC]01[A-Z0-9]{6}', symbol):
        raise TradingError('PROBE_INVALID_MONTHLY_OPTION_CODE')
    client = ReadOnlyDerivativesClient(home)
    result = dict(symbol=symbol, observed_at=datetime.now(KST).isoformat(), samples=[])
    for day in dict.fromkeys((args.date, args.compare_date)):
        for hour in ('100000', '140000'):
            payload = dict(FID_COND_MRKT_DIV_CODE='O', FID_INPUT_ISCD=symbol,
                           FID_HOUR_CLS_CODE='60', FID_PW_DATA_INCU_YN='Y',
                           FID_FAKE_TICK_INCU_YN='N',
                           FID_INPUT_DATE_1=day.replace('-', ''), FID_INPUT_HOUR_1=hour)
            try:
                data = client.request(FUTURES_MINUTES, DERIVATIVE_READS[FUTURES_MINUTES], payload)
                sample = summarize(data, day, hour)
            except TradingError as error:
                sample = dict(request_date=day, request_time=hour, error=str(error))
                result['samples'].append(sample)
                # Honor shared cooldown; do not retry an API failure in this probe.
                break
            result['samples'].append(sample)
        if result['samples'][-1].get('error'):
            break
    result['interpretation'] = (
        'Header OI is not proven historical. Identical values are inconclusive; '
        'different values can reflect live updates. Historical bar timestamps and '
        'per-bar OI must be verified before backtesting. No tokens or orders issued.')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (TradingError, ValueError, OSError):
        # Do not print configuration contents, paths, keys, tokens or traceback.
        raise SystemExit('진단을 시작하지 못했습니다. DOMINO_HOME과 기존 실전 토큰을 확인하세요.')
