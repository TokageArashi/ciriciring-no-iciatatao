import datetime
import hashlib
import io
import json
import os
import uuid

import google.generativeai as genai
from gTTS import gTTS
import pandas as pd
import streamlit as st
from supabase import Client, create_client

# --- 1. 全域設定與 Supabase 連線 ---
MODEL_NAME = "gemini-3.6-flash"

st.set_page_config(
    page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide"
)

# 自訂 CSS 樣式：設定達悟文與中文換行編排及獨立字級
st.markdown(
    """
    <style>
    /* 選單 Radio Group 樣式：支援換行與字級 */
    div[role='radiogroup'] label { 
        font-size: 20px !important; 
        font-weight: bold !important; 
        padding: 5px !important;
        white-space: pre-line !important; 
    }
    .stButton>button { width: 100%; height: 3.2em; font-size: 18px !important; white-space: pre-line !important; }
    .stTextInput input { font-size: 18px !important; }
    
    /* 雙語文字格式化 */
    .tao-text-lg { font-size: 22px; font-weight: bold; color: #1E3A8A; line-height: 1.3; }
    .zh-text-sm { font-size: 15px; color: #4B5563; font-weight: 500; }
    .tao-header { font-size: 26px; font-weight: bold; color: #1E3A8A; margin-bottom: 2px; }
    .zh-subheader { font-size: 18px; font-weight: 600; color: #4B5563; margin-bottom: 10px; }
    </style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style='text-align: center; padding-top: 5px; padding-bottom: 15px;'>
        <h1 style='font-size: 42px; font-weight: bold; color: #1E3A8A; margin-bottom: 0px;'>ciriciring no iciatatao</h1>
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
        tts = gTTS(text=text, lang="id", slow=False)
        fp = io.BytesIO()
        tts.write_to_fp(fp)
        fp.seek(0)
        return fp.read()
    except Exception:
        return None


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

    legal_corpus = []

    # 1. 從 corpus 讀取官方語料
    try:
        res_corpus = supabase.from_("corpus").select("*").execute()
        for r in res_corpus.data:
            legal_corpus.append(
                f"[Corpus 語料 ID #{r.get('id')}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('r_tao')} | 回應中文: {r.get('r_zh')}"
            )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取 corpus 語料庫提示：{e}")

    # 2. 從 feedback 讀取「超過 15 人投票」且「通過驗證」的語料
    try:
        res_feedback = (
            supabase.from_("feedback")
            .select("*")
            .eq("is_ready_for_ai", 1)
            .execute()
        )
        for r in res_feedback.data:
            f_id = r.get("id")
            votes_res = (
                supabase.from_("community_votes")
                .select("id")
                .eq("feedback_id", f_id)
                .execute()
            )
            if len(votes_res.data) >= 15:
                legal_corpus.append(
                    f"[15人驗證語料 ID #{f_id}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('tao_text')} | 回應中文: {r.get('zh_text')}"
                )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取社群驗證語料提示：{e}")

    corpus_context = (
        "\n".join(legal_corpus)
        if legal_corpus
        else "【目前尚無合規的參考語料】"
    )

    system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。

【唯一合規參考語料庫】:
{corpus_context}

【參考資料比對與引用規則】：
1. **比對優先序**：原則上與使用者輸入句子「相同字數越多越好」。
2. **單字與關鍵字備選**：若無法找到多字相符的句子，可以只參考包含「一個字」的例句；在此情況下，必須以句子中的「關鍵字（核心實詞/動詞/名詞）」優先採納，而「文法標記（如格位標記 o, no, do、焦點標記等虛詞）」可忽略不計。
3. **來源與數量限制**：僅能從【唯一合規參考語料庫】中尋找並引用，最多**不得超過 5 句**。若完全無可參考之語料，請於 reference 中註明「無相符合規參考語料」。

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的達悟語或對照羅馬字",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子",
  "ai_reply_zh": "回應之中文翻譯",
  "reference": "說明引用的完整句子與語料 ID（最多 5 句），並註明匹配的關鍵字；若無則填寫無相符合規參考語料"
}}
"""

    contents = [system_prompt]

    if audio_file is not None:
        try:
            audio_bytes = get_bytes_from_input(audio_file)
            mime_type = getattr(audio_file, "type", "audio/wav")
            contents.append({"mime_type": mime_type, "data": audio_bytes})
            contents.append(
                "請對照【唯一合規參考語料庫】進行語音轉寫與回應。"
            )
        except Exception as e:
            st.error(f"讀取錄音檔失敗：{e}")
            return None
    elif text_prompt:
        contents.append(f"使用者輸入文字：{text_prompt}")
    else:
        return None

    try:
        response = model.generate_content(
            contents,
            generation_config={
                "response_mime_type": "application/json",
                "temperature": 0.0,
            },
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"❌ AI 辨識失敗：{e}")
        return None


# --- 6. 側邊欄：登入與註冊 ---
if "user_info" not in st.session_state:
    st.session_state.user_info = None

with st.sidebar:
    st.markdown(
        """
        <div class='tao-header'>vahay do kararay</div>
        <div class='zh-subheader'>會員中心</div>
    """,
        unsafe_allow_html=True,
    )
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
    [
        "miAI ko\n我要用AI",
        "manita so tao a miAI\n看別人用AI",
        "amian so AI ori\n關於本站",
    ],
    horizontal=True,
)

