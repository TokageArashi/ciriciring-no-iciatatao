import datetime
import hashlib
import html
import json
import os
import re
import time
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


# --- 1. 全域設定與 Supabase 連線 ---
MODEL_NAME = "gemini-3.6-flash"

st.set_page_config(
    page_title="ciriciring no iciatatao", page_icon="🏝️", layout="wide"
)

if "user_info" not in st.session_state:
    st.session_state.user_info = None

# 自動關閉拼字檢查
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
        const observer = new MutationObserver(() => disableSpellcheck());
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
    /* 句子流式排列：把 container 內每個單字（純文字或 popover）排成同一行並自動換行 */
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

    /* 螢光標籤單字（popover 按鈕）：Y = 黃色、R = 紅色 */
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


# --- 2. 會員機制 ---
def make_hashes(password):
    return hashlib.sha256(str.encode(password)).hexdigest()


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


# --- 3. Supabase 讀寫函式 ---
def save_語料_to_supabase(
    user_info,
    bg_info,
    q_orig,
    q_trans,
    r_tao,
    r_zh,
    is_edited=False,
    is_error=False,
    pending_sentence_id=None,
):
    try:
        region_val = bg_info["region"]
        region_str = ", ".join(region_val) if isinstance(region_val, list) else region_val

        data = {
            "user_id": user_info["username"],
            "user_email": user_info["email"],
            "region": region_str,
            "age_group": bg_info["age_group"],
            "gender": bg_info["gender"],
            "q_original": q_orig,
            "q_trans": q_trans,
            "q_audio_data": None,
            "q_audio_mime": None,
            "tao_text": r_tao,
            "zh_text": r_zh,
            "r_audio_data": None,
            "r_audio_mime": None,
            "is_edited": 1 if is_edited else 0,
            "error_count": 1 if is_error else 0,
            "timestamp": datetime.datetime.now().isoformat(),
        }
        res_ins = supabase.from_("feedback").insert(data).execute()
        build_word_index.clear()
        load_user_typed_words.clear()

        # 使用者若在「尚未儲存」的回應句上投過票，句子存檔（且未修改）後，
        # 把那些票改掛到新例句的 id，這樣同一句不會在例句頁被重複投票。
        if pending_sentence_id and not is_edited and res_ins.data:
            try:
                new_id = res_ins.data[0].get("id")
                if new_id:
                    supabase.from_("community_votes").update({"feedback_id": new_id}).eq(
                        "feedback_id", 0
                    ).like("hidden_suggestion", f"%sent:{pending_sentence_id}%").execute()
                    load_vote_data.clear()
                    load_user_votes.clear()
            except Exception:
                pass
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

        # 只刪例句本身：該句產生的投票（community_votes）刻意保留，
        # 用例數則因為例句消失而自動扣除（見 build_word_index）。
        supabase.from_("feedback").delete().eq("id", feedback_id).execute()
        build_word_index.clear()
        load_all_raw_corpus.clear()
        return True, "刪除成功！"
    except Exception as e:
        return False, f"刪除失敗：{e}"


# --- 4. 語料庫讀取與檢索 ---
@st.cache_data(ttl=300)
def load_all_raw_corpus():
    corpus_items = []
    try:
        res_corpus = supabase.from_("corpus").select("*").execute()
        for r in res_corpus.data:
            corpus_items.append(
                {
                    "id": f"Corpus ID #{r.get('id')}",
                    "tao": r.get("tao_text", ""),
                    "zh": r.get("zh_text", ""),
                    "source": r.get("source", "官方語料庫"),
                    "full_text": f"{r.get('tao_text', '')} {r.get('zh_text', '')}".lower(),
                }
            )
    except Exception as e:
        st.warning(f"⚠️ 讀取 corpus 語料庫提示：{e}")

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
                        "tao": r.get("tao_text", ""),
                        "zh": r.get("zh_text", ""),
                        "source": f"社群驗證 (提供者: {r.get('user_id', '匿名')})",
                        "is_feedback": True,
                        "full_text": f"{r.get('tao_text', '')} {r.get('zh_text', '')}".lower(),
                    }
                )
    except Exception as e:
        st.warning(f"⚠️ 讀取社群驗證語料提示：{e}")

    return corpus_items


