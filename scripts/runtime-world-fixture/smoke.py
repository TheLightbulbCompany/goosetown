"""Synthetic local CLI -> actual backend handlers -> Godot, with renewal and unlink."""
import asyncio, json, os, signal, subprocess, sys, tempfile, time, uuid
from pathlib import Path
import urllib.request
import websockets
ROOT=Path(__file__).resolve().parents[2]
API='http://127.0.0.1:8877/api/v1'; BASE='http://127.0.0.1:8877'
PYTHON='/Users/prasiddhaparthsarthy/Desktop/goosetown.nosync/apps/goosetown-cli/.venv/bin/python'

def request(path,post=False):
    req=urllib.request.Request(BASE+path,data=b'{}' if post else None,headers={'Content-Type':'application/json'} if post else {},method='POST' if post else 'GET')
    with urllib.request.urlopen(req,timeout=5) as r:return json.load(r)

def main():
    workspace=Path(tempfile.mkdtemp(prefix='gt-runtime-world-'));name='fixture-'+uuid.uuid4().hex[:12]
    env=os.environ.copy(); env.update(AGENT_DIR=str(workspace),TOWN_API_URL=API,TOWN_WS_URL='ws://127.0.0.1:8877/ws',PYTHONPATH=str(ROOT/'apps/goosetown-cli/src'),PATH=str(ROOT/'../cli-binding-proof/target/debug')+os.pathsep+os.environ.get('PATH',''),LIGHTBULB_CONNECT_DEBUG_NO_DEFAULT_RELAYS='1',LIGHTBULB_CONNECT_DEBUG_NO_TELEMETRY='1',LIGHTBULB_API='http://127.0.0.1:9')
    health=request('/fixture');assert health['fixture']=='runtime-world-moto-loopback'
    def cli(*args,allow_failure=False):
        p=subprocess.run([PYTHON,'-m','goosetown',*args],cwd=workspace,env=env,capture_output=True,text=True,timeout=90)
        if p.returncode and not allow_failure:raise RuntimeError('CLI '+args[0]+' failed: '+p.stderr[:400]+' '+p.stdout[:400])
        try:return json.loads(p.stdout)
        except json.JSONDecodeError:return {'exit':p.returncode,'output':p.stdout[:300],'stderr':p.stderr[:300]}
    try:
        join=cli('join',health['join_token'],'--name',name,'--appearance','fixture','--harness','none');assert join.get('connected') is True,join
        state=cli('status');agent_id=state['agent']['id'];assert state['agent']['location']=='plaza',state
        move=cli('act','move','cafe');assert move.get('status')=='arrived' and move.get('location')=='cafe',move
        async def observe(loc):
            async with websockets.connect('ws://127.0.0.1:8080') as ws:
                await ws.send(json.dumps({'type':'viewer'}))
                for _ in range(8):
                    frame=json.loads(await asyncio.wait_for(ws.recv(),timeout=5))
                    if any(a.get('id')==agent_id and a.get('location')==loc for a in frame.get('agents',[])):return
                raise AssertionError('Godot viewer did not observe requested location')
        asyncio.run(observe('cafe'))
        deadline=time.monotonic()+35
        while request('/fixture/counts')['connects']<2:
            if time.monotonic()>=deadline:raise AssertionError('renewal did not reconnect')
            time.sleep(.25)
        second=cli('act','idle');assert second.get('status') == 'ok',second
        asyncio.run(observe('cafe'))
        assert request('/fixture/unlink/'+name,True)['ok'] is True
        denied=cli('act','idle',allow_failure=True)
        deadline=time.monotonic()+10
        while request('/fixture/counts')['denied']<1 and time.monotonic()<deadline:time.sleep(.1)
        counts=request('/fixture/counts');assert counts['denied']>=1,(denied,counts)
        assert denied.get('status')!='accepted',denied
        leave=cli('leave',allow_failure=True)
        print(json.dumps({'agent':name,'connected':True,'cafe_observed':True,'renewal_connects':counts['connects'],'idle_after_renewal':second,'unlink_denied':True,'denied_result':denied,'leave_result':leave}))
    finally:
        cli('leave',allow_failure=True)
        pidfile=Path('/tmp/goosetown')/name/'daemon.pid'
        if pidfile.exists():
            try:
                pid=int(pidfile.read_text().strip()); details=subprocess.run(['ps','eww','-p',str(pid),'-o','command='],capture_output=True,text=True,timeout=3).stdout
                if 'goosetown _daemon' in details and f'TOWN_AGENT={name}' in details and f'TOWN_WORKSPACE={workspace}' in details:os.kill(pid,signal.SIGTERM)
            except (OSError,ValueError,subprocess.SubprocessError):pass

if __name__=='__main__':main()
