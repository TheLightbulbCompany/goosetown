"""Loopback synthetic AWS gateway around the actual GooseTown router and Moto."""
import asyncio, os, sys, uuid
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'apps/backend'))
os.environ.update(AWS_ACCESS_KEY_ID='testing', AWS_SECRET_ACCESS_KEY='testing', AWS_SESSION_TOKEN='testing', AWS_DEFAULT_REGION='us-east-1', AWS_EC2_METADATA_DISABLED='true', DYNAMODB_TABLE_PREFIX='goosetown', ENVIRONMENT='dev', RUNTIME_BINDING_ENABLED='true', RUNTIME_SESSION_ENFORCEMENT_ENABLED='true', RUNTIME_BINDING_ORIGIN='http://127.0.0.1:8877', TOWN_TOKEN_SECRET='test-secret', WS_LAMBDA_SECRET='fixture-secret', PIXELLAB_API_KEY='', SPRITE_S3_BUCKET='', SPRITE_CDN_URL='', GAME_SERVER_URL='ws://127.0.0.1:8080', TOWN_WS_URL='ws://127.0.0.1:8877/ws', TOWN_API_URL='http://127.0.0.1:8877/api/v1')
import boto3, httpx
from moto import mock_aws
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from tests.conftest import _create_tables
mock = mock_aws(); mock.start(); _create_tables(boto3.resource('dynamodb',region_name='us-east-1'))
from core.services.town_service import TownService
from core.services import daemon_mailbox, sqs_drainer
from core.services.daemon_mailbox import DaemonMailbox
from core.town_token import verify_town_token
from routers.town import router
from core.auth import get_current_user, AuthContext
from core.config import settings
from core.runtime_enrollment import mint_join_token
from routers import runtime_session
runtime_session.TOKEN_TTL = 65  # fixture-only fast renewal
sqs_drainer.create_agent_queue = lambda agent_id: 'fixture-disabled'
async def no_drain(*args, **kwargs): return 0
sqs_drainer.drain_for_agent = no_drain
owner = TownService().create_instance('fixture-owner')
other = TownService().create_instance('fixture-other')
app = FastAPI(); app.include_router(router, prefix='/api/v1/town')
app.dependency_overrides[get_current_user] = lambda: AuthContext(user_id='fixture-owner')
counts = {'connects': 0, 'moves': 0, 'denied': 0}

queues = {}; loop = None
class Delivery:
    def send_message(self, connection_id, payload):
        loop.call_soon_threadsafe(queues[connection_id].put_nowait, payload)
        return True
    def close_connection(self, connection_id):
        pass
daemon_mailbox._singleton = DaemonMailbox(mgmt=Delivery())
@app.on_event('startup')
async def start():
    global loop
    loop = asyncio.get_running_loop()
@app.get('/fixture')
async def fixture():
    counts.update(connects=0, moves=0, denied=0)
    return {'fixture':'runtime-world-moto-loopback', 'token':owner['townToken'], 'join_token':mint_join_token(owner['id'],'fixture-owner',owner['townToken'])}
@app.get('/fixture/counts')
async def stats(): return counts

@app.post('/fixture/unlink/{name}')
async def unlink_fixture(name: str):
    agent = TownService().get_agent_by_name('fixture-owner',name)
    if not agent: return {'error':'not found'}
    table = __import__('core.dynamo',fromlist=['agents_table']).agents_table()
    table.update_item(Key={'id':agent['id']},UpdateExpression='SET bindingRevision = :r REMOVE runtimeBinding',ExpressionAttributeValues={':r':int(agent.get('bindingRevision',0))+1})
    return {'ok':True}

@app.post('/fixture/ready/{name}')
async def ready(name: str):
    a=TownService().get_agent_by_name('fixture-owner', name)
    if not a: return {'error':'not found'}
    TownService().update_agent_state(a['id'], sprite_ready=True, onboarding_phase='alive')
    TownService().update_agent_context(a['id'], 'town','plaza')
    return {'agent_id':a['id']}
@app.websocket('/ws')
async def ws_gateway(ws: WebSocket):
    token=ws.query_params.get('token','')
    payload=verify_town_token(token)
    if not payload:
        await ws.close(code=1008); return
    await ws.accept(); cid='fixture-'+uuid.uuid4().hex; queues[cid]=asyncio.Queue()
    transport=httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,base_url='http://local') as client:
        async def send():
            while True: await ws.send_json(await queues[cid].get())
        sender=asyncio.create_task(send())
        try:
            while True:
                body=await ws.receive_json()
                result=await client.post('/api/v1/town/ws-message',headers={'x-ws-lambda-secret':'fixture-secret'},json={'connectionId':cid,'principalId':payload['uid'],'body':body})
                if body.get('type') == 'town_agent_connect' and result.status_code == 200 and result.json().get('statusCode') == 200: counts['connects'] += 1
                if body.get('type') == 'town_agent_act' and body.get('action') == 'move' and result.status_code == 200 and result.json().get('statusCode') == 200: counts['moves'] += 1
                if result.status_code == 200 and result.json().get('statusCode') == 401: counts['denied'] += 1
                if result.status_code != 200 or result.json().get('statusCode') != 200:
                    print('FORWARD_REJECT',result.status_code,flush=True)
        except WebSocketDisconnect: pass
        finally:
            await client.post('/api/v1/town/ws-disconnect',headers={'x-ws-lambda-secret':'fixture-secret'},json={'connectionId':cid})
            sender.cancel(); await asyncio.gather(sender,return_exceptions=True); queues.pop(cid,None)
@app.middleware('http')
async def synthetic_sprite_fixture(request, call_next):
    if request.url.path == '/api/v1/town/agent/register':
        data = await request.json()
        response = await call_next(request)
        if response.status_code == 200:
            a = TownService().get_agent_by_name('fixture-owner', data['agent_name'])
            if a:
                TownService().update_agent_state(a['id'], sprite_ready=True, onboarding_phase='alive')
                TownService().update_agent_context(a['id'], 'town', 'plaza')
        return response
    return await call_next(request)
