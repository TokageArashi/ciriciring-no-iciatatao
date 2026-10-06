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

# --- 輔助函式 ---
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

        // 頁面首次載入時執行
        disableSpellcheck();

        // 建立 MutationObserver 監控 Streamlit 的動態 DOM 變化
        const observer = new MutationObserver((mutations) => {
            disableSpellcheck();
        });

        // 開始監聽整個 body 結構變化
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
    
    /* 強制移除輸入框與文字區域的拼字檢查紅線外觀 */
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
        <h1 style='font-size: 42px; font-weight: bold; color: #1E3A8A;'>ciriciring no iciatatao</h1>
        <div style='font-size: 24px; color: #4B5563; font-weight: 600;'>眾語</div>
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
        # 設定為 Tagalog (tl) 塔加祿語
        tts = gTTS(text=text, lang="tl", slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


# --- 快取語料庫讀取 (設定 ttl 為 300 秒，避免每次 API 請求都重新撈取全表) ---
@st.cache_data(ttl=300)
def fetch_legal_corpus():
    legal_corpus = []

    # 1. 從 corpus 讀取官方語料
    try:
        res_corpus = supabase.from_("corpus").select("*").execute()
        for r in res_corpus.data:
            legal_corpus.append(
                f"[Corpus 官方語料 ID #{r.get('id')}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('r_tao')} | 回應中文: {r.get('r_zh')}"
            )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取 corpus 語料庫提示：{e}")

    # 2. 優化 N+1 查詢：一次撈取 feedback 及其對應的投票數
    try:
        res_feedback = (
            supabase.from_("feedback")
            .select("*, community_votes(id)")
            .eq("is_ready_for_ai", 1)
            .execute()
        )
        for r in res_feedback.data:
            votes = r.get("community_votes", [])
            if len(votes) >= 15:
                f_id = r.get("id")
                legal_corpus.append(
                    f"[Feedback 15人驗證語料 ID #{f_id}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('tao_text')} | 回應中文: {r.get('zh_text')}"
                )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取社群驗證語料提示：{e}")

    return legal_corpus[-100:] if legal_corpus else []


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

    legal_corpus = fetch_legal_corpus()

    corpus_context = (
        "\n".join(legal_corpus)
        if legal_corpus
        else "【目前尚無合規的參考語料】"
    )

    system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。

【唯一合規參考語料庫】:
{corpus_context}

【參考資料比對與引用規則（強制執行）】：
1. **強制引用原則**：你**必須**從【唯一合規參考語料庫】中，挑選出**至少 1 筆且最多 5 筆**最相關的語料作為參考資料。絕不能填寫「無參考資料」或留空。
2. **相似度比對優先序**：
   - 優先搜尋：句意相似或包含相同字詞數最多的句子。
   - 次要搜尋：若無完整匹配句子，請比對核心關鍵字（如核心動詞、名詞、主詞），忽略格位標記（o, no, do）或焦點標記。
   - 備選方案：若完全找不到情境相同的句子，請挑選語法結構最接近，或包含共通單字的語料。
3. **大小寫規範**：達悟語句子的句首字母**不需要大寫**，請統一保持為小寫。

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的達悟語或對照羅馬字（句首保持小寫）",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子（句首保持小寫）",
  "ai_reply_zh": "回應之中文翻譯",
  "reference": "必須列出至少 1 筆引用的完整句子與語料 ID（格式：[語料類型 ID #X] 句子...），並簡短說明比對到的關鍵字或關聯性"
}}
"""

    contents = [system_prompt]

    if audio_file is not None:
        try:
            audio_bytes = get_bytes_from_input(audio_file)
            mime_type = getattr(audio_file, "type", "audio/wav")
            contents.append({"mime_type": mime_type, "data": audio_bytes})
            contents.append(
                "請對照【唯一合規參考語料庫】進行語音轉寫與回應，並務必附上至少一筆參考資料。"
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
    st.title("👤 vahay do kararay 會員中心")
    st.caption("☁️ 雲端資料庫已連線")
    if st.session_state.user_info is None:
        auth_choice = st.radio("請選擇：", ["登入", "註冊帳號"])
        if auth_choice == "登入":
            login_username = st.text_input("帳號")
            login_password = st.text_input("密碼", type="password")
            if st.button("登入"):
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
            new_u = st.text_input("設定帳號")
            new_p = st.text_input("設定密碼", type="password")
            new_e = st.text_input("信箱")
            new_r = st.selectbox(
                "ili 部落：",
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
            )
            new_a = st.selectbox(
                "kakawakawan 年齡：",
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
            )
            new_g = st.selectbox(
                "mikataretarek 性別：",
                ["mehakay 男", "mavakes 女", "ji nipanci 不公開"],
            )
            if st.button("註冊"):
                if add_user(new_u, new_p, new_e, new_r, new_a, new_g):
                    st.success("註冊成功，請切換登入。")
                else:
                    st.warning("帳號已被註冊。")
    else:
        u = st.session_state.user_info
        st.success(f"登入身分：**{u['username']}**")
        if st.button("🚪 登出"):
            st.session_state.user_info = None
            st.rerun()

# --- 7. 主頁面區塊 ---
main_menu = st.radio(
    "",
    ["miAI ko 我要用AI", "manita so tao a miAI 看別人用AI", "amian so AI ori 關於本站"],
    horizontal=True,
)

if main_menu == "miAI ko 我要用AI":
    st.subheader(
        "💬 macisirisiring do AI kano misinsinmo so vakong no AI AI 對話與語音語料採集"
    )
    if st.session_state.user_info is None:
        st.warning("🔒 本系統需登入後使用，請先在左側邊欄登入。")
    else:
        user = st.session_state.user_info
        st.markdown("##### 📱 本次輸入者設定")
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
            cur_r = st.selectbox(
                "ili 部落：",
                r_list,
                index=r_list.index(user["region"])
                if user["region"] in r_list
                else 0,
            )
        with c2:
            cur_a = st.selectbox(
                "kakawakawan 年齡：",
                a_list,
                index=a_list.index(user["age_group"])
                if user["age_group"] in a_list
                else 3,
            )
        with c3:
            cur_g = st.selectbox(
                "mikataretarek 性別：",
                g_list,
                index=g_list.index(user["gender"])
                if user["gender"] in g_list
                else 0,
            )
        current_bg = {"region": cur_r, "age_group": cur_a, "gender": cur_g}

        st.divider()

        input_type = st.radio(
            "apen mo o pangap mo do vahey ta 請選擇輸入方式：",
            ["🎤 koan ko 達悟語語音輸入", "⌨️ mivatvatek ko 文字輸入"],
            horizontal=True,
        )

        if input_type == "🎤 koan ko 達悟語語音輸入":
            st.caption(
                "meypespes so maykevon oya, no teyka meyzezyak am teyka rana"
                " 請點擊下方麥克風圖示開始錄音，完成後停止即可自動辨識："
            )
            voice_input = st.audio_input(
                "mapalolo so ciring 開始錄音", key="voice_input_main"
            )

            if voice_input is not None:
                if st.session_state.get("last_processed_audio") != voice_input:
                    with st.spinner(
                        "⏳ 正在聽取語音並依達悟語音系轉寫中..."
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
            text_input = st.text_input(
                "manvood so teygami 請輸入：",
                placeholder="例如：Akokay/今天天氣如何？",
            )
            if st.button("🚀 itoro so teygami 發送文字詢問"):
                if text_input.strip():
                    with st.spinner("⏳ AI 思考與語音合成中..."):
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
            st.info(
                f"**【句子 1 - maniring o tao am 輸入與辨識】**\n* ciriciring no tao"
                f" 辨識/mivaliw 原文：{ai_data.get('user_recognized_tao', q_orig)}\n*"
                f" 翻譯：{ai_data.get('user_translation', '')}"
            )
            if st.session_state.get("user_audio_bytes"):
                st.audio(
                    st.session_state.user_audio_bytes,
                    format=st.session_state.get(
                        "user_audio_mime", "audio/mp3"
                    ),
                )

            st.success(
                f"**【句子 2 - maniring o AI am AI 對話回答】**\n* ciriciring no tao"
                f" 達悟(雅美)語：{ai_data.get('ai_reply_tao', '')}\n* mivaliw"
                f" 中文對照：{ai_data.get('ai_reply_zh', '')}"
            )
            if st.session_state.get("ai_audio_bytes"):
                st.audio(
                    st.session_state.ai_audio_bytes, format="audio/mp3"
                )

            st.warning(
                f"**【📚 參考資料 / 語料來源（上限 5 句）】**\n*"
                f" {ai_data.get('reference', '未提供參考資料')}"
            )

            st.divider()
            st.subheader("📝 語料品質評估")
            eval_choice = st.radio(
                "manakem mo o ipanci mo? 這組 AI 辨識與翻譯是否正確？",
                ["正確", "錯誤"],
                horizontal=True,
            )

            if eval_choice == "正確":
                if st.button("✅ 直接送出儲存至 Supabase"):
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
                            "🎉 語料與語音檔已成功寫入 Supabase 雲端資料庫！"
                        )
                        del st.session_state.ai_data
                        st.rerun()

            elif eval_choice == "錯誤":
                st.warning(
                    "⚠ 發現錯誤。您可以修正文字、語音與參考資料來源："
                )

                # 1. 修正問題 (句子 1)
                st.markdown("##### ✏ 修正【句子 1 - 問題】")
                e_q_tao = st.text_input(
                    "修改句子 1 達悟語：",
                    value=ai_data.get("user_recognized_tao", q_orig),
                )
                e_q_trans = st.text_input(
                    "修改句子 1 翻譯：",
                    value=ai_data.get("user_translation", ""),
                )

                q_audio_rec = st.audio_input(
                    "🎤 重新錄製【問題語音】（選擇性）：", key="edit_q_rec"
                )
                q_audio_file = st.file_uploader(
                    "📁 上傳【問題語音檔】（選擇性）：",
                    type=["wav", "mp3", "m4a"],
                    key="edit_q_file",
                )

                # 2. 修正回應 (句子 2)
                st.markdown("##### ✏️ 修正【句子 2 - 回應】")
                e_r_tao = st.text_input(
                    "修改句子 2 達悟語：",
                    value=ai_data.get("ai_reply_tao", ""),
                )
                e_r_zh = st.text_input(
                    "修改句子 2 翻譯：",
                    value=ai_data.get("ai_reply_zh", ""),
                )

                r_audio_rec = st.audio_input(
                    "🎤 重新錄製【回應語音】（選擇性）：", key="edit_r_rec"
                )
                r_audio_file = st.file_uploader(
                    "📁 上傳【回應語音檔】（選擇性）：",
                    type=["wav", "mp3", "m4a"],
                    key="edit_r_file",
                )

                # 3. 修正參考資料
                e_ref = st.text_input(
                    "修改【參考資料 / 語料來源】：",
                    value=ai_data.get("reference", ""),
                )

                if st.button("💾 儲存修正版至 Supabase"):
                    # 處理問題語音檔
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

                    # 處理回應語音檔
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

                    # 寫入資料庫
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
                        st.success("🎉 修正版語料與語音已成功更新至 Supabase 雲端資料庫！")
                        del st.session_state.ai_data
                        st.rerun()

elif main_menu == "manita so tao a miAI 看別人用AI":
    st.subheader("👥 社群驗證與語料檢視")
    try:
        res = (
            supabase.from_("feedback")
            .select("*, community_votes(id, voter_username)")
            .order("timestamp", desc=True)
            .execute()
        )
        feedbacks = res.data

        if not feedbacks:
            st.info("目前尚無採集到的語料。")
        else:
            current_user = (
                st.session_state.user_info["username"]
                if st.session_state.user_info
                else None
            )

            for fb in feedbacks:
                with st.expander(
                    f"📌 語料 ID #{fb['id']} - 由 {fb['user_id']} 提供 ({fb['timestamp'][:10]})"
                ):
                    col_info, col_action = st.columns([4, 1])

                    with col_info:
                        st.write(f"**問（達悟語）：** {fb.get('q_original')}")
                        st.write(f"**問（中文）：** {fb.get('q_trans')}")
                        st.write(f"**答（達悟語）：** {fb.get('tao_text')}")
                        st.write(f"**答（中文）：** {fb.get('zh_text')}")
                        st.write(
                            f"**部落/背景：** {fb.get('region')} | {fb.get('age_group')} | {fb.get('gender')}"
                        )

                    votes = fb.get("community_votes", [])
                    vote_count = len(votes)
                    user_voted = any(
                        v.get("voter_username") == current_user for v in votes
                    )

                    with col_action:
                        st.metric("社群認同數", f"{vote_count} 票")

                        if current_user:
                            if user_voted:
                                st.success("已贊同")
                            else:
                                if st.button(
                                    "👍 贊同 (+1)", key=f"vote_{fb['id']}"
                                ):
                                    supabase.from_("community_votes").insert(
                                        {
                                            "feedback_id": fb["id"],
                                            "voter_username": current_user,
                                        }
                                    ).execute()
                                    st.rerun()

                            # 權限刪除檢查 (提供者本或 admin 均可刪除)
                            if (
                                current_user == "admin"
                                or fb.get("user_id") == current_user
                            ):
                                if st.button(
                                    "🗑️ 刪除", key=f"del_{fb['id']}"
                                ):
                                    ok, msg = delete_feedback_item(
                                        fb["id"], current_user
                                    )
                                    if ok:
                                        st.success(msg)
                                        st.rerun()
                                    else:
                                        st.error(msg)

    except Exception as e:
        st.error(f"讀取社群資料失敗：{e}")

elif main_menu == "amian so AI ori 關於本站":
    st.subheader("🏝️ 關於 ciriciring no iciatatao (眾語)")
    st.markdown(
        """
        ### 專案願景
        本平台致力於保存與推廣**達悟語（Yami/Tao）**，結合前沿大型語言模型（Gemini）與在地部落語料庫，建構即時雙向的語音與文字對話互動系統。

        ### 系統特色
        1. **正詞法與音系約束**：採用達悟語專屬羅馬字拼音規則，自動關閉瀏覽器拼字檢查以提供最佳輸入體驗。
        2. **嚴謹引用機制**：AI 生成回應時，強制比對與引用合規參考語料庫，確保語法與文化情境精確。
        3. **社群共同驗證**：引入眾包（Crowdsourcing）評估與投票機制，收集在地部落族人經驗，持續優化開放語料庫。
        """
    )
