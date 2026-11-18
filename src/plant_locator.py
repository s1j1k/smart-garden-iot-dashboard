"""Pre-trained, open-vocabulary detection of the net pots and plants.

Uses Grounding DINO (IDEA-Research/grounding-dino-tiny, Apache-2.0) through
Hugging Face transformers: it finds objects described in text ("net pot",
"plant") without being trained on this garden. It knows what a pot is, which
colour thresholds do not, so it anchors the plants: growth_detection.py gives
each net pot one plant, assigns leaves to the nearest pot, and drops green
below a pot's rim (reflections in the lid).

Too slow for every frame on a Raspberry Pi 4 (minutes, not seconds), and the
pots do not move once the camera is fixed, so scripts/locate_plants.py runs it
on the latest frame now and then and saves the result to data/camera/layout.json.

Needs torch and transformers (requirements-ai.txt); the model (~700 MB) is
downloaded from Hugging Face on first use.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

MODEL_ID = "IDEA-Research/grounding-dino-tiny"
MODEL_LICENCE = "Apache-2.0"
PROMPT = "net pot. plant."
BOX_THRESHOLD = 0.2        # keep weak boxes; pots are then filtered by POT_MIN_SCORE
TEXT_THRESHOLD = 0.25
POT_MIN_SCORE = 0.25     # the basil pot on the Pi's first real frame scored 0.28
OVERLAP_IOU = 0.5        # boxes of one kind overlapping more than this are one object
MAX_POT_FRAC = 0.5       # a "pot" box wider than half the frame is the whole reservoir

_model = None


def _load():
    global _model
    if _model is None:
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        torch.set_num_threads(2)        # leave the Pi's other cores to the dashboard
        processor = AutoProcessor.from_pretrained(MODEL_ID)
        model = AutoModelForZeroShotObjectDetection.from_pretrained(MODEL_ID).eval()
        _model = (torch, processor, model)
    return _model


def detect(image) -> list[dict]:
    """Raw detections on a PIL image: ``label``, ``score``, ``box`` (x0, y0, x1, y1 px)."""
    torch, processor, model = _load()
    inputs = processor(images=image, text=PROMPT, return_tensors="pt")
    with torch.no_grad():
        outputs = model(**inputs)
    result = processor.post_process_grounded_object_detection(
        outputs, inputs.input_ids, threshold=BOX_THRESHOLD, text_threshold=TEXT_THRESHOLD,
        target_sizes=[image.size[::-1]],
    )[0]
    labels = result.get("text_labels", result.get("labels"))
    return [
        {"label": str(label), "score": float(score), "box": [float(v) for v in box]}
        for score, label, box in zip(result["scores"], labels, result["boxes"])
    ]


def _iou(a, b) -> float:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _distinct(boxes: list[dict]) -> list[dict]:
    """Highest-scoring box of each group that overlaps by more than OVERLAP_IOU."""
    kept: list[dict] = []
    for det in sorted(boxes, key=lambda d: -d["score"]):
        if all(_iou(det["box"], k["box"]) <= OVERLAP_IOU for k in kept):
            kept.append(det)
    return kept


def layout_from_detections(detections: list[dict], size: tuple[int, int]) -> dict:
    """Pots and plants as fractions of the frame, sorted left to right.

    ``size`` is (width, height). "Pots" as wide as the reservoir are dropped,
    and a pot needs POT_MIN_SCORE.
    """
    width, height = size

    def frac(det):
        x0, y0, x1, y1 = det["box"]
        return {"box": [x0 / width, y0 / height, x1 / width, y1 / height],
                "score": round(det["score"], 3)}

    def kind(word):
        return [d for d in detections if word in d["label"].lower()]

    # A box labelled "net pot plant" spans pot, plant and reflection: not a pot.
    pots = [d for d in kind("pot")
            if "plant" not in d["label"].lower()
            and d["score"] >= POT_MIN_SCORE
            and (d["box"][2] - d["box"][0]) / width <= MAX_POT_FRAC]
    plants = kind("plant")
    return {
        "pots": sorted((frac(d) for d in _distinct(pots)), key=lambda p: p["box"][0]),
        "plants": sorted((frac(d) for d in _distinct(plants)), key=lambda p: p["box"][0]),
        "model": MODEL_ID,
    }


def locate(image_path: Path) -> dict:
    """Run the model on one photo; the layout plus where and when it came from."""
    from PIL import Image

    image = Image.open(image_path).convert("RGB")
    started = datetime.now()
    layout = layout_from_detections(detect(image), image.size)
    layout["image"] = image_path.name
    layout["source_mtime"] = image_path.stat().st_mtime
    layout["located_at"] = started.strftime("%Y-%m-%d %H:%M:%S")
    layout["seconds"] = round((datetime.now() - started).total_seconds(), 1)
    return layout


def draw_layout(image_path: Path, layout: dict, out_path: Path) -> None:
    """The photo with the model's pots and plants drawn on, for the Camera page."""
    from PIL import Image, ImageDraw

    image = Image.open(image_path).convert("RGB")
    width, height = image.size
    draw = ImageDraw.Draw(image)
    line = max(2, width // 400)
    for kind, colour in (("pots", (0, 200, 220)), ("plants", (120, 230, 60))):
        for number, item in enumerate(layout[kind], start=1):
            x0, y0, x1, y1 = item["box"]
            box = [x0 * width, y0 * height, x1 * width, y1 * height]
            draw.rectangle(box, outline=colour, width=line)
            draw.text((box[0] + 4, box[1] + 4),
                      f"{kind[:-1]} {number} {item['score']:.2f}", fill=colour)
    temporary = out_path.with_name(out_path.stem + ".tmp" + out_path.suffix)
    image.save(temporary, quality=85)
    temporary.replace(out_path)
