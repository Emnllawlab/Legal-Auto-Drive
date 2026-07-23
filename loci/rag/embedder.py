"""Ollama embedding API wrapper — bge-m3 (1024d，多语言，中文法律检索强）。

从 nomic-embed-text(768d) 换成 bge-m3(1024d)：nomic 对中文检索弱（实测问"仲裁"
会漏掉仲裁条款），bge-m3 中文明显更准，从而可用"快检索"取代"慢整文精读"。
注意维度 768→1024 不兼容，切换后须清空重建向量库。可用 env LOCI_EMBED 覆盖。
"""
import json
import os
import urllib.request as _req

EMBED_URL   = "http://localhost:11434/api/embed"
EMBED_MODEL = os.environ.get("LOCI_EMBED", "bge-m3")


def embed_batch(texts: list[str], model: str = EMBED_MODEL) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": texts}).encode()
    req = _req.Request(EMBED_URL, data=payload,
                       headers={"Content-Type": "application/json"}, method="POST")
    with _req.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    if "error" in data:
        raise RuntimeError(f"Ollama: {data['error']}")
    return data["embeddings"]


def embed_one(text: str, model: str = EMBED_MODEL) -> list[float]:
    return embed_batch([text], model)[0]


def model_available(model: str = EMBED_MODEL) -> bool:
    try:
        embed_one("ping", model)
        return True
    except Exception:
        return False
