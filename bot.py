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
SOURCES = {x.strip().lower() for x in os.getenv("SOURCES", "rad,mets").split(",") if x.strip()}
METS_AUTO_CATS = _env_ids("METS_AUTO_CATS", "1")                 # легковой автомобиль
METS_REALTY_CATS = _env_ids("METS_REALTY_CATS", "36,34,37,39")   # квартира, жилой дом, нежилое помещение, прочие постройки
METS_REGIONS = [str(x) for x in _env_ids("METS_REGIONS", "50,77")]       # для недвижимости: Московская обл., Москва
METS_AUTO_REGIONS = [str(x) for x in _env_ids("METS_AUTO_REGIONS", "")]  # для авто: пусто — вся Россия
METS_MAX_PAGES = _env_int("METS_MAX_PAGES", 10)                  # страниц поиска МЭТС на категорию (по 20 лотов)
SRC_LABEL = {"rad": "РАД", "mets": "МЭТС"}

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
    cat_name, cat_margin = RAD_CATEGORIES.get(cat_id, (f"Категория {cat_id}", 40))
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


def mets_filters(kind, cat, sort):
    f = {}
    regions = METS_REGIONS if kind == "realty" else METS_AUTO_REGIONS
    if regions:
        f["xregion[]"] = regions
    f["search_category"] = str(cat)
    if MIN_PRICE:
        f["cena_tek_ot"] = str(MIN_PRICE)
    if MAX_PRICE:
        f["cena_tek_do"] = str(MAX_PRICE)
    if kind == "auto":
        f["autoyear_ot"] = str(AUTO_MIN_YEAR)
    if kind == "realty" and "share" in REALTY_EXCLUDE:
        f["not_dolevoe"] = "on"
    f["stat[]"] = ["0", "1", "7"]          # объявлены, приём заявок, подача ценовых предложений
    f["sortby"] = "1" if sort == "new" else "4"   # 1 — по дате публикации, 4 — сначала дешёвые
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


def fetch_mets(kind, cats, sort, progress=_noop, errors=None):
    """Лоты МЭТС по категориям: фильтры региона, цены и года задаются прямо в поиске сайта."""
    lots, seen = [], set()
    for cat in cats:
        name = METS_CATEGORIES.get(cat, f"Категория {cat}")
        for page in range(1, METS_MAX_PAGES + 1):
            try:
                r = _get(mets_search_url(mets_filters(kind, cat, sort), page))
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


def gather_all(kind, rad_cats, mets_cats, sort, progress=_noop):
    """Лоты с обеих площадок + сколько просмотрено на каждой и ошибки загрузки."""
    errors, mets_errors = [], []
    rad = gather(rad_cats, progress) if "rad" in SOURCES else []
    mets = fetch_mets(kind, mets_cats, sort, progress, mets_errors) if "mets" in SOURCES else []
    for reason in dict.fromkeys(mets_errors):
        errors.append(f"МЭТС — {reason} (категорий: {mets_errors.count(reason)} из {len(mets_cats)})")
    for l in rad:
        l["kind"] = kind
    by_src = {}
    if "rad" in SOURCES:
        by_src["РАД"] = len(rad)
    if "mets" in SOURCES:
        by_src["МЭТС"] = len(mets)
    return rad + mets, by_src, errors


def _src_label(lot):
    return SRC_LABEL.get(lot.get("src", "rad"), "")

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


def parse_sort(args):
    a = " ".join(args).lower()
    if any(w in a for w in ("нов", "дат", "свеж", "new")):
        return "new"
    if any(w in a for w in ("цен", "дешев", "дёшев", "price")):
        return "price"
    return "new" if SORT_DEFAULT.lower().startswith("new") else "price"


