"""
OZON P&L Dashboard
==================
Запуск: streamlit run ozon_dashboard.py
"""

import streamlit as st
import requests
import pandas as pd
from datetime import datetime, date, timedelta
import calendar

# ── Настройки страницы ──────────────────────────────────────────────────────
st.set_page_config(
    page_title="Ozon P&L",
    page_icon="📊",
    layout="wide",
)

st.markdown("""
<style>
    .metric-box {
        background: #1a1d23;
        border: 1px solid #2a2d35;
        border-radius: 10px;
        padding: 16px 20px;
    }
    .stDataFrame { font-size: 13px; }
    div[data-testid="metric-container"] {
        background: #1a1d23;
        border: 1px solid #2a2d35;
        border-radius: 10px;
        padding: 12px 16px;
    }
</style>
""", unsafe_allow_html=True)

# ── Константы ───────────────────────────────────────────────────────────────
TAX       = 0.07
ACQUIRING = 0.015  # резервная ставка для отчёта реализации; для транзакций берётся из API (type_id=29)
AGENT_FEE = 20   # агентское для экспресс/FBS вместо последней мили

def ru_float(v, dec: int = 2) -> str:
    """1 234,56"""
    try:
        fv = float(v)
        if fv != fv:
            return "—"
        s = f"{abs(fv):,.{dec}f}"
        int_part, *dec_part = s.split(".")
        int_part = int_part.replace(",", " ")
        result = int_part + ("," + dec_part[0] if dec_part and dec > 0 else "")
        return ("-" if fv < 0 else "") + result
    except Exception:
        return str(v)

def ru_rub(v, dec: int = 0) -> str:
    """404,3 rub"""
    try:
        fv = float(v)
        if fv != fv:
            return "—"
        return ru_float(v, dec) + " ₽"
    except Exception:
        return str(v)

def ru_pct(v, dec: int = 1) -> str:
    """14,9%"""
    try:
        fv = float(v)
        if fv != fv:
            return "—"
        return f"{fv:.{dec}f}".replace(".", ",") + "%"
    except Exception:
        return str(v)

def r(n: float) -> str:
    """Рубли с пробелом как разделителем тысяч: 33 520 ₽"""
    return f"{n:,.0f} ₽".replace(",", " ")


# fee_type_id → читаемое название (item_fees и delivery.services)
TYPE_NAMES: dict[int, str] = {
    1:  "Эквайринг",
    3:  "Реклама / Продвижение бренда",
    12: "Кросс-докинг",
    16: "Обработка Drop-off (ПВЗ)",
    17: "Обработка Drop-off партнёрами (ПВЗ)",
    22: "Рассрочка Ozon",
    29: "Доставка до ПВЗ партнёрами",
    32: "Логистика FBO",
    41: "Оплата за клик",
    45: "Обработка возвратов партнёрами",
    46: "Размещение на складе",
    48: "Бонусы продавца",
    52: "Подписка Premium / Premium Plus",
    54: "Продвижение с оплатой за заказ",
    59: "Обратная логистика",
    74: "Звёздные товары",
    77: "Поштучная приёмка",
    79: "Временное размещение партнёрами",
    93: "Штраф (индекс ошибок)",
    96: "Ускоренный сбор отзывов",
    98: "Доставка до ПВЗ силами Ozon",
}

# Категории для item_fees
ACQUIRING_TYPE_ID   = 1
PROMO_TYPE_IDS      = {3, 74}       # реклама по SKU → колонка "Реклама"
INSTALLMENT_TYPE_ID = 22

# non_item_fee — расходы магазина (не на артикул, а на кабинет в целом)
# Группы для отображения в блоке "Расходы магазина"
STORE_COST_GROUPS: dict[int, str] = {
    52: "Подписки",
    54: "Реклама",
    41: "Реклама",
    96: "Реклама",
    48: "Реклама",
    12: "Услуги FBO",
    46: "Услуги FBO",
    77: "Услуги FBO",
    79: "Услуги партнёров",
    93: "Штрафы",
}

EXPRESS_TARIFF = [(2000, 300), (4000, 400), (7500, 500), (20000, 600), (float("inf"), 800)]

def express_cost(order_total: float) -> float:
    for threshold, cost in EXPRESS_TARIFF:
        if order_total <= threshold:
            return cost
    return 800

def _get_type_id(obj: dict):
    """Читает type_id / accrual_id — Ozon переименовал поле 09.06.2026."""
    v = obj.get("accrual_id")
    if v is None:
        v = obj.get("type_id")
    return v

def collect_store_costs(ops: list[dict]) -> dict[int, float]:
    """Собирает non_item_fee по type_id/accrual_id — расходы уровня магазина."""
    totals: dict[int, float] = {}
    for accrual in ops:
        nif = accrual.get("non_item_fee")
        if not isinstance(nif, dict):
            continue
        tid = _get_type_id(nif)
        if tid is None:
            continue
        amt = float(((nif.get("accrued") or {}).get("amount") or 0))
        totals[tid] = totals.get(tid, 0) + amt
    return totals

# ── Ozon API ────────────────────────────────────────────────────────────────
API_URL = "https://api-seller.ozon.ru"

