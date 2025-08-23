# app.py
import os
import re
import json
import random
import requests
import certifi
import pandas as pd
import streamlit as st
from bs4 import BeautifulSoup
from pymongo import MongoClient
from dotenv import load_dotenv
from pymongo.errors import ServerSelectionTimeoutError, ConfigurationError, ConnectionFailure

# ------------------ App setup ------------------
st.set_page_config(page_title="Spanish Movies Sampler", page_icon="🎬", layout="centered")
st.title("🎬 Spanish Movies — Simple Random Sampler")

# ------------------ Load .env ------------------
load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "")
DB_NAME = os.getenv("DB_NAME", "Movie-List")
POP_COLLECTION = os.getenv("COLLECTION_NAME", "Population")
DATASET_COLLECTION = "Data-Set"

if not MONGO_URI:
    st.error("Missing MONGO_URI in .env")
    st.stop()

# ---------- Mongo (lazy connection + clean TLS) ----------
def get_client():
    return MongoClient(
        MONGO_URI,
        appname="movie-sampler",
        serverSelectionTimeoutMS=15000,
        connectTimeoutMS=15000,
        tls=True,
        tlsCAFile=certifi.where(),
    )

client = None
db = None
col = None
ds_col = None

def ensure_connection():
    global client, db, col, ds_col
    if client is None:
        client = get_client()
        client.admin.command("ping")  # forces handshake
        db = client[DB_NAME]
        col = db[POP_COLLECTION]
        ds_col = db[DATASET_COLLECTION]

# ------------------ Helpers ------------------
@st.cache_data(ttl=10)
def load_df() -> pd.DataFrame:
    try:
        ensure_connection()
        docs = list(col.find({}, {"_id": 0, "ID": 1, "Movie": 1}).sort("ID", 1))
        df = pd.DataFrame(docs)
        if not df.empty:
            df["ID"] = df["ID"].astype(int)
            df["Movie"] = df["Movie"].astype(str)
        return df
    except (ServerSelectionTimeoutError, ConnectionFailure, ConfigurationError) as e:
        st.error(f"Could not reach MongoDB Atlas: {e}")
        return pd.DataFrame()

def delete_one_by_id(movie_id: int) -> int:
    try:
        ensure_connection()
        return col.delete_one({"ID": int(movie_id)}).deleted_count
    except Exception as e:
        st.error(f"Delete failed: {e}")
        return 0

def sample_docs(k: int):
    try:
        ensure_connection()
        pipeline = [{"$sample": {"size": int(k)}}, {"$project": {"_id": 0, "ID": 1, "Movie": 1}}]
        return list(col.aggregate(pipeline))
    except Exception as e:
        st.error(f"Sampling failed: {e}")
        return []

def delete_many_by_ids(ids):
    try:
        ensure_connection()
        return col.delete_many({"ID": {"$in": [int(x) for x in ids]}}).deleted_count
    except Exception as e:
        st.error(f"Bulk delete failed: {e}")
        return 0

def _to_int_safe(text):
    if text is None:
        return None
    digits = re.sub(r"[^\d]", "", str(text))
    return int(digits) if digits else None

def _iso8601_duration_to_minutes(iso_str):
    if not iso_str:
        return None
    h = m = 0
    m_h = re.search(r"(\d+)H", iso_str)
    m_m = re.search(r"(\d+)M", iso_str)
    if m_h:
        h = int(m_h.group(1))
    if m_m:
        m = int(m_m.group(1))
    total = h * 60 + m
    return total if total > 0 else None