def order_lots(found, sort, progress=_noop):
    """Сортировка и даты публикации для показываемых лотов."""
    if sort == "new":
        # Номера лотов растут со временем, но у каждой площадки свои — берём самые свежие с каждой,
        # узнаём даты публикации и уже по ним сортируем вместе
        top, ids = [], set()
        for src in ("rad", "mets"):
            part = sorted((l for l in found if l.get("src", "rad") == src),
                          key=lambda l: (-(l.get("code") or 0), l["price"]))
            top += part[:TOP_N]
        ids = {id(l) for l in top}
        rest = sorted((l for l in found if id(l) not in ids), key=lambda l: -(l.get("code") or 0))
    else:
        found.sort(key=lambda l: l["price"])
        top, rest = found[:TOP_N], found[TOP_N:]
    for l in top:
        if not l.get("published") and l["url"] in _pub_cache:
            l["published"], l["pub_key"] = _pub_cache[l["url"]]
    need = [l for l in top if not l.get("published")]

    def work(l):
        try:
            text, _ = fetch_detail_text(l["url"])
            remember_pub(l, text)
        except Exception as e:
            log.error(f"Дата публикации {l['url']}: {e}")

    _fetch_details(need, work, progress, "даты публикации")
    if sort == "new":
        top.sort(key=lambda l: (l.get("pub_key") or (0,), l.get("code") or 0), reverse=True)
    found[:] = top + rest
    return found

# ─── Исключения для недвижимости ──────────────────────

_LOT_PREFIX = r"^(?:лот\s*№?\s*\d+\s*[,.:;-]?\s*)?"
_EXCLUDE_RULES = {
    # «Право аренды нежилого помещения…» — но не «Жилой дом и право аренды земельного участка»
    "lease": ("право аренды", re.compile(_LOT_PREFIX + r"(?:прав[оа]|уступк\w*\s+прав\w*)\s+(?:\w+\s+){0,2}аренд")),
    # «Доля в праве 1/18…», «1/2 доли в праве на квартиру», «…(1/3 доли)»
    "share": ("доли в праве", re.compile(_LOT_PREFIX + r"дол[яиюе]\b|\d+\s*/\s*\d+\s*(?:дол|доли)|"
                                          r"\bдол[яиюе]\s+(?:в\s+праве|в\s+общей|в\s+размере)")),
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


def _price_ok(l):
    return MIN_PRICE <= l["price"] <= MAX_PRICE


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


def search_auto(progress=_noop, sort="price"):
    all_lots, by_src, errors = gather_all("auto", AUTO_CATS, METS_AUTO_CATS, sort, progress)
    lots = [l for l in all_lots if _price_ok(l)]
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
    found = [l for l in lots if l["years"] and l["years"][-1] >= AUTO_MIN_YEAR]
    order_lots(found, sort, progress)
    stats = {"total": len(all_lots), "in_price": len(lots), "opened": opened,
             "not_opened": max(0, len(need) - opened),
             "no_year": sum(1 for l in lots if not l["years"]),
             "by_src": by_src, "errors": errors}
    return found, stats


def search_realty(progress=_noop, sort="price"):
    all_lots, by_src, errors = gather_all("realty", REALTY_CATS, METS_REALTY_CATS, sort, progress)
    in_price = [l for l in all_lots if _price_ok(l)]
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
            loc = (l.get("location") or "").lower()
            if "москв" in loc or "московск" in loc:
                # Город для подписи берём из названия и описания, если он там указан
                reg, label, _ = detect_region(l["card_text"])
                if "обл" in loc:
                    label = label if (reg == "msk" and label and label.startswith("Моск. обл.")) else "Московская обл."
                else:
                    label = label if label == "Москва (Зеленоград)" else "Москва"
                l["region"], l["region_label"], l["region_src"] = "msk", label, "МЭТС"
            else:
                l["region"], l["region_label"], l["region_src"] = ("other" if loc else None), None, "МЭТС"
            continue
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
    found = [l for l in lots if l.get("region") == "msk"]
    order_lots(found, sort, progress)
    stats = {"total": len(all_lots), "in_price": len(in_price), "opened": opened,
             "not_opened": max(0, len(need) - opened),
             "unknown": sum(1 for l in lots if not l.get("region")),
             "excluded": excluded, "by_src": by_src, "errors": errors}
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
    if kind == "auto" and lot.get("src") == "mets" and lot.get("location"):
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
    return line


def _sources_title():
    return " + ".join(SRC_LABEL[s] for s in ("rad", "mets") if s in SOURCES)


def fmt_auto(lots, stats, sort="price"):
    head = (f"🚗 <b>Авто от {AUTO_MIN_YEAR} г.в., до {rub(MAX_PRICE)}</b> · {_sources_title()}\n"
            f"Найдено: <b>{len(lots)}</b> · {_sort_label(sort)}\n")
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


def fmt_realty(lots, stats, sort="price"):
    head = (f"🏠 <b>Недвижимость: Москва и Московская обл., до {rub(MAX_PRICE)}</b> · {_sources_title()}\n"
            f"Найдено: <b>{len(lots)}</b> · {_sort_label(sort)}\n")
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
    "<b>Бот банкротских торгов (РАД lot-online.ru + МЭТС m-ets.ru)</b>\n\n"
    f"/auto — легковые авто от {AUTO_MIN_YEAR} г.в., до {rub(MAX_PRICE)}\n"
    f"/realty — недвижимость в Москве и МО, до {rub(MAX_PRICE)}\n"
    "/auto новые, /realty новые — сначала свежие лоты\n"
    "/auto цена, /realty цена — сначала дешёвые\n"
    "/analyze 3 — подробнее о лоте №3 из последнего списка\n"
    "/scan — общий поиск по всем категориям (только РАД)\n"
    "/scan слово — поиск по слову\n"
    "/categories — список категорий\n"
    "/debug — проверка чтения сайта\n\n"
    "Поиск занимает несколько минут, ход виден в сообщении бота.\n"
    f"Утренняя рассылка: {DAILY_HOUR:02d}:{DAILY_MINUTE:02d} МСК"
)


