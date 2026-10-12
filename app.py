from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import pytz
import requests
import ssl
from requests.adapters import HTTPAdapter
from urllib3.poolmanager import PoolManager
import streamlit as st
from streamlit_js_eval import get_geolocation


class CompatibleTLSAdapter(HTTPAdapter):
    """放寬 OpenSSL 的 X.509 strict 檢查，但仍保留憑證驗證與主機名稱驗證。"""
    def __init__(self, *args, **kwargs):
        self.ssl_context = ssl.create_default_context()
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.ssl_context.verify_flags &= ~strict_flag
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self.ssl_context
        self.poolmanager = PoolManager(
            num_pools=connections,
            maxsize=maxsize,
            block=block,
            **pool_kwargs,
        )


def make_tide_session():
    session = requests.Session()
    session.mount("https://", CompatibleTLSAdapter())
    return session

# 頁面基本設定
st.set_page_config(
    page_title="港口過灘與 UKC 評估系統",
    page_icon="🚢",
    layout="centered",
)

tw_tz = pytz.timezone("Asia/Taipei")
now = datetime.now(tw_tz)

st.title("🚢 港口過灘與 UKC 評估系統")
st.caption(f"📅 當前時間：{now.strftime('%Y-%m-%d %H:%M:%S')} (台灣時間)")
st.info(
    "本版本使用港灣環境資訊網的實際潮位觀測資料。觀測資料不是未來預報，"
    "因此本頁不產生未來 24 小時潮位曲線，也不推算未來潮窗。"
)

# --- 1. 僅保留兩個港口 ---
PORTS = {
    "高雄港第二航道": {
        "depth": 17.0,
        "port_code": "KH",
        "lat": 22.56,
        "lon": 120.30,
    },
    "基隆港": {
        "depth": 15.5,
        "port_code": "KL",
        "lat": 25.15,
        "lon": 121.75,
    },
}

# --- 2. GPS 定位與港口選擇 ---
st.subheader("📍 港口與航道選擇")
geo_data = get_geolocation()
auto_detected_port = "高雄港第二航道"

if geo_data and "coords" in geo_data:
    user_lat = geo_data["coords"].get("latitude")
    user_lon = geo_data["coords"].get("longitude")
    if user_lat is not None and user_lon is not None:
        auto_detected_port = min(
            PORTS,
            key=lambda name: (
                (user_lat - PORTS[name]["lat"]) ** 2
                + (user_lon - PORTS[name]["lon"]) ** 2
            ),
        )
        st.success(
            f"已取得 GPS 座標 ({user_lat:.4f}, {user_lon:.4f})，"
            f"依距離估算最近港口：**{auto_detected_port}**"
        )
else:
    st.info("💡 可允許瀏覽器取得定位權限；也可以直接從下方選擇港口。")

port_options = list(PORTS.keys())
selected_port = st.selectbox(
    "請選擇目標港口／航道：",
    port_options,
    index=port_options.index(auto_detected_port),
)
port_info = PORTS[selected_port]
channel_depth = float(port_info["depth"])
port_code = port_info["port_code"]

# --- 3. 船舶參數：停用 Squat，只保留靜態吃水 ---
st.subheader("🚢 船舶參數")
draft = st.number_input(
    "靜態吃水 Static Draft (m)",
    min_value=0.1,
    max_value=30.0,
    value=16.0,
    step=0.1,
)
st.write(f"**航道設計水深：** `{channel_depth:.2f} m`")
st.caption("Squat 動態下沉量已停用；本版 UKC 僅以靜態吃水進行初步估算。")

# --- 4. 港灣環境資訊網：實際觀測潮位 ---
TIDE_API_URL = "https://isohe.ihmt.gov.tw/opendata/Tide"


def _first_value(record: Dict[str, Any], candidates: List[str]) -> Any:
    """從可能的欄位名稱中取第一個有值的欄位。"""
    for key in candidates:
        if key in record and record[key] not in (None, ""):
            return record[key]
    lower_map = {str(k).lower(): v for k, v in record.items()}
    for key in candidates:
        value = lower_map.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _parse_datetime(value: Any) -> Optional[datetime]:
    """API 的 DateTime 是 YYYYMMDDHHMMSS，例如 20261012194200。"""
    if value is None:
        return None
    try:
        text = str(value).strip()
        # 先按 API 明確定義的 14 位日期時間解析，避免被當成 Unix 奈秒而變成 1970 年。
        if text.endswith(".0"):
            text = text[:-2]
        if len(text) == 14 and text.isdigit():
            dt = datetime.strptime(text, "%Y%m%d%H%M%S")
            return tw_tz.localize(dt)
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return None
        dt = dt.to_pydatetime()
        if dt.tzinfo is None:
            return tw_tz.localize(dt)
        return dt.astimezone(tw_tz)
    except Exception:
        return None


