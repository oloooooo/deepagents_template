"""注册 / 登录 / 刷新 / 登出 / me 的端到端验证。

前置条件：本机 PostgreSQL 可用且已执行 ``uv run alembic upgrade head``。
运行方式：``uv run python tests/test_auth.py``（无需 pytest，失败即非 0 退出）。
"""

import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bcrypt  # noqa: E402
import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from config import app_config  # noqa: E402
from main import app  # noqa: E402

PASSWORD = "Passw0rd!123"
SUFFIX = uuid4().hex[:8]
ACCOUNT = f"tester_{SUFFIX}"
EMAIL = f"tester_{SUFFIX}@example.com"

checks = 0


def step(name: str) -> None:
    global checks
    checks += 1
    print(f"  [{checks:02d}] {name}")


def db_execute(sql: str, params: tuple = ()) -> list[tuple]:
    with psycopg.connect(app_config.postgresql.user.uri, autocommit=True) as conn:
        cur = conn.execute(sql, params)
        # DELETE/UPDATE 没有结果集，别 fetchall
        return cur.fetchall() if cur.description else []


def cleanup() -> None:
    db_execute("delete from users where account = %s or account like %s", (ACCOUNT, "tester_%"))


def main() -> None:
    cleanup()
    with TestClient(app) as client:
        print("== 健康检查 ==")
        step("GET /health -> 200")
        assert client.get("/health").status_code == 200

        print("== 注册 ==")
        step("POST /auth/register -> 201，返回用户信息且不含任何密码字段")
        resp = client.post("/auth/register", json={"account": ACCOUNT, "email": EMAIL, "password": PASSWORD})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["account"] == ACCOUNT and body["email"] == EMAIL and body["is_active"] is True
        assert "password" not in resp.text

        step("库中存的是 bcrypt 哈希，不是明文")
        rows = db_execute("select hashed_password from users where account = %s", (ACCOUNT,))
        assert len(rows) == 1, rows
        stored_hash = rows[0][0]
        assert stored_hash.startswith("$2b$") and PASSWORD not in stored_hash
        assert bcrypt.checkpw(PASSWORD.encode(), stored_hash.encode())

        step("重复注册同账号/同邮箱 -> 409")
        assert client.post("/auth/register", json={"account": ACCOUNT, "email": f"x_{SUFFIX}@example.com", "password": PASSWORD}).status_code == 409
        assert client.post("/auth/register", json={"account": f"x_{SUFFIX}", "email": EMAIL, "password": PASSWORD}).status_code == 409

        step("非法邮箱 / 密码过短 / 密码超 72 字节 -> 422")
        assert client.post("/auth/register", json={"account": f"y_{SUFFIX}", "email": "not-an-email", "password": PASSWORD}).status_code == 422
        assert client.post("/auth/register", json={"account": f"y_{SUFFIX}", "email": f"y_{SUFFIX}@example.com", "password": "short"}).status_code == 422
        assert client.post("/auth/register", json={"account": f"y_{SUFFIX}", "email": f"y_{SUFFIX}@example.com", "password": "密" * 40}).status_code == 422

        print("== 登录 ==")
        step("密码错误 / 账号不存在 -> 401")
        assert client.post("/auth/login", json={"account": ACCOUNT, "password": "WrongPass!123"}).status_code == 401
        assert client.post("/auth/login", json={"account": "ghost_nobody", "password": PASSWORD}).status_code == 401

        step("POST /auth/login（账号）-> 200 返回双令牌")
        resp = client.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        login_tokens = resp.json()
        assert login_tokens["token_type"] == "bearer" and login_tokens["expires_in"] == 1800
        assert login_tokens["access_token"] and login_tokens["refresh_token"]

        step("POST /auth/login（邮箱）-> 200")
        resp = client.post("/auth/login", json={"account": EMAIL, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        tokens = resp.json()
        access, refresh = tokens["access_token"], tokens["refresh_token"]

        step("登录后 refresh_token 落库")
        assert db_execute("select refresh_token from users where account = %s", (ACCOUNT,))[0][0] == refresh

        print("== 鉴权 ==")
        step("GET /auth/me + Bearer -> 200 且是本人")
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
        assert resp.status_code == 200 and resp.json()["account"] == ACCOUNT

        step("无令牌 / 乱码令牌 / 拿 refresh 当 access 用 -> 401")
        assert client.get("/auth/me").status_code == 401
        assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401

        print("== 刷新与登出 ==")
        step("POST /auth/refresh -> 200 且令牌轮换（新令牌 != 旧令牌）")
        resp = client.post("/auth/refresh", json={"refresh_token": refresh})
        assert resp.status_code == 200, resp.text
        rotated = resp.json()
        assert rotated["refresh_token"] != refresh
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}).status_code == 200

        step("旧 refresh token 已失效 -> 401")
        assert client.post("/auth/refresh", json={"refresh_token": refresh}).status_code == 401
        step("伪造 refresh token -> 401")
        assert client.post("/auth/refresh", json={"refresh_token": "a.b.c"}).status_code == 401

        step("POST /auth/logout -> 204 且库中 refresh_token 置空")
        resp = client.post("/auth/logout", headers={"Authorization": f"Bearer {rotated['access_token']}"})
        assert resp.status_code == 204 and resp.content == b"", (resp.status_code, resp.content)
        assert db_execute("select refresh_token from users where account = %s", (ACCOUNT,))[0][0] is None

        step("登出后该 refresh token -> 401")
        assert client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]}).status_code == 401

        step("无令牌调 /auth/logout -> 401")
        assert client.post("/auth/logout").status_code == 401

        print("== 禁用用户 ==")
        step("is_active=False 后登录 -> 403，调用 /auth/me -> 403")
        db_execute("update users set is_active = false where account = %s", (ACCOUNT,))
        assert client.post("/auth/login", json={"account": ACCOUNT, "password": PASSWORD}).status_code == 403
        assert client.get("/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"}).status_code == 403

    cleanup()
    print(f"\n全部通过：{checks} 项检查")


if __name__ == "__main__":
    try:
        main()
    finally:
        cleanup()