if main_menu == "miAI ko\n我要用AI":
    st.markdown(
        """
        <div class='tao-header'>macisirisiring do AI kano misinsinmo so vakong no AI</div>
        <div class='zh-subheader'>AI 對話與語音語料採集</div>
    """,
        unsafe_allow_html=True,
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
            "apen mo o pangap mo do vahey ta\n請選擇輸入方式：",
            [
                "🎤 koan ko\n達悟語語音輸入",
                "⌨️ mivatvatek ko\n文字輸入",
            ],
            horizontal=True,
        )

        if input_type == "🎤 koan ko\n達悟語語音輸入":
            st.caption(
                "meypespes so maykevon oya, no teyka meyzezyak am teyka rana\n請點擊下方麥克風圖示開始錄音，完成後停止即可自動辨識："
            )
            voice_input = st.audio_input(
                "mapalolo so ciring\n開始錄音", key="voice_input_main"
            )

            if voice_input is not None:
                if st.session_state.get("last_processed_audio") != voice_input:
                    with st.spinner("⏳ 正在聽取語音並依達悟語音系轉寫中..."):
                        audio_bytes = get_bytes_from_input(voice_input)
                        mime_type = getattr(voice_input, "type", "audio/wav")
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
                "manvood so teygami\n請輸入：",
                placeholder="例如：Akokay/今天天氣如何？",
            )
            if st.button("🚀 itoro so teygami\n發送文字詢問"):
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
                f"**【句子 1 - maniring o tao am\n輸入與辨識】**\n* ciriciring no tao 辨識/mivaliw 原文：{ai_data.get('user_recognized_tao', q_orig)}\n* 翻譯：{ai_data.get('user_translation', '')}"
            )
            if st.session_state.get("user_audio_bytes"):
                st.audio(
                    st.session_state.user_audio_bytes,
                    format=st.session_state.get("user_audio_mime", "audio/mp3"),
                )

            st.success(
                f"**【句子 2 - maniring o AI am\nAI 對話回答】**\n* ciriciring no tao 達悟(雅美)語：{ai_data.get('ai_reply_tao', '')}\n* mivaliw 中文對照：{ai_data.get('ai_reply_zh', '')}"
            )
            if st.session_state.get("ai_audio_bytes"):
                st.audio(st.session_state.ai_audio_bytes, format="audio/mp3")

            st.warning(
                f"**【📚 參考資料 / 語料來源（上限 5 句）】**\n* {ai_data.get('reference', '未提供參考資料')}"
            )

            st.divider()
            st.subheader("📝 語料品質評估")
            eval_choice = st.radio(
                "manakem mo o ipanci mo?\n這組 AI 辨識與翻譯是否正確？",
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
                    "⚠️ 發現錯誤。您可以修正文字、語音與參考資料來源："
                )

                # 1. 修正問題 (句子 1)
                st.markdown("##### ✏️ 修正【句子 1 - 問題】")
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
                    "修改句子 2 達悟語：", value=ai_data.get("ai_reply_tao", "")
                )
                e_r_zh = st.text_input(
                    "修改句子 2 翻譯：", value=ai_data.get("ai_reply_zh", "")
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
                        e_q_tao,
                        e_q_trans,
                        final_q_bytes,
                        final_q_mime,
                        e_r_tao,
                        e_r_zh,
                        final_r_bytes,
                        final_r_mime,
                        is_edited=True,
                        is_error=False,
                    ):
                        st.success(
                            "🎉 修正版語料與更新後的語音檔已成功儲存至 Supabase！"
                        )
                        del st.session_state.ai_data
                        st.rerun()

