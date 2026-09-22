# -*- coding: utf-8 -*-
"""票证检测矫正 HTTP 服务

基于 ModelScope 读光票证检测矫正模型 (cv_resnet18_card_correction)。
输入一张图片(拍照/扫描,可含多张卡证票据混贴、任意角度),
原图与矫正后子图均上传到 RustFS (S3 兼容) 对象存储,接口仅返回预签名 URL。

启动:
    .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8300
接口文档: http://127.0.0.1:8300/docs
"""
import io
import json
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import boto3
import cv2
import numpy as np
import torch
from botocore.client import Config as BotoConfig
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.security import APIKeyHeader
from PIL import Image, ImageOps
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("card-correction")

MODEL_DIR = Path(__file__).resolve().parent
CONFIG_PATH = MODEL_DIR / "service_config.json"

DEFAULT_CONFIG = {
    "endpoint": "http://127.0.0.1:9001",
    "access_key": "",
    "secret_key": "",
    "bucket": "card-correction",
    "region": "us-east-1",
    "prefix": "card-correction",
    "url_expires_seconds": 3600,
    "auto_create_bucket": True,
    "addressing_style": "path",
    "s3_connect_timeout": 5,
    "s3_read_timeout": 30,
    "s3_max_attempts": 2,
    "auth": {
        "enabled": True,
        "api_keys": [],
    },
    "nacos": {
        "enabled": True,
        "server_addr": "127.0.0.1:8848",
        "namespace": "",
        "group": "DEFAULT_GROUP",
        "username": "",
        "password": "",
        "service_name": "card-correction",
        "register_ip": "",
        "register_port": 8300,
        "metadata": {},
    },
}
_config = dict(DEFAULT_CONFIG)


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    global _config
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
            cfg = _deep_merge(cfg, {k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        except Exception as e:
            logger.warning("读取 %s 失败, 使用默认配置: %s", CONFIG_PATH, e)
    _config = cfg
    return cfg


# ---------------------------------------------------------------------------
# 模型加载(懒加载 + 进程内单例;pipeline 非线程安全,推理加锁串行)
# ---------------------------------------------------------------------------
_device = os.environ.get("DEVICE", "cpu")
if _device.startswith("gpu") and not torch.cuda.is_available():
    logger.warning("CUDA 不可用, 回退到 CPU")
    _device = "cpu"

_pipeline = None
_lock = threading.Lock()


def get_pipeline():
    global _pipeline
    if _pipeline is None:
        from modelscope.pipelines import pipeline
        from modelscope.utils.constant import Tasks

        t0 = time.time()
        _pipeline = pipeline(
            Tasks.card_detection_correction,
            model=str(MODEL_DIR),
            device=_device,
        )
        logger.info("模型加载完成 (%.1fs, device=%s)", time.time() - t0, _device)
    return _pipeline


# ---------------------------------------------------------------------------
# RustFS / S3 客户端
# ---------------------------------------------------------------------------
_s3 = None


def get_s3():
    global _s3
    if _s3 is None:
        cfg = _config
        _s3 = boto3.client(
            "s3",
            endpoint_url=cfg["endpoint"],
            aws_access_key_id=cfg["access_key"],
            aws_secret_access_key=cfg["secret_key"],
            region_name=cfg["region"],
            config=BotoConfig(
                signature_version="s3v4",
                s3={"addressing_style": cfg["addressing_style"]},
                # 连接/读取超时与重试: 防止 RustFS 抖动时请求被挂死
                connect_timeout=int(cfg.get("s3_connect_timeout", 5)),
                read_timeout=int(cfg.get("s3_read_timeout", 30)),
                retries={"max_attempts": int(cfg.get("s3_max_attempts", 2)), "mode": "standard"},
            ),
        )
    return _s3


class StorageError(Exception):
    """统一封装 RustFS / S3 调用异常"""


def _wrap_s3_error(e: Exception) -> StorageError:
    return StorageError(f"{type(e).__name__}: {e}")


def ensure_bucket():
    cfg = _config
    s3 = get_s3()
    bucket = cfg["bucket"]
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("RustFS bucket 已就绪: %s", bucket)
        return
    except (ClientError, BotoCoreError) as e:
        logger.warning("head_bucket 失败 (%s), 尝试创建", type(e).__name__)
        if not cfg["auto_create_bucket"]:
            raise RuntimeError(f"bucket {bucket} 不可用且未开启自动创建: {e}") from e
    try:
        s3.create_bucket(Bucket=bucket)
        logger.info("已创建 RustFS bucket: %s", bucket)
    except (ClientError, BotoCoreError) as ce:
        raise RuntimeError(f"创建 bucket {bucket} 失败: {ce}") from ce


def put_object(key: str, body: bytes, content_type: str) -> str:
    s3 = get_s3()
    try:
        s3.put_object(Bucket=_config["bucket"], Key=key, Body=body, ContentType=content_type)
    except (ClientError, BotoCoreError) as e:
        raise _wrap_s3_error(e) from e
    return presign(key)


def presign(key: str) -> str:
    try:
        return get_s3().generate_presigned_url(
            "get_object",
            Params={"Bucket": _config["bucket"], "Key": key},
            ExpiresIn=int(_config["url_expires_seconds"]),
        )
    except (ClientError, BotoCoreError) as e:
        raise _wrap_s3_error(e) from e


def make_key(request_id: str, suffix: str, ext: str = ".jpg") -> str:
    prefix = _config["prefix"].strip("/")
    date = time.strftime("%Y-%m-%d")
    parts = [p for p in (prefix, date, f"{request_id}_{suffix}{ext}") if p]
    return "/".join(parts)


def new_request_id() -> str:
    return time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]


def guess_ext(filename: Optional[str], default: str = ".jpg") -> str:
    if not filename:
        return default
    ext = Path(filename).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".bmp"} else default


