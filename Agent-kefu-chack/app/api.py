"""Local HTTP API. Start with one Uvicorn worker on 127.0.0.1."""
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from uuid import UUID
try:
    import fcntl  # Unix
except ImportError:  # Windows
    import msvcrt
    fcntl=None
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator
from app.jobs import JobStore
from app.providers import get_detector

ROOT=Path(__file__).resolve().parent.parent

class Reply(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id: str=Field(min_length=1,max_length=100)
    user_question: str=Field(min_length=1,max_length=20000)
    system_reply: str=Field(min_length=1,max_length=20000)
    knowledge_base: str=Field(min_length=1,max_length=20000)

class CheckRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    mode: Literal['mock','llm']='mock'
    items: list[Reply]=Field(min_length=1,max_length=20)
    @field_validator('items')
    @classmethod
    def unique_ids(cls,items):
        if len({x.id for x in items})!=len(items):raise ValueError('案例 ID 不得重复')
        return items

class Label(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
    id: str=Field(min_length=1,max_length=100)
    is_hallucination: StrictBool

class EvaluationRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    labels: list[Label]=Field(min_length=1,max_length=20)


def create_app(output_root=None, factory=get_detector):
    directory=Path(output_root or ROOT/'outputs/api')
    @asynccontextmanager
    async def lifespan(app):
        directory.mkdir(parents=True,exist_ok=True)
        lockfile=(directory/'.service.lock').open('a')
        try:
            if fcntl is not None:
                fcntl.flock(lockfile,fcntl.LOCK_EX|fcntl.LOCK_NB)
            else:
                msvcrt.locking(lockfile.fileno(),msvcrt.LK_NBLCK,1)
        except (BlockingIOError,OSError):
            lockfile.close();raise RuntimeError('同一任务目录只能运行一个服务进程；请使用 --workers 1')
        try:
            app.state.store=JobStore(directory,factory)
            yield
        finally:
            if hasattr(app.state,'store'):app.state.store.close()
            lockfile.close()
    app=FastAPI(title='客服回复幻觉检测 API',version='0.2.0',lifespan=lifespan,
                description='本机异步检测接口。默认 mock；mode=llm 会调用服务端配置的真实模型。单批最多20条。',
                servers=[{'url':'http://127.0.0.1:8000'}])

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        # Do not echo arbitrary input, keys or request bodies into error responses.
        errors=[{'location':list(e['loc']),'message':e['msg']} for e in exc.errors()]
        return JSONResponse(status_code=422,content={'detail':errors})

    @app.middleware('http')
    async def local_requests(request,call_next):
        # This first version is a local tool, not a public multi-user service.
        if request.client and request.client.host not in ('127.0.0.1','::1','testclient'):
            return JSONResponse(status_code=403,content={'detail':'当前服务仅允许本机调用'})
        if request.headers.get('origin') not in (None,'http://127.0.0.1:8000','http://localhost:8000'):
            return JSONResponse(status_code=403,content={'detail':'不接受其他站点的浏览器请求；请使用本机客户端'})
        return await call_next(request)

    def read(task_id):
        try:return app.state.store.get(str(task_id))
        except KeyError:raise HTTPException(404,'任务不存在')

    @app.get('/health',summary='服务健康检查')
    def health():return {'status':'ok','version':'0.2.0','max_batch_size':20,'max_running_tasks':2}

    @app.post('/api/checks',status_code=202,summary='提交单条或批量检测')
    def submit(body:CheckRequest):
        try:task_id=app.state.store.submit(body.mode,[r.model_dump() for r in body.items])
        except OverflowError as exc:raise HTTPException(429,str(exc))
        except ValueError:raise HTTPException(503,'真实模型配置缺失或无效，请检查服务端 .env')
        return {'task_id':task_id,'status':'accepted','status_url':f'/api/checks/{task_id}',
                'report_url':f'/api/checks/{task_id}/report'}

    @app.get('/api/checks/{task_id}',summary='查询任务进度及逐条结果')
    def result(task_id:UUID):
        job=read(task_id)
        job.pop('items',None)
        job['progress']={'processed':job['completed']+job['failed'],'total':job['total'],
                         'succeeded':job['completed'],'failed':job['failed']}
        return job

    @app.post('/api/checks/{task_id}/evaluate',summary='提交人工标签进行独立评估')
    def score(task_id:UUID,body:EvaluationRequest):
        read(task_id)
        try:return app.state.store.score(str(task_id),[r.model_dump() for r in body.labels])
        except RuntimeError as exc:raise HTTPException(409,str(exc))
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.get('/api/checks/{task_id}/report',summary='打开 HTML 图表报告')
    def report(task_id:UUID):
        job=read(task_id)
        if not job['report_ready']:raise HTTPException(409,'报告尚未生成；请先查询任务状态')
        return FileResponse(directory/str(task_id)/'report.html',media_type='text/html',headers={'Cache-Control':'no-store'})
    return app

app=create_app()
