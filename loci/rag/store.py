"""ChromaDB-backed vector store for Loci RAG."""
import hashlib
from pathlib import Path

import chromadb

from .embedder import embed_batch, embed_one
from .chunker import chunk_text

_BATCH = 32  # embed chunks in batches of this size


def _cid(prefix: str, key: str) -> str:
    """Stable collection name: loci_{prefix}_{12-char hex}."""
    h = hashlib.md5(key.encode()).hexdigest()[:12]
    return f"loci_{prefix}_{h}"


class LociRAG:
    def __init__(self, db_path: str):
        Path(db_path).mkdir(parents=True, exist_ok=True)
        self._db = chromadb.PersistentClient(path=db_path)

    # ── internals ─────────────────────────────────────────────────────────

    def _col(self, cid: str):
        try:
            return self._db.get_collection(cid)
        except Exception:
            return None

    def _fresh(self, cid: str, mtime: int) -> bool:
        col = self._col(cid)
        return col is not None and str(col.metadata.get("mtime")) == str(mtime)

    def _index(self, cid: str, chunks: list[str], metas: list[dict], mtime: int) -> None:
        try:
            self._db.delete_collection(cid)
        except Exception:
            pass
        col = self._db.create_collection(cid, metadata={"mtime": str(mtime)})
        embeddings: list = []
        for i in range(0, len(chunks), _BATCH):
            embeddings.extend(embed_batch(chunks[i:i + _BATCH]))
        col.add(
            ids=[f"{cid}_{i}" for i in range(len(chunks))],
            documents=chunks,
            embeddings=embeddings,
            metadatas=metas,
        )

    # ── public API ────────────────────────────────────────────────────────

    def index_file(self, path: str, text: str, mtime: int) -> tuple[str, int, bool]:
        """Index a single file. Returns (collection_id, n_chunks, was_cached)."""
        cid = _cid("f", path)
        if self._fresh(cid, mtime):
            return cid, self._col(cid).count(), True
        chunks = chunk_text(text)
        if not chunks:
            return cid, 0, False
        name = Path(path).name
        self._index(cid, chunks, [{"src": name, "i": i} for i, _ in enumerate(chunks)], mtime)
        return cid, len(chunks), False

    def index_folder(self, path: str, file_texts: dict[str, str], mtime: int) -> tuple[str, int, bool]:
        """Index all files in a folder. file_texts = {rel_path: text}."""
        cid = _cid("d", path)
        if self._fresh(cid, mtime):
            return cid, self._col(cid).count(), True
        all_chunks, all_metas = [], []
        for rel, text in file_texts.items():
            for i, chunk in enumerate(chunk_text(text)):
                all_chunks.append(chunk)
                all_metas.append({"src": rel, "i": i})
        if not all_chunks:
            return cid, 0, False
        self._index(cid, all_chunks, all_metas, mtime)
        return cid, len(all_chunks), False

    def index_text(self, text: str, source: str = "upload") -> tuple[str, int]:
        """Index arbitrary text (uploaded file). Collection persists keyed by content hash."""
        cid = _cid("u", source + text[:120])
        if self._col(cid):
            return cid, self._col(cid).count()
        chunks = chunk_text(text)
        if not chunks:
            return cid, 0
        self._index(cid, chunks, [{"src": source, "i": i} for i, _ in enumerate(chunks)], 0)
        return cid, len(chunks)

    def get_all(self, cid: str) -> list[str]:
        """按原文顺序返回全部切块（用于"整文精读"：文档不超窗口时喂全文，不做检索）。

        顺序按 metadata 的 (src, i) 排序，保证多文件文件夹分组、单文件按块序还原。
        切块间有 overlap，拼接会有少量重复，对模型无碍。
        """
        col = self._col(cid)
        if col is None or col.count() == 0:
            return []
        res = col.get(include=["documents", "metadatas"])
        docs = res.get("documents") or []
        metas = res.get("metadatas") or []
        order = sorted(
            range(len(docs)),
            key=lambda k: ((metas[k] or {}).get("src", ""), (metas[k] or {}).get("i", 0))
                          if k < len(metas) else ("", 0),
        )
        return [docs[k] for k in order]

    def retrieve(self, cid: str, question: str, n: int = 4) -> list[str]:
        """Semantic search: return top-n chunks for question."""
        col = self._col(cid)
        if col is None or col.count() == 0:
            return []
        q_emb = embed_one(question)
        res = col.query(
            query_embeddings=[q_emb],
            n_results=min(n, col.count()),
            include=["documents"],
        )
        return res["documents"][0] if res["documents"] else []
