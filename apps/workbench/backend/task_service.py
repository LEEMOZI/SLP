import asyncio
import hashlib
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional

from .tasks import (
    Task,
    TaskOutcome,
    TaskRepository,
    TaskStateError,
    new_id,
)


logger = logging.getLogger(__name__)


class TaskCancelled(Exception):
    pass


class TaskExecutionContext:
    def __init__(self, repository: TaskRepository, task_id: str, attempt_id: str):
        self.repository = repository
        self.task_id = task_id
        self.attempt_id = attempt_id

    async def checkpoint(self) -> None:
        """在执行器可安全停下的位置响应协作式取消。"""
        if self.repository.is_cancellation_requested(self.task_id, self.attempt_id):
            raise TaskCancelled(self.task_id)
        await asyncio.sleep(0)

    async def report(
        self,
        *,
        current_stage: str,
        progress_detail: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress 必须位于 0 到 100 之间")
        await self.checkpoint()
        self.repository.report(
            self.task_id,
            self.attempt_id,
            current_stage=current_stage,
            progress_detail=progress_detail,
            progress=progress,
            result=result,
        )

    async def report_partial(
        self,
        *,
        current_stage: str,
        progress_detail: str,
        progress: Optional[int] = None,
        result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if progress is not None and not 0 <= progress <= 100:
            raise ValueError("progress 必须位于 0 到 100 之间")
        self.repository.report(
            self.task_id,
            self.attempt_id,
            current_stage=current_stage,
            progress_detail=progress_detail,
            progress=progress,
            result=result,
        )
        await self.checkpoint()


TaskHandler = Callable[
    [TaskExecutionContext, Dict[str, Any]], Awaitable[Optional[TaskOutcome]]
]


class TaskService:
    def __init__(self, repository: TaskRepository):
        self.repository = repository
        self.handlers: Dict[str, TaskHandler] = {}
        self.running: Dict[str, asyncio.Task] = {}

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self.handlers[task_type] = handler

    async def create_task(
        self,
        *,
        case_id: str,
        task_type: str,
        title: str,
        input_snapshot: Dict[str, Any],
        model_role: Optional[str] = None,
        client_request_id: Optional[str] = None,
    ) -> Task:
        if task_type not in self.handlers:
            raise TaskStateError(f"任务类型 {task_type} 尚未注册执行器")
        request_hash = None
        if client_request_id is not None:
            canonical_request = json.dumps(
                {
                    "case_id": case_id,
                    "task_type": task_type,
                    "title": title,
                    "model_role": model_role,
                    "input_snapshot": input_snapshot,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            request_hash = hashlib.sha256(canonical_request).hexdigest()
        task_id = new_id()
        attempt_id = new_id()
        task, created = self.repository.create(
            task_id=task_id,
            attempt_id=attempt_id,
            case_id=case_id,
            task_type=task_type,
            title=title,
            model_role=model_role,
            input_snapshot=input_snapshot,
            client_request_id=client_request_id,
            request_hash=request_hash,
        )
        if created:
            self._schedule(task_id, attempt_id)
        return task

    def cancel(self, task_id: str) -> Task:
        return self.repository.request_cancel(task_id)

    async def retry(self, task_id: str, scope: Optional[Dict[str, Any]] = None) -> Task:
        task = self.repository.get(task_id)
        if task.task_type not in self.handlers:
            raise TaskStateError(f"任务类型 {task.task_type} 尚未注册执行器")
        attempt_id = new_id()
        retried = self.repository.append_attempt(
            task_id=task_id,
            attempt_id=attempt_id,
            scope=scope,
        )
        self._schedule(task_id, attempt_id)
        return retried

    def recover_interrupted(self) -> int:
        return self.repository.recover_interrupted()

    def _schedule(self, task_id: str, attempt_id: str) -> None:
        runner = asyncio.create_task(self._run(task_id, attempt_id))
        self.running[attempt_id] = runner

    async def _run(self, task_id: str, attempt_id: str) -> None:
        try:
            input_snapshot = self.repository.begin_attempt(task_id, attempt_id)
            if input_snapshot is None:
                return
            task = self.repository.get(task_id)
            handler = self.handlers[task.task_type]
            context = TaskExecutionContext(self.repository, task_id, attempt_id)
            try:
                outcome = await handler(context, input_snapshot)
                await context.checkpoint()
                if outcome is None:
                    outcome = TaskOutcome(
                        current_stage="已完成",
                        progress_detail="任务执行完成",
                    )
                self.repository.complete(task_id, attempt_id, outcome)
            except TaskCancelled:
                self.repository.finish_cancelled(task_id, attempt_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("任务执行失败：%s / %s", task_id, attempt_id)
                self.repository.fail(task_id, attempt_id, str(error) or type(error).__name__)
        finally:
            self.running.pop(attempt_id, None)

    async def shutdown(self) -> None:
        """停止本地协程但不猜测最终状态，留待下次启动恢复。"""
        runners = list(self.running.values())
        for runner in runners:
            runner.cancel()
        if runners:
            await asyncio.gather(*runners, return_exceptions=True)