elif main_menu == "manita so tao a miAI\n看別人用AI":
    st.markdown(
        """
        <div class='tao-header'>vakong no cireng kano mapili</div>
        <div class='zh-subheader'>社群公開語料審查與投票</div>
    """,
        unsafe_allow_html=True,
    )

    try:
        res = (
            supabase.from_("feedback")
            .select("*")
            .order("id", desc=True)
            .execute()
        )
        rows = res.data
    except Exception as e:
        st.error(f"無法從 Supabase 讀取資料：{e}")
        rows = []

    if not rows:
        st.info("目前 Supabase 中尚無語料資料。")
    else:
        current_username = (
            st.session_state.user_info["username"]
            if st.session_state.user_info
            else None
        )

        for i, row in enumerate(rows, start=1):
            f_id = row["id"]
            owner_id = row.get("user_id", "匿名")
            region = row.get("region", "未知")
            is_edited = row.get("is_edited", 0)
            error_count = row.get("error_count", 0)
            status_tag = (
                "✏️ 經修訂"
                if is_edited
                else ("❌ 含有錯" if error_count > 0 else "✅ 原始產出")
            )

            with st.expander(
                f"💬 對話 第 {i} 筆 (ID #{f_id}) | 上傳者：{owner_id} | 部落：{region} | 狀態：{status_tag}"
            ):
                st.markdown("**【句子 1 - 輸入與翻譯】**")
                st.write(f"1. 達悟語：{row.get('q_original')}")
                st.write(f"2. 翻譯：{row.get('q_trans')}")
                if row.get("q_audio_data"):
                    st.audio(
                        bytes.fromhex(
                            row["q_audio_data"].replace("\\x", "")
                        ),
                        format=row.get("q_audio_mime", "audio/wav"),
                    )

                st.markdown("**【句子 2 - 對答與翻譯】**")
                st.write(f"3. 達悟語：{row.get('tao_text')}")
                st.write(f"4. 中文對照：{row.get('zh_text')}")
                if row.get("r_audio_data"):
                    st.audio(
                        bytes.fromhex(
                            row["r_audio_data"].replace("\\x", "")
                        ),
                        format=row.get("r_audio_mime", "audio/mp3"),
                    )

                st.divider()

                if current_username and (
                    current_username == owner_id or current_username == "admin"
                ):
                    if st.button(
                        f"🗑️ 刪除此筆資料 (ID #{f_id})", key=f"del_{f_id}"
                    ):
                        success, msg = delete_feedback_item(
                            f_id, current_username
                        )
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                    st.divider()

                votes_res = (
                    supabase.from_("community_votes")
                    .select("*")
                    .eq("feedback_id", f_id)
                    .execute()
                )
                all_votes = votes_res.data
                vote_count = len(all_votes)

                user_voted = (
                    any(v["voter_id"] == current_username for v in all_votes)
                    if current_username
                    else False
                )

                if vote_count >= 15:
                    correct_votes = sum(
                        1 for v in all_votes if v.get("vote_result") == "正確"
                    )
                    correct_pct = (correct_votes / vote_count) * 100
                    st.success(
                        f"📊 社群盲投票結果 (已滿 15 人)：正確率 {correct_pct:.1f}%"
                    )

                    if correct_pct >= 80.0 and row.get("is_ready_for_ai") != 1:
                        supabase.from_("feedback").update(
                            {"is_ready_for_ai": 1}
                        ).eq("id", f_id).execute()
                else:
                    st.info(
                        f"🔒 社群盲投票進行中：目前累積 {vote_count}/15 票。"
                    )

                if st.session_state.user_info is None:
                    st.caption("🔒 請先登入帳號以參與投票。")
                elif user_voted:
                    st.warning(
                        "🖐️ 您已參與過此筆語料的投票，感謝協助！"
                    )
                else:
                    st.markdown("##### 🗳️ 我要投票")
                    vote_opt = st.radio(
                        f"您認為對話 #{f_id} 是否道地正確？",
                        ["正確", "錯誤"],
                        key=f"v_opt_{f_id}",
                        horizontal=True,
                    )
                    hidden_sug = st.text_area(
                        "改善建議 (僅供 AI 學習對照)：", key=f"sug_{f_id}"
                    )

                    if st.button("送出投票", key=f"v_btn_{f_id}"):
                        voter = st.session_state.user_info
                        v_data = {
                            "feedback_id": f_id,
                            "voter_id": voter["username"],
                            "region": voter["region"],
                            "age_group": voter["age_group"],
                            "gender": voter["gender"],
                            "vote_result": vote_opt,
                            "hidden_suggestion": hidden_sug,
                            "timestamp": datetime.datetime.now().isoformat(),
                        }
                        supabase.from_("community_votes").insert(
                            v_data
                        ).execute()
                        st.success("🎉 投票已送出！")
                        st.rerun()

