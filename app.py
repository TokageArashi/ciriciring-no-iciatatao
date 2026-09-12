import streamlit as st
import sqlite3
import pandas as pd
import google.generativeai as genai
from gtts import gTTS
import os
import json
import time
import datetime
import hashlib
import uuid

# --- 1. Google Drive 永久目錄設定 ---
DRIVE_DIR = '/content/drive/MyDrive/tao_corpus_data'
AUDIO_DIR = os.path.join(DRIVE_DIR, 'audio_files')

# 自動建立持久化儲存目錄
if os.path.exists('/content/drive/MyDrive'):
    os.makedirs(DRIVE_DIR, exist_ok=True)
    os.makedirs(AUDIO_DIR, exist_ok=True)
    DB_NAME = os.path.join(DRIVE_DIR, 'tao_corpus.db')
else:
    # 防呆：若未掛載 Drive 則使用本地檔案
    DB_NAME = 'tao_corpus.db'
    AUDIO_DIR = 'audio_files'
    os.makedirs(AUDIO_DIR, exist_ok=True)

# --- 2. 設定與模型配置 ---
st.set_page_config(page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide")
MODEL_NAME = 'gemini-3.6-flash'

TAO_PHONOLOGY_PROMPT = """
你是一位精通達悟語（Tao / Yami）語言學與正詞法的專家。
請依據達悟語的標準羅馬拼音系統與語音規則進行辨識：
1. 元音（Vowels）：a, e (中元音 /ə/), i, o (或 u)。
2. 子音（Consonants）：b, c (/tɕ/), j (/dʒ/), d (/ɖ/), g, h (/ɰ/), k, m, n, ng (/ŋ/), p, r (ɻ), s (ʂ), t, v (f), w, y (j), z (r), ' (/ʔ/)。
3. 請注意達悟語雙元音與雙子音特徵，輸出標準的達悟語羅馬字。
"""

st.markdown("""
    <style>
    div[role='radiogroup'] label { font-size: 20px !important; font-weight: bold !important; padding: 5px !important; }
    .stButton>button { width: 100%; height: 2.8em; font-size: 18px !important; }
    .stTextInput input { font-size: 18px !important; }
    </style>
""", unsafe_allow_html=True)

st.markdown("""
    <div style='text-align: center; padding-top: 5px; padding-bottom: 15px;'>
        <h1 style='font-size: 42px; font-weight: bold; color: #1E3A8A;'>ciriciring no iciatatao</h1>
        <div style='font-size: 24px; color: #4B5563; font-weight: 600;'>眾語 (雲端永久儲存版)</div>
    </div>
""", unsafe_allow_html=True)

# --- 3. 安全性與資料庫初始化 ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT, email TEXT,
        region TEXT, age_group TEXT, gender TEXT, created_at DATETIME)''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, user_email TEXT, region TEXT, age_group TEXT, gender TEXT,
        q_original TEXT, q_trans TEXT, q_audio_path TEXT,
        r_tao TEXT, r_zh TEXT, r_audio_path TEXT,
        is_edited INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0, is_ready_for_ai INTEGER DEFAULT 0, timestamp DATETIME)''')

    cursor.execute("PRAGMA table_info(feedback)")
    existing_cols = [c[1] for c in cursor.fetchall()]
    required_cols = [
        ("user_id", "TEXT"), ("user_email", "TEXT"), ("region", "TEXT"), ("age_group", "TEXT"), ("gender", "TEXT"),
        ("q_original", "TEXT"), ("q_trans", "TEXT"), ("q_audio_path", "TEXT"),
        ("r_tao", "TEXT"), ("r_zh", "TEXT"), ("r_audio_path", "TEXT"),
        ("is_edited", "INTEGER DEFAULT 0"), ("error_count", "INTEGER DEFAULT 0"),
        ("is_ready_for_ai", "INTEGER DEFAULT 0"), ("timestamp", "DATETIME")
    ]
    for col_name, col_type in required_cols:
        if col_name not in existing_cols:
            cursor.execute(f"ALTER TABLE feedback ADD COLUMN {col_name} {col_type}")

    cursor.execute('''CREATE TABLE IF NOT EXISTS community_votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT, feedback_id INTEGER, voter_id TEXT, region TEXT, age_group TEXT,
        gender TEXT, vote_result TEXT, hidden_suggestion TEXT, timestamp DATETIME,
        UNIQUE(feedback_id, voter_id))''')

    conn.commit()
    conn.close()

init_db()

# --- 4. 帳號與持久化語音處理輔助函數 ---
def add_user(username, password, email, region, age_group, gender):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    try:
        cursor.execute('INSERT INTO users(username, password, email, region, age_group, gender, created_at) VALUES (?,?,?,?,?,?,?)',
                       (username, make_hashes(password), email, region, age_group, gender, datetime.datetime.now()))
        conn.commit()
        conn.close()
        return True
    except sqlite3.IntegrityError:
        conn.close()
        return False

def login_user(username, password):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('SELECT username, password, email, region, age_group, gender FROM users WHERE username =? AND password = ?',
                   (username, make_hashes(password)))
    data = cursor.fetchall()
    conn.close()
    return data

def save_audio_bytes_permanently(audio_bytes, suffix=".wav"):
    """將語音二進位檔案永久儲存至 Google Drive 目錄"""
    if not audio_bytes: return None
    try:
        file_name = f"audio_{uuid.uuid4().hex[:10]}{suffix}"
        file_path = os.path.join(AUDIO_DIR, file_name)

        with open(file_path, "wb") as f:
            if hasattr(audio_bytes, 'seek'):
                audio_bytes.seek(0)
            if hasattr(audio_bytes, 'read'):
                f.write(audio_bytes.read())
            else:
                f.write(audio_bytes)
        return file_path
    except Exception as e:
        st.error(f"儲存語音檔失敗: {e}")
        return None

def generate_tts_audio(text):
    if not text: return None
    try:
        tts = gTTS(text=text, lang='id', slow=False)
        file_name = f"tts_{uuid.uuid4().hex[:10]}.mp3"
        file_path = os.path.join(AUDIO_DIR, file_name)
        tts.save(file_path)
        return file_path
    except Exception:
        return None

# --- 5. AI 處理函數 ---
def process_ai_input(text_prompt=None, audio_file=None):
    # 1. 讀取 API Key
    api_key = st.secrets.get("GOOGLE_API_KEY") or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        st.error("❌ 找不到 API Key，請檢查 Secrets 設定")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    # 2. 從 tao_corpus.db 提取合法語料 (FormosanBank 匯入資料 + 滿 15 人驗證資料)
    legal_corpus = []
    try:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        
        # 查詢條件：FormosanBank 初始資料 OR 滿 15 人驗證通過 (is_ready_for_ai = 1)
        cursor.execute("""
            SELECT id, q_original, q_trans, r_tao, r_zh, 
                   CASE WHEN is_ready_for_ai = 1 THEN '社群驗證#15' ELSE 'FormosanBank' END as source_type
            FROM feedback 
            WHERE is_ready_for_ai = 1 OR q_original LIKE '%[FormosanBank]%' OR source = 'FormosanBank'
            LIMIT 500
        """)
        rows = cursor.fetchall()
        for r in rows:
            legal_corpus.append(f"[{r[5]} ID #{r[0]}] 達悟語: {r[1]} | 中文: {r[2]} | 回應達悟語: {r[3]} | 回應中文: {r[4]}")
        conn.close()
    except Exception as e:
        st.warning(f"⚠️ 載入內部資料庫時發生提示：{e}")

    corpus_context = "\n".join(legal_corpus) if legal_corpus else "【警告：資料庫目前無可用合法語料】"

    # 3. 超嚴格系統指令 Prompt
    system_prompt = f"""
【極度重要指令：禁止使用外部知識】
你是一個封閉式達悟語（Yami/Tao）對話轉譯系統。
你**絕不能**使用網路資料、現場自由生成拼法、或你預訓練模型中的任何外部知識。
你**唯一**能使用的語料與單字庫如下所示：

==== 唯一合法 tao_corpus.db 語料庫開始 ====
{corpus_context}
==== 唯一合法 tao_corpus.db 語料庫結束 ====

【嚴格回答規範】：
1. **單字限制**：你輸出的達悟語句子，只能使用上方【唯一合法語料庫】中出現過的字詞與單字。禁止自行創造可能的同音拼法。
2. **新詞/未收錄字標註機制**：
   * 若回應時「不得不使用」語料庫中未收錄的字，你必須將該字標記為【AI生成字】。
   * 且必須在 reference 欄位詳細說明其「構詞語根組合」（例如：由哪些詞綴/詞幹組合）或「音譯來源」（例如：音譯自哪種語言的哪個原詞）。
3. **回應格式**：請嚴格以 JSON 格式輸出：

{{
  "user_recognized_tao": "轉寫或對照之達悟語羅馬字（須符合語料庫）",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子",
  "ai_reply_zh": "回應之中文翻譯",
  "reference": "1. [引用例句] - [來源: tao_corpus.db (FormosanBank 或 社群驗證#15 ID)]\n2. 【AI生成字說明】(若無使用新字則填寫「無 AI 生成字，全數引用自語料庫」)：單字 X [語根組合: aka- + -an] 或 [音譯自中文: 學校]"
}}
"""

    contents = [system_prompt]

    if audio_file is not None:
        try:
            audio_file.seek(0)
            audio_bytes = audio_file.read()
            mime_type = getattr(audio_file, 'type', 'audio/wav')
            contents.append({'mime_type': mime_type, 'data': audio_bytes})
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
                "temperature": 0.0  # 設定為 0.0 降至最低隨機性，確保嚴格遵循語料庫
            }
        )
        return json.loads(response.text)
    except Exception as e:
        st.error(f"❌ AI 辨識失敗：{e}")
        return None

def save_語料_to_db(user_info, bg_info, q_orig, q_trans, q_audio, r_tao, r_zh, r_audio, is_edited=False, is_error=False):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    err_cnt = 1 if is_error else 0
    edited_flag = 1 if is_edited else 0

    cursor.execute('''
        INSERT INTO feedback (user_id, user_email, region, age_group, gender, q_original, q_trans, q_audio_path, r_tao, r_zh, r_audio_path, is_edited, error_count, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_info["username"], user_info["email"], bg_info["region"], bg_info["age_group"], bg_info["gender"],
          q_orig, q_trans, q_audio, r_tao, r_zh, r_audio, edited_flag, err_cnt, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    conn.commit()
    conn.close()

# --- 6. 側邊欄：登入與註冊 ---
if "user_info" not in st.session_state:
    st.session_state.user_info = None

with st.sidebar:
    st.title("👤 會員中心")
    st.caption(f"📁 資料保存路徑：\n`{DB_NAME}`")
    if st.session_state.user_info is None:
        auth_choice = st.radio("請選擇：", ["登入", "註冊帳號"])
        if auth_choice == "登入":
            login_username = st.text_input("帳號")
            login_password = st.text_input("密碼", type='password')
            if st.button("登入"):
                res = login_user(login_username, login_password)
                if res:
                    st.session_state.user_info = {"username": res[0][0], "email": res[0][2], "region": res[0][3], "age_group": res[0][4], "gender": res[0][5]}
                    st.rerun()
                else:
                    st.error("帳號或密碼錯誤。")
        else:
            new_u = st.text_input("設定帳號")
            new_p = st.text_input("設定密碼", type='password')
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

# --- 7. 主頁面 ---
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
        with c1: cur_r = st.selectbox("部落：", r_list, index=r_list.index(user['region']) if user['region'] in r_list else 0)
        with c2: cur_a = st.selectbox("年齡：", a_list, index=a_list.index(user['age_group']) if user['age_group'] in a_list else 3)
        with c3: cur_g = st.selectbox("性別：", g_list, index=g_list.index(user['gender']) if user['gender'] in g_list else 0)
        current_bg = {"region": cur_r, "age_group": cur_a, "gender": cur_g}

        st.divider()

        input_type = st.radio("請選擇輸入方式：", ["🎤 達悟語語音輸入", "⌨️ 文字輸入"], horizontal=True)

        if input_type == "🎤 達悟語語音輸入":
            st.caption("請點擊下方麥克風圖示開始錄音，完成後停止即可自動辨識：")
            voice_input = st.audio_input("點擊麥克風開始錄音", key="voice_input_main")

            if voice_input is not None:
                if st.session_state.get("last_processed_audio") != voice_input:
                    with st.spinner("⏳ 正在聽取語音並依達悟語音系轉寫中..."):
                        user_audio_path = save_audio_bytes_permanently(voice_input)
                        voice_input.seek(0)
                        ai_data = process_ai_input(audio_file=voice_input)

                        if ai_data:
                            st.session_state.active_q = ai_data.get("user_recognized_tao", "語音輸入")
                            st.session_state.ai_data = ai_data
                            st.session_state.user_audio_path = user_audio_path
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
                            st.session_state.user_audio_path = None
                            st.session_state.active_bg = current_bg

        # 顯示 AI 產出結果與評估介面
# 顯示 AI 產出結果與評估介面
        if "ai_data" in st.session_state and st.session_state.ai_data:
            ai_data = st.session_state.ai_data
            q_orig = st.session_state.get('active_q', '')
            bg_info = st.session_state.get("active_bg", current_bg)
            input_audio_saved = st.session_state.get("user_audio_path", None)
            
            st.markdown("---")
            st.info(f"**【句子 1 - 輸入與辨識】**\n* 辨識/原文：{ai_data.get('user_recognized_tao', q_orig)}\n* 翻譯：{ai_data.get('user_translation', '')}")
            if input_audio_saved and os.path.exists(input_audio_saved):
                st.write("🔊 **輸入的達悟語原音：**")
                st.audio(input_audio_saved)

            st.success(f"**【句子 2 - AI 對話回答】**\n* 達悟語：{ai_data.get('ai_reply_tao', '')}\n* 中文對照：{ai_data.get('ai_reply_zh', '')}")

            # 💡 新增：顯示參考資料區塊
            ref_info = ai_data.get('reference', '尚無標註參考來源')
            st.warning(f"📚 **【語料參考資料出處】**\n{ref_info}")

            r_tts = generate_tts_audio(ai_data.get('ai_reply_tao', ''))
            if r_tts:
                st.write("🔊 **AI 回答語音預覽：**")
                st.audio(r_tts, format='audio/mp3')

            st.divider()
            st.subheader("📝 語料品質評估與覆蓋錄音")
            eval_choice = st.radio("這組 AI 辨識、翻譯與對答是否正確？", ["正確", "錯誤"], horizontal=True)

            final_q_audio_default = input_audio_saved or generate_tts_audio(ai_data.get('user_recognized_tao', q_orig))

            if eval_choice == "正確":
                if st.button("✅ 直接送出儲存"):
                    save_語料_to_db(st.session_state.user_info, bg_info, ai_data.get('user_recognized_tao', q_orig), ai_data.get('user_translation'), final_q_audio_default, ai_data.get('ai_reply_tao'), ai_data.get('ai_reply_zh'), r_tts, is_edited=False, is_error=False)
                    st.success("🎉 語料與語音檔已順利儲存至 Google Drive！")
                    del st.session_state.ai_data

            elif eval_choice == "錯誤":
                st.warning("⚠️ 發現錯誤。您可以直接修正文字，或重新錄製語音覆蓋原始音檔：")

                e_q_tao = st.text_input("修改【句子 1 達悟語羅馬字】：", value=ai_data.get('user_recognized_tao', q_orig))
                e_q_trans = st.text_input("修改【句子 1 中文翻譯】：", value=ai_data.get('user_translation', ''))
                e_r_tao = st.text_input("修改【句子 2 達悟語回答】：", value=ai_data.get('ai_reply_tao', ''))
                e_r_zh = st.text_input("修改【句子 2 中文對照】：", value=ai_data.get('ai_reply_zh', ''))

                st.markdown("🎙️ **重錄/覆蓋語音檔 (選填，未錄製則保留原音檔)：**")
                re_audio_q = st.audio_input("重新錄製【句子 1 語音】(覆蓋原音)", key="re_q")
                re_audio_r = st.audio_input("重新錄製【句子 2 語音】(覆蓋 AI 音)", key="re_r")

                col_e1, col_e2 = st.columns(2)
                if col_e1.button("🚫 本次不編輯 (記為錯誤)"):
                    save_語料_to_db(st.session_state.user_info, bg_info, ai_data.get('user_recognized_tao', q_orig), ai_data.get('user_translation'), final_q_audio_default, ai_data.get('ai_reply_tao'), ai_data.get('ai_reply_zh'), r_tts, is_edited=False, is_error=True)
                    st.error("已記錄此語料為「錯誤」次數 +1！")
                    del st.session_state.ai_data

                if col_e2.button("💾 編輯完成 (送出修正版)"):
                    new_q_path = save_audio_bytes_permanently(re_audio_q) if re_audio_q else final_q_audio_default
                    new_r_path = save_audio_bytes_permanently(re_audio_r) if re_audio_r else r_tts

                    save_語料_to_db(st.session_state.user_info, bg_info, e_q_tao, e_q_trans, new_q_path, e_r_tao, e_r_zh, new_r_path, is_edited=True, is_error=False)
                    st.success("🎉 修正版文字與覆蓋語音檔已順利儲存至 Google Drive！")
                    del st.session_state.ai_data

elif main_menu == "看別人用AI":
    st.subheader("📖 社群公開語料審查與盲投票")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM feedback ORDER BY id DESC", conn)
    conn.close()

    if df.empty:
        st.info("目前尚無對話語料。")
    else:
        current_voter = st.session_state.user_info["username"] if st.session_state.user_info else "guest"

        for _, row in df.iterrows():
            f_id = row['id']
            status_tag = "✏️ 經修訂" if row['is_edited'] else ("❌ 含有錯" if row['error_count'] > 0 else "✅ 原始產出")

            with st.expander(f"💬 對話 #{f_id} | 來源部落：{row['region']} | 狀態：{status_tag}"):
                st.markdown("**【句子 1 - 輸入與翻譯】**")
                st.write(f"1. 達悟語：{row['q_original']}")
                st.write(f"2. 翻譯：{row['q_trans']}")
                if row['q_audio_path'] and os.path.exists(row['q_audio_path']):
                    st.audio(row['q_audio_path'])

                st.markdown("**【句子 2 - 對答與翻譯】**")
                st.write(f"3. 達悟語：{row['r_tao']}")
                st.write(f"4. 中文對照：{row['r_zh']}")
                if row['r_audio_path'] and os.path.exists(row['r_audio_path']):
                    st.audio(row['r_audio_path'])

                st.divider()

                conn = sqlite3.connect(DB_NAME)
                v_check = pd.read_sql_query("SELECT * FROM community_votes WHERE feedback_id = ? AND voter_id = ?", conn, params=(f_id, current_voter))
                all_votes = pd.read_sql_query("SELECT * FROM community_votes WHERE feedback_id = ?", conn, params=(f_id,))
                conn.close()

                vote_count = len(all_votes)

                if vote_count >= 15:
                    correct_pct = (all_votes['vote_result'] == '正確').mean() * 100
                    st.success(f"📊 社群盲投票結果 (已滿 15 人，共 {vote_count} 票)：正確率 {correct_pct:.1f}%")

                    if correct_pct >= 80.0 and row['is_ready_for_ai'] == 0:
                        conn = sqlite3.connect(DB_NAME)
                        conn.execute("UPDATE feedback SET is_ready_for_ai = 1 WHERE id = ?", (f_id,))
                        conn.commit()
                        conn.close()
                else:
                    st.info(f"🔒 社群盲投票進行中：目前累積 {vote_count}/15 票（滿 15 票公開統計並評估納入 AI 學習）。")

                if st.session_state.user_info is None:
                    st.caption("🔒 請先登入帳號以參與投票。")
                elif not v_check.empty:
                    st.warning(f"🖐️ 您已投票過（您的選擇：{v_check.iloc[0]['vote_result']}）。感謝您的協助！")
                else:
                    st.markdown("##### 🗳️ 我要投票與給予建議")
                    vote_opt = st.radio(f"您認為對話 #{f_id} 是否道地正確？", ["正確", "錯誤"], key=f"v_opt_{f_id}", horizontal=True)
                    hidden_sug = st.text_area("改善建議 (僅供學者與 AI 學習對照，不會對外公開)：", key=f"sug_{f_id}")

                    if st.button("送出投票", key=f"v_btn_{f_id}"):
                        voter = st.session_state.user_info
                        conn = sqlite3.connect(DB_NAME)
                        cursor = conn.cursor()
                        cursor.execute('''
                            INSERT INTO community_votes (feedback_id, voter_id, region, age_group, gender, vote_result, hidden_suggestion, timestamp)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        ''', (f_id, voter['username'], voter['region'], voter['age_group'], voter['gender'], vote_opt, hidden_sug, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
                        conn.commit()
                        conn.close()
                        st.success("🎉 投票成功！感謝您為達悟語資料庫貢獻一份力量。")
                        st.rerun()

elif main_menu == "關於本站":
    st.write("### 蘭嶼在地化語言學習與語料採集平台")
    st.write("提供達悟語雙向翻譯、AI 對答、真人/TTS 雙語語音錄製與社群盲投票機制。")
