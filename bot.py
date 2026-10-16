"""
Bankrot Bot — лоты банкротских торгов с lot-online.ru (ЭТП РАД).

Команды:
  /auto    — легковые авто: год выпуска от AUTO_MIN_YEAR, цена до MAX_PRICE
  /realty  — недвижимость в Москве и Московской области, цена до MAX_PRICE
  /scan    — общий поиск по всем категориям (как в исходной версии)
  /analyze — подробности по лоту из последнего поиска
  /debug   — проверка, как бот читает страницы lot-online.ru

Версия для Render.com (webhook mode). Фильтры меняются переменными окружения.
"""
import os, re, sys, time, html, json, base64, logging, requests, threading
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from flask import Flask, request as flask_request
from lxml import etree, html as LH
from market_price import search_market_prices, calculate_margin, search_drom
from llm_analyzer import llm_analyze_lot, llm_compare_lots, is_available as llm_available


def _env_int(name, default):
    try:
        return int(str(os.getenv(name, default)).strip())
    except ValueError:
        return default


def _env_ids(name, default):
    return [int(x) for x in re.findall(r"\d+", os.getenv(name, default) or "")]


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_ID = os.getenv("CHAT_ID", "")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Moscow")
DAILY_HOUR = _env_int("DAILY_HOUR", 9)
DAILY_MINUTE = _env_int("DAILY_MINUTE", 0)
PORT = _env_int("PORT", 10000)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ─── Фильтры (меняются переменными окружения в Render) ────
AUTO_MIN_YEAR = _env_int("AUTO_MIN_YEAR", 2019)
MIN_PRICE = _env_int("MIN_PRICE", 0)
MAX_PRICE = _env_int("MAX_PRICE", 10_000_000)
AUTO_CATS = _env_ids("AUTO_CATS", "44")                 # легковые автомобили
REALTY_CATS = _env_ids("REALTY_CATS", "34,38,23,26")    # квартиры, дома, нежилые, гаражи
MAX_PAGES = _env_int("MAX_PAGES", 10)                   # страниц каталога на категорию
ITEMS_PER_PAGE = _env_int("ITEMS_PER_PAGE", 96)
MAX_DETAIL_FETCH = _env_int("MAX_DETAIL_FETCH", 150)    # сколько карточек лотов открывать за поиск
TOP_N = _env_int("TOP_N", 15)                           # сколько лотов показывать
DAILY_DIGEST = os.getenv("DAILY_DIGEST", "auto,realty") # что присылать утром
# Какие лоты недвижимости убирать: lease — право аренды, share — доли в праве,
# claim — права требования/дебиторка, storage — кладовые и хозблоки
SORT_DEFAULT = os.getenv("SORT_DEFAULT", "price")        # price — по цене, new — сначала новые
REALTY_EXCLUDE = {x.strip() for x in os.getenv("REALTY_EXCLUDE", "lease,share,claim").split(",") if x.strip()}

# Площадки: rad — lot-online.ru (РАД), mets — m-ets.ru (МЭТС)
# Площадки: rad — lot-online.ru (РАД), mets — m-ets.ru (МЭТС), alfalot — Альфалот,
# cdtrf — Центр дистанционных торгов, nistp — НИСТП
SOURCES = {x.strip().lower() for x in os.getenv("SOURCES", "rad,mets,alfalot,cdtrf,nistp").split(",") if x.strip()}
OTHER_MAX_PAGES = _env_int("OTHER_MAX_PAGES", 8)                 # страниц на Альфалоте, ЦДТ и НИСТП
METS_AUTO_CATS = _env_ids("METS_AUTO_CATS", "1")                 # легковой автомобиль
METS_REALTY_CATS = _env_ids("METS_REALTY_CATS", "36,34,37,39")   # квартира, жилой дом, нежилое помещение, прочие постройки
METS_REGIONS = [str(x) for x in _env_ids("METS_REGIONS", "50,77")]       # для недвижимости: Московская обл., Москва
METS_AUTO_REGIONS = [str(x) for x in _env_ids("METS_AUTO_REGIONS", "")]  # для авто: пусто — вся Россия
METS_MAX_PAGES = _env_int("METS_MAX_PAGES", 10)                  # страниц поиска МЭТС на категорию (по 20 лотов)
SRC_ORDER = ("rad", "mets", "alfalot", "cdtrf", "nistp")
SRC_LABEL = {"rad": "РАД", "mets": "МЭТС", "alfalot": "Альфалот", "cdtrf": "ЦДТ", "nistp": "НИСТП"}

CUR_YEAR = datetime.now().year

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("bankrot")

# ─── Telegram ──────────────────────────────────────────

def api_call(method, **kwargs):
    # Пустые параметры не отправляем: Telegram отклоняет, например, parse_mode = null
    payload = {k: v for k, v in kwargs.items() if v is not None}
    try:
        r = requests.post(f"{API}/{method}", json=payload, timeout=30)
        return r.json()
    except Exception as e:
        log.error(f"API {method}: {e}")
        return {"ok": False}


def _split(text, limit=4000):
    parts = []
    while len(text) > limit:
        cut = text[:limit].rfind("\n\n")
        if cut <= 0:
            cut = text[:limit].rfind("\n")
        if cut <= 0:
            cut = limit
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        parts.append(text)
    return parts


def send_message(cid, text, parse_mode="HTML"):
    for part in _split(text):
        res = api_call("sendMessage", chat_id=cid, text=part, parse_mode=parse_mode,
                       disable_web_page_preview=True)
        if res.get("ok"):
            continue
        log.warning(f"sendMessage to {cid} failed: {res.get('description')}")
        if parse_mode:
            # Если Telegram не принял HTML — отправляем тем же текстом без разметки
            plain = html.unescape(re.sub(r"<[^>]+>", "", part))
            res = api_call("sendMessage", chat_id=cid, text=plain, disable_web_page_preview=True)
            if not res.get("ok"):
                log.error(f"sendMessage plain to {cid} failed: {res.get('description')}")


def answer(cid, text):
    send_message(cid, text)


def esc(s):
    return html.escape(str(s or ""), quote=False)


def _short(title, n=150):
    title = re.sub(r"\s+", " ", title or "?").strip()
    return title if len(title) <= n else title[:n - 1] + "…"


def rub(x):
    return f"{int(x):,}".replace(",", " ") + " ₽"

# ─── Категории РАД ─────────────────────────────────────
# lot-online.ru category_id -> (название, условная маржа% — только для /scan)

RAD_CATEGORIES = {
    # Транспорт
    44: ("🚗 Легковые автомобили", 40),
    45: ("🚛 Грузовики/спецтехника", 55),
    48: ("📦 Прицепы", 45),
    49: ("⛵ Водный транспорт", 50),
    51: ("🚂 ЖД транспорт", 60),
    53: ("🚐 Иной транспорт", 45),
    165: ("🏍 Мототехника", 55),
    61: ("🔧 Автозапчасти", 60),
    # Оборудование
    55: ("🏦 Банковское оборудование", 60),
    167: ("📡 Сетевое оборудование", 70),
    57: ("🏭 Производственное оборудование", 80),
    58: ("⚙️ Иное оборудование", 65),
    60: ("💻 Бытовая/компьютерная техника", 70),
    # Недвижимость
    22: ("🏢 Здания", 50),
    23: ("🏪 Нежилые помещения", 45),
    25: ("🏗 Производственные объекты", 55),
    26: ("🅿️ Паркинги/гаражи", 40),
    34: ("🏠 Квартиры", 35),
    38: ("🏡 Дома/коттеджи", 40),
    # Прочее
    64: ("🔩 Металлолом", 35),
    70: ("📦 Иное имущество", 50),
    62: ("🪙 Монеты", 60),
    63: ("🪑 Мебель", 45),
}

KW_MARGINS = {
    "ноутбук": (1, 80), "ноутбуки": (1, 80), "смартфон": (1, 60),
    "телефон": (1, 50), "сервер": (1, 90), "компьютер": (1, 70),
    "монитор": (1, 60), "принтер": (2, 40), "роутер": (2, 50),
    "станок": (1, 100), "генератор": (1, 80), "компрессор": (2, 70),
    "холодильник": (1, 60), "стиральная": (1, 50), "кондиционер": (1, 70),
    "велосипед": (1, 60), "тренажёр": (1, 60), "инструмент": (1, 70),
    "автомобиль": (1, 40), "грузовик": (1, 50), "экскаватор": (1, 60),
    "погрузчик": (1, 60), "трактор": (1, 50), "кран": (1, 55),
}

# ─── Загрузка страниц lot-online.ru ─────────────────────

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
           "Accept": "text/html,application/xhtml+xml",
           "Accept-Language": "ru-RU,ru;q=0.9"}
CATALOG = "https://catalog.lot-online.ru/index.php"


def _get(url, timeout=20):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r


def _category_url(cat_id, page=1):
    return (f"{CATALOG}?dispatch=categories.view&category_id={cat_id}"
            f"&sort_by=timestamp&sort_order=desc&items_per_page={ITEMS_PER_PAGE}&page={page}")


def parse_price(text):
    """'1 250 000,00 ₽' -> 1250000.0 (копейки отбрасываем)."""
    m = re.search(r"\d[\d\s\xa0 ]*(?:[.,]\d{1,2})?", text or "")
    if not m:
        return 0.0
    num = re.split(r"[.,]", m.group(0))[0]
    num = re.sub(r"\D", "", num)
    return float(num) if num else 0.0


def _doc(page_html):
    """HTML -> дерево lxml (в разы быстрее BeautifulSoup — важно на слабом сервере Render)."""
    page_html = re.sub(r"^\s*<\?xml[^>]*\?>", "", page_html or "") or "<html></html>"
    doc = LH.document_fromstring(page_html)
    etree.strip_elements(doc, etree.Comment, "script", "style", "noscript", "svg", with_tail=False)
    return doc


def _cls(name, rel=False):
    return f"{'.' if rel else ''}//*[contains(concat(' ', normalize-space(@class), ' '), ' {name} ')]"


def _txt(node):
    return re.sub(r"\s+", " ", " ".join(node.itertext())).strip()


def _parse_catalog_page(page_html, cat_id, cat_name, cat_margin):
    """Карточки лотов со страницы каталога lot-online.ru."""
    doc = _doc(page_html)
    lots = []
    cards = doc.xpath(_cls("ty-grid-list__item")) or doc.xpath(_cls("ty-compact-list__item")) \
        or doc.xpath(_cls("ty-product-list"))

    for card in cards:
        title, url = "", ""
        for a in card.xpath(".//a[@href]"):
            href = a.get("href", "")
            if "product_id" in href or "products.view" in href:
                text = _txt(a)
                if text and len(text) > len(title):
                    title = text
                    pid_m = re.search(r"product_id=(\d+)", href)
                    if pid_m:
                        url = f"{CATALOG}?dispatch=products.view&product_id={pid_m.group(1)}"
        if not title or len(title) < 5 or not url:
            continue

        price = 0.0
        price_el = card.xpath(_cls("ty-price", rel=True))
        if price_el:
            price = parse_price(_txt(price_el[0]))
        if price <= 0:
            for chunk in card.itertext():
                if re.search(r"\d[\d\s\xa0]{3,}", chunk):
                    val = parse_price(chunk)
                    if val > 999:
                        price = val
                        break
        if price <= 0:
            continue

        card_text = _txt(card)
        date_end = ""
        days_match = re.search(r"Торги через (\d+) дн", card_text)
        if days_match:
            date_end = f"через {days_match.group(1)} дн."

        lots.append({
            "title": title,
            "card_text": card_text,
            "price": price,
            "url": url,
            "cat_id": cat_id,
            "cat_name": cat_name,
            "cat_margin": cat_margin,
            "date_end": date_end,
            "code": int(code_m.group(1)) if (code_m := re.search(r"РАД-(\d+)", card_text)) else 0,
            "source": "lot-online.ru (РАД)",
            "src": "rad",
        })
    return lots


