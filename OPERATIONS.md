# 票证检测矫正服务 - 操作手册

> 版本: 2.0.0  
> 适用对象: 自己部署、调试、上线该服务的运维 / 开发

---

## 0. 架构速览

```
客户端 ──HTTP+API Key──> [card-correction 服务 (FastAPI:8300)]
                              │
                              ├──> RustFS (S3 兼容) 192.168.2.61:9001
                              │      上传原图 + 矫正子图,返回预签名 URL
                              │
                              └──> Nacos 192.168.2.61:8848 (gRPC 9848)
                                     服务注册/心跳/注销
```

**两个业务接口**(都需要 API Key):
- `POST /api/upload` — 只上传原图到 RustFS
- `POST /api/correct` — 上传原图 + 推理矫正 + 上传所有子图

**辅助接口**(无需鉴权):
- `GET /health` — 健康检查,Docker HEALTHCHECK 依赖
- `GET /docs` / `GET /openapi.json` — Swagger 文档

---

## 1. 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Linux / macOS(Docker 部署);Windows 推荐 WSL2 |
| Docker | ≥ 20.10,GPU 部署还需 `nvidia-container-toolkit` |
| Docker Compose | v2 |
| 网络 | 能访问 `192.168.2.61` 的 8848 / 9848 / 9000 / 9001 端口 |
| RustFS | 已部署,bucket `card-correction` 存在(或允许服务自动创建) |
| Nacos | 2.x 版本,8848 + 9848 + 9849 都开放 |
| 资源 | CPU 模式 ≥ 4 核 8G;GPU 模式 NVIDIA 卡 ≥ 8G 显存(V100/T4/A10 等) |

---

## 2. 配置文件

所有运行参数集中在 `service_config.json`。**环境变量优先级高于配置文件**,容器化部署推荐用环境变量。

### 2.1 完整字段

```json
{
  "auth": {
    "enabled": true,
    "api_keys": ["ck_dev_2f8a91b6c4e7d0358a19f6e2b7c4d8a0"]
  },
  "endpoint": "http://192.168.2.61:9001",
  "access_key": "2XJm3oNV6YwHlm2z7SRw",
  "secret_key": "4Z0GS22Ad9PJNdc73JHfJKfiFm7cG0ib9r1vgqzD",
  "bucket": "card-correction",
  "region": "us-east-1",
  "prefix": "card-correction",
  "url_expires_seconds": 3600,
  "auto_create_bucket": true,
  "addressing_style": "path",
  "s3_connect_timeout": 5,
  "s3_read_timeout": 30,
  "s3_max_attempts": 2,
  "nacos": {
    "enabled": true,
    "server_addr": "192.168.2.61:8848",
    "namespace": "",
    "group": "DEFAULT_GROUP",
    "username": "",
    "password": "",
    "service_name": "card-correction",
    "register_ip": "",
    "register_port": 8300,
    "metadata": {"version": "2.0.0"}
  }
}
```

### 2.2 环境变量覆盖一览

| 用途 | 环境变量 | 示例 |
|---|---|---|
| 鉴权开关 | `AUTH_ENABLED` | `false` |
| API Key 列表 | `API_KEYS` | `key1,key2,key3`(逗号分隔) |
| Nacos 开关 | `NACOS_ENABLED` | `false` |
| Nacos 地址 | `NACOS_SERVER_ADDR` | `192.168.2.61:8848` |
| Nacos 命名空间 | `NACOS_NAMESPACE` | `prod` |
| Nacos 分组 | `NACOS_GROUP` | `DEFAULT_GROUP` |
| Nacos 账号 | `NACOS_USERNAME` / `NACOS_PASSWORD` | — |
| 注册的服务名 | `NACOS_SERVICE_NAME` | `card-correction` |
| **注册的 IP** | `NACOS_REGISTER_IP` | **Docker 必须显式传宿主机 IP** |
| 注册的端口 | `NACOS_REGISTER_PORT` | 双卡 GPU 第二个实例填 `8301` |
| 推理设备 | `DEVICE` | `cpu` / `gpu` / `gpu:0` |
| 监听端口 | `PORT` | `8300` |

---

## 3. 本地开发运行(不走 Docker)

```bash
cd /Users/dongzhuo/Desktop/python-project/card_correction

# 1. 建虚拟环境
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 2. 直接启动(uvicorn 热重载, 改代码自动重启)
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8300 --reload

# 或后台启动
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8300 &
```

启动成功的日志关键行:
```
RustFS bucket 已就绪: card-correction
模型加载完成 (0.1s, device=cpu)
模型预热完成 (0.7s)
已注册到 Nacos: card-correction -> <宿主机IP>:8300 (group=DEFAULT_GROUP, namespace=public)
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8300
```