def _find_records(payload: Any) -> List[Dict[str, Any]]:
    """在 API 回傳的常見 JSON 包裝結構中尋找潮位紀錄。"""
    found: List[Dict[str, Any]] = []
    time_keys = ["DateTime", "datetime", "ObservationTime", "ObsTime", "time", "Time"]
    tide_keys = [
        "Tide_TWD_m", "Tide_TWVD_m", "Tide_CDL_m", "Tide_REF_m",
        "Tide", "TideHeight", "tide_height", "潮位", "潮高",
    ]

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if (
                _first_value(node, time_keys) is not None
                and _first_value(node, tide_keys) is not None
            ):
                found.append(node)
            else:
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    # 去除同一筆紀錄可能因巢狀結構造成的重複
    unique: Dict[str, Dict[str, Any]] = {}
    for item in found:
        key = str(_first_value(item, time_keys)) + "|" + str(
            _first_value(item, ["Tide_TWD_m", "Tide_TWVD_m", "Tide_CDL_m", "Tide_REF_m", "Tide", "TideHeight", "潮位", "潮高"])
        )
        unique[key] = item
    return list(unique.values())


def _normalise_records(payload: Any) -> List[Dict[str, Any]]:
    records = _find_records(payload)
    normalised = []
    for record in records:
        dt = _parse_datetime(
            _first_value(
                record,
                ["DateTime", "datetime", "ObservationTime", "ObsTime", "time", "Time"],
            )
        )
        if dt is None:
            continue
        row: Dict[str, Any] = {"觀測時間": dt}
        for field, aliases in {
            "TWD": ["Tide_TWD_m"],
            "CDL": ["Tide_CDL_m"],
            "REF": ["Tide_REF_m"],
        }.items():
            value = _first_value(record, aliases)
            try:
                numeric = float(value) if value not in (None, "") else None
                # API 用 -999 表示無效/缺測值，不可當成真實潮位。
                row[field] = None if numeric is None or numeric == -999 else numeric
            except (TypeError, ValueError):
                row[field] = None
        # 若 API 回傳使用非標準欄位，保留通用潮位欄位作備援
        if all(row.get(k) is None for k in ("TWD", "CDL", "REF")):
            value = _first_value(
                record, ["Tide", "TideHeight", "tide_height", "潮位", "潮高"]
            )
            try:
                row["其他潮位欄位"] = float(value)
            except (TypeError, ValueError):
                pass
        note = _first_value(record, ["Note", "note", "Remark", "備註"])
        if note not in (None, ""):
            row["備註"] = str(note)
        normalised.append(row)
    normalised.sort(key=lambda x: x["觀測時間"])
    return normalised


