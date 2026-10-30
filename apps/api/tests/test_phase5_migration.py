"""Pinned pre-Phase5 schema8 reader, authoritative schema9 migration/rollback guard."""
import hashlib,json,os,sqlite3,subprocess
from pathlib import Path
import pytest
from test_supervised_runtime import BIN
pytestmark=pytest.mark.skipif(os.getenv('KANE_PHASE5_TEST')!='1',reason='opt-in Phase5 pinned schema8 migration')

def test_schema8_upgrades_without_changing_old_state_or_allowing_old_reader(tmp_path):
    old=Path(os.environ['KANE_PHASE5_SCHEMA8_BINARY'])
    assert old.is_file() and hashlib.sha256(old.read_bytes()).hexdigest()=='fbaac2caf1cdb564e78a31b13a7a5f1ed63a6358d6f0515948c1ce02a2694e4c'
    database=tmp_path/'core.db'
    def call(binary,body):return subprocess.run([str(binary),'--db',str(database),'host-request'],input=json.dumps(body),text=True,capture_output=True)
    initialized=call(old,{'op':'create_workspace','kind':'document'});assert initialized.returncode==0,initialized.stderr
    sql=sqlite3.connect(database);rows=sql.execute('SELECT * FROM workspace').fetchall();events=sql.execute('SELECT * FROM ledger ORDER BY seq').fetchall();assert sql.execute('PRAGMA user_version').fetchone()[0]==8;sql.close()
    upgraded=call(BIN,{'op':'health'});assert upgraded.returncode==0 and json.loads(upgraded.stdout)['schema']==9
    rejected=call(old,{'op':'health'});assert rejected.returncode!=0
    sql=sqlite3.connect(database);assert sql.execute('PRAGMA user_version').fetchone()[0]==9;assert sql.execute('SELECT * FROM workspace').fetchall()==rows;assert sql.execute('SELECT * FROM ledger ORDER BY seq').fetchall()==events;assert sql.execute('PRAGMA foreign_key_check').fetchall()==[];sql.close()
    (tmp_path/'schema8-to-9.json').write_text(json.dumps({'old_sha256':hashlib.sha256(old.read_bytes()).hexdigest(),'new_sha256':hashlib.sha256(BIN.read_bytes()).hexdigest(),'old_reader_rejected':True,'workspace_and_ledger_unchanged':True},indent=2))
