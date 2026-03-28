import joblib
import numpy as np
from sklearn.linear_model import LinearRegression
from config import USE_TQDM_PROGRESS_BAR


def _fix_sklearn_compat(model):
    """
    Patch a loaded sklearn model to add any attributes missing due to version
    mismatch (e.g. 'tol' added in newer scikit-learn versions).
    """
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
        self.model_path, self.training_threshold = model_path, training_threshold
        self.new_data = []
        self.historic_X, self.historic_y = [], []
        try:
            self.model, self.historic_X, self.historic_y = joblib.load(self.model_path)
            # Patch missing attributes caused by scikit-learn version differences
            self.model = _fix_sklearn_compat(self.model)
            print(f"--- 成功加载本地【时长】预测模型。模型已有 {sum(len(x) for x in self.historic_X)} 条历史数据。 ---")
        except (FileNotFoundError, EOFError, ValueError) as e:
            print(f"--- 未找到或无法解析本地时长模型 ({e.__class__.__name__})，将创建一个新模型。 ---")
            self.model = LinearRegression()

    def _get_features(self, text: str) -> np.ndarray:
        return np.array([len(text)]).reshape(1, -1)

    def predict_duration(self, text: str) -> float:
        if not hasattr(self.model, "coef_") or self.model.coef_ is None:
            return len(text) / 6.0
        predicted_duration = self.model.predict(self._get_features(text))[0]
        return max(0.1, predicted_duration)

    def add_data_point_and_retrain(self, text: str, actual_raw_duration_s: float):
        if actual_raw_duration_s <= 0: return
        self.new_data.append({"features": self._get_features(text)[0], "duration": actual_raw_duration_s})
        if not USE_TQDM_PROGRESS_BAR:
            print(f"  -> 已收集 {len(self.new_data)}/{self.training_threshold} 个新数据点。")
        if len(self.new_data) >= self.training_threshold:
            self.train()

    def train(self):
        if not self.new_data: return
        X_new = np.array([d['features'] for d in self.new_data])
        y_new = np.array([d['duration'] for d in self.new_data])
        X_combined = np.concatenate(self.historic_X + [X_new]) if self.historic_X else X_new
        y_combined = np.concatenate(self.historic_y + [y_new]) if self.historic_y else y_new
        self.model.fit(X_combined, y_combined)
        self.historic_X.append(X_new)
        self.historic_y.append(y_new)
        self.new_data = []
        try:
            joblib.dump((self.model, self.historic_X, self.historic_y), self.model_path)
        except Exception as e:
            print(f"!! 保存时长模型时发生错误: {e}")
