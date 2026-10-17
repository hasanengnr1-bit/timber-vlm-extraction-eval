import os
import asyncio
from typing import Any
from google.genai import types

from services.utils import get_logger
from core.llm.clients import google_client_async
from core.schema import (
    ShearWallData,
    RoofSystemData,
    WallSystemData,
    PostData,
    FloorSystemData,
    FootingSystemData,
    ProjectContextData,
)
from core.llm.prompts import (
    SYS_PROMPT_FLOOR,
    SYS_PROMPT_FOOTING,
    SYS_PROMPT_POST,
    SYS_PROMPT_ROOF,
    SYS_PROMPT_SHEAR_WALL,
    SYS_PROMPT_WALL,
    SYS_PROMPT_PROJECT_CONTEXT,
)

logger = get_logger(__name__)

GEMINI_MODEL_NAME = os.environ["GEMINI_MODEL_NAME"]


async def fetch_with_gemini(
    call_id: str,
    config: types.GenerateContentConfig,
    file_uri: str,
):
    contents_list: list[Any] = [
        types.Part.from_uri(file_uri=file_uri, mime_type="application/pdf"),
        "Analyze the given structural plan based on the provided instructions",
    ]
    response = await google_client_async.models.generate_content(
        model=GEMINI_MODEL_NAME,
        config=config,
        contents=contents_list,
    )
    # Access the usage metadata here
    # usage = response.usage_metadata
    # print(f"--- Usage Stats for {call_id} ---")
    # print(f"Input Tokens: {usage.prompt_token_count}")
    # print(f"Thinking Tokens: {usage.thoughts_token_count}")
    # print(f"Output Tokens (Actual Response): {usage.candidates_token_count}")
    # print(f"Total Tokens: {usage.total_token_count}")
    return call_id, response.parsed


async def visual_extractor(file_uri: str) -> dict:
    # try:
    floor_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_FLOOR,
        response_schema=FloorSystemData,
        response_mime_type="application/json",
    )
    footing_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_FOOTING,
        response_schema=FootingSystemData,
        response_mime_type="application/json",
    )
    post_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_POST,
        response_schema=PostData,
        response_mime_type="application/json",
    )
    roof_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_ROOF,
        response_schema=RoofSystemData,
        response_mime_type="application/json",
    )
    shear_wall_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_SHEAR_WALL,
        response_schema=ShearWallData,
        response_mime_type="application/json",
    )
    wall_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_WALL,
        response_schema=WallSystemData,
        response_mime_type="application/json",
    )
    pc_config = types.GenerateContentConfig(
        system_instruction=SYS_PROMPT_PROJECT_CONTEXT,
        response_schema=ProjectContextData,
        response_mime_type="application/json",
    )

    configs = [
        ("floor", floor_config),
        ("footing", footing_config),
        ("post", post_config),
        ("roof", roof_config),
        ("shear_wall", shear_wall_config),
        ("wall", wall_config),
        ("project_context", pc_config),
    ]

    tasks = []

    # Launch tasks with a slight stagger
    for call_id, config in configs:
        tasks.append(
            fetch_with_gemini(call_id=call_id, config=config, file_uri=file_uri)
        )
        await asyncio.sleep(1)  # <--- Give the backend 1 second to breathe

    parsed_data = {}

    for task in asyncio.as_completed(tasks):
        call_id, data = await task
        parsed_data[call_id] = data
        logger.info(f"Finished {call_id}")

    wall_dump = parsed_data["wall"].model_dump()
    return {
        "roof_system": parsed_data["roof"].model_dump(),
        "floor_system": parsed_data["floor"].model_dump(),
        "footing": parsed_data["footing"].model_dump(),
        "post": parsed_data["post"].model_dump(),
        "wall": wall_dump,
        "shear_wall": parsed_data["shear_wall"].model_dump(),
        "project_context": parsed_data["project_context"].model_dump(),
    }


# except ClientError as e:
#     logger.error(f"Google API Client Error: {e.message}")
#     raise Exception("AI Quota exceeded or invalid request.")

# except Exception as e:
#     logger.critical("Error: ", e)
#     raise Exception("Something went Wrong while extracting data from document")