def search_relevant_corpus(user_input, top_k=3):
    all_corpus = load_all_raw_corpus()
    if not all_corpus:
        return []

    if not user_input:
        matched_items = all_corpus[-top_k:]
    else:
        clean_input = user_input.lower().strip()
        keywords = set(re.findall(r"\w+", clean_input))

        scored_items = []
        for item in all_corpus:
            score = 0
            if clean_input in item["full_text"]:
                score += 100

            for kw in keywords:
                if kw in item["full_text"]:
                    score += 10

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


def algorithm_first_processing(user_text):
    if not user_text:
        return None

    all_corpus = load_all_raw_corpus()
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

    keywords = set(re.findall(r"\w+", clean_input))
    best_item = None
    max_matches = 0
    for item in all_corpus:
        matches = sum(1 for kw in keywords if kw in item["full_text"])
        if matches > max_matches:
            max_matches = matches
            best_item = item

    if best_item and max_matches >= 2:
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
HIGHLIGHT_THRESHOLD = 10  # 用例 <= 10 → 黃色；> 10 → 一般單字不標示
WORD_STRIP_CHARS = ".,!?;:\"“”()[]「」『』。，！？；：、…"


def normalize_word(token):
    """全站統一的單字識別：去除頭尾標點並轉小寫。"""
    return token.strip(WORD_STRIP_CHARS).lower()


def split_token(token):
    """把 'akokay,' 拆成 (前標點, 單字, 後標點)。"""
    esc = re.escape(WORD_STRIP_CHARS)
    m = re.match(rf"^([{esc}]*)(.*?)([{esc}]*)$", token)
    return m.group(1), m.group(2), m.group(3)


@st.cache_data(ttl=300)
def build_word_index():
    """
    單字 -> {'count': 在本網站出現的總次數, 'zh': 一則例句翻譯}。
    來源 = 官方語料庫 + feedback 表中所有已儲存的例句（使用者輸入 q_original 與回應 tao_text）。
    同一句用到兩次就算兩次；例句被刪除後，下次重算時自然扣掉。
    """
    index = {}

    def add(text, zh):
        for tok in (text or "").split():
            w = normalize_word(tok)
            if not w:
                continue
            entry = index.setdefault(w, {"count": 0, "zh": zh})
            entry["count"] += 1

    # 官方語料；已驗證而併入語料的 feedback 略過，改由下面的 feedback 全量計算，避免重複
    for item in load_all_raw_corpus():
        if not item.get("is_feedback"):
            add(item["tao"], item["zh"])

    try:
        res = supabase.from_("feedback").select("q_original, q_trans, tao_text, zh_text").execute()
        for r in res.data or []:
            add(r.get("q_original"), r.get("q_trans"))
            add(r.get("tao_text"), r.get("zh_text"))
    except Exception:
        pass

    return index


def _parse_vote(v):
    """
    把一筆 community_votes 解析成 (被投票的單字, 投給的拼法)。
    新格式: word_key:apia | pick:apiya
    舊格式: word_key:apia | 替代詞: apiya  /  word_key:apia | 贊同  /  word_key:apia | 不贊同
    """
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
        pick = None  # 舊資料：不贊同但沒有提出替代拼法
    return word, (pick or None)


