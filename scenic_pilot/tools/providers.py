from __future__ import annotations

import math
from datetime import UTC, date as date_type, datetime
from typing import Any

import httpx

from scenic_pilot.tools.contracts import ToolProviderError


OPEN_METEO_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AMAP_GEOCODING_URL = "https://restapi.amap.com/v3/geocode/geo"
AMAP_DRIVING_URL = "https://restapi.amap.com/v5/direction/driving"


def _json_get(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, object],
    timeout_seconds: float,
) -> dict[str, Any]:
    try:
        response = client.get(url, params=params, timeout=timeout_seconds)
    except httpx.TimeoutException as error:
        raise ToolProviderError("timeout", "外部服务请求超时。") from error
    except httpx.RequestError as error:
        raise ToolProviderError("network_error", "外部服务网络请求失败。") from error
    if response.status_code >= 500:
        raise ToolProviderError(
            "provider_5xx", f"外部服务返回 {response.status_code}。"
        )
    if response.status_code >= 400:
        raise ToolProviderError(
            "provider_4xx",
            f"外部服务拒绝请求（{response.status_code}）。",
            retryable=False,
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise ToolProviderError(
            "invalid_json", "外部服务返回了无效 JSON。"
        ) from error
    if not isinstance(payload, dict):
        raise ToolProviderError(
            "invalid_payload", "外部服务返回结构不正确。", retryable=False
        )
    return payload


class OpenMeteoWeatherProvider:
    provider_id = "open-meteo"
    source_url = "https://open-meteo.com/en/docs"
    _zhuhai_aliases = {
        "珠海",
        "珠海市",
        "珠海长隆",
        "珠海长隆度假区",
        "珠海横琴",
        "长隆海洋王国",
        "海洋王国",
        "长隆飞船乐园",
        "飞船乐园",
    }
    _weather_codes = {
        0: "晴",
        1: "大部晴朗",
        2: "局部多云",
        3: "阴",
        45: "雾",
        48: "雾凇",
        51: "小毛毛雨",
        53: "毛毛雨",
        55: "强毛毛雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        80: "阵雨",
        81: "中等阵雨",
        82: "强阵雨",
        95: "雷暴",
        96: "雷暴伴小冰雹",
        99: "雷暴伴强冰雹",
    }

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        today=lambda: date_type.today(),
        now=lambda: datetime.now(UTC),
    ) -> None:
        self.client = client or httpx.Client(
            headers={"User-Agent": "ScenicPilot/0.1 weather-tool"}
        )
        self.today = today
        self.now = now

    def fetch(
        self, *, location: str, date: str | None, timeout_seconds: float
    ) -> dict[str, object]:
        target_date = date or self.today().isoformat()
        try:
            date_type.fromisoformat(target_date)
        except ValueError as error:
            raise ToolProviderError(
                "invalid_date", "天气日期必须使用 YYYY-MM-DD。", retryable=False
            ) from error

        normalized_location = location.strip()
        expects_zhuhai = normalized_location in self._zhuhai_aliases
        # Open-Meteo's Chinese query for “珠海” currently ranks a Shandong
        # namesake first; the Latin city name returns the Guangdong prefecture.
        search_name = "Zhuhai" if expects_zhuhai else normalized_location
        geocoding = _json_get(
            self.client,
            OPEN_METEO_GEOCODING_URL,
            params={
                "name": search_name,
                "count": 10 if expects_zhuhai else 1,
                "language": "zh",
                "format": "json",
                "countryCode": "CN",
            },
            timeout_seconds=timeout_seconds,
        )
        locations = geocoding.get("results") or []
        if not isinstance(locations, list) or not locations:
            raise ToolProviderError(
                "location_not_found", f"无法解析天气地点：{location}", retryable=False
            )
        resolved = self._select_location(locations, expects_zhuhai=expects_zhuhai)
        if not isinstance(resolved, dict):
            raise ToolProviderError(
                "invalid_geocoding_payload", "天气地理编码结果无效。", retryable=False
            )
        latitude = resolved.get("latitude")
        longitude = resolved.get("longitude")
        if latitude is None or longitude is None:
            raise ToolProviderError(
                "missing_coordinates", "天气地理编码缺少经纬度。", retryable=False
            )

        forecast = _json_get(
            self.client,
            OPEN_METEO_FORECAST_URL,
            params={
                "latitude": latitude,
                "longitude": longitude,
                "daily": (
                    "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max"
                ),
                "timezone": "Asia/Shanghai",
                "forecast_days": 16,
            },
            timeout_seconds=timeout_seconds,
        )
        daily = forecast.get("daily") or {}
        if not isinstance(daily, dict):
            raise ToolProviderError(
                "invalid_forecast_payload", "天气预报结构无效。", retryable=False
            )
        dates = daily.get("time") or []
        if target_date not in dates:
            raise ToolProviderError(
                "forecast_date_unavailable",
                "目标日期不在当前天气预报窗口内。",
                retryable=False,
            )
        index = dates.index(target_date)

        def value(name: str) -> object:
            values = daily.get(name) or []
            return values[index] if index < len(values) else None

        raw_code = value("weather_code")
        weather_code = int(raw_code) if raw_code is not None else None
        return {
            "status": "ok",
            "provider": self.provider_id,
            "source_url": self.source_url,
            "observed_at": self.now().isoformat(),
            "location": location,
            "resolved_location": resolved.get("name", search_name),
            "latitude": latitude,
            "longitude": longitude,
            "date": target_date,
            "weather_code": weather_code,
            "weather": self._weather_codes.get(weather_code, "未知"),
            "temperature_max_c": value("temperature_2m_max"),
            "temperature_min_c": value("temperature_2m_min"),
            "precipitation_probability_max": value(
                "precipitation_probability_max"
            ),
            "message": "天气预报会持续更新，请临近出发再次核验。",
        }

    @staticmethod
    def _select_location(
        locations: list[object], *, expects_zhuhai: bool
    ) -> dict[str, object]:
        candidates = [item for item in locations if isinstance(item, dict)]
        if expects_zhuhai:
            candidates = [
                item
                for item in candidates
                if str(item.get("admin1", "")).casefold() in {"广东", "guangdong"}
                and str(item.get("name", "")).casefold()
                in {"珠海", "珠海市", "zhuhai"}
            ]
            if not candidates:
                raise ToolProviderError(
                    "location_mismatch",
                    "天气地点候选未通过广东珠海行政区校验。",
                    retryable=False,
                )
        if not candidates:
            raise ToolProviderError(
                "invalid_geocoding_payload", "天气地理编码结果无效。", retryable=False
            )
        return candidates[0]


