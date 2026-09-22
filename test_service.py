# -*- coding: utf-8 -*-
"""矫正效果批量测试脚本

用法(先启动服务):
    .venv/bin/uvicorn app:app --port 8300 &
    .venv/bin/python test_service.py [--url http://127.0.0.1:8300]

对 data/ 下所有 demo 图调用 /api/correct:
  1. 从返回的预签名 URL 下载矫正子图, 保存到 output/<原图名>/corrected_i.jpg
  2. 下载原图并叠加检测框, 保存到 output/<原图名>/overlay.jpg
  3. 打印汇总表
"""
import argparse
import time
from pathlib import Path

import cv2
import numpy as np
import requests

DATA_DIR = Path(__file__).resolve().parent / "data"
OUT_DIR = Path(__file__).resolve().parent / "output"


def fetch_image(url: str) -> np.ndarray:
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    arr = cv2.imdecode(np.frombuffer(r.content, dtype=np.uint8), cv2.IMREAD_COLOR)
    if arr is None:
        raise RuntimeError(f"无法解码图片: {url}")
    return arr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8300")
    args = ap.parse_args()

    r = requests.get(f"{args.url}/health", timeout=5)
    print(f"服务状态: {r.json()}")

    images = sorted(DATA_DIR.glob("*.jp*g")) + sorted(DATA_DIR.glob("*.png"))
    assert images, f"{DATA_DIR} 下没有测试图"

    total, total_ms = 0, 0
    for path in images:
        t0 = time.time()
        with open(path, "rb") as f:
            resp = requests.post(
                f"{args.url}/api/correct",
                files={"file": (path.name, f, "image/jpeg")},
                timeout=120,
            )
        wall = (time.time() - t0) * 1000
        resp.raise_for_status()
        j = resp.json()

        save_dir = OUT_DIR / path.stem
        save_dir.mkdir(parents=True, exist_ok=True)

        orig = fetch_image(j["upload"]["url"])
        for it in j["items"]:
            pts = np.array(it["polygon"], dtype=np.int32).reshape(-1, 1, 2)
            cv2.polylines(orig, [pts], True, (0, 0, 255), max(2, orig.shape[1] // 400))
            x, y = pts[0][0]
            cv2.putText(orig, f'#{it["index"]} {it["score"]:.2f}', (int(x), int(y) - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
        cv2.imwrite(str(save_dir / "overlay.jpg"), orig)

        for it in j["items"]:
            arr = fetch_image(it["object"]["url"])
            cv2.imwrite(str(save_dir / f'corrected_{it["index"]}.jpg'), arr)

        total += j["count"]
        total_ms += j["elapsed_ms"]
        descs = ", ".join(
            f'#{it["index"]}: {it["score"]:.3f} {it["label_desc"]} {it["layout_desc"]}'
            f' {it["width"]}x{it["height"]}'
            for it in j["items"]
        ) or "-"
        print(
            f"{path.name:12s} -> {j['count']} 张子图 | 推理 {j['elapsed_ms']}ms (端到端 {wall:.0f}ms)\n"
            f"             {descs}\n"
            f"             已保存: {save_dir}/"
        )

    print(f"共 {len(images)} 张图, 检出 {total} 张子图, 平均推理 {total_ms/max(len(images),1):.0f}ms")


if __name__ == "__main__":
    main()