@st.cache_data(ttl=30)
def load_vote_data():
    """
    tally      {拼法: 被選次數}
    group_of   {單字: 群組代表字}      只有「被提出過替代拼法」的單字才有群組
    members    {群組代表字: [同組拼法]}
    user_picks {帳號: {群組代表字: {拼法: 次數}}}
    """
    empty = {"tally": {}, "group_of": {}, "members": {}, "user_picks": {}}
    try:
        res = supabase.from_("community_votes").select("voter_id, vote_result, hidden_suggestion").execute()
    except Exception:
        return empty

    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    tally, rows = {}, []
    for v in res.data or []:
        parsed = _parse_vote(v)
        if not parsed:
            continue
        word, pick = parsed
        find(word)
        if pick:
            tally[pick] = tally.get(pick, 0) + 1
            if pick != word:  # 有人把 word 的替代拼法提為 pick → 兩者視為同一個詞的不同拼法
                ra, rb = find(word), find(pick)
                if ra != rb:
                    parent[rb] = ra
        rows.append((v.get("voter_id"), pick))

    roots = {w: find(w) for w in list(parent)}
    members = {}
    for w, r in roots.items():
        members.setdefault(r, []).append(w)
    members = {r: sorted(ws) for r, ws in members.items() if len(ws) > 1}
    group_of = {w: r for w, r in roots.items() if r in members}

    user_picks = {}
    for voter, pick in rows:
        if voter and pick and pick in group_of:
            d = user_picks.setdefault(voter, {}).setdefault(group_of[pick], {})
            d[pick] = d.get(pick, 0) + 1

    return {"tally": tally, "group_of": group_of, "members": members, "user_picks": user_picks}


def sentence_key(fb_id, sentence_id):
    """例句的唯一識別：已儲存 -> feedback id；尚未儲存的回應句 -> 這次生成的暫時 id。"""
    if fb_id:
        return f"fb:{fb_id}"
    if sentence_id:
        return f"tmp:{sentence_id}"
    return None


def _fetch_user_votes(voter_id):
    """直接查 Supabase：回傳這個帳號已投過的 {(例句識別, 單字)}。查詢失敗會丟出例外。"""
    res = (
        supabase.from_("community_votes")
        .select("feedback_id, hidden_suggestion")
        .eq("voter_id", voter_id)
        .execute()
    )
    keys = set()
    for v in res.data or []:
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
    """顯示用（有短暫快取）；真正送出投票時會改用 _fetch_user_votes 重查。"""
    try:
        return _fetch_user_votes(voter_id)
    except Exception:
        return set()


@st.cache_data(ttl=60)
def load_user_typed_words(username):
    """此帳號自己打過的字：已儲存的輸入句，以及自己修改過的回應句。AI 原樣回應的句子不算。"""
    words = []
    try:
        res = (
            supabase.from_("feedback")
            .select("q_original, tao_text, is_edited")
            .eq("user_id", username)
            .execute()
        )
    except Exception:
        return words
    for r in res.data or []:
        texts = [r.get("q_original")]
        if r.get("is_edited"):
            texts.append(r.get("tao_text"))
        for t in texts:
            words += [normalize_word(tok) for tok in (t or "").split()]
    return [w for w in words if w]


def get_spelling_prefs(username, current_text=""):
    """
    {單字: 這個帳號慣用的拼法}（只列出該被換掉的字）。
    只看這個帳號自己的打字（含這次輸入）與自己的投票選擇，不受其他使用者影響；
    各拼法分數相同時不調整。
    """
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

    prefs = {}
    for root, d in score.items():
        ranked = sorted(d.items(), key=lambda kv: -kv[1])
        if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
            continue
        best = ranked[0][0]
        for m in vd["members"].get(root, []):
            if m != best:
                prefs[m] = best
    return prefs


def apply_spelling_prefs(text, prefs):
    """把句子裡的單字換成慣用拼法；回傳 (新句子, {原字: 新字})。"""
    if not text or not prefs:
        return text, {}
    out, swaps = [], {}
    for tok in text.split():
        lead, core, trail = split_token(tok)
        new = prefs.get(core.lower()) if core else None
        if new:
            if core[:1].isupper():
                new = new[:1].upper() + new[1:]
            swaps[core] = new
            tok = f"{lead}{new}{trail}"
        out.append(tok)
    return " ".join(out), swaps