class AmapWeatherProvider:
    """Domestic weather source via AMap Web Service; stable from within China.

    Reuses the same AMAP_API_KEY as the distance provider. AMap's forecast
    endpoint returns up to 4 days of daily casts and responds in well under a
    second from domestic networks, avoiding the跨境 latency jitter that makes
    Open-Meteo unreliable for this deployment.
    """

    provider_id = "amap-weather"
    source_url = "https://lbs.amap.com/api/webservice/guide/api/weatherinfo"
    _AMAP_WEATHER_URL = "https://restapi.amap.com/v3/weather/weatherInfo"
    _zhuhai_adcode = "440400"  # 珠海市

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        *,
        today=lambda: date_type.today(),
        now=lambda: datetime.now(UTC),
    ) -> None:
        if not api_key.strip():
            raise ValueError("AMap weather API key cannot be empty")
        self.api_key = api_key
        self.client = client or httpx.Client(
            headers={"User-Agent": "ScenicPilot/0.1 weather-tool"}
        )
        self.today = today
        self.now = now

    def fetch(
        self, *, location: str, date: str | None, timeout_seconds: float
    ) -> dict[str, object]:
        target_date = date or self.today().isoformat()
        try:
            date_type.fromisoformat(target_date)
        except ValueError as error:
            raise ToolProviderError(
                "invalid_date", "天气日期必须使用 YYYY-MM-DD。", retryable=False
            ) from error

        # 高德天气只到市级；本项目固定珠海单场景，任何地点都映射到珠海市。
        adcode = self._zhuhai_adcode
        payload = _json_get(
            self.client,
            self._AMAP_WEATHER_URL,
            params={
                "key": self.api_key,
                "city": adcode,
                "extensions": "all",
                "output": "JSON",
            },
            timeout_seconds=timeout_seconds,
        )
        if str(payload.get("status")) != "1":
            info = payload.get("info", "unknown")
            raise ToolProviderError(
                "amap_weather_error",
                f"高德天气查询失败：{info}。",
                retryable=str(payload.get("infocode")) not in {"10001", "10002"},
            )
        forecasts = payload.get("forecasts") or []
        if (
            not isinstance(forecasts, list)
            or not forecasts
            or not isinstance(forecasts[0], dict)
        ):
            raise ToolProviderError(
                "invalid_weather_payload", "高德天气返回结构无效。", retryable=False
            )
        forecast = forecasts[0]
        casts = forecast.get("casts") or []
        if not isinstance(casts, list) or not casts:
            raise ToolProviderError(
                "invalid_weather_payload", "高德天气预报数据缺失。", retryable=False
            )
        cast = next(
            (
                item
                for item in casts
                if isinstance(item, dict) and item.get("date") == target_date
            ),
            None,
        )
        if cast is None:
            raise ToolProviderError(
                "forecast_date_unavailable",
                "目标日期不在当前天气预报窗口内。",
                retryable=False,
            )

        def to_float(value: object) -> float | None:
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "status": "ok",
            "provider": self.provider_id,
            "source_url": self.source_url,
            "observed_at": self.now().isoformat(),
            "location": location,
            "resolved_location": forecast.get("city", location),
            "latitude": None,
            "longitude": None,
            "date": target_date,
            "weather_code": None,
            "weather": str(cast.get("dayweather") or "未知"),
            "temperature_max_c": to_float(cast.get("daytemp")),
            "temperature_min_c": to_float(cast.get("nighttemp")),
            "precipitation_probability_max": None,
            "message": "天气预报会持续更新，请临近出发再次核验。",
        }