elif main_menu == "amian so AI ori\n關於本站":
    st.markdown(
        """
### 關於本站：蘭嶼在地化語言學習與語料採集平台

歡迎使用 **ciriciring no iciatatao (眾語)**！本平台致力於結合 AI 技術與社群力量，推動達悟語（Yami/Tao）的保存、學習與對話應用。透過雙向翻譯、語音轉寫、AI 對答與社群審查機制，我們希望建立一個精準且道地的達悟語數位語料庫。

以下為本平台的三大核心功能使用指南：

**1. miAI ko (我要用 AI)**

* **個人背景設定**：使用前請先於側邊欄登入，並確認您的部落、年齡與性別設定，這有助於語料的分類與記錄。
* **輸入方式**：
  * **🎤 達悟語語音輸入**：點擊麥克風按鈕進行錄音，系統將自動進行語音辨識、轉寫與翻譯。
  * **⌨️ 文字輸入**：輸入達悟語或中文句子，AI 將即時給出對應翻譯與對話回應。
* **結果確認與反饋**：
  * **正確**：若 AI 的辨識與翻譯無誤，請點擊「直接送出儲存至 Supabase」，將高品質語料寫入資料庫。
  * **錯誤**：若發現辨識不準或翻譯不道地，請選擇「錯誤」，即可手動修正句子文字、上傳/重新錄製正確語音，並送出修正版語料。

**2. manita so tao a miAI (看別人用 AI)**

* **瀏覽社群語料**：您可以在此查看其他使用者產出的對話資料與語音檔，了解 AI 的翻譯品質與應用狀況。
* **社群盲投票與驗證**：
  * 每位登入使用者可針對公開語料進行「正確」或「錯誤」的投票，並留下改善建議。
  * **AI 訓練機制**：當一筆語料累積滿 15 人投票，該語料將自動通過驗證，進入【唯一合規參考語料庫】，成為未來 AI 學習與對照的標準教材。
* **資料管理**：您可以隨時刪除自己所提供的對話資料（管理員可管理全部資料）。

**3. amian so AI ori (關於本站)**

* 提供平台的成立宗旨、更新日誌與使用說明。

歡迎多加利用與分享，共同為達悟語的數位保存與文化傳承盡一份心力！
"""
    )

    st.markdown("---")
    st.subheader("📝 提交網站修訂建議")
    with st.form(key="suggestion_form"):
        user_email_input = st.text_input(
            "您的聯絡信箱（選填）：",
            value=st.session_state.user_info["email"]
            if st.session_state.user_info
            else "",
        )
        suggestion_type = st.selectbox(
            "建議類型：",
            [
                "語料與翻譯建議",
                "功能與介面改善",
                "系統錯誤(Bug)回報",
                "其他",
            ],
        )
        suggestion_text = st.text_area(
            "建議內容：",
            placeholder="請詳細描述您的建議或遇到問題...",
        )

        submit_sug = st.form_submit_button("🚀 送出建議")

        if submit_sug:
            if suggestion_text.strip():
                sug_data = {
                    "user_id": st.session_state.user_info["username"]
                    if st.session_state.user_info
                    else "guest",
                    "email": user_email_input,
                    "category": suggestion_type,
                    "content": suggestion_text,
                    "timestamp": datetime.datetime.now().isoformat(),
                }
                supabase.from_("site_suggestions").insert(sug_data).execute()
                st.success(
                    "🎉 感謝您的寶貴建議！我們將會認真評估並持續改進網站。"
                )
            else:
                st.warning("請輸入建議內容後再送出。")
