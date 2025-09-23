# imghdr.py — minimal shim for Python 3.13 (stdlib module removed)
# Provides imghdr.what() used by python-telegram-bot 13.x

def what(filename=None, h=None):
    # Detect by header if available
    if h:
        try:
            if h.startswith(b"\xff\xd8"):  # JPEG
                return "jpeg"
            if h.startswith(b"\x89PNG\r\n\x1a\n"):  # PNG
                return "png"
            if h.startswith(b"GIF87a") or h.startswith(b"GIF89a"):  # GIF
                return "gif"
            if h[:6] in (b"BM",):  # BMP
                return "bmp"
            if h.startswith(b"RIFF") and h[8:12] == b"WEBP":
                return "webp"
        except Exception:
            return None
    # Fallback: guess from filename extension
    if filename:
        fn = filename.lower()
        if fn.endswith((".jpg", ".jpeg")): return "jpeg"
        if fn.endswith(".png"): return "png"
        if fn.endswith(".gif"): return "gif"
        if fn.endswith(".bmp"): return "bmp"
        if fn.endswith(".webp"): return "webp"
    return None
