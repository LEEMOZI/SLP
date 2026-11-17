import asyncio
from typing import Literal

from pydantic import BaseModel, Field

from .task_service import TaskExecutionContext, TaskService
from .tasks import TaskOutcome


class DemoTaskCreate(BaseModel):
    case_id: str
    outcome: Literal["completed", "partial", "failed", "cancellable"] = "completed"
    delay_ms: int = Field(default=1, ge=0, le=10000)


async def run_demo_task(context: TaskExecutionContext, input_snapshot):
    scope = input_snapshot.get("scope")
    if not isinstance(scope, dict):
        scope = {}
    outcome = scope.get("outcome", input_snapshot["outcome"])
    delay_ms = scope.get("delay_ms", input_snapshot["delay_ms"])
    delay = max(delay_ms, 1) / 1000

    await context.report(current_stage="准备执行", progress_detail="已读取输入快照")
    await asyncio.sleep(delay)
    await context.checkpoint()

    if outcome == "failed":
        raise RuntimeError("演示任务按配置失败")
    if outcome == "partial":
        result = {"items": ["已保留的部分结果"]}
        await context.report(
            current_stage="生成部分结果",
            progress_detail="两个阶段已完成一个，部分结果已保存",
            progress=50,
            result=result,
        )
        return TaskOutcome(
            status="partial",
            current_stage="部分完成",
            progress_detail="部分结果已保留，可修改范围后重试",
            progress=50,
            result=result,
        )
    if outcome == "cancellable":
        for _ in range(200):
            await asyncio.sleep(delay)
            await context.checkpoint()

    return TaskOutcome(
        current_stage="已完成",
        progress_detail="演示任务执行完成",
        result={"items": ["演示执行结果"]},
    )


def register_demo_handler(service: TaskService) -> None:
    service.register_handler("demo", run_demo_task)