# ---------------------------------------------------------------------------
# Nacos 服务注册 (env 覆盖 service_config.json; 同时兼容 SDK 1.x HTTP 与 3.x gRPC)
# ---------------------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


def resolve_nacos_config() -> dict:
    cfg = dict(_config.get("nacos") or {})
    str_envs = {
        "server_addr": "NACOS_SERVER_ADDR",
        "namespace": "NACOS_NAMESPACE",
        "group": "NACOS_GROUP",
        "username": "NACOS_USERNAME",
        "password": "NACOS_PASSWORD",
        "service_name": "NACOS_SERVICE_NAME",
        "register_ip": "NACOS_REGISTER_IP",
    }
    for key, env_name in str_envs.items():
        v = os.environ.get(env_name)
        if v:
            cfg[key] = v
    if os.environ.get("NACOS_REGISTER_PORT"):
        cfg["register_port"] = int(os.environ["NACOS_REGISTER_PORT"])
    if "NACOS_ENABLED" in os.environ:
        cfg["enabled"] = _env_bool("NACOS_ENABLED", True)
    if not cfg.get("register_ip"):
        try:
            cfg["register_ip"] = socket.gethostbyname(socket.gethostname())
        except Exception:
            cfg["register_ip"] = "127.0.0.1"
    return cfg


def nacos_register():
    """同步入口, 兼容旧调用; 内部委托给 async 版本"""
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # 已在事件循环里(如 FastAPI lifespan), 创建 task 即可
        loop.create_task(_nacos_register_async())
    else:
        asyncio.run(_nacos_register_async())


def nacos_deregister():
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        loop.create_task(_nacos_deregister_async())
    else:
        asyncio.run(_nacos_deregister_async())


_nacos_state: dict = {"client": None, "cfg": {}, "version": 0}


async def _nacos_register_async():
    cfg = resolve_nacos_config()
    if not cfg.get("enabled", True):
        logger.info("Nacos 注册已禁用")
        return

    # ---- 优先尝试 v3.x SDK (gRPC) ----
    try:
        from v2.nacos import (
            ClientConfigBuilder,
            NacosNamingService,
            RegisterInstanceParam,
        )
        builder = ClientConfigBuilder().server_address(
            cfg["server_addr"].replace("http://", "").replace("https://", "")
        )
        if cfg.get("namespace"):
            builder.namespace_id(cfg["namespace"])
        if cfg.get("username"):
            builder.username(cfg["username"])
        if cfg.get("password"):
            builder.password(cfg["password"])
        client_config = builder.build()
        naming = await NacosNamingService.create_naming_service(client_config)
        meta = {str(k): str(v) for k, v in (cfg.get("metadata") or {}).items()}
        await naming.register_instance(RegisterInstanceParam(
            ip=cfg["register_ip"],
            port=int(cfg["register_port"]),
            service_name=cfg["service_name"],
            group_name=cfg.get("group", "DEFAULT_GROUP"),
            weight=1.0,
            metadata=meta,
        ))
        _nacos_state.update({"client": naming, "cfg": cfg, "version": 3})
        logger.info(
            "已注册到 Nacos (v3 gRPC): %s -> %s:%s (group=%s, namespace=%s)",
            cfg["service_name"], cfg["register_ip"], cfg["register_port"],
            cfg.get("group", "DEFAULT_GROUP"), cfg.get("namespace") or "public",
        )
        return
    except ImportError:
        logger.info("未检测到 nacos-sdk-python 3.x, 尝试 1.x")
    except Exception as e:
        logger.exception("Nacos v3 注册失败, 尝试 1.x: %s", e)

    # ---- 回退 v1.x SDK (HTTP) ----
    try:
        import nacos as nacos_v1
    except ImportError:
        logger.error(
            "未安装可用的 nacos-sdk-python (3.x 需要 `pip install nacos-sdk-python>=3`, "
            "1.x 需要 `pip install nacos-sdk-python<2`); 跳过注册"
        )
        return
    try:
        server = cfg["server_addr"].replace("http://", "").replace("https://", "")
        client = nacos_v1.NacosClient(
            server_addresses=server,
            namespace=cfg.get("namespace", "") or "",
            username=cfg.get("username") or None,
            password=cfg.get("password") or None,
        )
        client.add_naming_instance(
            service_name=cfg["service_name"],
            ip=cfg["register_ip"],
            port=int(cfg["register_port"]),
            group_name=cfg.get("group", "DEFAULT_GROUP"),
            weight=1.0,
            metadata=cfg.get("metadata") or {},
        )
        _nacos_state.update({"client": client, "cfg": cfg, "version": 1})
        logger.info(
            "已注册到 Nacos (v1 HTTP): %s -> %s:%s (group=%s, namespace=%s)",
            cfg["service_name"], cfg["register_ip"], cfg["register_port"],
            cfg.get("group", "DEFAULT_GROUP"), cfg.get("namespace") or "public",
        )
    except Exception as e:
        logger.exception("Nacos v1 注册失败 (服务仍正常启动): %s", e)


