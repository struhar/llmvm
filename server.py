import asyncio
import datetime as dt
import json
import os
from typing import Dict, List, Optional, cast

import async_timeout
import jsonpickle
import nest_asyncio
import openai
import rich
import uvicorn
from fastapi import (BackgroundTasks, FastAPI, HTTPException, Request,
                     UploadFile)
from fastapi.param_functions import File, Form
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from container import Container
from helpers.firefox import FirefoxHelpers
from helpers.helpers import Helpers
from helpers.logging_helpers import setup_logging
from objects import (Answer, Assistant, AstNode, Content, DownloadItem,
                     Message, MessageModel, Response, SessionThread, Statement,
                     StopNode, System, TokenStopNode, User)
from openai_executor import OpenAIExecutor
from persistent_cache import PersistentCache
from starlark_execution_controller import StarlarkExecutionController
from vector_search import VectorSearch
from vector_store import VectorStore

nest_asyncio.apply()

logging = setup_logging()

app = FastAPI()

agents = list(
    filter(
        lambda x: x is not None, [Helpers.get_callable(logging, agent) for agent in Container().get('helper_functions')]
    )
)

for agent in agents:
    rich.print(f'[green]Loaded agent: {agent.__name__}[/green]')  # type: ignore

cache_session = PersistentCache(Container().get('cache_directory') + '/session.cache')
cache_execution = PersistentCache(Container().get('cache_directory') + '/execution.cache')
cdn_directory = Container().get('cdn_directory')
if not os.path.exists(cdn_directory):
    os.makedirs(cdn_directory)


executor = OpenAIExecutor(
    openai_key=os.environ.get('OPENAI_API_KEY', ''),
    default_model=Container().get('model'),
    api_endpoint=Container().get('api_base'),
    cache=cache_execution,
    default_max_tokens=int(Container().get('max_tokens')),
)

vector_store = VectorStore(
    token_calculator=executor.calculate_tokens,
    store_filename=Container().get('vector_store_index_directory'),
    embedding_model=Container().get('vector_store_embedding_model'),
    chunk_size=int(Container().get('vector_store_chunk_size')),
    chunk_overlap=10
)

vector_search = VectorSearch(vector_store=vector_store)

controller = StarlarkExecutionController(
    executor=executor,
    agents=agents,  # type: ignore
    vector_search=vector_search,
    cache=cache_execution,
    edit_hook=None,
    continuation_passing_style=False,
    tools_model=Container().get('tools_model')
)


def __get_thread(id: int) -> SessionThread:
    if not cache_session.has_key(id) or id <= 0:
        id = cache_session.gen_key()
        thread = SessionThread(current_mode='tool', id=id)
        cache_session.set(thread.id, thread)
    return cast(SessionThread, cache_session.get(id))


async def stream_response(response):
    content = ''
    async with async_timeout.timeout(220):
        try:
            async for chunk in response:
                content += str(chunk)
                yield f"data: {jsonpickle.encode(chunk)}\n\n"
            yield "data: [DONE]"
        except asyncio.TimeoutError:
            raise HTTPException(status_code=504, detail="Stream timed out")


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    try:
        # Construct the prompt from the messages
        data = await request.json()
        messages = data.get('messages', [])
        prompt = ""
        for msg in messages:
            prompt += f'{msg["role"]}: {msg["content"]}\n'

        # Get the JSON body of the request
        if 'stream' in data and data['stream']:
            response = await openai.ChatCompletion.acreate(
                model=data['model'],
                temperature=0.0,
                max_tokens=150,
                messages=messages,
                stream=True,
            )
            return StreamingResponse(stream_response(response), media_type='text/event-stream')  # media_type="application/json")
        else:
            response = openai.ChatCompletion.create(
                model=data['model'],
                temperature=0.0,
                max_tokens=150,
                messages=messages,
                stream=False,
            )
            return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get('/cdn/{filename}')
def get_file(filename: str):
    file_path = os.path.join(cdn_directory, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail='File not found')
    return FileResponse(path=file_path)

@app.get('/search/{query}')
def search(query: str):
    results = vector_search.search(query, max_results=10, min_score=0.7)
    return results