def classify_word(count, in_user_input, is_ai_generated, user_accepted):
    """回傳 None（一般單字）、'yellow'、'red'。"""
    if count > HIGHLIGHT_THRESHOLD:
        return None
    if count == 0:
        if in_user_input:
            return "yellow"  # 使用者輸入、語料庫沒有
        if is_ai_generated and not user_accepted:
            return "red"  # AI 自創
    return "yellow"  # 用例不超過 10 例 / 已被採納的自創字


def _plain(text, css_class="tao-w"):
    st.markdown(f'<span class="{css_class}">{html.escape(text)}</span>', unsafe_allow_html=True)


def render_interactive_sentence(sentence, is_ai_generated=False, user_accepted=False, etymology_map=None, key_prefix="sentence", enable_word_voting=False, fb_id=None, current_user=None, user_input_text="", sentence_id=None):
    if not isinstance(etymology_map, dict):
        etymology_map = {}

    word_index = build_word_index()
    vote_data = load_vote_data()
    user_words = {normalize_word(t) for t in (user_input_text or "").split()} - {""}

    skey = sentence_key(fb_id, sentence_id)
    my_votes = load_user_votes(current_user) if (enable_word_voting and current_user) else set()

    # 這個 container 會被 CSS 排成「一行、自動換行」的句子（見頁首 CSS）
    with st.container(key=f"sent_{key_prefix}"):
        for idx, token in enumerate((sentence or "").split()):
            lead, w, trail = split_token(token)
            if lead:
                _plain(lead, "tao-lead")

            if w:
                clean_w = w.lower()
                info = word_index.get(clean_w, {})
                occ_count = info.get("count", 0)
                level = classify_word(occ_count, clean_w in user_words, is_ai_generated, user_accepted)

                if level is None:
                    _plain(w)  # 一般單字：不標示、不可點擊
                else:
                    etymology_text = etymology_map.get(w, etymology_map.get(clean_w, ""))
                    example_zh = info.get("zh")
                    zh_meaning = f"例句翻譯: {example_zh}" if example_zh else (etymology_text or "暫無中文說明")
                    category_title = "🔴 AI 自創字" if level == "red" else "🟡 使用者輸入 / 低用例詞彙"
                    tag = "hlR" if level == "red" else "hlY"

                    # 同一個詞的不同拼法（有人提出過替代拼法才會成組）與各自票數
                    group_root = vote_data["group_of"].get(clean_w)
                    others = [m for m in vote_data["members"].get(group_root, []) if m != clean_w]
                    usage = {m: word_index.get(m, {}).get("count", 0) for m in [clean_w] + others}
                    votes_n = {m: vote_data["tally"].get(m, 0) for m in usage}
                    totals = {m: usage[m] + votes_n[m] for m in usage}
                    variants = [clean_w] + sorted(others, key=lambda m: -totals[m])

                    with st.popover(w, key=f"{key_prefix}_{tag}_{idx}"):
                        st.markdown(f"### 🔠 單字詳情: **{w}**")
                        st.markdown(f"- **中文解釋**: {zh_meaning}")
                        st.markdown(f"- **分類**: {category_title}")
                        if etymology_text and example_zh:
                            st.markdown(f"- **語源說明**: {etymology_text}")
                        st.markdown(f"- **本站總用例數**: `{occ_count}` 次")

                        st.divider()
                        st.markdown("#### 📊 拼法與票數（票數 ＝ 本站用例 ＋ 投票人數）")
                        for m in variants:
                            mark = "（本句）" if m == clean_w else ""
                            st.markdown(f"- **{m}**{mark}：用例 {usage[m]} ＋ 投票 {votes_n[m]} ＝ `{totals[m]}` 票")

                        if enable_word_voting and current_user and skey and (skey, clean_w) in my_votes:
                            st.info(f"✅ 您已對本句的「{w}」投過票了")
                        elif enable_word_voting and current_user:
                            st.markdown("#### 🗳️ 這裡該用哪個拼法？")
                            w_choice = st.radio(
                                "選擇拼法",
                                variants,
                                key=f"w_choice_{key_prefix}_{idx}",
                                format_func=lambda m, t=totals: f"{m}（{t[m]}）",
                                horizontal=True,
                                label_visibility="collapsed",
                            )
                            w_alt_input = st.text_input(
                                "或提出其他拼法（單一單字；留空則採用上面的選擇）：",
                                key=f"w_alt_{key_prefix}_{idx}",
                                placeholder="例如：apiya",
                            )

                            if st.button("送出投票", key=f"btn_w_vote_{key_prefix}_{idx}"):
                                try:
                                    # 送出前重新查一次，確保「同帳號、同例句、同單字」只有一票
                                    load_user_votes.clear()
                                    if skey and (skey, clean_w) in _fetch_user_votes(current_user):
                                        raise ValueError(f"您已對本句的「{w}」投過票，無法重複投票。")

                                    alt_raw = w_alt_input.strip()
                                    if alt_raw and re.search(r"\s", alt_raw):
                                        raise ValueError("提出的拼法請只輸入單一單字。")
                                    alt = normalize_word(alt_raw)
                                    if alt_raw and not alt:
                                        raise ValueError("提出的拼法無效。")
                                    pick = alt or w_choice  # 提出新拼法 = 同時投給該拼法

                                    voter_info = st.session_state.user_info
                                    voter_region = voter_info.get("region", "未知")
                                    voter_region_str = ", ".join(voter_region) if isinstance(voter_region, list) else voter_region

                                    # 尚未儲存的回應句用 sent:<暫時id> 標記是哪一句
                                    sent_part = f" | sent:{sentence_id}" if (not fb_id and sentence_id) else ""
                                    suggestion_content = f"word_key:{clean_w}{sent_part} | pick:{pick}"

                                    supabase.from_("community_votes").insert({
                                        "feedback_id": fb_id if fb_id else 0,
                                        "voter_id": current_user,
                                        "region": voter_region_str,
                                        "age_group": voter_info.get("age_group", "未知"),
                                        "gender": voter_info.get("gender", "未知"),
                                        "vote_result": "贊同" if pick == clean_w else "不贊同",
                                        "hidden_suggestion": suggestion_content,
                                        "timestamp": datetime.datetime.now().isoformat(),
                                    }).execute()
                                    load_vote_data.clear()
                                    load_user_votes.clear()
                                    st.success(f"🎉 已投給「{pick}」！")
                                    st.rerun()
                                except Exception as vote_err:
                                    st.error(f"投票失敗：{vote_err}")

            if trail:
                _plain(trail, "tao-punct")