def cmd_auto(cid, args=()):
    sort = parse_sort(args)
    t0 = time.time()
    progress = Progress(cid, f"🚗 Ищу авто от {AUTO_MIN_YEAR} г.в. до {rub(MAX_PRICE)}. "
                            "Это может занять несколько минут — ход поиска ниже.")
    lots, stats = search_auto(progress, sort)
    progress("Сравниваю с объявлениями drom.ru…", force=True)
    add_drom_prices(lots)
    LAST_LOTS[cid] = lots
    progress(f"✅ Готово за {int(time.time() - t0) // 60} мин {int(time.time() - t0) % 60} с", force=True)
    send_message(cid, fmt_auto(lots, stats, sort))


def cmd_realty(cid, args=()):
    sort = parse_sort(args)
    t0 = time.time()
    progress = Progress(cid, f"🏠 Ищу недвижимость в Москве и МО до {rub(MAX_PRICE)}. "
                            "Это может занять несколько минут — ход поиска ниже.")
    lots, stats = search_realty(progress, sort)
    LAST_LOTS[cid] = lots
    progress(f"✅ Готово за {int(time.time() - t0) // 60} мин {int(time.time() - t0) % 60} с", force=True)
    send_message(cid, fmt_realty(lots, stats, sort))


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
                r = _get(mets_search_url(mets_filters(kind, cat, "price")))
                lots = _parse_mets_page(r.text, cat, kind)
                out.append(f"{name}: ответ {r.status_code}, {len(r.text) // 1024} КБ, лотов на стр.1: {len(lots)}")
                for l in lots[:3]:
                    extra = (f"год: {year_label(extract_years(l['title'], True) or extract_years(l['card_text'])) or '—'}"
                             if kind == "auto" else f"регион: {l['location'] or '—'}")
                    out.append(f" • {_short(l['title'], 80)} | {rub(l['price'])} | {extra} | {_short(l['status'], 40)}")
            except Exception as e:
                out.append(f"{name}: ошибка — {_short(str(e), 200)}")
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
        lines = ["<b>Категории на РАД:</b>\n"]
        lines += [f"  {cat_id}: {name}" for cat_id, (name, _) in sorted(RAD_CATEGORIES.items())]
        lines.append(f"\nАвто: {AUTO_CATS} · Недвижимость: {REALTY_CATS}")
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
            sort = parse_sort([])
            lots, stats = search_auto(sort=sort)
            add_drom_prices(lots)
            messages.append(("auto", lots, fmt_auto(lots, stats, sort)))
        if "realty" in kinds:
            sort = parse_sort([])
            lots, stats = search_realty(sort=sort)
            messages.append(("realty", lots, fmt_realty(lots, stats, sort)))
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