---

## 4. Docker 部署

### 4.1 CPU 单机

```bash
# 启动前确认宿主机 IP, 用于 Nacos 注册
export HOST_IP=$(ipconfig getifaddr en0 2>/dev/null || hostname -I | awk '{print $1}')
echo "HOST_IP=$HOST_IP"

docker compose -f docker-compose.cpu.yml up -d --build
docker logs -f card-correction-cpu     # 看到 Application startup complete 后 Ctrl+C
```

访问:`http://127.0.0.1:8300/docs`

停止 / 重启:
```bash
docker compose -f docker-compose.cpu.yml down
docker compose -f docker-compose.cpu.yml restart
```

### 4.2 GPU 单卡

```bash
export HOST_IP=<宿主机 IP>
docker compose -f docker-compose.gpu.yml up -d --build
```

要求宿主机已装 NVIDIA 驱动 + `nvidia-container-toolkit`。CUDA 不可用时服务自动回退 CPU,日志会有 WARNING。

### 4.3 GPU 双卡(每卡一实例)

```bash
export HOST_IP=<宿主机 IP>
docker compose -f docker-compose.gpu2.yml up -d --build
```

- `card-correction-gpu0` → 宿主机 `8300`,Nacos 注册端口 `8300`
- `card-correction-gpu1` → 宿主机 `8301`,Nacos 注册端口 `8301`
- Nacos 服务列表里会看到同一服务名下两个实例,消费方按权重负载

### 4.4 镜像导出/导入(无外网环境)

```bash
# 在有网机器构建并导出
docker build -f Dockerfile.cpu -t card-correction:cpu .
docker save card-correction:cpu | gzip > card-correction-cpu.tar.gz

# 在目标机器导入
gunzip -c card-correction-cpu.tar.gz | docker load
```

---

## 5. 接口调用示例

下面所有示例使用开发 key `ck_dev_2f8a91b6c4e7d0358a19f6e2b7c4d8a0`,**生产请换成自己的 key**。

### 5.1 健康检查

```bash
curl http://127.0.0.1:8300/health
# {"status":"ok","model_loaded":true,"device":"cpu"}
```

### 5.2 仅上传原图

```bash
curl -X POST \
  -H "X-API-Key: ck_dev_2f8a91b6c4e7d0358a19f6e2b7c4d8a0" \
  -F "file=@data/demo.jpg" \
  http://127.0.0.1:8300/api/upload
```

响应:
```json
{
  "request_id": "20260922_004500_a1b2c3",
  "object": {
    "key": "card-correction/2026-09-22/20260922_004500_a1b2c3_upload.jpg",
    "url": "http://192.168.2.61:9001/card-correction/...?X-Amz-Signature=...",
    "expires_in": 3600
  }
}
```

### 5.3 上传 + 检测矫正

```bash
curl -X POST \
  -H "Authorization: Bearer ck_dev_2f8a91b6c4e7d0358a19f6e2b7c4d8a0" \
  -F "file=@data/demo3.jpg" \
  http://127.0.0.1:8300/api/correct | jq
```

响应(身份证正反面会返回 2 个 items):
```json
{
  "request_id": "20260922_004612_d4e5f6",
  "count": 2,
  "elapsed_ms": 221,
  "upload": {"key": "...", "url": "...", "expires_in": 3600},
  "items": [
    {
      "index": 0,
      "score": 0.9987,
      "label": 0,
      "label_desc": "无需旋转",
      "layout": 0,
      "layout_desc": "原件",
      "polygon": [[120.5, 80.2], [660.1, 78.9], [662.3, 440.5], [122.7, 442.1]],
      "width": 540,
      "height": 360,
      "object": {"key": "...", "url": "...", "expires_in": 3600}
    },
    { "index": 1, "...": "..." }
  ]
}
```

直接把响应里的 `url` 粘到浏览器即可下载/预览(预签名 1 小时内有效)。

### 5.4 批量回归测试

```bash
# 服务起来后, 跑 data/ 下所有 demo 图
.venv/bin/python test_service.py --url http://127.0.0.1:8300
# 结果保存到 output/<原图名>/{overlay.jpg, corrected_*.jpg}
```

如果不想用 venv:
```bash
docker run --rm -v "$PWD":/w -w /w --network host \
  --entrypoint python card-correction:cpu \
  test_service.py --url http://127.0.0.1:8300
```

---

## 6. 验证清单(部署后必跑)

### 6.1 鉴权链路