@st.cache_data(ttl=300, show_spinner=False)
def fetch_observed_tide(port: str) -> Dict[str, Any]:
    """取得港灣環境資訊網觀測潮位；不建立或補造任何預報值。"""
    try:
        session = make_tide_session()
        response = session.get(
            TIDE_API_URL,
            params={"format": "JSON", "port": port},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        rows = _normalise_records(payload)
        return {"ok": True, "rows": rows, "error": None}
    except requests.RequestException as exc:
        return {"ok": False, "rows": [], "error": f"HTTP 連線錯誤：{exc}"}
    except ValueError as exc:
        return {
            "ok": False,
            "rows": [],
            "error": f"API 回傳內容不是可解析的 JSON：{exc}",
        }
    except Exception as exc:
        return {"ok": False, "rows": [], "error": f"資料解析錯誤：{exc}"}


with st.spinner("正在取得港灣環境資訊網觀測潮位…"):
    tide_result = fetch_observed_tide(port_code)

st.markdown(
    f"**資料來源：** [港灣環境資訊網](https://isohe.ihmt.gov.tw/Frontend/Report.aspx) \n"
    f"**資料類型：** 實際觀測潮位（港口代碼：`{port_code}`）"
)

if not tide_result["ok"]:
    st.error("潮位資料取得失敗，無法計算本次 UKC。")
    st.code(tide_result["error"])
    st.warning(
        "目前不會使用模擬公式代替觀測資料。請稍後重試，或檢查網站/API 是否可連線。"
    )
    st.stop()

observations = tide_result["rows"]
if not observations:
    st.error(
        "API 已回應，但程式未能從回傳內容辨識潮位紀錄，因此不顯示假資料，也不計算 UKC。"
    )
    st.caption(
        "若網站端點的 JSON 欄位格式有變，需要依實際回傳內容調整資料解析欄位。"
    )
    st.stop()

# API 的三種欄位是不同高程/潮位基準，不可任意互換。
# 這裡以 REF 作為觀測值展示，TWD/CDL 同時列出供核對；在確認航道水深基準前暫停 UKC 數值計算。
usable = [row for row in observations if row.get("REF") is not None]
if not usable:
    st.error("目前沒有有效的 Tide_REF_m 觀測值（API 的 -999 代表缺測），請稍後重試。")
    st.stop()

latest = usable[-1]
latest_time = latest["觀測時間"]
latest_ref = float(latest["REF"])

st.subheader("🌊 最新實際觀測潮位")
c1, c2 = st.columns(2)
with c1:
    st.metric("相對潮位 Tide_REF_m", f"{latest_ref:.3f} m")
with c2:
    st.metric("觀測時間", latest_time.strftime("%Y-%m-%d %H:%M:%S"))
st.caption(
    "時間已按 API 的 YYYYMMDDHHMMSS 格式解析。Tide_REF_m 僅先用於觀測值展示；"
    "在確認航道設計水深與潮位基準一致前，不把它加到設計水深中。"
)

st.subheader("🧭 其他潮位欄位（分開呈現，不互相替代）")
latest_fields = []
for label, key, meaning in [
    ("Tide_TWD_m", "TWD", "中潮系統潮位"),
    ("Tide_CDL_m", "CDL", "築港高程系統潮位高程"),
    ("Tide_REF_m", "REF", "相對潮位"),
]:
    value = latest.get(key)
    latest_fields.append({
        "API 欄位": label,
        "說明": meaning,
        "最新值 (m)": round(float(value), 3) if value is not None else "缺測",
    })
st.dataframe(pd.DataFrame(latest_fields), use_container_width=True, hide_index=True)

st.subheader("⚓ UKC 計算狀態")
st.warning(
    "目前暫停顯示 UKC、可用水深及安全燈號：尚未確認高雄港第二航道與基隆港的"
    "設計水深所採用的垂直基準，是否與 API 的 Tide_TWD_m、Tide_CDL_m 或 Tide_REF_m 一致。"
    "若直接把不同基準的數值相加，可能產生錯誤且具誤導性的 UKC。"
)
st.write(f"**航道設計水深（待核對基準）：** {channel_depth:.2f} m")
st.write(f"**船舶靜態吃水：** {draft:.2f} m")
st.caption("Squat 已停用。確認兩者垂直基準一致後，再恢復 UKC 計算。")

st.subheader("📈 實際觀測相對潮位紀錄")
plot_rows = [
    {"觀測時間": row["觀測時間"], "相對潮位 Tide_REF_m (m)": float(row["REF"])}
    for row in usable
]
df_observations = pd.DataFrame(plot_rows).set_index("觀測時間")
st.line_chart(df_observations)

st.subheader("📊 實際觀測資料明細")
display_rows = []
for row in observations:
    item = {
        "觀測時間": row["觀測時間"].strftime("%Y-%m-%d %H:%M:%S"),
        "Tide_TWD_m 中潮系統 (m)": row.get("TWD"),
        "Tide_CDL_m 築港高程 (m)": row.get("CDL"),
        "Tide_REF_m 相對潮位 (m)": row.get("REF"),
    }
    if row.get("備註"):
        item["備註"] = row["備註"]
    display_rows.append(item)
st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

st.info(
    f"API 回傳並解析 {len(observations)} 筆紀錄，其中 {len(usable)} 筆有有效的 Tide_REF_m。"
    "本系統只顯示實際觀測值，不將觀測值外推成未來 24 小時預報，也不推算未來潮窗。"
)
