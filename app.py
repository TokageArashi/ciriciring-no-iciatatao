import datetime
import hashlib
import io
import json
import os
import time
import uuid

from google.api_core.exceptions import ResourceExhausted
import google.generativeai as genai
from gtts import gTTS
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from supabase import Client, create_client

# --- 輔助函式：上下雙語排版用（適應暗色/亮色模式的高對比顏色） ---
def dual_text(tao: str, zh: str, is_title: bool = False) -> str:
    """產生上方達悟文（稍大）、下方中文的雙語對照 HTML 結構，使用高對比顏色以支援暗色模式"""
    size_tao = "22px" if is_title else "18px"
    size_zh = "16px" if is_title else "14px"
    return f"""
    <div style="margin-bottom: 8px;">
        <div style="font-size: {size_tao}; font-weight: bold; color: var(--text-color, #F3F4F6); line-height: 1.3;">{tao}</div>
        <div style="font-size: {size_zh}; color: var(--secondary-text-color, #9CA3AF); line-height: 1.2;">{zh}</div>
    </div>
    """

def lower_first_char(text: str) -> str:
    """將字串的第一個字元轉為小寫"""
    if not text:
        return text
    return text[0].lower() + text[1:]


# --- 1. 全域設定與 Supabase 連線 ---
MODEL_NAME = "gemini-3.6-flash"  # Gemini 模型名稱

st.set_page_config(
    page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide"
)

# 嵌入 JavaScript：利用 MutationObserver 自動追蹤並動態關閉所有輸入框的拼字檢查
components.html(
    """
    <script>
        function disableSpellcheck() {
            const inputs = parent.document.querySelectorAll('input, textarea');
            inputs.forEach(input => {
                input.setAttribute('spellcheck', 'false');
                input.setAttribute('autocomplete', 'off');
                input.setAttribute('autocorrect', 'off');
                input.setAttribute('autocapitalize', 'off');
            });
        }

        disableSpellcheck();

        const observer = new MutationObserver((mutations) => {
            disableSpellcheck();
        });

        observer.observe(parent.document.body, {
            childList: true,
            subtree: true
        });
    </script>
    """,
    height=0,
)

st.markdown(
    """
    <style>
    div[role='radiogroup'] label { font-size: 20px !important; font-weight: bold !important; padding: 5px !important; }
    .stButton>button { width: 100%; height: 2.8em; font-size: 18px !important; }
    .stTextInput input { font-size: 18px !important; }
    
    input, textarea {
        spellcheck: false !important;
        -webkit-spellcheck: false !important;
    }
    </style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style='text-align: center; padding-top: 5px; padding-bottom: 15px;'>
        <h1 style='font-size: 42px; font-weight: bold; color: #3B82F6;'>ciriciring no iciatatao</h1>
        <div style='font-size: 24px; color: #9CA3AF; font-weight: 600;'>眾語</div>
    </div>
""",
    unsafe_allow_html=True,
)


# 初始化 Supabase
@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


supabase = init_supabase()


# --- 2. 會員機制 (使用 Supabase users 表) ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()


def add_user(username, password, email, region, age_group, gender):
    try:
        data = {
            "username": username,
            "password": make_hashes(password),
            "email": email,
            "region": region,
            "age_group": age_group,
            "gender": gender,
            "created_at": datetime.datetime.now().isoformat(),
        }
        supabase.from_("users").insert(data).execute()
        return True
    except Exception as e:
        st.error(f"註冊失敗：{e}")
        return False


def login_user(username, password):
    try:
        res = (
            supabase.from_("users")
            .select("*")
            .eq("username", username)
            .eq("password", make_hashes(password))
            .execute()
        )
        return res.data
    except Exception as e:
        st.error(f"登入查詢失敗：{e}")
        return []


# --- 3. Supabase 資料庫讀寫函式 ---
def save_語料_to_supabase(
    user_info,
    bg_info,
    q_orig,
    q_trans,
    q_audio_bytes,
    q_mime,
    r_tao,
    r_zh,
    r_audio_bytes,
    r_mime,
    is_edited=False,
    is_error=False,
):
    try:
        data = {
            "user_id": user_info["username"],
            "user_email": user_info["email"],
            "region": bg_info["region"],
            "age_group": bg_info["age_group"],
            "gender": bg_info["gender"],
            "q_original": q_orig,
            "q_trans": q_trans,
            "q_audio_data": (
                f"\\x{q_audio_bytes.hex()}" if q_audio_bytes else None
            ),
            "q_audio_mime": q_mime,
            "tao_text": r_tao,
            "zh_text": r_zh,
            "r_audio_data": (
                f"\\x{r_audio_bytes.hex()}" if r_audio_bytes else None
            ),
            "r_audio_mime": r_mime,
            "is_edited": 1 if is_edited else 0,
            "error_count": 1 if is_error else 0,
            "timestamp": datetime.datetime.now().isoformat(),
        }
        supabase.from_("feedback").insert(data).execute()
        return True
    except Exception as e:
        st.error(f"寫入 Supabase 失敗：{e}")
        return False


