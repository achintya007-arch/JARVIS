# perception/vision.py
import cv2
from ultralytics import YOLO
import asyncio

class VisionEngine:
    def __init__(self):
        self.model = YOLO("yolov8n.pt")  # nano fits your VRAM easily alongside LLM
        self.cap = cv2.VideoCapture(0)

    async def describe_scene(self) -> str:
        ret, frame = self.cap.read()
        if not ret:
            return "Camera feed unavailable."
        results = self.model(frame, verbose=False)
        detected = [self.model.names[int(c)] for r in results for c in r.boxes.cls]
        if not detected:
            return "No objects detected in frame."
        from collections import Counter
        counts = Counter(detected)
        parts = [f"{v} {k}{'s' if v > 1 else ''}" for k, v in counts.items()]
        return "I can see " + ", ".join(parts) + "."

    async def capture_and_analyze(self) -> str:
        """For 'what do you see?' queries"""
        return await asyncio.to_thread(self._run_detection)

    def _run_detection(self) -> str:
        ret, frame = self.cap.read()
        if not ret:
            return "Camera unavailable."
        results = self.model(frame, verbose=False)
        objects = list(set([self.model.names[int(c)] for r in results for c in r.boxes.cls]))
        return f"Currently detecting: {', '.join(objects) if objects else 'nothing unusual'}."