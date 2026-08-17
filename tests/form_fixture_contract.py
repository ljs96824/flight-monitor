"""表单业务夹具与存储元数据之间的测试边界。"""

from __future__ import annotations


def without_storage_identity(payload: dict) -> dict:
    """屏蔽保存时生成的随机身份，仅比较业务规范化字段。"""
    comparable = dict(payload)
    comparable["subscription_id"] = None
    return comparable