async def _nacos_deregister_async():
    client = _nacos_state.get("client")
    if client is None:
        return
    cfg = _nacos_state["cfg"]
    version = _nacos_state["version"]
    try:
        if version == 3:
            from v2.nacos import DeregisterInstanceParam
            await client.deregister_instance(DeregisterInstanceParam(
                ip=cfg["register_ip"],
                port=int(cfg["register_port"]),
                service_name=cfg["service_name"],
                group_name=cfg.get("group", "DEFAULT_GROUP"),
            ))
        else:
            client.remove_naming_instance(
                service_name=cfg["service_name"],
                ip=cfg["register_ip"],
                port=int(cfg["register_port"]),
                group_name=cfg.get("group", "DEFAULT_GROUP"),
            )
        logger.info("已从 Nacos 注销 (v%d): %s", version, cfg["service_name"])
    except Exception as e:
        logger.warning("Nacos 注销失败: %s", e)
    finally:
        _nacos_state.update({"client": None, "cfg": {}, "version": 0})


# ---------------------------------------------------------------------------
# OpenAPI 鉴权 (X-API-Key 或 Authorization: Bearer <key>)
# ---------------------------------------------------------------------------
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def resolve_api_keys() -> set:
    """env API_KEYS=k1,k2 优先于 service_config.json"""
    raw = os.environ.get("API_KEYS")
    if raw is not None:
        return {k.strip() for k in raw.split(",") if k.strip()}
    keys = (_config.get("auth") or {}).get("api_keys") or []
    return {str(k) for k in keys if k}


def auth_enabled() -> bool:
    if os.environ.get("AUTH_ENABLED") is not None:
        return _env_bool("AUTH_ENABLED", True)
    cfg = _config.get("auth") or {}
    if not cfg.get("enabled", True):
        return False
    # 没有配置任何 key 时, 自动关闭鉴权(本地调试友好)
    return bool(resolve_api_keys())


async def require_api_key(
    x_api_key: Optional[str] = Depends(_api_key_header),
    authorization: Optional[str] = Header(None),
):
    if not auth_enabled():
        return
    key = x_api_key
    if not key and authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            key = parts[1].strip()
    if not key:
        raise HTTPException(
            status_code=401,
            detail="缺少 API Key, 请通过 X-API-Key 头或 Authorization: Bearer <key> 传入",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
    if key not in resolve_api_keys():
        raise HTTPException(status_code=403, detail="无效的 API Key")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def decode_upload(data: bytes) -> Image.Image:
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img)
        return img.convert("RGB")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解码图片: {e}")


def run_inference(img: Image.Image) -> dict:
    pipe = get_pipeline()
    with _lock:
        t0 = time.time()
        result = pipe(img)
        elapsed = time.time() - t0
    return {
        "polygons": result.get("polygons", []),
        "scores": result.get("scores", []),
        "labels": result.get("labels", []),
        "layout": result.get("layout", []),
        "output_imgs": result.get("output_imgs", []),
        "elapsed": elapsed,
    }


ROTATE_DESC = {
    0: "无需旋转",
    1: "旋转90°(逆时针)转正",
    2: "旋转180°转正",
    3: "旋转90°(顺时针)转正",
}
LAYOUT_DESC = {0: "原件", 1: "复印件"}


def encode_jpeg(arr: np.ndarray, quality: int = 95) -> bytes:
    ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise RuntimeError("JPEG 编码失败")
    return buf.tobytes()


