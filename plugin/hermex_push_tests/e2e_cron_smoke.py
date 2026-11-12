"""Hermes 0.23.1 integration smoke. Run with a disposable /tmp HERMES_HOME and Hermes on PYTHONPATH.

The model and push relay are loopback HTTP fixtures; the actual scheduler, AIAgent,
platform registry, sender and SessionDB run unchanged. Only Discord's network leaf
is captured. The owner-store context is bound directly, so this does not test gateway
RPC admission, the daemon ticker, APNs, or physical-phone presentation.
"""
import os
from pathlib import Path
import tempfile
assert Path(os.environ['HERMES_HOME']).resolve().is_relative_to(Path('/tmp').resolve()), 'Use a disposable HERMES_HOME under /tmp'
os.environ['HERMES_RUNTIME_DIR'] = str(Path(os.environ['HERMES_HOME'])/'test-runtime')
import hermes_bootstrap
import asyncio
import http.server
import importlib.util
import json
import sys
import threading
import uuid
import types

PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN))
from hermex_push.keys import load_or_create_keys
from hermex_push.privacy import unseal
from hermex_push.hooks import HermexPush
from hermes_state import SessionDB
from cron import scheduler
from gateway.session_cron import bind_execution, _execution

capture=[]
REPORT='# Daily summary\n' + 'A complete long report line.\n'*100
class Capture(http.server.BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if '/notify' in self.path:
            capture.append(body)
            response={'result':'accepted'}
        else:
            response={'id':'test-completion','object':'chat.completion','created':123,'model':'fixture-model',
                      'choices':[{'index':0,'message':{'role':'assistant','content':REPORT},'finish_reason':'stop'}],
                      'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
        if body.get('stream'):
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            chunk={'id':'fixture-stream','object':'chat.completion.chunk','created':123,'model':'fixture-model',
                   'choices':[{'index':0,'delta':{'role':'assistant','content':REPORT},'finish_reason':None}]}
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\n').encode())
            chunk['choices']=[{'index':0,'delta':{},'finish_reason':'stop'}]
            self.wfile.write(('data: '+json.dumps(chunk)+'\n\ndata: [DONE]\n\n').encode())
            return
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers()
        self.wfile.write(json.dumps(response).encode())
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Capture)
threading.Thread(target=server.serve_forever,daemon=True).start()
url=f'http://127.0.0.1:{server.server_port}'
root=Path(os.environ['HERMES_HOME']);root.mkdir(parents=True,exist_ok=True)
(root/'.env').write_text(f'HERMEX_PUSH_RELAY_URL={url}\nHERMEX_HOME_CHANNEL=default\nOPENAI_API_KEY=fixture-only\n')
(root/'config.yaml').write_text(f'''model:
  default: fixture-model
  provider: custom
  base_url: {url}/v1
  api_key: fixture-only
platforms:
  discord:
    enabled: true
    token: fixture-only
plugins:
  enabled: [hermex-push]
cron:
  preflight: false
  wrap_response: true
terminal:
  backend: local
''')
(root/'plugins').mkdir(exist_ok=True)

if not (root/'plugins/hermex-push').exists():
    (root/'plugins/hermex-push').symlink_to(PLUGIN,target_is_directory=True)
os.environ['HERMEX_PUSH_RELAY_URL']=url
os.environ['HERMEX_HOME_CHANNEL']='default'
os.environ['OPENAI_API_KEY']='fixture-only'
os.environ['HERMES_INFERENCE_PROVIDER']='custom'
os.environ['OPENAI_BASE_URL']=url+'/v1'
from hermes_cli.plugins import discover_plugins
discover_plugins()
from gateway.platform_registry import platform_registry
assert platform_registry.get('hermex').standalone_sender_fn
from cron.jobs import create_job
from cron.execution_identity import enter_cron_execution, exit_cron_execution

try:
    job=create_job(prompt='Write a report.',schedule='every 1h',name='Morning report',deliver='hermex',
                   model='fixture-model',provider='custom',base_url=url+'/v1',enabled_toolsets=[])
    eid=uuid.uuid4().hex
    sid='local-'+uuid.uuid4().hex
    from hermes_state_registry import acquire
    owner_db=acquire(root/'state.db')
    owner_db.create_session(sid,source='cron')
    token=bind_execution(types.SimpleNamespace(db=owner_db),sid,job['id'],eid)
    identity=enter_cron_execution(job,eid,{})
    try:
        success,output,result,error=scheduler.run_job(job,execution_id=eid)
        print('AGENT',success, 'error=',error, flush=True)
        assert success and result.strip()==REPORT.strip(),(error,result)
        assert capture==[], 'ordinary cron hooks pushed unexpectedly'
        err=scheduler._deliver_result(job,result)
        if err:
            from hermex_push.delivery import _deliver
            _deliver('default',result,{},None)
        assert err is None,err
    finally:
        _execution.reset(token);exit_cron_execution(identity)
    assert len(capture)==1 and capture[0]['kind']=='delivery'
    print('agent delivery OK',flush=True)
    (root/'scripts').mkdir(exist_ok=True)
    script=root/'scripts/report.sh';script.write_text("#!/bin/sh\nprintf 'Script report line\\n%.0s' $(seq 1 100)\n");script.chmod(0o700)
    sjob=create_job(prompt=None,schedule='every 1h',name='Script report',deliver='hermex',script=str(script),no_agent=True)
    success,output,result,error=scheduler.run_job(sjob)
    assert success,(error,output)
    assert scheduler._deliver_result(sjob,result) is None
    assert len(capture)==2
    keys=load_or_create_keys()
    for event in capture:
        inner=unseal(event['sealed'],preview_key=keys.preview_key,install_key=keys.install_key)
        db=SessionDB();messages=db.get_messages(event['session_id']);db.close()
        assert len(messages[-1]['content'])>400
        assert inner['subtitle'] in ('Morning report','Script report')
        expected_profile=root.name if root.parent.name=='profiles' else ''
        assert inner['profile']==expected_profile,(inner['profile'],expected_profile)
        assert len(inner['body'])<=400
    # Only the Discord network leaf is replaced; Hermes resolves/deduplicates both targets.
    from cron.scheduler_delivery import _resolve_delivery_targets
    multi=dict(sjob, deliver='discord:123456789,hermex,hermex')
    targets=_resolve_delivery_targets(multi)
    assert [(t['platform'],str(t['chat_id'])) for t in targets]==[('discord','123456789'),('hermex','default')],targets
    import tools.send_message_tool as sends
    original=sends._send_to_platform
    discord=[]
    async def capture_discord(platform,pconfig,chat_id,message,**kwargs):
        if str(getattr(platform,'value',platform))=='discord':
            discord.append((chat_id,message))
            return {'success':True,'message_id':'discord-capture'}
        return await original(platform,pconfig,chat_id,message,**kwargs)
    sends._send_to_platform=capture_discord
    try:
        multi_error=scheduler._deliver_result(multi,result)
        assert multi_error is None,multi_error
    finally:
        sends._send_to_platform=original
    assert len(capture)==3 and len(discord)==1
    # Exercise a real live scheduler lane and its standalone handoff with media.
    from gateway.config import load_gateway_config, Platform
    from hermex_push.adapter import make_adapter
    from hermex_push.delivery import MEDIA_NOTICE
    config=load_gateway_config()
    platform=Platform('hermex')
    adapter=make_adapter(config.platforms[platform])
    long_body='Live report\n'+'complete line\n'*600+'TAIL_SENTINEL'
    attachment=root/'report.txt'
    attachment.write_text('fixture attachment')
    media_message=long_body+'\nMEDIA:'+str(attachment)
    loop=asyncio.new_event_loop()
    ready=threading.Event()
    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
    live_thread=threading.Thread(target=run_loop,daemon=True)
    live_thread.start()
    assert ready.wait(5)
    try:
        live_error=scheduler._deliver_result(sjob,media_message,adapters={platform:adapter},loop=loop)
        assert live_error is None,live_error
    finally:
        loop.call_soon_threadsafe(loop.stop)
        live_thread.join(5)
        loop.close()
    assert len(capture)==4
    media_preview=unseal(capture[-1]["sealed"],preview_key=keys.preview_key,install_key=keys.install_key)
    assert MEDIA_NOTICE in media_preview["body"]
    db=SessionDB()
    live_output=db.get_messages(capture[-1]['session_id'])[-1]['content']
    assert long_body in live_output and live_output.count(MEDIA_NOTICE)==1
    assert str(attachment) not in live_output
    # The gateway-absent path must deliver the same one snapshot and attachment notice.
    assert scheduler._deliver_result(sjob,media_message) is None
    assert len(capture)==5
    standalone_output=db.get_messages(capture[-1]['session_id'])[-1]['content']
    db.close()
    assert standalone_output==live_output
    print('PASS agent + script + multi-target + live/standalone >4K MEDIA parity; five sealed deliveries',flush=True)

finally:
    server.shutdown();server.server_close()
