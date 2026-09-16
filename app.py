import datetime
import hashlib
import io
import json
import os
import uuid

import google.generativeai as genai
from gtts import gTTS
import pandas as pd
import streamlit as st
from supabase import create_client, Client

# --- 1. 全域設定與 Supabase 連線 ---
MODEL_NAME = "gemini-3.6-flash"

st.set_page_config(
    page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide"
)

st.markdown(
    """
    <style>
    div[role='radiogroup'] label { font-size: 20px !important; font-weight: bold !important; padding: 5px !important; }
    .stButton>button { width: 100%; height: 2.8em; font-size: 18px !important; }
    .stTextInput input { font-size: 18px !important; }
    </style>
""",
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div style='text-align: center; padding-top: 5px; padding-bottom: 15px;'>
        <h1 style='font-size: 42px; font-weight: bold; color: #1E3A8A;'>ciriciring no iciatatao</h1>
        <div style='font-size: 24px; color: #4B5563; font-weight: 600;'>眾語 (Supabase 雲端版)</div>
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
            "created_at": datetime.datetime.now().isoformat()
        }
        supabase.from_("users").insert(data).execute()
        return True
    except Exception as e:
        st.error(f"註冊失敗：{e}")
        return False

def login_user(username, password):
    try:
        res = supabase.from_("users").select("*").eq("username", username).eq("password", make_hashes(password)).execute()
        return res.data
    except Exception as e:
        st.error(f"登入查詢失敗：{e}")
        return []

# --- 3. Supabase 資料庫讀寫函式 ---
def save_語料_to_supabase(
    user_info, bg_info, q_orig, q_trans, q_audio_url,
    r_tao, r_zh, r_audio_url, is_edited=False, is_error=False
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
            "q_audio_url": q_audio_url,
            "r_tao": r_tao,
            "r_zh": r_zh,
            "r_audio_url": r_audio_url,
            "is_edited": 1 if is_edited else 0,
            "error_count": 1 if is_error else 0,
            "timestamp": datetime.datetime.now().isoformat()
        }
        supabase.from_("feedback").insert(data).execute()
        return True
    except Exception as e:
        st.error(f"寫入 Supabase 失敗：{e}")
        return False

def delete_feedback_item(feedback_id, current_username):
    try:
        res = supabase.from_("feedback").select("user_id").eq("id", feedback_id).execute()
        if not res.data:
            return False, "找不到該筆資料。"
        
        owner_id = res.data[0]["user_id"]
        if current_username != "admin" and owner_id != current_username:
            return False, "權限不足：您只能刪除自己提供的資料！"

        # 刪除關聯投票與語料
        supabase.from_("community_votes").delete().eq("feedback_id", feedback_id).execute()
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

# --- 5. AI 處理邏輯 (自 Supabase 提取語料庫) ---
def process_ai_input(text_prompt=None, audio_file=None):
    api_key = st.secrets.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        st.error("❌ 找不到 GOOGLE_API_KEY，請檢查 Secrets 設定")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    legal_corpus = []
    
    # 1. 從 Supabase 讀取基礎語料庫 (corpus 表)
    try:
        res_corpus = supabase.from_("corpus").select("*").execute()
        for r in res_corpus.data:
            legal_corpus.append(
                f"[基礎語料 ID #{r.get('id')}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('r_tao')} | 回應中文: {r.get('r_zh')}"
            )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取 corpus 語料庫提示：{e}")

    # 2. 從 Supabase 讀取社群驗證通過的語料 (feedback 表且 is_ready_for_ai = 1)
    try:
        res_feedback = supabase.from_("feedback").select("*").eq("is_ready_for_ai", 1).execute()
        for r in res_feedback.data:
            legal_corpus.append(
                f"[社群驗證 ID #{r.get('id')}] 達悟語: {r.get('q_original')} | 中文: {r.get('q_trans')} | 回應達悟語: {r.get('r_tao')} | 回應中文: {r.get('r_zh')}"
            )
    except Exception as e:
        st.warning(f"⚠️ 從 Supabase 讀取 feedback 語料庫提示：{e}")

    corpus_context = "\n".join(legal_corpus) if legal_corpus else "【警告：Supabase 目前無可用合法語料】"

# --- 修改後的 AI  Prompt 指示 ---
　　system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。

【唯一合法參考語料庫】:
　　{corpus_context}

