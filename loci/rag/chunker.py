"""Chinese-aware text chunker with overlap."""
import re


def chunk_text(text: str, size: int = 400, overlap: int = 80) -> list[str]:
    """Split text into overlapping chunks, breaking at paragraph/sentence boundaries."""
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            # Prefer breaking at paragraph > sentence > clause > space
            for sep in ['\n\n', '。', '！', '？', '\n', '；', '，', ' ']:
                pos = text.rfind(sep, start + size // 2, end)
                if pos > start:
                    end = pos + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        next_start = end - overlap
        if next_start <= start:
            next_start = start + max(1, size // 4)
        start = next_start

    return chunks
