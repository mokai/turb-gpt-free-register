# -*- coding: utf-8 -*-
"""任务级代理池覆盖（线程局部）。

WebUI 允许在「注册 / 查活 / 批量开 2FA / 批量换邮箱 / 查套餐」时指定代理分组，
该选择优先级高于全局配置。每个后台任务都在独立线程里执行，因此这里用
threading.local() 保存"当前线程正在使用的代理池"，任务开始时设置、结束时清除。

注意：本模块 **故意不放进** config.reload_all() 的 _RELOADABLE_SUBMODULES，
否则热加载会新建一个 threading.local 对象，正在跑的任务会丢掉自己的分组
（表现为保存配置后任务悄悄回退到全局默认代理池）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

_TLS = threading.local()


def normalize_pool(pool) -> list[str] | None:
    """把外部传入的代理池规范成 list[str]；None 表示"未指定"。"""
    if pool is None:
        return None
    if isinstance(pool, str):
        pool = pool.splitlines()
    items = [str(item or "").strip() for item in pool]
    return [item for item in items if item]


def set_task_pool(pool, name: str | None = None) -> None:
    """设置当前线程的任务级代理池；传 None 表示清除（回退全局配置）。

    name 记录选中的分组名（用于把该分组继续传给后续子任务，例如注册成功后的
    自动套餐查询），pool 为 None 时 name 一并清除。
    """
    pool_items = normalize_pool(pool)
    _TLS.pool = pool_items
    _TLS.name = (str(name or "").strip() or None) if pool_items else None


def get_task_pool() -> list[str] | None:
    """读取当前线程的任务级代理池；未设置返回 None。"""
    pool = getattr(_TLS, "pool", None)
    return list(pool) if pool else None


def get_task_group_name() -> str | None:
    """读取当前线程选中的代理分组名；未设置返回 None。"""
    return getattr(_TLS, "name", None)


def clear_task_pool() -> None:
    _TLS.pool = None
    _TLS.name = None


@contextmanager
def task_pool(pool):
    """with 语法设置当前线程代理池，退出时恢复原值。"""
    previous = getattr(_TLS, "pool", None)
    previous_name = getattr(_TLS, "name", None)
    set_task_pool(pool)
    try:
        yield
    finally:
        _TLS.pool = previous
        _TLS.name = previous_name
