import json
import os
import time

# V11: Persistent Disk
_data_dir = "/data" if os.path.isdir("/data") else "."
FEATURES_FILE = os.path.join(_data_dir, "features.json")

DEFAULT_FEATURES = {
    "new_listing_filter": {"enabled": True, "shadow": False, "win": 0, "loss": 0, "desc": "Phase 2: Filter coins < 14 days old"},
    "z_score_normalization": {"enabled": False, "shadow": True, "win": 0, "loss": 0, "desc": "Phase 4.1: Z-Score Score Normalization"},
    "atr_position_sizing": {"enabled": False, "shadow": True, "win": 0, "loss": 0, "desc": "Phase 4.2: Volatility-Adjusted Sizing"},
    "atr_trailing_stop": {"enabled": False, "shadow": True, "win": 0, "loss": 0, "desc": "Phase 4.3: ATR-based Trailing Stop"},
    "correlation_filter": {"enabled": True, "shadow": False, "win": 0, "loss": 0, "desc": "Phase 5.2: Avoid duplicated exposure"},
    "drawdown_protection": {"enabled": True, "shadow": False, "win": 0, "loss": 0, "desc": "Phase 5.3: Session Drawdown Protection"},
    "smc_quality_grading": {"enabled": False, "shadow": True, "win": 0, "loss": 0, "desc": "Phase 7.1: SMC A/B/C Quality Multiplier"}
}

def _load_features():
    if not os.path.exists(FEATURES_FILE):
        return DEFAULT_FEATURES.copy()
    try:
        with open(FEATURES_FILE, "r") as f:
            data = json.load(f)
            # Merge with defaults to ensure all keys exist
            for k, v in DEFAULT_FEATURES.items():
                if k not in data:
                    data[k] = v
            return data
    except Exception:
        return DEFAULT_FEATURES.copy()

def _save_features(data):
    try:
        with open(FEATURES_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"Error saving features: {e}")

class FeatureManager:
    def __init__(self):
        self.features = _load_features()
        _save_features(self.features)

    def is_enabled(self, feature_name: str) -> bool:
        return self.features.get(feature_name, {}).get("enabled", False)

    def is_shadow(self, feature_name: str) -> bool:
        return self.features.get(feature_name, {}).get("shadow", False)

    def record_result(self, feature_name: str, is_win: bool):
        if feature_name in self.features:
            if is_win:
                self.features[feature_name]["win"] += 1
            else:
                self.features[feature_name]["loss"] += 1
            _save_features(self.features)

    def evaluate_features(self):
        """Called daily to turn features on/off based on performance."""
        changed = False
        for name, data in self.features.items():
            total = data["win"] + data["loss"]
            if total >= 20: # Evaluate after 20 trades
                wr = data["win"] / total
                if data["enabled"] and wr < 0.40:
                    print(f"🔄 Feature '{name}' is underperforming (WR: {wr:.2f}). Disabling and moving to shadow mode.")
                    data["enabled"] = False
                    data["shadow"] = True
                    data["win"], data["loss"] = 0, 0
                    changed = True
                elif not data["enabled"] and data["shadow"] and wr > 0.55:
                    print(f"🔄 Feature '{name}' works well in shadow (WR: {wr:.2f}). Enabling.")
                    data["enabled"] = True
                    data["shadow"] = False
                    data["win"], data["loss"] = 0, 0
                    changed = True
        
        if changed:
            _save_features(self.features)

# Global instance
manager = FeatureManager()
