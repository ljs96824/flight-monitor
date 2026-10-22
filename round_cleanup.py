"""采集轮收尾的逐步保护(R11)。

收尾链里任一步(统计与用量台账核对、哨兵、解除采集计划、恢复轮次上下文、日志保留演练)抛错时，
只记一行 `[收尾失败]`，后续步骤照常执行，轮档仍能关闭。收尾失败不当作成功：关闭轮档时 ok 降为 partial；
主体本身没有抛错时，全部收尾做完后再把第一个收尾异常原样抛出，保持原有的失败出口与退出码；
主体已经抛错时，原始异常照常向外传播，收尾异常只留在日志里，不覆盖它。
"""

from __future__ import annotations

from typing import Callable

from log_utils import safe_log


class RoundCleanup:
    """依次执行收尾步骤；每步的异常只记录，不打断后续步骤。"""

    def __init__(self, round_id) -> None:
        self.round_id = round_id
        self.failures: list[tuple[str, Exception]] = []

    def step(self, name: str, func: Callable, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            self.failures.append((name, exc))
            try:
                safe_log(
                    f"[收尾失败] round_id={self.round_id or 'unknown'} 步骤={name} "
                    f"原因={type(exc).__name__}:{exc}"
                )
            except Exception:
                pass
            return None

    def status(self, round_status: str) -> str:
        """收尾有失败时 ok 降为 partial；其余状态原样返回。"""

        return "partial" if self.failures and round_status == "ok" else round_status

    def raise_first_failure(self) -> None:
        """仅在主体没有抛错时调用：把第一个收尾异常原样抛出。"""

        if self.failures:
            raise self.failures[0][1]
