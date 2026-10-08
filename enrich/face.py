"""Face verification with OpenCV's open-source YuNet (detector) + SFace (embedder).

Both are small ONNX models from opencv_zoo (Apache-2.0); they run on CPU in ~20 ms
and need nothing beyond opencv-python. SFace's published same-identity threshold for
cosine similarity is 0.363.
"""

import re
import urllib.request

import cv2
import numpy as np

from . import config
from .net import log

MODELS = {
    "det": ("face_detection_yunet_2023mar.onnx",
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"),
    "rec": ("face_recognition_sface_2021dec.onnx",
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"),
}
SFACE_SAME = 0.363


def drive_direct(url: str | None) -> str | None:
    """Google Drive share link -> direct download URL."""
    if not url:
        return None
    m = re.search(r"/d/([\w-]{10,})", url) or re.search(r"[?&]id=([\w-]{10,})", url)
    return f"https://drive.google.com/uc?export=download&id={m.group(1)}" if m else url


def _model_path(key: str) -> str:
    name, url = MODELS[key]
    path = config.MODELS_DIR / name
    if not path.exists():
        config.MODELS_DIR.mkdir(parents=True, exist_ok=True)
        log(f"  [face] downloading {name}")
        urllib.request.urlretrieve(url, path)
    return str(path)


class FaceMatcher:
    def __init__(self, fetcher, cache):
        self.fetcher = fetcher
        self.cache = cache
        self.det = cv2.FaceDetectorYN.create(_model_path("det"), "", (320, 320), 0.7, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(_model_path("rec"), "")

    def _image(self, url: str, bucket: str) -> np.ndarray | None:
        try:
            r = self.fetcher.get(url, bucket=bucket, cache_ns="img",
                                 linkedin=False)
        except Exception as e:
            log(f"  [face] image fetch failed: {e}")
            return None
        if r.status != 200 or not r.content:
            return None
        arr = np.frombuffer(r.content, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return img

    def embedding(self, url: str | None, bucket: str = "web") -> dict | None:
        """Returns {'emb': list[float], 'score': det_conf, 'size': px} or None if no face."""
        if not url:
            return None
        key = url.split("&t=")[0]
        hit = self.cache.get_json("face", key)
        if hit is not None:
            return hit or None
        img = self._image(url, bucket)
        out = {}
        if img is not None:
            h, w = img.shape[:2]
            scale = 640 / max(h, w) if max(h, w) > 640 else 1.0
            if scale != 1.0:
                img = cv2.resize(img, (int(w * scale), int(h * scale)))
                h, w = img.shape[:2]
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(img)
            if faces is not None and len(faces):
                f = max(faces, key=lambda r: r[2] * r[3])        # largest face
                aligned = self.rec.alignCrop(img, f)
                emb = self.rec.feature(aligned).flatten()
                out = {"emb": emb.tolist(), "score": float(f[-1]),
                       "size": int(min(f[2], f[3])), "n_faces": int(len(faces))}
        self.cache.set_json("face", key, out)
        return out or None

    @staticmethod
    def similarity(a: dict | None, b: dict | None) -> float | None:
        if not a or not b:
            return None
        x, y = np.asarray(a["emb"], np.float32), np.asarray(b["emb"], np.float32)
        return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))
