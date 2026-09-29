"""미디어: 분석용 768px 사본(EXIF 는 읽기만), 얼굴 흐림(선택), 여행 동선 정렬, FFmpeg 클립 편집.
사진 증식·EXIF 조작은 하지 않는다 — 원본은 사용자가 폰에서 직접 업로드."""
import datetime as dt
import io
import shutil
import subprocess

import pillow_heif
from PIL import Image, ImageFilter, ImageOps

from store import CFG

pillow_heif.register_heif_opener()


def save_photo(raw, dest, meta=None):
    """긴 변 768px JPEG 사본 저장(메타데이터 없음). 반환: 촬영시각·위치 (단축어 meta > EXIF)."""
    img = Image.open(io.BytesIO(raw))
    info = _exif(img)
    info.update({k: meta[k] for k in ("taken_at", "lat", "lon") if meta and meta.get(k) is not None})
    img = ImageOps.exif_transpose(img).convert("RGB")
    img.thumbnail((768, 768))
    if CFG.get("media", {}).get("blur_faces"):
        img = _blur_faces(img)
    img.save(dest, "JPEG", quality=85)
    return info


def _exif(img):
    out = {}
    try:
        ex = img.getexif()
        t = ex.get_ifd(0x8769).get(36867) or ex.get(306)  # DateTimeOriginal / DateTime
        if t:
            out["taken_at"] = dt.datetime.strptime(t, "%Y:%m:%d %H:%M:%S").isoformat()
        gps = ex.get_ifd(0x8825)
        if 2 in gps and 4 in gps:
            deg = lambda d, ref: (float(d[0]) + float(d[1]) / 60 + float(d[2]) / 3600) * (-1 if ref in ("S", "W") else 1)
            out["lat"], out["lon"] = deg(gps[2], gps.get(1)), deg(gps[4], gps.get(3))
    except Exception:
        pass  # 브라우저에서 줄인 사진은 EXIF 가 없음 → 단축어 meta 로 받음
    return out


def _blur_faces(img):
    """무료 티어 입력은 서비스 개선에 쓰일 수 있어 타인 얼굴을 흐림. 선택 의존성 opencv-python-headless."""
    import cv2
    import numpy as np
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    for x, y, w, h in det.detectMultiScale(cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY), 1.1, 5):
        box = (int(x), int(y), int(x + w), int(y + h))
        img.paste(img.crop(box).filter(ImageFilter.GaussianBlur(max(4, int(w) // 4))), box)
    return img


def route_order(photos, places):
    """여행: 사진을 촬영시각 순으로 번호 매기고, 장소는 가까운 사진의 가장 이른 시각 순으로 정렬."""
    photos.sort(key=lambda p: p.get("taken_at") or "~")
    for i, p in enumerate(photos, 1):
        p["no"] = i
    first = {}
    geo = [(i, pl) for i, pl in enumerate(places) if pl.get("lat")]
    for p in photos:
        if geo and p.get("lat") is not None:
            # 도시 규모 최근접 비교라 위도 37° 근처 경도 보정(0.8)만 둔 평면 거리
            i = min(geo, key=lambda g: (g[1]["lat"] - p["lat"]) ** 2 + ((g[1]["lng"] - p["lon"]) * 0.8) ** 2)[0]
            first.setdefault(i, p.get("taken_at") or "~")
    places[:] = [places[i] for i in sorted(range(len(places)), key=lambda i: (first.get(i, "~"), i))]


def _ts(s):
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},{int(s % 1 * 1000):03d}"


def make_clip(folder, videos, captions, font=None):
    """사용자 영상 2~4개 → 세로 1080x1920 · 자막 · 최대 60초 clip.mp4. 반환: 길이(초), 영상/ffmpeg 없으면 None."""
    if not videos or not shutil.which("ffmpeg"):
        return None
    probe = lambda v, *a: subprocess.run(["ffprobe", "-v", "error", *a, "-of", "csv=p=0", v], cwd=folder,
                                         capture_output=True, text=True).stdout.strip()
    durs = [float(probe(v, "-show_entries", "format=duration") or 0) for v in videos]
    audio = all(probe(v, "-select_streams", "a", "-show_entries", "stream=index") for v in videos)
    srt, t = [], 0.0
    for i, (d, c) in enumerate(zip(durs, list(captions) + [""] * len(videos)), 1):
        if c and t < 60:
            srt.append(f"{len(srt) + 1}\n{_ts(t)} --> {_ts(min(t + d, 60))}\n{c}\n")
        t += d
    (folder / "clip.srt").write_text("\n".join(srt), encoding="utf-8")
    n = len(videos)
    fit = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30"
    style = f":force_style='FontName={font},FontSize=14,Outline=2'" if font else ""
    fc = ("".join(f"[{i}:v]{fit}[v{i}];" for i in range(n))
          + "".join(f"[v{i}]" + (f"[{i}:a]" if audio else "") for i in range(n))
          + f"concat=n={n}:v=1:a={int(audio)}[cv]" + ("[a]" if audio else "")
          + (f";[cv]subtitles=clip.srt{style}[out]" if srt else ";[cv]null[out]"))
    subprocess.run(["ffmpeg", "-y", *[a for v in videos for a in ("-i", v)], "-filter_complex", fc, "-map", "[out]",
                    *(["-map", "[a]", "-c:a", "aac"] if audio else ["-an"]),
                    "-t", "60", "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p", "clip.mp4"],
                   cwd=folder, check=True, capture_output=True)
    return min(sum(durs), 60.0)
