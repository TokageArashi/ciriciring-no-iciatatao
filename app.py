import datetime
import difflib
import hashlib
import html
import json
import os
import re
import time
import unicodedata
import uuid

from google.api_core.exceptions import ResourceExhausted
import google.generativeai as genai
import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from supabase import Client, create_client


# --- 輔助函式：上下雙語排版用 ---
def dual_text(tao: str, zh: str, is_title: bool = False) -> str:
    size_tao = "22px" if is_title else "18px"
    size_zh = "16px" if is_title else "14px"
    return f"""
    <div style="margin-bottom: 8px;">
        <div style="font-size: {size_tao}; font-weight: bold; color: var(--text-color, #F3F4F6); line-height: 1.3;">{tao}</div>
        <div style="font-size: {size_zh}; color: var(--secondary-text-color, #9CA3AF); line-height: 1.2;">{zh}</div>
    </div>
    """


def lower_first_char(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:]


def detect_input_origin(text: str) -> str:
    """
    依文字與文法特徵判斷原始輸入是中文還是達悟文：
    - 'zh'：大多數為漢字且文法可辨識為中文（例如中文句子混合 AI 等羅馬字）
    - 'tao'：主要使用羅馬字且文法特徵符合達悟語
    """
    s = (text or "").strip()
    if not s:
        return "tao"

    cjk_count = len(re.findall(r"[\u4e00-\u9fff\u3400-\u4dbf]", s))
    latin_count = len(re.findall(r"[A-Za-z]", s))

    tao_grammar_words = {
        "a", "am", "no", "do", "o", "kano", "ji", "ya", "ko", "mo", "nia",
        "nire", "ikongo", "akokay", "ayoy", "ciring", "tao", "sirisiring", "vahay",
    }

    tokens = [t.strip(".,!?;:\"“”()[]「」『』。，！？；：、…—–'").lower() for t in s.split()]
    tao_grammar_hits = sum(1 for t in tokens if t in tao_grammar_words)

    if cjk_count > latin_count or (cjk_count > 0 and tao_grammar_hits == 0):
        return "zh"

    return "tao"


def extract_romanized_tokens(text: str) -> list[str]:
    """擷取輸入文字中的羅馬字／英文字詞（保留原始大小寫與順序，去重）。"""
    seen = set()
    tokens = []
    for m in re.findall(r"[A-Za-z][A-Za-z0-9'’ʼʻ-]*", text or ""):
        key = m.lower()
        if key not in seen:
            seen.add(key)
            tokens.append(m)
    return tokens


def preserve_romanized_tokens(tao_text: str, source_text: str) -> str:
    """
    當使用者輸入含羅馬字（例如中文混用 AI）時，確保達悟文句子中對應的字
    保留使用者原始輸入的羅馬字拼法（包含大小寫，如 AI）。
    """
    if not tao_text or not source_text:
        return tao_text
    orig_tokens = extract_romanized_tokens(source_text)
    if not orig_tokens:
        return tao_text

    by_lower = {t.lower(): t for t in orig_tokens}
    result = tao_text
    for lower_tok, exact_tok in by_lower.items():
        pattern = re.compile(rf"\b{re.escape(lower_tok)}\b", flags=re.IGNORECASE)
        result = pattern.sub(exact_tok, result)
    return result


# --- 1. 全域設定與 Supabase 連線 ---
MODEL_NAME = "gemini-3.6-flash"
PRIVATE_TABLE = "private_feedback"
LOCAL_PRIVATE_DIR = ".private_user_logs"
PHRASE_STORE_FILE = ".phrases_store.json"
AUTO_LOGIN_FILE = ".auto_login_tokens.json"
WORD_DETAIL_STORE_FILE = ".word_details_store.json"

CORPUS_TAO_COLS = ["tao", "tao_text", "yami", "yami_text", "text_tao", "ciriciring", "sentence_tao", "sentence"]
CORPUS_ZH_COLS = ["zh", "zh_text", "chinese", "chinese_text", "text_zh", "translation", "meaning", "sentence_zh"]

st.set_page_config(
    page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide"
)

if "user_info" not in st.session_state:
    st.session_state.user_info = None
if "local_private_cache" not in st.session_state:
    st.session_state.local_private_cache = {}
if "auto_login_checked" not in st.session_state:
    st.session_state.auto_login_checked = False
if "per_sentence_disabled_phrases" not in st.session_state:
    st.session_state.per_sentence_disabled_phrases = {}
if "pending_session_votes" not in st.session_state:
    st.session_state.pending_session_votes = []