# --- 6. 雙軌邏輯 ---
def process_ai_input(text_prompt=None):
    if text_prompt:
        algo_result = algorithm_first_processing(text_prompt)
        if algo_result:
            return algo_result

    api_key = st.secrets.get("GOOGLE_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        st.error("❌ 找不到 GOOGLE_API_KEY，請檢查 Secrets 設定")
        return None

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(MODEL_NAME)

    query_str = text_prompt if text_prompt else ""
    legal_corpus = search_relevant_corpus(query_str, top_k=3)
    corpus_context = (
        "\n".join(legal_corpus)
        if legal_corpus
        else "【目前尚無合規的參考語料】"
    )

    system_prompt = f"""
你是一個達悟語（Yami/Tao）對話與翻譯助手。目前處於「AI 備援生成」模式。

【唯一合規參考語料庫】:
{corpus_context}

【絕對強制規則】：
1. 請勿在生成的達悟語句子中包含任何如 `[使用者推薦]` 或 `[AI自創]` 的文字標籤，保持句子乾淨。
2. etymology 必須是一個 JSON 物件 (Dictionary)，鍵為達悟語單字，值為該單字的中文翻譯與語源說明（例如：{{"akokay": "你好 / 傳統問候語", "vani": "眼睛 / 傳統詞彙"}}）。絕不可回傳字串。

【輸出格式】：
請嚴格以 JSON 格式輸出：
{{
  "user_recognized_tao": "使用者輸入的純達悟語句子",
  "user_translation": "中文對照翻譯",
  "ai_reply_tao": "達悟語回應句子",
  "ai_reply_zh": "回應之中文翻譯",
  "etymology": {{
     "關鍵單字": "中文翻譯與語源說明"
  }},
  "is_ai_generated": true,
  "reference": "必須明確列出引用的語料庫ID與出處"
}}
"""

    contents = [system_prompt]
    if text_prompt:
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

            return result
        except ResourceExhausted:
            if attempt < max_retries - 1:
                wait_time = 5 * (2 ** attempt)
                time.sleep(wait_time)
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
            st.session_state.user_info = None
            st.rerun()

# --- 8. 主頁面區塊 ---
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
        dual_text("macisirisiring do AI kano misinsinmo so vakong no AI", "演算法與 AI 備援對話語料採集", is_title=True), 
        unsafe_allow_html=True
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
        text_input = st.text_input("text_input_box", placeholder="例如：Akokay / 今天天氣如何？", label_visibility="collapsed")
        if st.button("itoro 發送查詢（演算法優先）"):
            if text_input.strip():
                with st.spinner("⏳ 演算法檢索與備援處理中..."):
                    ai_data = process_ai_input(text_prompt=text_input)
                    if ai_data:
                        st.session_state.active_q = text_input
                        ai_data["sentence_id"] = uuid.uuid4().hex  # 這一句回應的暫時識別

                        # 依「這位使用者自己」的打字與投票習慣，把回應中的字換成他慣用的拼法
                        prefs = get_spelling_prefs(st.session_state.user_info["username"], text_input)
                        ai_data["ai_reply_tao"], swaps = apply_spelling_prefs(ai_data.get("ai_reply_tao", ""), prefs)
                        if swaps:
                            ety = ai_data.get("etymology") if isinstance(ai_data.get("etymology"), dict) else {}
                            for old, new in swaps.items():
                                if old in ety and new not in ety:
                                    ety[new] = ety[old]
                            ai_data["etymology"] = ety
                            ai_data["spelling_swaps"] = swaps
                        st.session_state.ai_data = ai_data
                        st.session_state.active_bg = current_bg

        if "ai_data" in st.session_state and st.session_state.ai_data:
            ai_data = st.session_state.ai_data
            q_orig = st.session_state.get("active_q", "")
            bg_info = st.session_state.get("active_bg", current_bg)

            reg_val = bg_info["region"]
            reg_str = ", ".join(reg_val) if isinstance(reg_val, list) else reg_val
            user_profile_str = f"部落: {reg_str} | 年齡層: {bg_info['age_group']} | 性別: {bg_info['gender']}"
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
                    enable_word_voting=False,  # 自己輸入的句子只看說明與票數，投票在回應句進行
                    user_input_text=user_sentence,
                )
                st.markdown(f"<div style='font-size: 14px; opacity: 0.8;'>{ai_data.get('user_translation', '')}</div>", unsafe_allow_html=True)

            st.markdown("##### maniring o AI/演算法 am (回應結果 - 點擊有螢光標籤的單字查看說明與投票)")
            render_interactive_sentence(
                ai_data.get("ai_reply_tao", ""), 
                is_ai_generated=is_ai, 
                user_accepted=False, 
                etymology_map=ai_data.get("etymology", {}),
                key_prefix="main_reply",
                enable_word_voting=True,
                current_user=st.session_state.user_info["username"],
                user_input_text=ai_data.get("user_recognized_tao", q_orig),
                sentence_id=ai_data.get("sentence_id")
            )
            st.markdown(f"<div style='font-size: 14px; opacity: 0.8; margin-top: 10px; margin-bottom: 15px;'>{ai_data.get('ai_reply_zh', '')}</div>", unsafe_allow_html=True)
            if ai_data.get("spelling_swaps"):
                st.caption("已依您的慣用拼法調整：" + "、".join(f"{o} → {n}" for o, n in ai_data["spelling_swaps"].items()))

            st.markdown(
                f"""
                <div style="padding: 15px; border-radius: 8px; margin-bottom: 10px; border: 1px solid rgba(128, 128, 128, 0.3);">
                    <div style="font-size: 18px; font-weight: bold;">o pakaaranan no ciring (參考資料 / 語料來源)</div>
                    <div style="font-size: 16px; font-weight: bold; margin-top: 5px;">{user_profile_str}</div>
                </div>
                """,
                unsafe_allow_html=True
            )

            st.divider()
            st.markdown(dual_text("o kapakaapiya no ciring", "語料品質評估", is_title=True), unsafe_allow_html=True)
            
            eval_choice = st.radio(
                "eval_choice_radio",
                ["apiya / 正確 (直接採納)", "beken / 錯誤 (我要修改)"],
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
                        ai_data.get("ai_reply_tao"),
                        ai_data.get("ai_reply_zh"),
                        is_edited=False,
                        is_error=False,
                        pending_sentence_id=ai_data.get("sentence_id"),
                    ):
                        st.success("🎉 已成功儲存！")
                        del st.session_state.ai_data
                        st.rerun()

            elif "beken" in eval_choice:
                st.warning("⚠ 發現錯誤。您可以修正文字：")

                st.markdown("**ablisan o pacici**<br><span style='font-size:14px; opacity:0.8;'>修正【問題】</span>", unsafe_allow_html=True)
                e_q_tao = st.text_input("修改句子 1 達悟語：", value=ai_data.get("user_recognized_tao", q_orig))
                st.markdown("**ablisan o mivaliw no pacici**<br><span style='font-size:14px; opacity:0.8;'>修改問題翻譯</span>", unsafe_allow_html=True)
                e_q_trans = st.text_input("修改句子 1 翻譯：", value=ai_data.get("user_translation", ""))

                st.markdown("**ablisan o tovis**<br><span style='font-size:14px; opacity:0.8;'>修正【回應】</span>", unsafe_allow_html=True)
                e_r_tao = st.text_input("修改句子 2 達悟語：", value=ai_data.get("ai_reply_tao", ""))
                st.markdown("**ablisan o mivaliw no tovis**<br><span style='font-size:14px; opacity:0.8;'>修改回應翻譯</span>", unsafe_allow_html=True)
                e_r_zh = st.text_input("修改句子 2 翻譯：", value=ai_data.get("ai_reply_zh", ""))

                if st.button("voka 💾 儲存修正版"):
                    if save_語料_to_supabase(
                        st.session_state.user_info,
                        bg_info,
                        lower_first_char(e_q_tao),
                        e_q_trans,
                        lower_first_char(e_r_tao),
                        e_r_zh,
                        is_edited=True,
                        is_error=True,
                    ):
                        st.success("🎉 已成功更新修正版")
                        del st.session_state.ai_data
                        st.rerun()

