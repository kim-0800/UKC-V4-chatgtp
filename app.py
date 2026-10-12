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
    if value is None:
        return None
    try:
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
        "Tide_TWVD_m", "Tide_CDL_m", "Tide_REF_m",
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
            _first_value(item, ["Tide_TWVD_m", "Tide_CDL_m", "Tide_REF_m", "Tide", "TideHeight", "潮位", "潮高"])
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
            "TWVD": ["Tide_TWVD_m"],
            "CDL": ["Tide_CDL_m"],
            "REF": ["Tide_REF_m"],
        }.items():
            value = _first_value(record, aliases)
            try:
                row[field] = float(value) if value not in (None, "") else None
            except (TypeError, ValueError):
                row[field] = None
        # 若 API 回傳使用非標準欄位，保留通用潮位欄位作備援
        if all(row.get(k) is None for k in ("TWVD", "CDL", "REF")):
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

# 以資料中可用的垂直基準選擇潮位欄位
available_datums = [
    datum for datum in ("TWVD", "CDL", "REF")
    if any(row.get(datum) is not None for row in observations)
]
if available_datums:
    preferred_index = available_datums.index("CDL") if "CDL" in available_datums else 0
    selected_datum = st.selectbox(
        "潮位垂直基準（需與航道水深基準一致）",
        available_datums,
        index=preferred_index,
        help="請依航道水深資料的垂直基準選擇。若基準不同，UKC 數值可能不具可比性。",
    )
    tide_column = selected_datum
else:
    tide_column = "其他潮位欄位"
    if not any(row.get(tide_column) is not None for row in observations):
        st.error("觀測紀錄中沒有可用的潮位數值，無法計算 UKC。")
        st.stop()
    st.warning("API 未提供已識別的 TWVD/CDL/REF 欄位，請先確認潮位基準。")

usable = [row for row in observations if row.get(tide_column) is not None]
if not usable:
    st.error(f"觀測資料中沒有可用的 {tide_column} 潮位數值，無法計算 UKC。")
    st.stop()

latest = usable[-1]
latest_time = latest["觀測時間"]
latest_tide = float(latest[tide_column])
available_depth = channel_depth + latest_tide
ukc = available_depth - draft
ukc_pct = (ukc / draft) * 100 if draft > 0 else 0.0

if ukc_pct >= 15.0:
    current_status = "GREEN"
    status_text = "🟢 初步估算：UKC 裕度達 15% 門檻"
elif ukc_pct >= 10.0:
    current_status = "YELLOW"
    status_text = "🟡 初步估算：UKC 裕度介於 10% 至 15%"
else:
    current_status = "RED"
    status_text = "🔴 初步估算：UKC 裕度低於 10% 門檻"

bg_color_map = {
    "GREEN": "#e8f8f5",
    "YELLOW": "#fef9e7",
    "RED": "#fadbd8",
}
bg_color = bg_color_map[current_status]
st.markdown(
    f""" <style> .stApp {{ background-color: {bg_color}; transition: background-color 0.5s ease; }} </style> """,
    unsafe_allow_html=True,
)

st.subheader("🌊 最新實際觀測潮位")
c1, c2 = st.columns(2)
with c1:
    st.metric("最新觀測潮位", f"{latest_tide:.2f} m")
    st.caption(f"基準：{tide_column}")
with c2:
    st.metric("觀測時間", latest_time.strftime("%Y-%m-%d %H:%M:%S"))
st.caption(
    "此時間是資料來源所提供的觀測時間，不一定等於目前時間；請留意資料更新頻率。"
)

st.subheader("⚓ 依最新觀測值估算 UKC")
st.write(status_text)
m1, m2, m3 = st.columns(3)
m1.metric("可用水深（初步估算）", f"{available_depth:.2f} m")
m2.metric("UKC（初步估算）", f"{ukc:.2f} m")
m3.metric("UKC 裕度", f"{ukc_pct:.1f}%")

st.warning(
    "基準與安全提醒：以上 UKC 只以「航道設計水深 + 最新觀測潮位 − 靜態吃水」估算。"
    "使用前必須確認航道水深與所選潮位的垂直基準一致，並核對航道官方水深、"
    "潮位資料品質及船舶/港口規定。本頁狀態顏色不代表正式航行許可。"
)

# --- 5. 僅呈現 API 實際提供的觀測紀錄，不生成未來值 ---
st.subheader("📈 已取得的實際觀測潮位資料")
plot_rows = []
for row in usable:
    tide = float(row[tide_column])
    plot_rows.append(
        {
            "觀測時間": row["觀測時間"],
            "觀測潮位 (m)": tide,
            "可用水深初步估算 (m)": channel_depth + tide,
        }
    )

df_observations = pd.DataFrame(plot_rows).set_index("觀測時間")
st.line_chart(df_observations[["觀測潮位 (m)", "可用水深初步估算 (m)"]])

st.subheader("📊 實際觀測資料明細")
display_rows = []
for row in usable:
    item = {
        "觀測時間": row["觀測時間"].strftime("%Y-%m-%d %H:%M:%S"),
        f"潮位 {tide_column} (m)": row[tide_column],
        "可用水深初步估算 (m)": round(channel_depth + float(row[tide_column]), 2),
        "UKC 初步估算 (m)": round(channel_depth + float(row[tide_column]) - draft, 2),
    }
    if row.get("備註"):
        item["備註"] = row["備註"]
    display_rows.append(item)
st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)

st.info(
    f"目前 API 回傳並成功辨識 {len(usable)} 筆觀測紀錄。"
    "本系統不會將這些觀測值外推成未來 24 小時預報，也不會提供未來潮窗推算。"
)