def _noop(*a, **k):
    pass


class Progress:
    """Одно сообщение в Telegram, которое обновляется по ходу поиска."""

    def __init__(self, cid, head):
        self.cid, self.head, self.last = cid, head, 0.0
        res = api_call("sendMessage", chat_id=cid, text=head)
        self.mid = (res.get("result") or {}).get("message_id")

    def __call__(self, line, force=False):
        now = time.time()
        if not self.mid or (not force and now - self.last < 4):
            return
        self.last = now
        api_call("editMessageText", chat_id=self.cid, message_id=self.mid, text=f"{self.head}\n\n{line}")


def fetch_category(cat_id, max_pages=None, progress=_noop):
    """Все лоты категории: листаем страницы, пока появляются новые."""
    site_name = _rad_site_cats.get(cat_id)
    cat_name, cat_margin = RAD_CATEGORIES.get(cat_id, (site_name or f"Категория {cat_id}", 40))
    lots, seen = [], set()
    for page in range(1, (max_pages or MAX_PAGES) + 1):
        try:
            r = _get(_category_url(cat_id, page))
        except requests.HTTPError as e:
            if page > 1 and e.response is not None and e.response.status_code == 404:
                log.info(f"  {cat_name}: страницы закончились на {page - 1}")
            else:
                log.error(f"  {cat_name} стр.{page}: {e}")
            break
        except Exception as e:
            log.error(f"  {cat_name} стр.{page}: {e}")
            break
        page_lots = _parse_catalog_page(r.text, cat_id, cat_name, cat_margin)
        new = [l for l in page_lots if l["url"] not in seen]
        seen.update(l["url"] for l in new)
        lots.extend(new)
        log.info(f"  {cat_name} стр.{page}: на странице {len(page_lots)}, новых {len(new)}")
        progress(f"{cat_name}: страница {page}, лотов {len(lots)}")
        if not new:
            break
    return lots


_rad_site_cats = {}


def rad_site_categories():
    """Список разделов каталога РАД (id → название) из меню сайта. Загружается один раз."""
    if not _rad_site_cats:
        try:
            doc = _doc(_get(_category_url(34, 1)).text)
            for a in doc.xpath("//a[contains(@href, 'category_id=')]"):
                m = re.search(r"category_id=(\d+)", a.get("href") or "")
                name = _txt(a)
                if m and name and len(name) < 80:
                    _rad_site_cats.setdefault(int(m.group(1)), name)
        except Exception as e:
            log.error(f"Меню разделов РАД: {e}")
    return _rad_site_cats


def rad_cats_by_name(pattern):
    return [cid for cid, name in rad_site_categories().items() if re.search(pattern, name, re.I)]


def gather(cat_ids, progress=_noop):
    lots, seen = [], set()
    for cid in cat_ids:
        for l in fetch_category(cid, progress=progress):
            if l["url"] not in seen:
                seen.add(l["url"])
                lots.append(l)
    return lots


def fetch_rad_lots():
    """Первая страница каждой категории — для общего /scan."""
    all_lots = []
    for cat_id in RAD_CATEGORIES:
        all_lots.extend(fetch_category(cat_id, max_pages=1))
    log.info(f"Всего с RAD: {len(all_lots)} лотов")
    return all_lots


def fetch_rad_by_keyword(keyword):
    """Поиск по слову на lot-online.ru."""
    url = (f"{CATALOG}?dispatch=products.search&search_performed=Y&match=all"
           f"&q={requests.utils.quote(keyword)}&items_per_page={ITEMS_PER_PAGE}")
    try:
        r = _get(url)
        return _parse_catalog_page(r.text, 0, f"Поиск: {keyword}", 50)
    except Exception as e:
        log.error(f"RAD search '{keyword}': {e}")
        return []


def fetch_detail_text(url):
    """Текст карточки лота без шапки и подвала сайта. Возвращает (текст, откуда взят)."""
    doc = _doc(_get(url).text)
    nodes = (doc.xpath(_cls("ty-product-block")) + doc.xpath("//*[@id='content_description']")
             + doc.xpath("//*[@id='content_features']") + doc.xpath(_cls("ty-product-block__description"))
             + doc.xpath(_cls("ty-tabs__content")))
    texts = []
    for node in nodes:
        t = _txt(node)
        if t and t not in texts:
            texts.append(t)
    text, source = " ".join(texts), "блок лота"
    if len(text) < 200:
        cont = doc.xpath(_cls("tygh-content")) or doc.xpath("//body") or [doc]
        node = cont[0]
        for bad in node.xpath(f"{_cls('tygh-header', True)}|{_cls('tygh-footer', True)}|.//header|.//footer|.//nav"):
            bad.drop_tree()
        text, source = _txt(node), "вся страница"
    return text, source

# ─── МЭТС (m-ets.ru) ──────────────────────────────────

METS = "https://m-ets.ru"
METS_CATEGORIES = {1: "Легковой автомобиль", 34: "Жилой дом", 36: "Квартира", 37: "Нежилое помещение",
                   38: "Нежилое здание", 39: "Прочие постройки", 11: "Земельный участок"}
_METS_CLOSED = re.compile(r"заверш|отмен|приостанов|подведени|не состоял", re.I)


def mets_search_url(filters, page=1):
    """Фильтры поиска МЭТС передаются в ссылке как JSON, закодированный в base64."""
    raw = json.dumps(filters, ensure_ascii=False, separators=(",", ":"))
    q = base64.b64encode(raw.encode()).decode().rstrip("=")
    return f"{METS}/search?q={quote(q, safe='')}" + (f"&page={page}" if page > 1 else "")


def mets_filters(kind, cat, q):
    q = q or default_query(kind)
    f = {}
    if kind == "realty":
        regions = {"city": ["77"], "oblast": ["50"]}.get(q.get("region"), METS_REGIONS)
    else:
        regions = METS_AUTO_REGIONS
    if regions:
        f["xregion[]"] = regions
    f["search_category"] = str(cat)
    if kind == "auto" and q.get("words"):
        f["lots"] = " ".join(q["words"])     # марка или модель — ищем прямо на сайте
        f["lotst"] = "1"
    if q.get("min_price"):
        f["cena_tek_ot"] = str(q["min_price"])
    if q.get("max_price"):
        f["cena_tek_do"] = str(q["max_price"])
    if kind == "auto":
        f["autoyear_ot"] = str(q["min_year"])
    if kind == "realty" and "share" in REALTY_EXCLUDE:
        f["not_dolevoe"] = "on"
    f["stat[]"] = ["0", "1", "7"]          # объявлены, приём заявок, подача ценовых предложений
    f["sortby"] = "1" if q["sort"] == "new" else "4"   # 1 — по дате публикации, 4 — сначала дешёвые
    return f


def _parse_mets_page(page_html, cat, kind):
    """Карточки лотов со страницы поиска МЭТС (закрытые торги помечаются closed)."""
    doc = _doc(page_html)
    lots = []
    for it in doc.xpath("//a[contains(concat(' ', normalize-space(@class), ' '), ' search-comp-item ')]"):
        m = re.search(r"(\d+)-(\d+)/?$", (it.get("href") or "").split("?")[0])
        if not m:
            continue

        def first(name):
            r = it.xpath(_cls(name, rel=True))
            return _txt(r[0]) if r else ""

        title = first("comp-title") or first("title")
        if len(title) < 3:
            continue
        price = 0.0
        pr = it.xpath(".//*[@itemprop='price']/@content")
        if pr:
            try:
                price = float(pr[0])
            except ValueError:
                pass
        if price <= 0:
            price = parse_price(first("cost"))
        if price <= 0:
            continue
        status = first("comp-type")
        left = re.sub(r"^\s*Осталось:?\s*", "", first("time-info"))
        desc = first("description")
        location = first("search-item-location")
        lots.append({
            "title": title,
            "card_text": f"{title} . {desc} . {location}",
            "price": price,
            "url": f"{METS}/{m.group(1)}-{m.group(2)}",
            "cat_id": f"mets:{cat}",
            "cat_name": METS_CATEGORIES.get(cat, f"Категория {cat}"),
            "cat_margin": 40,
            "date_end": f"через {left}" if left else "",
            "code": int(m.group(1)),
            "source": "m-ets.ru (МЭТС)",
            "src": "mets",
            "location": location,
            "min_price": parse_price(first("min")),
            "status": status,
            "closed": bool(_METS_CLOSED.search(status)),
            "kind": kind,
        })
    return lots


def fetch_mets(kind, cats, q, progress=_noop, errors=None):
    """Лоты МЭТС по категориям: фильтры региона, цены и года задаются прямо в поиске сайта."""
    lots, seen = [], set()
    for cat in cats:
        name = METS_CATEGORIES.get(cat, f"Категория {cat}")
        for page in range(1, METS_MAX_PAGES + 1):
            try:
                r = _get(mets_search_url(mets_filters(kind, cat, q), page))
            except Exception as e:
                log.error(f"  МЭТС {name} стр.{page}: {e}")
                if errors is not None and page == 1:
                    errors.append(_error_reason(e))
                break
            page_lots = _parse_mets_page(r.text, cat, kind)
            new = [l for l in page_lots if l["url"] not in seen]
            seen.update(l["url"] for l in new)
            lots.extend(l for l in new if not l["closed"])
            log.info(f"  МЭТС {name} стр.{page}: на странице {len(page_lots)}, новых {len(new)}")
            progress(f"МЭТС · {name}: страница {page}, лотов {len(lots)}")
            if not new:
                break
    return lots


def _error_reason(e):
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"сайт ответил ошибкой {e.response.status_code}"
    if isinstance(e, requests.exceptions.ProxyError):
        return "ошибка прокси"
    if isinstance(e, requests.Timeout):
        return "сайт не ответил вовремя"
    return type(e).__name__


def gather_all(kind, rad_cats, mets_cats, q, progress=_noop):
    """Лоты со всех включённых площадок + сколько просмотрено на каждой и ошибки загрузки."""
    fetchers = {
        "rad": lambda errs: gather(rad_cats, progress),
        "mets": lambda errs: fetch_mets(kind, mets_cats, q, progress, errs),
        "alfalot": lambda errs: fetch_alfalot(kind, q, progress, errs),
        "cdtrf": lambda errs: fetch_cdtrf(kind, q, progress, errs),
        "nistp": lambda errs: fetch_nistp(kind, q, progress, errs),
    }
    all_lots, by_src, errors = [], {}, []
    for src in SRC_ORDER:
        if src not in SOURCES:
            continue
        errs = []
        try:
            lots = fetchers[src](errs)
        except Exception as e:
            log.error(f"{SRC_LABEL[src]}: {e}", exc_info=True)
            errs.append(_error_reason(e))
            lots = []
        for l in lots:
            l["kind"] = kind
            l.setdefault("src", src)
        all_lots += lots
        by_src[SRC_LABEL[src]] = len(lots)
        for reason in dict.fromkeys(errs):
            extra = f" (категорий: {errs.count(reason)} из {len(mets_cats)})" if src == "mets" and len(mets_cats) > 1 else ""
            errors.append(f"{SRC_LABEL[src]} — {reason}{extra}")
    return all_lots, by_src, errors


