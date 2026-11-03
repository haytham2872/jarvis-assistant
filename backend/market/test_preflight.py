import sqlite3
from pathlib import Path

from scripts.portfolio_preflight import database_check,source_checks


def test_preflight_migrates_only_a_copy_of_the_database(tmp_path):
    from backend.storage import portfolio as S
    from backend.brain import portfolio as P
    S.record_trades([{'instrument':P.resolve_instrument('NVDA'),'side':'buy','shares':'10','price':'100',
        'currency':'USD','fees':'0','native_price':'100','native_fees':'0','traded_on':'2026-09-01',
        'quote':'private fixture quote','dedup_key':'fixture'}],run_id='fixture')
    source=tmp_path/'source.db'
    dest=sqlite3.connect(source)
    try:
        S.connection().backup(dest)
    finally:
        dest.close()
    original=source.read_bytes()
    report=database_check(source)
    assert report['ok'] and report['facts_preserved'] and report['mode']=='snapshot'
    assert source.read_bytes()==original
    assert 'private fixture quote' not in str(report) and 'NVDA' not in str(report)


def test_missing_database_is_not_created(tmp_path):
    missing=tmp_path/'absent.db'
    assert not database_check(missing)['ok']
    assert not missing.exists()


def test_empty_schema_check_is_not_a_real_database_rehearsal():
    result=database_check(None)
    assert result['ok'] and result['mode']=='empty_schema'


def test_source_validation_requires_all_twenty_exchange_comparisons():
    report={'comparisons':[{'symbol':s,'source':'yahoo','exchange_overlap':20,'max_difference':'0.0049'}
                           for s in ('NVDA','TTWO','TSLA')],
            'results':[{'source':'ecb','closes':[{'date':f'2026-09-{d:02}','close':'0.9'} for d in range(1,21)]}]}
    assert source_checks(report)['ok']
    report['comparisons'][0]['exchange_overlap']=19
    assert not source_checks(report)['ok']
    report['comparisons'][0]['exchange_overlap']=20
    report['comparisons'][1]['max_difference']='0.01'
    assert not source_checks(report)['ok']