def api_post(endpoint: str, body: dict, client_id: str, api_key: str) -> dict:
    headers = {
        "Client-Id": client_id,
        "Api-Key":   api_key,
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(API_URL + endpoint, json=body, headers=headers, timeout=30)
        if r.status_code != 200:
            st.error(f"Ошибка API {r.status_code}: {r.text[:300]}")
            return {}
        return r.json()
    except Exception as e:
        st.error(f"Ошибка соединения с API: {e}")
        return {}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_realization(client_id: str, api_key: str, year: int, month: int) -> list[dict]:
    """
    /v2/finance/realization — месячный отчёт о реализации.
    Доступен только за предыдущие месяцы (Ozon формирует в начале след. месяца).
    Возвращает список строк по артикулам.
    """
    data = api_post(
        "/v2/finance/realization",
        {"year": year, "month": month},
        client_id, api_key,
    )
    result = data.get("result") if data else {}
    return (result or {}).get("rows", [])

@st.cache_data(ttl=300, show_spinner=False)
def fetch_transactions(client_id: str, api_key: str, date_from: str, date_to: str) -> list[dict]:
    """
    /v1/finance/accrual/by-day — новый API начислений.
    Запрашиваем по одному дню за раз, обходим весь период date_from..date_to.
    """
    all_accruals = []

    from_dt = date.fromisoformat(date_from)
    to_dt   = date.fromisoformat(date_to)
    cur     = from_dt

    progress_bar = st.progress(0)
    total_days = (to_dt - from_dt).days + 1
    day_count = 0

    while cur <= to_dt:
        day_str = cur.strftime("%Y-%m-%d")
        last_id = ""
        
        while True:
            try:
                data = api_post(
                    "/v1/finance/accrual/by-day",
                    {"date": day_str, "last_id": last_id},
                    client_id, api_key,
                )
                
                if not data:
                    break
                    
                accruals = data.get("accruals") or []
                if accruals:
                    for a in accruals:
                        if isinstance(a, dict):
                            a["_date"] = day_str
                    all_accruals.extend(accruals)
                
                last_id = data.get("last_id") or ""
                if not last_id or not accruals:
                    break
                    
            except Exception as e:
                st.warning(f"Ошибка при загрузке дня {day_str}: {e}")
                break
                
        cur += timedelta(days=1)
        day_count += 1
        progress_bar.progress(min(day_count / total_days, 1.0))
    
    progress_bar.empty()
    return all_accruals

@st.cache_data(ttl=86400, show_spinner=False)
def fetch_accrual_types(client_id: str, api_key: str) -> dict[int, str]:
    """
    /v1/finance/accrual/types — справочник всех type_id с названиями.
    Возвращает {type_id: название}.
    """
    data = api_post("/v1/finance/accrual/types", {}, client_id, api_key)
    result = {}
    for t in (data.get("types") or []):
        tid = t.get("accrual_id") or t.get("type_id")
        name = t.get("name") or t.get("title") or t.get("description")
        if tid is not None and name:
            result[int(tid)] = name
    return result

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_sku_map(client_id: str, api_key: str) -> dict:
    """
    Строит словарь {sku: offer_id} из заказов FBO и FBS.
    """
    sku_map = {}
    today = date.today()
    since = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    to    = today.strftime("%Y-%m-%d")

    for schema, path in [("fbo", "/v3/posting/fbo/list"), ("fbs", "/v4/posting/fbs/list")]:
        try:
            offset = 0
            page_size = 100  # v3/fbo и v4/fbs оба имеют max=100
            while True:
                body = {
                    "dir": "ASC",
                    "filter": {
                        "since": since + "T00:00:00.000Z",
                        "to":    to    + "T23:59:59.000Z",
                    },
                    "limit": page_size, "offset": offset,
                    "with": {"financial_data": False, "analytics_data": False}
                }
                resp = api_post(path, body, client_id, api_key)

                if not resp:
                    break

                if schema == "fbo":
                    result = resp.get("result", {})
                    postings = result if isinstance(result, list) else result.get("postings", [])
                else:
                    result = resp.get("result", {})
                    postings = result.get("postings", []) if result else []

                if not postings:
                    break

                for p in postings:
                    if not isinstance(p, dict):
                        continue
                    for prod in (p.get("products") or []):
                        if not isinstance(prod, dict):
                            continue
                        sku = str(prod.get("sku") or "")
                        offer_id = str(prod.get("offer_id") or "")
                        if sku and offer_id and sku not in sku_map:
                            sku_map[sku] = offer_id

                if len(postings) < page_size:
                    break
                offset += len(postings)
        except Exception:
            pass

    return sku_map

TURNOVER_GRADE_RU = {
    "GRADES_NONE":     "⏳ Ожидается поставка",
    "GRADES_NOSALES":  "⚪ Нет продаж",
    "GRADES_GREEN":    "🟢 Хорошо",
    "GRADES_YELLOW":   "🟡 Средне",
    "GRADES_RED":      "🔴 Плохо",
    "GRADES_CRITICAL": "🔴 Критично",
}

STOCK_GRADE_EMOJI = {
    "GRADES_GREEN":    "🟢",
    "GRADES_YELLOW":   "🟡",
    "GRADES_RED":      "🔴",
    "GRADES_CRITICAL": "🔴",
    "GRADES_NOSALES":  "⚪",
    "GRADES_NONE":     "⏳",
}

@st.cache_data(ttl=300, show_spinner=False)
def fetch_stocks(client_id: str, api_key: str, skus: tuple) -> list[dict]:
    """
    /v1/analytics/turnover/stocks — оборачиваемость и остатки по SKU (от Ozon).
    skus — кортеж числовых SKU (из sku_map_cache).
    Возвращает список с полями: offer_id, name, sku, current_stock, ads, idc, idc_grade, turnover, turnover_grade.
    """
    if not skus:
        return []
    all_items = []
    sku_list = list(skus)
    batch_size = 1000
    offset = 0
    while True:
        batch = sku_list[offset: offset + batch_size]
        if not batch:
            break
        body = {"sku": [str(s) for s in batch], "limit": len(batch), "offset": 0}
        data = api_post("/v1/analytics/turnover/stocks", body, client_id, api_key)
        if not data:
            break
        items = data.get("items") or []
        all_items.extend(items)
        if len(items) < batch_size:
            break
        offset += batch_size
    return all_items

@st.cache_data(ttl=3600, show_spinner=False)
def fetch_name_map(client_id: str, api_key: str) -> dict:
    """Строит {sku: название} из заказов FBO+FBS за 90 дней."""
    name_map = {}
    today = date.today()
    since = (today - timedelta(days=90)).strftime("%Y-%m-%d")
    to    = today.strftime("%Y-%m-%d")
    
    for schema, path in [("fbo", "/v3/posting/fbo/list"), ("fbs", "/v4/posting/fbs/list")]:
        try:
            offset = 0
            page_size = 100  # v3/fbo и v4/fbs оба имеют max=100
            while True:
                body = {
                    "dir": "ASC",
                    "filter": {
                        "since": since + "T00:00:00.000Z",
                        "to": to + "T23:59:59.000Z"
                    },
                    "limit": page_size,
                    "offset": offset,
                    "with": {"financial_data": False, "analytics_data": False}
                }
                resp = api_post(path, body, client_id, api_key)

                if not resp:
                    break

                result = resp.get("result", {})
                if schema == "fbo":
                    postings = result if isinstance(result, list) else result.get("postings", [])
                else:
                    postings = result.get("postings", []) if result else []

                if not postings:
                    break

                for p in postings:
                    if not isinstance(p, dict):
                        continue
                    for prod in (p.get("products") or []):
                        if not isinstance(prod, dict):
                            continue
                        sku = str(prod.get("sku") or "")
                        name = str(prod.get("name") or "")
                        if sku and name and sku not in name_map:
                            name_map[sku] = name

                if len(postings) < page_size:
                    break
                offset += len(postings)
        except Exception:
            pass
            
    return name_map

def transactions_to_df(ops: list[dict]) -> pd.DataFrame:
    """
    Разбираем ответ /v1/finance/accrual/by-day.
    """
    if not ops:
        return pd.DataFrame()

    rows = []
    for accrual in ops:
        if not isinstance(accrual, dict):
            continue

        date_str = accrual.get("_date", "")
        category = accrual.get("accrued_category", "")

        # ── POSTING: продажи/возвраты с выручкой и комиссией ──────────────────
        posting = accrual.get("posting") or {}
        products = posting.get("products") or []

        for prod in products:
            if not isinstance(prod, dict):
                continue

            sku = str(prod.get("sku") or "")
            if not sku:
                continue

            comm_block = prod.get("commission") or {}
            delivery_block = prod.get("delivery") or {}

            # Выручка (цена продавца * кол-во)
            sale_amount_block = (comm_block.get("sale_amount") or {}) if isinstance(comm_block, dict) else {}
            revenue_val = float((sale_amount_block.get("amount") or 0) if isinstance(sale_amount_block, dict) else 0)

            # Комиссия Ozon
            sale_comm = (comm_block.get("sale_commission") or {}) if isinstance(comm_block, dict) else {}
            commission_val = float((sale_comm.get("amount") or 0) if isinstance(sale_comm, dict) else 0)

            # Логистика = весь total_accrued (включает FBO + доставку партнёрами type_29 и др.)
            logi_block = (delivery_block.get("total_accrued") or {}) if isinstance(delivery_block, dict) else {}
            logistics_val = float((logi_block.get("amount") or 0) if isinstance(logi_block, dict) else 0)

            # Эквайринг — приходит через ITEM-начисления (item_fees), не из delivery.services
            acquiring_val = 0.0

            # Количество: sale_amount / seller_price (для мультизаказов)
            seller_price_block = (comm_block.get("seller_price") or {}) if isinstance(comm_block, dict) else {}
            seller_price_val = float((seller_price_block.get("amount") or 0) if isinstance(seller_price_block, dict) else 0)
            if seller_price_val > 0 and abs(revenue_val) > 0:
                qty = max(1, round(abs(revenue_val) / seller_price_val))
            else:
                qty = 1

            is_return = revenue_val < 0
            is_sale = not is_return and revenue_val != 0

            rows.append({
                "sku": sku,
                "article": sku,
                "name": "",
                "qty": qty if is_sale else 0,
                "qty_ret": qty if is_return else 0,
                "sale": revenue_val if is_sale else 0,
                "return": revenue_val if is_return else 0,
                "commission": commission_val,
                "logistics": logistics_val,
                "acquiring":   acquiring_val,
                "promo":       0.0,
                "installment": 0.0,
                "other_costs": 0.0,
            })

        # ── ITEM: доп. сборы по SKU — эквайринг, реклама, прочее ────────────
        item_fees_block = accrual.get("item_fees") or {}
        for sku_fees in (item_fees_block.get("fees") or []):
            if not isinstance(sku_fees, dict):
                continue
            sku = str(sku_fees.get("sku") or "")
            if not sku:
                continue
            for fee in (sku_fees.get("fees") or []):
                if not isinstance(fee, dict):
                    continue
                tid = _get_type_id(fee)
                amount = float((fee.get("accrued") or {}).get("amount") or 0)
                known = {ACQUIRING_TYPE_ID, INSTALLMENT_TYPE_ID} | PROMO_TYPE_IDS
                rows.append({
                    "sku": sku, "article": sku, "name": "",
                    "qty": 0, "qty_ret": 0, "sale": 0, "return": 0,
                    "commission": 0, "logistics": 0,
                    "acquiring":    amount if tid == ACQUIRING_TYPE_ID   else 0.0,
                    "promo":        amount if tid in PROMO_TYPE_IDS       else 0.0,
                    "installment":  amount if tid == INSTALLMENT_TYPE_ID  else 0.0,
                    "other_costs":  amount if tid not in known            else 0.0,
                })

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    for col in ["qty", "qty_ret", "sale", "return", "commission", "logistics", "acquiring", "promo", "installment", "other_costs"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    grouped = df.groupby(["sku", "article"]).agg(
        name=("name", "first"),
        qty=("qty", "sum"),
        qty_ret=("qty_ret", "sum"),
        sale=("sale", "sum"),
        return_sum=("return", "sum"),
        commission=("commission", "sum"),
        logistics=("logistics", "sum"),
        acquiring=("acquiring", "sum"),
        promo=("promo", "sum"),
        installment=("installment", "sum"),
        other_costs=("other_costs", "sum"),
    ).reset_index()

    grouped["revenue"] = grouped["sale"] + grouped["return_sum"]
    return grouped

def apply_sku_map(df: pd.DataFrame, sku_map: dict, name_map: dict = None) -> pd.DataFrame:
    """Заменяет числовой SKU на твой артикул (offer_id) и название через справочник."""
    if df.empty or not sku_map:
        return df
    df = df.copy()
    df["article"] = df["sku"].map(sku_map).fillna(df["article"])
    if name_map:
        df["name"] = df["sku"].map(name_map).fillna(df["name"])
    return df

def realization_to_df(rows: list[dict]) -> pd.DataFrame:
    """
    Парсим ответ /v2/finance/realization.
    Каждая строка — одна продажа. Группируем по offer_id.
    """
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        item = r.get("item") or {}
        dc = r.get("delivery_commission") or {}
        rc = r.get("return_commission")  # может быть null

        article = str(item.get("offer_id") or item.get("sku") or "—")
        name = str(item.get("name") or "")
        sku = str(item.get("sku") or "")

        qty_sold = int(dc.get("quantity") or 0)
        qty_ret = int((rc or {}).get("quantity") or 0)

        seller_price = float(r.get("seller_price_per_instance") or 0)
        revenue = seller_price * qty_sold
        return_amount = -(seller_price * qty_ret)

        # Комиссия Ozon = standard_fee (продажа + возврат)
        commission = -float(dc.get("standard_fee") or 0)
        if rc:
            commission += -float(rc.get("standard_fee") or 0)

        # Логистика = delivery_fee (доставка покупателю + доставка возврата)
        logistics = -float(dc.get("delivery_fee") or 0)
        if rc:
            logistics += -float(rc.get("delivery_fee") or 0)

        out.append({
            "article": article,
            "name": name,
            "sku": sku,
            "qty": qty_sold,
            "qty_ret": qty_ret,
            "revenue": revenue,
            "return_sum": return_amount,
            "commission": commission,
            "logistics": logistics,
            "acquiring":   0,
            "promo":       0,
            "installment": 0,
            "other_costs": 0,
        })

    if not out:
        return pd.DataFrame()

    df = pd.DataFrame(out)
    for col in ["qty", "qty_ret", "revenue", "return_sum", "commission", "logistics", "acquiring", "promo", "other_costs"]:
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    grouped = df.groupby(["article", "sku"]).agg(
        name=("name", "first"),
        qty=("qty", "sum"),
        qty_ret=("qty_ret", "sum"),
        revenue=("revenue", "sum"),
        return_sum=("return_sum", "sum"),
        commission=("commission", "sum"),
        logistics=("logistics", "sum"),
        acquiring=("acquiring", "sum"),
        promo=("promo", "sum"),
        installment=("installment", "sum"),
        other_costs=("other_costs", "sum"),
    ).reset_index()

    return grouped

def enrich_with_cost(df: pd.DataFrame, cost_map: dict) -> pd.DataFrame:
    """
    Добавляем себестоимость, налог, эквайринг и считаем прибыль.
    """
    if df.empty:
        return df

    df = df.copy()
    df["cost_price"] = df["article"].map(cost_map).fillna(0)
    df["cost_total"] = df["cost_price"] * df["qty"]
    # Если эквайринг пришёл из API (транзакции) — используем его; иначе расчётный %
    if "acquiring" not in df.columns or df["acquiring"].abs().sum() == 0:
        df["acquiring"] = ACQUIRING * df["revenue"]
    else:
        df["acquiring"] = df["acquiring"].abs()  # приводим к положительному для отображения расхода
    df["tax"] = TAX * df["revenue"]
    # promo и installment приводим к положительному (расход), как acquiring
    for col in ("promo", "installment"):
        if col in df.columns:
            df[col] = df[col].abs()
        else:
            df[col] = 0.0
    if "other_costs" not in df.columns:
        df["other_costs"] = 0.0
    df["profit"] = (
        df["revenue"]
        + df["commission"]     # отрицательная
        + df["logistics"]      # отрицательная
        - df["promo"]          # положительная (расход) — после abs()
        - df["installment"]    # положительная (расход) — после abs()
        + df["other_costs"]    # прочие (могут быть ± )
        - df["cost_total"]
        - df["acquiring"]
        - df["tax"]
    )
    df["margin_pct"] = (df["profit"] / df["revenue"].replace(0, float("nan"))) * 100
    return df

# ── Sidebar ─────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("⬡ Ozon P&L")
    st.caption("Дашборд юнит-экономики")

    st.divider()
    st.subheader("🔑 API-доступ")
    client_id = st.text_input("Client-ID", placeholder="123456")
    api_key = st.text_input("API-Key", type="password", placeholder="xxxx-xxxx-xxxx")

    st.divider()
    st.subheader("📅 Период")
    mode = st.radio("Режим", ["Текущий месяц", "Прошлый месяц", "Произвольные даты"])

    today = date.today()
    if mode == "Текущий месяц":
        d_from = today.replace(day=1)
        d_to = today
        use_transactions = True
    elif mode == "Прошлый месяц":
        first_this = today.replace(day=1)
        d_to = first_this - timedelta(days=1)
        d_from = d_to.replace(day=1)
        use_transactions = True
    else:
        d_from = st.date_input("Дата от", value=today.replace(day=1))
        d_to = st.date_input("Дата до", value=today)
        use_transactions = not (d_from.day == 1 and d_to == date(d_from.year, d_from.month,
            calendar.monthrange(d_from.year, d_from.month)[1]))

    st.caption(f"{'Транзакции' if use_transactions else 'Отчёт реализации'}: {d_from} — {d_to}")

    st.divider()
    st.subheader("💰 Себестоимость")

    cost_file = st.file_uploader(
        "Загрузи файл Excel/CSV: колонки «артикул» и «себестоимость»",
        type=["xlsx", "csv"],
        help="При изменении цен — просто загрузи новый файл"
    )

    if "cost_history" not in st.session_state:
        st.session_state.cost_history = []

    cost_map = {}

    if cost_file is not None:
        try:
            if cost_file.name.endswith(".csv"):
                cost_df = pd.read_csv(cost_file)
            else:
                cost_df = pd.read_excel(cost_file)

            cost_df.columns = [c.strip().lower() for c in cost_df.columns]
            art_col = next((c for c in cost_df.columns if "артикул" in c or "article" in c or "sku" in c), None)
            cost_col = next((c for c in cost_df.columns if "себест" in c or "cost" in c or "закуп" in c or "цена" in c), None)

            if art_col and cost_col:
                cost_df = cost_df.dropna(subset=[art_col])
                cost_df[art_col] = cost_df[art_col].astype(str).str.strip()
                cost_df = cost_df[cost_df[art_col].str.len() > 0]
                cost_df[cost_col] = pd.to_numeric(cost_df[cost_col], errors="coerce")
                cost_map = dict(zip(cost_df[art_col], cost_df[cost_col].fillna(0)))

                already = any(h["filename"] == cost_file.name and
                              h["date"] == date.today().strftime("%d.%m.%Y")
                              for h in st.session_state.cost_history)
                if not already:
                    st.session_state.cost_history.append({
                        "date": date.today().strftime("%d.%m.%Y"),
                        "filename": cost_file.name,
                        "count": len(cost_map),
                        "data": cost_map.copy(),
                    })
                st.success(f"✅ Загружено {len(cost_map)} артикулов")
            else:
                st.error("Не нашла колонки. Нужны: «артикул» и «себестоимость»")
        except Exception as e:
            st.error(f"Ошибка: {e}")

    if not cost_map and st.session_state.cost_history:
        last = st.session_state.cost_history[-1]
        cost_map = last["data"]
        st.info(f"Используется: {last['filename']} от {last['date']}")

    if st.session_state.cost_history:
        with st.expander(f"📜 История загрузок ({len(st.session_state.cost_history)})"):
            for h in reversed(st.session_state.cost_history):
                st.caption(f"📅 {h['date']} — {h['filename']} ({h['count']} артикулов)")

    load_btn = st.button("🔄 Загрузить данные", type="primary", use_container_width=True)
    demo_btn = st.button("🎲 Демо-данные", use_container_width=True)
    debug_btn = st.button("🔍 Показать сырые данные API", use_container_width=True)

# ── Demo data ────────────────────────────────────────────────────────────────
def make_demo() -> pd.DataFrame:
    import random
    random.seed(42)
    skus = [
        ("11860",   "pjur Woman Nude 100мл",      1204, 3251, 45, 289),
        ("6033487", "Фаллоим. UTOPIA S 13.5см",   3100, 7400, 40, 322),
        ("O17182",  "Orgie Cocktail Тропик 50мл",  1100, 2769, 39, 195),
        ("6022351", "MixGliss Nature 150мл",        1800, 3410, 39, 238),
        ("3003Un",  "Unilatex Natural 3шт",           75,  299, 29,  42),
        ("3017Un",  "Unilatex Multifruits 3шт",       75,  309, 29,  42),
        ("541438",  "Erotist VIVID ACT 30мл",         320, 1478, 45, 110),
        ("6033081", "LoveToLove SUNRISE",            3900, 7965, 40, 276),
        ("602211",  "MixGliss SUN 50мл",              900, 3733, 39, 195),
        ("51652",   "Orgie Orgasm Drops Vibe!",      1200, 3139, 39, 168),
    ]
    rows = []
    for art, name, cost, price, comm_pct, log in skus:
        qty = random.randint(3, 45)
        qret = max(0, random.randint(0, 2))
        rev = price * qty
        comm = (comm_pct / 100) * rev
        acq = ACQUIRING * rev
        tx = TAX * rev
        logi = log * qty
        rows.append({
            "article": art,
            "name": name,
            "qty": qty,
            "qty_ret": qret,
            "revenue": rev,
            "return_sum": -(price * qret),
            "commission": -comm,
            "logistics": -logi,
            "cost_price": cost,
            "cost_total": cost * qty,
            "acquiring": acq,
            "tax": tx,
            "profit": rev - cost * qty - comm - acq - tx - logi,
            "other": 0,
        })
    df = pd.DataFrame(rows)
    df["margin_pct"] = (df["profit"] / df["revenue"].replace(0, float("nan"))) * 100
    return df

# ── State ────────────────────────────────────────────────────────────────────
if "df" not in st.session_state:
    st.session_state.df = None
if "is_demo" not in st.session_state:
    st.session_state.is_demo = False
if "raw_ops" not in st.session_state:
    st.session_state.raw_ops = []
if "sku_map_cache" not in st.session_state:
    st.session_state.sku_map_cache = {}
if "store_costs" not in st.session_state:
    st.session_state.store_costs = {}
if "loaded_period" not in st.session_state:
    st.session_state.loaded_period = None  # (d_from, d_to) последней успешной загрузки
if "stocks_data" not in st.session_state:
    st.session_state.stocks_data = []

if demo_btn:
    st.session_state.df = make_demo()
    st.session_state.is_demo = True

if debug_btn:
    if not client_id or not api_key:
        st.error("Введи Client-ID и API-Key в боковом меню")
    else:
        st.subheader("🔍 Сырые данные API (для диагностики)")
        today_str = date.today().strftime("%Y-%m-%d")
        prev_month = date.today().replace(day=1) - timedelta(days=1)

        with st.expander("📄 /v2/finance/realization — прошлый месяц (первые 2 строки)", expanded=True):
            with st.spinner("Запрос..."):
                raw_real = api_post(
                    "/v2/finance/realization",
                    {"year": prev_month.year, "month": prev_month.month},
                    client_id, api_key,
                )
            result = (raw_real.get("result") or {}) if raw_real else {}
            rows = (result.get("rows") or [])[:2]
            if rows:
                import json
                st.code(json.dumps(rows, ensure_ascii=False, indent=2), language="json")
            else:
                st.warning("Нет данных (отчёт формируется в начале следующего месяца)")
                st.json(raw_real or {})

        with st.expander("📄 /v1/finance/accrual/by-day — вчера (первые 2 начисления)", expanded=True):
            yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
            with st.spinner("Запрос..."):
                raw_tx = api_post(
                    "/v1/finance/accrual/by-day",
                    {"date": yesterday, "last_id": ""},
                    client_id, api_key,
                )
            accruals = (raw_tx.get("accruals") or [])[:2] if raw_tx else []
            if accruals:
                import json
                st.code(json.dumps(accruals, ensure_ascii=False, indent=2), language="json")
            else:
                st.warning("Нет начислений за вчера")
                st.json(raw_tx or {})

        st.info("Скопируй содержимое блоков выше и пришли — найдём поле эквайринга.")

if load_btn:
    if not client_id or not api_key:
        st.error("Введи Client-ID и API-Key в боковом меню")
    else:
        # Сбрасываем старые данные ДО запроса — чтобы не показывать чужой период при ошибке
        st.session_state.df = None
        st.session_state.loaded_period = None
        st.session_state.store_costs = {}
        with st.spinner("Загружаем данные из Ozon API..."):
            try:
                if use_transactions:
                    ops = fetch_transactions(client_id, api_key,
                                             d_from.strftime("%Y-%m-%d"),
                                             d_to.strftime("%Y-%m-%d"))
                    raw_df = transactions_to_df(ops)
                else:
                    rows_api = fetch_realization(client_id, api_key, d_from.year, d_from.month)
                    raw_df = realization_to_df(rows_api)

                if raw_df.empty:
                    st.warning("Нет данных за выбранный период. Отчёт реализации формируется Ozon в начале следующего месяца.")
                else:
                    # Загружаем справочник типов начислений (type_id → название)
                    with st.spinner("Загружаем справочник типов начислений..."):
                        api_type_names = fetch_accrual_types(client_id, api_key)
                        if api_type_names:
                            TYPE_NAMES.update(api_type_names)

                    # Загружаем справочник sku → offer_id
                    with st.spinner("Загружаем справочник артикулов..."):
                        sku_map = fetch_sku_map(client_id, api_key)
                    if sku_map:
                        name_map = fetch_name_map(client_id, api_key)
                        raw_df = apply_sku_map(raw_df, sku_map, name_map)
                        st.caption(f"Справочник: {len(sku_map)} товаров, {len(name_map) if name_map else 0} названий")

                    if use_transactions:
                        # Транзакционный режим — логистика уже в raw_df
                        st.session_state.raw_ops = ops
                        st.session_state.sku_map_cache = sku_map if sku_map else {}
                        st.session_state.store_costs = collect_store_costs(ops)
                    else:
                        # Реализация: транзакции для логистики + расходов магазина
                        # Запрашиваем ДО enrich_with_cost, чтобы логистика попала в расчёт прибыли
                        with st.spinner("Загружаем логистику и расходы магазина..."):
                            try:
                                ops_for_costs = fetch_transactions(
                                    client_id, api_key,
                                    d_from.strftime("%Y-%m-%d"),
                                    d_to.strftime("%Y-%m-%d"),
                                )
                                st.session_state.store_costs = collect_store_costs(ops_for_costs)
                                st.session_state.raw_ops = ops_for_costs
                                st.session_state.sku_map_cache = sku_map if sku_map else {}

                                # Суммируем логистику по артикулам из транзакций
                                _logi: dict[str, float] = {}
                                for _a in ops_for_costs:
                                    _p = _a.get("posting") or {}
                                    for _pr in (_p.get("products") or []):
                                        if not isinstance(_pr, dict):
                                            continue
                                        _sku = str(_pr.get("sku") or "")
                                        if not _sku:
                                            continue
                                        _oid = (sku_map or {}).get(_sku, _sku)
                                        _d = _pr.get("delivery") or {}
                                        _v = float(((_d.get("total_accrued") or {}).get("amount") or 0))
                                        if _v:
                                            _logi[_oid] = _logi.get(_oid, 0.0) + _v
                                # Обновляем logistics в raw_df ДО enrich_with_cost
                                if _logi:
                                    raw_df["logistics"] = raw_df["article"].map(_logi).fillna(0.0)
                            except Exception:
                                pass  # расходы магазина необязательны

                    # enrich_with_cost вызывается ПОСЛЕ обновления логистики
                    st.session_state.df = enrich_with_cost(raw_df, cost_map)
                    st.session_state.is_demo = False
                    st.session_state.loaded_period = (d_from, d_to)
                    with st.spinner("Загружаем остатки и оборачиваемость..."):
                        skus_tuple = tuple(sorted(sku_map.keys())) if sku_map else ()
                        st.session_state.stocks_data = fetch_stocks(client_id, api_key, skus_tuple)
            except Exception as e:
                st.error(f"Ошибка загрузки: {e}")

# ── Main ─────────────────────────────────────────────────────────────────────
df = st.session_state.df

if df is None:
    st.title("📊 Ozon P&L Dashboard")
    st.info("👈 Введи API-ключи и нажми **Загрузить данные** — или попробуй **Демо-данные**")
    with st.expander("Как получить API-ключи?"):
        st.markdown("""
1. Зайди в [seller.ozon.ru](https://seller.ozon.ru)
2. **Настройки → API-ключи**
3. Нажми «Создать ключ», выбери роль **Финансы** (или Администратор)
4. Скопируй **Client-ID** и **API-Key** → вставь в меню слева
        """)
    st.stop()

if st.session_state.is_demo:
    st.warning("⚠️ Демо-данные. Введи API-ключи для реальных цифр.", icon="🎲")

# ── Заголовок ─────────────────────────────────────────────────────────────────
loaded_period = st.session_state.get("loaded_period")
if loaded_period and not st.session_state.is_demo:
    loaded_from, loaded_to = loaded_period
    if loaded_from != d_from or loaded_to != d_to:
        st.warning(
            f"⚠️ Данные загружены за **{loaded_from.strftime('%d.%m.%Y')} — {loaded_to.strftime('%d.%m.%Y')}**, "
            f"а выбран период **{d_from.strftime('%d.%m.%Y')} — {d_to.strftime('%d.%m.%Y')}**. "
            "Нажми **Загрузить данные** для обновления."
        )
    days = (loaded_to - loaded_from).days + 1
    st.title(f"📊 P&L: {loaded_from.strftime('%d.%m')} — {loaded_to.strftime('%d.%m.%Y')}")
else:
    days = (d_to - d_from).days + 1
    st.title(f"📊 P&L: {d_from.strftime('%d.%m')} — {d_to.strftime('%d.%m.%Y')}")

# ── KPI ───────────────────────────────────────────────────────────────────────
total_rev  = df["revenue"].sum()
total_prof = df["profit"].sum()
total_comm = df["commission"].abs().sum()
total_log  = df["logistics"].abs().sum() if "logistics" in df.columns else 0
total_cost = df["cost_total"].sum() if "cost_total" in df.columns else 0
total_qty  = df["qty"].sum()

# Расходы магазина нужны ДО метрик — чтобы показать реальную прибыль и правильную маржу
_sc_kpi: dict = st.session_state.get("store_costs", {})
_sc_total_kpi = sum(v for v in _sc_kpi.values() if v < 0) if _sc_kpi else 0
total_prof_adj = total_prof + _sc_total_kpi   # реальная прибыль = прибыль по артикулам − расходы магазина
total_margin = (total_prof_adj / total_rev * 100) if total_rev else 0

comm_pct = (total_comm / total_rev * 100) if total_rev else 0
log_pct  = (total_log  / total_rev * 100) if total_rev else 0
cost_pct = (total_cost / total_rev * 100) if total_rev else 0

c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
c1.metric("Выручка", r(total_rev), f"{total_rev/days:,.0f} ₽/день".replace(",", " "))
c2.metric("Реальная прибыль", r(total_prof_adj), f"{total_prof_adj/days:,.0f} ₽/день".replace(",", " "),
          delta_color="normal" if total_prof_adj >= 0 else "inverse")
c3.metric("Маржа", ru_pct(total_margin), delta_color="normal" if total_margin >= 5 else "inverse")
c4.metric("Продано", f"{int(total_qty)} шт", f"{total_qty/days:.1f}".replace(".", ",") + " шт/день")
with c5:
    st.metric("Себестоимость", r(total_cost))
    st.caption(f"({ru_pct(cost_pct)} от выручки)")
with c6:
    st.metric("Комиссия", r(total_comm))
    st.caption(f"({ru_pct(comm_pct)} от выручки)")
with c7:
    st.metric("Логистика", r(total_log))
    st.caption(f"({ru_pct(log_pct)} от выручки)")

# ── Расходы магазина (non_item_fee) ─────────────────────────────────────────
store_costs: dict = _sc_kpi   # уже загружено выше для KPI
if store_costs:
    store_total = _sc_total_kpi   # уже вычислено выше
    # total_prof_adj уже вычислен выше

    with st.expander(f"🏪 Расходы магазина: {r(abs(store_total))} (не входят в таблицу по артикулам)", expanded=False):
        sc_rows = []
        for tid, amt in sorted(store_costs.items(), key=lambda x: x[1]):
            if amt == 0:
                continue
            sc_rows.append({
                "Статья": TYPE_NAMES.get(tid, f"type_{tid}"),
                "Группа": STORE_COST_GROUPS.get(tid, "Прочее"),
                "Сумма": amt,
            })
        if sc_rows:
            sc_df = pd.DataFrame(sc_rows)
            by_group = sc_df.groupby("Группа")["Сумма"].sum().reset_index().sort_values("Сумма")
            col_sc1, col_sc2 = st.columns([1, 2])
            with col_sc1:
                st.markdown("**По группам:**")
                for _, row in by_group.iterrows():
                    st.write(f"{row['Группа']}: **{r(abs(row['Сумма']))}**")
            with col_sc2:
                st.markdown("**Детализация:**")
                st.dataframe(
                    sc_df.style.format({"Сумма": lambda v: ru_float(v, 2)}),
                    use_container_width=True, hide_index=True,
                )
        sa1, sa2, sa3 = st.columns(3)
        sa1.metric("Прибыль по артикулам", r(total_prof))
        sa2.metric("Расходы магазина", r(abs(store_total)))
        sa3.metric("Реальная прибыль", r(total_prof_adj),
                   delta_color="normal" if total_prof_adj >= 0 else "inverse")

st.divider()

# ── Таблица по артикулам ───────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs(["📋 По артикулам", "📦 Остатки", "📈 Диаграммы", "🔢 Калькулятор", "🔍 Детализация"])

with tab1:
    show_cols = ["article", "name", "qty", "revenue", "cost_total",
                 "commission", "acquiring", "tax", "logistics", "promo", "installment", "other_costs", "profit", "margin_pct"]
    available = [c for c in show_cols if c in df.columns]
    display_df = df[available].copy()

    rename = {
        "article": "Артикул",
        "name": "Товар",
        "qty": "Продано",
        "revenue": "Выручка",
        "cost_total": "Себестоимость",
        "commission": "Комиссия",
        "acquiring": "Эквайринг",
        "tax": "Налог",
        "logistics": "Логистика",
        "promo":       "Реклама",
        "installment": "Рассрочка",
        "other_costs": "Прочие расходы",
        "profit": "Прибыль",
        "margin_pct": "Маржа %",
    }
    display_df = display_df.rename(columns=rename).sort_values("Прибыль", ascending=False)

    # Форматирование с пробелом как разделителем тысяч
    def _fmt_rub(v):
        if pd.isna(v): return "—"
        return ru_rub(v, 0)

    rub_cols = ["Выручка", "Себестоимость", "Комиссия", "Эквайринг", "Налог", "Логистика", "Реклама", "Рассрочка", "Прочие расходы", "Прибыль"]
    fmt_dict = {c: _fmt_rub for c in rub_cols if c in display_df.columns}
    if "Маржа %" in display_df.columns:
        fmt_dict["Маржа %"] = ru_pct

    def color_margin(val):
        if pd.isna(val):
            return ""
        if val >= 10:
            return "color: #3DD68C; font-weight: bold"
        if val >= 5:
            return "color: #F0A93D"
        return "color: #F05B5B"

    def color_profit(val):
        if pd.isna(val):
            return ""
        return "color: #3DD68C" if val >= 0 else "color: #F05B5B"

    styled = (
        display_df.style
        .format(fmt_dict, na_rep="—")
        .map(color_margin, subset=["Маржа %"] if "Маржа %" in display_df.columns else [])
        .map(color_profit, subset=["Прибыль"] if "Прибыль" in display_df.columns else [])
    )
    st.dataframe(styled, use_container_width=True, height=420)

    # Итого
    st.markdown("**Итого по всем артикулам:**")
    _total_promo = df["promo"].abs().sum() if "promo" in df.columns else 0
    _total_acq   = df["acquiring"].abs().sum() if "acquiring" in df.columns else 0
    _total_inst  = df["installment"].abs().sum() if "installment" in df.columns else 0
    ci1, ci2, ci3, ci4 = st.columns(4)
    ci1.metric("Выручка", r(total_rev))
    ci2.metric("Прибыль", r(total_prof))
    ci3.metric("Маржа", ru_pct(total_margin))
    ci4.metric("Расходы (комиссия + логистика)", r(total_comm + total_log))
    ci5, ci6, ci7, ci8 = st.columns(4)
    ci5.metric("Реклама (per-артикул)", r(_total_promo))
    ci6.metric("Эквайринг (per-артикул)", r(_total_acq))
    ci7.metric("Рассрочка (per-артикул)", r(_total_inst))
    ci8.metric("Налог (расчётный)", r(df["tax"].sum() if "tax" in df.columns else 0))

    # Скачать Excel (форматированный — для просмотра)
    @st.cache_data
    def to_excel_display(df_in):
        """Форматированный Excel: числа с ₽ и пробелами. Артикулы — текст."""
        import io
        from openpyxl import load_workbook
        buf = io.BytesIO()
        df_in.to_excel(buf, index=False)
        buf.seek(0)
        wb = load_workbook(buf)
        ws = wb.active
        # Находим колонку Артикул и принудительно ставим текстовый формат
        art_col_idx = None
        for col_idx, cell in enumerate(ws[1], 1):
            if cell.value == "Артикул":
                art_col_idx = col_idx
                break
        if art_col_idx:
            for row in ws.iter_rows(min_row=2, min_col=art_col_idx, max_col=art_col_idx):
                for c in row:
                    c.number_format = "@"
                    if c.value is not None:
                        c.value = str(c.value)
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    @st.cache_data
    def to_excel_raw(df_in):
        """Сырой Excel: чистые числа без ₽, артикулы — текст. Для расчётов."""
        import io
        from openpyxl import load_workbook
        buf = io.BytesIO()
        # Копируем нужные колонки с чистыми числами
        raw_cols = {
            "article":     "Артикул",
            "name":        "Товар",
            "qty":         "Продано шт",
            "revenue":     "Выручка",
            "cost_total":  "Себестоимость",
            "commission":  "Комиссия",
            "acquiring":   "Эквайринг",
            "tax":         "Налог",
            "logistics":   "Логистика",
            "promo":       "Реклама",
            "installment": "Рассрочка",
            "other_costs": "Прочие расходы",
            "profit":      "Прибыль",
            "margin_pct":  "Маржа %",
        }
        available = {k: v for k, v in raw_cols.items() if k in df_in.columns}
        raw_df = df_in[list(available.keys())].copy()
        raw_df = raw_df.rename(columns=available)
        # Артикул — строго текст
        raw_df["Артикул"] = raw_df["Артикул"].astype(str)
        raw_df.to_excel(buf, index=False)
        buf.seek(0)
        # Принудительно ставим текстовый формат на Артикул
        wb = load_workbook(buf)
        ws = wb.active
        for col_idx, cell in enumerate(ws[1], 1):
            if cell.value == "Артикул":
                for row in ws.iter_rows(min_row=2, min_col=col_idx, max_col=col_idx):
                    for c in row:
                        c.number_format = "@"
                        if c.value is not None:
                            c.value = str(c.value)
                break
        out = io.BytesIO()
        wb.save(out)
        return out.getvalue()

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        st.download_button(
            "⬇️ Скачать в Excel (отображение)",
            data=to_excel_display(display_df),
            file_name=f"ozon_pnl_{d_from}_{d_to}.xlsx",
            mime="application/vnd.ms-excel",
            help="С форматированием: числа с ₽, удобно для просмотра",
        )
    with btn_col2:
        st.download_button(
            "⬇️ Скачать данные (чистые числа)",
            data=to_excel_raw(df),
            file_name=f"ozon_pnl_raw_{d_from}_{d_to}.xlsx",
            mime="application/vnd.ms-excel",
            help="Чистые числа без ₽, артикулы как текст — для ВПР и расчётов в своём файле",
        )

    # ── Средняя логистика по артикулу ─────────────────────────────────────────
    st.divider()
    st.subheader("📦 Средняя логистика на единицу товара")
    if "logistics" in df.columns:
        log_df = df[df["logistics"].abs() > 0][["article", "name", "qty", "logistics"]].copy()
        log_df["logistics_abs"] = log_df["logistics"].abs()

        # Считаем уникальные отправления с ненулевой доставкой из raw_ops
        # Это точнее, чем qty_продаж — логистика может быть начислена раньше выручки
        raw_ops_log = st.session_state.get("raw_ops", [])
        sku_map_log  = st.session_state.get("sku_map_cache", {})
        shipment_sets: dict[str, set] = {}
        if raw_ops_log:
            for accrual in raw_ops_log:
                unit = accrual.get("unit_number", "")
                posting = accrual.get("posting") or {}
                for prod in (posting.get("products") or []):
                    if not isinstance(prod, dict):
                        continue
                    sku = str(prod.get("sku") or "")
                    offer_id = sku_map_log.get(sku, sku)
                    deliv = prod.get("delivery") or {}
                    total_v = float(((deliv.get("total_accrued") or {}).get("amount") or 0))
                    if total_v != 0 and unit:
                        shipment_sets.setdefault(offer_id, set()).add(unit)

        shipment_counts = {k: len(v) for k, v in shipment_sets.items()}
        if shipment_counts:
            log_df["отправлений"] = log_df["article"].map(shipment_counts).fillna(
                log_df["qty"].clip(lower=1)
            )
        else:
            log_df["отправлений"] = log_df["qty"].clip(lower=1)

        log_df["лог/отпр"] = (log_df["logistics_abs"] / log_df["отправлений"]).round(1)

        log_df = log_df.rename(columns={
            "article":      "Артикул",
            "name":         "Товар",
            "qty":          "Продано (финанс.)",
            "отправлений":  "Отправлений",
            "logistics_abs":"Логистика всего",
            "лог/отпр":     "Лог/отправление (ср.)",
        })
        log_df = log_df[[
            "Артикул", "Товар", "Продано (финанс.)", "Отправлений",
            "Логистика всего", "Лог/отправление (ср.)"
        ]].sort_values("Лог/отправление (ср.)", ascending=False)

        def color_logprice(val):
            if pd.isna(val): return ""
            if val >= 400: return "color: #F05B5B; font-weight: bold"
            if val >= 250: return "color: #F0A93D"
            return "color: #3DD68C"

        st.dataframe(
            log_df.style
            .format({
                "Продано (финанс.)":     lambda v: ru_float(v, 0),
                "Отправлений":           lambda v: ru_float(v, 0),
                "Логистика всего":       _fmt_rub,
                "Лог/отправление (ср.)": lambda v: ru_rub(v, 1),
            })
            .map(color_logprice, subset=["Лог/отправление (ср.)"]),
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "🔴 ≥400 ₽ — высокая логистика (проверь упаковку/склад) · 🟡 250–400 ₽ · 🟢 <250 ₽  \n"
            "**Отправлений** — уникальные заказы с начисленной логистикой. "
            "**Продано (финанс.)** — продажи с зачисленной выручкой (могут не совпадать из-за задержки начислений)."
        )

with tab2:
    # ── Остатки и оборачиваемость ──────────────────────────────────────────────
    st.subheader("📦 Остатки и оборачиваемость")
    stocks_data = st.session_state.get("stocks_data", [])

    if not stocks_data:
        st.info("Остатки загружаются вместе с данными. Нажми **Загрузить данные** в боковом меню.")
    else:
        # Данные из /v1/analytics/turnover/stocks
        # Поля: offer_id, name, sku, current_stock, ads, idc, idc_grade, turnover, turnover_grade
        stocks_df = pd.DataFrame(stocks_data)

        # Нужные колонки — добавляем если отсутствуют
        for col, default in [("offer_id",""), ("name",""), ("current_stock",0),
                              ("ads",0.0), ("idc",0.0), ("idc_grade",""), ("turnover",0.0), ("turnover_grade","")]:
            if col not in stocks_df.columns:
                stocks_df[col] = default

        stocks_df["offer_id"] = stocks_df["offer_id"].astype(str)

        # Переводим оценки в читаемый вид
        stocks_df["Статус запаса"] = stocks_df["idc_grade"].map(TURNOVER_GRADE_RU).fillna(stocks_df["idc_grade"])
        stocks_df["Статус оборач."] = stocks_df["turnover_grade"].map(TURNOVER_GRADE_RU).fillna(stocks_df["turnover_grade"])

        # Продажи из P&L за период (для сравнения)
        loaded_period = st.session_state.get("loaded_period")
        if loaded_period:
            p_from, p_to = loaded_period
            period_days_calc = max(1, (p_to - p_from).days + 1)
        else:
            period_days_calc = max(1, days)

        sales_df = df[df["qty"] > 0][["article", "qty"]].copy()
        sales_df = sales_df.rename(columns={"article": "offer_id"})
        stocks_df = stocks_df.merge(sales_df, on="offer_id", how="left")
        stocks_df["qty"] = stocks_df["qty"].fillna(0)
        stocks_df["Продано (P&L)"] = stocks_df["qty"].astype(int)

        # Сортировка — сначала критичные
        grade_order = {
            "GRADES_CRITICAL": 0, "GRADES_RED": 1, "GRADES_YELLOW": 2,
            "GRADES_GREEN": 3, "GRADES_NOSALES": 4, "GRADES_NONE": 5, "": 6,
        }
        stocks_df["_sort"] = stocks_df["idc_grade"].map(grade_order).fillna(6)
        stocks_df = stocks_df.sort_values(["_sort", "idc"])

        # Итоговая таблица
        out_df = stocks_df[[
            "offer_id", "name", "current_stock", "ads", "idc", "turnover",
            "Статус запаса", "Статус оборач.", "Продано (P&L)"
        ]].rename(columns={
            "offer_id":      "Артикул",
            "name":          "Товар",
            "current_stock": "Остаток",
            "ads":           "Ср. прод/день (Ozon)",
            "idc":           "Дней запаса",
            "turnover":      "Оборач. (дни)",
        })

        def color_idc(val):
            if pd.isna(val): return ""
            if val == 0: return "color: #F05B5B; font-weight: bold"
            if val < 28: return "color: #F05B5B; font-weight: bold"
            if val < 56: return "color: #F0A93D"
            return "color: #3DD68C"

        st.dataframe(
            out_df.style
            .format({
                "Остаток":               lambda v: ru_float(v, 0),
                "Ср. прод/день (Ozon)":  lambda v: ru_float(v, 2),
                "Дней запаса":           lambda v: ru_float(v, 1) if not pd.isna(v) else "—",
                "Оборач. (дни)":         lambda v: ru_float(v, 1) if not pd.isna(v) else "—",
                "Продано (P&L)":         lambda v: ru_float(v, 0),
            })
            .map(color_idc, subset=["Дней запаса"]),
            use_container_width=True,
            height=500,
            hide_index=True,
        )

        st.caption(
            "**Дней запаса** и **Ср. прод/день** — данные Ozon за 60 дней. "
            "**Оборач.** — фактическая оборачиваемость в днях. "
            "**Продано (P&L)** — за выбранный период из P&L."
        )

        # KPI-сводка
        grade_counts = stocks_df["idc_grade"].value_counts()
        ks1, ks2, ks3, ks4, ks5 = st.columns(5)
        ks1.metric("🔴 Критично",   int(grade_counts.get("GRADES_CRITICAL", 0)) + int(grade_counts.get("GRADES_RED", 0)))
        ks2.metric("🟡 Средне",     int(grade_counts.get("GRADES_YELLOW", 0)))
        ks3.metric("🟢 Хорошо",     int(grade_counts.get("GRADES_GREEN", 0)))
        ks4.metric("⚪ Нет продаж", int(grade_counts.get("GRADES_NOSALES", 0)))
        ks5.metric("⏳ Нет остатка", int(grade_counts.get("GRADES_NONE", 0)))

        # Предупреждения по критичным позициям
        critical_items = out_df[out_df["Дней запаса"] < 28].copy()
        critical_items = critical_items[critical_items["Дней запаса"] > 0]
        if not critical_items.empty:
            st.warning(
                f"⚠️ {len(critical_items)} товаров с запасом менее 28 дней. "
                "Требуется пополнение или включать рекламу только после завоза."
            )
            with st.expander("Критичные позиции", expanded=True):
                st.dataframe(
                    critical_items[["Артикул", "Товар", "Остаток", "Ср. прод/день (Ozon)", "Дней запаса", "Статус запаса"]],
                    use_container_width=True, hide_index=True,
                )

        # Скачать
        @st.cache_data
        def stocks_to_excel(df_in):
            import io
            buf = io.BytesIO()
            df_in.to_excel(buf, index=False)
            return buf.getvalue()

        st.download_button(
            "⬇️ Скачать остатки в Excel",
            data=stocks_to_excel(out_df),
            file_name="ozon_stocks.xlsx",
            mime="application/vnd.ms-excel",
        )

with tab3:
    if total_rev > 0:
        # Структура расходов (pie)
        st.subheader("Структура выручки")
        breakdown = {
            "Прибыль": max(0, total_prof),
            "Себестоимость": total_cost,
            "Комиссия": total_comm,
            "Логистика": total_log,
            "Налог": df["tax"].sum() if "tax" in df.columns else 0,
            "Эквайринг": df["acquiring"].sum() if "acquiring" in df.columns else 0,
            "Реклама":   df["promo"].abs().sum()       if "promo"       in df.columns else 0,
            "Рассрочка": df["installment"].abs().sum() if "installment" in df.columns else 0,
        }
        pie_df = pd.DataFrame({
            "Статья": list(breakdown.keys()),
            "Сумма": list(breakdown.values()),
        })
        st.bar_chart(pie_df.set_index("Статья"), horizontal=True)

        # Топ по прибыли
        st.subheader("Топ артикулов по прибыли")
        top = df.nlargest(10, "profit")[["name", "profit", "margin_pct"]].set_index("name")
        st.bar_chart(top["profit"])

with tab4:
    st.subheader("🔢 Калькулятор юнит-экономики")
    st.caption("Считает маржу по правильной формуле с учётом типа доставки")

    col_l, col_r = st.columns([1, 1])

    with col_l:
        c_price = st.number_input("Цена продажи, ₽", value=3251, step=50)
        c_cost = st.number_input("Себестоимость, ₽", value=1204, step=50)
        c_comm = st.number_input("Комиссия Ozon, %", value=45, step=1, min_value=0, max_value=100)
        c_log = st.number_input("Логистика FBO, ₽", value=289, step=10)
        c_qty = st.number_input("Количество в заказе", value=1, step=1, min_value=1)
        c_mode = st.selectbox("Тип доставки", ["Экспресс (realFBS)", "FBS", "FBO"])

    revenue = c_price * c_qty
    cost_t = c_cost * c_qty
    commission = (c_comm / 100) * revenue
    acquiring = ACQUIRING * revenue
    tax = TAX * revenue

    if "Экспресс" in c_mode:
        logistics = express_cost(revenue)
        agent = AGENT_FEE
    elif c_mode == "FBS":
        logistics = c_log * c_qty
        agent = 25
    else:  # FBO
        logistics = c_log * c_qty
        agent = 0

    total_exp = commission + acquiring + tax + logistics + agent
    profit = revenue - cost_t - total_exp
    margin = (profit / revenue * 100) if revenue else 0

    with col_r:
        color = "green" if margin >= 5 else ("orange" if margin >= 0 else "red")
        st.markdown(f"### Маржа: :{color}[{ru_pct(margin)}]")
        st.markdown(f"### Прибыль: :{color}[{r(profit)}]")

        st.markdown("---")
        breakdown_calc = {
            "Выручка": revenue,
            "− Себестоимость": -cost_t,
            f"− Комиссия {c_comm}%": -commission,
            f"− Эквайринг {ru_pct(ACQUIRING*100)}": -acquiring,
            "− Налог УСН 7%": -tax,
            f"− {'Экспресс' if 'Экспресс' in c_mode else 'Логистика'}": -logistics,
        }
        if agent:
            breakdown_calc["− Агентское / посл.миля"] = -agent

        breakdown_calc["= ПРИБЫЛЬ"] = profit

        for label, val in breakdown_calc.items():
            col_a, col_b = st.columns([3, 1])
            col_a.write(label)
            col_b.write(f"**{r(val)}**")

        if "Экспресс" in c_mode:
            st.info(f"Экспресс-тариф: заказ {r(revenue)} → **{logistics:.0f} ₽**\n\n"
                    "до 2 000 → 300 · до 4 000 → 400 · до 7 500 → 500 · до 20 000 → 600 · от 20 000 → 800")

with tab5:
    st.subheader("🔍 Детализация по заказам")

    raw_ops = st.session_state.get("raw_ops", [])
    sku_map_cache = st.session_state.get("sku_map_cache", {})

    if not raw_ops:
        st.info("Данные детализации доступны после загрузки в режиме 'Текущий месяц' или 'Произвольные даты'.")
        st.stop()

    # ── Диагностика структуры API ──────────────────────────────────────────────
    with st.expander("🔧 Диагностика API", expanded=False):
        import json

        # 1. Категории начислений и их количество
        cat_counts: dict[str, int] = {}
        cat_has_item_fees: dict[str, int] = {}
        for _a in raw_ops:
            _cat = _a.get("accrued_category", "UNKNOWN")
            cat_counts[_cat] = cat_counts.get(_cat, 0) + 1
            _ifs = _a.get("item_fees")
            if _ifs and isinstance(_ifs, dict) and (_ifs.get("fees") or []):
                cat_has_item_fees[_cat] = cat_has_item_fees.get(_cat, 0) + 1

        st.subheader("Категории начислений")
        _cat_rows = [{"Категория": k, "Кол-во": v,
                      "С item_fees": cat_has_item_fees.get(k, 0)} for k, v in sorted(cat_counts.items())]
        st.dataframe(pd.DataFrame(_cat_rows), hide_index=True, use_container_width=True)

        # 2. Логистика: сопоставленная vs несопоставленная (numeric SKU без offer_id)
        st.subheader("Логистика: сопоставлено / несопоставлено")
        _logi_matched = 0.0
        _logi_unmatched = 0.0
        _unmatched_skus: dict[str, float] = {}
        for _a in raw_ops:
            _p = _a.get("posting") or {}
            for _pr in (_p.get("products") or []):
                if not isinstance(_pr, dict): continue
                _sku = str(_pr.get("sku") or "")
                _v = float((((_pr.get("delivery") or {}).get("total_accrued") or {}).get("amount") or 0))
                if not _v: continue
                if sku_map_cache.get(_sku):
                    _logi_matched += _v
                else:
                    _logi_unmatched += _v
                    _unmatched_skus[_sku] = _unmatched_skus.get(_sku, 0.0) + _v
        st.metric("Сопоставлено (есть offer_id)", f"{abs(_logi_matched):,.0f} ₽")
        st.metric("Несопоставлено (нет в справочнике)", f"{abs(_logi_unmatched):,.0f} ₽")
        if _unmatched_skus:
            st.caption("Несопоставленные SKU:")
            st.dataframe(pd.DataFrame([{"SKU": k, "Логистика": v}
                                        for k, v in _unmatched_skus.items()]),
                         hide_index=True, use_container_width=True)

        # 3. item_fees: тип начисления и суммы
        st.subheader("Суммы по item_fees (type_id / accrual_id)")
        _fee_totals: dict[int, float] = {}
        for _a in raw_ops:
            _ifs = _a.get("item_fees") or {}
            for _sf in (_ifs.get("fees") or []):
                if not isinstance(_sf, dict): continue
                for _f in (_sf.get("fees") or []):
                    if not isinstance(_f, dict): continue
                    _tid = _get_type_id(_f)
                    _amt = float(((_f.get("accrued") or {}).get("amount") or 0))
                    if _amt: _fee_totals[_tid] = _fee_totals.get(_tid, 0.0) + _amt
        if _fee_totals:
            st.dataframe(pd.DataFrame([
                {"type_id": k, "Название": TYPE_NAMES.get(k, "⚠️ Неизвестно"), "Сумма": v}
                for k, v in sorted(_fee_totals.items(), key=lambda x: x[1])
            ]), hide_index=True, use_container_width=True)
        else:
            st.info("item_fees пусты для всех начислений в этом периоде")

        # 4. Сырой пример POSTING-начисления
        st.subheader("Пример POSTING (products)")
        _sample = next(
            (a for a in raw_ops if a.get("posting") and (a["posting"].get("products") or [])),
            raw_ops[0] if raw_ops else {}
        )
        st.json(_sample, expanded=2)

        # 5. Итоговые суммы в финальном df (после enrich_with_cost)
        st.subheader("Итоги в финальном df (после enrich_with_cost)")
        _fdf = st.session_state.get("df")
        if _fdf is not None and not _fdf.empty:
            _d5c1, _d5c2, _d5c3, _d5c4 = st.columns(4)
            _d5c1.metric("Реклама (promo) — сумма", f"{_fdf['promo'].abs().sum():,.0f} ₽".replace(",", " ") if "promo" in _fdf.columns else "нет колонки")
            _d5c2.metric("Эквайринг (acquiring) — сумма", f"{_fdf['acquiring'].abs().sum():,.0f} ₽".replace(",", " ") if "acquiring" in _fdf.columns else "нет колонки")
            _d5c3.metric("Рассрочка (installment) — сумма", f"{_fdf['installment'].abs().sum():,.0f} ₽".replace(",", " ") if "installment" in _fdf.columns else "нет колонки")
            _d5c4.metric("Логистика (logistics) — сумма", f"{_fdf['logistics'].abs().sum():,.0f} ₽".replace(",", " ") if "logistics" in _fdf.columns else "нет колонки")
            st.caption("Если Реклама и Эквайринг = 0 — item_fees не дошли до таблицы (нужно проверить сопоставление SKU). Если > 0 — данные есть и отображаются в колонках.")
        else:
            st.warning("df ещё не загружен — сначала нажмите «Загрузить данные»")

    # Обратный справочник: offer_id → список numeric SKU
    reverse_map: dict[str, list[str]] = {}
    for num_sku, offer_id in sku_map_cache.items():
        reverse_map.setdefault(offer_id, []).append(str(num_sku))

    articles = sorted(df["article"].unique().tolist())
    sel_article = st.selectbox("Артикул", articles)
    sel_skus = set(reverse_map.get(sel_article, [sel_article]))

    detail_rows = []
    typeid_totals: dict[int, float] = {}

    for accrual in raw_ops:
        date_str = accrual.get("_date", "")
        unit = accrual.get("unit_number", "")
        category = accrual.get("accrued_category", "")

        # Posting — продажи
        posting = accrual.get("posting") or {}
        for prod in (posting.get("products") or []):
            if not isinstance(prod, dict):
                continue
            if str(prod.get("sku") or "") not in sel_skus:
                continue

            comm = prod.get("commission") or {}
            deliv = prod.get("delivery") or {}
            services = deliv.get("services") or []

            revenue_v = float(((comm.get("sale_amount") or {}).get("amount") or 0))
            commission_v = float(((comm.get("sale_commission") or {}).get("amount") or 0))
            total_deliv_v = float(((deliv.get("total_accrued") or {}).get("amount") or 0))

            for svc in services:
                if not isinstance(svc, dict):
                    continue
                tid = _get_type_id(svc)
                amt = float(((svc.get("accrued") or {}).get("amount") or 0))
                src = "delivery.services"
                typeid_totals[(tid, src)] = typeid_totals.get((tid, src), 0) + amt
                detail_rows.append({
                    "Дата": date_str,
                    "Заказ": unit,
                    "Схема": posting.get("delivery_schema", ""),
                    "fee_type_id": tid,
                    "Название": TYPE_NAMES.get(tid, f"delivery type_id={tid}"),
                    "Сумма": amt,
                    "Выручка": revenue_v,
                    "Комиссия": commission_v,
                    "Доставка (итого)": total_deliv_v,
                })
                revenue_v = 0  # не дублируем выручку на каждый сервис одного заказа
                commission_v = 0
                total_deliv_v = 0

            if not services:
                detail_rows.append({
                    "Дата": date_str,
                    "Заказ": unit,
                    "Схема": posting.get("delivery_schema", ""),
                    "fee_type_id": None,
                    "Название": "—",
                    "Сумма": 0,
                    "Выручка": revenue_v,
                    "Комиссия": commission_v,
                    "Доставка (итого)": total_deliv_v,
                })

        # Item fees — прочие начисления по SKU (эквайринг, реклама и т.п.)
        item_fees_block = accrual.get("item_fees") or {}
        for sku_fees in (item_fees_block.get("fees") or []):
            if not isinstance(sku_fees, dict):
                continue
            if str(sku_fees.get("sku") or "") not in sel_skus:
                continue
            for fee in (sku_fees.get("fees") or []):
                if not isinstance(fee, dict):
                    continue
                tid = _get_type_id(fee)
                amt = float(((fee.get("accrued") or {}).get("amount") or 0))
                src = "item_fees"
                typeid_totals[(tid, src)] = typeid_totals.get((tid, src), 0) + amt
                detail_rows.append({
                    "Дата": date_str,
                    "Заказ": unit,
                    "Схема": TYPE_NAMES.get(tid, f"ITEM type_id={tid}"),
                    "fee_type_id": tid,
                    "Сумма": amt,
                    "Выручка": 0,
                    "Комиссия": 0,
                    "Доставка (итого)": 0,
                })

    if detail_rows:
        det_df = pd.DataFrame(detail_rows).fillna(0)
        num_cols = [c for c in det_df.columns if c not in ("Дата", "Заказ", "Схема", "fee_type_id")]
        for c in num_cols:
            det_df[c] = pd.to_numeric(det_df[c], errors="coerce").fillna(0)

        # Убираем нулевые числовые строки из вывода
        mask = det_df[num_cols].abs().sum(axis=1) > 0
        det_shown = det_df[mask]
        st.markdown(f"**Строк с данными: {mask.sum()}**")

        # Подсчёт компонентов логистики для объяснения
        if "Доставка (итого)" in det_shown.columns and "Заказ" in det_shown.columns:
            total_deliv_sum = det_shown["Доставка (итого)"].sum()
            # Считаем уникальные заказы, у которых была ненулевая логистика
            orders_with_logi = det_shown[det_shown["Доставка (итого)"].abs() > 0]["Заказ"].nunique()
            if orders_with_logi > 0 and total_deliv_sum != 0:
                avg_logi = abs(total_deliv_sum) / orders_with_logi
                st.caption(
                    f"Логистика всего за период: **{ru_rub(abs(total_deliv_sum), 2)}** · "
                    f"Отправлений с логистикой: **{orders_with_logi} шт** · "
                    f"Ср. на отправление: **{ru_rub(avg_logi, 1)}** "
                    f"= Доставка (итого) / кол-во отправлений"
                )

        st.dataframe(det_shown.style.format(
            {c: lambda v: ru_float(v, 2) for c in num_cols}
        ), use_container_width=True, height=400)

        # Сводка начислений по артикулу
        st.subheader("Начисления по выбранному артикулу")
        if typeid_totals:
            known_item_ids = {ACQUIRING_TYPE_ID, INSTALLMENT_TYPE_ID} | PROMO_TYPE_IDS
            tid_rows = []
            for (tid, src), amt in sorted(typeid_totals.items(), key=lambda x: (x[0][0] or 0)):
                on_dash = tid in known_item_ids or src == "delivery.services"
                tid_rows.append({
                    "Код (type_id)": tid,
                    "Название": TYPE_NAMES.get(tid, "⚠️ Неизвестно"),
                    "Источник": src,
                    "Сумма": amt,
                    "Учтён": "✅" if on_dash else "❌",
                })
            tid_df = pd.DataFrame(tid_rows)
            st.dataframe(
                tid_df.style.format({"Сумма": lambda v: ru_float(v, 2)}),
                use_container_width=True,
                hide_index=True,
            )
    else:
        st.warning(f"Нет данных для артикула {sel_article} в загруженном периоде.")

    # ── Все начисления кабинета за период ─────────────────────────────────────
    st.divider()
    st.subheader("Все начисления кабинета за период")

    cab_totals: dict[tuple, float] = {}
    for accrual in raw_ops:
        # delivery.services (по всем заказам)
        posting = accrual.get("posting") or {}
        for prod in (posting.get("products") or []):
            if not isinstance(prod, dict):
                continue
            for svc in ((prod.get("delivery") or {}).get("services") or []):
                if not isinstance(svc, dict):
                    continue
                tid = _get_type_id(svc)
                amt = float(((svc.get("accrued") or {}).get("amount") or 0))
                cab_totals[(tid, "delivery.services")] = cab_totals.get((tid, "delivery.services"), 0) + amt

        # item_fees (по всем SKU)
        for sku_fees in ((accrual.get("item_fees") or {}).get("fees") or []):
            if not isinstance(sku_fees, dict):
                continue
            for fee in (sku_fees.get("fees") or []):
                if not isinstance(fee, dict):
                    continue
                tid = _get_type_id(fee)
                amt = float(((fee.get("accrued") or {}).get("amount") or 0))
                cab_totals[(tid, "item_fees")] = cab_totals.get((tid, "item_fees"), 0) + amt

        # non_item_fee — общие сборы на уровне заказа (хранение и т.п.)
        nif = accrual.get("non_item_fee")
        if isinstance(nif, dict) and _get_type_id(nif) is not None:
            tid = _get_type_id(nif)
            amt = float(((nif.get("accrued") or {}).get("amount") or 0))
            cab_totals[(tid, "non_item_fee")] = cab_totals.get((tid, "non_item_fee"), 0) + amt

    if cab_totals:
        known_ids = {ACQUIRING_TYPE_ID, INSTALLMENT_TYPE_ID, 29, 32} | PROMO_TYPE_IDS | set(STORE_COST_GROUPS.keys())
        cab_rows = []
        for (tid, src), amt in sorted(cab_totals.items(), key=lambda x: x[1]):
            if amt == 0:
                continue
            name = TYPE_NAMES.get(tid, "⚠️ Неизвестно")
            on_dashboard = tid in known_ids or src == "delivery.services"
            cab_rows.append({
                "Код (type_id)": tid,
                "Название": name,
                "Источник": src,
                "Сумма": amt,
                "Учтён": "✅" if on_dashboard else "❌",
            })
        cab_df = pd.DataFrame(cab_rows)

        # Сначала показываем нераспознанные — они самые важные
        unrecognized = cab_df[cab_df["Учтён"] == "❌"].copy()
        recognized   = cab_df[cab_df["Учтён"] == "✅"].copy()

        unrecognized_expenses = unrecognized[unrecognized["Сумма"] < 0]
        unrecognized_income   = unrecognized[unrecognized["Сумма"] > 0]
        total_unrecognized_exp = unrecognized_expenses["Сумма"].sum()
        total_unrecognized_inc = unrecognized_income["Сумма"].sum()

        if not unrecognized.empty:
            st.error(
                f"⚠️ Нераспознанные начисления: расходы **{total_unrecognized_exp:,.0f} ₽**, "
                f"доходы **+{total_unrecognized_inc:,.0f} ₽** — не вошли в P&L."
            )
            with st.expander("❌ Нераспознанные начисления (не в P&L) — развернуть", expanded=True):
                st.dataframe(
                    unrecognized.style.format({"Сумма": lambda v: ru_float(v, 2)}),
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    "Эти начисления есть в API, но дашборд их не классифицировал. "
                    "Сообщи, в какую категорию отнести каждый код — добавим в формулу."
                )
        else:
            st.success("✅ Все начисления распознаны и учтены в P&L.")

        with st.expander("✅ Учтённые начисления (полный список)", expanded=False):
            st.dataframe(
                recognized.style.format({"Сумма": lambda v: ru_float(v, 2)}),
                use_container_width=True, hide_index=True,
                height=400,
            )