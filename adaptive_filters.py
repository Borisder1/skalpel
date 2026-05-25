class AdaptiveFilterManager:
    """Розумний менеджер фільтрів з авто-фолбеком."""

    LADDER = [
        {"level": 1, "adx": 15, "vol": 0.7, "fvg": 0.08, "label": "STRICT"},
        {"level": 2, "adx": 12, "vol": 0.6, "fvg": 0.06, "label": "NORMAL"},
        {"level": 3, "adx": 10, "vol": 0.5, "fvg": 0.05, "label": "RELAXED"},
        {"level": 4, "adx": 8, "vol": 0.4, "fvg": 0.04, "label": "MINIMAL"},
        {"level": 5, "adx": 0, "vol": 0.0, "fvg": 0.00, "label": "DIAGNOSTIC"},
    ]

    DRY_CYCLES_TO_FALLBACK = 10
    SETUPS_TO_UPGRADE = 3

    def __init__(self):
        self.current_level = 0
        self.dry_streak = 0
        self.setup_streak = 0
        self.total_fallbacks = 0
        self.total_upgrades = 0
        self.level_history = []

    def get_filters(self) -> dict:
        return self.LADDER[self.current_level].copy()

    def report_cycle(self, setups_found: int) -> dict:
        changed = False
        old_level = self.current_level

        if setups_found == 0:
            self.dry_streak += 1
            self.setup_streak = 0
            if self.dry_streak >= self.DRY_CYCLES_TO_FALLBACK and self.current_level < len(self.LADDER) - 1:
                self.current_level += 1
                self.dry_streak = 0
                self.total_fallbacks += 1
                changed = True
        else:
            self.setup_streak += 1
            self.dry_streak = 0
            if self.setup_streak >= self.SETUPS_TO_UPGRADE and self.current_level > 0:
                self.current_level -= 1
                self.setup_streak = 0
                self.total_upgrades += 1
                changed = True

        result = {
            "changed": changed,
            "old_level": old_level,
            "new_level": self.current_level,
            "filters": self.get_filters(),
            "dry_streak": self.dry_streak,
            "setup_streak": self.setup_streak,
            "is_diagnostic": self.current_level == len(self.LADDER) - 1,
        }

        if changed:
            direction = "⬇ FALLBACK" if self.current_level > old_level else "⬆ UPGRADE"
            self.level_history.append({
                "direction": direction,
                "from": self.LADDER[old_level]["label"],
                "to": self.LADDER[self.current_level]["label"],
            })
        return result

    def get_status(self) -> str:
        f = self.get_filters()
        return (
            f"[LEVEL {f['level']} {f['label']}] "
            f"ADX≥{f['adx']} VOL≥{f['vol']} FVG≥{f['fvg']} | "
            f"dry={self.dry_streak}/{self.DRY_CYCLES_TO_FALLBACK} "
            f"falls={self.total_fallbacks} ups={self.total_upgrades}"
        )