class AmapDistanceProvider:
    provider_id = "amap-web-service"
    source_url = "https://lbs.amap.com/api/webservice/guide/api/newroute"

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        *,
        city: str = "珠海",
        now=lambda: datetime.now(UTC),
    ) -> None:
        if not api_key.strip():
            raise ValueError("AMap Web Service API key cannot be empty")
        self.api_key = api_key
        self.client = client or httpx.Client(
            headers={"User-Agent": "ScenicPilot/0.1 distance-tool"}
        )
        self.city = city
        self.now = now

    def fetch(
        self, *, origin: str, destination: str, timeout_seconds: float
    ) -> dict[str, object]:
        if origin.strip() == destination.strip():
            return {
                "status": "ok",
                "provider": self.provider_id,
                "source_url": self.source_url,
                "observed_at": self.now().isoformat(),
                "origin": origin,
                "destination": destination,
                "distance_meters": 0,
                "duration_minutes": 0,
                "mode": "driving",
                "message": "起点与终点相同。",
            }
        origin_coordinates = self._geocode(origin, timeout_seconds)
        destination_coordinates = self._geocode(destination, timeout_seconds)
        payload = _json_get(
            self.client,
            AMAP_DRIVING_URL,
            params={
                "key": self.api_key,
                "origin": origin_coordinates,
                "destination": destination_coordinates,
                "show_fields": "cost",
            },
            timeout_seconds=timeout_seconds,
        )
        if str(payload.get("status")) != "1":
            raise ToolProviderError(
                "amap_route_error",
                f"高德路径规划失败：{payload.get('info', 'unknown')}。",
                retryable=str(payload.get("infocode")) not in {"10001", "10002"},
            )
        route = payload.get("route") or {}
        paths = route.get("paths") if isinstance(route, dict) else None
        if not isinstance(paths, list) or not paths or not isinstance(paths[0], dict):
            raise ToolProviderError(
                "route_not_found", "未找到可用驾车路线。", retryable=False
            )
        path = paths[0]
        cost = path.get("cost") if isinstance(path.get("cost"), dict) else {}
        duration_seconds = cost.get("duration") or path.get("duration")
        return {
            "status": "ok",
            "provider": self.provider_id,
            "source_url": self.source_url,
            "observed_at": self.now().isoformat(),
            "origin": origin,
            "destination": destination,
            "origin_coordinates": origin_coordinates,
            "destination_coordinates": destination_coordinates,
            "distance_meters": int(float(path["distance"])),
            "duration_minutes": (
                math.ceil(float(duration_seconds) / 60)
                if duration_seconds is not None
                else None
            ),
            "taxi_cost_cny": route.get("taxi_cost"),
            "mode": "driving",
            "message": "路线时间会随路况变化，请临近出发再次核验。",
        }

    def _geocode(self, address: str, timeout_seconds: float) -> str:
        payload = _json_get(
            self.client,
            AMAP_GEOCODING_URL,
            params={
                "key": self.api_key,
                "address": address,
                "city": self.city,
                "output": "json",
            },
            timeout_seconds=timeout_seconds,
        )
        if str(payload.get("status")) != "1":
            raise ToolProviderError(
                "amap_geocoding_error",
                f"高德地理编码失败：{payload.get('info', 'unknown')}。",
                retryable=str(payload.get("infocode")) not in {"10001", "10002"},
            )
        geocodes = payload.get("geocodes") or []
        if not isinstance(geocodes, list) or not geocodes:
            raise ToolProviderError(
                "location_not_found", f"无法解析地点：{address}", retryable=False
            )
        location = geocodes[0].get("location") if isinstance(geocodes[0], dict) else None
        if not location:
            raise ToolProviderError(
                "missing_coordinates", f"地点缺少坐标：{address}", retryable=False
            )
        return str(location)
