from typing import Any
from dotenv import load_dotenv

load_dotenv()

import os
import asyncio
from sqlalchemy import update
from taskiq_task.utils import visual_extractor
from taskiq_redis import RedisAsyncResultBackend, RedisStreamBroker

from services.utils import get_logger, report_and_raise_500
from database import local_async_session, Project, User
from core.llm.clients import google_client_async
from services.matching.link_resolver import resolve_links

REDIS_ENDPOINT = os.environ.get("REDIS_ENDPOINT", "redis://localhost:6379")

# Setup where results are stored
result_backend: RedisAsyncResultBackend = RedisAsyncResultBackend(
    redis_url=REDIS_ENDPOINT, result_ex_time=300  # auto-delete results after 300 secs
)

# Setup the Broker
broker = RedisStreamBroker(
    url=REDIS_ENDPOINT, xread_block=3000  # check the queue every 3 sec
).with_result_backend(result_backend)


logger = get_logger(__file__)


async def _refund_token_and_mark_error(
    user_email: str, project_id: str, reason: str
) -> None:
    """Atomically refund 1 token and mark the project as failed."""
    logger.warning(
        f"Refunding token for {user_email} | project {project_id} | reason: {reason}"
    )
    async with local_async_session() as db:
        try:
            await db.execute(
                update(User)
                .where(User.email == user_email)
                .values(tokens=User.tokens + 1)
            )
            await db.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(status="extraction_error")
            )
            await db.commit()
        except Exception as db_err:
            await report_and_raise_500(db_err, context="_refund_token_and_mark_error")


@broker.task
async def data_extraction_task(
    file_uri: str, project_id: str, user_email: str
) -> dict[Any, Any]:
    try:
        file_info = await google_client_async.files.get(name=file_uri)

        while file_info.state and file_info.state.name == "PROCESSING":
            await asyncio.sleep(2)
            file_info = await google_client_async.files.get(name=file_uri)

        if not file_info.state or file_info.state.name == "FAILED":
            logger.critical(f"File Upload Failed")
            await _refund_token_and_mark_error(
                user_email=user_email,
                project_id=project_id,
                reason="File upload failed",
            )
            return {"status": "error", "data": "Something Went Wrong!"}

        extracted_data = await visual_extractor(file_uri=file_uri)
        extracted_data = await resolve_links(extracted_data)

        async with local_async_session() as db:
            stmt = (
                update(Project)
                .where(Project.id == project_id)
                .values(extracted_data=extracted_data, status="extracted")
            )
            await db.execute(stmt)
            await db.commit()

        return {"status": "success", "data": extracted_data, "project_id": project_id}

    except Exception as e:
        await _refund_token_and_mark_error(
            user_email=user_email,
            project_id=project_id,
            reason=str(e),
        )
        await report_and_raise_500(e, context="data_extraction_task")
        return {"status": "error", "data": "Something Went Wrong!"}
