"""验证知识库 backend 切换：全局默认 store、按微服务覆盖 local、s3 预留不实现（docs/adr/0002）。

不需要数据库、不需要模型 API：临时目录当 ``local_root`` + ``InMemoryStore``。

1) ``backend_for``：没覆盖用全局，覆盖了用覆盖值（按微服务名，空间=微服务所以等价于按空间）；
2) local：字节落磁盘（``{local_root}/{微服务名}/{层}/...``），读 / 删 / 穿越防护；
3) s3：``cell_backend`` 直接 ``NotImplementedError``，不静默降级；
4) purge：store 命名空间与磁盘目录**都**清（配置中途换过 backend 也不残留）；
5) 挂载层跟着配置走：同一份 ``/kb/`` 挂载，local 格子读到的是磁盘内容。

运行方式：``uv run python tests/test_kb_backend.py``
"""

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.store.memory import InMemoryStore  # noqa: E402

import agents.kb.mount as kb_mount_mod  # noqa: E402
from agents.agent import AgentContext  # noqa: E402
from agents.kb import kb_mount  # noqa: E402
from agents.kb.storage import (  # noqa: E402
    adelete_file,
    aget_bytes,
    aput_bytes,
    cell_backend,
    cell_root,
    purge,
)
from config import app_config  # noqa: E402

STORE_MS, LOCAL_MS, S3_MS = "store-svc", "local-svc", "s3-svc"
STORE_ID, LOCAL_ID = "id_store", "id_local"
tmp = tempfile.mkdtemp(prefix="kb_backend_")
store = InMemoryStore()


def step(name: str) -> None:
    print(f"  - {name}")


def main() -> None:
    original = app_config.kb.model_dump()
    try:
        run()
    finally:
        app_config.kb.local_root = Path(original["local_root"])
        app_config.kb.backend = original["backend"]
        app_config.kb.overrides = dict(original["overrides"])
        shutil.rmtree(tmp, ignore_errors=True)


def run() -> None:
    from config import KbConfig  # noqa: PLC0415 — 避免顶层再引一次

    app_config.kb = KbConfig(
        backend="store",
        local_root=Path(tmp),
        overrides={LOCAL_MS: "local", S3_MS: "s3"},
    )

    print("== 1. backend_for：全局默认 + 按微服务覆盖 ==")
    assert app_config.kb.backend_for(STORE_MS) == "store"
    assert app_config.kb.backend_for(LOCAL_MS) == "local"
    assert app_config.kb.backend_for(S3_MS) == "s3"
    step("没配置的用全局 store，overrides 里 local / s3 各自生效")

    print("== 2. local：字节落磁盘，读 / 删 / 穿越防护 ==")
    asyncio.run(aput_bytes(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared",
        path="docs/概念.md", content="本地磁盘".encode(),
    ))
    disk = app_config.kb.resolved_local_root / LOCAL_MS / "shared" / "docs" / "概念.md"
    assert disk.is_file(), f"没落到磁盘：{disk}"
    step(f"落盘 {disk.relative_to(tmp)}")
    back = asyncio.run(aget_bytes(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared", path="docs/概念.md",
    ))
    assert back == "本地磁盘".encode(), back
    step("读回一致")
    assert asyncio.run(adelete_file(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared", path="docs/概念.md",
    )) is True
    assert not disk.exists()
    assert asyncio.run(adelete_file(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared", path="docs/概念.md",
    )) is False, "删不存在的文件该返回 False"
    step("删成功 / 再删返回 False")
    try:
        asyncio.run(aput_bytes(
            name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared",
            path="../escape.md", content=b"x",
        ))
        raise AssertionError("穿越路径竟然写进去了")
    except ValueError:
        step("_key 挡住 ../ 穿越（ValueError）")
    assert cell_root(LOCAL_MS, "private", user_id="u1").parts[-3:] == (
        LOCAL_MS, "private", "u1"
    )
    step("private 层磁盘目录带 user_id 段")

    print("== 3. s3：预留未实现，直接报错 ==")
    try:
        cell_backend(name=S3_MS, microservice_id="id_s3", layer="shared", store=store)
        raise AssertionError("s3 竟然建出了 backend")
    except NotImplementedError as exc:
        assert "S3" in str(exc), exc
        step("cell_backend(s3) -> NotImplementedError（不静默降级）")

    print("== 4. purge：store 命名空间与磁盘目录都清 ==")
    asyncio.run(aput_bytes(
        name=STORE_MS, microservice_id=STORE_ID, layer="shared",
        path="a.md", content=b"x", store=store,
    ))
    asyncio.run(aput_bytes(
        name=STORE_MS, microservice_id=STORE_ID, layer="private", user_id="u1",
        path="表.xlsx", content=b"\xff\xfe", store=store,
    ))
    asyncio.run(aput_bytes(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared",
        path="b.md", content=b"y",
    ))
    assert store.search(("kb", STORE_ID)), "前置数据没进 store"
    assert cell_root(LOCAL_MS, "shared").exists(), "前置数据没进磁盘"
    asyncio.run(purge(store, microservice_id=STORE_ID, name=STORE_MS))
    asyncio.run(purge(store, microservice_id=LOCAL_ID, name=LOCAL_MS))
    assert store.search(("kb", STORE_ID)) == [], "store 没清干净"
    assert store.search(("kb", LOCAL_ID)) == [], "另一个微服务的 store 被误清了？"
    assert not (app_config.kb.resolved_local_root / LOCAL_MS).exists(), "磁盘没清干净"
    assert cell_root(LOCAL_MS, "shared").exists() is False
    step("store 前缀 + 磁盘目录都清空；别的微服务不受影响")
    # store 里另一个微服务的留着
    asyncio.run(purge(None, microservice_id=STORE_ID, name=STORE_MS))  # store=None 也能清磁盘
    step("purge(store=None) 不炸（agent 没起时至少能清磁盘）")

    print("== 5. 挂载层跟着配置走：local 格子读的是磁盘内容 ==")
    asyncio.run(aput_bytes(
        name=LOCAL_MS, microservice_id=LOCAL_ID, layer="shared",
        path="磁盘.md", content="来自 local".encode(),
    ))
    mount = kb_mount(store)
    kb_mount_mod.get_runtime = lambda: SimpleNamespace(
        context=AgentContext(user_id="alice", kb_cells={LOCAL_MS: LOCAL_ID})
    )
    got = mount.read(f"/{LOCAL_MS}/shared/磁盘.md")
    assert got.error is None and got.file_data["content"] == "来自 local", got.error
    ls = mount.ls(f"/{LOCAL_MS}/shared")
    assert [e["path"] for e in ls.entries] == ["/磁盘.md"], ls.entries
    step("同一份 /kb/ 挂载，local 格子 ls / read 正常")

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