def polygon_to_list(poly) -> List[List[float]]:
    pts = np.asarray(poly, dtype=float).reshape(4, 2)
    return [[round(float(x), 2), round(float(y), 2)] for x, y in pts]


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
def warmup(retries: int = 3, delay: float = 2.0):
    global _pipeline
    for attempt in range(1, retries + 1):
        try:
            t0 = time.time()
            pipe = get_pipeline()
            dummy = Image.new("RGB", (64, 64), (128, 128, 128))
            with _lock:
                pipe(dummy)
            logger.info("模型预热完成 (%.1fs)", time.time() - t0)
            return
        except Exception as e:
            logger.error("模型预热失败 (第 %d/%d 次): %s", attempt, retries, e)
            _pipeline = None
            time.sleep(delay)
    logger.error("模型预热全部失败, 将在首个请求时再次尝试懒加载")


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    try:
        ensure_bucket()
    except Exception as e:
        logger.error("RustFS 初始化失败: %s", e)
    warmup()
    await _nacos_register_async()
    try:
        yield
    finally:
        await _nacos_deregister_async()


app = FastAPI(
    title="票证检测矫正服务",
    description="上传图片到 RustFS 对象存储; /api/correct 额外做检测矫正,所有结果均以预签名 URL 返回。",
    version="2.0.0",
    lifespan=lifespan,
)


class ObjectRef(BaseModel):
    key: str
    url: str
    expires_in: int


class UploadResponse(BaseModel):
    request_id: str
    object: ObjectRef


class CorrectedItem(BaseModel):
    index: int
    score: float
    label: int
    label_desc: str
    layout: int
    layout_desc: str
    polygon: List[List[float]]
    width: int
    height: int
    object: ObjectRef


class CorrectResponse(BaseModel):
    request_id: str
    count: int
    elapsed_ms: int
    upload: ObjectRef
    items: List[CorrectedItem]


@app.get("/health")
def health():
    return {"status": "ok", "model_loaded": _pipeline is not None, "device": _device}


@app.post(
    "/api/upload",
    response_model=UploadResponse,
    summary="仅上传原图到 RustFS, 不做检测",
    dependencies=[Depends(require_api_key)],
)
async def api_upload(file: UploadFile = File(..., description="待上传图片(jpg/png/webp)")):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    decode_upload(data)  # 提前校验是否为有效图片

    rid = new_request_id()
    ext = guess_ext(file.filename)
    key = make_key(rid, "upload", ext)
    try:
        url = put_object(key, data, file.content_type or "image/jpeg")
    except StorageError as e:
        raise HTTPException(502, f"上传 RustFS 失败: {e}") from e

    return UploadResponse(
        request_id=rid,
        object=ObjectRef(key=key, url=url, expires_in=int(_config["url_expires_seconds"])),
    )


@app.post(
    "/api/correct",
    response_model=CorrectResponse,
    summary="上传 + 检测矫正, 原图与子图全部入 RustFS",
    dependencies=[Depends(require_api_key)],
)
async def api_correct(file: UploadFile = File(..., description="待处理图片(jpg/png/webp)")):
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    img = decode_upload(data)

    rid = new_request_id()
    ext = guess_ext(file.filename)
    upload_key = make_key(rid, "upload", ext)
    try:
        upload_url = put_object(upload_key, data, file.content_type or "image/jpeg")
    except StorageError as e:
        raise HTTPException(502, f"上传原图到 RustFS 失败: {e}") from e

    raw = run_inference(img)
    expires_in = int(_config["url_expires_seconds"])

    items: List[CorrectedItem] = []
    for i, arr in enumerate(raw["output_imgs"]):
        label = int(np.asarray(raw["labels"]).flatten()[i]) if i < len(raw["labels"]) else -1
        layout = int(np.asarray(raw["layout"]).flatten()[i]) if i < len(raw["layout"]) else -1
        arr_np = np.asarray(arr)
        jpg = encode_jpeg(arr_np)
        key = make_key(rid, f"corrected_{i}", ".jpg")
        try:
            url = put_object(key, jpg, "image/jpeg")
        except StorageError as e:
            logger.exception("上传子图 %s 失败: %s", key, e)
            continue
        items.append(
            CorrectedItem(
                index=i,
                score=round(float(np.asarray(raw["scores"]).flatten()[i]), 4),
                label=label,
                label_desc=ROTATE_DESC.get(label, "未知"),
                layout=layout,
                layout_desc=LAYOUT_DESC.get(layout, "未知"),
                polygon=polygon_to_list(raw["polygons"][i]),
                width=int(arr_np.shape[1]),
                height=int(arr_np.shape[0]),
                object=ObjectRef(key=key, url=url, expires_in=expires_in),
            )
        )

    return CorrectResponse(
        request_id=rid,
        count=len(items),
        elapsed_ms=int(raw["elapsed"] * 1000),
        upload=ObjectRef(key=upload_key, url=upload_url, expires_in=expires_in),
        items=items,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8300)))
