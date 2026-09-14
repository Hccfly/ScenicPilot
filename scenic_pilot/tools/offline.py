from __future__ import annotations


class OfflineWeatherTool:
    def forecast(self, *, location: str, date: str | None = None) -> dict[str, object]:
        return {
            "status": "unavailable_offline",
            "provider": "offline",
            "location": location,
            "date": date,
            "message": "离线模式未查询实时天气，请在出发前核验。",
        }


class OfflineDistanceTool:
    def estimate(self, *, origin: str, destination: str) -> dict[str, object]:
        return {
            "status": "estimated",
            "provider": "offline-heuristic",
            "origin": origin,
            "destination": destination,
            "distance_meters": 0 if origin == destination else None,
            "duration_minutes": 20 if origin != destination else 0,
            "message": "离线演示估算，不代表实时路况。",
        }


class OfflineBudgetTool:
    def estimate(self, *, party_size: int, duration_minutes: int) -> dict[str, object]:
        meal_blocks = max(1, duration_minutes // 300)
        return {
            "status": "estimated",
            "provider": "local-budget-rule-v1",
            "estimated_cost": float(max(1, party_size) * meal_blocks * 100),
            "currency": "CNY",
            "excludes": ["门票", "住宿", "交通"],
            "message": "仅为餐饮预留的演示估算，不是官方价格。",
        }


class OfflineOpeningHoursTool:
    def check(self, *, venue: str, date: str | None = None) -> dict[str, object]:
        return {
            "status": "requires_live_check",
            "provider": "offline",
            "venue": venue,
            "date": date,
            "is_open": None,
            "message": "开放时间属于易变信息，需要查询官方当日安排。",
        }