def _src_label(lot):
    return SRC_LABEL.get(lot.get("src", "rad"), "")

# ─── Альфалот, ЦДТ, НИСТП ─────────────────────────────

DEFAULT_REALTY_TYPES = ["flat", "house", "office", "garage"]
EXTRA_CATS = {   # категории ЦДТ и Альфалота для типов недвижимости
    "flat":     {"cdtrf": [21], "alfalot": [1]},
    "house":    {"cdtrf": [22], "alfalot": [1]},
    "office":   {"cdtrf": [31], "alfalot": [1]},
    "building": {"cdtrf": [32, 33], "alfalot": [1]},
    "garage":   {"cdtrf": [23], "alfalot": [1]},
    "land":     {"cdtrf": [41], "alfalot": [7]},
}
JSON_HEADERS = {**HEADERS, "Accept": "application/json"}
_OPEN_STATUS_BAD = re.compile(r"заверш|окончен|отмен|приостан|подведени|не состоял|определение участ|допущен", re.I)
_PASSENGER_BODY = re.compile(r"седан|хэтчбек|хетчбек|универсал|купе|кабриолет|внедорож|кроссовер|лифтбек|минивэн|комби|пикап", re.I)
_NOT_CAR = re.compile(r"грузов|самосвал|тягач|прицеп|автобус|фургон|спецтех|экскават|трактор|погрузч|автокран|кран\b|"
                      r"мотоцикл|квадроцикл|снегоход|катер|лодк|бульдозер|каток", re.I)


