"""Linux command evidence; child rusage is a maximum, not summed process-tree RSS."""
import json,resource,subprocess,sys,time
from pathlib import Path
out=Path(sys.argv[1]); command=sys.argv[2:]; start=time.monotonic()
with out.with_suffix('.log').open('w') as log:
 p=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
r=resource.getrusage(resource.RUSAGE_CHILDREN)
out.with_suffix('.json').write_text(json.dumps(dict(command=command,exit_code=p.returncode,wall_seconds=time.monotonic()-start,max_child_rss_kib=r.ru_maxrss,user_seconds=r.ru_utime,system_seconds=r.ru_stime),indent=2)+'\n')
sys.exit(p.returncode)