# 自動關閉拼字檢查 + 片語滑鼠懸停同步黑底線
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

        function bindPhraseHoverUnderline() {
            const doc = parent.document;
            const phraseEls = doc.querySelectorAll('[data-phrase-group]');
            phraseEls.forEach(el => {
                if (el.dataset.phraseHoverBound === '1') return;
                el.dataset.phraseHoverBound = '1';
                const grp = el.getAttribute('data-phrase-group');
                el.addEventListener('mouseenter', () => {
                    doc.querySelectorAll(`[data-phrase-group="${grp}"]`).forEach(target => {
                        target.classList.add('phrase-hover-active');
                    });
                });
                el.addEventListener('mouseleave', () => {
                    doc.querySelectorAll(`[data-phrase-group="${grp}"]`).forEach(target => {
                        target.classList.remove('phrase-hover-active');
                    });
                });
            });
        }

        disableSpellcheck();
        bindPhraseHoverUnderline();
        const observer = new MutationObserver(() => {
            disableSpellcheck();
            bindPhraseHoverUnderline();
        });
        observer.observe(parent.document.body, { childList: true, subtree: true });
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
    input, textarea { spellcheck: false !important; -webkit-spellcheck: false !important; }
    
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) {
        display: flex !important;
        flex-direction: row !important;
        flex-wrap: wrap !important;
        align-items: baseline !important;
        gap: 0 !important;
        width: 100% !important;
    }
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) > :is(.element-container, [data-testid="stElementContainer"]) {
        width: auto !important;
        flex: 0 0 auto !important;
        min-width: 0 !important;
        margin: 0 0 0 0.3em !important;
    }
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) > :is(.element-container, [data-testid="stElementContainer"]):first-child { margin-left: 0 !important; }
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) > :is(.element-container, [data-testid="stElementContainer"]):has(.tao-punct) { margin-left: 0 !important; }
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) > :is(.element-container, [data-testid="stElementContainer"]):has(.tao-lead) + * { margin-left: 0 !important; }
    :is(div[class*="st-key-sent_"], div[class*="st-key-sent_"] > [data-testid="stVerticalBlock"]) > :is(.element-container, [data-testid="stElementContainer"]) [data-testid="stMarkdownContainer"] p { margin: 0 !important; }
    .tao-w, .tao-punct, .tao-lead { font-size: 20px; font-weight: bold; line-height: 2.0; }

    /* 黑色一般詞彙也可點擊查看單字詳情 */
    [class*="_hlK_"] button {
        border: none !important;
        box-shadow: none !important;
        background-color: transparent !important;
        color: inherit !important;
        height: auto !important;
        min-height: unset !important;
        padding: 0 0.15em !important;
        line-height: 1.6 !important;
        font-size: 20px !important;
        font-weight: bold !important;
        border-radius: 4px !important;
        cursor: pointer;
    }
    [class*="_hlK_"] button p { font-size: 20px !important; margin: 0 !important; }
    [class*="_hlK_"] button svg, [class*="_hlK_"] button [data-testid="stIconMaterial"] { display: none !important; }
    [class*="_hlK_"] button:hover {
        background-color: rgba(17, 24, 39, 0.08) !important;
        text-decoration: underline solid #111827 2px;
        text-underline-offset: 4px;
    }

    /* 黃色／紅色詞彙按鈕樣式 */
    [class*="_hlY_"] button, [class*="_hlR_"] button {
        border: none !important;
        box-shadow: none !important;
        color: inherit !important;
        height: auto !important;
        min-height: unset !important;
        padding: 0 0.25em !important;
        line-height: 1.6 !important;
        font-size: 20px !important;
        font-weight: bold !important;
        border-radius: 4px !important;
        cursor: pointer;
    }
    [class*="_hlY_"] button p, [class*="_hlR_"] button p { font-size: 20px !important; margin: 0 !important; }
    [class*="_hlY_"] button svg, [class*="_hlR_"] button svg,
    [class*="_hlY_"] button [data-testid="stIconMaterial"], [class*="_hlR_"] button [data-testid="stIconMaterial"] { display: none !important; }
    [class*="_hlY_"] button { background-color: rgba(250, 204, 21, 0.55) !important; }
    [class*="_hlY_"] button:hover { background-color: rgba(250, 204, 21, 0.85) !important; }
    [class*="_hlR_"] button { background-color: rgba(239, 68, 68, 0.50) !important; }
    [class*="_hlR_"] button:hover { background-color: rgba(239, 68, 68, 0.80) !important; }

    /* 片語滑鼠懸停時組合中各字一起出現黑底線 */
    .phrase-hover-active,
    .phrase-hover-active button,
    :is([class*="_hlY_"], [class*="_hlR_"], [class*="_hlK_"]):has([data-phrase-group].phrase-hover-active) button {
        text-decoration: underline solid #111827 2.5px !important;
        text-underline-offset: 4px !important;
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


@st.cache_resource
def init_supabase() -> Client:
    url = st.secrets["SUPABASE_URL"]
    key = st.secrets["SUPABASE_KEY"]
    return create_client(url, key)


supabase = init_supabase()


def fetch_all_rows(table, columns="*", order=None, eq=None, page=1000):
    rows, start = [], 0
    while True:
        q = supabase.from_(table).select(columns)
        for k, v in (eq or {}).items():
            q = q.eq(k, v)
        for col, desc in (order or []):
            q = q.order(col, desc=desc)
        try:
            data = q.range(start, start + page - 1).execute().data or []
        except Exception:
            if order:
                return fetch_all_rows(table, columns, None, eq, page)
            raise
        rows.extend(data)
        if len(data) < page:
            return rows
        start += page


def _first_col(row: dict, cols: list[str]) -> str:
    if not isinstance(row, dict):
        return ""
    for c in cols:
        val = row.get(c)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def _detect_col_name(row: dict, cols: list[str], fallback: str) -> str:
    if isinstance(row, dict):
        for c in cols:
            if c in row:
                return c
    return fallback


def get_fb_q_tao(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("q_tao") or row.get("q_original") or "").strip()


def get_fb_q_zh(row: dict) -> str:
    if not isinstance(row, dict):
        return ""
    return str(row.get("q_zh") or row.get("q_trans") or "").strip()


def is_public_feedback(row: dict) -> bool:
    if not isinstance(row, dict):
        return False
    if "is_public" in row and row.get("is_public") is not None:
        return bool(row.get("is_public"))
    return True


def is_unverified(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    return bool(item.get("is_unverified"))


def is_sentence_item(item: dict) -> bool:
    if not isinstance(item, dict):
        return False
    if is_unverified(item):
        return False
    tao = (item.get("tao") or "").strip()
    return bool(tao and len(tao.split()) > 1)


def update_corpus_sentence(corpus_row: dict, new_tao: str, new_zh: str, editor: str) -> tuple[bool, str]:
    """更新語料庫（corpus）中的句子，讓語料更接近自然的達悟語。"""
    cid = corpus_row.get("id")
    if cid is None:
        return False, "找不到語料庫句子 ID。"
    tao_col = _detect_col_name(corpus_row, CORPUS_TAO_COLS, "tao")
    zh_col = _detect_col_name(corpus_row, CORPUS_ZH_COLS, "zh")
    payload = {
        tao_col: new_tao.strip(),
        zh_col: new_zh.strip(),
    }
    try:
        supabase.from_("corpus").update(payload).eq("id", cid).execute()
        load_all_raw_corpus.clear()
        build_word_index.clear()
        find_bilingual_examples.clear()
        _spell_resources.clear()
        spell_check_word.clear()
        return True, f"已更新語料庫句子 #{cid}（編輯者：{editor}）。"
    except Exception as e:
        return False, f"更新語料庫失敗：{e}"


@st.cache_data(ttl=300)
def _load_all_raw_corpus_cached():
    """
    載入所有可供演算法搜尋的語料句：
    1. corpus 語料庫所有句子
    2. 公開的社群句子（is_public=True 且非待審 is_unverified）：因無句子級社群認證門檻，只要公開即納入演算法搜尋對象
    """
    corpus_items = []
    try:
        for r in fetch_all_rows("corpus", order=[("id", False)]):
            tao = _first_col(r, CORPUS_TAO_COLS)
            zh = _first_col(r, CORPUS_ZH_COLS)
            corpus_items.append(
                {
                    "id": f"Corpus ID #{r.get('id')}",
                    "corpus_id": r.get("id"),
                    "corpus_raw": r,
                    "tao": tao,
                    "zh": zh,
                    "source": r.get("source") or "語料庫",
                    "full_text": f"{tao} {zh}".lower(),
                }
            )
    except Exception as e:
        st.warning(f"⚠️ 讀取 corpus 語料庫提示：{e}")

    try:
        pub_rows = fetch_all_rows("feedback", order=[("id", False)])
        for r in pub_rows:
            if not is_public_feedback(r) or is_unverified(r):
                continue
            f_id = r.get("id")
            q_tao = get_fb_q_tao(r)
            q_zh = get_fb_q_zh(r)
            r_tao = (r.get("tao_text") or "").strip()
            r_zh = (r.get("zh_text") or "").strip()
            provider = r.get("user_id") or "匿名"

            if q_tao and len(q_tao.split()) > 1:
                corpus_items.append(
                    {
                        "id": f"公開社群語料 ID #{f_id} (問)",
                        "tao": q_tao,
                        "zh": q_zh or r_zh,
                        "source": f"公開社群句子 (提供者: {provider})",
                        "is_feedback": True,
                        "full_text": f"{q_tao} {q_zh}".lower(),
                    }
                )
            if r_tao and len(r_tao.split()) > 1:
                corpus_items.append(
                    {
                        "id": f"公開社群語料 ID #{f_id} (答)",
                        "tao": r_tao,
                        "zh": r_zh or q_zh,
                        "source": f"公開社群句子 (提供者: {provider})",
                        "is_feedback": True,
                        "full_text": f"{r_tao} {r_zh}".lower(),
                    }
                )
    except Exception as e:
        st.warning(f"⚠️ 讀取公開社群語料提示：{e}")

    return corpus_items


def load_all_raw_corpus():
    items = _load_all_raw_corpus_cached()
    if not items:
        _load_all_raw_corpus_cached.clear()
    return items


load_all_raw_corpus.clear = _load_all_raw_corpus_cached.clear


def diagnose_corpus_table() -> dict:
    try:
        rows = supabase.from_("corpus").select("*").limit(5).execute().data or []
        return {"count": len(rows), "error": None}
    except Exception as e:
        return {"count": 0, "error": str(e)}


# --- 2. 會員機制與自動登入 ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()


def _load_auto_tokens() -> dict:
    if os.path.exists(AUTO_LOGIN_FILE):
        try:
            with open(AUTO_LOGIN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _save_auto_tokens(data: dict) -> None:
    try:
        with open(AUTO_LOGIN_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def issue_auto_login(username: str) -> None:
    tokens = _load_auto_tokens()
    tok = uuid.uuid4().hex
    tokens[username] = tok
    _save_auto_tokens(tokens)
    try:
        st.query_params["auto_user"] = username
        st.query_params["auto_token"] = tok
    except Exception:
        pass


def clear_auto_login(username: str | None = None) -> None:
    tokens = _load_auto_tokens()
    target_user = username or st.query_params.get("auto_user")
    if target_user and target_user in tokens:
        tokens.pop(target_user, None)
        _save_auto_tokens(tokens)
    try:
        if "auto_user" in st.query_params:
            del st.query_params["auto_user"]
        if "auto_token" in st.query_params:
            del st.query_params["auto_token"]
    except Exception:
        pass


def try_auto_login_from_device() -> None:
    if st.session_state.user_info is not None or st.session_state.auto_login_checked:
        return
    st.session_state.auto_login_checked = True
    try:
        q_user = st.query_params.get("auto_user")
        q_tok = st.query_params.get("auto_token")
    except Exception:
        q_user, q_tok = None, None
    if not q_user or not q_tok:
        return
    tokens = _load_auto_tokens()
    if tokens.get(q_user) == q_tok:
        try:
            res = supabase.from_("users").select("*").eq("username", q_user).execute()
            if res.data:
                row = res.data[0]
                st.session_state.user_info = {
                    "username": row["username"],
                    "email": row["email"],
                    "region": row["region"],
                    "age_group": row["age_group"],
                    "gender": row["gender"],
                }
        except Exception:
            pass


def add_user(username, password, email, region, age_group, gender):
    try:
        region_str = ", ".join(region) if isinstance(region, list) else region
        data = {
            "username": username,
            "password": make_hashes(password),
            "email": email,
            "region": region_str,
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


try_auto_login_from_device()


# --- 2.5 單字／片語詳情儲存與片語辨識 ---
ETYMOLOGY_OPTIONS = ["固有詞", "新造詞", "音譯"]

DEFAULT_AUTO_PHRASES = [
    {"phrase": "pongso no tao", "meaning": "蘭嶼（人之島）", "source": "auto"},
]


@st.cache_data(ttl=60)
def _load_word_details_cached() -> dict:
    if os.path.exists(WORD_DETAIL_STORE_FILE):
        try:
            with open(WORD_DETAIL_STORE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {}


def _load_word_details_store() -> dict:
    return dict(_load_word_details_cached())


def _save_word_details_store(store: dict) -> None:
    try:
        with open(WORD_DETAIL_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    finally:
        _load_word_details_cached.clear()


def get_word_detail_record(key: str) -> dict:
    k = (key or "").strip().lower()
    store = _load_word_details_cached()
    rec = store.get(k) or {}
    return {
        "pipipangayan": rec.get("pipipangayan", ""),
        "explanation": rec.get("explanation", ""),
        "origin_type": rec.get("origin_type", "固有詞"),
        "root_combo": rec.get("root_combo", ""),
        "loan_lang": rec.get("loan_lang", ""),
        "editor": rec.get("editor", ""),
        "updated_at": rec.get("updated_at", ""),
    }


def save_word_detail_record(
    key: str,
    pipipangayan: str,
    explanation: str,
    origin_type: str,
    root_combo: str,
    loan_lang: str,
    editor: str,
) -> tuple[bool, str]:
    k = (key or "").strip().lower()
    if not k:
        return False, "詞彙鍵值無效。"
    if origin_type not in ETYMOLOGY_OPTIONS:
        return False, "語源僅可選擇：固有詞、新造詞、音譯。"
    if origin_type == "新造詞" and not (root_combo or "").strip():
        return False, "選擇「新造詞」時，詞根組合為必填欄位。"
    if origin_type == "音譯" and not (loan_lang or "").strip():
        return False, "選擇「音譯」時，何種語言為必填欄位。"

    store = _load_word_details_store()
    store[k] = {
        "pipipangayan": (pipipangayan or "").strip(),
        "explanation": (explanation or "").strip(),
        "origin_type": origin_type,
        "root_combo": (root_combo or "").strip(),
        "loan_lang": (loan_lang or "").strip(),
        "editor": editor,
        "updated_at": datetime.datetime.now().isoformat(),
    }
    _save_word_details_store(store)
    return True, "已儲存單字詳情！"


def _load_phrase_store() -> dict:
    store = {
        "phrases": list(DEFAULT_AUTO_PHRASES),
    }
    if os.path.exists(PHRASE_STORE_FILE):
        try:
            with open(PHRASE_STORE_FILE, "r", encoding="utf-8") as f:
                raw = json.load(f)
                if isinstance(raw, dict) and isinstance(raw.get("phrases"), list):
                    store["phrases"] = raw["phrases"]
        except Exception:
            pass
    return store


def _save_phrase_store(store: dict) -> None:
    try:
        with open(PHRASE_STORE_FILE, "w", encoding="utf-8") as f:
            json.dump(store, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def normalize_phrase_key(phrase: str) -> str:
    return re.sub(r"\s+", " ", (phrase or "").strip().lower())


def is_phrase_disabled_in_sentence(sentence_key_id: str | None, phrase: str) -> bool:
    if not sentence_key_id or not phrase:
        return False
    p_key = normalize_phrase_key(phrase)
    disabled_map = st.session_state.per_sentence_disabled_phrases
    return p_key in set(disabled_map.get(sentence_key_id, []))


def cancel_phrase_in_sentence(sentence_key_id: str | None, phrase: str) -> None:
    """僅取消該句子中的片語組合，不會刪除其他句子或全域登錄的片語組合。"""
    if not sentence_key_id or not phrase:
        return
    p_key = normalize_phrase_key(phrase)
    disabled_map = st.session_state.per_sentence_disabled_phrases
    s_set = set(disabled_map.get(sentence_key_id, []))
    s_set.add(p_key)
    disabled_map[sentence_key_id] = sorted(s_set)


def register_phrase(phrase: str, meaning: str = "", source: str = "manual") -> bool:
    norm_phrase = re.sub(r"\s+", " ", (phrase or "").strip())
    key = normalize_phrase_key(norm_phrase)
    if not key or len(key.split()) < 2:
        return False

    store = _load_phrase_store()
    existing_idx = None
    for idx, item in enumerate(store.get("phrases", [])):
        if normalize_phrase_key(item.get("phrase")) == key:
            existing_idx = idx
            break

    entry = {
        "phrase": norm_phrase,
        "meaning": (meaning or "").strip() or (store["phrases"][existing_idx].get("meaning") if existing_idx is not None else "片語"),
        "source": source,
    }
    if existing_idx is not None:
        store["phrases"][existing_idx] = entry
    else:
        store.setdefault("phrases", []).append(entry)
    _save_phrase_store(store)

    if entry["meaning"]:
        detail_store = _load_word_details_store()
        merged_key = "_".join(key.split())
        if merged_key not in detail_store or not detail_store[merged_key].get("explanation"):
            cur = detail_store.get(merged_key, {})
            cur["explanation"] = cur.get("explanation") or entry["meaning"]
            cur.setdefault("pipipangayan", "")
            cur.setdefault("origin_type", "固有詞")
            cur.setdefault("root_combo", "")
            cur.setdefault("loan_lang", "")
            detail_store[merged_key] = cur
            _save_word_details_store(detail_store)
    return True


def auto_detect_and_save_phrases(tao_text: str, zh_text: str = "") -> list[str]:
    detected = []
    combined = f"{tao_text or ''} {zh_text or ''}"

    for m in re.finditer(r"([\u4e00-\u9fffA-Za-z0-9'’ʼʻ -]{1,30}?)\s*[（(]\s*([A-Za-z][A-Za-z0-9'’ʼʻ-]*(?:\s+[A-Za-z][A-Za-z0-9'’ʼʻ-]*)+)\s*[）)]", combined):
        zh_label = m.group(1).strip()
        tao_phrase = re.sub(r"\s+", " ", m.group(2).strip())
        meaning = f"{zh_label}（自動辨識片語）" if zh_label else "自動辨識片語"
        if register_phrase(tao_phrase, meaning=meaning, source="auto"):
            detected.append(tao_phrase)

    store = _load_phrase_store()
    for p_item in store.get("phrases", []):
        p_str = p_item.get("phrase") or ""
        p_key = normalize_phrase_key(p_str)
        if p_key:
            pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in p_key.split()) + r"\b", flags=re.IGNORECASE)
            if pattern.search(tao_text or ""):
                detected.append(p_str)

    return list(dict.fromkeys(detected))


def get_active_phrases() -> list[dict]:
    return list(_load_phrase_store().get("phrases", []))


def merge_phrases_in_sentence(sentence: str, sent_key_id: str | None = None) -> tuple[str, dict[str, dict]]:
    s = sentence or ""
    phrases = sorted(get_active_phrases(), key=lambda x: -len(normalize_phrase_key(x.get("phrase", "")).split()))
    phrase_meta = {}
    if not phrases:
        return s, phrase_meta

    for item in phrases:
        raw_p = (item.get("phrase") or "").strip()
        if is_phrase_disabled_in_sentence(sent_key_id, raw_p):
            continue
        words = [w for w in normalize_phrase_key(raw_p).split() if w]
        if len(words) < 2:
            continue
        merged_token = "_".join(words)
        phrase_meta[merged_token.lower()] = {
            "phrase": raw_p,
            "meaning": item.get("meaning") or "片語",
            "source": item.get("source") or "manual",
        }
        pattern = re.compile(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b", flags=re.IGNORECASE)
        s = pattern.sub(merged_token, s)

    return s, phrase_meta


# --- 3. 公開 / 私人語料讀寫函式 ---
def word_diff(old, new):
    a, b = (old or "").split(), (new or "").split()
    sm = difflib.SequenceMatcher(None, [x.lower() for x in a], [x.lower() for x in b], autojunk=False)
    ops = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        op = {"op": tag, "pos": i1, "from": " ".join(a[i1:i2]), "to": " ".join(b[j1:j2])}
        if tag == "replace" and i2 - i1 == 1 and j2 - j1 == 1:
            cs = difflib.SequenceMatcher(None, a[i1].lower(), b[j1].lower(), autojunk=False)
            op["chars"] = [
                [t, a[i1].lower()[x1:x2], b[j1].lower()[y1:y2]]
                for t, x1, x2, y1, y2 in cs.get_opcodes()
                if t != "equal"
            ]
        ops.append(op)
    return ops


def build_edit_record(ai_data, q_fallback, e_q_tao, e_q_zh, e_a_tao, e_a_zh):
    o_q_tao = ai_data.get("user_recognized_tao", q_fallback) or ""
    o_q_zh = ai_data.get("user_translation") or ""
    o_a_tao = ai_data.get("ai_reply_tao") or ""
    o_a_zh = ai_data.get("ai_reply_zh") or ""
    pairs = [(o_q_tao, e_q_tao), (o_q_zh, e_q_zh), (o_a_tao, e_a_tao), (o_a_zh, e_a_zh)]
    if all((o or "").strip() == (e or "").strip() for o, e in pairs):
        return None
    return {
        "source": "ai" if ai_data.get("is_ai_generated") else "algorithm",
        "original_q_tao": o_q_tao, "original_q_zh": o_q_zh,
        "original_a_tao": o_a_tao, "original_a_zh": o_a_zh,
        "raw_a_tao": ai_data.get("ai_reply_tao_raw") or o_a_tao,
        "edited_q_tao": e_q_tao, "edited_q_zh": e_q_zh,
        "edited_a_tao": e_a_tao, "edited_a_zh": e_a_zh,
        "diff_json": {"q": word_diff(o_q_tao, e_q_tao), "a": word_diff(o_a_tao, e_a_tao)},
    }


def _local_private_file(username: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", username or "anonymous")
    os.makedirs(LOCAL_PRIVATE_DIR, exist_ok=True)
    return os.path.join(LOCAL_PRIVATE_DIR, f"{safe_name}.json")


def _load_local_private_rows(username: str) -> list[dict]:
    cache = st.session_state.local_private_cache
    if username in cache:
        return list(cache[username])
    path = _local_private_file(username)
    rows = []
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    rows = data
        except Exception:
            rows = []
    cache[username] = rows
    return list(rows)


def _save_local_private_rows(username: str, rows: list[dict]) -> None:
    st.session_state.local_private_cache[username] = list(rows)
    path = _local_private_file(username)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def save_private_record(private_payload: dict) -> bool:
    username = private_payload["user_id"]
    payload = dict(private_payload)
    while True:
        try:
            supabase.from_(PRIVATE_TABLE).insert(payload).execute()
            load_personal_private_corpus.clear()
            return True
        except Exception as db_err:
            err_msg = str(db_err)
            removed = False
            for opt_col in ("orig_ai_q_tao", "orig_ai_a_tao"):
                if opt_col in payload and opt_col in err_msg:
                    payload.pop(opt_col)
                    removed = True
            if not removed:
                break

    rows = _load_local_private_rows(username)
    local_rec = dict(private_payload)
    local_rec["id"] = f"local_{uuid.uuid4().hex[:8]}"
    local_rec["_storage"] = "local"
    rows.insert(0, local_rec)
    _save_local_private_rows(username, rows)
    load_personal_private_corpus.clear()
    return True


def fetch_user_private_records(username: str) -> list[dict]:
    if not username:
        return []
    combined = []
    try:
        db_rows = fetch_all_rows(
            PRIVATE_TABLE,
            order=[("timestamp", True), ("id", True)],
            eq={"user_id": username},
        )
        for r in db_rows:
            if r.get("user_id") != username:
                continue
            rec = dict(r)
            rec["_storage"] = PRIVATE_TABLE
            rec["is_public"] = False
            combined.append(rec)
    except Exception:
        pass

    for r in _load_local_private_rows(username):
        if r.get("user_id") != username:
            continue
        rec = dict(r)
        rec["_storage"] = rec.get("_storage") or "local"
        rec["is_public"] = False
        combined.append(rec)

    combined.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)
    return combined


@st.cache_data(ttl=120)
def load_personal_private_corpus(username: str | None) -> list[dict]:
    """僅讀取指定個人帳號（username）的私人句子，供該帳號自己的演算法使用。"""
    if not username:
        return []
    items = []
    for r in fetch_user_private_records(username):
        if r.get("user_id") != username:
            continue
        q_tao = (get_fb_q_tao(r) or "").strip()
        q_zh = (get_fb_q_zh(r) or "").strip()
        r_tao = (r.get("tao_text") or "").strip()
        r_zh = (r.get("zh_text") or "").strip()

        if q_tao and len(q_tao.split()) > 1:
            items.append({
                "id": f"Private #{r.get('id')}",
                "tao": q_tao,
                "zh": q_zh or r_zh,
                "source": f"個人私人語料 ({username})",
                "is_private": True,
                "owner_id": username,
                "full_text": f"{q_tao} {q_zh}".lower(),
            })
        if r_tao and len(r_tao.split()) > 1:
            items.append({
                "id": f"Private #{r.get('id')}",
                "tao": r_tao,
                "zh": r_zh or q_zh,
                "source": f"個人私人語料 ({username})",
                "is_private": True,
                "owner_id": username,
                "full_text": f"{r_tao} {r_zh}".lower(),
            })
    return items


def _insert_public_feedback_row(data: dict) -> bool:
    payload = dict(data)
    while True:
        try:
            supabase.from_("feedback").insert(payload).execute()
            break
        except Exception as col_err:
            err_msg = str(col_err)
            removed = False
            if "q_tao" in err_msg and "q_original" not in payload:
                payload["q_original"] = payload.pop("q_tao")
                payload["q_trans"] = payload.pop("q_zh", None)
                removed = True
            for opt_col in ("is_public", "origin", "input_lang_type", "orig_ai_q_tao", "orig_ai_a_tao"):
                if opt_col in payload and opt_col in err_msg:
                    payload.pop(opt_col)
                    removed = True
            if not removed:
                raise
    build_word_index.clear()
    load_user_typed_words.clear()
    load_all_raw_corpus.clear()
    find_bilingual_examples.clear()
    return True


def save_語料_to_supabase(
    user_info,
    bg_info,
    q_tao,
    q_zh,
    r_tao,
    r_zh,
    is_edited=False,
    is_error=False,
    is_public=True,
    origin=None,
    pending_sentence_id=None,
    edit_record=None,
    orig_ai_q_tao=None,
    orig_ai_a_tao=None,
):
    try:
        region_val = bg_info["region"]
        region_str = ", ".join(region_val) if isinstance(region_val, list) else region_val
        input_origin = origin or detect_input_origin(q_tao if origin == "tao" else q_zh)
        now_iso = datetime.datetime.now().isoformat()

        auto_detect_and_save_phrases(f"{q_tao or ''} {r_tao or ''}", f"{q_zh or ''} {r_zh or ''}")

        if not bool(is_public):
            private_data = {
                "user_id": user_info["username"],
                "user_email": user_info["email"],
                "region": region_str,
                "age_group": bg_info["age_group"],
                "gender": bg_info["gender"],
                "q_tao": q_tao,
                "q_zh": q_zh,
                "tao_text": r_tao,
                "zh_text": r_zh,
                "orig_ai_q_tao": orig_ai_q_tao,
                "orig_ai_a_tao": orig_ai_a_tao,
                "is_edited": 1 if is_edited else 0,
                "error_count": 1 if is_error else 0,
                "origin": input_origin,
                "timestamp": now_iso,
            }
            return save_private_record(private_data)

        data = {
            "user_id": user_info["username"],
            "user_email": user_info["email"],
            "region": region_str,
            "age_group": bg_info["age_group"],
            "gender": bg_info["gender"],
            "q_tao": q_tao,
            "q_zh": q_zh,
            "q_audio_data": None,
            "q_audio_mime": None,
            "tao_text": r_tao,
            "zh_text": r_zh,
            "orig_ai_q_tao": orig_ai_q_tao,
            "orig_ai_a_tao": orig_ai_a_tao,
            "r_audio_data": None,
            "r_audio_mime": None,
            "is_edited": 1 if is_edited else 0,
            "error_count": 1 if is_error else 0,
            "is_public": True,
            "origin": input_origin,
            "timestamp": now_iso,
        }

        _insert_public_feedback_row(data)

        if edit_record and is_edited:
            try:
                res_latest = supabase.from_("feedback").select("id").eq("user_id", user_info["username"]).order("id", desc=True).limit(1).execute()
                fb_new_id = res_latest.data[0].get("id") if res_latest.data else None
                supabase.from_("sentence_edits").insert(
                    {**edit_record, "feedback_id": fb_new_id, "user_id": user_info["username"]}
                ).execute()
            except Exception as edit_err:
                st.toast(f"修改前的原句未能保存（請建立 sentence_edits 資料表）：{edit_err}", icon="⚠️")

        if pending_sentence_id and not is_edited:
            try:
                res_latest = supabase.from_("feedback").select("id").eq("user_id", user_info["username"]).order("id", desc=True).limit(1).execute()
                new_id = res_latest.data[0].get("id") if res_latest.data else None
                if new_id:
                    supabase.from_("community_votes").update({"feedback_id": new_id}).is_(
                        "feedback_id", "null"
                    ).like("hidden_suggestion", f"%sent:{pending_sentence_id}%").execute()

                    remaining_pending = []
                    for pv in st.session_state.get("pending_session_votes", []):
                        if pv.get("sentence_id") == pending_sentence_id:
                            row_to_insert = dict(pv["payload"])
                            row_to_insert["feedback_id"] = new_id
                            supabase.from_("community_votes").insert(row_to_insert).execute()
                        else:
                            remaining_pending.append(pv)
                    st.session_state["pending_session_votes"] = remaining_pending

                    load_vote_data.clear()
                    load_user_votes.clear()
            except Exception:
                pass

        return True
    except Exception as e:
        st.error(f"儲存失敗：{e}")
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

        supabase.from_("feedback").delete().eq("id", feedback_id).execute()
        build_word_index.clear()
        load_user_typed_words.clear()
        load_all_raw_corpus.clear()
        find_bilingual_examples.clear()
        return True, "刪除成功！"
    except Exception as e:
        return False, f"刪除失敗：{e}"


def delete_private_record(item: dict, current_username: str) -> tuple[bool, str]:
    rec_id = item.get("id")
    storage = item.get("_storage")
    if not current_username:
        return False, "尚未登入。"
    if storage == "local" or str(rec_id).startswith("local_"):
        rows = [r for r in _load_local_private_rows(current_username) if r.get("id") != rec_id]
        _save_local_private_rows(current_username, rows)
        load_personal_private_corpus.clear()
        return True, "已刪除私人紀錄！"
    try:
        supabase.from_(PRIVATE_TABLE).delete().eq("id", rec_id).eq("user_id", current_username).execute()
        load_personal_private_corpus.clear()
        return True, "已刪除私人紀錄！"
    except Exception as e:
        return False, f"刪除私人紀錄失敗：{e}"


def update_existing_conversation(
    existing_item: dict,
    user_info: dict,
    new_q_tao: str,
    new_q_zh: str,
    new_r_tao: str,
    new_r_zh: str,
    target_public: bool,
) -> tuple[bool, str]:
    username = user_info["username"]
    was_public = existing_item.get("_source_table") != "private" and is_public_feedback(existing_item)
    rec_id = existing_item.get("id")
    new_origin = detect_input_origin(new_q_tao or new_q_zh)

    if was_public == bool(target_public):
        if was_public:
            upd = {
                "q_tao": new_q_tao,
                "q_zh": new_q_zh,
                "tao_text": new_r_tao,
                "zh_text": new_r_zh,
                "is_edited": 1,
                "origin": new_origin,
            }
            try:
                supabase.from_("feedback").update(upd).eq("id", rec_id).eq("user_id", username).execute()
            except Exception:
                upd.pop("origin", None)
                if "q_tao" in str(upd):
                    upd["q_original"] = upd.pop("q_tao")
                    upd["q_trans"] = upd.pop("q_zh")
                supabase.from_("feedback").update(upd).eq("id", rec_id).eq("user_id", username).execute()
            build_word_index.clear()
            load_user_typed_words.clear()
            load_all_raw_corpus.clear()
            find_bilingual_examples.clear()
            return True, "已更新公開對話紀錄！"
        else:
            if existing_item.get("_storage") == "local" or str(rec_id).startswith("local_"):
                rows = _load_local_private_rows(username)
                for r in rows:
                    if r.get("id") == rec_id:
                        r["q_tao"] = new_q_tao
                        r["q_zh"] = new_q_zh
                        r["tao_text"] = new_r_tao
                        r["zh_text"] = new_r_zh
                        r["is_edited"] = 1
                        r["origin"] = new_origin
                _save_local_private_rows(username, rows)
            else:
                supabase.from_(PRIVATE_TABLE).update({
                    "q_tao": new_q_tao,
                    "q_zh": new_q_zh,
                    "tao_text": new_r_tao,
                    "zh_text": new_r_zh,
                    "is_edited": 1,
                    "origin": new_origin,
                }).eq("id", rec_id).eq("user_id", username).execute()
            load_personal_private_corpus.clear()
            return True, "已更新私人對話紀錄！"

    bg_info = {
        "region": existing_item.get("region") or user_info.get("region"),
        "age_group": existing_item.get("age_group") or user_info.get("age_group"),
        "gender": existing_item.get("gender") or user_info.get("gender"),
    }
    ok_save = save_語料_to_supabase(
        user_info=user_info,
        bg_info=bg_info,
        q_tao=new_q_tao,
        q_zh=new_q_zh,
        r_tao=new_r_tao,
        r_zh=new_r_zh,
        is_edited=True,
        is_error=bool(existing_item.get("error_count")),
        is_public=bool(target_public),
        origin=new_origin,
        orig_ai_q_tao=existing_item.get("orig_ai_q_tao"),
        orig_ai_a_tao=existing_item.get("orig_ai_a_tao"),
    )
    if not ok_save:
        return False, "切換公開／私人狀態時儲存失敗。"
    if was_public:
        delete_feedback_item(rec_id, username)
    else:
        delete_private_record(existing_item, username)
    return True, "已更新內容並切換公開／私人狀態！"


def _get_algorithm_sentence_pool(username: str | None = None) -> list[dict]:
    """
    取得演算法可搜尋句子池：
    - 語料庫與所有公開社群句子（is_public=True）
    - 若有登入帳號，再加入該個人帳號自己的私人句子
    """
    pool = [it for it in load_all_raw_corpus() if is_sentence_item(it)]
    if username:
        for p_item in load_personal_private_corpus(username):
            if p_item.get("owner_id") == username:
                pool.append(p_item)
    return pool


GRAMMAR_MARKERS = {
    "a", "am", "no", "do", "o", "kano", "ji", "ya", "ko", "mo", "nia",
    "nire", "ikongo", "akokay", "ayoy", "sirisiring", "macisirisiring",
    "maniring", "mivatvatek", "mivaliw", "ma'ogto",
}


def _grammar_signature(tokens: list[str]) -> set[str]:
    return {t for t in tokens if t in GRAMMAR_MARKERS}


def search_relevant_corpus(user_input, top_k=3, username=None):
    all_corpus = _get_algorithm_sentence_pool(username=username)
    if not all_corpus:
        return []

    if not user_input:
        matched_items = all_corpus[-top_k:]
    else:
        clean_input = user_input.lower().strip()
        user_tokens = [normalize_word(t) for t in user_input.split()]
        user_set = {t for t in user_tokens if t}
        user_gram = _grammar_signature(user_tokens)
        phrase = " ".join(t for t in user_tokens if t)

        scored_items = []
        for item in all_corpus:
            score = 0
            item_words = [normalize_word(t) for t in item["tao"].split()]
            item_set = {t for t in item_words if t}
            item_gram = _grammar_signature(item_words)

            if clean_input in item["full_text"] or (phrase and phrase in " ".join(item_words)):
                score += 100

            gram_overlap = len(user_gram & item_gram)
            if gram_overlap:
                score += gram_overlap * 15

            for kw in user_set:
                if kw in item_set:
                    score += 12 if kw in GRAMMAR_MARKERS else 8

            if score > 0:
                scored_items.append((score, item))

        scored_items.sort(key=lambda x: x[0], reverse=True)
        matched_items = [item for score, item in scored_items[:top_k]]

        if not matched_items:
            matched_items = all_corpus[-top_k:]

    formatted_corpus = []
    for r in matched_items:
        formatted_corpus.append(
            f"[{r['id']}] 出處: {r['source']} | 達悟語: {r['tao']} | 中文: {r['zh']}"
        )

    return formatted_corpus


def algorithm_first_processing(user_text, username=None):
    if not user_text:
        return None

    all_corpus = _get_algorithm_sentence_pool(username=username)
    clean_input = user_text.lower().strip()

    for item in all_corpus:
        if clean_input == item["tao"].lower().strip() or clean_input == item["zh"].lower().strip():
            return {
                "user_recognized_tao": item["tao"],
                "user_translation": item["zh"],
                "ai_reply_tao": item["tao"],
                "ai_reply_zh": item["zh"],
                "etymology": {w: f"中文對應: {item['zh']}" for w in item["tao"].split()},
                "reference": f"【純演算法完全匹配】{item['id']} 出處: {item['source']}",
                "is_algorithm": True,
            }

    user_tokens_list = [normalize_word(t) for t in user_text.split()]
    user_tokens = {t for t in user_tokens_list if t}
    user_gram = _grammar_signature(user_tokens_list)

    best_item = None
    best_key = (0, 0.0, 0)
    for item in all_corpus:
        item_tokens_list = [normalize_word(t) for t in item["tao"].split()]
        item_tokens = {t for t in item_tokens_list if t}
        item_gram = _grammar_signature(item_tokens_list)
        matches = len(user_tokens & item_tokens)
        gram_matches = len(user_gram & item_gram)
        if matches < 2 or not user_tokens:
            continue
        coverage = matches / len(user_tokens)
        jaccard = matches / len(user_tokens | item_tokens)
        if coverage >= 0.8 and jaccard >= 0.75 and (matches, jaccard, gram_matches) > best_key:
            best_key = (matches, jaccard, gram_matches)
            best_item = item

    if best_item:
        return {
            "user_recognized_tao": best_item["tao"],
            "user_translation": best_item["zh"],
            "ai_reply_tao": best_item["tao"],
            "ai_reply_zh": best_item["zh"],
            "etymology": {w: f"中文對應: {best_item['zh']}" for w in best_item["tao"].split()},
            "reference": f"【純演算法權重匹配】{best_item['id']} 出處: {best_item['source']}",
            "is_algorithm": True,
        }

    return None


# --- 5. 單字用例統計、螢光標註與投票 ---
HIGHLIGHT_THRESHOLD = 10
WORD_STRIP_CHARS = ".,!?;:\"“”()[]「」『』。，！？；：、…—–"

APOSTROPHE_CHARS = "’‘ʼʻ´`′ʹʔ"
_APOS_TABLE = str.maketrans({c: "'" for c in APOSTROPHE_CHARS})


def canon_apostrophes(text):
    return (text or "").translate(_APOS_TABLE)


def normalize_word(token):
    t = canon_apostrophes(unicodedata.normalize("NFC", token or ""))
    return t.strip(WORD_STRIP_CHARS).lower()


def split_token(token):
    esc = re.escape(WORD_STRIP_CHARS)
    m = re.match(rf"^([{esc}]*)(.*?)([{esc}]*)$", token)
    return m.group(1), m.group(2), m.group(3)


@st.cache_data(ttl=300)
def _build_word_index_cached():
    index = {}

    def add(text, zh, source, wordlist=False):
        toks = (text or "").split()
        for tok in toks:
            w = normalize_word(tok)
            if not w:
                continue
            entry = index.setdefault(w, {"count": 0, "corpus": 0, "community": 0, "zh": zh, "glosses": []})
            entry["count"] += 1
            entry[source] += 1
            if wordlist and len(toks) == 1 and zh and zh not in entry["glosses"]:
                entry["glosses"].append(zh)

    for item in load_all_raw_corpus():
        if not item.get("is_feedback"):
            if not is_unverified(item):
                add(item["tao"], item["zh"], "corpus", wordlist=True)

    try:
        rows = fetch_all_rows("feedback")
        for r in rows:
            if not is_public_feedback(r):
                continue
            add(get_fb_q_tao(r), get_fb_q_zh(r), "community")
            add(r.get("tao_text"), r.get("zh_text"), "community")
    except Exception:
        pass

    return index


def build_word_index():
    index = _build_word_index_cached()
    if not any(e.get("corpus") for e in index.values()):
        _build_word_index_cached.clear()
    return index


build_word_index.clear = _build_word_index_cached.clear


@st.cache_data(ttl=120)
def find_bilingual_examples(word_key: str, limit: int = 1) -> list[tuple[str, str]]:
    w_target = normalize_word(word_key)
    if not w_target:
        return []
    results = []
    seen = set()

    for it in load_all_raw_corpus():
        if is_unverified(it):
            continue
        tao_s = (it.get("tao") or "").strip()
        zh_s = (it.get("zh") or "").strip()
        if not tao_s or not zh_s:
            continue
        toks = {normalize_word(t) for t in tao_s.split()}
        if w_target in toks and tao_s not in seen:
            seen.add(tao_s)
            results.append((tao_s, zh_s))
            if len(results) >= limit:
                return results

    try:
        for fb in fetch_all_rows("feedback"):
            if not is_public_feedback(fb):
                continue
            for t_col, z_col in [(get_fb_q_tao(fb), get_fb_q_zh(fb)), (fb.get("tao_text"), fb.get("zh_text"))]:
                tao_s = (t_col or "").strip()
                zh_s = (z_col or "").strip()
                if not tao_s or not zh_s:
                    continue
                toks = {normalize_word(t) for t in tao_s.split()}
                if w_target in toks and tao_s not in seen:
                    seen.add(tao_s)
                    results.append((tao_s, zh_s))
                    if len(results) >= limit:
                        return results
    except Exception:
        pass

    return results


# --- 5.5 單字描述校正與異體字判定 ---
LOANWORD_TAG = "loanword"
REL_SPELLING = "拼法不同（同一個字的不同寫法）"
REL_TRANSLATION = "譯詞相同（不同的字，意思完全一樣）"


def _desc_norm(s):
    return re.sub(r"[\s\u3000,.;:!?，。；：！？、/／()（）「」『』\"'“”]+", "", (s or "").lower())


def _edit_distance(a, b):
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def spelling_similar(a, b):
    if not a or not b or a == b:
        return False
    longer = max(len(a), len(b))
    return longer >= 3 and _edit_distance(a, b) <= max(1, longer // 4)


@st.cache_data(ttl=60)
def load_word_notes():
    notes = {}
    try:
        rows = fetch_all_rows("word_notes", "word, description, editor, created_at, tag", order=[("id", False)])
    except Exception:
        try:
            rows = fetch_all_rows("word_notes", "word, description, editor, created_at", order=[("id", False)])
        except Exception:
            return notes
    for r in rows:
        w = normalize_word(r.get("word") or "")
        d = (r.get("description") or "").strip()
        if w and d:
            notes[w] = {"description": d, "editor": r.get("editor"), "created_at": r.get("created_at"), "tag": r.get("tag")}
    return notes


def is_loanword(word, notes):
    return (notes.get(word) or {}).get("tag") == LOANWORD_TAG


def save_word_note(word, description, editor, tag=None):
    row = {"word": word, "description": description.strip(), "editor": editor}
    if tag:
        row["tag"] = tag
    supabase.from_("word_notes").insert(row).execute()
    load_word_notes.clear()
    load_vote_data.clear()
    load_variant_stats.clear()


def link_is_valid(a, b, rel, notes):
    da = (notes.get(a) or {}).get("description")
    db = (notes.get(b) or {}).get("description")
    if rel == "spelling":
        if da and db and _desc_norm(da) != _desc_norm(db):
            return False
        return spelling_similar(a, b)
    if rel == "translation":
        return bool(da and db and _desc_norm(da) == _desc_norm(db))
    return False


def check_variant_proposal(word, alt, rel, shared_desc, notes):
    da = (notes.get(word) or {}).get("description")
    db = (notes.get(alt) or {}).get("description")

    if rel == "spelling":
        if not spelling_similar(word, alt):
            return False, (
                f"「{alt}」與「{word}」的拼法差異太大，不能算只是拼法不同。"
                "若兩者是不同的字、但譯詞完全相同，請改選「譯詞相同」並填寫共同描述。"
            ), {}
        if da and db and _desc_norm(da) != _desc_norm(db):
            return False, f"「{word}」（{da}）與「{alt}」（{db}）的描述不同，不能算同一個字的不同拼法。", {}
        return True, "", {}

    shared = (shared_desc or "").strip()
    if not shared:
        return False, "選擇「譯詞相同」時，請填寫兩字共同的中文描述。", {}
    to_save = {}
    for w in (word, alt):
        cur = (notes.get(w) or {}).get("description")
        if cur:
            if _desc_norm(cur) != _desc_norm(shared):
                return False, (
                    f"「{w}」目前的描述是「{cur}」，與您填寫的「{shared}」不同，所以不能算異體字。"
                    "若描述有誤，請先修改該字的描述；若兩字只是意思相近、並非譯詞，請不要提出為異體字。"
                ), {}
        else:
            to_save[w] = shared
    return True, "", to_save


def _describe_word_with_origin(word: str, detail_rec: dict, note: dict | None, glosses: list[str]) -> str:
    """組合單字語源與詞義描述，避免固有詞與音譯詞同形干擾。"""
    origin_type = (detail_rec.get("origin_type") or "").strip()
    expl = (detail_rec.get("explanation") or "").strip() or ((note or {}).get("description") or "").strip()
    gloss_str = "；".join(glosses[:3]) if glosses else ""

    if not origin_type:
        if (note or {}).get("tag") == LOANWORD_TAG or "音譯" in expl:
            origin_type = "音譯"
        else:
            origin_type = "固有詞"

    meaning_part = expl or gloss_str or "語意待確認"
    if origin_type == "音譯" and detail_rec.get("loan_lang"):
        return f"[語源：音譯（{detail_rec['loan_lang']}）] {meaning_part}"
    if origin_type == "新造詞" and detail_rec.get("root_combo"):
        return f"[語源：新造詞（詞根：{detail_rec['root_combo']}）] {meaning_part}"
    return f"[語源：{origin_type}] {meaning_part}"


def build_dictionary_context(text, limit=40):
    idx = build_word_index()
    notes = load_word_notes()
    lines, seen = [], set()
    for tok in (text or "").split():
        w = normalize_word(tok)
        if not w or w in seen:
            continue
        seen.add(w)
        det = get_word_detail_record(w)
        gl = idx.get(w, {}).get("glosses", [])
        if det.get("explanation") or w in notes or gl:
            lines.append(f"- {tok} ({w}): {_describe_word_with_origin(w, det, notes.get(w), gl)}")
    return "\n".join(lines[:limit])


def build_loanword_context(limit=80):
    notes, idx = load_word_notes(), build_word_index()
    group_of = load_vote_data()["group_of"]
    store = _load_word_details_cached()
    best = {}

    candidate_words = set()
    for w, n in notes.items():
        if n.get("tag") == LOANWORD_TAG or "音譯" in (n.get("description") or ""):
            candidate_words.add(w)
    for k, rec in store.items():
        if rec.get("origin_type") == "音譯":
            candidate_words.add(normalize_word(k))

    for w in candidate_words:
        if not w:
            continue
        key = group_of.get(w, w)
        if key not in best or idx.get(w, {}).get("count", 0) > idx.get(best[key], {}).get("count", 0):
            best[key] = w

    lines = []
    for w in sorted(best.values(), key=lambda x: -idx.get(x, {}).get("count", 0))[:limit]:
        det = get_word_detail_record(w)
        desc = det.get("explanation") or re.sub(r"^華語音譯[：:]", "", (notes.get(w) or {}).get("description", ""))
        lang_hint = f"（{det['loan_lang']}音譯）" if det.get("loan_lang") else "（音譯詞）"
        lines.append(f"- {w} {lang_hint}: {desc}（注意：若上下文為達悟語固有詞用法，請勿誤套音譯詞義）")
    return "\n".join(lines)


def register_new_words(ai_data, final_tao, username):
    new_words = ai_data.get("new_words") if isinstance(ai_data, dict) else None
    if not isinstance(new_words, dict) or not new_words:
        return
    notes = load_word_notes()
    present = {normalize_word(t) for t in (final_tao or "").split()}
    for tao_w, zh in new_words.items():
        w = normalize_word(tao_w)
        if not w or not zh or w not in present or w in notes:
            continue
        try:
            save_word_note(w, f"華語音譯：{zh}", f"AI生成（{username} 採納）", tag=LOANWORD_TAG)
        except Exception:
            pass


def build_notes_context(*texts, limit=30):
    notes = load_word_notes()
    store = _load_word_details_cached()
    if not notes and not store:
        return ""
    seen, lines = set(), []
    for t in texts:
        for tok in (t or "").split():
            w = normalize_word(tok)
            if w and w not in seen and (w in notes or w in store):
                seen.add(w)
                det = get_word_detail_record(w)
                lines.append(f"- {tok} ({w}): {_describe_word_with_origin(w, det, notes.get(w), [])}")
    return "\n".join(lines[:limit])


# --- 拼寫與寫法檢查 ---
SPELL_MIN_LEN = 4
SPELL_MIN_CANDIDATE_COUNT = 3


def _deletions(w, max_d):
    out, frontier = {w}, {w}
    for _ in range(max_d):
        nxt = {x[:i] + x[i + 1:] for x in frontier for i in range(len(x))}
        out |= nxt
        frontier = nxt
    return out


@st.cache_resource(ttl=300)
def _spell_resources():
    idx = build_word_index()
    deletes, chars, n_types, plain = {}, {}, 0, {}
    for w, e in idx.items():
        c = e.get("corpus", 0)
        if c <= 0:
            continue
        n_types += 1
        plain.setdefault(w.replace("'", ""), set()).add(w)
        if len(w) >= SPELL_MIN_LEN and c >= SPELL_MIN_CANDIDATE_COUNT:
            for d in _deletions(w, 2):
                deletes.setdefault(d, set()).add(w)
        for ch in set(w):
            if ch.isalpha() or ch == "'":
                chars[ch] = chars.get(ch, 0) + 1
    return {"idx": idx, "deletes": deletes, "chars": chars, "n_types": n_types, "plain": plain}


@st.cache_data(ttl=300)
def spell_check_word(word):
    out = {"suggestions": [], "odd_chars": [], "glottal": []}
    res = _spell_resources()
    if not res["chars"] or not word:
        return out

    out["glottal"] = sorted(
        ((c, res["idx"][c]["corpus"]) for c in res["plain"].get(word.replace("'", ""), set()) if c != word),
        key=lambda x: -x[1],
    )[:3]

    rare_cut = 2 if res["n_types"] >= 500 else 0
    out["odd_chars"] = sorted(
        {ch for ch in word if (ch.isalpha() or ch == "'") and res["chars"].get(ch, 0) <= rare_cut}
    )

    if len(word) >= SPELL_MIN_LEN:
        k = min(2, max(1, len(word) // 4))
        cands = set()
        for d in _deletions(word, k):
            cands |= res["deletes"].get(d, set())
        need = max(SPELL_MIN_CANDIDATE_COUNT, 3 * res["idx"].get(word, {}).get("count", 0))
        scored = []
        for c in cands:
            if c == word:
                continue
            dist = _edit_distance(word, c)
            n = res["idx"][c]["corpus"]
            if dist <= k and n >= need:
                scored.append((dist, -n, c))
        scored.sort()
        out["suggestions"] = [(c, d, -n) for d, n, c in scored[:3]]
    return out


def spellcheck_notes(sentence, limit=5):
    idx = _spell_resources()["idx"]
    notes, seen = [], set()
    for tok in (sentence or "").split():
        w = normalize_word(tok)
        if not w or w in seen or idx.get(w, {}).get("count", 0) > HIGHLIGHT_THRESHOLD:
            continue
        seen.add(w)
        sc = spell_check_word(w)
        if sc["glottal"]:
            notes.append(f"「{w}」只差喉塞音符號 → {'、'.join(g[0] for g in sc['glottal'])}")
        elif sc["suggestions"]:
            notes.append(f"「{w}」→ 你是不是想寫 {'、'.join(s[0] for s in sc['suggestions'])}")
        elif sc["odd_chars"]:
            notes.append(f"「{w}」含有語料庫中罕見的字母 {'、'.join(sc['odd_chars'])}")
        if len(notes) >= limit:
            break
    return notes


def _parse_vote(v):
    sug = str(v.get("hidden_suggestion", "") or "").lower()
    m = re.search(r"word_key:(.+?)\s*(?:\||$)", sug) or re.search(r"\[(.+?)\]", sug)
    if not m:
        return None
    word = m.group(1).strip()

    pk = re.search(r"pick:(.+?)\s*(?:\||$)", sug)
    if pk:
        pick = pk.group(1).strip()
    elif "替代詞:" in sug:
        pick = normalize_word(sug.split("替代詞:")[-1].strip())
        if re.search(r"\s", pick):
            pick = None
    elif v.get("vote_result") == "贊同":
        pick = word
    else:
        pick = None
    rl = re.search(r"rel:(spelling|translation)", sug)
    return word, (pick or None), (rl.group(1) if rl else None)


@st.cache_data(ttl=30)
def load_vote_data():
    empty = {"tally": {}, "group_of": {}, "members": {}, "user_picks": {}, "pick_rows": []}
    try:
        vote_rows = fetch_all_rows("community_votes", "voter_id, vote_result, hidden_suggestion, region, age_group, gender", order=[("id", False)])
    except Exception:
        return empty

    notes = load_word_notes()
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tally, rows = {}, []
    for v in vote_rows:
        parsed = _parse_vote(v)
        if not parsed:
            continue
        word, pick, rel = parsed
        find(word)
        if pick:
            tally[pick] = tally.get(pick, 0) + 1
            if pick != word and rel and link_is_valid(word, pick, rel, notes):
                ra, rb = find(word), find(pick)
                if ra != rb:
                    parent[rb] = ra
        rows.append((v.get("voter_id"), pick, v.get("region"), v.get("age_group"), v.get("gender")))

    try:
        for r in fetch_all_rows("word_variants", "word_a, word_b"):
            a, b = normalize_word(r.get("word_a")), normalize_word(r.get("word_b"))
            if a and b and a != b:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra
    except Exception:
        pass

    roots = {w: find(w) for w in list(parent)}
    members = {}
    for w, r in roots.items():
        members.setdefault(r, []).append(w)
    members = {r: sorted(ws) for r, ws in members.items() if len(ws) > 1}
    group_of = {w: r for w, r in roots.items() if r in members}

    user_picks = {}
    pick_rows = []
    for voter, pick, region, age, gender in rows:
        if pick and pick in group_of:
            pick_rows.append((voter, pick, region, age, gender))
            if voter:
                d = user_picks.setdefault(voter, {}).setdefault(group_of[pick], {})
                d[pick] = d.get(pick, 0) + 1

    return {"tally": tally, "group_of": group_of, "members": members, "user_picks": user_picks, "pick_rows": pick_rows}


def sentence_key(fb_id, sentence_id):
    if fb_id:
        return f"fb:{fb_id}"
    if sentence_id:
        return f"tmp:{sentence_id}"
    return None


def _fetch_user_votes(voter_id):
    rows = fetch_all_rows("community_votes", "feedback_id, hidden_suggestion", eq={"voter_id": voter_id})
    keys = set()
    for v in rows:
        sug = str(v.get("hidden_suggestion", "") or "").lower()
        m = re.search(r"word_key:(.+?)\s*(?:\||$)", sug)
        if not m:
            continue
        word = m.group(1).strip()
        fid = v.get("feedback_id")
        if fid:
            keys.add((f"fb:{fid}", word))
        else:
            s = re.search(r"sent:([0-9a-f]+)", sug)
            if s:
                keys.add((f"tmp:{s.group(1)}", word))
    return keys


@st.cache_data(ttl=30)
def load_user_votes(voter_id):
    try:
        return _fetch_user_votes(voter_id)
    except Exception:
        return set()


@st.cache_data(ttl=60)
def load_user_typed_words(username):
    words = []
    try:
        rows = fetch_all_rows("feedback", eq={"user_id": username})
    except Exception:
        return words
    for r in rows:
        if not is_public_feedback(r):
            continue
        texts = [get_fb_q_tao(r)]
        if r.get("is_edited"):
            texts.append(r.get("tao_text"))
        for t in texts:
            words += [normalize_word(tok) for tok in (t or "").split()]
    return [w for w in words if w]


def canonicalize_ai_data(ai_data, source_text=""):
    if not isinstance(ai_data, dict):
        return ai_data
    for k in ("user_recognized_tao", "ai_reply_tao"):
        if isinstance(ai_data.get(k), str):
            val = canon_apostrophes(ai_data[k])
            if source_text:
                val = preserve_romanized_tokens(val, source_text)
            ai_data[k] = val
    for field in ("etymology", "new_words"):
        if isinstance(ai_data.get(field), dict):
            ai_data[field] = {canon_apostrophes(k): v for k, v in ai_data[field].items()}
    return ai_data


def get_personal_choice(username, current_text=""):
    if not username:
        return {}
    vd = load_vote_data()
    group_of = vd["group_of"]
    if not group_of:
        return {}

    score = {}

    def bump(root, variant, n=1):
        d = score.setdefault(root, {})
        d[variant] = d.get(variant, 0) + n

    typed = list(load_user_typed_words(username)) + [normalize_word(t) for t in (current_text or "").split()]
    for w in typed:
        if w in group_of:
            bump(group_of[w], w)
    for root, picks in vd["user_picks"].get(username, {}).items():
        for variant, n in picks.items():
            bump(root, variant, n)

    best = {}
    for root, d in score.items():
        ranked = sorted(d.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        best[root] = ranked[0][0]
    return best


def get_spelling_prefs(username, current_text=""):
    vd = load_vote_data()
    prefs = {}
    for root, best in get_personal_choice(username, current_text).items():
        for m in vd["members"].get(root, []):
            if m != best:
                prefs[m] = best
    return prefs


def _region_list(value):
    items = value if isinstance(value, (list, tuple)) else str(value or "").split(",")
    return [s.strip() for s in items if s and s.strip()]


@st.cache_data(ttl=60)
def load_variant_stats():
    vd = load_vote_data()
    group_of = vd["group_of"]
    stats, accounts = {}, {}
    if not group_of:
        return stats

    def add(word, acct, region, age, gender):
        s = stats.setdefault(word, {"accounts": 0, "region": {}, "age": {}, "gender": {}})
        accounts.setdefault(word, set()).add(acct)
        for r in _region_list(region):
            s["region"][r] = s["region"].get(r, 0) + 1
        if age:
            s["age"][age] = s["age"].get(age, 0) + 1
        if gender:
            s["gender"][gender] = s["gender"].get(gender, 0) + 1

    try:
        rows = fetch_all_rows("feedback")
    except Exception:
        rows = []
    for r in rows:
        if not is_public_feedback(r):
            continue
        texts = [get_fb_q_tao(r)]
        if r.get("is_edited"):
            texts.append(r.get("tao_text"))
        for w in {normalize_word(t) for tx in texts for t in (tx or "").split()}:
            if w in group_of:
                add(w, r.get("user_id"), r.get("region"), r.get("age_group"), r.get("gender"))
    for voter, pick, region, age, gender in vd.get("pick_rows", []):
        add(pick, voter, region, age, gender)
    for w, accts in accounts.items():
        stats[w]["accounts"] = len({a for a in accts if a})
    return stats


def variant_eligible(word, idx, stats, notes):
    if is_loanword(word, notes):
        return True
    if idx.get(word, {}).get("corpus", 0) > 0:
        return True
    return stats.get(word, {}).get("accounts", 0) >= 2


def choose_default_variants(user_info, skip_roots=()):
    user_info = user_info or {}
    vd = load_vote_data()
    if not vd["members"]:
        return {}
    stats, idx, notes = load_variant_stats(), build_word_index(), load_word_notes()
    regions = set(_region_list(user_info.get("region")))
    age, gender = user_info.get("age_group"), user_info.get("gender")

    out = {}
    for root, members in vd["members"].items():
        if root in skip_roots:
            continue
        elig = [m for m in members if variant_eligible(m, idx, stats, notes)]
        if not elig:
            continue
        usage = {m: idx.get(m, {}).get("count", 0) for m in elig}
        ranked = sorted(elig, key=lambda m: (-usage[m], m))

        def favorite(kind, keys):
            best, best_n = None, 0
            for m in ranked:
                n = sum(stats.get(m, {}).get(kind, {}).get(k, 0) for k in keys if k)
                if n > best_n:
                    best, best_n = m, n
            return best

        favs = [f for f in (favorite("region", regions), favorite("age", {age}), favorite("gender", {gender})) if f]
        out[root] = {"pool": list(dict.fromkeys(ranked[:3] + favs)), "preferred": favs[0] if favs else ranked[0]}
    return out


def get_variant_policy(user_info, current_text=""):
    personal = get_personal_choice((user_info or {}).get("username"), current_text)
    return {"personal": personal, "defaults": choose_default_variants(user_info, skip_roots=set(personal))}


def apply_variant_policy(text, policy):
    """
    依「個人傾向 ＋ 社群用例數」替換句子中的詞彙／異體字拼法：
    1. 個人傾向（personal）優先
    2. 其次社群整體用例數（word_index count + vote tally）與背景偏好（defaults）
    3. 保留全大寫縮寫（如 AI）不被小寫拼法誤改
    """
    if not text:
        return text, {}
    policy = policy or {}
    group_of = load_vote_data()["group_of"]
    idx = build_word_index()
    notes = load_word_notes()
    personal, defaults = policy.get("personal", {}), policy.get("defaults", {})
    out, swaps = [], {}

    for tok in text.split():
        lead, core, trail = split_token(tok)
        if not core or core.isupper():
            out.append(tok)
            continue

        w = normalize_word(core)
        root = group_of.get(w)
        target = None

        if root in personal:
            target = personal[root]
        elif root in defaults and w not in defaults[root]["pool"]:
            target = defaults[root]["preferred"]
        else:
            cur_count = idx.get(w, {}).get("count", 0)
            if cur_count <= 2 and w in notes:
                cur_desc = _desc_norm(notes[w].get("description"))
                best_cand = None
                best_c = cur_count
                for cand_w, n_info in notes.items():
                    if cand_w == w or is_loanword(cand_w, notes):
                        continue
                    if cur_desc and _desc_norm(n_info.get("description")) == cur_desc:
                        c_count = idx.get(cand_w, {}).get("count", 0)
                        if c_count > best_c + 3:
                            best_c = c_count
                            best_cand = cand_w
                if best_cand:
                    target = best_cand

        if target and target != w:
            new = target[:1].upper() + target[1:] if core[:1].isupper() else target
            swaps[core] = new
            tok = f"{lead}{new}{trail}"
        out.append(tok)

    return " ".join(out), swaps


def apply_spelling_prefs(text, prefs):
    if not text or not prefs:
        return text, {}
    out, swaps = [], {}
    for tok in text.split():
        lead, core, trail = split_token(tok)
        new = prefs.get(normalize_word(core)) if core else None
        if new:
            if core[:1].isupper():
                new = new[:1].upper() + new[1:]
            swaps[core] = new
            tok = f"{lead}{new}{trail}"
        out.append(tok)
    return " ".join(out), swaps


def classify_word(count, in_user_input, is_ai_generated, user_accepted):
    if count > HIGHLIGHT_THRESHOLD:
        return "black"
    if count == 0:
        if in_user_input:
            return "yellow"
        return "red"
    return "yellow"


def _plain(text, css_class="tao-w", phrase_group=None):
    if phrase_group:
        st.markdown(
            f'<span class="{css_class}" data-phrase-group="{html.escape(phrase_group)}">{html.escape(text)}</span>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f'<span class="{css_class}">{html.escape(text)}</span>', unsafe_allow_html=True)


def _submit_variant_vote(
    clean_w: str,
    w_display: str,
    pick: str,
    skey: str | None,
    sentence_id: str | None,
    fb_id: int | None,
    current_user: str,
    rel_part: str = "",
) -> tuple[bool, str]:
    """送出單一異體字拼法投票（僅在明確點選時送出）。"""
    load_user_votes.clear()
    if skey and (skey, clean_w) in _fetch_user_votes(current_user):
        return False, f"您已對本句的「{w_display}」投過票，無法重複投票。"

    voter_info = st.session_state.user_info or {}
    voter_region = voter_info.get("region", "未知")
    voter_region_str = ", ".join(voter_region) if isinstance(voter_region, list) else voter_region

    sent_part = f" | sent:{sentence_id}" if (not fb_id and sentence_id) else ""
    suggestion_content = f"word_key:{clean_w}{sent_part} | pick:{pick}{rel_part}"

    vote_payload = {
        "feedback_id": int(fb_id) if fb_id else None,
        "voter_id": current_user,
        "region": voter_region_str,
        "age_group": voter_info.get("age_group", "未知"),
        "gender": voter_info.get("gender", "未知"),
        "vote_result": "贊同" if pick == clean_w else "不贊同",
        "hidden_suggestion": suggestion_content,
        "timestamp": datetime.datetime.now().isoformat(),
    }

    try:
        supabase.from_("community_votes").insert(vote_payload).execute()
    except Exception as fk_err:
        if "null value in column" in str(fk_err).lower() or "23502" in str(fk_err):
            pending_list = st.session_state.setdefault("pending_session_votes", [])
            pending_list.append({
                "sentence_id": sentence_id,
                "payload": vote_payload,
            })
        else:
            raise

    load_vote_data.clear()
    load_user_votes.clear()
    return True, f"🎉 已投給「{pick}」！"


def render_interactive_sentence(
    sentence,
    is_ai_generated=False,
    user_accepted=False,
    etymology_map=None,
    key_prefix="sentence",
    enable_word_voting=False,
    fb_id=None,
    current_user=None,
    user_input_text="",
    sentence_id=None,
    allow_phrase_selection=False,
):
    if not isinstance(etymology_map, dict):
        etymology_map = {}

    skey = sentence_key(fb_id, sentence_id)
    sent_track_id = skey or key_prefix
    merged_sentence, phrase_meta = merge_phrases_in_sentence(sentence or "", sent_key_id=sent_track_id)

    word_index = build_word_index()
    vote_data = load_vote_data()
    notes = load_word_notes()
    raw_user_words = {
        normalize_word(t)
        for t in re.findall(r"[A-Za-z][A-Za-z0-9'’ʼʻ-]*", user_input_text or "")
    } - {""}

    my_votes = load_user_votes(current_user) if (enable_word_voting and current_user) else set()
    tokens_list = merged_sentence.split()

    with st.container(key=f"sent_{key_prefix}"):
        for idx, token in enumerate(tokens_list):
            lead, w, trail = split_token(token)
            if lead:
                _plain(lead, "tao-lead")

            if w:
                clean_w = normalize_word(w)
                p_info = phrase_meta.get(clean_w)
                phrase_group_id = f"{key_prefix}_pg_{clean_w}" if p_info else None

                info = word_index.get(clean_w, {})
                occ_count = info.get("count", 0)
                in_user_raw = clean_w in raw_user_words
                level = classify_word(occ_count, in_user_raw, is_ai_generated, user_accepted)

                if p_info and level == "black":
                    level = "yellow"

                etymology_text = etymology_map.get(w, etymology_map.get(clean_w, ""))
                example_zh = info.get("zh")
                note = notes.get(clean_w)
                gloss = "；".join(info.get("glosses", []))
                detail_rec = get_word_detail_record(clean_w)

                if detail_rec["explanation"]:
                    zh_meaning = detail_rec["explanation"]
                elif note:
                    zh_meaning = note["description"]
                elif p_info:
                    zh_meaning = p_info["meaning"]
                elif gloss:
                    zh_meaning = gloss
                else:
                    zh_meaning = f"例句翻譯: {example_zh}" if example_zh else (etymology_text or "暫無中文說明")

                tag = "hlR" if level == "red" else ("hlY" if level == "yellow" else "hlK")

                group_root = vote_data["group_of"].get(clean_w)
                all_variants_set = {clean_w}
                if group_root:
                    all_variants_set.update(vote_data["members"].get(group_root, []))
                for nw_key in notes.keys():
                    if nw_key == clean_w or (group_root and vote_data["group_of"].get(nw_key) == group_root):
                        all_variants_set.add(nw_key)
                    elif nw_key != clean_w and spelling_similar(clean_w, nw_key) and link_is_valid(clean_w, nw_key, "spelling", notes):
                        all_variants_set.add(nw_key)

                usage = {m: word_index.get(m, {}).get("count", 0) for m in all_variants_set}
                corpus_sum = sum(word_index.get(m, {}).get("corpus", 0) for m in all_variants_set)
                comm_sum = sum(word_index.get(m, {}).get("community", 0) for m in all_variants_set)
                total_variant_usage = sum(usage.values())

                votes_n = {m: vote_data["tally"].get(m, 0) for m in all_variants_set}
                totals = {m: usage[m] + votes_n[m] for m in all_variants_set}
                others_sorted = sorted([m for m in all_variants_set if m != clean_w], key=lambda m: (-totals[m], m))
                variants = [clean_w] + others_sorted

                popover_label = f'<span data-phrase-group="{html.escape(phrase_group_id)}">{html.escape(w)}</span>' if phrase_group_id else w
                with st.popover(popover_label, key=f"{key_prefix}_{tag}_{idx}"):
                    st.markdown(f"### 🔠 單字詳情: **{p_info['phrase'] if p_info else w}**")
                    st.markdown(f"- **用例數（含所有異體字總和）**: `{total_variant_usage}` 次（語料庫 {corpus_sum} ＋ 社群例句 {comm_sum}）")

                    if p_info and current_user:
                        if st.button("✂️ 取消此句中的片語組合", key=f"btn_cancel_sent_phr_{key_prefix}_{idx}"):
                            cancel_phrase_in_sentence(sent_track_id, p_info["phrase"])
                            st.success(f"已取消本句中的片語「{p_info['phrase']}」（其他句子不受影響）。")
                            st.rerun()

                    cur_origin_type = detail_rec["origin_type"] if detail_rec["origin_type"] in ETYMOLOGY_OPTIONS else "固有詞"
                    edit_mode_key = f"word_detail_edit_mode_{key_prefix}_{idx}"
                    is_editing = bool(st.session_state.get(edit_mode_key, False))

                    st.divider()
                    st.markdown(f"- **pipipangayan（達悟文解釋）**：{detail_rec['pipipangayan'] or '（尚未填寫）'}")
                    st.markdown(f"- **解釋**：{zh_meaning}")
                    origin_extra = ""
                    if cur_origin_type == "新造詞" and detail_rec["root_combo"]:
                        origin_extra = f"（詞根組合：{detail_rec['root_combo']}）"
                    elif cur_origin_type == "音譯" and detail_rec["loan_lang"]:
                        origin_extra = f"（語言：{detail_rec['loan_lang']}）"
                    elif cur_origin_type == "固有詞" and detail_rec["root_combo"]:
                        origin_extra = f"（詞根組合：{detail_rec['root_combo']}）"
                    st.markdown(f"- **語源**：{cur_origin_type}{origin_extra}")

                    if current_user and not is_editing:
                        if st.button("✏️ 編輯單字詳情", key=f"btn_enter_edit_{key_prefix}_{idx}"):
                            st.session_state[edit_mode_key] = True
                            st.rerun()

                    if current_user and is_editing:
                        st.markdown("**1. pipipangayan（達悟文的解釋）**")
                        inp_pipi = st.text_input(
                            "pipipangayan",
                            value=detail_rec["pipipangayan"],
                            key=f"pipi_{key_prefix}_{idx}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**2. 解釋**")
                        inp_expl = st.text_input(
                            "解釋",
                            value=zh_meaning if zh_meaning != "暫無中文說明" else "",
                            key=f"expl_{key_prefix}_{idx}",
                            label_visibility="collapsed",
                        )

                        st.markdown("**3. 語源**")
                        sel_origin_type = st.radio(
                            "語源類型",
                            ETYMOLOGY_OPTIONS,
                            index=ETYMOLOGY_OPTIONS.index(cur_origin_type),
                            horizontal=True,
                            key=f"orig_type_{key_prefix}_{idx}",
                            label_visibility="collapsed",
                        )
                        inp_root_combo = ""
                        inp_loan_lang = ""
                        if sel_origin_type == "固有詞":
                            inp_root_combo = st.text_input(
                                "詞根組合（選填）",
                                value=detail_rec["root_combo"],
                                placeholder="選填詞根組合",
                                key=f"root_opt_{key_prefix}_{idx}",
                            )
                        elif sel_origin_type == "新造詞":
                            inp_root_combo = st.text_input(
                                "詞根組合（必填）*",
                                value=detail_rec["root_combo"],
                                placeholder="請填寫詞根組合（必填）",
                                key=f"root_req_{key_prefix}_{idx}",
                            )
                        elif sel_origin_type == "音譯":
                            inp_loan_lang = st.text_input(
                                "何種語言（必填）*",
                                value=detail_rec["loan_lang"],
                                placeholder="例如：華語、日語、英語（必填）",
                                key=f"lang_req_{key_prefix}_{idx}",
                            )

                        col_s, col_c = st.columns(2)
                        with col_s:
                            if st.button("💾 儲存單字詳情", key=f"btn_save_detail_{key_prefix}_{idx}"):
                                ok_d, msg_d = save_word_detail_record(
                                    key=clean_w,
                                    pipipangayan=inp_pipi,
                                    explanation=inp_expl,
                                    origin_type=sel_origin_type,
                                    root_combo=inp_root_combo,
                                    loan_lang=inp_loan_lang,
                                    editor=current_user,
                                )
                                if ok_d:
                                    if inp_expl.strip():
                                        try:
                                            save_word_note(
                                                clean_w,
                                                inp_expl.strip(),
                                                current_user,
                                                tag=LOANWORD_TAG if sel_origin_type == "音譯" else None,
                                            )
                                        except Exception:
                                            pass
                                    st.session_state[edit_mode_key] = False
                                    st.success(msg_d)
                                    st.rerun()
                                else:
                                    st.error(msg_d)
                        with col_c:
                            if st.button("取消", key=f"btn_cancel_edit_{key_prefix}_{idx}"):
                                st.session_state[edit_mode_key] = False
                                st.rerun()

                    st.divider()
                    st.markdown("**4. 例句（雙語）**")
                    bilingual_exs = find_bilingual_examples(clean_w, limit=1)
                    if bilingual_exs:
                        for ex_tao, ex_zh in bilingual_exs:
                            st.markdown(f"- {ex_tao}  \n  {ex_zh}")
                    else:
                        st.caption("目前網站例句中尚無包含此詞／片語的雙語例句。")

                    st.divider()
                    st.markdown("**5. 異體字**")
                    for m in variants:
                        mark = "（本句）" if m == clean_w else ""
                        desc_m = (notes.get(m) or {}).get("description") or "；".join(word_index.get(m, {}).get("glosses", []))
                        desc_part = f"〔{desc_m}〕" if desc_m else ""
                        st.markdown(f"- **{m}**{mark}{desc_part}（用例 {usage[m]}｜投票 {votes_n[m]}｜總計 {totals[m]}）")

                    if enable_word_voting and current_user and skey and (skey, clean_w) in my_votes:
                        st.info(f"✅ 您已對本句的「{w}」投過票了")
                    elif enable_word_voting and current_user:
                        st.caption("點選喜歡的拼法即送出；若要登錄新的拼法，請點選「其他」：")
                        choice_options = variants + ["其他"]
                        pick_state_key = f"var_pick_state_{key_prefix}_{idx}"
                        radio_key = f"w_choice_{key_prefix}_{idx}"

                        if pick_state_key not in st.session_state:
                            st.session_state[pick_state_key] = None
                            st.session_state[radio_key] = None

                        selected_opt = st.radio(
                            "選擇拼法或其他",
                            choice_options,
                            key=radio_key,
                            horizontal=True,
                            label_visibility="collapsed",
                        )

                        prev_recorded = st.session_state.get(pick_state_key)
                        if selected_opt is not None and selected_opt != prev_recorded:
                            st.session_state[pick_state_key] = selected_opt
                            if selected_opt != "其他":
                                try:
                                    ok_v, msg_v = _submit_variant_vote(
                                        clean_w=clean_w,
                                        w_display=w,
                                        pick=selected_opt,
                                        skey=skey,
                                        sentence_id=sentence_id,
                                        fb_id=fb_id,
                                        current_user=current_user,
                                    )
                                    if ok_v:
                                        st.success(msg_v)
                                        st.rerun()
                                    else:
                                        st.warning(msg_v)
                                except Exception as vote_err:
                                    st.error(f"投票失敗：{vote_err}")
                            else:
                                st.rerun()

                        if selected_opt == "其他":
                            w_alt_input = st.text_input(
                                "輸入新的異體字拼法（單一單字）：",
                                key=f"w_alt_{key_prefix}_{idx}",
                                placeholder="例如：apiya",
                            )
                            w_rel = st.radio(
                                "它與本字的關係：",
                                [REL_SPELLING, REL_TRANSLATION],
                                key=f"w_rel_{key_prefix}_{idx}",
                            )
                            w_shared = st.text_input(
                                "兩字共同的中文描述（選「譯詞相同」時必填）：",
                                key=f"w_shared_{key_prefix}_{idx}",
                                value=note["description"] if note else "",
                            )

                            if st.button("送出其他拼法", key=f"btn_w_vote_{key_prefix}_{idx}"):
                                try:
                                    alt_raw = w_alt_input.strip()
                                    if not alt_raw:
                                        raise ValueError("請輸入新的異體字拼法。")
                                    if re.search(r"\s", alt_raw):
                                        raise ValueError("提出的字請只輸入單一單字。")
                                    alt = normalize_word(alt_raw)
                                    if not alt:
                                        raise ValueError("提出的字無效。")

                                    rel_part = ""
                                    if alt != clean_w:
                                        rel = "spelling" if w_rel == REL_SPELLING else "translation"
                                        load_word_notes.clear()
                                        ok, msg, to_save = check_variant_proposal(clean_w, alt, rel, w_shared, load_word_notes())
                                        if not ok:
                                            raise ValueError(msg)
                                        for ww, dd in to_save.items():
                                            save_word_note(ww, dd, current_user)
                                        rel_part = f" | rel:{rel}"

                                    ok_v, msg_v = _submit_variant_vote(
                                        clean_w=clean_w,
                                        w_display=w,
                                        pick=alt,
                                        skey=skey,
                                        sentence_id=sentence_id,
                                        fb_id=fb_id,
                                        current_user=current_user,
                                        rel_part=rel_part,
                                    )
                                    if ok_v:
                                        st.session_state[pick_state_key] = alt
                                        st.success(msg_v)
                                        st.rerun()
                                    else:
                                        st.warning(msg_v)
                                except Exception as vote_err:
                                    st.error(f"送出失敗：{vote_err}")

            if trail:
                _plain(trail, "tao-punct")

    if allow_phrase_selection and current_user and len(tokens_list) >= 2:
        word_candidates = []
        for idx_c, tok_c in enumerate(tokens_list):
            _, core_c, _ = split_token(tok_c)
            if core_c:
                word_candidates.append((idx_c, core_c))
        if len(word_candidates) >= 2:
            with st.popover("🧩 在此句中選取組合登錄為片語", key=f"pop_sel_phrase_{key_prefix}"):
                picked_indices = st.multiselect(
                    "勾選要組合成片語的詞彙（依出現順序組合）：",
                    options=[i for i, _ in word_candidates],
                    format_func=lambda i: f"{i+1}. {word_candidates[[x for x,_ in word_candidates].index(i)][1]}",
                    key=f"ms_phrase_{key_prefix}",
                )
                phr_meaning_inp = st.text_input(
                    "片語中文解釋（選填）：",
                    key=f"inp_phr_mean_{key_prefix}",
                    placeholder="例如：蘭嶼",
                )
                if st.button("✅ 登錄為片語", key=f"btn_reg_phr_{key_prefix}"):
                    ordered = [word_candidates[[x for x, _ in word_candidates].index(i)][1] for i in sorted(picked_indices)]
                    joined_phrase = " ".join(ordered)
                    if register_phrase(joined_phrase, meaning=phr_meaning_inp, source="manual"):
                        st.success(f"已登錄片語「{joined_phrase}」，點擊文句中對應詞彙即可編輯單字詳情！")
                        st.rerun()
                    else:
                        st.warning("請至少勾選 2 個詞彙。")


# --- 6. 雙軌邏輯 ---
def process_ai_input(text_prompt=None, username=None, user_profile=None):
    origin = detect_input_origin(text_prompt)
    roman_tokens = extract_romanized_tokens(text_prompt)

    if text_prompt:
        algo_result = algorithm_first_processing(text_prompt, username=username)
        if algo_result:
            algo_result["origin"] = origin
            return algo_result

    if not text_prompt:
        return None

    api_key = st.secrets.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        st.error("❌ 找不到 GOOGLE_API_KEY，請檢查 Secrets 設定")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    legal_corpus = search_relevant_corpus(text_prompt, top_k=3, username=username)
    corpus_context = (
        "\n".join(legal_corpus)
        if legal_corpus
        else "【目前尚無合規的參考語料】"
    )

    notes_context = build_notes_context(text_prompt, *legal_corpus) or "（無）"
    dictionary_context = build_dictionary_context(text_prompt) or "（無）"
    loanword_context = build_loanword_context() or "（無）"

    w_idx = build_word_index()
    top_comm_words = [
        f"{w}（用例 {info.get('count', 0)}）"
        for w, info in sorted(w_idx.items(), key=lambda kv: -kv[1].get("count", 0))[:15]
    ]
    community_freq_hint = "、".join(top_comm_words) if top_comm_words else "（無）"

    personal_map = get_personal_choice(username, text_prompt) if username else {}
    personal_pref_hint = (
        "、".join(f"→ {v}" for v in personal_map.values()) if personal_map else "（無特定偏好）"
    )

    roman_rule = ""
    if roman_tokens and origin == "zh":
        roman_rule = (
            f"\n6. 使用者輸入的中文裡包含以下羅馬字拼法：{', '.join(roman_tokens)}。"
            f"在轉譯或生成達悟語句子時，若用到這些概念，必須原封不動保留該羅馬字拼法（例如保留「{roman_tokens[0]}」），不可改寫成其他音譯拼法或改變大小寫。"
        )

    system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。目前處於「AI 備援生成」模式。
公開句子只要進入搜尋池，即主要參照句中的文法與句型結構；但回應中的單字與拼法選用，請優先依循「個人傾向」與「社群用例數」，盡量採用個人常用或社群高頻的拼法或單字。

【可搜尋語料句（主要參照文法與句型）】:
{corpus_context}

【個人詞彙／拼法傾向】: {personal_pref_hint}
【社群高用例數詞彙參考】: {community_freq_hint}

【使用者已校正的單字描述（含語源標註；回應或 etymology 用到這些字時，請沿用對應語源與描述）】:
{notes_context}

【單字表釋義（詞典；含語源標註，若與上面使用者校正的描述衝突，以校正描述為準）】:
{dictionary_context}

【現代借詞表（社群已採用；遇到同形詞如 ai / AI 或 ito 等，請依上下文語意區分固有詞與音譯詞，不可誤判）】:
{loanword_context}

【絕對強制規則】：
1. 請勿在生成的達悟語句子中包含任何如 `[使用者推薦]` 或 `[AI自創]` 的文字標籤，保持句子乾淨。
2. etymology 必須是一個 JSON 物件 (Dictionary)，鍵為達悟語單字，值為該單字的語源與中文翻譯說明（例如：{{"akokay": "[固有詞] 你好 / 傳統問候語", "ito": "[固有詞] 他、那個（另見音譯詞棉線）"}}）。絕不可回傳字串。
3. 喉塞音（glottal stop）一律使用半形撇號 '（U+0027）書寫，例如 ma'ogto；不要改用 ’ ‘ ʼ ʻ ` 等其他符號，也不要自行增減喉塞音。
4. 遇到達悟語沒有對應詞的現代概念（例如 演算法、網際網路），絕對不要自創達悟語詞彙。一律以華語（國語）的發音音譯，並依達悟語書寫系統的字母書寫（例如 電腦→tennaw、電話→dingwa、國中→kocong、高中→kawcong、國小→kosiaw）。「現代借詞表」已有的詞請直接使用，但若遇到與達悟語固有詞同音或同拼法者，必須依句子語意避免混淆。
5. 音譯而來的新詞請列在 new_words（鍵為音譯後的達悟語字，值為華語原詞；沒有就回傳空物件 {{}}），並在 etymology 中寫明「[音譯] 華語音譯：華語原詞」。{roman_rule}

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的純達悟語句子",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子",
  "ai_reply_zh": "回應之中文翻譯",
  "etymology": {{
     "關鍵單字": "[語源] 中文翻譯與語源說明"
  }},
  "new_words": {{
     "音譯後的達悟語字": "華語原詞"
  }},
  "is_ai_generated": true,
  "reference": "必須明確列出引用的語料庫ID與出處"
}}
"""

    contents = [system_prompt, f"使用者輸入文字：{text_prompt}"]

    max_retries = 2
    for attempt in range(max_retries):
        try:
            response = model.generate_content(
                contents,
                generation_config={
                    "response_mime_type": "application/json",
                    "temperature": 0.0,
                },
            )
            result = json.loads(response.text)

            if "user_recognized_tao" in result:
                result["user_recognized_tao"] = lower_first_char(result["user_recognized_tao"])
            if "ai_reply_tao" in result:
                result["ai_reply_tao"] = lower_first_char(result["ai_reply_tao"])
            if "is_ai_generated" not in result:
                result["is_ai_generated"] = False

            if not isinstance(result.get("etymology"), dict):
                result["etymology"] = {}
            if not isinstance(result.get("new_words"), dict):
                result["new_words"] = {}
            if result["new_words"]:
                result["is_ai_generated"] = True

            result["origin"] = origin
            return result
        except ResourceExhausted:
            if attempt < max_retries - 1:
                time.sleep(2)
            else:
                st.error("❌ 請求超出 API 配額限制，請稍候再試。")
                return None
        except Exception as e:
            st.error(f"❌ 辨識失敗：{e}")
            return None


# --- 全域選項選單 ---
REGION_OPTIONS = [
    "Jiyayo 椰油", "Jiraralay 朗島", "Jiranmilek 東清", "Jivalino 野銀",
    "Jimowrod 紅頭", "Jiratay 漁人", "Ivatan 巴丹", "Ji Taywan 台灣", "ilaod 其他",
]
AGE_OPTIONS = [
    "alikey 學齡前", "kosiaw 國小", "kocong aka kawcong 國高中", "18-30",
    "31-40", "41-50", "51-60", "61-70", "71-80", "ikaroa a ngernan o kakawakawan 81歲及以上",
]
GENDER_OPTIONS = ["mehakay 男", "mavakes 女", "ji nipanci 不公開"]


# --- 7. 側邊欄：登入與註冊 ---
with st.sidebar:
    st.markdown(dual_text("vahay do kararay", "會員中心", is_title=True), unsafe_allow_html=True)
    st.caption("☁️ 雲端資料庫已連線")
    if st.session_state.user_info is None:
        auth_choice = st.radio("請選擇：", ["登入", "註冊帳號"])
        if auth_choice == "登入":
            st.markdown(dual_text("ngaran", "帳號"), unsafe_allow_html=True)
            login_username = st.text_input("帳號輸入框", label_visibility="collapsed")
            st.markdown(dual_text("kagi", "密碼"), unsafe_allow_html=True)
            login_password = st.text_input("密碼輸入框", type="password", label_visibility="collapsed")
            remember_me = st.checkbox("🔒 在此裝置記住帳號（下次自動登入）", value=True, key="remember_login_cb")
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
                    if remember_me:
                        issue_auto_login(res[0]["username"])
                    else:
                        clear_auto_login(res[0]["username"])
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

            st.markdown(dual_text("ili", "部落 (可複選)"), unsafe_allow_html=True)
            new_r = st.multiselect("部落選擇", REGION_OPTIONS, default=["Jiyayo 椰油"], label_visibility="collapsed")
            st.markdown(dual_text("kakawakawan", "年齡"), unsafe_allow_html=True)
            new_a = st.selectbox("年齡選擇", AGE_OPTIONS, label_visibility="collapsed")
            st.markdown(dual_text("mikataretarek", "性別"), unsafe_allow_html=True)
            new_g = st.selectbox("性別選擇", GENDER_OPTIONS, label_visibility="collapsed")
            if st.button("mamareng 註冊"):
                if not new_r:
                    st.warning("請至少選擇一個部落。")
                elif add_user(new_u, new_p, new_e, new_r, new_a, new_g):
                    st.success("註冊成功，請切換登入。")
                else:
                    st.warning("帳號已被註冊。")
    else:
        u = st.session_state.user_info
        st.success(f"ngaran 登入身分：**{u['username']}**")
        if st.button("makowbot 🚪 登出"):
            clear_auto_login(u["username"])
            st.session_state.user_info = None
            st.rerun()


# --- 8. 主頁面區塊 ---
main_menu = st.radio(
    "",
    [
        "miAI ko / 我要用AI",
        "manita so tao a miAI / 看別人用AI",
        "amian so AI ori / 關於本站",
    ],
    horizontal=True,
)

if main_menu == "miAI ko / 我要用AI":
    st.markdown(
        dual_text("macisirisiring do AI kano misinsinmo so vakong no AI", "演算法與 AI 備援對話語料採集", is_title=True),
        unsafe_allow_html=True,
    )
    if st.session_state.user_info is None:
        st.warning("🔒 本系統需登入後使用，請先在左側邊欄登入。")
    else:
        user = st.session_state.user_info

        saved_region = user["region"]
        if isinstance(saved_region, str):
            user_regions = [r.strip() for r in saved_region.split(",")]
        else:
            user_regions = saved_region
        default_regions = [r for r in user_regions if r in REGION_OPTIONS]
        if not default_regions:
            default_regions = ["Jiyayo 椰油"]

        c1, c2, c3 = st.columns(3)
        with c1:
            st.markdown(dual_text("ili", "部落 (可複選)"), unsafe_allow_html=True)
            cur_r = st.multiselect("ili", REGION_OPTIONS, default=default_regions, label_visibility="collapsed")
        with c2:
            st.markdown(dual_text("kakawakawan", "年齡"), unsafe_allow_html=True)
            cur_a = st.selectbox("kakawakawan", AGE_OPTIONS, index=AGE_OPTIONS.index(user["age_group"]) if user["age_group"] in AGE_OPTIONS else 3, label_visibility="collapsed")
        with c3:
            st.markdown(dual_text("mikataretarek", "性別"), unsafe_allow_html=True)
            cur_g = st.selectbox("mikataretarek", GENDER_OPTIONS, index=GENDER_OPTIONS.index(user["gender"]) if user["gender"] in GENDER_OPTIONS else 0, label_visibility="collapsed")
        current_bg = {"region": cur_r, "age_group": cur_a, "gender": cur_g}

        st.divider()

        st.markdown(dual_text("mivatvatek", "請輸入文字"), unsafe_allow_html=True)
        text_input = st.text_input("text_input_box", placeholder="akokay…／你好…", label_visibility="collapsed")
        if st.button("itoro 發送查詢"):
            if text_input.strip():
                with st.spinner("⏳ 演算法檢索與備援處理中..."):
                    ai_data = process_ai_input(
                        text_prompt=text_input,
                        username=st.session_state.user_info["username"],
                        user_profile=current_bg,
                    )
                    if ai_data:
                        st.session_state.active_q = text_input
                        ai_data = canonicalize_ai_data(ai_data, source_text=text_input)
                        ai_data["sentence_id"] = uuid.uuid4().hex

                        policy = get_variant_policy(
                            {**st.session_state.user_info, **current_bg},
                            text_input,
                        )
                        ai_data["ai_reply_tao_raw"] = ai_data.get("ai_reply_tao", "")
                        swapped_tao, swaps = apply_variant_policy(ai_data.get("ai_reply_tao", ""), policy)
                        ai_data["ai_reply_tao"] = preserve_romanized_tokens(swapped_tao, text_input)
                        if swaps:
                            ety = ai_data.get("etymology") if isinstance(ai_data.get("etymology"), dict) else {}
                            for old, new in swaps.items():
                                if old in ety and new not in ety:
                                    ety[new] = ety[old]
                            ai_data["etymology"] = ety
                            ai_data["spelling_swaps"] = swaps

                        auto_detect_and_save_phrases(
                            f"{text_input} {ai_data.get('user_recognized_tao', '')} {ai_data.get('ai_reply_tao', '')}",
                            f"{ai_data.get('user_translation', '')} {ai_data.get('ai_reply_zh', '')}",
                        )
                        st.session_state.ai_data = ai_data
                        st.session_state.active_bg = current_bg

        if "ai_data" in st.session_state and st.session_state.ai_data:
            ai_data = st.session_state.ai_data
            q_orig = st.session_state.get("active_q", "")
            bg_info = st.session_state.get("active_bg", current_bg)
            input_origin = ai_data.get("origin") or detect_input_origin(q_orig)

            reg_val = bg_info["region"]
            reg_str = ", ".join(reg_val) if isinstance(reg_val, list) else reg_val
            user_profile_str = f"部落: {reg_str} | 年齡層: {bg_info['age_group']} | 性別: {bg_info['gender']} | 原始輸入語言: {input_origin}"
            is_ai = ai_data.get("is_ai_generated", False)

            st.markdown("---")
            user_sentence = ai_data.get("user_recognized_tao", q_orig)
            with st.container(border=True):
                st.markdown("<div style='font-size: 18px; font-weight: bold;'>maniring o tao am (輸入與辨識)</div>", unsafe_allow_html=True)
                render_interactive_sentence(
                    user_sentence,
                    is_ai_generated=False,
                    user_accepted=True,
                    etymology_map=ai_data.get("etymology", {}),
                    key_prefix="main_user",
                    enable_word_voting=False,
                    current_user=st.session_state.user_info["username"],
                    user_input_text=q_orig,
                    allow_phrase_selection=True,
                )
                st.markdown(f"<div style='font-size: 14px; opacity: 0.8;'>{ai_data.get('user_translation', '')}</div>", unsafe_allow_html=True)
                _notes_user = spellcheck_notes(user_sentence)
                if _notes_user:
                    st.caption("🔎 拼寫檢查：" + "；".join(_notes_user))

            st.markdown("##### maniring o AI/演算法 am (回應結果)")
            render_interactive_sentence(
                ai_data.get("ai_reply_tao", ""),
                is_ai_generated=is_ai,
                user_accepted=False,
                etymology_map=ai_data.get("etymology", {}),
                key_prefix="main_reply",
                enable_word_voting=True,
                current_user=st.session_state.user_info["username"],
                user_input_text=q_orig,
                sentence_id=ai_data.get("sentence_id"),
                allow_phrase_selection=True,
            )
            st.markdown(f"<div style='font-size: 14px; opacity: 0.8; margin-top: 10px; margin-bottom: 15px;'>{ai_data.get('ai_reply_zh', '')}</div>", unsafe_allow_html=True)
            if ai_data.get("spelling_swaps"):
                st.caption("已依個人傾向與社群用例數調整詞彙／拼法：" + "、".join(f"{o} → {n}" for o, n in ai_data["spelling_swaps"].items()))
            _notes_reply = spellcheck_notes(ai_data.get("ai_reply_tao", ""))
            if _notes_reply:
                st.caption("🔎 拼寫檢查：" + "；".join(_notes_reply))

            st.markdown(
                f"""
                <div style="padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid rgba(128, 128, 128, 0.3);">
                    <div style="font-size: 18px; font-weight: bold;">o pakaaranan no ciring (參考資料 / 語料來源)</div>
                    <div style="font-size: 16px; font-weight: bold; margin-top: 5px;">{user_profile_str}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )

            st.divider()
            st.markdown(dual_text("o kapakaapiya no ciring", "語料品質評估", is_title=True), unsafe_allow_html=True)

            visibility_choice = st.radio(
                "儲存位置與公開設定：",
                [
                    "🌐 公開（存入 feedback，其他使用者可在「看別人用AI」看到，並納入公開演算法搜尋）",
                    "🔒 私人（僅存入個人私人紀錄，不寫入 feedback；僅供您自己的演算法讀取）",
                ],
                horizontal=True,
                key="save_visibility_radio",
            )
            is_public_selected = visibility_choice.startswith("🌐")

            eval_choice = st.radio(
                "eval_choice_radio",
                ["apiya / 正確 (直接採納)", "beken / 錯誤 (我要修改)"],
                horizontal=True,
                label_visibility="collapsed",
            )

            if "apiya" in eval_choice:
                if st.button("voka ✅ 直接送出儲存"):
                    final_q_tao = canon_apostrophes((ai_data.get("user_recognized_tao") or q_orig).strip())
                    final_q_zh = ai_data.get("user_translation") or ""
                    final_r_tao = preserve_romanized_tokens(
                        canon_apostrophes((ai_data.get("ai_reply_tao") or "").strip()),
                        q_orig,
                    )
                    if save_語料_to_supabase(
                        st.session_state.user_info,
                        bg_info,
                        final_q_tao,
                        final_q_zh,
                        final_r_tao,
                        ai_data.get("ai_reply_zh"),
                        is_edited=False,
                        is_error=False,
                        is_public=is_public_selected,
                        origin=input_origin,
                        pending_sentence_id=ai_data.get("sentence_id"),
                    ):
                        if is_public_selected:
                            register_new_words(ai_data, final_r_tao, st.session_state.user_info["username"])
                        st.success("🎉 已成功儲存至私人紀錄！" if not is_public_selected else "🎉 已成功儲存至 feedback！")
                        del st.session_state.ai_data
                        st.rerun()

            elif "beken" in eval_choice:
                st.warning("⚠ 發現錯誤。您可以修正文字，並保留修正前的 AI 原句：")

                default_orig_ai_q = ai_data.get("user_recognized_tao", q_orig)
                default_orig_ai_a = ai_data.get("ai_reply_tao", "")

                st.markdown("**修正前 AI 原句（輸入／辨識）**")
                orig_ai_q_inp = st.text_input("修正前 AI 輸入／辨識原句：", value=default_orig_ai_q, key="orig_ai_q_input")
                st.markdown("**修正前 AI 原句（回應）**")
                orig_ai_a_inp = st.text_input("修正前 AI 回應原句：", value=default_orig_ai_a, key="orig_ai_a_input")

                st.markdown("**ablisan o pacici**<br><span style='font-size:14px; opacity:0.8;'>修正【問題】</span>", unsafe_allow_html=True)
                e_q_tao = st.text_input("修改句子 1 達悟語：", value=ai_data.get("user_recognized_tao", q_orig))
                st.markdown("**ablisan o mivaliw no pacici**<br><span style='font-size:14px; opacity:0.8;'>修改問題翻譯</span>", unsafe_allow_html=True)
                e_q_trans = st.text_input("修改句子 1 翻譯：", value=ai_data.get("user_translation", ""))

                st.markdown("**ablisan o tovis**<br><span style='font-size:14px; opacity:0.8;'>修正【回應】</span>", unsafe_allow_html=True)
                e_r_tao = st.text_input("修改句子 2 達悟語：", value=ai_data.get("ai_reply_tao", ""))
                st.markdown("**ablisan o mivaliw no tovis**<br><span style='font-size:14px; opacity:0.8;'>修改回應翻譯</span>", unsafe_allow_html=True)
                e_r_zh = st.text_input("修改句子 2 翻譯：", value=ai_data.get("ai_reply_zh", ""))

                if st.button("voka 💾 儲存修正版"):
                    clean_e_q_tao = canon_apostrophes(e_q_tao.strip())
                    clean_e_r_tao = canon_apostrophes(e_r_tao.strip())
                    clean_orig_ai_q = canon_apostrophes((orig_ai_q_inp or "").strip())
                    clean_orig_ai_a = canon_apostrophes((orig_ai_a_inp or "").strip())
                    if save_語料_to_supabase(
                        st.session_state.user_info,
                        bg_info,
                        clean_e_q_tao,
                        e_q_trans,
                        clean_e_r_tao,
                        e_r_zh,
                        is_edited=True,
                        is_error=True,
                        is_public=is_public_selected,
                        origin=input_origin,
                        orig_ai_q_tao=clean_orig_ai_q,
                        orig_ai_a_tao=clean_orig_ai_a,
                        edit_record=build_edit_record(
                            ai_data, q_orig,
                            clean_e_q_tao, e_q_trans, clean_e_r_tao, e_r_zh,
                        ),
                    ):
                        if is_public_selected:
                            register_new_words(ai_data, clean_e_r_tao, st.session_state.user_info["username"])
                        st.success("🎉 已成功儲存私人修正版！" if not is_public_selected else "🎉 已成功更新修正版")
                        del st.session_state.ai_data
                        st.rerun()

        # --- 使用者自己的歷史對話紀錄（含編輯、公開/私人切換、刪除） ---
        st.divider()
        st.markdown(dual_text("no ciriciring mo a nimareng", "我的歷史對話紀錄", is_title=True), unsafe_allow_html=True)
        try:
            public_my_history = fetch_all_rows(
                "feedback",
                order=[("timestamp", True), ("id", True)],
                eq={"user_id": user["username"]},
            )
            combined_history = []
            for item in public_my_history:
                rec = dict(item)
                rec["_source_table"] = "feedback"
                combined_history.append(rec)

            for priv_item in fetch_user_private_records(user["username"]):
                rec = dict(priv_item)
                rec["_source_table"] = "private"
                rec["is_public"] = False
                combined_history.append(rec)

            combined_history.sort(key=lambda x: str(x.get("timestamp") or ""), reverse=True)

            if not combined_history:
                st.info("您目前尚未儲存任何對話紀錄。")
            else:
                st.caption(f"共 {len(combined_history)} 筆對話紀錄")
                for idx_h, item in enumerate(combined_history):
                    item_id = item.get("id")
                    is_priv = item.get("_source_table") == "private" or not is_public_feedback(item)
                    pub_badge = "🔒 私人" if is_priv else "🌐 公開"
                    q_tao_val = get_fb_q_tao(item)
                    q_zh_val = get_fb_q_zh(item)
                    r_tao_val = item.get("tao_text") or ""
                    r_zh_val = item.get("zh_text") or ""
                    orig_ai_q_val = item.get("orig_ai_q_tao") or ""
                    orig_ai_a_val = item.get("orig_ai_a_tao") or ""
                    item_origin = item.get("origin") or detect_input_origin(q_tao_val)
                    ts_str = str(item.get("timestamp") or "")[:19].replace("T", " ")
                    row_uid = f"{item.get('_source_table')}_{item_id}_{idx_h}"

                    with st.expander(f"💬 #{item_id}｜{pub_badge}｜origin: {item_origin}｜{ts_str}｜{q_tao_val}"):
                        st.markdown(f"**問 (q_tao)**：{q_tao_val}")
                        if q_zh_val:
                            st.caption(f"翻譯 (q_zh)：{q_zh_val}")
                        st.markdown(f"**答 (tao_text)**：{r_tao_val}")
                        if r_zh_val:
                            st.caption(f"回應翻譯 (zh_text)：{r_zh_val}")
                        if orig_ai_q_val or orig_ai_a_val:
                            st.caption(f"修正前 AI 原句｜問：{orig_ai_q_val}｜答：{orig_ai_a_val}")
                        st.caption(
                            f"狀態：{pub_badge}｜原始語言 (origin)：{item_origin}｜"
                            f"部落：{item.get('region') or ''}｜年齡：{item.get('age_group') or ''}｜性別：{item.get('gender') or ''}"
                        )

                        with st.popover("✏️ 編輯文句、翻譯或公開／私人", key=f"pop_edit_{row_uid}"):
                            st.markdown("#### 編輯此筆對話紀錄")
                            ed_q_tao = st.text_input("問題文句（達悟語／原句）", value=q_tao_val, key=f"ed_qt_{row_uid}")
                            ed_q_zh = st.text_input("問題翻譯", value=q_zh_val, key=f"ed_qz_{row_uid}")
                            ed_r_tao = st.text_input("回應文句（達悟語）", value=r_tao_val, key=f"ed_rt_{row_uid}")
                            ed_r_zh = st.text_input("回應翻譯", value=r_zh_val, key=f"ed_rz_{row_uid}")
                            ed_vis = st.radio(
                                "公開狀態：",
                                ["🌐 公開", "🔒 私人"],
                                index=1 if is_priv else 0,
                                horizontal=True,
                                key=f"ed_vis_{row_uid}",
                            )
                            if st.button("💾 儲存修改", key=f"btn_save_ed_{row_uid}"):
                                auto_detect_and_save_phrases(f"{ed_q_tao} {ed_r_tao}", f"{ed_q_zh} {ed_r_zh}")
                                ok_upd, msg_upd = update_existing_conversation(
                                    existing_item=item,
                                    user_info=user,
                                    new_q_tao=canon_apostrophes(ed_q_tao.strip()),
                                    new_q_zh=ed_q_zh.strip(),
                                    new_r_tao=canon_apostrophes(ed_r_tao.strip()),
                                    new_r_zh=ed_r_zh.strip(),
                                    target_public=ed_vis.startswith("🌐"),
                                )
                                if ok_upd:
                                    st.success(msg_upd)
                                    st.rerun()
                                else:
                                    st.error(msg_upd)

                        if st.button(f"🗑️ 刪除此筆對話 (#{item_id})", key=f"del_my_fb_{row_uid}"):
                            if is_priv:
                                ok_del, msg_del = delete_private_record(item, user["username"])
                            else:
                                ok_del, msg_del = delete_feedback_item(item_id, user["username"])
                            if ok_del:
                                st.success(msg_del)
                                st.rerun()
                            else:
                                st.error(msg_del)
        except Exception as hist_err:
            st.error(f"讀取您的歷史對話失敗：{hist_err}")

elif main_menu == "manita so tao a miAI / 看別人用AI":
    st.markdown(
        dual_text("kapisirisiring kano manita o vakong no ciring", "社群驗證與單字用法投票", is_title=True),
        unsafe_allow_html=True,
    )
    current_user = st.session_state.user_info["username"] if st.session_state.user_info else None

    with st.expander("🧪 語料庫檢索狀態"):
        try:
            _raw = load_all_raw_corpus()
            _pending = [it for it in _raw if is_unverified(it)]
            _corpus_only = [it for it in _raw if not it.get("is_feedback") and not is_unverified(it)]
            _pub_search_only = [it for it in _raw if it.get("is_feedback") and not is_unverified(it)]
            _widx = build_word_index()
            st.markdown(
                f"- 語料庫：**{len(_corpus_only)}** 筆；已公開納入演算法搜尋的社群句子：**{len(_pub_search_only)}** 筆\n"
                f"- 本站不同單字：**{len(_widx)}** 個；用例超過 {HIGHLIGHT_THRESHOLD} 次為黑色一般詞彙（可點擊查看詳情），其餘會標示螢光"
            )
            if not _corpus_only:
                _d = diagnose_corpus_table()
                if _d["error"]:
                    st.error(f"查詢 corpus 資料表時發生錯誤：{_d['error']}")
                elif not _d["count"]:
                    st.error("查詢成功，但 corpus 讀到 0 筆。")
                else:
                    load_all_raw_corpus.clear()
                    build_word_index.clear()
                    st.warning("快取已自動清除，請重新整理頁面。")
            else:
                _empty = sum(1 for it in _corpus_only if not it["tao"].strip())
                if _empty == len(_corpus_only):
                    st.error("讀到了語料列，但達悟語欄位全是空的。")
                elif _empty:
                    st.warning(f"有 {_empty} 筆語料的達悟語欄位是空的。")
                _top = sorted(_widx.items(), key=lambda kv: -kv[1]["count"])[:10]
                st.markdown("- 最常出現的字：" + "、".join(f"{w}（{e['count']}）" for w, e in _top))
        except Exception as diag_err:
            st.error(f"診斷失敗：{diag_err}")
        if st.button("🔄 重新載入語料庫", key="reload_corpus_cache"):
            load_all_raw_corpus.clear()
            build_word_index.clear()
            load_vote_data.clear()
            load_word_notes.clear()
            _spell_resources.clear()
            load_variant_stats.clear()
            spell_check_word.clear()
            _load_word_details_cached.clear()
            find_bilingual_examples.clear()
            load_personal_private_corpus.clear()
            st.rerun()

    try:
        all_feedbacks = fetch_all_rows("feedback", order=[("timestamp", True), ("id", True)])
        feedbacks = [fb for fb in all_feedbacks if is_public_feedback(fb)]

        if not feedbacks:
            st.info("目前尚無公開的社群語料。")
        else:
            st.markdown("#### 🔍 檢索單字用例")
            search_raw = st.text_input(
                "輸入單字，查找包含該字的用例（留空則顯示全部）",
                key="fb_search_word",
                placeholder="",
            )
            query = normalize_word(search_raw.split()[0]) if search_raw.strip() else ""
            shown = feedbacks

            if query:
                vd = load_vote_data()
                notes = load_word_notes()
                site_count = build_word_index().get(query, {}).get("count", 0)

                tok_sets = {
                    fb["id"]: {normalize_word(t) for t in f"{get_fb_q_tao(fb)} {fb.get('tao_text') or ''}".split()}
                    for fb in feedbacks
                }
                root = vd["group_of"].get(query)
                variants = [m for m in vd["members"].get(root, []) if m != query]
                counts = {m: sum(1 for ts in tok_sets.values() if m in ts) for m in [query] + variants}

                st.markdown(
                    f"**「{query}」** 共有 **{counts[query]}** 筆公開社群例句"
                    f"（本站總用例 {site_count} 次，含語料庫與公開社群例句）"
                )

                include_variants = False
                if variants:
                    lines = []
                    for m in variants:
                        desc_m = (notes.get(m) or {}).get("description") or "；".join(build_word_index().get(m, {}).get("glosses", []))
                        lines.append(f"- **{m}**{f'〔{desc_m}〕' if desc_m else ''}：{counts[m]} 筆")
                    st.markdown("🔀 **有異體字**\n" + "\n".join(lines))
                    include_variants = st.checkbox("同時顯示異體字的用例", value=True, key="fb_search_variants")
                else:
                    st.caption("目前沒有異體字（尚無人提出，或提出的字未通過異體字判定）。")

                forms = {x for x in ({query, *variants} if include_variants else {query})}
                shown = [fb for fb in feedbacks if tok_sets[fb["id"]] & forms]

                corpus_pool = [it for it in load_all_raw_corpus() if not it.get("is_feedback") and not is_unverified(it)]
                corpus_hits = [
                    it for it in corpus_pool
                    if forms & {normalize_word(t) for t in (it.get("tao") or "").split()}
                ]
                st.markdown(f"📚 語料庫：**{len(corpus_hits)}** 筆句子")
                if corpus_hits:
                    with st.expander("查看與編輯語料庫例句"):
                        for idx_c, it in enumerate(corpus_hits[:20]):
                            c_raw = it.get("corpus_raw") or {}
                            c_id = it.get("corpus_id") or c_raw.get("id")
                            st.markdown(f"- **#{c_id}**：{it['tao']} — {it['zh']}")
                            if current_user and c_raw:
                                with st.popover(f"✏️ 編輯語料庫句子 #{c_id}", key=f"pop_edit_corpus_{c_id}_{idx_c}"):
                                    ed_c_tao = st.text_input(
                                        "達悟語句子（修改為更自然的達悟語）",
                                        value=it.get("tao") or "",
                                        key=f"ed_c_tao_{c_id}_{idx_c}",
                                    )
                                    ed_c_zh = st.text_input(
                                        "中文翻譯",
                                        value=it.get("zh") or "",
                                        key=f"ed_c_zh_{c_id}_{idx_c}",
                                    )
                                    if st.button("💾 儲存語料庫修改", key=f"btn_save_c_{c_id}_{idx_c}"):
                                        ok_c, msg_c = update_corpus_sentence(
                                            c_raw,
                                            canon_apostrophes(ed_c_tao),
                                            ed_c_zh,
                                            current_user,
                                        )
                                        if ok_c:
                                            st.success(msg_c)
                                            st.rerun()
                                        else:
                                            st.error(msg_c)
                        if len(corpus_hits) > 20:
                            st.caption(f"僅顯示前 20 筆，共 {len(corpus_hits)} 筆。")
                if include_variants:
                    st.markdown(f"合計 **{len(shown)}** 筆（同一筆不重複計算）")
                if not shown:
                    st.info(f"找不到包含「{query}」的公開社群例句。")
                st.divider()
            elif current_user:
                corpus_all = [it for it in load_all_raw_corpus() if not it.get("is_feedback") and not is_unverified(it) and it.get("corpus_raw")]
                if corpus_all:
                    with st.expander("✏️ 編輯語料庫句子（讓句子更接近自然的達悟語）"):
                        for idx_ca, it in enumerate(corpus_all[:30]):
                            c_raw = it.get("corpus_raw") or {}
                            c_id = it.get("corpus_id") or c_raw.get("id")
                            st.markdown(f"- **#{c_id}**：{it['tao']} — {it['zh']}")
                            with st.popover(f"✏️ 編輯 #{c_id}", key=f"pop_ed_all_corp_{c_id}_{idx_ca}"):
                                ed_ca_tao = st.text_input(
                                    "達悟語句子",
                                    value=it.get("tao") or "",
                                    key=f"ed_ca_tao_{c_id}_{idx_ca}",
                                )
                                ed_ca_zh = st.text_input(
                                    "中文翻譯",
                                    value=it.get("zh") or "",
                                    key=f"ed_ca_zh_{c_id}_{idx_ca}",
                                )
                                if st.button("💾 儲存修改", key=f"btn_save_ca_{c_id}_{idx_ca}"):
                                    ok_ca, msg_ca = update_corpus_sentence(
                                        c_raw,
                                        canon_apostrophes(ed_ca_tao),
                                        ed_ca_zh,
                                        current_user,
                                    )
                                    if ok_ca:
                                        st.success(msg_ca)
                                        st.rerun()
                                    else:
                                        st.error(msg_ca)
                        if len(corpus_all) > 30:
                            st.caption("輸入單字檢索可精準定位更多語料庫句子。")

            for fb in shown:
                fb_id = fb["id"]
                q_tao_val = get_fb_q_tao(fb)
                q_zh_val = get_fb_q_zh(fb)
                fb_origin = fb.get("origin") or detect_input_origin(q_tao_val)

                with st.expander(
                    f"📌 語料 ID #{fb_id} - 由 {fb.get('user_id', '匿名')} 提供 ({str(fb.get('timestamp', ''))[:10]})",
                    expanded=bool(query) and len(shown) <= 3,
                ):
                    with st.container(border=True):
                        st.markdown("<div style='font-size: 16px; font-weight: bold;'>pacici (問)</div>", unsafe_allow_html=True)
                        render_interactive_sentence(
                            q_tao_val,
                            is_ai_generated=False,
                            user_accepted=True,
                            key_prefix=f"fbq_{fb_id}",
                            enable_word_voting=False,
                            current_user=current_user,
                            user_input_text=q_tao_val if fb_origin == "tao" else "",
                            allow_phrase_selection=bool(current_user),
                        )
                        st.markdown(f"<div style='font-size: 14px; opacity: 0.8;'>{q_zh_val}</div>", unsafe_allow_html=True)

                    st.markdown("##### tovis (答)")

                    render_interactive_sentence(
                        fb.get("tao_text", ""),
                        is_ai_generated=False,
                        user_accepted=True,
                        key_prefix=f"fb_{fb_id}",
                        enable_word_voting=True,
                        fb_id=fb_id,
                        current_user=current_user,
                        user_input_text=q_tao_val if fb_origin == "tao" else "",
                        allow_phrase_selection=bool(current_user),
                    )
                    st.markdown(f"<div style='font-size: 14px; opacity: 0.8; margin-top: 10px; margin-bottom: 8px;'>{fb.get('zh_text')}</div>", unsafe_allow_html=True)

                    st.markdown(
                        f"""
                        <div style="font-size: 14px; opacity: 0.8; margin-top: 5px;">
                            <b>o pakatotoan (背景):</b> {fb.get('region')} | {fb.get('age_group')} | {fb.get('gender')} | <b>origin:</b> {fb_origin}
                        </div>
                        """,
                        unsafe_allow_html=True,
                    )

    except Exception as e:
        st.error(f"讀取社群資料失敗：{e}")

elif main_menu == "amian so AI ori / 關於本站":
    st.subheader("🏝️ 關於達悟文語料庫&AI訓練")
    st.markdown(
        """
        ### 專案願景
        本平台致力於保存與推廣**達悟語（Yami/Tao）**，結合演算法優先匹配與備援 LLM 模型，建構即時雙向的文字語意互動系統。

        ### 系統特色
        1. **智慧螢光筆標註**：用例超過 10 次的詞彙維持黑色顯示（仍可點擊查看詳情）；低用例或使用者輸入字顯示**黃色螢光筆**；語料庫與網站中未曾出現且非使用者輸入的 AI 生成字顯示**紅色螢光筆**（直接採納後自動轉為黃色）。
        2. **公開句子文法檢索與個人／社群單字偏好**：句子只要公開即納入演算法搜尋池，主要參照句中文法；AI 回應中的單字與拼法則優先看個人傾向與社群用例數，盡量採用個人常用或社群高頻的拼法或單字。
        3. **語料庫句子編輯、單字詳情與社群投票**：使用者可編輯語料庫句子以更貼近自然達悟語；點擊任何單字或片語即可查看 `pipipangayan`、解釋、語源（固有詞／新造詞／音譯）並按「編輯」修改，查看雙語例句（各詞 1 句）、含異體字總和的用例數與各異體字明細（點選喜歡的拼法即送出，選「其他」才出現輸入框）。
        4. **模型訓練資料來源**：本模型所使用的訓練與參考資料皆來自 [FormosanBank](https://github.com/FormosanBank/FormosanBank/tree/main)，包含以下重要來源：
           * **族語E樂園雅美(達悟)語版**：Indigenous Languages Research and Development Foundation. (2020). 族語E樂園. https://web.klokah.tw/
           * **原語會族語線上辭典雅美(達悟)語版**：Council of Indigenous Peoples, & Indigenous Languages Research and Development Foundation. (2024, January). 原住民族語言線上辭典: 雅美語. Executing Institution: National Taiwan Normal University. https://e-dictionary.ilrdf.org.tw/
           * **總統道歉文雅美(達悟)語版**：Tsai, I. W. (2016, August 1). President Tsai Ing-wen's apology to the Indigenous Peoples on behalf of the government [Speech transcript]. Office of the President.
           * **Rau-Dong**：Rau, D. V., and Dong, M. N. (2006). Yami texts with reference grammar and dictionary. Institute of Linguistics, Academia Sinica.
           * **The Wakelin Texts**：Indosan, S., Wakelin, G., Dararyaw, S., and Kalaku, S. (1958). Yami texts. Work Papers of the Summer Institute of Linguistics, University of North Dakota Session: Vol. 2, Article 7. 10.31356/silwp.vol02.07
        """
    )
