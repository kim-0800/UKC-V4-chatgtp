import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_js_eval import get_geolocation

st.set_page_config(page_title="乘潮進港 UKC 評估系統", page_icon="🚢", layout="wide")
TW = ZoneInfo("Asia/Taipei")
SIMULATION_PAGE = "https://isohe.ihmt.gov.tw/Frontend/Simulation/CommercialPortSim.aspx"
REPORT_PAGE = "https://isohe.ihmt.gov.tw/Frontend/Report.aspx"

# 港口座標只用於 GPS 最近港口推薦，不代表航道位置。
PORTS = {
    "基隆港": (25.155, 121.748),
    "臺北港": (25.151, 121.365),
    "蘇澳港": (24.595, 121.866),
    "花蓮港": (23.991, 121.625),
    "臺中港": (24.279, 120.507),
    "高雄港": (22.615, 120.288),
    "安平港": (22.985, 120.155),
    "布袋港": (23.378, 120.154),
    "麥寮工業港": (23.798, 120.196),
    "和平工業港": (24.305, 121.762),
}
CHANNELS = {
    "基隆港": ["請確認目標航道/區段"],
    "臺北港": ["請確認目標航道/區段"],
    "蘇澳港": ["請確認目標航道/區段"],
    "花蓮港": ["請確認目標航道/區段"],
    "臺中港": ["請確認目標航道/區段"],
    "高雄港": ["第一港口（需核實航道水深）", "第二港口（需核實航道水深）"],
    "安平港": ["請確認目標航道/區段"],
    "布袋港": ["請確認目標航道/區段"],
    "麥寮工業港": ["請確認目標航道/區段"],
    "和平工業港": ["請確認目標航道/區段"],
}

def distance_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2*r*math.asin(math.sqrt(a))

def nearest_port(lat, lon):
    return min(PORTS, key=lambda n: distance_km(lat, lon, *PORTS[n]))

def fetch_observed_tide(port):
    # 此端點是觀測潮位，只用來檢查連線，絕不當作未來預報。
    codes = {"基隆港":"KL", "高雄港":"KH", "臺中港":"TC",
             "臺北港":"TP", "花蓮港":"HL", "蘇澳港":"SU",
             "安平港":"AP", "布袋港":"BD"}
    code = codes.get(port)
    if not code:
        return None, "此港口尚無已驗證的觀測 API 對應碼。"
    url = f"https://isohe.ihmt.gov.tw/opendata/Tide?format=JSON&port={code}"
    try:
        response = requests.get(url, timeout=12)
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("Datas", []) if isinstance(payload, dict) else []
        result = []
        for row in rows:
            try:
                dt = datetime.strptime(str(row.get("DateTime")), "%Y%m%d%H%M%S").replace(tzinfo=TW)
                value = float(row.get("Tide_REF_m"))
                if value == -999 or not math.isfinite(value):
                    continue
                result.append({"時間": dt, "相對潮位 (m)": value})
            except (ValueError, TypeError):
                continue
        if not result:
            return None, "API 有回應，但沒有有效的 Tide_REF_m 觀測值。"
        return pd.DataFrame(result).sort_values("時間"), None
    except Exception as exc:
        return None, f"觀測資料取得失敗：{exc}"

st.title("🚢 乘潮進港 UKC 評估系統")
st.caption("從現在開始評估未來可通行時段｜Squat 暫不計算")
st.info(
    "本版不會把歷史/即時觀測潮位延伸成未來預報。港灣環境資訊網有商港模擬潮位頁面，"
    "但預報資料串接及各航道官方水深對應尚須驗證；完成前不會產生虛構的 UKC 或通航燈號。"
)

if "geo" not in st.session_state:
    st.session_state.geo = None

with st.sidebar:
    st.header("1｜港口與船舶設定")
    if st.button("📍 使用 GPS 推薦最近港口"):
        try:
            st.session_state.geo = get_geolocation()
        except Exception:
            st.session_state.geo = None
    suggested = None
    geo = st.session_state.geo
    if isinstance(geo, dict):
        coords = geo.get("coords", geo)
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is not None and lon is not None:
            suggested = nearest_port(float(lat), float(lon))
            st.success(f"最近港口推薦：{suggested}")
    if not suggested:
        st.caption("允許瀏覽器定位後可推薦最近港口，也可以直接手動選擇。")
    port_names = list(PORTS)
    port = st.selectbox("目標港口", port_names,
                        index=port_names.index(suggested) if suggested else 0)
    channel = st.selectbox("目標航道/區段", CHANNELS[port])
    draft = st.number_input("船舶靜態吃水（m）", min_value=0.1,
                            max_value=40.0, value=10.0, step=0.1)
    st.caption("本版依你的選擇，以靜態吃水近似動態吃水；尚未計入 Squat、波浪運動、縱傾等修正。")
    st.divider()
    st.header("2｜官方航道設計水深")
    st.warning("尚未建立可追溯的「港口＋航道」官方水深資料表，不能安全地自動填入猜測值。")
    depth = st.number_input(
        "輸入已核實的航道基準水深（m）", min_value=0.0, max_value=100.0,
        value=0.0, step=0.1,
        help="請依港務單位/官方航道圖確認水深及其基準面；0 代表尚未提供。"
    )
    st.link_button("官方公開資料頁", REPORT_PAGE)

st.subheader("3｜未來潮位預測")
st.markdown(f"**目標港口：** {port}　　**航道：** {channel}　　**船舶吃水：** {draft:.2f} m")
st.link_button("開啟官方商港模擬潮位頁面", SIMULATION_PAGE)
st.warning(
    "未來潮位預測目前尚未完成可驗證的自動串接。網站不會使用觀測值或正弦公式假造未來潮汐。"
)

with st.expander("查看最新實際觀測潮位（僅供連線檢查，不用於未來 UKC 預報）"):
    obs_df, obs_error = fetch_observed_tide(port)
    if obs_error:
        st.error(obs_error)
    else:
        latest = obs_df.iloc[-1]
        left, right = st.columns(2)
        left.metric("最新相對潮位（觀測）", f"{latest['相對潮位 (m)']:.3f} m")
        right.metric("觀測時間", latest["時間"].strftime("%Y-%m-%d %H:%M:%S"))
        st.dataframe(obs_df.tail(12).sort_values("時間", ascending=False),
                     use_container_width=True, hide_index=True)

st.subheader("4｜逐時 UKC 計算與 5｜候選進港時段")
if depth <= 0:
    st.info("請先輸入已核實的航道官方基準水深。")
else:
    st.info(
        f"已輸入水深 {depth:.2f} m。由於未來潮位預報尚未可靠串接，"
        "系統暫不計算 UKC 百分比、不顯示通航燈號，也不推薦進港時段。"
    )

st.markdown("### UKC 裕度判定門檻")
a, b, c = st.columns(3)
a.success("🟢 GREEN\n\nUKC 裕度 ≥ 15%")
b.warning("🟡 YELLOW\n\n10% ≤ UKC 裕度 < 15%")
c.error("🔴 RED\n\nUKC 裕度 < 10%")
st.markdown("### 計算公式")
st.latex(r"\mathrm{UKC\ Margin}(\%)=\frac{\mathrm{Available\ Depth}-\mathrm{Dynamic\ Draft}}{\mathrm{Dynamic\ Draft}}\times100\%")
st.caption("可用總水深 = 航道基準水深 + 同一高程基準下的未來預測潮位。")
st.caption(
    "此網站是研究用決策輔助原型。港灣環境資訊網提醒觀測及預測資料尚未經嚴密品管；"
    "實際通航仍須遵循港務單位、航港機關、船長及引水人指示。"
)