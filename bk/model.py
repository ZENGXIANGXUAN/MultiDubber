import joblib
import numpy as np
from sklearn.linear_model import LinearRegression
from config import USE_TQDM_PROGRESS_BAR
from logger import log as _log

MAX_HISTORY = 5000  # 滑动窗口上限，防止内存无界增长


def _fix_sklearn_compat(model: LinearRegression) -> LinearRegression:
    defaults = {
        "tol": 1e-4,
        "positive": False,
        "n_jobs": None,
    }
    for attr, val in defaults.items():
        if not hasattr(model, attr):
            setattr(model, attr, val)
    return model


class DurationPredictor:
    def __init__(self, model_path: str, training_threshold: int):
        self.model_path = model_path
        self.training_threshold = training_threshold
        self.new_data: list[dict] = []
        self.historic_X: np.ndarray = np.empty((0, 1))
        self.historic_y: np.ndarray = np.empty((0,))
        try:
            loaded = joblib.load(self.model_path)
            if isinstance(loaded, tuple) and len(loaded) == 3:
                self.model, hx, hy = loaded
                # Backward compat: old format stored list of arrays
                if isinstance(hx, list):
                    self.historic_X = np.concatenate(hx) if hx else np.empty((0, 1))
                    self.historic_y = np.concatenate(hy) if hy else np.empty((0,))
                else:
                    self.historic_X = hx if hx is not None and len(hx) > 0 else np.empty((0, 1))
                    self.historic_y = hy if hy is not None and len(hy) > 0 else np.empty((0,))
                self.model = _fix_sklearn_compat(self.model)
                _log("INIT", f"成功加载本地时长预测模型。模型已有 {len(self.historic_X)} 条历史数据。")
            else:
                raise ValueError("Unknown model format")
        except (FileNotFoundError, EOFError, ValueError) as e:
            _log("INIT", f"未找到或无法解析本地时长模型 ({e.__class__.__name__})，将创建一个新模型。")
            self.model: LinearRegression = LinearRegression()

    def _get_features(self, text: str) -> np.ndarray:
        return np.array([len(text)]).reshape(1, -1)

    def predict_duration(self, text: str) -> float:
        if not hasattr(self.model, "coef_") or self.model.coef_ is None:
            return len(text) / 6.0
        predicted_duration = self.model.predict(self._get_features(text))[0]
        return max(0.1, predicted_duration)

    def add_data_point_and_retrain(self, text: str, actual_raw_duration_s: float) -> None:
        if actual_raw_duration_s <= 0:
            return
        self.new_data.append({"features": self._get_features(text)[0], "duration": actual_raw_duration_s})
        if not USE_TQDM_PROGRESS_BAR:
            _log("INFO", f"已收集 {len(self.new_data)}/{self.training_threshold} 个新数据点。")
        if len(self.new_data) >= self.training_threshold:
            self.train()

    def train(self) -> None:
        if not self.new_data:
            return
        X_new = np.array([d['features'] for d in self.new_data])
        y_new = np.array([d['duration'] for d in self.new_data])

        if len(self.historic_X) > 0:
            X_combined = np.concatenate([self.historic_X, X_new])
            y_combined = np.concatenate([self.historic_y, y_new])
        else:
            X_combined, y_combined = X_new, y_new

        if len(X_combined) > MAX_HISTORY:
            X_combined = X_combined[-MAX_HISTORY:]
            y_combined = y_combined[-MAX_HISTORY:]

        self.model.fit(X_combined, y_combined)
        self.historic_X = X_combined
        self.historic_y = y_combined
        self.new_data = []

        try:
            joblib.dump((self.model, self.historic_X, self.historic_y), self.model_path)
        except Exception as e:
            _log("ERROR", f"保存时长模型时发生错误: {e}")
