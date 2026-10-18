# control_server.py
"""
本地控制服务器 —— 暴露 pipeline 状态查询和重启接口。

仅监听 127.0.0.1，不对外网开放。
"""

import os
import sys
import json
import threading
import urllib.request
import traceback
import config
import runtime_state
import dispatcher
from logger import log as _log

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_settings.json")

_WXPUSHER_API_URL = "https://wxpusher.zjiecode.com/api/send/message"


def _load_wxpusher_config() -> tuple:
    """从 app_settings.json 读取 WxPusher 配置，返回 (app_token, uid)。"""
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("wxpusher_app_token", ""), data.get("wxpusher_uid", "")
    except Exception:
        return "", ""


def _send_webhook(title: str, content: str):
    """后台线程发送 WxPusher 通知。"""
    app_token, uid = _load_wxpusher_config()
    if not app_token or not uid:
        return

    def _do():
        try:
            data = json.dumps({
                "appToken": app_token,
                "content": content,
                "summary": title[:100],
                "contentType": 1,
                "uids": [uid],
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                _WXPUSHER_API_URL,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = resp.read().decode("utf-8", errors="ignore")
                result = json.loads(body)
                if result.get("code") == 1000:
                    _log("WEBHOOK", f"WxPusher 发送成功: {title}")
                else:
                    _log("WEBHOOK", f"WxPusher 发送失败: {result.get('msg')}")
        except Exception as e:
            _log("WEBHOOK", f"WxPusher 发送异常: {e}")

    threading.Thread(target=_do, daemon=True).start()

app = FastAPI(title="MultTTS Control Server")


class RestartRequest(BaseModel):
    force: bool = False


_process_runner = None


def set_process_runner(runner):
    """注册重启时调用的函数（通常是 restart_last_pipeline）。"""
    global _process_runner
    _process_runner = runner


@app.get("/api/pipeline/status")
def get_pipeline_status():
    """查询当前 pipeline 运行状态。"""
    return runtime_state.snapshot()


@app.post("/api/pipeline/restart")
def restart_pipeline(req: RestartRequest):
    """重启整条处理流程。

    不是恢复旧线程/旧 dispatcher，而是新建线程重新调用 process_srt_files。
    """
    snap = runtime_state.snapshot()

    if snap["pipeline_running"] and not req.force:
        raise HTTPException(
            status_code=409,
            detail="pipeline is still running; use force=true to force restart"
        )

    if not snap["has_last_job_context"]:
        raise HTTPException(
            status_code=400,
            detail="no previous pipeline context found"
        )

    if _process_runner is None:
        raise HTTPException(
            status_code=500,
            detail="process runner not registered"
        )

    # 重置状态
    runtime_state.reset_before_pipeline_restart()
    config.ABORT_ALL = False
    config.STOP_REASON = None

    # 清除熔断状态
    if hasattr(dispatcher, "reset_circuit_breaker_states"):
        dispatcher.reset_circuit_breaker_states()

    def _run_safely():
        try:
            _process_runner()
        except BaseException:
            _log("FATAL", "重启流程线程未捕获异常:\n" + traceback.format_exc())

    # 新线程重启整条流程。不能用 daemon=True，否则主线程结束时会被直接杀掉，表现成“闪退”。
    threading.Thread(
        target=_run_safely,
        daemon=False,
        name="PIPELINE_RESTART_THREAD"
    ).start()

    _log("CONTROL", "收到 pipeline 重启请求，已启动新流程线程")

    # WxPusher 通知
    _send_webhook(
        "Pipeline 重启请求",
        "收到外部 POST /api/pipeline/restart 请求，已重新启动整条处理流程。"
    )

    return {
        "accepted": True,
        "message": "full pipeline restart accepted",
        "restart_mode": "FULL_PIPELINE_RESTART"
    }


def start_control_server(host="127.0.0.1", port=8767):
    """在后台线程启动控制服务器。"""
    def _run_server():
        try:
            # Windows GUI / pythonw 下 sys.stdout 为 None，防止 uvicorn 崩溃
            config_kwargs = {"host": host, "port": port, "log_level": "warning"}
            if sys.stdout is None:
                config_kwargs["log_config"] = None
            uvicorn.run(app, **config_kwargs)
        except Exception as e:
            _log("CONTROL_ERR", f"控制服务器异常: {e}")

    thread = threading.Thread(
        target=_run_server,
        daemon=True,
        name="CONTROL_SERVER"
    )
    thread.start()
    _log("CONTROL", f"控制服务器已启动: http://{host}:{port}")

