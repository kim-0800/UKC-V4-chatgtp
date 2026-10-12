import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_js_eval import get_geolocation

st.set_page_config(page_title="乘潮進港 UKC 評估", page_icon="🚢", layout="wide")
TW = ZoneInfo("Asia/Taipei")
OBS_URL = "https://isohe.ihmt.gov.tw/opendata/Tide?format=JSON&port={}"
SIM_URL = "https://isohe.ihmt.gov.tw/Frontend/Simulation/CommercialPortSim.aspx"
REPORT_URL = "https://isohe.ihmt.gov.tw/Frontend/Report.aspx"

# 港口參考座標：用於最近港口推薦，並非航道中心線座標。
PORTS = {
    "基隆港": (25.155, 121.748, "KL"),
    "臺北港": (25.151, 121.365, "TP"),
    "蘇澳港": (24.595, 121.866, "SU"),
    "花蓮港": (23.991, 121.625, "HL"),
    "臺中港": (24.279, 120.507, "TC"),
    "高雄港": (22.615, 120.288, "KH"),
    "安平港": (22.985, 120.155, "AP"),
    "布袋港": (23.378, 120.154, "BD"),
    "麥寮工業港": (23.798, 120.196, None),
    "和平工業港": (24.305, 121.762, None),
}
CHANNELS = {
    "基隆港": ["主航道／請依官方資料確認"],
    "臺北港": ["主航道／請依官方資料確認"],
    "蘇澳港": ["主航道／請依官方資料確認"],
    "花蓮港": ["主航道／請依官方資料確認"],
    "臺中港": ["主航道／請依官方資料確認"],
    "高雄港": ["第一航道", "第二航道", "其他航道／請確認"],
    "安平港": ["主航道／請依官方資料確認"],
    "布袋港": ["主航道／請依官方資料確認"],
    "麥寮工業港": ["請依工業港官方資料確認航道"],
    "和平工業港": ["請依工業港官方資料確認航道"],
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_observed(port_code):
    """官方公開 API 的實際觀測潮位；絕不視為預報。"""
    if not port_code:
        return None, "此港口目前沒有設定已確認的觀測 API 代碼。"
    url = OBS_URL.format(port_code)
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("Datas", []) if isinstance(payload, dict) else []
        parsed = []
        for item in rows:
            try:
                dt = datetime.strptime(str(item.get("DateTime")), "%Y%m%d%H%M%S").replace(tzinfo=TW)
                ref = float(item.get("Tide_REF_m"))
                if ref == -999 or not math.isfinite(ref):
                    continue
                parsed.append({"時間": dt, "相對潮位 (m)": ref})
            except (TypeError, ValueError):
                continue
        if not parsed:
            return None, f"已連到 API，但沒有有效潮位資料。回應欄位：{list(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}"
        return pd.DataFrame(parsed).sort_values("時間"), None
    except Exception as e:
        return None, f"取得觀測潮位失敗：{type(e).__name__}: {e}"

@st.cache_data(ttl=300, show_spinner=False)
def fetch_simulation_page():
    """抓取官方商港模擬頁 HTML，嘗試讀取頁面當前顯示的模擬表格。 若頁面資料由瀏覽器 JavaScript/表單互動載入，會回報無法自動解析，不偽造預報。 """
    try:
        r = requests.get(SIM_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        tables = pd.read_html(r.text)
        return r.text, tables, None
    except Exception as e:
        return None, [], f"無法自動解析官方模擬頁：{type(e).__name__}: {e}"


def haversine(lat1, lon1, lat2, lon2):
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * radius * math.asin(math.sqrt(a))


def nearest_port(lat, lon):
    return min(PORTS, key=lambda n: haversine(lat, lon, PORTS[n][0], PORTS[n][1]))

st.title("🚢 乘潮進港 UKC 評估系統")
st.caption("GPS 最近港口推薦｜手動選港｜船舶吃水輸入｜官方潮位資料連線")
st.warning("Squat 暫不計算：目前以輸入的靜態吃水近似動態吃水，結果不是完整的動態通航評估。")

# ---- Location / user inputs ----
with st.sidebar:
    st.header("1｜定位與港口選擇")
    if "geo_result" not in st.session_state:
        st.session_state.geo_result = None
    if st.button("📍 自動定位並推薦最近港口", use_container_width=True):
        try:
            st.session_state.geo_result = get_geolocation()
        except Exception as e:
            st.session_state.geo_result = {"error": str(e)}
    geo = st.session_state.geo_result
    suggested = None
    if isinstance(geo, dict):
        coords = geo.get("coords", geo)
        lat, lon = coords.get("latitude"), coords.get("longitude")
        if lat is not None and lon is not None:
            suggested = nearest_port(float(lat), float(lon))
            st.success(f"定位成功：推薦 {suggested}")
        elif geo.get("error"):
            st.error("定位失敗，請確認瀏覽器已允許位置權限；也可手動選港。")
        else:
            st.info("等待瀏覽器定位結果；若沒有跳出權限提示，請檢查網站的位置權限。")
    names = list(PORTS.keys())
    default_idx = names.index(suggested) if suggested in PORTS else 0
    port = st.selectbox("手動選擇目標港口", names, index=default_idx, key="target_port")
    channel = st.selectbox("目標航道／區段", CHANNELS[port], key="target_channel")

    st.header("2｜船舶吃水")
    draft = st.number_input("船舶靜態吃水（公尺）", min_value=0.1, max_value=40.0, value=10.0, step=0.1, format="%.2f")
    st.caption("依目前設定：動態吃水 ≈ 靜態吃水；尚未計入 Squat、波浪運動、縱傾等修正。")

st.subheader("3｜官方潮汐資料")
st.write(f"**目標港口：** {port}　｜　**航道：** {channel}　｜　**輸入吃水：** {draft:.2f} m")

# Actual observation endpoint: verifies live data retrieval, not forecast.
code = PORTS[port][2]
with st.expander("A. 取得官方最新觀測潮位（連線測試；不是未來預報）", expanded=True):
    if st.button("🔄 抓取最新觀測潮位", key="fetch_obs"):
        fetch_observed.clear()
        st.session_state.do_obs_fetch = True
    if st.session_state.get("do_obs_fetch", False):
        with st.spinner("正在向港灣環境資訊網請求資料…"):
            obs_df, obs_err = fetch_observed(code)
        if obs_err:
            st.error(obs_err)
        else:
            latest = obs_df.iloc[-1]
            x, y = st.columns(2)
            x.metric("最新觀測相對潮位", f"{latest['相對潮位 (m)']:.3f} m")
            y.metric("觀測時間（臺灣時間）", latest["時間"].strftime("%Y-%m-%d %H:%M:%S"))
            st.dataframe(obs_df.tail(20).sort_values("時間", ascending=False), use_container_width=True, hide_index=True)
            st.caption("來源為 Tide API 的 Tide_REF_m；-999 缺測值已排除。觀測值不能當成未來預報。")

with st.expander("B. 取得官方未來模擬潮位（預報來源）", expanded=True):
    st.write("官方頁面：商港模擬資訊。這是未來潮位候選來源，不是歷史觀測資料。")
    st.link_button("開啟官方商港模擬資訊", SIM_URL)
    if st.button("🔄 嘗試讀取官方模擬頁資料", key="fetch_sim"):
        fetch_simulation_page.clear()
        st.session_state.do_sim_fetch = True
    if st.session_state.get("do_sim_fetch", False):
        with st.spinner("正在讀取官方模擬頁…"):
            html, tables, sim_err = fetch_simulation_page()
        if sim_err:
            st.error(sim_err)
            st.info("你仍可用上方官方連結查看模擬時間與潮位。若頁面透過互動式下拉選單載入資料，需再依網站實際請求格式串接。")
        else:
            st.success(f"成功取得官方模擬頁 HTML；解析到 {len(tables)} 個表格。")
            matched = 0
            for i, table in enumerate(tables):
                as_text = table.astype(str).to_string(index=False)
                if port in as_text or "潮位" in as_text or "潮汐" in as_text:
                    st.markdown(f"**官方頁面表格 {i+1}**")
                    st.dataframe(table, use_container_width=True, hide_index=True)
                    matched += 1
            if matched == 0:
                st.warning("HTML 可取得，但沒有解析到港口潮位表格。可能需要分析網站的互動式請求；不能把此結果當作已取得預報。")

st.subheader("4｜UKC 裕度與 5｜候選進港時段")
st.info("只有取得帶有未來時間戳的官方預測潮位，並確認該航道設計水深及潮位基準相容後，才能計算 UKC 與三色燈號。此版不會拿觀測潮位代替預報。")
st.markdown("**計算公式**")
st.latex(r"\mathrm{UKC\ Margin}(\%)=\frac{\mathrm{可用總水深}-\mathrm{動態吃水}}{\mathrm{動態吃水}}\times100\%")
cols = st.columns(3)
cols[0].success("🟢 GREEN\n\nUKC 裕度 ≥ 15%")
cols[1].warning("🟡 YELLOW\n\n10% ≤ UKC 裕度 < 15%")
cols[2].error("🔴 RED\n\nUKC 裕度 < 10%")
st.caption("可用總水深 = 官方航道基準水深 + 同一高程基準下的未來預測潮位。設計水深尚未自動配對，故本版不產生通航判定。")
st.caption("此原型供研究與決策輔助，不是通航許可。港灣環境資訊網亦提醒觀測/預測資料尚未經嚴密品管。")