```bash
B=http://127.0.0.1:8300
K='ck_dev_2f8a91b6c4e7d0358a19f6e2b7c4d8a0'

# 期望 200
curl -s -o /dev/null -w "health: %{http_code}\n" $B/health

# 期望 401
curl -s -o /dev/null -w "no-key: %{http_code}\n" \
  -X POST -F "file=@data/demo.jpg" $B/api/upload

# 期望 403
curl -s -o /dev/null -w "wrong-key: %{http_code}\n" \
  -X POST -H "X-API-Key: bad" -F "file=@data/demo.jpg" $B/api/upload

# 期望 200, body 含 url
curl -s -w "\nright-key: %{http_code}\n" \
  -X POST -H "X-API-Key: $K" -F "file=@data/demo.jpg" $B/api/upload
```

### 6.2 RustFS 写入

调用 `/api/upload` 后:
```bash
# 浏览器打开返回的 url, 应该看到刚上传的图
# 或用 mc / aws cli 查 bucket
aws --endpoint-url http://192.168.2.61:9001 s3 ls s3://card-correction/card-correction/$(date +%Y-%m-%d)/
```

### 6.3 Nacos 注册

浏览器打开:`http://192.168.2.61:8848/nacos`(默认账号 `nacos/nacos`,看你部署是否改过)

**服务管理 → 服务列表** 应能看到:
- 服务名: `card-correction`
- 分组: `DEFAULT_GROUP`
- 实例数: 1(CPU/GPU 单卡)或 2(GPU 双卡)
- 健康实例数 = 实例数

点进实例详情,核对:
- IP = `NACOS_REGISTER_IP` 传入的宿主机 IP
- 端口 = `NACOS_REGISTER_PORT`(8300 / 8301)
- 元数据 `version=2.0.0`

### 6.4 容器健康

```bash
docker ps --filter name=card-correction
# STATUS 应为 "Up X minutes (healthy)"
# 如果一直 (health: starting) 或 (unhealthy), 看 docker logs
```

---

## 7. 常见问题排查

| 现象 | 可能原因 | 排查/解决 |
|---|---|---|
| `/api/upload` 返回 `502 上传 RustFS 失败: ConnectTimeoutError` | RustFS 不可达 | 在容器内 `docker exec -it <name> python -c "import socket; socket.create_connection(('192.168.2.61',9001),3)"`;检查防火墙、RustFS 服务状态 |
| `/api/upload` 返回 `502 ... NoSuchBucket` | bucket 不存在且 `auto_create_bucket=false` | 改 `auto_create_bucket=true` 重启,或手动建桶 |
| `/api/upload` 返回 `502 ... SignatureDoesNotMatch` | access_key/secret_key 错 | 核对 RustFS 控制台凭据 |
| 启动日志 `Nacos 注册失败` 但服务正常 | Nacos 不可达 / 端口被防火墙拦 | 确认 8848 + 9848 + 9849 都通;Nacos 2.x 客户端走 gRPC,只开 8848 不够 |
| Nacos 服务列表里实例 IP 是容器内网 IP(如 `172.17.0.2`) | 没传 `NACOS_REGISTER_IP` | 必须在 compose 里设 `NACOS_REGISTER_IP: <宿主机 IP>`,否则消费方连不上 |
| 容器一直 `health: starting` | 启动慢(RustFS/Nacos 网络超时 + 模型加载) | 等 2-3 分钟;或调小 `s3_connect_timeout` / 关 `NACOS_ENABLED` 排查 |
| 端口 8300 被占 | 宿主机有别的进程在跑 | `lsof -nP -iTCP:8300 -sTCP:LISTEN` 找 PID,kill 掉或换端口 |
| 401 但我明明传了 key | header 名拼错 / 多个空格 | 必须是 `X-API-Key: <key>` 或 `Authorization: Bearer <key>`,大小写敏感 |
| 403 但 key 是对的 | 配置里有多个 key,你用的不在列表 | 看 `service_config.json` 的 `auth.api_keys` 或 `API_KEYS` 环境变量 |
| GPU 模式日志 `CUDA 不可用, 回退到 CPU` | 容器没拿到 GPU | 检查 `nvidia-container-toolkit`、`docker info \| grep -i runtime` 是否有 nvidia |
| 预签名 URL 浏览器打不开 / 403 | URL 过期(默认 1 小时)或客户端时钟漂移 | 调大 `url_expires_seconds`;同步服务器时间(NTP) |

### 看日志

```bash
# Docker
docker logs -f card-correction-cpu
docker logs --tail 200 card-correction-cpu

# 本地
tail -f nohup.out    # 如果用 nohup 启的
# 或前台跑直接看终端
```