def delete_feedback_item(feedback_id, current_username):
    try:
        res = (
            supabase.from_("feedback")
            .select("user_id")
            .eq("id", feedback_id)
            .execute()
        )
        if not res.data:
            return False, "找不到該筆資料。"

        owner_id = res.data[0]["user_id"]
        if current_username != "admin" and owner_id != current_username:
            return False, "權限不足：您只能刪除自己提供的資料！"

        supabase.from_("community_votes").delete().eq(
            "feedback_id", feedback_id
        ).execute()
        supabase.from_("feedback").delete().eq("id", feedback_id).execute()
        return True, "刪除成功！"
    except Exception as e:
        return False, f"刪除失敗：{e}"


# --- 4. 語音處理輔助函數 ---
def get_bytes_from_input(audio_input):
    if not audio_input:
        return None
    if hasattr(audio_input, "seek"):
        audio_input.seek(0)
    if hasattr(audio_input, "read"):
        return audio_input.read()
    return audio_input


def generate_tts_bytes(text):
    if not text:
        return None
    try:
        tts = gTTS(text=text, lang="tl", slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


# --- 1. 全量快取與關鍵字檢索語料庫 ---
@st.cache_data(ttl=300)
def load_all_raw_corpus():
    """從 Supabase 撈取所有合規語料，並建立統一結構供關鍵字檢索使用"""
    corpus_items = []

    # 1. 撈取官方語料 corpus (包含來源 source)
    try:
        res_corpus = supabase.from_("corpus").select("*").execute()
        for r in res_corpus.data:
            corpus_items.append(
                {
                    "id": f"Corpus 官方語料 ID #{r.get('id')}",
                    "tao": r.get("q_original", ""),
                    "zh": r.get("q_trans", ""),
                    "r_tao": r.get("r_tao", ""),
                    "r_zh": r.get("r_zh", ""),
                    "source": r.get("source", "官方語料庫"),
                    "full_text": f"{r.get('q_original', '')} {r.get('q_trans', '')} {r.get('r_tao', '')} {r.get('r_zh', '')}".lower(),
                }
            )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取 corpus 語料庫提示：{e}")

    # 2. 撈取 15 人驗證以上的社群語料 feedback
    try:
        res_feedback = (
            supabase.from_("feedback")
            .select("*")
            .eq("is_ready_for_ai", 1)
            .execute()
        )
        res_votes = supabase.from_("community_votes").select("feedback_id, vote_result").execute()

        vote_counts = {}
        if res_votes.data:
            for v in res_votes.data:
                fid = v.get("feedback_id")
                result = v.get("vote_result")
                if fid is not None and result == "贊同":
                    vote_counts[fid] = vote_counts.get(fid, 0) + 1

        for r in res_feedback.data:
            f_id = r.get("id")
            if vote_counts.get(f_id, 0) >= 15:
                corpus_items.append(
                    {
                        "id": f"Feedback 15人驗證語料 ID #{f_id}",
                        "tao": r.get("q_original", ""),
                        "zh": r.get("q_trans", ""),
                        "r_tao": r.get("tao_text", ""),
                        "r_zh": r.get("zh_text", ""),
                        "source": f"社群驗證 (提供者: {r.get('user_id', '匿名')})",
                        "full_text": f"{r.get('q_original', '')} {r.get('q_trans', '')} {r.get('tao_text', '')} {r.get('zh_text', '')}".lower(),
                    }
                )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取社群驗證語料提示：{e}")

    return corpus_items


def search_relevant_corpus(user_input, top_k=20):
    """根據使用者輸入進行關鍵字索引比對，回傳最相關的 top_k 筆語料清單"""
    all_corpus = load_all_raw_corpus()
    if not all_corpus:
        return []

    if not user_input:
        matched_items = all_corpus[-top_k:]
    else:
        clean_input = user_input.lower()
        keywords = set(re.findall(r"\w+", clean_input))

        scored_items = []
        for item in all_corpus:
            score = 0
            for kw in keywords:
                if kw in item["full_text"]:
                    score += 1
            if score > 0:
                scored_items.append((score, item))

        scored_items.sort(key=lambda x: x[0], reverse=True)
        matched_items = [item for score, item in scored_items[:top_k]]

        # 若無關鍵字命中，預設回傳最新的 top_k 筆
        if not matched_items:
            matched_items = all_corpus[-top_k:]

    formatted_corpus = []
    for r in matched_items:
        formatted_corpus.append(
            f"[{r['id']}] 出處: {r['source']} | 達悟語: {r['tao']} | 中文: {r['zh']} | 回應達悟語: {r['r_tao']} | 回應中文: {r['r_zh']}"
        )

    return formatted_corpus


# --- 5. AI 處理邏輯 ---
def process_ai_input(text_prompt=None, audio_file=None):
    api_key = st.secrets.get("GOOGLE_API_KEY") or os.environ.get(
        "GOOGLE_API_KEY"
    )
    if not api_key:
        st.error("❌ 找不到 GOOGLE_API_KEY，請檢查 Secrets 設定")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    # 執行關鍵字索引，抓取最相關的 20 筆語料
    query_str = text_prompt if text_prompt else ""
    legal_corpus = search_relevant_corpus(query_str, top_k=20)

    corpus_context = (
        "\n".join(legal_corpus)
        if legal_corpus
        else "【目前尚無合規的參考語料】"
    )

    system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。

【唯一合規參考語料庫（已透過關鍵字索引篩選最相關內容）】:
{corpus_context}

【絕對強制規則（違者視為嚴重錯誤）】：
1. **嚴禁竄改與腦補翻譯**：當你從【唯一合規參考語料庫】中選出參考句子時，該句子的**中文翻譯與達悟語必須完全照抄語料庫中原本的記錄**。絕對不准自己重新猜測、自行翻譯或曲解單字含意（例如：語料庫若記錄該句為「我很會畫圖」，你就絕對不能將其解釋或翻譯成「我很會游泳」）。
2. **強制引用原則**：你**必須**從上述清單中挑選 1 筆最相關的語料作為參考，並在 reference 中**原封不動**地列出該筆語料的正確中文與達悟文。
3. **大小寫規範**：達悟語句子的句首字母**不需要大寫**。

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的達悟語或對照羅馬字（句首保持小寫）",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子（句首保持小寫）",
  "ai_reply_zh": "回應之中文翻譯",
  "reference": "必須明確列出引用的完整參考句、原始出處、達悟文以及語料庫中原本的中文翻譯（格式範例：[ID] 出處: ... | 達悟語: ... | 中文: ...），並絕對不可自行更改中文含意。"
}}
"""

    contents = [system_prompt]

    if audio_file is not None:
        try:
            audio_bytes = get_bytes_from_input(audio_file)
            mime_type = getattr(audio_file, "type", "audio/wav")
            contents.append({"mime_type": mime_type, "data": audio_bytes})
            contents.append(
                "請對照【唯一合規參考語料庫】進行語音轉寫與回應，並務必附上至少一筆包含達悟文、中文與出處的參考資料。"
            )
        except Exception as e:
            st.error(f"讀取錄音檔失敗：{e}")
            return None
    elif text_prompt:
        contents.append(f"使用者輸入文字：{text_prompt}")
    else:
        return None

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                contents,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.2,
                },
            )
            result = json.loads(response.text)

            if "user_recognized_tao" in result:
                result["user_recognized_tao"] = lower_first_char(
                    result["user_recognized_tao"]
                )
            if "ai_reply_tao" in result:
                result["ai_reply_tao"] = lower_first_char(
                    result["ai_reply_tao"]
                )

            return result
        except ResourceExhausted:
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 5
                st.warning(
                    f"⏳ API 請求太過頻繁，正在等待 {wait_time} 秒後重試..."
                )
                time.sleep(wait_time)
            else:
                st.error("❌ 請求過於頻繁（超出限制），請稍後再試。")
                return None
        except Exception as e:
            st.error(f"❌ AI 辨識失敗：{e}")
            return None

# --- 6. 側邊欄：登入與註冊 ---
if "user_info" not in st.session_state:
    st.session_state.user_info = None

with st.sidebar:
    st.markdown(dual_text("vahay do kararay", "會員中心", is_title=True), unsafe_allow_html=True)
    st.caption("☁️ 雲端資料庫已連線")
    if st.session_state.user_info is None:
        auth_choice = st.radio("請選擇：", ["somdep 登入", "mamareng 註冊帳號"])
        if auth_choice == "somdep 登入":
            st.markdown(dual_text("ngaran", "帳號"), unsafe_allow_html=True)
            login_username = st.text_input("帳號輸入框", label_visibility="collapsed")
            st.markdown(dual_text("kagi", "密碼"), unsafe_allow_html=True)
            login_password = st.text_input("密碼輸入框", type="password", label_visibility="collapsed")
            if st.button("somdep 登入"):
                res = login_user(login_username, login_password)
                if res:
                    st.session_state.user_info = {
                        "username": res[0]["username"],
                        "email": res[0]["email"],
                        "region": res[0]["region"],
                        "age_group": res[0]["age_group"],
                        "gender": res[0]["gender"],
                    }
                    st.rerun()
                else:
                    st.error("帳號或密碼錯誤。")
        else:
            st.markdown(dual_text("ngaran", "設定帳號"), unsafe_allow_html=True)
            new_u = st.text_input("設定帳號輸入框", label_visibility="collapsed")
            st.markdown(dual_text("kagi", "設定密碼"), unsafe_allow_html=True)
            new_p = st.text_input("設定密碼輸入框", type="password", label_visibility="collapsed")
            st.markdown(dual_text("ivoka no teygami", "信箱"), unsafe_allow_html=True)
            new_e = st.text_input("信箱輸入框", label_visibility="collapsed")
            
            st.markdown(dual_text("ili", "部落"), unsafe_allow_html=True)
            new_r = st.selectbox(
                "部落選擇",
                [
                    "Jiyayo 椰油",
                    "Jiraralay 朗島",
                    "Jiranmilek 東清",
                    "Jivalino 野銀",
                    "Jimowrod 紅頭",
                    "Jiratay 漁人",
                    "Ivatan 巴丹",
                    "Ji Taywan 台灣",
                    "ilaod 其他",
                ],
                label_visibility="collapsed"
            )
            st.markdown(dual_text("kakawakawan", "年齡"), unsafe_allow_html=True)
            new_a = st.selectbox(
                "年齡選擇",
                [
                    "alikey 學齡前",
                    "kosiaw 國小",
                    "kocong aka kawcong 國高中",
                    "18-30",
                    "31-40",
                    "41-50",
                    "51-60",
                    "61-70",
                    "71-80",
                    "ikaroa a ngernan o kakawakawan 81歲及以上",
                ],
                label_visibility="collapsed"
            )
            st.markdown(dual_text("mikataretarek", "性別"), unsafe_allow_html=True)
            new_g = st.selectbox(
                "性別選擇",
                ["mehakay 男", "mavakes 女", "ji nipanci 不公開"],
                label_visibility="collapsed"
            )
            if st.button("mamareng 註冊"):
                if add_user(new_u, new_p, new_e, new_r, new_a, new_g):
                    st.success("註冊成功，請切換登入。")
                else:
                    st.warning("帳號已被註冊。")
    else:
        u = st.session_state.user_info
        st.success(f"ngaran 登入身分：**{u['username']}**")
        if st.button("makowbot 🚪 登出"):
            st.session_state.user_info = None
            st.rerun()

# --- 7. 主頁面區塊 ---
main_menu = st.radio(
    "",
    [
        "miAI ko / 我要用AI", 
        "manita so tao a miAI / 看別人用AI", 
        "amian so AI ori / 關於本站"
    ],
    horizontal=True,
)

if main_menu == "miAI ko / 我要用AI":
    st.markdown(
        dual_text("macisirisiring do AI kano misinsinmo so vakong no AI", "AI 對話與語音語料採集", is_title=True), 
        unsafe_allow_html=True
    )
    if st.session_state.user_info is None:
        st.warning("🔒 本系統需登入後使用，請先在左側邊欄登入。")
    else:
        user = st.session_state.user_info
        r_list = [
            "Jiyayo 椰油",
            "Jiraralay 朗島",
            "Jiranmilek 東清",
            "Jivalino 野銀",
            "Jimowrod 紅頭",
            "Jiratay 漁人",
            "Ivatan 巴丹",
            "Ji Taywan 台灣",
            "ilaod 其他",
        ]
        a_list = [
            "alikey 學齡前",
            "kosiaw 國小",
            "kocong aka kawcong 國高中",
            "18-30",
            "31-40",
            "41-50",
            "51-60",
            "61-70",
            "71-80",
            "ikaroa a ngernan o kakawakawan 81歲及以上",
        ]
        g_list = ["mehakay 男", "mavakes 女", "ji nipanci 不公開"]

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(dual_text("ili", "部落"), unsafe_allow_html=True)
            cur_r = st.selectbox(
                "ili",
                r_list,
                index=r_list.index(user["region"])
                if user["region"] in r_list
                else 0,
                label_visibility="collapsed"
            )
        with c2:
            st.markdown(dual_text("kakawakawan", "年齡"), unsafe_allow_html=True)
            cur_a = st.selectbox(
                "kakawakawan",
                a_list,
                index=a_list.index(user["age_group"])
                if user["age_group"] in a_list
                else 3,
                label_visibility="collapsed"
            )
        with c3:
            st.markdown(dual_text("mikataretarek", "性別"), unsafe_allow_html=True)
            cur_g = st.selectbox(
                "mikataretarek",
                g_list,
                index=g_list.index(user["gender"])
                if user["gender"] in g_list
                else 0,
                label_visibility="collapsed"
            )
        current_bg = {"region": cur_r, "age_group": cur_a, "gender": cur_g}

        st.divider()

        st.markdown(dual_text("apen mo o pangap mo do vahey ta", "請選擇輸入方式"), unsafe_allow_html=True)
        input_type = st.radio(
            "input_type_radio",
            ["koan ko / 達悟語語音輸入", "mivatvatek ko / 文字輸入"],
            horizontal=True,
            label_visibility="collapsed"
        )

        if "koan ko" in input_type:
            st.markdown(
                dual_text("apisen o maykevon do teyrahem, no mateyka am teyka rana", "請點擊下方麥克風圖示開始錄音，完成後停止即可自動辨識"), 
                unsafe_allow_html=True
            )
            voice_input = st.audio_input(
                "mapalolo so ciring 開始錄音", key="voice_input_main"
            )

            if voice_input is not None:
                if st.session_state.get("last_processed_audio") != voice_input:
                    with st.spinner(
                        "⏳ 正在辨識..."
                    ):
                        audio_bytes = get_bytes_from_input(voice_input)
                        mime_type = getattr(
                            voice_input, "type", "audio/wav"
                        )
                        ai_data = process_ai_input(audio_file=voice_input)

                        if ai_data:
                            r_tts_bytes = generate_tts_bytes(
                                ai_data.get("ai_reply_tao")
                            )
                            st.session_state.active_q = ai_data.get(
                                "user_recognized_tao", "語音輸入"
                            )
                            st.session_state.ai_data = ai_data
                            st.session_state.user_audio_bytes = audio_bytes
                            st.session_state.user_audio_mime = mime_type
                            st.session_state.ai_audio_bytes = r_tts_bytes
                            st.session_state.ai_audio_mime = (
                                "audio/mp3" if r_tts_bytes else None
                            )
                            st.session_state.active_bg = current_bg
                            st.session_state.last_processed_audio = voice_input
                            st.rerun()

        else:
            st.markdown(dual_text("mivatvatek", "請輸入"), unsafe_allow_html=True)
            text_input = st.text_input(
                "text_input_box",
                placeholder="例如：Akokay/今天天氣如何？",
                label_visibility="collapsed"
            )
            if st.button("itoro do AI 發送文字詢問"):
                if text_input.strip():
                    with st.spinner("⏳ AI 思考中..."):
                        ai_data = process_ai_input(text_prompt=text_input)
                        if ai_data:
                            q_tts_bytes = generate_tts_bytes(
                                ai_data.get(
                                    "user_recognized_tao", text_input
                                )
                            )
                            r_tts_bytes = generate_tts_bytes(
                                ai_data.get("ai_reply_tao")
                            )

                            st.session_state.active_q = text_input
                            st.session_state.ai_data = ai_data
                            st.session_state.user_audio_bytes = q_tts_bytes
                            st.session_state.user_audio_mime = (
                                "audio/mp3" if q_tts_bytes else None
                            )
                            st.session_state.ai_audio_bytes = r_tts_bytes
                            st.session_state.ai_audio_mime = (
                                "audio/mp3" if r_tts_bytes else None
                            )
                            st.session_state.active_bg = current_bg

        if "ai_data" in st.session_state and st.session_state.ai_data:
            ai_data = st.session_state.ai_data
            q_orig = st.session_state.get("active_q", "")
            bg_info = st.session_state.get("active_bg", current_bg)

            st.markdown("---")
            st.markdown(
                f"""
                <div style="padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid rgba(128, 128, 128, 0.3);">
                    <div style="font-size: 18px; font-weight: bold;">maniring o tao am (輸入與辨識)</div>
                    <div style="font-size: 18px; font-weight: bold; margin-top: 5px;">{ai_data.get('user_recognized_tao', q_orig)}</div>
                    <div style="font-size: 14px; opacity: 0.8;">{ai_data.get('user_translation', '')}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            if st.session_state.get("user_audio_bytes"):
                st.audio(
                    st.session_state.user_audio_bytes,
                    format=st.session_state.get(
                        "user_audio_mime", "audio/mp3"
                    ),
                )

            st.markdown(
                f"""
                <div style="padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid rgba(128, 128, 128, 0.3);">
                    <div style="font-size: 18px; font-weight: bold;">maniring o AI am (AI 對話回答)</div>
                    <div style="font-size: 18px; font-weight: bold; margin-top: 5px;">{ai_data.get('ai_reply_tao', '')}</div>
                    <div style="font-size: 14px; opacity: 0.8;">{ai_data.get('ai_reply_zh', '')}</div>
                </div>
                """,
                unsafe_allow_html=True
            )
            if st.session_state.get("ai_audio_bytes"):
                st.audio(
                    st.session_state.ai_audio_bytes, format="audio/mp3"
                )

            st.markdown(
                f"""
                <div style="padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid rgba(128, 128, 128, 0.3);">
                    <div style="font-size: 18px; font-weight: bold;">o pakaaranan no ciring (參考資料 / 語料來源)</div>
                    <div style="font-size: 16px; font-weight: bold; margin-top: 5px;">{ai_data.get('reference', '未提供參考資料')}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.divider()
            st.markdown(dual_text("o kapakaapiya no ciring", "語料品質評估", is_title=True), unsafe_allow_html=True)
            st.markdown(dual_text("ya apiya o niyawen kano mivaliw ya?", "這組 AI 辨識與翻譯是否正確？"), unsafe_allow_html=True)
            
            eval_choice = st.radio(
                "eval_choice_radio",
                ["apiya / 正確", "beken / 錯誤"],
                horizontal=True,
                label_visibility="collapsed"
            )

            if "apiya" in eval_choice:
                if st.button("voka ✅ 直接送出儲存至 Supabase"):
                    if save_語料_to_supabase(
                        st.session_state.user_info,
                        bg_info,
                        ai_data.get("user_recognized_tao", q_orig),
                        ai_data.get("user_translation"),
                        st.session_state.get("user_audio_bytes"),
                        st.session_state.get("user_audio_mime"),
                        ai_data.get("ai_reply_tao"),
                        ai_data.get("ai_reply_zh"),
                        st.session_state.get("ai_audio_bytes"),
                        st.session_state.get("ai_audio_mime"),
                        is_edited=False,
                        is_error=False,
                    ):
                        st.success(
                            "🎉 已成功儲存！"
                        )
                        del st.session_state.ai_data
                        st.rerun()

            elif "beken" in eval_choice:
                st.warning("⚠ 發現錯誤。您可以修正文字、語音與參考資料來源：")

                st.markdown(dual_text("ablisan o pacici", "修正【問題】"))
                e_q_tao = st.text_input(
                    "修改句子 1 達悟語：",
                    value=ai_data.get("user_recognized_tao", q_orig),
                )
                st.markdown(dual_text("ablisan o mivaliw no pacici", "修改問題翻譯"))
                e_q_trans = st.text_input(
                    "修改句子 1 翻譯：",
                    value=ai_data.get("user_translation", ""),
                )

                q_audio_rec = st.audio_input(
                    "mapalolo so ciring no pacici 🎤 重新錄製【問題語音】（選擇性）：", key="edit_q_rec"
                )
                q_audio_file = st.file_uploader(
                    "ipanta o izeziak do tyenaw 📁 上傳【語音檔】（選擇性）：",
                    type=["wav", "mp3", "m4a"],
                    key="edit_q_file",
                )

                st.markdown(dual_text("ablisan o tovis", "修正【回應】"))
                e_r_tao = st.text_input(
                    "修改句子 2 達悟語：",
                    value=ai_data.get("ai_reply_tao", ""),
                )
                st.markdown(dual_text("ablisan o mivaliw no tovis", "修改回應翻譯"))
                e_r_zh = st.text_input(
                    "修改句子 2 翻譯：",
                    value=ai_data.get("ai_reply_zh", ""),
                )

                r_audio_rec = st.audio_input(
                    "mapalolo so ciring no tovis 🎤 重新錄製【回應語音】（選擇性）：", key="edit_r_rec"
                )
                r_audio_file = st.file_uploader(
                    "ipanta ipanta o izeziak do tyenaw 📁 上傳【回應語音檔】（選擇性）：",
                    type=["wav", "mp3", "m4a"],
                    key="edit_r_file",
                )

                st.markdown(dual_text("ablisan o vakong no ciring", "修改【參考資料 / 語料來源】"))
                e_ref = st.text_input(
                    "修改【參考資料 / 語料來源】：",
                    value=ai_data.get("reference", ""),
                )

                if st.button("voka 💾 儲存"):
                    new_q_input = q_audio_rec or q_audio_file
                    if new_q_input:
                        final_q_bytes = get_bytes_from_input(new_q_input)
                        final_q_mime = getattr(
                            new_q_input, "type", "audio/wav"
                        )
                    else:
                        final_q_bytes = generate_tts_bytes(e_q_tao)
                        final_q_mime = (
                            "audio/mp3" if final_q_bytes else None
                        )

                    new_r_input = r_audio_rec or r_audio_file
                    if new_r_input:
                        final_r_bytes = get_bytes_from_input(new_r_input)
                        final_r_mime = getattr(
                            new_r_input, "type", "audio/wav"
                        )
                    else:
                        final_r_bytes = generate_tts_bytes(e_r_tao)
                        final_r_mime = (
                            "audio/mp3" if final_r_bytes else None
                        )

                    if save_語料_to_supabase(
                        st.session_state.user_info,
                        bg_info,
                        lower_first_char(e_q_tao),
                        e_q_trans,
                        final_q_bytes,
                        final_q_mime,
                        lower_first_char(e_r_tao),
                        e_r_zh,
                        final_r_bytes,
                        final_r_mime,
                        is_edited=True,
                        is_error=True,
                    ):
                        st.success("🎉 已成功更新")
                        del st.session_state.ai_data
                        st.rerun()

elif main_menu == "manita so tao a miAI / 看別人用AI":
    st.markdown(
        dual_text("kapisirisiring kano manita o vakong no ciring", "社群驗證與語料檢視", is_title=True), 
        unsafe_allow_html=True
    )
    try:
        res_feedback = (
            supabase.from_("feedback")
            .select("*")
            .order("timestamp", desc=True)
            .execute()
        )
        res_votes = supabase.from_("community_votes").select("*").execute()

        feedbacks = res_feedback.data if res_feedback.data else []
        votes_data = res_votes.data if res_votes.data else []

        votes_map = {}
        for v in votes_data:
            fid = v.get("feedback_id")
            if fid not in votes_map:
                votes_map[fid] = []
            votes_map[fid].append(v)

        if not feedbacks:
            st.info("目前尚無採集到的語料。")
        else:
            current_user = (
                st.session_state.user_info["username"]
                if st.session_state.user_info
                else None
            )

            for fb in feedbacks:
                fb_id = fb["id"]
                fb_votes = votes_map.get(fb_id, [])
                vote_count = len(fb_votes)

                agree_count = sum(1 for v in fb_votes if v.get("vote_result") == "贊同")
                disagree_count = sum(1 for v in fb_votes if v.get("vote_result") == "不贊同")

                user_voted = any(
                    v.get("voter_id") == current_user for v in fb_votes
                )

                with st.expander(
                    f"📌 語料 ID #{fb_id} - 由 {fb.get('user_id', '匿名')} 提供 ({str(fb.get('timestamp', ''))[:10]})"
                ):
                    col_info, col_action = st.columns([4, 1])

                    with col_info:
                        st.markdown(
                            f"""
                            <div style="padding: 10px; border-radius: 6px; margin-bottom: 8px; border: 1px solid rgba(128, 128, 128, 0.3);">
                                <div style="font-size: 16px; font-weight: bold;">pacici (問)</div>
                                <div style="font-size: 18px; font-weight: bold; margin-top: 4px;">{fb.get('q_original')}</div>
                                <div style="font-size: 14px; opacity: 0.8;">{fb.get('q_trans')}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                        if fb.get("q_audio_data"):
                            try:
                                q_bytes = bytes.fromhex(fb["q_audio_data"].replace("\\x", ""))
                                st.audio(q_bytes, format=fb.get("q_audio_mime", "audio/wav"))
                            except Exception:
                                pass

                        st.markdown(
                            f"""
                            <div style="padding: 10px; border-radius: 6px; margin-bottom: 8px; border: 1px solid rgba(128, 128, 128, 0.3);">
                                <div style="font-size: 16px; font-weight: bold;">tovis (答)</div>
                                <div style="font-size: 18px; font-weight: bold; margin-top: 4px;">{fb.get('tao_text')}</div>
                                <div style="font-size: 14px; opacity: 0.8;">{fb.get('zh_text')}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )
                        if fb.get("r_audio_data"):
                            try:
                                r_bytes = bytes.fromhex(fb["r_audio_data"].replace("\\x", ""))
                                st.audio(r_bytes, format=fb.get("r_audio_mime", "audio/mp3"))
                            except Exception:
                                pass

                        st.markdown(
                            f"""
                            <div style="font-size: 14px; opacity: 0.8; margin-top: 5px;">
                                <b>o pakatotoan (背景):</b> {fb.get('region')} | {fb.get('age_group')} | {fb.get('gender')}
                            </div>
                            """,
                            unsafe_allow_html=True
                        )

                        if fb_votes:
                            st.markdown("---")
                            st.caption("📊 方言差與投票背景統計：")
                            for v in fb_votes:
                                r_voter = v.get("region", "未知部落")
                                a_voter = v.get("age_group", "未知年齡")
                                g_voter = v.get("gender", "未知性別")
                                res_voter = v.get("vote_result", "未表態")
                                sug_voter = v.get("hidden_suggestion", "")
                                sug_text = f" (建議: {sug_voter})" if sug_voter else ""
                                st.text(f"• [{r_voter} / {a_voter} / {g_voter}] 投: {res_voter}{sug_text}")

                    with col_action:
                        st.markdown(dual_text("kapisirisiring", "總投票數"), unsafe_allow_html=True)
                        st.metric("總投票數", f"{vote_count} 票", label_visibility="collapsed")
                        st.caption(f"👍 apiya 贊同: {agree_count} | 👎 beken 不贊同: {disagree_count}")

                        if current_user:
                            if user_voted:
                                st.markdown(dual_text("niapiya", "已贊同"), unsafe_allow_html=True)
                            else:
                                st.markdown("##### 🗳️ 參與評估")
                                vote_choice = st.radio(
                                    "選擇投票：",
                                    ["apiya 贊同", "beken 不贊同"],
                                    key=f"v_choice_{fb_id}",
                                    horizontal=True,
                                )
                                suggestion_input = st.text_area(
                                    "選填建議說明：",
                                    key=f"v_sug_{fb_id}",
                                    placeholder="可輸入不同部落的慣用語法或修正意見...",
                                )

                                if st.button("voka 送出", key=f"btn_vote_{fb_id}"):
                                    try:
                                        voter_info = st.session_state.user_info
                                        vote_record = {
                                            "feedback_id": fb_id,
                                            "voter_id": current_user,
                                            "region": voter_info.get("region", "未知"),
                                            "age_group": voter_info.get("age_group", "未知"),
                                            "gender": voter_info.get("gender", "未知"),
                                            "vote_result": vote_choice,
                                            "hidden_suggestion": suggestion_input,
                                            "timestamp": datetime.datetime.now().isoformat(),
                                        }
                                        supabase.from_("community_votes").insert(vote_record).execute()
                                        st.success("🎉 已儲存！")
                                        st.rerun()
                                    except Exception as err:
                                        st.error(f"投票失敗：{err}")

                            if (
                                current_user == "admin"
                                or fb.get("user_id") == current_user
                            ):
                                if st.button("opas 🗑️ 刪除", key=f"del_{fb_id}"):
                                    ok, msg = delete_feedback_item(
                                        fb_id, current_user
                                    )
                                    if ok:
                                        st.success(msg)
                                        st.rerun()
                                    else:
                                        st.error(msg)

    except Exception as e:
        st.error(f"讀取社群資料失敗：{e}")

elif main_menu == "amian so AI ori / 關於本站":
    st.subheader("🏝️ 關於 ciriciring no iciatatao (眾語)")
    st.markdown(
        """
        ### 專案願景
        本平台致力於保存與推廣**達悟語（Yami/Tao）**，結合前沿大型語言模型（Gemini）與在地部落語料庫，建構即時雙向的語音與文字對話互動系統。

        ### 系統特色
        1. **正詞法與音系約束**：採用達悟語專屬羅馬字拼音規則，自動關閉瀏覽器拼字檢查以提供最佳輸入體驗。
        2. **嚴謹引用機制**：AI 生成回應時，強制比對與引用合規參考語料庫，確保語法與文化情境精確。
        3. **社群共同驗證與方言差分析**：引入眾包（Crowdsourcing）評估與投票機制，結合使用者部落、年齡與性別背景，記錄贊同與不贊同意見與改善建議，有助於後續的方言差比對與統計。
        4. **模型訓練資料來源**：本模型所使用的訓練與參考資料皆來自 [FormosanBank](https://github.com/FormosanBank/FormosanBank/tree/main)，包含以下重要來源：
           * **族語E樂園雅美(達悟)語版**：Indigenous Languages Research and Development Foundation. (2020). 族語E樂園. https://web.klokah.tw/
           * **原語會族語線上辭典雅美(達悟)語版**：Council of Indigenous Peoples, & Indigenous Languages Research and Development Foundation. (2024, January). 原住民族語言線上辭典: 雅美語. Executing Institution: National Taiwan Normal University. https://e-dictionary.ilrdf.org.tw/
           * **總統道歉文雅美(達悟)語版**：Tsai, I. W. (2016, August 1). President Tsai Ing-wen's apology to the Indigenous Peoples on behalf of the government [Speech transcript]. Office of the President.
           * **Rau-Dong**：Rau, D. V., and Dong, M. N. (2006). Yami texts with reference grammar and dictionary. Institute of Linguistics, Academia Sinica.
           * **The Wakelin Texts**：Indosan, S., Wakelin, G., Dararyaw, S., and Kalaku, S. (1958). Yami texts. Work Papers of the Summer Institute of Linguistics, University of North Dakota Session: Vol. 2, Article 7. 10.31356/silwp.vol02.07
        """
    )