def _get_json(url, params=None, timeout=25):
    r = requests.get(url, params=params, headers=JSON_HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _parse_dt(s):
    """'2026-11-23T12:00:00+03:00', '2026-10-19T10:00:00', '19.10.2026 10:00', '11/18/2026 11:00:00' → datetime."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        pass
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y", "%m/%d/%Y %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def _dt_key(dt):
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute) if dt else None


def _days_left(dt):
    if not dt:
        return ""
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    days = (dt.date() - today).days
    if days < 0:
        return ""
    return "сегодня" if days == 0 else f"через {days} дн."


def realty_types_of(head):
    """Тип недвижимости по началу описания лота (для площадок без своих категорий)."""
    t = (head or "").lower().replace("ё", "е")
    types = set()
    if re.search(r"земельн\w*\s+участ|\bзем\.\s*участ|\bучаст(?:ок|ки)\b|\bземли\b", t):
        types.add("land")
    if re.search(r"гараж|машин\w*[- ]?мест|\bм/м\b|парковочн\w*\s+мест|\bпаркинг|\bгск\b|\bгк\s*«", t):
        types.add("garage")
    if re.search(r"нежил\w*\s+здани|\bздани[ея]\b|\bсооружени", t):
        types.add("building")
    if re.search(r"нежил\w*\s+помещ|\bофис|торгов\w*\s+помещ", t):
        types.add("office")
    t2 = re.sub(r"нежил\w*", " ", t)
    if re.search(r"квартир|жил\w*\s+помещ|\bкомнат", t2):
        types.add("flat")
    if re.search(r"жил\w*\s+дом|^\W*дом\b|\bдом\s+(?:\d|площ|с\s+земел)|коттедж|домовлад|садов\w*\s+дом|"
                 r"дачн\w*\s+дом|таунхаус", t2):
        types.add("house")
    return types


def is_passenger_car(text, body=""):
    t = (text or "").lower()
    if re.search(r"легков", t) or _PASSENGER_BODY.search(body or ""):
        return True
    if _NOT_CAR.search(t):
        return False
    return bool(re.search(r"автомобил|\bавто\b", t))


def _region_from(region_text, extra_text=""):
    """(регион, подпись) по названию региона площадки; если его нет — по тексту лота."""
    loc = (region_text or "").lower()
    if not re.search(r"[а-я]", loc):
        reg, label, _ = detect_region(extra_text)
        return reg, label
    if "москв" in loc or "московск" in loc:
        reg, label, _ = detect_region(extra_text)
        if "обл" in loc:
            return "msk", (label if (reg == "msk" and label and label.startswith("Моск. обл.")) else "Московская обл.")
        return "msk", (label if label == "Москва (Зеленоград)" else "Москва")
    return "other", None


def _realty_allowed(q):
    return set(q.get("types") or DEFAULT_REALTY_TYPES)


# Альфалот: витрина лотов отдаёт JSON
ALFALOT_API = "https://ecosystem.alfalot.ru/api/showcase/lots/recommended/paginated"


def fetch_alfalot(kind, q, progress=_noop, errors=None, max_pages=None):
    if kind == "auto":
        cats = [4]
    else:
        cats = sorted({c for t in _realty_allowed(q) for c in EXTRA_CATS[t]["alfalot"]})
    params = {"per_page": 36, "categories": ",".join(map(str, cats)),
              "status": "Прием заявок,Извещение опубликовано"}
    if kind == "realty":
        params["regions"] = {"city": "77", "oblast": "50"}.get(q.get("region"), "50,77")
    elif METS_AUTO_REGIONS:
        params["regions"] = ",".join(METS_AUTO_REGIONS)
    if q.get("max_price"):
        params["max_price"] = q["max_price"]
    if q.get("min_price"):
        params["min_price"] = q["min_price"]
    if kind == "auto" and q.get("words"):
        params["search"] = " ".join(q["words"])
    if q["sort"] == "price" and kind == "realty":
        params.update(sort="price", order="asc")
    else:
        params.update(sort="created_at", order="desc")   # сначала новые
    allowed = _realty_allowed(q)
    lots, seen = [], set()
    for page in range(1, (max_pages or OTHER_MAX_PAGES) + 1):
        try:
            data = _get_json(ALFALOT_API, {**params, "page": page})
        except Exception as e:
            log.error(f"  Альфалот стр.{page}: {e}")
            if errors is not None and page == 1:
                errors.append(_error_reason(e))
            break
        items = data.get("lots") or []
        new = 0
        for it in items:
            url = it.get("url") or ""
            if not url or url in seen:
                continue
            seen.add(url)
            new += 1
            status = it.get("status") or ""
            if _OPEN_STATUS_BAD.search(status):
                continue
            title = re.sub(r"\s+", " ", it.get("title") or "").strip()
            ext = it.get("extension") or []
            addr = " ".join(str((e.get("fields") or {}).get("field6") or "") for e in ext if e.get("type") == "rosreestr")
            body = " ".join(str((e.get("fields") or {}).get("field5") or "") for e in ext if e.get("type") == "gibdd")
            try:
                price = float(it.get("current_price") or it.get("price") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0 or not title:
                continue
            cat = it.get("category") or {}
            cat_name = cat.get("title") or ""
            if kind == "realty":
                types = realty_types_of(title)
                if not types & allowed:
                    continue
                cat_name = ", ".join(REALTY_TYPES[t][0] for t in REALTY_TYPES if t in types)
            elif not is_passenger_car(title, body):
                continue
            start, end = _parse_dt(it.get("bid_start_date")), _parse_dt(it.get("bid_end_date"))
            location = it.get("location") or ""
            l = {"title": title, "card_text": f"{title} . {addr} . {location}", "price": price, "url": url,
                 "cat_id": f"alfalot:{cat.get('id')}", "cat_name": cat_name, "cat_margin": 40,
                 "date_end": _days_left(end), "code": int(it.get("auction_number") or 0), "src": "alfalot",
                 "source": "alfalot.ru", "location": location if re.search(r"[А-Яа-я]", location) else "",
                 "status": status, "fresh_key": _dt_key(start),
                 "apps_from": start.strftime("%d.%m.%Y") if start else ""}
            if kind == "realty":
                l["region"], l["region_label"] = _region_from(location, f"{title} . {addr}")
                l["region_src"] = "площадка"
            lots.append(l)
        log.info(f"  Альфалот стр.{page}: на странице {len(items)}, новых {new}")
        progress(f"Альфалот: страница {page}, лотов {len(lots)}")
        last = (data.get("pagination") or {}).get("last_page") or page
        if not new or page >= last:
            break
    return lots


# ЦДТ (Центр дистанционных торгов): список торгов отдаётся в JSON
CDTRF = "https://torgi.cdtrf.ru"
CDTRF_CATS = {1: "Легковой транспорт", 21: "Квартира", 22: "Жилой дом", 23: "Прочие постройки",
              31: "Нежилое помещение", 32: "Нежилые здания", 33: "Прочие здания", 41: "Земельный участок"}


def fetch_cdtrf(kind, q, progress=_noop, errors=None, max_pages=None):
    if kind == "auto":
        cats = [1]
        regions = METS_AUTO_REGIONS
    else:
        cats = sorted({c for t in _realty_allowed(q) for c in EXTRA_CATS[t]["cdtrf"]})
        regions = {"city": ["77"], "oblast": ["50"]}.get(q.get("region"), ["50", "77"])
    params = [("Declare", "true"), ("RecieveReq", "true"), ("PageSize", "20"),
              ("Categories", ",".join(map(str, cats)))]
    if regions:
        params.append(("RegionIds", ",".join(regions)))
    if q.get("max_price"):
        params.append(("CurrentPrice2", str(q["max_price"])))
    if q.get("min_price"):
        params.append(("CurrentPrice1", str(q["min_price"])))
    if q["sort"] == "price" and kind == "realty":
        params.append(("Sort", "ActualPrice"))   # иначе — сначала новые
    lots, seen = [], set()
    for page in range(1, (max_pages or OTHER_MAX_PAGES) + 1):
        try:
            data = _get_json(f"{CDTRF}/api/Trade/trades", params + [("PageNum", str(page))])
        except Exception as e:
            log.error(f"  ЦДТ стр.{page}: {e}")
            if errors is not None and page == 1:
                errors.append(_error_reason(e))
            break
        items = data.get("items") or []
        new = 0
        for it in items:
            key = it.get("tradeLotId")
            if not key or key in seen:
                continue
            seen.add(key)
            new += 1
            status = it.get("tradeStatusDescription") or ""
            if _OPEN_STATUS_BAD.search(status):
                continue
            title = re.sub(r"\s+", " ", it.get("name") or "").strip()
            try:
                price = float(it.get("actualPrice") or 0)
            except (TypeError, ValueError):
                price = 0.0
            if price <= 0 or not title:
                continue
            lot_cats = [int(x) for x in re.findall(r"\d+", it.get("categories") or "")]
            if lot_cats and not set(lot_cats) & set(cats):
                continue
            cat_name = next((CDTRF_CATS[c] for c in lot_cats if c in cats and c in CDTRF_CATS), "")
            start, end = _parse_dt(it.get("requestTimeBegin")), _parse_dt(it.get("requestTimeEnd"))
            rid = it.get("regionId")
            l = {"title": title, "card_text": f"{title} . {cat_name}", "price": price,
                 "url": f"{CDTRF}/trades/{it.get('tradeId')}", "cat_id": f"cdtrf:{','.join(map(str, lot_cats))}",
                 "cat_name": cat_name, "cat_margin": 40, "date_end": _days_left(end), "code": int(key),
                 "src": "cdtrf", "source": "torgi.cdtrf.ru", "status": status,
                 "status_note": "действует минимальная цена" if it.get("isMinPrice") else "",
                 "fresh_key": _dt_key(start), "apps_from": start.strftime("%d.%m.%Y") if start else "",
                 "location": {77: "г. Москва", 50: "Московская область"}.get(rid, "")}
            if kind == "realty":
                region_text = {77: "г. Москва", 50: "Московская область"}.get(rid, "другой регион")
                l["region"], l["region_label"] = _region_from(region_text, title)
                l["region_src"] = "площадка"
            lots.append(l)
        log.info(f"  ЦДТ стр.{page}: на странице {len(items)}, новых {new}")
        progress(f"ЦДТ: страница {page}, лотов {len(lots)}")
        if not new or len(items) < 20:
            break
    return lots


# НИСТП: список лотов (HTML), поиск по тексту описания; регион и цена — только в платной версии
NISTP = "https://nistp.ru"


def _parse_nistp_page(page_html, kind):
    doc = _doc(page_html)
    lots = []
    for tr in doc.xpath("//tr[td and .//a[contains(@href, 'trade_view')]]"):
        tds = tr.xpath("./td")
        if len(tds) < 7:
            continue
        hrefs = [a.get("href") for a in tr.xpath(".//a[contains(@href, 'trade_view')]")]
        url = next((h for h in hrefs if "#lot" in h), hrefs[-1])
        if url.startswith("/"):
            url = NISTP + url
        # В ячейке: «Лот № N: краткое название <br> полное описание»
        lot_a = next(iter(tr.xpath(".//a[contains(@href, '#lot')]")), None)
        if lot_a is not None:
            parts = re.split(r"<br\s*/?>", etree.tostring(lot_a, encoding="unicode", with_tail=False), maxsplit=1)
            plain = [html.unescape(re.sub(r"<[^>]+>", " ", x)) for x in parts]
            short = re.sub(r"^\s*Лот\s*№\s*\d+\s*:?", "", re.sub(r"\s+", " ", plain[0])).strip()
            full = re.sub(r"\s+", " ", plain[1]).strip() if len(plain) > 1 else ""
        else:
            cell = _txt(tds[2])
            m = re.search(r"Лот\s*№\s*(\d+)\s*:\s*(.*)", cell)
            short, full = (m.group(2) if m else cell), ""
        sep = ". . ." in short
        short = short.replace(". . .", " ")
        full = re.sub(r"\s*\.\s\.\s\.\s*$", "", full).strip(" .") or short
        name = re.sub(r"\s+", " ", short).strip(" ,.")
        if sep:                       # название обрезано сайтом — берём начало полного описания
            title = _short(full, 150)
        elif len(name) >= 25 or not full or full == name:
            title = name
        else:
            title = _short(f"{name}. {full}", 150)
        code_m = re.match(r"(\d+)", _txt(tds[0]))
        start, end = _parse_dt(_txt(tds[4])), _parse_dt(_txt(tds[5]))
        lots.append({
            "title": title, "card_text": f"{name} . {full}", "price": parse_price(_txt(tds[3])), "url": url,
            "cat_id": "nistp", "cat_name": "", "cat_margin": 40, "date_end": _days_left(end),
            "code": int(code_m.group(1)) if code_m else 0, "src": "nistp", "source": "nistp.ru",
            "status": _txt(tds[6]), "fresh_key": _dt_key(start),
            "apps_from": start.strftime("%d.%m.%Y") if start else "", "head": f"{name} . {full[:150]}",
        })
    return lots


def fetch_nistp(kind, q, progress=_noop, errors=None, max_pages=None):
    desc = "автомобиль" if kind == "auto" else "Моск"
    params = [("view_type", "lot"), ("lot_description", desc),
              ("trade_state[]", "Торги объявлены"), ("trade_state[]", "Прием заявок")]
    allowed = _realty_allowed(q)
    lots, seen = [], set()
    for page in range(1, (max_pages or OTHER_MAX_PAGES) + 1):
        try:
            r = requests.get(f"{NISTP}/bankrot/trade_list.php", params=params + [("pagenum", page)],
                             headers=HEADERS, timeout=25)
            r.raise_for_status()
        except Exception as e:
            log.error(f"  НИСТП стр.{page}: {e}")
            if errors is not None and page == 1:
                errors.append(_error_reason(e))
            break
        page_lots = _parse_nistp_page(r.text, kind)
        new = [l for l in page_lots if l["url"] not in seen]
        seen.update(l["url"] for l in new)
        for l in new:
            if l["price"] <= 0 or _OPEN_STATUS_BAD.search(l["status"]):
                continue
            if kind == "realty":
                types = realty_types_of(l["head"])
                if not types & allowed:
                    continue
                l["cat_name"] = ", ".join(REALTY_TYPES[t][0] for t in REALTY_TYPES if t in types)
                reg, label, src = detect_region(l["card_text"])
                l["region"], l["region_label"], l["region_src"] = reg, label, ("текст" if src == "текст" else "адрес")
            elif not is_passenger_car(l["head"]):
                continue
            lots.append(l)
        log.info(f"  НИСТП стр.{page}: на странице {len(page_lots)}, новых {len(new)}")
        progress(f"НИСТП: страница {page}, лотов {len(lots)}")
        if not new:
            break
    return lots

# ─── Год выпуска ───────────────────────────────────────

_DATE_RE = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
_YEAR_RES = [
    re.compile(r"год\w*\s+(?:выпуска|изготовления)[^\d]{0,25}((?:19|20)\d\d)"),
    # «2012 г.в.», «2012г.в», «2012 года выпуска» — но не «от 5 марта 2019 г. в отношении должника»
    re.compile(r"((?:19|20)\d\d)\s*(?:г\.?\s?в\.|г\.?в\b|гв\b|года?\s+выпуска|г\.?\s+выпуска|"
               r"года?\s+изготовления)"),
    re.compile(r"\bг\.\s?в\.\s*[:\-–—]?\s*((?:19|20)\d\d)"),
]
_BARE_YEAR_RE = re.compile(r"(?<![\w.,/-])((?:19[89]|20[0-3])\d)(?![\w])")


def extract_years(text, allow_bare=False, first_only=False):
    """Годы выпуска из текста. allow_bare — искать и «голый» год (только для названия лота).
    first_only — взять только первое упоминание (на странице лота дальше бывают другие лоты)."""
    t = _DATE_RE.sub(" ", (text or "").lower())
    found = []
    for rx in _YEAR_RES:
        found += [(m.start(), int(m.group(1))) for m in rx.finditer(t)]
    if not found and allow_bare:
        found = [(m.start(), int(m.group(1))) for m in _BARE_YEAR_RE.finditer(t)]
    found = [(p, y) for p, y in found if 1980 <= y <= CUR_YEAR + 1]
    if not found:
        return []
    if first_only:
        return [min(found)[1]]
    return sorted({y for _, y in found})


def _focus(text, title, span):
    """Фрагмент страницы лота, начиная с его собственного названия (если оно найдено)."""
    key = re.sub(r"\s+", " ", (title or "").lower()).strip()[:40]
    pos = text.lower().find(key) if len(key) >= 10 else -1
    return text[pos: pos + span] if pos >= 0 else None


def detail_years(text, title):
    focus = _focus(text, title, 1500)
    return (extract_years(focus, first_only=True) if focus else []) or extract_years(text, first_only=True)


# На странице лота есть служебные поля: «Код лота РАД-… Код процедуры … Регион <…> Адрес <…>».
# Это данные самого лота; ниже на странице идут «похожие лоты» с чужими адресами.
_FIELD_OWN_RE = re.compile(r"Код лота\s+\S+.{0,120}?\bРегион\s+(.{2,60}?)\s+Адрес\s+(.{0,160})")
_FIELD_ANY_RE = re.compile(r"\bРегион\s+(.{2,60}?)\s+Адрес\s+(.{0,160})")


def region_from_fields(text):
    m = _FIELD_OWN_RE.search(text or "") or _FIELD_ANY_RE.search(text or "")
    if not m:
        return None
    reg = m.group(1).lower().replace("ё", "е")
    addr = m.group(2).lower().replace("ё", "е")
    if "москв" in reg or "московск" in reg:
        if "московск" in reg:
            city = _MO_CITY_RE.search(addr)
            label = f"Моск. обл., {city.group(1).title()}" if city else "Московская обл."
        else:
            label = "Москва"
        return "msk", label, "поле «Регион»"
    return "other", None, "поле «Регион»"


def detail_region(text, title):
    reg = region_from_fields(text)
    if reg:
        return reg
    focus = _focus(text, title, 3000)
    reg = detect_region(focus) if focus else (None, None, None)
    return reg if reg[0] else detect_region(text)


def year_label(years):
    if not years:
        return ""
    return f"{years[0]} г.в." if len(years) == 1 else f"{years[0]}–{years[-1]} г.в."

# ─── Регион (Москва и область) ─────────────────────────

_MO_CITIES = [
    "одинцово", "химки", "мытищи", "балашиха", "подольск", "красногорск", "люберцы", "королев",
    "домодедово", "раменское", "щелково", "сергиев посад", "пушкино", "ногинск", "коломна",
    "серпухов", "электросталь", "долгопрудный", "реутов", "видное", "истра", "наро-фоминск",
    "можайск", "дмитров", "клин", "солнечногорск", "чехов", "жуковский", "ивантеевка", "лобня",
    "звенигород", "руза", "волоколамск", "егорьевск", "шатура", "ступино", "кашира", "луховицы",
    "зарайск", "орехово-зуево", "павловский посад", "воскресенск", "талдом", "лотошино",
    "шаховская", "серебряные пруды", "фрязино", "лыткарино", "дзержинский", "котельники",
    "электроугли", "старая купавна", "апрелевка", "голицыно", "кубинка", "дедовск",
    "краснознаменск", "бронницы", "протвино", "пущино", "дубна", "черноголовка", "власиха",
    "электрогорск", "ликино-дулево", "рошаль", "озеры", "коммунарка", "щербинка", "троицк",
]
_MSK_RE = re.compile(r"\bмосква\b|\bмосквы\b|\bмоскве\b|\bг\.?\s*москв|московск\w*\s+обл|"
                     r"\bмо\b|подмосков|зеленоград|новая\s+москва")
_MO_CITY_RE = re.compile(r"(?:\bг\.?\s*о?\.?\s*|\bгород\w*\s+(?:округ\s+)?)(" +
                         "|".join(re.escape(c) for c in _MO_CITIES) + r")\b")
_SPB_RE = re.compile(r"санкт-петербург|ленинградск\w*\s+обл|\bспб\b")
_OBL_RE = re.compile(r"\b[а-я-]{1,40}(?:ская|цкая|ской|цкой)\s+обл")
_OTHER_RE = re.compile(r"\b[а-я-]{1,40}(?:ская|цкая|ской|цкой)\s+обл|\bкра[йя]\b|\bреспублик|"
                       r"санкт-петербург|ленинградск|\bг\.\s*(?!москв)[а-я-]{3,}|"
                       r"\bгород\s+(?!москв)[а-я-]{3,}")
# Упоминания, где адрес относится не к имуществу (суд, управляющий, регистрация должника и т.п.)
_NOISE_RE = re.compile(
    r"(арбитражн\w*\s+суд\w*|суд\w*\s+(?:г\.|города)|ифнс|уфнс|инспекци\w*\s+(?:фнс|федеральной)|"
    r"росреестр\w*|зарегистрирован\w*|юридическ\w*\s+адрес|адрес\s+регистрации|место\s+жительства|"
    r"проживающ\w*|паспорт\w*|организатор\w*|арбитражн\w*\s+управляющ\w*|конкурсн\w*\s+управляющ\w*|"
    r"финансов\w*\s+управляющ\w*|почтов\w*\s+адрес|адрес\s+для\s+корреспонденции|а/я|"
    r"абонентск\w*\s+ящик|e-?mail|тел\.|телефон\w*|заявк\w*)[^.;]{0,150}")
_ABBR_RE = re.compile(r"\b(г|ул|д|обл|кв|пр|пл|п|с|ст|корп|стр|пос|мкр|им|о|пер|ш|наб|тер|уч|р|вл)\.")
_ADDR_RE = re.compile(r"(местонахожд\w*|местоположен\w*|расположен\w*|по\s+адресу|"
                      r"адрес\s+объекта|адрес\s+имущества|адрес\s*:|регион\w*(?:\s+объекта)?\s*:)")


def _classify(w):
    """Регион по первому упоминанию в тексте: дальше часто идут адреса соседних лотов."""
    hits = []
    for rx, kind in ((_MSK_RE, "msk"), (_SPB_RE, "other"), (_OTHER_RE, "other")):
        m = rx.search(w)
        if m:
            hits.append((m.start(), 0 if kind == "msk" else 1, kind))
    city = _MO_CITY_RE.search(w)
    if city:
        obl = _OBL_RE.search(w)
        # Город из Подмосковья учитываем, если рядом не названа другая область (Троицк бывает и Челябинский)
        if not obl or _MSK_RE.search(obl.group(0)):
            hits.append((city.start(), 0, "msk"))
    return min(hits)[2] if hits else None


def _region_label(w):
    city = _MO_CITY_RE.search(w)
    if "зеленоград" in w:
        return "Москва (Зеленоград)"
    if re.search(r"\bмосква\b|\bмосквы\b|\bмоскве\b|\bг\.?\s*москв|новая\s+москва", w) \
            and not re.search(r"московск\w*\s+обл", w):
        return "Москва"
    if city:
        return f"Моск. обл., {city.group(1).title()}"
    return "Московская обл."


def detect_region(text):
    """('msk'|'other'|None, подпись, по чему определено)."""
    t = (text or "").lower().replace("ё", "е")
    # Точки в сокращениях («г.», «ул.», «обл.») не считаем концом фразы при вырезании «шума»
    t = _ABBR_RE.sub(r"\1§", t)
    p = _NOISE_RE.sub(" ", t)          # здесь точки — только концы фраз
    t = p.replace("§", ".")
    # Берём первое адресное упоминание, по которому регион определяется однозначно.
    # Адрес обрезаем по концу фразы, чтобы не захватить адрес соседнего лота.
    for m in _ADDR_RE.finditer(p):
        w = re.split(r"\.\s|;|\bлот\s*№?\s*\d", p[m.end(): m.end() + 200])[0].replace("§", ".")
        v = _classify(w)
        if v == "msk":
            return "msk", _region_label(w), "адрес"
        if v == "other":
            return "other", None, "адрес"
    v = _classify(t)
    if v == "msk":
        return "msk", _region_label(t), "текст"
    return v, None, ("текст" if v else None)

# ─── Дата публикации ──────────────────────────────────

_PUB_RE = re.compile(r"[Нн]а lot-online\.ru:?\s*(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
# На МЭТС берём дату размещения сообщения о торгах в ЕФРСБ
_PUB_RE_METS = re.compile(r"Дата размещения сообщения в Едином [Фф]едеральном [Рр]еестре[^0-9]{0,80}"
                          r"(\d{2})\.(\d{2})\.(\d{4})(?:\s+(\d{1,2}):(\d{2}))?")
_pub_cache = {}   # url -> (дата «дд.мм.гггг», ключ для сортировки)


def extract_pub_date(text):
    m = _PUB_RE.search(text or "") or _PUB_RE_METS.search(text or "")
    if not m:
        return None
    d, mo, y, h, mi = m.groups()
    return f"{d}.{mo}.{y}", (int(y), int(mo), int(d), int(h or 0), int(mi or 0))


def remember_pub(l, text):
    pub = extract_pub_date(text)
    if pub:
        l["published"], l["pub_key"] = pub
        _pub_cache[l["url"]] = pub


# ─── Параметры поиска из команды ──────────────────────
# Примеры: «/realty квартиры москва до 8 млн новые», «/auto kia 2021 до 2 млн»

REALTY_TYPES = {
    # ключ: (подпись, разделы РАД, категории МЭТС, слова в команде)
    "flat":    ("квартиры", [34], [36], r"\bкварт\w*"),
    "house":   ("дома", [38], [34], r"\b(?:дом|дома|домов|домик\w*|коттедж\w*|дач\w*)\b"),
    "office":  ("нежилые помещения", [23], [37], r"\bнежил\w*|\bпомещ\w*|\bофис\w*|\bкоммерч\w*"),
    "building": ("здания", [22], [38], r"\bздани\w*"),
    "garage":  ("гаражи и машино-места", [26], [39], r"\bгараж\w*|\bмашин\w*[- ]?мест\w*|\bпаркинг\w*"),
    "land":    ("земельные участки", "land", [11], r"\bучаст\w*|\bземл\w*|\bземел\w*"),
}
_SORT_NEW_RE = re.compile(r"\b(?:нов\w*|свеж\w*|дат\w*|new)\b")
_SORT_PRICE_RE = re.compile(r"\b(?:цен\w*|дешев\w*|price)\b")
_REG_OBL_RE = re.compile(r"\bобласт\w*|\bмо\b|\bподмосков\w*")
_REG_CITY_RE = re.compile(r"\bмосква\b|\bмоскве\b|\bмск\b")
_YEAR_ARG_RE = re.compile(r"(?:\b(?:с|от|после)\s*)?\b(19[89]\d|20[0-3]\d)\b(?:\s*(?:г\.?\s?в\.?|года?|гв|г\.))?"
                          r"(?!\s*(?:млн|тыс|т\b|к\b|руб|₽|р\b))")
_PRICE_ARG_RE = re.compile(r"\b(до|от)\s*(\d+(?:[.,]\d+)?(?:\s\d{3})*)\s*"
                           r"(млн\w*|миллион\w*|м\b|тыс\w*|т\.?р\.?|т\b|к\b|k\b|руб\w*|р\b|₽)?")
_STOP_WORDS = {"все", "лоты", "лот", "руб", "рублей", "млн", "тыс", "года", "год", "для", "без", "или"}


def default_query(kind):
    return {"kind": kind, "sort": "new" if SORT_DEFAULT.lower().startswith("new") else "price",
            "types": None, "min_price": MIN_PRICE, "max_price": MAX_PRICE,
            "min_year": AUTO_MIN_YEAR, "region": None, "words": []}


def parse_query(args, kind):
    """Разбирает слова после команды: тип объекта, цена, год, регион, сортировка, слова для поиска."""
    q = default_query(kind)
    t = " " + " ".join(args).lower().replace("ё", "е") + " "
    if _SORT_NEW_RE.search(t):
        q["sort"] = "new"
    elif _SORT_PRICE_RE.search(t):
        q["sort"] = "price"
    t = _SORT_PRICE_RE.sub(" ", _SORT_NEW_RE.sub(" ", t))
    if kind == "auto":
        m = _YEAR_ARG_RE.search(t)
        if m:
            q["min_year"] = int(m.group(1))
            t = t[:m.start()] + " " + t[m.end():]
    for m in list(_PRICE_ARG_RE.finditer(t)):
        num = float(re.sub(r"\s", "", m.group(2)).replace(",", "."))
        unit = (m.group(3) or "").lower()
        if unit.startswith(("млн", "миллион", "м")):
            num *= 1_000_000
        elif unit.startswith(("тыс", "т", "к", "k")):
            num *= 1_000
        elif not unit.startswith(("руб", "р", "₽")) and num < 1000:
            num *= 1_000_000          # «до 5» — считаем, что это миллионы
        q["max_price" if m.group(1) == "до" else "min_price"] = int(num)
    t = _PRICE_ARG_RE.sub(" ", t)
    if kind == "realty":
        types = [k for k, v in REALTY_TYPES.items() if re.search(v[3], t)]
        if types:
            q["types"] = types
        for v in REALTY_TYPES.values():
            t = re.sub(v[3], " ", t)
        if _REG_OBL_RE.search(t):
            q["region"] = "oblast"
        elif _REG_CITY_RE.search(t):
            q["region"] = "city"
        t = _REG_CITY_RE.sub(" ", _REG_OBL_RE.sub(" ", t))
    q["words"] = [w for w in re.findall(r"[a-zа-я0-9][a-zа-я0-9-]{2,}", t) if w not in _STOP_WORDS]
    return q


def parse_sort(args):
    return parse_query(args, "auto")["sort"]


def realty_cats(q):
    """Разделы РАД и категории МЭТС для выбранных типов недвижимости."""
    if not q.get("types"):
        return REALTY_CATS, METS_REALTY_CATS, []
    rad, mets, notes = [], [], []
    for key in q["types"]:
        label, rad_ids, mets_ids, _ = REALTY_TYPES[key]
        if rad_ids == "land":
            found = rad_cats_by_name(r"земел|земл")
            if not found and "rad" in SOURCES:
                notes.append("РАД — раздел с земельными участками не найден на сайте")
            rad_ids = found
        rad += [c for c in rad_ids if c not in rad]
        mets += [c for c in mets_ids if c not in mets]
    return rad, mets, notes


def _region_ok(l, q):
    if not q.get("region"):
        return True
    city = (l.get("region_label") or "").startswith("Москва")
    return city if q["region"] == "city" else not city


def _words_ok(l, q):
    if not q.get("words"):
        return True
    hay = " ".join([l.get("title", ""), l.get("card_text", ""), l.get("region_label") or "",
                    l.get("location") or ""]).lower().replace("ё", "е")
    return all(w in hay for w in q["words"])


def describe_query(q):
    """Короткая строка с условиями поиска для заголовка списка."""
    parts = []
    if q["kind"] == "realty":
        what = ", ".join(REALTY_TYPES[k][0] for k in q["types"]) if q.get("types") else "недвижимость"
        where = {"city": "Москва", "oblast": "Московская обл."}.get(q.get("region"), "Москва и Московская обл.")
        parts.append(f"{what[:1].upper()}{what[1:]}: {where}")
    else:
        parts.append(f"Авто от {q['min_year']} г.в.")
    price = []
    if q.get("min_price"):
        price.append(f"от {rub(q['min_price'])}")
    if q.get("max_price"):
        price.append(f"до {rub(q['max_price'])}")
    if price:
        parts.append(" ".join(price))
    if q.get("words"):
        parts.append("«" + " ".join(q["words"]) + "»")
    return ", ".join(parts)


def order_lots(found, sort, progress=_noop):
    """Сортировка и даты публикации для показываемых лотов."""
    if sort == "new":
        # Номера лотов растут со временем, но у каждой площадки свои — берём самые свежие с каждой,
        # узнаём даты публикации и уже по ним сортируем вместе
        top, ids = [], set()
        for src in SRC_ORDER:
            part = sorted((l for l in found if l.get("src", "rad") == src),
                          key=lambda l: (l.get("fresh_key") or (0,), l.get("code") or 0), reverse=True)
            top += part[:TOP_N]
        ids = {id(l) for l in top}
        rest = sorted((l for l in found if id(l) not in ids), key=lambda l: -(l.get("code") or 0))
    else:
        found.sort(key=lambda l: l["price"])
        top, rest = found[:TOP_N], found[TOP_N:]
    for l in top:
        if not l.get("published") and l["url"] in _pub_cache:
            l["published"], l["pub_key"] = _pub_cache[l["url"]]
    # Дату публикации со страницы лота берём только у РАД и МЭТС; у остальных есть дата начала приёма заявок
    need = [l for l in top if not l.get("published") and l.get("src", "rad") in ("rad", "mets")]

    def work(l):
        try:
            text, _ = fetch_detail_text(l["url"])
            remember_pub(l, text)
        except Exception as e:
            log.error(f"Дата публикации {l['url']}: {e}")

    _fetch_details(need, work, progress, "даты публикации")
    if sort == "new":
        top.sort(key=lambda l: (l.get("pub_key") or l.get("fresh_key") or (0,), l.get("code") or 0), reverse=True)
    found[:] = top + rest
    return found

# ─── Исключения для недвижимости ──────────────────────

_LOT_PREFIX = r"^(?:лот\s*№?\s*\d+\s*[,.:;-]?\s*)?"
_EXCLUDE_RULES = {
    # «Право аренды нежилого помещения…» — но не «Жилой дом и право аренды земельного участка»
    "lease": ("право аренды", re.compile(_LOT_PREFIX + r"(?:прав[оа]|уступк\w*\s+прав\w*)\s+(?:\w+\s+){0,2}аренд")),
    # «Доля в праве 1/18…», «1/2 доли в праве на квартиру», «…(1/3 доли)»
    "share": ("доли в праве", re.compile(_LOT_PREFIX + r"дол[яиюе]\b|\d+\s*/\s*\d+\s*(?:дол|доли)|"
                                          r"\bдол(?:я|и|ю|е|ей)\s+(?:\d|в\s+(?:праве|общей|размере|земельн|"
                                          r"квартир|жил|нежил|доме|ооо|уставн))|\bторги\s+долей\b")),
    "claim": ("права требования", re.compile(r"прав[оа]\s+(?:\w+\s+){0,2}требовани|дебиторск\w*\s+задолж|"
                                             r"уступк\w*\s+прав\w*\s+требовани")),
    "storage": ("кладовые", re.compile(r"кладов|хозблок|хозяйственн\w*\s+блок")),
}


def excluded_reason(title):
    t = re.sub(r"\s+", " ", (title or "").lower().replace("ё", "е")).strip()
    for key in REALTY_EXCLUDE:
        rule = _EXCLUDE_RULES.get(key)
        if rule and rule[1].search(t):
            return rule[0]
    return None

# ─── Поиск авто и недвижимости ─────────────────────────

_year_cache = {}     # url -> список годов из карточки лота
_region_cache = {}   # url -> (регион, подпись, источник)


def _price_ok(l, q):
    return (q.get("min_price") or 0) <= l["price"] <= (q.get("max_price") or float("inf"))


def _fetch_details(lots, worker, progress=_noop, what="карточки лотов"):
    lots = lots[:MAX_DETAIL_FETCH]
    done, lock = [0], threading.Lock()

    def run(l):
        worker(l)
        with lock:
            done[0] += 1
            n = done[0]
        progress(f"Открываю {what}: {n} из {len(lots)}")

    if lots:
        with ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(run, lots))
    return len(lots)


def search_auto(progress=_noop, q=None):
    q = q or default_query("auto")
    all_lots, by_src, errors = gather_all("auto", AUTO_CATS, METS_AUTO_CATS, q, progress)
    lots = [l for l in all_lots if _price_ok(l, q) and _words_ok(l, q)]
    need = []
    for l in lots:
        years = extract_years(l["title"], allow_bare=True) or extract_years(l["card_text"])
        if not years and l["url"] in _year_cache:
            years = _year_cache[l["url"]]
        l["years"] = years
        if not years and l["url"] not in _year_cache:
            need.append(l)

    def work(l):
        try:
            text, _ = fetch_detail_text(l["url"])
            l["years"] = detail_years(text, l["title"])
            remember_pub(l, text)
        except Exception as e:
            log.error(f"Карточка {l['url']}: {e}")
            return
        _year_cache[l["url"]] = l["years"]

    opened = _fetch_details(need, work, progress, "лоты без года в названии")
    found = [l for l in lots if l["years"] and l["years"][-1] >= q["min_year"]]
    order_lots(found, q["sort"], progress)
    stats = {"total": len(all_lots), "in_price": len(lots), "opened": opened,
             "not_opened": max(0, len(need) - opened),
             "no_year": sum(1 for l in lots if not l["years"]),
             "by_src": by_src, "errors": errors}
    return found, stats


def search_realty(progress=_noop, q=None):
    q = q or default_query("realty")
    rad_cats, mets_cats, notes = realty_cats(q)
    all_lots, by_src, errors = gather_all("realty", rad_cats, mets_cats, q, progress)
    in_price = [l for l in all_lots if _price_ok(l, q)]
    lots, excluded = [], {}
    for l in in_price:
        why = excluded_reason(l["title"])
        if why:
            excluded[why] = excluded.get(why, 0) + 1
        else:
            lots.append(l)
    need = []
    for l in lots:
        if l.get("src") == "mets":
            # У МЭТС регион — отдельное поле «Регион местонахождения имущества»
            loc = l.get("location") or ""
            reg, label = _region_from(loc, l["card_text"]) if loc else (None, None)
            l["region"], l["region_label"], l["region_src"] = reg, label, "МЭТС"
            continue
        if l.get("src", "rad") != "rad":
            continue          # Альфалот, ЦДТ, НИСТП — регион уже определён при загрузке
        if l["url"] in _region_cache:
            l["region"], l["region_label"], l["region_src"] = _region_cache[l["url"]]
            continue
        reg, label, src = detect_region(l["title"] + " . " + l["card_text"])
        l["region"], l["region_label"], l["region_src"] = reg, label, ("название" if reg else None)
        if reg:
            _region_cache[l["url"]] = (reg, label, "название")
        else:
            need.append(l)

    def work(l):
        try:
            text, _ = fetch_detail_text(l["url"])
        except Exception as e:
            log.error(f"Карточка {l['url']}: {e}")
            return
        reg, label, src = detail_region(text, l["title"])
        l["region"], l["region_label"], l["region_src"] = reg, label, src
        remember_pub(l, text)
        _region_cache[l["url"]] = (reg, label, src)

    opened = _fetch_details(need, work, progress, "лоты без адреса в названии")
    found = [l for l in lots if l.get("region") == "msk" and _region_ok(l, q) and _words_ok(l, q)]
    order_lots(found, q["sort"], progress)
    stats = {"total": len(all_lots), "in_price": len(in_price), "opened": opened,
             "not_opened": max(0, len(need) - opened),
             "unknown": sum(1 for l in lots if not l.get("region")),
             "excluded": excluded, "by_src": by_src, "errors": errors, "notes": notes}
    return found, stats


def add_drom_prices(lots, n=5):
    """Для топ-N авто ищем объявления на drom.ru (только реальные цены, без «эвристики»)."""
    for l in lots[:n]:
        try:
            prices = sorted(search_drom(l["title"]))
        except Exception as e:
            log.error(f"drom: {e}")
            continue
        if len(prices) >= 2:
            l["drom"] = {"median": prices[len(prices) // 2], "count": len(prices)}

# ─── Форматирование ───────────────────────────────────

def fmt_simple(lot, i, kind):
    parts = [f"💰 <b>{rub(lot['price'])}</b>"]
    if kind == "auto" and lot.get("years"):
        parts.append(f"📅 {year_label(lot['years'])}")
    if kind == "auto" and lot.get("src", "rad") != "rad" and lot.get("location"):
        parts.append(f"📍 {esc(lot['location'])}")
    if kind == "realty":
        if lot.get("region_label"):
            label = lot["region_label"]
            if lot.get("region_src") == "текст":
                label += " (по тексту лота — проверьте адрес)"
            parts.append(f"📍 {esc(label)}")
        parts.append(esc(re.sub(r"^\W+\s*", "", lot.get("cat_name", ""))))
    if lot.get("min_price") and lot["min_price"] < lot["price"]:
        parts.append(f"📉 мин. {rub(lot['min_price'])}")
    if lot.get("published"):
        parts.append(f"📢 опубл. {lot['published']}")
    elif lot.get("apps_from"):
        parts.append(f"📝 заявки с {lot['apps_from']}")
    if lot.get("date_end"):
        parts.append(f"⏳ торги {esc(lot['date_end'])}")
    if _src_label(lot):
        parts.append(f"🏛 {_src_label(lot)}")
    msg = f"<b>{i}. {esc(_short(lot['title']))}</b>\n" + " · ".join(parts) + "\n"
    d = lot.get("drom")
    if d:
        diff = (d["median"] - lot["price"]) / lot["price"] * 100 if lot["price"] else 0
        msg += f"🏪 drom.ru: ~{rub(d['median'])} (объявлений: {d['count']}, разница {diff:+.0f}%)\n"
    msg += f'<a href="{lot["url"]}">Открыть лот</a>\n'
    return msg


def _sort_label(sort):
    return "сначала новые" if sort == "new" else "отсортировано по цене"


def _sources_line(stats):
    by = stats.get("by_src") or {}
    line = "\nПросмотрено лотов: " + ", ".join(f"{k} — {v}" for k, v in by.items()) if by else \
        f"\nПросмотрено лотов: {stats['total']}"
    for e in stats.get("errors") or []:
        line += f"\n⚠️ Не удалось загрузить {esc(e)}"
    for n in stats.get("notes") or []:
        line += f"\n⚠️ {esc(n)}"
    return line


def _sources_title():
    return " + ".join(SRC_LABEL[s] for s in SRC_ORDER if s in SOURCES)


def fmt_auto(lots, stats, q=None):
    q = q or default_query("auto")
    head = (f"🚗 <b>{esc(describe_query(q))}</b> · {_sources_title()}\n"
            f"Найдено: <b>{len(lots)}</b> · {_sort_label(q['sort'])}\n")
    if not lots:
        body = "\nПодходящих лотов сейчас нет.\n"
    else:
        body = "\n" + "\n".join(fmt_simple(l, i, "auto") for i, l in enumerate(lots[:TOP_N], 1))
    foot = (_sources_line(stats) + f"\nПо цене подошло: {stats['in_price']}, "
            f"год не указан: {stats['no_year']}")
    if stats["not_opened"]:
        foot += f", не проверено (лимит): {stats['not_opened']}"
    if len(lots) > TOP_N:
        foot += f"\nПоказаны первые {TOP_N} из {len(lots)}."
    return head + body + foot + "\n💡 /analyze N — подробнее о лоте N из последнего списка"


def fmt_realty(lots, stats, q=None):
    q = q or default_query("realty")
    head = (f"🏠 <b>{esc(describe_query(q))}</b> · {_sources_title()}\n"
            f"Найдено: <b>{len(lots)}</b> · {_sort_label(q['sort'])}\n")
    if not lots:
        body = "\nПодходящих лотов сейчас нет.\n"
    else:
        body = "\n" + "\n".join(fmt_simple(l, i, "realty") for i, l in enumerate(lots[:TOP_N], 1))
    foot = (_sources_line(stats) + f"\nПо цене подошло: {stats['in_price']}, "
            f"регион не определён: {stats['unknown']}")
    if stats["not_opened"]:
        foot += f", не проверено (лимит): {stats['not_opened']}"
    if stats.get("excluded"):
        foot += "\nИсключено: " + ", ".join(f"{k} — {v}" for k, v in stats["excluded"].items())
    if len(lots) > TOP_N:
        foot += f"\nПоказаны первые {TOP_N} из {len(lots)}."
    return head + body + foot + "\n💡 /analyze N — подробнее о лоте N из последнего списка"


def analyze(lots, keyword=None):
    """Сортировка для общего /scan (по условной марже категории и цене)."""
    for lot in lots:
        text = (lot.get("title", "") or "").lower()
        kw_margin = max([m for kw, (p, m) in KW_MARGINS.items() if kw in text] or [0])
        lot["margin"] = max(lot.get("cat_margin", 40), kw_margin)
        price = lot.get("price", 0)
        pb = 1.3 if price <= 50000 else (1.1 if price <= 500000 else 0.8)
        lot["score"] = lot["margin"] * pb
    lots.sort(key=lambda x: x["score"], reverse=True)
    return lots


def real_market(title, price):
    """Рыночный анализ только по реальным найденным ценам — «эвристику» отбрасываем."""
    m = search_market_prices(title)
    if not m:
        return None
    srcs = set((m.get("sources") or {}).keys())
    if not srcs or srcs <= {"эвристика"}:
        return None
    return calculate_margin(price, m)


def fmt_lot(lot, i):
    p = lot.get("price", 0)
    msg = f"<b>{i}. {esc(_short(lot.get('title'), 70))}</b>\nЦена на торгах: <b>{rub(p)}</b>\n"
    ma = lot.get("market_analysis")
    if ma and ma.get("status") not in ("no_data", "error"):
        msg += (f"Рыночная цена: <b>{rub(ma['avg_market'])}</b>\n"
                f"{ma['emoji']} Разница с рынком: <b>{ma['margin_pct']}%</b>\n")
        if ma.get("sources"):
            msg += f"Источники цен: {', '.join(ma['sources'].keys())} (цен: {ma['total_prices']})\n"
    else:
        msg += "Рыночная цена: не найдена\n"
    if lot.get("cat_name"):
        msg += f"Категория: {esc(lot['cat_name'])}\n"
    if lot.get("date_end"):
        msg += f"Окончание: {lot['date_end']}\n"
    msg += f'<a href="{lot["url"]}">Перейти к лоту</a>\n'
    return msg


def fmt_lot_full(lot, i):
    p = lot.get("price", 0)
    msg = f"<b>📊 ЛОТ #{i}</b>\n━━━━━━━━━━━━━━━━━━━━━━━━━\n<b>{esc(_short(lot.get('title'), 400))}</b>\n\n"
    msg += f"<b>🏦 Цена на торгах:</b> {rub(p)}\n"
    if lot.get("min_price") and lot["min_price"] < p:
        msg += f"📉 Минимальная цена (публичное предложение): {rub(lot['min_price'])}\n"
    if lot.get("years"):
        msg += f"📅 {year_label(lot['years'])}\n"
    if lot.get("region_label"):
        msg += f"📍 {esc(lot['region_label'])}\n"
    ma = lot.get("market_analysis")
    if ma and ma.get("status") not in ("no_data", "error"):
        msg += f"\n<b>🏪 Рыночная цена:</b> {rub(ma['avg_market'])}\n"
        msg += f"  Минимум: {rub(ma['min_market'])}\n"
        msg += f"\n{ma['emoji']} <b>Разница с рынком: {ma['margin_pct']}%</b>\n{ma['recommendation']}\n"
        msg += f"  Оптимальная цена продажи: {rub(ma['optimal_sell'])} (прибыль {rub(ma['profit_optimal'])})\n"
        msg += f"  💸 Налог 13%: ~{rub(ma.get('tax', 0))}\n"
        if ma.get("sources"):
            msg += "\n<b>📍 Источники цен:</b>\n"
            for src, info in ma["sources"].items():
                msg += f"  • {esc(src)}: {info['count']} цен, средняя {rub(info['avg'])}\n"
        msg += "⚠️ Цены из поиска грубые — сверяйте вручную.\n"
    elif lot.get("drom"):
        d = lot["drom"]
        diff = (d["median"] - p) / p * 100 if p else 0
        msg += (f"\n<b>🏪 drom.ru:</b> медиана {rub(d['median'])} по {d['count']} объявлениям "
                f"(разница с ценой лота {diff:+.0f}%)\n"
                "⚠️ Поиск по названию — сверьте модель, комплектацию и пробег вручную.\n")
    elif lot.get("kind") == "realty" or lot.get("cat_id") in REALTY_CATS:
        msg += "\nℹ️ Автоматической оценки для недвижимости нет — сравните с ЦИАН/Авито по адресу.\n"
    else:
        msg += "\n⚠️ Рыночных цен не найдено — оцените вручную.\n"
    if lot.get("cat_name"):
        msg += f"\nКатегория: {esc(lot['cat_name'])}"
    if lot.get("status"):
        msg += f"\nСтатус: {esc(lot['status'])}"
    if lot.get("status_note"):
        msg += f" ({esc(lot['status_note'])})"
    if lot.get("published"):
        msg += f"\nОпубликовано: {lot['published']}"
    elif lot.get("apps_from"):
        msg += f"\nПриём заявок с {lot['apps_from']}"
    if lot.get("date_end"):
        msg += f"\nОкончание: {esc(lot['date_end'])}"
    if _src_label(lot):
        msg += f"\nПлощадка: {_src_label(lot)}"
    msg += f'\n<a href="{lot["url"]}">Перейти к лоту</a>\n'
    return msg


def fmt_digest(lots, n=10):
    if not lots:
        return "<b>Торги — дайджест</b>\n\nПодходящих лотов нет."
    lines = ["<b>ТОП лотов</b>\n━━━━━━━━━━━━━━━━━━━━━\n"]
    for i, lot in enumerate(lots[:n], 1):
        lines.append(fmt_lot(lot, i))
        lines.append("───────────────────")
    lines.append(f"\nВсего: {len(lots)} лотов")
    lines.append("💡 /analyze [номер] — подробности по лоту")
    return "\n".join(lines)

# ─── Chat IDs и состояние ─────────────────────────────

LAST_LOTS = {}          # chat_id -> лоты последнего поиска (для /analyze)
_busy, _busy_lock = set(), threading.Lock()


def load_chat_ids():
    ids = set()
    if CHAT_ID:
        ids.add(int(CHAT_ID))
    try:
        with open("chat_ids.txt") as f:
            ids.update(int(l.strip()) for l in f if l.strip())
    except FileNotFoundError:
        pass
    return ids


def save_chat_id(cid):
    if cid not in load_chat_ids():
        with open("chat_ids.txt", "a") as f:
            f.write(f"{cid}\n")


def run_exclusive(cid, fn):
    with _busy_lock:
        if cid in _busy:
            answer(cid, "⏳ Предыдущий поиск ещё идёт — дождитесь результата.")
            return
        _busy.add(cid)
    try:
        fn()
    except Exception as e:
        log.error(f"Command error: {e}", exc_info=True)
        answer(cid, "Ошибка при поиске. Подробности — в логах Render.")
    finally:
        with _busy_lock:
            _busy.discard(cid)

# ─── Команды ───────────────────────────────────────────

HELP_TEXT = (
    "<b>Бот банкротских торгов</b>\nПлощадки: РАД, МЭТС, Альфалот, ЦДТ, НИСТП\n\n"
    "<b>Недвижимость в Москве и МО</b>\n"
    "/realty — квартиры, дома, нежилые, гаражи\n"
    "После команды можно добавить условия в любом порядке:\n"
    "• тип: квартиры, дома, участки, нежилые, здания, гаражи\n"
    "• где: москва или область\n"
    "• цена: до 8 млн, от 500 тыс\n"
    "• порядок: новые или цена\n"
    "• любое слово из описания: одинцово, химки…\n"
    "Примеры:\n"
    "/realty квартиры\n"
    "/realty дома участки область до 5 млн\n"
    "/realty квартиры москва новые\n"
    "/realty одинцово\n\n"
    "<b>Авто</b>\n"
    f"/auto — легковые от {AUTO_MIN_YEAR} г.в., до {rub(MAX_PRICE)}\n"
    "Условия: год (2021), цена (до 2 млн), марка (kia), новые\n"
    "Пример: /auto kia 2021 до 2 млн\n\n"
    "<b>Другое</b>\n"
    "/analyze 3 — подробнее о лоте №3 из последнего списка\n"
    "/scan — общий поиск по всем категориям (только РАД)\n"
    "/categories — разделы площадок\n"
    "/debug — проверка чтения сайтов\n\n"
    "Поиск занимает несколько минут, ход виден в сообщении бота.\n"
    f"Утренняя рассылка: {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} МСК"
)


def cmd_auto(cid, args=()):
    q = parse_query(args, "auto")
    t0 = time.time()
    progress = Progress(cid, f"🚗 Ищу: {describe_query(q)}. "
                            "Это может занять несколько минут — ход поиска ниже.")
    lots, stats = search_auto(progress, q)
    progress("Сравниваю с объявлениями drom.ru…", force=True)
    add_drom_prices(lots)
    LAST_LOTS[cid] = lots
    progress(f"✅ Готово за {int(time.time() - t0) // 60} мин {int(time.time() - t0) % 60} с", force=True)
    send_message(cid, fmt_auto(lots, stats, q))


def cmd_realty(cid, args=()):
    q = parse_query(args, "realty")
    t0 = time.time()
    progress = Progress(cid, f"🏠 Ищу: {describe_query(q)}. "
                            "Это может занять несколько минут — ход поиска ниже.")
    lots, stats = search_realty(progress, q)
    LAST_LOTS[cid] = lots
    progress(f"✅ Готово за {int(time.time() - t0) // 60} мин {int(time.time() - t0) % 60} с", force=True)
    send_message(cid, fmt_realty(lots, stats, q))


def cmd_scan(cid, keyword):
    send_message(cid, "Сканирую РАД (lot-online.ru)… 1–2 минуты.")
    lots = fetch_rad_by_keyword(keyword) if keyword else fetch_rad_lots()
    analyzed = analyze(lots, keyword)
    for lot in analyzed[:5]:
        try:
            ma = real_market(lot["title"], lot["price"])
            if ma:
                lot["market_analysis"] = ma
        except Exception as e:
            log.error(f"Market analysis error: {e}")
    LAST_LOTS[cid] = analyzed
    msg = f"Результаты: «{esc(keyword)}»\n\n" if keyword else ""
    send_message(cid, msg + fmt_digest(analyzed, 10))


def cmd_analyze(cid, args):
    if not args or not args[0].isdigit():
        answer(cid, "Использование: /analyze 3 (номер лота из последнего списка)")
        return
    lots = LAST_LOTS.get(cid)
    if not lots:
        answer(cid, "Сначала выполните /auto, /realty или /scan.")
        return
    n = int(args[0])
    if n < 1 or n > len(lots):
        answer(cid, f"Лот #{n} не найден. Доступно: 1–{len(lots)}")
        return
    lot = lots[n - 1]
    send_message(cid, f"🔍 Анализирую лот #{n}…")
    cat = lot.get("cat_id")
    if lot.get("kind") == "auto" or cat in AUTO_CATS:
        # Для авто — только объявления drom.ru (поиск через Bing даёт случайные цены)
        if "drom" not in lot:
            add_drom_prices([lot], n=1)
        lot["market_analysis"] = {"status": "no_data"}
    elif lot.get("kind") == "realty" or cat in REALTY_CATS:
        # Для недвижимости автоматической оценки нет — сравнивайте с ЦИАН/Авито вручную
        lot["market_analysis"] = {"status": "no_data"}
    else:
        try:
            lot["market_analysis"] = real_market(lot["title"], lot["price"]) or {"status": "no_data"}
        except Exception as e:
            log.error(f"Market analysis error: {e}")
            lot["market_analysis"] = {"status": "error"}
    send_message(cid, fmt_lot_full(lot, n))

    if llm_available():
        ma = lot.get("market_analysis") or {}
        market_data = ""
        if ma.get("status") not in ("no_data", "error"):
            market_data = (f"Рыночная цена: {ma.get('avg_market')} руб., "
                           f"мин: {ma.get('min_market')} руб.")
        llm_result = llm_analyze_lot(title=lot["title"], price=lot["price"],
                                     market_data=market_data, category=lot.get("cat_name", ""))
        if llm_result:
            send_message(cid, f"<b>🤖 AI-АНАЛИЗ:</b>\n\n{esc(llm_result)}")


def cmd_debug(cid):
    """Показывает, как бот видит страницы lot-online.ru — для настройки фильтров."""
    out = ["🛠 ПРОВЕРКА ЧТЕНИЯ САЙТА\n"]
    for cat in (AUTO_CATS[:1] + REALTY_CATS[:1]):
        name = RAD_CATEGORIES.get(cat, (str(cat), 0))[0]
        log.info(f"debug: категория {cat}")
        try:
            p1 = _parse_catalog_page(_get(_category_url(cat, 1)).text, cat, name, 0)
            p2 = _parse_catalog_page(_get(_category_url(cat, 2)).text, cat, name, 0)
        except Exception as e:
            out.append(f"{name}: ошибка загрузки — {e}\n")
            continue
        overlap = len({l['url'] for l in p1} & {l['url'] for l in p2})
        out.append(f"{name}: стр.1 — {len(p1)} лотов, стр.2 — {len(p2)} (совпадений {overlap})")
        def info(l):
            if cat in AUTO_CATS:
                return f"год: {year_label(extract_years(l['title'], True)) or '—'}"
            reg, label, _ = detect_region(l['title'] + ' . ' + l['card_text'])
            return f"регион: {label or ('другой регион' if reg == 'other' else '—')}"
        for l in p1[:3]:
            out.append(f" • {_short(l['title'], 90)} | {rub(l['price'])} | {info(l)}")
        if p1:
            out.append(f" Текст карточки в каталоге: {_short(p1[0]['card_text'], 200)}")
            # Открываем лот, у которого по названию не определились год/регион — самый показательный
            probe = next((l for l in p1 if info(l).endswith("—")), p1[0])
            try:
                text, src = fetch_detail_text(probe["url"])
                focus = _focus(text, probe["title"], 3000)
                if cat in AUTO_CATS:
                    result = f"год: {year_label(detail_years(text, probe['title'])) or '—'}"
                    snip_re = r"год\w*\s+выпуска|\d{4}\s*г\.?\s?в\."
                else:
                    reg = detail_region(text, probe["title"])
                    result = f"регион: {reg[1] or reg[0] or '—'} (по: {reg[2] or '—'})"
                    snip_re = r"регион|местонахожд|местоположен|по адресу|адрес объекта"
                out.append(f" Открыт лот «{_short(probe['title'], 60)}»: текст {len(text)} симв. ({src}), "
                           f"название на странице {'найдено' if focus else 'не найдено'}; {result}")
                for m in list(re.finditer(snip_re, text, re.I))[:4]:
                    out.append(f"   …{text[max(0, m.start() - 60): m.end() + 90]}…")
            except Exception as e:
                out.append(f" Карточка лота: ошибка — {e}")
        out.append("")
    if "mets" in SOURCES:
        out.append("🏛 МЭТС (m-ets.ru)")
        checks = []
        if METS_REALTY_CATS:
            checks.append(("realty", METS_REALTY_CATS[0]))
        if METS_AUTO_CATS:
            checks.append(("auto", METS_AUTO_CATS[0]))
        for kind, cat in checks:
            name = METS_CATEGORIES.get(cat, str(cat))
            log.info(f"debug: МЭТС {name}")
            try:
                r = _get(mets_search_url(mets_filters(kind, cat, default_query(kind))))
                lots = _parse_mets_page(r.text, cat, kind)
                out.append(f"{name}: ответ {r.status_code}, {len(r.text) // 1024} КБ, лотов на стр.1: {len(lots)}")
                for l in lots[:3]:
                    extra = (f"год: {year_label(extract_years(l['title'], True) or extract_years(l['card_text'])) or '—'}"
                             if kind == "auto" else f"регион: {l['location'] or '—'}")
                    out.append(f" • {_short(l['title'], 80)} | {rub(l['price'])} | {extra} | {_short(l['status'], 40)}")
            except Exception as e:
                out.append(f"{name}: ошибка — {_short(str(e), 200)}")
        out.append("")
    for src, fn in (("alfalot", fetch_alfalot), ("cdtrf", fetch_cdtrf), ("nistp", fetch_nistp)):
        if src not in SOURCES:
            continue
        log.info(f"debug: {SRC_LABEL[src]}")
        for kind in ("realty", "auto"):
            errs = []
            try:
                lots = fn(kind, default_query(kind), errors=errs, max_pages=1)
            except Exception as e:
                errs.append(_short(str(e), 150))
                lots = []
            what = "недвижимость" if kind == "realty" else "авто"
            out.append(f"🏛 {SRC_LABEL[src]}, {what}: отобрано на стр.1 — {len(lots)}"
                       + (f"; ошибка: {errs[0]}" if errs else ""))
            for l in lots[:2]:
                extra = (l.get("region_label") or l.get("region") or "—") if kind == "realty" else \
                    (year_label(extract_years(l["title"], True) or extract_years(l["card_text"])) or "год —")
                out.append(f" • {_short(l['title'], 70)} | {rub(l['price'])} | {extra}")
        out.append("")
    log.info(f"debug: отправляю отчёт ({sum(len(x) for x in out)} симв.)")
    send_message(cid, "\n".join(out), parse_mode=None)


def handle_command(cid, text, user):
    parts = (text or "").split()
    cmd = parts[0].lower().split("@")[0] if parts else ""
    args = parts[1:]
    log.info(f"Command {cmd} from chat={cid}")

    if cmd in ("/start", "/help"):
        answer(cid, HELP_TEXT)
    elif cmd == "/categories":
        lines = ["<b>Типы недвижимости для /realty:</b>"]
        lines += [f"  {v[0]}" for v in REALTY_TYPES.values()]
        lines.append("\n<b>Разделы РАД:</b>")
        lines += [f"  {cat_id}: {esc(name)}" for cat_id, (name, _) in sorted(RAD_CATEGORIES.items())]
        land = rad_cats_by_name(r"земел|земл")
        lines.append(f"  Земельные участки на сайте: {land or 'не найдены'}")
        lines.append("\n<b>Категории МЭТС:</b>")
        lines += [f"  {cat_id}: {name}" for cat_id, name in sorted(METS_CATEGORIES.items())]
        answer(cid, "\n".join(lines))
    elif cmd == "/auto":
        run_exclusive(cid, lambda: cmd_auto(cid, args))
    elif cmd == "/realty":
        run_exclusive(cid, lambda: cmd_realty(cid, args))
    elif cmd == "/scan":
        run_exclusive(cid, lambda: cmd_scan(cid, " ".join(args) or None))
    elif cmd == "/analyze":
        run_exclusive(cid, lambda: cmd_analyze(cid, args))
    elif cmd == "/debug":
        run_exclusive(cid, lambda: cmd_debug(cid))
    else:
        answer(cid, "Неизвестная команда. /help")

# ─── Ежедневная рассылка ──────────────────────────────

def do_daily():
    log.info("Daily digest started")
    kinds = [k.strip() for k in DAILY_DIGEST.split(",") if k.strip()]
    messages = []
    try:
        if "auto" in kinds:
            q = default_query("auto")
            lots, stats = search_auto(q=q)
            add_drom_prices(lots)
            messages.append(("auto", lots, fmt_auto(lots, stats, q)))
        if "realty" in kinds:
            q = default_query("realty")
            lots, stats = search_realty(q=q)
            messages.append(("realty", lots, fmt_realty(lots, stats, q)))
    except Exception as e:
        log.error(f"Daily error: {e}", exc_info=True)
    for cid in load_chat_ids():
        send_message(cid, "☀️ Утренний дайджест торгов")
        for kind, lots, text in messages:
            send_message(cid, text)
            LAST_LOTS[cid] = lots


def scheduler():
    tz = ZoneInfo(TIMEZONE)
    last = None
    while True:
        try:
            now = datetime.now(tz)
            key = now.strftime("%Y-%m-%d")
            if now.hour == DAILY_HOUR and now.minute == DAILY_MINUTE and last != key:
                last = key
                do_daily()
        except Exception as e:
            log.error(f"Scheduler: {e}")
        time.sleep(30)

# ─── Flask ─────────────────────────────────────────────

app = Flask(__name__)


@app.route("/")
def index():
    return "Bankrot bot is running!", 200


@app.route("/health")
def health():
    return "OK", 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = flask_request.get_json(silent=True)
        msg = (data or {}).get("message")
        if not msg:
            return "OK", 200
        save_chat_id(msg["chat"]["id"])
        text = msg.get("text", "")
        if text.startswith("/"):
            threading.Thread(target=handle_command,
                             args=(msg["chat"]["id"], text, msg.get("from", {})),
                             daemon=True).start()
    except Exception as e:
        log.error(f"Webhook: {e}", exc_info=True)
    return "OK", 200

# ─── Main ──────────────────────────────────────────────

def main():
    log.info("Starting bankrot bot (lot-online.ru / RAD)...")
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set!")
        return

    if RENDER_EXTERNAL_URL:
        r = api_call("setWebhook", url=f"{RENDER_EXTERNAL_URL}/webhook")
        log.info(f"Webhook: {r}")

    api_call("setMyCommands", commands=[
        {"command": "auto", "description": f"Авто от {AUTO_MIN_YEAR} г.в."},
        {"command": "realty", "description": "Недвижимость Москва и МО"},
        {"command": "analyze", "description": "Подробнее о лоте из списка"},
        {"command": "scan", "description": "Общий поиск по всем категориям"},
        {"command": "debug", "description": "Проверка чтения сайта"},
        {"command": "help", "description": "Справка"},
    ])

    threading.Thread(target=scheduler, daemon=True).start()
    log.info(f"Daily at {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} ({TIMEZONE}): {DAILY_DIGEST}")
    log.info(f"Filters: auto>={AUTO_MIN_YEAR}, price<={MAX_PRICE}, auto={AUTO_CATS}, realty={REALTY_CATS}")

    log.info(f"Server on {PORT}...")
    app.run(host="0.0.0.0", port=PORT, debug=False)


if __name__ == "__main__":
    main()