@app.post('/injest')
async def injest(file: UploadFile = File(...), background_tasks: BackgroundTasks = BackgroundTasks()):
    try:
        name = os.path.basename(str(file.filename))

        with open(f"{cdn_directory}/{name}", "wb") as buffer:
            buffer.write(file.file.read())
            background_tasks.add_task(
                vector_search.injest_file,
                f"{cdn_directory}/{name}",
                str(file.filename),
                {}
            )
        return {"filename": file.filename, "detail": "Injestion started."}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Exception: {e}")

@app.post('/download')
async def download(
    download_item: DownloadItem,
    background_tasks: BackgroundTasks = BackgroundTasks()
):
    thread = __get_thread(download_item.id)

    if not cache_session.has_key(thread.id) or thread.id <= 0:
        temp = __get_thread(0)
        thread.id = temp.id

    queue = asyncio.Queue()

    async def callback(token: AstNode):
        queue.put_nowait(token)

    async def stream():
        async def execute_and_signal():
            stream_handler = callback
            from bcl import ContentDownloader
            downloader = ContentDownloader(
                expr=download_item.url,
                agents=[],
                messages=[],
                starlark_runtime=controller.starlark_runtime,
                original_code='',
                original_query=''
            )
            content = downloader.get()
            queue.put_nowait(StopNode())

            if content:
                background_tasks.add_task(
                    vector_search.injest_text,
                    content,
                    content[:25],
                    download_item.url,
                    {}
                )
            return content

        task = asyncio.create_task(execute_and_signal())

        while True:
            data = await queue.get()
            if isinstance(data, StopNode):
                break
            yield data

        await task
        content = task.result()
        thread.messages.append(MessageModel.from_message(User(Content(content))))
        cache_session.set(thread.id, thread)
        yield thread.model_dump()

    return StreamingResponse(stream_response(stream()), media_type='text/event-stream')  # media_type="application/json")

@app.get('/v1/chat/get_thread')
async def get_thread(id: int) -> SessionThread:
    return __get_thread(id)

@app.get('/v1/chat/get_threads')
async def get_threads() -> List[SessionThread]:
    return [cast(SessionThread, cache_session.get(id)) for id in cache_session.keys()]

@app.get('v1/chat/clear_threads')
async def clear_threads() -> None:
    for id in cache_session.keys():
        cache_session.delete(id)

@app.post('/v1/chat/tools_completions', response_model=None)
async def tools_completions(request: SessionThread):
    thread = request

    if not cache_session.has_key(thread.id) or thread.id <= 0:
        temp = __get_thread(0)
        thread.id = temp.id

    messages = [MessageModel.to_message(m) for m in thread.messages]  # type: ignore
    mode = thread.current_mode
    queue = asyncio.Queue()

    if len(messages) == 0:
        raise HTTPException(status_code=400, detail='No messages provided')

    # todo perform some merge logic of the messages here
    # I don't think we want to have the client always grab the full thread
    # before posting

    async def callback(token: AstNode):
        queue.put_nowait(token)

    async def stream():
        async def execute_and_signal():
            result = await controller.aexecute(
                messages=messages,
                temperature=thread.temperature,
                mode=mode,
                stream_handler=callback,
            )
            queue.put_nowait(StopNode())
            return result

        task = asyncio.create_task(execute_and_signal())

        while True:
            data = await queue.get()
            if isinstance(data, StopNode):
                break
            yield data

        await task
        statements: List[Statement] = task.result()

        # todo parse Answers into Assistants for now
        results = []
        for statement in statements:
            if isinstance(statement, Answer):
                results.append(Assistant(Content(str(cast(Answer, statement).result()))))
            elif isinstance(statement, Assistant):
                results.append(statement)

        if len(results) > 0:
            for result in results:
                thread.messages.append(MessageModel.from_message(result))
            cache_session.set(thread.id, thread)
            yield thread.model_dump()
        else:
            # todo need to do something here to deal with error cases
            yield thread.model_dump()

    return StreamingResponse(stream_response(stream()), media_type='text/event-stream')  # media_type="application/json")


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    rich.print(f"Unhandled error: {exc}")
    return {"detail": f"Unhandled error: {exc}"}


if __name__ == '__main__':
    config = uvicorn.Config(
        app='server:app',
        host=Container().get('server_host'),
        port=int(Container().get('server_port')),
        reload=True,
        loop='asyncio',
        reload_includes=['*.py'],
    )
    server = uvicorn.Server(config)
    server.run()