# ---------- Minimal IMDb fetch: ONLY Year, Rating, Votes, Runtime ----------
def fetch_min_from_imdb(url: str) -> dict:
    """
    Fetch only:
      - Year
      - IMDb rating
      - Number of Votes
      - Runtime (minutes)
    Everything else is manual.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        )
    }
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    # JSON-LD
    ld = None
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                for d in data:
                    if isinstance(d, dict) and d.get("@type") in ("Movie", "CreativeWork"):
                        ld = d
                        break
            elif isinstance(data, dict) and data.get("@type") in ("Movie", "CreativeWork"):
                ld = data
            if ld:
                break
        except Exception:
            continue

    year = None
    rating_value = None
    rating_count = None
    runtime_min = None

    if ld:
        # Year
        if ld.get("datePublished"):
            m = re.match(r"(\d{4})", str(ld["datePublished"]))
            if m:
                year = int(m.group(1))
        # Ratings
        agg = ld.get("aggregateRating") or {}
        rating_value = agg.get("ratingValue")
        rating_count = agg.get("ratingCount")
        rating_value = float(rating_value) if rating_value is not None else None
        rating_count = _to_int_safe(rating_count)
        # Runtime
        runtime_min = _iso8601_duration_to_minutes(ld.get("duration"))

    # Runtime fallback (e.g., "2h 10m")
    if runtime_min is None:
        runtime_tag = soup.select_one('[data-testid="title-techspec_runtime"] li')
        if runtime_tag:
            txt = runtime_tag.get_text(" ", strip=True)
            h = re.search(r"(\d+)\s*h", txt)
            m = re.search(r"(\d+)\s*m", txt)
            runtime_min = (int(h.group(1)) * 60 if h else 0) + (int(m.group(1)) if m else 0)
            if runtime_min == 0:
                runtime_min = None

    return {
        "URL": url,
        "Year": year,
        "IMDb rating": rating_value,
        "Number of Votes": rating_count,
        "Runtime": runtime_min,
    }

def save_movie_record(doc: dict):
    """Upsert by URL into Data-Set."""
    ensure_connection()
    return ds_col.update_one({"URL": doc.get("URL")}, {"$set": doc}, upsert=True).upserted_id

# ------------------ Session ------------------
if "selected_id" not in st.session_state:
    st.session_state.selected_id = None
if "fetched_doc" not in st.session_state:
    st.session_state.fetched_doc = None

# ------------------ UI ------------------
if st.button("🔌 Test Atlas connection"):
    try:
        ensure_connection()
        st.success("Atlas connection OK (ping passed).")
    except Exception as e:
        st.error(f"Atlas connection failed: {e}")

tab1, tab2 = st.tabs(["Draw sample at random", "Add by URL → Data-Set"])

# ---- Tab 2: Draw sample ----
with tab1:
    st.subheader("Simple Random Sample (preview/download, optional delete-many)")
    df_all = load_df()
    remaining = len(df_all)
    st.write(f"Movies currently in population: **{remaining}**")
    if remaining > 0:
        k = st.number_input("Sample size", value=10, min_value=1, max_value=max(1, remaining), step=1)
        if remaining < k:
            st.warning(f"Not enough movies to sample {k}. Reduce k or add more items.")
        else:
            if st.button(f"🎲 Preview random sample of {k} (no deletion)"):
                sdocs = sample_docs(k)
                sdf = pd.DataFrame(sdocs)
                if not sdf.empty:
                    sdf["ID"] = sdf["ID"].astype(int)
                    st.dataframe(sdf.sort_values("ID")[["ID", "Movie"]], use_container_width=True, hide_index=True)
                    st.download_button(
                        "⬇️ Download sample as CSV",
                        data=sdf.sort_values("ID")[["ID", "Movie"]].to_csv(index=False).encode("utf-8"),
                        file_name=f"sample_{k}.csv",
                        mime="text/csv",
                    )
                else:
                    st.info("Sample came back empty.")

            with st.expander("Danger zone — draw & DELETE the sampled movies"):
                st.write("This will **permanently remove** the sampled documents from the population.")
                really = st.checkbox("I understand and want to proceed.")
                if st.button(f"❌ Draw and DELETE {k} at random", disabled=not really):
                    sdocs = sample_docs(k)
                    ids_to_delete = [d["ID"] for d in sdocs]
                    deleted = delete_many_by_ids(ids_to_delete)
                    st.success(f"Deleted {deleted} movies.")
                    st.cache_data.clear()
                    st.rerun()

# ---- Tab 3: Add by URL -> Data-Set (fetch ONLY year/rating/votes/runtime; manual for rest) ----
with tab2:
    st.subheader("Add a movie by URL → stored in collection: Data-Set")
    url = st.text_input(
        "Paste the movie URL (IMDb title page recommended):",
        placeholder="https://www.imdb.com/title/tt1234567/"
    )

    fetch_btn = st.button("🔎 Fetch (Year, Rating, Votes, Runtime only)")
    fetched = st.session_state.get("fetched_doc")

    if fetch_btn and url:
        try:
            with st.spinner("Fetching…"):
                doc = fetch_min_from_imdb(url.strip())
                st.session_state["fetched_doc"] = doc
            st.success("Fetched minimal fields.")
            fetched = doc
        except Exception as e:
            st.error(f"Fetch failed: {e}")
            st.session_state["fetched_doc"] = None
            fetched = None

    # --- Manual fields (always shown; you can fill even before fetch) ---
    st.markdown("**Manual fields** (these will be stored as entered):")
    mv = st.text_input("Movie (name)", max_chars=300)
    # genre chips via comma-separated → list
    genre_text = st.text_input("Genre (comma-separated, e.g., drama, thriller)")
    dir_text = st.text_input("Director(s) (comma-separated)")
    writer_text = st.text_input("Writer(s) (comma-separated)")
    country_text = st.text_input("Country/Countries (comma-separated)")
    gross_num = st.number_input("Gross Profit (numeric, e.g., 123456789)", min_value=0, step=1000)

    # --- Fetched read-only fields (only 4 of them) ---
    st.markdown("**Fetched (read-only) from URL:**")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.number_input("Year", value=int(fetched["Year"]) if fetched and fetched.get("Year") else 0, step=1, disabled=True)
    with c2:
        st.number_input("IMDb rating", value=float(fetched["IMDb rating"]) if fetched and fetched.get("IMDb rating") is not None else 0.0, step=0.1, disabled=True)
    with c3:
        st.number_input("Number of Votes", value=int(fetched["Number of Votes"]) if fetched and fetched.get("Number of Votes") else 0, step=1, disabled=True)
    with c4:
        st.number_input("Runtime (min)", value=int(fetched["Runtime"]) if fetched and fetched.get("Runtime") else 0, step=1, disabled=True)

    # --- Save ---
    if st.button("💾 Save to Data-Set", type="primary"):
        try:
            ensure_connection()
            # Build doc with your schema; exclude Budget entirely
            to_list = lambda s: [x.strip() for x in s.split(",") if x.strip()] if s else []
            doc_out = {
                "URL": url.strip() if url else None,
                "Movie": mv or None,                              # manual
                "Genre": to_list(genre_text),                     # manual -> list
                "Year": fetched.get("Year") if fetched else None, # fetched
                "IMDb rating": fetched.get("IMDb rating") if fetched else None,  # fetched
                "Director": to_list(dir_text),                    # manual -> list
                "Number of Votes": fetched.get("Number of Votes") if fetched else None, # fetched
                "Writer": to_list(writer_text),                   # manual -> list
                "Country": to_list(country_text),                 # manual -> list
                "Runtime": fetched.get("Runtime") if fetched else None,          # fetched
                "Gross Profit": int(gross_num) if gross_num else None,          # manual numeric
            }
            # Upsert by URL if URL present; else insert a new document
            key = {"URL": doc_out["URL"]} if doc_out["URL"] else {"Movie": doc_out["Movie"], "Year": doc_out["Year"]}
            ds_col.update_one(key, {"$set": doc_out}, upsert=True)
            st.success("Saved to Data-Set.")
        except Exception as e:
            st.error(f"Failed to save: {e}")

    with st.expander("Notes"):
        st.markdown(
            """
            - This tab **fetches only**: Year, IMDb rating, Number of Votes, Runtime (minutes).
            - All other fields are **manual inputs**. **Budget is excluded** and not stored.
            - We upsert by URL when available; otherwise by (Movie, Year).
            """
        )