【檢索與生成規則】：
1. **單字與詞組拆解**：請將使用者輸入的句子拆解為單字或短語，並在【唯一合法參考語料庫】中搜尋包含這些單字/短語的例句與詞彙。
2. **組合回答**：即使沒有一模一樣的整句例句，只要語料庫中包含該句子相關的單字、詞組或語法結構，請協助進行組合與翻譯。
3. **邊界限制**：若必須使用語料庫中沒有的單字，必須標示為AI生成字，依照達悟語之造詞規則用已有之語根組合造字，或者使用音譯，並標示造詞使用之詞根或音譯自何種語言。
4. **讀取原則**：若使用者使用語料庫中沒有收錄的單字，回答時亦可使用該字，然必須標註為「使用者推薦字」

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的達悟語或對照羅馬字",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子",
  "ai_reply_zh": "回應之中文翻譯",
  "reference": "引用之語料 ID 或單字來源"
}}
"""

　　contents = [system_prompt]

    if audio_file is not None:
        try:
            audio_bytes = get_bytes_from_input(audio_file)
            mime_type = getattr(audio_file, "type", "audio/wav")
            contents.append({"mime_type": mime_type, "data": audio_bytes})
            contents.append("請對照【唯一合法語料庫】進行語音轉寫與回應，絕不使用庫外單字。")
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
    st.title("👤 會員中心")
    st.caption("☁️ 連線模式：Supabase 雲端資料庫")
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
            new_r = st.selectbox("部落：", ["椰油", "朗島", "東清", "野銀", "紅頭", "漁人", "巴丹", "台灣", "其他"])
            new_a = st.selectbox("年齡：", ["學齡前", "國小", "國高中", "18-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81歲及以上"])
            new_g = st.selectbox("性別：", ["男", "女", "不公開"])
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
main_menu = st.radio("", ["我要用AI", "看別人用AI", "關於本站"], horizontal=True)

if main_menu == "我要用AI":
    st.subheader("💬 AI 對話與語音語料採集")
    if st.session_state.user_info is None:
        st.warning("🔒 本系統需登入後使用，請先在左側邊欄登入。")
    else:
        user = st.session_state.user_info
        st.markdown("##### 📱 本次輸入者設定")
        r_list = ["椰油", "朗島", "東清", "野銀", "紅頭", "漁人", "巴丹", "台灣", "其他"]
        a_list = ["學齡前", "國小", "國高中", "18-30", "31-40", "41-50", "51-60", "61-70", "71-80", "81歲及以上"]
        g_list = ["男", "女", "不公開"]

        c1, c2, c3 = st.columns(3)
        with c1:
            cur_r = st.selectbox("部落：", r_list, index=r_list.index(user["region"]) if user["region"] in r_list else 0)
        with c2:
            cur_a = st.selectbox("年齡：", a_list, index=a_list.index(user["age_group"]) if user["age_group"] in a_list else 3)
        with c3:
            cur_g = st.selectbox("性別：", g_list, index=g_list.index(user["gender"]) if user["gender"] in g_list else 0)
        current_bg = {"region": cur_r, "age_group": cur_a, "gender": cur_g}

        st.divider()

        input_type = st.radio("請選擇輸入方式：", ["🎤 達悟語語音輸入", "⌨️ 文字輸入"], horizontal=True)

        if input_type == "🎤 達悟語語音輸入":
            st.caption("請點擊下方麥克風圖示開始錄音，完成後停止即可自動辨識：")
            voice_input = st.audio_input("點擊麥克風開始錄音", key="voice_input_main")

            if voice_input is not None:
                if st.session_state.get("last_processed_audio") != voice_input:
                    with st.spinner("⏳ 正在聽取語音並依達悟語音系轉寫中..."):
                        audio_bytes = get_bytes_from_input(voice_input)
                        ai_data = process_ai_input(audio_file=voice_input)

                        if ai_data:
                            st.session_state.active_q = ai_data.get("user_recognized_tao", "語音輸入")
                            st.session_state.ai_data = ai_data
                            st.session_state.user_audio_bytes = audio_bytes
                            st.session_state.active_bg = current_bg
                            st.session_state.last_processed_audio = voice_input
                            st.rerun()

        else:
            text_input = st.text_input("請輸入問題或句子：", placeholder="例如：Akokay 或 今天天氣如何？")
            if st.button("🚀 發送文字詢問"):
                if text_input.strip():
                    with st.spinner("⏳ AI 思考中..."):
                        ai_data = process_ai_input(text_prompt=text_input)
                        if ai_data:
                            st.session_state.active_q = text_input
                            st.session_state.ai_data = ai_data
                            st.session_state.user_audio_bytes = None
                            st.session_state.active_bg = current_bg

        if "ai_data" in st.session_state and st.session_state.ai_data:
            ai_data = st.session_state.ai_data
            q_orig = st.session_state.get("active_q", "")
            bg_info = st.session_state.get("active_bg", current_bg)

            st.markdown("---")
            st.info(
                f"**【句子 1 - 輸入與辨識】**\n* 辨識/原文：{ai_data.get('user_recognized_tao', q_orig)}\n* 翻譯：{ai_data.get('user_translation', '')}"
            )
            st.success(
                f"**【句子 2 - AI 對話回答】**\n* 達悟語：{ai_data.get('ai_reply_tao', '')}\n* 中文對照：{ai_data.get('ai_reply_zh', '')}"
            )

            st.divider()
            st.subheader("📝 語料品質評估")
            eval_choice = st.radio("這組 AI 辨識與翻譯是否正確？", ["正確", "錯誤"], horizontal=True)

            if eval_choice == "正確":
                if st.button("✅ 直接送出儲存至 Supabase"):
                    if save_語料_to_supabase(
                        st.session_state.user_info, bg_info,
                        ai_data.get("user_recognized_tao", q_orig), ai_data.get("user_translation"), None,
                        ai_data.get("ai_reply_tao"), ai_data.get("ai_reply_zh"), None,
                        is_edited=False, is_error=False
                    ):
                        st.success("🎉 語料已成功寫入 Supabase 雲端資料庫！")
                        del st.session_state.ai_data
                        st.rerun()

            elif eval_choice == "錯誤":
                st.warning("⚠️ 發現錯誤。您可以直接修正文字：")
                e_q_tao = st.text_input("修改【句子 1 達悟語】：", value=ai_data.get("user_recognized_tao", q_orig))
                e_q_trans = st.text_input("修改【句子 1 翻譯】：", value=ai_data.get("user_translation", ""))
                e_r_tao = st.text_input("修改【句子 2 達悟語】：", value=ai_data.get("ai_reply_tao", ""))
                e_r_zh = st.text_input("修改【句子 2 翻譯】：", value=ai_data.get("ai_reply_zh", ""))

                if st.button("💾 儲存修正版至 Supabase"):
                    if save_語料_to_supabase(
                        st.session_state.user_info, bg_info,
                        e_q_tao, e_q_trans, None,
                        e_r_tao, e_r_zh, None,
                        is_edited=True, is_error=False
                    ):
                        st.success("🎉 修正版語料已成功儲存至 Supabase！")
                        del st.session_state.ai_data
                        st.rerun()

elif main_menu == "看別人用AI":
    st.subheader("📖 社群公開語料審查與盲投票")
    
    # 從 Supabase 提取全部 feedback 語料
    try:
        res = supabase.from_("feedback").select("*").order("id", desc=True).execute()
        rows = res.data
    except Exception as e:
        st.error(f"無法從 Supabase 讀取資料：{e}")
        rows = []

    if not rows:
        st.info("目前 Supabase 中尚無語料資料。")
    else:
        current_username = st.session_state.user_info["username"] if st.session_state.user_info else None

        for row in rows:
            f_id = row["id"]
            owner_id = row.get("user_id", "匿名")
            region = row.get("region", "未知")
            is_edited = row.get("is_edited", 0)
            error_count = row.get("error_count", 0)
            status_tag = "✏️ 經修訂" if is_edited else ("❌ 含有錯" if error_count > 0 else "✅ 原始產出")

            with st.expander(f"💬 對話 #{f_id} | 上傳者：{owner_id} | 部落：{region} | 狀態：{status_tag}"):
                st.markdown("**【句子 1 - 輸入與翻譯】**")
                st.write(f"1. 達悟語：{row.get('q_original')}")
                st.write(f"2. 翻譯：{row.get('q_trans')}")

                st.markdown("**【句子 2 - 對答與翻譯】**")
                st.write(f"3. 達悟語：{row.get('r_tao')}")
                st.write(f"4. 中文對照：{row.get('r_zh')}")

                st.divider()

                # 刪除按鈕權限檢查
                if current_username and (current_username == owner_id or current_username == "admin"):
                    if st.button(f"🗑️ 刪除此筆資料 (ID #{f_id})", key=f"del_{f_id}"):
                        success, msg = delete_feedback_item(f_id, current_username)
                        if success:
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                    st.divider()

                # 查詢投票狀態
                votes_res = supabase.from_("community_votes").select("*").eq("feedback_id", f_id).execute()
                all_votes = votes_res.data
                vote_count = len(all_votes)

                user_voted = any(v["voter_id"] == current_username for v in all_votes) if current_username else False

                if vote_count >= 15:
                    correct_votes = sum(1 for v in all_votes if v.get("vote_result") == "正確")
                    correct_pct = (correct_votes / vote_count) * 100
                    st.success(f"📊 社群盲投票結果 (已滿 15 人)：正確率 {correct_pct:.1f}%")

                    if correct_pct >= 80.0 and row.get("is_ready_for_ai") != 1:
                        supabase.from_("feedback").update({"is_ready_for_ai": 1}).eq("id", f_id).execute()
                else:
                    st.info(f"🔒 社群盲投票進行中：目前累積 {vote_count}/15 票。")

                if st.session_state.user_info is None:
                    st.caption("🔒 請先登入帳號以參與投票。")
                elif user_voted:
                    st.warning("🖐️ 您已參與過此筆語料的投票，感謝協助！")
                else:
                    st.markdown("##### 🗳️ 我要投票")
                    vote_opt = st.radio(f"您認為對話 #{f_id} 是否道地正確？", ["正確", "錯誤"], key=f"v_opt_{f_id}", horizontal=True)
                    hidden_sug = st.text_area("改善建議 (僅供 AI 學習對照)：", key=f"sug_{f_id}")

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
                            "timestamp": datetime.datetime.now().isoformat()
                        }
                        supabase.from_("community_votes").insert(v_data).execute()
                        st.success("🎉 投票已送出！")
                        st.rerun()

elif main_menu == "關於本站":
    st.write("### 蘭嶼在地化語言學習與語料採集平台 (Supabase 版)")
    st.write("提供達悟語雙向翻譯、AI 對答、語料採集與 Supabase 雲端盲投票機制。")