### 进容器调试

```bash
docker exec -it card-correction-cpu bash

# 容器内手测 RustFS
python -c "
import sys; sys.path.insert(0,'/app')
import app; app.load_config()
print(app.get_s3().list_buckets())
"

# 容器内手测 Nacos
python -c "
import nacos
c = nacos.NacosClient('192.168.2.61:8848')
print(c.list_naming_instance('card-correction'))
"
```

---

## 8. 生产上线检查清单

上线前**必须**做的事:

- [ ] **换掉开发 key**:`service_config.json` 里的 `ck_dev_...` 是公开的(在 git 历史/聊天里都可能出现过),生产前必须替换
- [ ] **凭据移到环境变量**:`API_KEYS`、`NACOS_PASSWORD`、RustFS `secret_key` 不要写文件里
- [ ] **`service_config.json` 加 `.gitignore`**(如果项目要进 git):仓库里只留 `service_config.example.json`
- [ ] **预签名 URL 过期时间**根据业务调整(`url_expires_seconds`)
- [ ] **bucket 访问策略**保持私有(默认),不要为了图省事改公开读
- [ ] **Nacos 命名空间隔离**:dev/prod 用不同 namespace,避免环境串
- [ ] **日志收集**:容器 stdout 接入 ELK / Loki / 阿里云 SLS
- [ ] **监控告警**:
  - `/health` 探活(已有 Docker HEALTHCHECK)
  - RustFS 5xx 比率
  - Nacos 实例健康数 < 期望值告警
  - 推理 P99 延迟
- [ ] **资源限制**:compose 里已设 `memory: 8G`(GPU),按需调整;CPU 模式没设上限,建议加 `mem_limit`
- [ ] **HTTPS**:对外暴露必须套 Nginx / Traefik + TLS,API Key 不能走明文 HTTP
- [ ] **限流**:Nginx 层加 `limit_req`,防止单 key 被打爆

### 推荐的 compose 生产覆盖

新建 `docker-compose.prod.yml`:
```yaml
services:
  card-correction:
    extends:
      file: docker-compose.cpu.yml
      service: card-correction-cpu
    environment:
      API_KEYS: ${API_KEYS}                # 从 .env 读
      NACOS_REGISTER_IP: ${HOST_IP}
      NACOS_NAMESPACE: prod
      AUTH_ENABLED: "true"
    restart: always
    logging:
      driver: json-file
      options:
        max-size: "100m"
        max-file: "5"
```

配 `.env`(权限 600,**不进 git**):
```
API_KEYS=prod_key_aaa,prod_key_bbb
HOST_IP=192.168.2.100
```

启动:
```bash
docker compose -f docker-compose.prod.yml up -d
```

---

## 9. 升级 / 回滚

### 升级

```bash
git pull                                      # 或拷新代码进来
docker compose -f docker-compose.cpu.yml up -d --build
docker logs -f card-correction-cpu            # 看启动日志确认无 ERROR
```

### 回滚

镜像 tag 化是关键。每次发布打 tag:
```bash
docker tag card-correction:cpu card-correction:cpu-$(date +%Y%m%d-%H%M)
docker push <你的镜像仓库>/card-correction:cpu-20260922-1200
```

回滚:
```bash
docker tag <仓库>/card-correction:cpu-20260921-1800 card-correction:cpu
docker compose -f docker-compose.cpu.yml up -d
```

---

## 10. 关键文件清单

| 文件 | 作用 | 改动需重启 |
|---|---|---|
| `app.py` | FastAPI 主服务 | ✅ |
| `service_config.json` | 运行配置 | ✅(没挂 volume 时需重新 build) |
| `requirements.txt` | Python 依赖 | ✅(需重新 build) |
| `Dockerfile.cpu` / `Dockerfile.gpu` | 镜像构建 | ✅ |
| `docker-compose.*.yml` | 编排 | ✅ |
| `pytorch_model.pt` | 模型权重 124MB | ✅ |
| `configuration.json` | ModelScope 模型元数据 | ✅ |
| `test_service.py` | 批量回归脚本 | ❌(客户端) |
| `data/*.jpg` | demo 测试图 | ❌ |
| `README.md` | 用户文档 | ❌ |
| `OPERATIONS.md` | 本手册 | ❌ |

---

## 11. 联系信息

- RustFS 控制台: `http://192.168.2.61:9001`(具体路径看你部署版本)
- Nacos 控制台: `http://192.168.2.61:8848/nacos`
- 1Panel: `http://192.168.2.61:8080`
- 服务 Swagger: `http://<部署机 IP>:8300/docs`
