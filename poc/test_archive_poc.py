"""Isolated synthetic preservation contracts; run from repository root."""
import copy,hashlib,json,os,sqlite3,subprocess,sys,tempfile,time,unittest,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=Path(os.environ.get('POC_EVIDENCE','/workspace/poc-evidence'))
EVIDENCE.mkdir(parents=True,exist_ok=True)

def fixture():
 def node(i,parent,children,text):
  return dict(id=i,parent=parent,children=children,message=dict(id='m-'+i,author={'role':'assistant'},content={'content_type':'text','parts':[text]},metadata={'custom_message':{'retained':True}}),custom_node='sentinel')
 return dict(id='synthetic-a',title='Synthetic archive',create_time=1,update_time=2,current_node='b',custom_top='sentinel',mapping={'r':node('r',None,['a','b'],'root'),'a':node('a','r',[],'branch a'),'b':node('b','r',[],'branch b')})

class ArchivePOC(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='archive-poc-'); self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name); self.db=self.root/'archive.db'
 def export(self,name,items,attachment=True):
  p=self.root/(name+'.zip')
  with zipfile.ZipFile(p,'w') as z:
   z.writestr('conversations.json',json.dumps(items))
   if attachment:z.writestr('attachments/synthetic.bin',b'\x00synthetic-binary\xff')
  return p
 def command(self,*args,expected=0):
  cmd=[sys.executable,str(ROOT/'chatgpt_archive.py'),*map(str,args)]
  start=time.monotonic(); p=subprocess.run(cmd,cwd=ROOT,capture_output=True,text=True)
  record=dict(test=self.id(),command=cmd,exit_code=p.returncode,wall_seconds=time.monotonic()-start,stdout=p.stdout,stderr=p.stderr)
  with (EVIDENCE/'commands.jsonl').open('a') as f:f.write(json.dumps(record)+'\n')
  self.assertEqual(p.returncode,expected,p.stderr+p.stdout)
  return p
 def load(self,p,db=None,expected=0): return self.command('import','--db',db or self.db,'--input',p,'--rebuild-fts',expected=expected)
 def rows(self,sql,db=None):
  with sqlite3.connect(db or self.db) as c:return c.execute(sql).fetchall()
 def canonical(self,db=None):
  return self.rows('SELECT conversation_id,title,current_node FROM conversations ORDER BY 1',db),self.rows('SELECT conversation_id,node_id,parent_node_id,children_json,content_text,raw_message_json FROM conversation_nodes ORDER BY 1,2',db)
 def test_initial_import(self):
  p=self.export('v1',[fixture()]); before=p.read_bytes(); self.load(p)
  self.assertEqual(self.rows('select count(*) from conversation_nodes'),[(3,)])
  self.assertEqual(p.read_bytes(),before); self.command('verify','--db',self.db)
  with (EVIDENCE/'sizes.jsonl').open('a') as f:f.write(json.dumps(dict(test=self.id(),zip_bytes=p.stat().st_size,db_bytes=self.db.stat().st_size,sidecars={x.name:x.stat().st_size for x in self.root.glob('*.db-*')}))+'\n')
 def test_same_export_twice(self):
  p=self.export('v1',[fixture()]); self.load(p); before=self.canonical(); self.load(p)
  self.assertEqual(before,self.canonical()); self.assertEqual(self.rows('select count(*) from import_runs'),[(2,)])
 def test_update_replaces_history(self):
  v=fixture(); self.load(self.export('v1',[v])); v['mapping'].pop('a'); v['mapping']['r']['children']=['b']; v['mapping']['b']['message']['content']['parts']=['edited']; v['update_time']=3
  self.load(self.export('v2',[v])); self.assertEqual(self.rows('select count(*) from conversation_nodes'),[(2,)])
  self.assertEqual(self.rows("select content_text from conversation_nodes where node_id='b'"),[('edited',)])
 def test_missing_conversation_retained(self):
  a=fixture(); b=fixture(); b['id']='synthetic-b'; self.load(self.export('both',[a,b])); self.load(self.export('one',[a])); self.assertEqual(self.rows('select count(*) from conversations'),[(2,)])
 def test_branches(self):
  self.load(self.export('v1',[fixture()])); self.assertEqual(self.rows('select node_id,is_on_current_path from conversation_nodes order by node_id'),[('a',0),('b',1),('r',1)])
 def test_bad_element_tolerated(self):
  self.load(self.export('partial',[None,42,fixture()])); self.assertEqual(self.rows('select count(*) from conversations'),[(1,)])
  self.assertGreater(self.rows('select count(*) from import_warnings')[0][0],0)
 def test_truncated_json_rolls_back(self):
  p=self.export('v1',[fixture()]); self.load(p); before=self.canonical()
  v=fixture();v['title']='must not commit'
  bad=self.root/'broken.zip'
  with zipfile.ZipFile(bad,'w') as z:z.writestr('conversations.json','['+json.dumps(v)+',')
  self.load(bad,expected=2); self.assertEqual(before,self.canonical()); self.command('verify','--db',self.db)
 def test_crc_corruption_rejected(self):
  p=self.export('crc',[fixture()]); data=bytearray(p.read_bytes()); pos=data.index(b'Synthetic archive'); data[pos]^=1;p.write_bytes(data)
  self.load(p,expected=2)
 def test_raw_multimodal_and_metadata_limits(self):
  v=fixture();v['mapping']['b']['message']['content']={'content_type':'multimodal_text','parts':['caption',{'content_type':'image_asset_pointer','asset_pointer':'file-service://synthetic'}]}
  self.load(self.export('raw',[v])); raw=json.loads(self.rows("select raw_message_json from conversation_nodes where node_id='b'")[0][0])
  self.assertEqual(raw,v['mapping']['b']['message'])
  self.assertNotIn('custom_top',json.loads(self.rows('select metadata_json from conversations')[0][0]))
  self.assertNotIn('custom_node',json.loads(self.rows("select metadata_json from conversation_nodes where node_id='b'")[0][0]))
  self.assertEqual(self.rows("select count(*) from file_index where source_path='attachments/synthetic.bin'"),[(1,)])
  self.assertEqual(self.rows("select count(*) from sqlite_master where type='table' and name like '%attachment%'")[0][0],0)
 def test_online_sqlite_backup_restore(self):
  self.load(self.export('v1',[fixture()])); backup=self.root/'backup.db'
  with sqlite3.connect(self.db) as src,sqlite3.connect(backup) as dst:src.backup(dst)
  self.assertEqual(self.canonical(),self.canonical(backup)); self.assertEqual(self.rows('pragma integrity_check',backup),[('ok',)])
  self.command('verify','--db',backup)
  with (EVIDENCE/'sizes.jsonl').open('a') as f:f.write(json.dumps(dict(test=self.id(),db_bytes=self.db.stat().st_size,backup_bytes=backup.stat().st_size))+'\n')
 def test_snapshot_reconstruction_and_downgrade(self):
  v=fixture(); old=self.export('v1',[v]); oldhash=hashlib.sha256(old.read_bytes()).hexdigest(); self.load(old); initial=self.canonical()
  v['mapping']['b']['message']['content']['parts']=['new']; self.load(self.export('v2',[v])); self.assertNotEqual(initial,self.canonical())
  restored=self.root/'restored.db';self.load(old,db=restored);self.assertEqual(initial,self.canonical(restored)); self.command('verify','--db',restored)
  self.load(old);self.assertEqual(initial,self.canonical());self.assertEqual(hashlib.sha256(old.read_bytes()).hexdigest(),oldhash)

if __name__=='__main__':unittest.main(verbosity=2)