elif main_menu == "manita so tao a miAI / 看別人用AI":
    st.markdown(
        dual_text("kapisirisiring kano manita o vakong no ciring", "社群驗證與單字用法投票", is_title=True), 
        unsafe_allow_html=True
    )
    try:
        res_feedback = supabase.from_("feedback").select("*").order("timestamp", desc=True).execute()
        feedbacks = res_feedback.data if res_feedback.data else []

        if not feedbacks:
            st.info("目前尚無採集到的語料。")
        else:
            current_user = st.session_state.user_info["username"] if st.session_state.user_info else None

            for fb in feedbacks:
                fb_id = fb["id"]

                with st.expander(f"📌 語料 ID #{fb_id} - 由 {fb.get('user_id', '匿名')} 提供 ({str(fb.get('timestamp', ''))[:10]})"):
                    with st.container(border=True):
                        st.markdown("<div style='font-size: 16px; font-weight: bold;'>pacici (問)</div>", unsafe_allow_html=True)
                        render_interactive_sentence(
                            fb.get('q_original', ''),
                            is_ai_generated=False,
                            user_accepted=True,
                            key_prefix=f"fbq_{fb_id}",
                            enable_word_voting=False,  # 問句只看說明與票數；投票在「答」進行
                            user_input_text=fb.get('q_original', ''),
                        )
                        st.markdown(f"<div style='font-size: 14px; opacity: 0.8;'>{fb.get('q_trans') or ''}</div>", unsafe_allow_html=True)

                    st.markdown("##### tovis (答 - 點擊有螢光標籤的單字即可查看說明與投票)")
                    
                    render_interactive_sentence(
                        fb.get('tao_text', ''),
                        is_ai_generated=False,
                        user_accepted=True,
                        key_prefix=f"fb_{fb_id}",
                        enable_word_voting=True,
                        fb_id=fb_id,
                        current_user=current_user
                    )
                    st.markdown(f"<div style='font-size: 14px; opacity: 0.8; margin-top: 10px; margin-bottom: 8px;'>{fb.get('zh_text')}</div>", unsafe_allow_html=True)

                    st.markdown(
                        f"""
                        <div style="font-size: 14px; opacity: 0.8; margin-top: 5px;">
                            <b>o pakatotoan (背景):</b> {fb.get('region')} | {fb.get('age_group')} | {fb.get('gender')}
                        </div>
                        """,
                        unsafe_allow_html=True
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
        1. **智慧螢光筆標註**：用例超過 10 次的詞彙維持正常顯示；低用例或使用者推薦字顯示**黃色螢光筆**；AI 自創字顯示**紅色螢光筆**（直接採納後自動轉為黃色）。
        2. **語源與字根解析**：點選詞彙可檢視 etymology（傳統用法、日語、英語、中文等借詞來源）。
        3. **單字用法社群投票**：社群成員可在同一個詞的不同拼法之間投票，也能提出新的拼法；系統會依每位使用者自己的打字與投票習慣調整回應用字，彼此不互相干擾。
        4. **模型訓練資料來源**：本模型所使用的訓練與參考資料皆來自 [FormosanBank](https://github.com/FormosanBank/FormosanBank/tree/main)，包含以下重要來源：
           * **族語E樂園雅美(達悟)語版**：Indigenous Languages Research and Development Foundation. (2020). 族語E樂園. https://web.klokah.tw/
           * **原語會族語線上辭典雅美(達悟)語版**：Council of Indigenous Peoples, & Indigenous Languages Research and Development Foundation. (2024, January). 原住民族語言線上辭典: 雅美語. Executing Institution: National Taiwan Normal University. https://e-dictionary.ilrdf.org.tw/
           * **總統道歉文雅美(達悟)語版**：Tsai, I. W. (2016, August 1). President Tsai Ing-wen's apology to the Indigenous Peoples on behalf of the government [Speech transcript]. Office of the President.
           * **Rau-Dong**：Rau, D. V., and Dong, M. N. (2006). Yami texts with reference grammar and dictionary. Institute of Linguistics, Academia Sinica.
           * **The Wakelin Texts**：Indosan, S., Wakelin, G., Dararyaw, S., and Kalaku, S. (1958). Yami texts. Work Papers of the Summer Institute of Linguistics, University of North Dakota Session: Vol. 2, Article 7. 10.31356/silwp.vol02.07
        """
    )
