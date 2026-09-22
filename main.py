"""本地启动入口：uv run uvicorn main:app --reload。"""

from __future__ import annotations

from fastapi import Depends

from app.ext.fastapi_lifespan import requires
from app.factory import create_app
from app.sample_plugins.clock import CLOCK_SPEC, ClockService
from app.sample_plugins.greeter import GREETER_SPEC, GreeterService

app = create_app(
    config_path="config.example.yaml",
    plugin_specs=[CLOCK_SPEC, GREETER_SPEC],
)


@app.get("/")
async def read_root(
    clock: ClockService = Depends(requires("clock")),
    greeter: GreeterService = Depends(requires("greeter")),
) -> dict[str, str]:
    return {"message": greeter.greet("world"), "clock_tick": str(clock.tick())}
