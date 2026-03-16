"""Double-Loop Learning integration."""
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

SHANGHAI_TZ = ZoneInfo('Asia/Shanghai')

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)

def log_prediction(task: str, prediction: str, confidence: str = "M", uncertainty: str = ""):
    """Record a prediction before starting a task."""
    subprocess.run([
        "python3", "/home/ubuntu/.openclaw/workspace/scripts/prediction_logger.py",
        "log", task, prediction, confidence, uncertainty
    ], capture_output=True)

def log_outcome(task: str, outcome: str, delta: str, lesson: str):
    """Record outcome after task completion."""
    subprocess.run([
        "python3", "/home/ubuntu/.openclaw/workspace/scripts/prediction_logger.py",
        "complete", task, outcome, delta, lesson
    ], capture_output=True)

class LearningContext:
    """Context manager for automatic prediction/outcome logging."""
    
    def __init__(self, task: str, prediction: str, confidence: str = "M", uncertainty: str = ""):
        self.task = task
        self.prediction = prediction
        self.confidence = confidence
        self.uncertainty = uncertainty
        self.outcome = None
        self.delta = None
        self.lesson = None
    
    def __enter__(self):
        log_prediction(self.task, self.prediction, self.confidence, self.uncertainty)
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            self.outcome = f"Failed: {exc_val}"
            self.delta = "Error occurred"
            self.lesson = "Need error handling improvement"
        else:
            # Outcome must be set via set_outcome() before exit
            pass
        log_outcome(self.task, self.outcome or "Completed", self.delta or "", self.lesson or "")
    
    def set_outcome(self, outcome: str, delta: str, lesson: str):
        self.outcome = outcome
        self.delta = delta
        self.lesson = lesson
