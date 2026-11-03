"""Public source checks and a migration rehearsal on a disposable SQLite copy.

No model calls, holdings printed, service restart, or writes to the source database.
Run on the intended server: python scripts/portfolio_preflight.py --host-label gcp
--runtime-db /opt/jarvis/.jarvis_runtime.db --output /tmp/portfolio-preflight.json
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
from contextlib import closing
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def source_checks(report: dict) -> dict:
    checks={}
    for symbol in ('NVDA','TTWO','TSLA'):
        comp=next((c for c in report.get('comparisons',[]) if c['symbol']==symbol and c['source']=='yahoo'),None)
        checks[symbol]=bool(comp and comp['exchange_overlap']>=20 and comp['max_difference'] is not None
                            and Decimal(comp['max_difference'])<=Decimal('0.005'))
    fx=next((r for r in report.get('results',[]) if r['source']=='ecb'),{})
    rates=fx.get('closes',[])
    checks['ECB']=bool(len(rates)>=20 and all(Decimal(r['close']).is_finite() and Decimal(r['close'])>0 for r in rates))
    return {'ok':all(checks.values()),'checks':checks,'measurement':report}


def _facts(conn) -> dict:
    """Compare the actual immutable rows privately; report only preservation."""
    result={}
    for table in ('instruments','trades','marks','fx','theses','thesis_events'):
        if conn.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?',(table,)).fetchone():
            rows=[tuple(r) for r in conn.execute(f'SELECT * FROM {table} ORDER BY rowid')]
            result[table]=hashlib.sha256(json.dumps(rows,default=str).encode()).digest()
    return result


def database_check(path: Path | None) -> dict:
    from backend.storage import db, portfolio
    mode='snapshot' if path else 'empty_schema'
    old_path=os.environ.get('JARVIS_DB_PATH')
    try:
        with tempfile.TemporaryDirectory(prefix='jarvis-portfolio-preflight-') as temp:
            scratch=Path(temp)/'runtime.db'
            before={}
            if path:
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as source:
                    source.execute('BEGIN')  # one consistent snapshot while the live app writes
                    before=_facts(source)
                    dest=sqlite3.connect(scratch)
                    try:
                        source.backup(dest)
                    finally:
                        dest.close()
            db.close_all()
            os.environ['JARVIS_DB_PATH']=str(scratch)
            try:
                conn=portfolio.connection()
                preserved=all(_facts(conn).get(table)==digest for table,digest in before.items())
                integrity=conn.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                foreign_keys=not conn.execute('PRAGMA foreign_key_check').fetchall()
                conn.execute('SELECT * FROM v_positions').fetchall()
                conn.execute('SELECT * FROM v_portfolio_daily LIMIT 1').fetchall()
                triggers=conn.execute("SELECT count(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'portfolio_%'").fetchone()[0]>=10
                return {'ok':preserved and integrity and foreign_keys and triggers,'mode':mode,
                        'facts_preserved':preserved,'integrity':integrity,'foreign_keys':foreign_keys,
                        'views_readable':True,'append_only_triggers':triggers}
            finally:
                db.close_all()
                if old_path is None:
                    os.environ.pop('JARVIS_DB_PATH',None)
                else:
                    os.environ['JARVIS_DB_PATH']=old_path
    except Exception as exc:
        return {'ok':False,'mode':mode,'error_type':type(exc).__name__}


def main():
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument('--host-label',choices=('desk','gcp'),default='desk')
    args.add_argument('--runtime-db',type=Path)
    args.add_argument('--offline',action='store_true',help='Only rehearse schema migration; source readiness remains unverified.')
    args.add_argument('--output',type=Path)
    options=args.parse_args()
    report={'host':platform.system(),'host_label':options.host_label,'database':database_check(options.runtime_db)}
    if options.offline:
        report['sources']={'ok':None,'state':'not_checked'}
    else:
        try:
            from scripts.measure_portfolio_sources import measure
            report['sources']=source_checks(asyncio.run(measure()))
        except Exception as exc:
            report['sources']={'ok':False,'error_type':type(exc).__name__}
    source_ok=report['sources']['ok'] is True
    report['source_validation_complete']=source_ok
    report['gcp_sources_verified']=bool(source_ok and options.host_label=='gcp' and platform.system()=='Linux')
    if 'measurement' in report['sources']:
        report['sources']['measurement']['gcp_verified']=report['gcp_sources_verified']
    report['ready_for_server_rollout']=bool(source_ok and report['database']['ok']
        and report['database']['mode']=='snapshot' and options.host_label=='gcp' and platform.system()=='Linux')
    if options.output:
        options.output.parent.mkdir(parents=True,exist_ok=True)
        options.output.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    summary={**report,'sources':{k:v for k,v in report['sources'].items() if k!='measurement'}} if options.output else report
    print(json.dumps(summary,indent=2))
    return 0 if report['database']['ok'] and (options.offline or source_ok) else 1


if __name__=='__main__':
    raise SystemExit(main())
