import os
import json
import time
import random
import hashlib
import re
import shutil
from datetime import datetime
from urllib.parse import quote
from playwright.sync_api import sync_playwright

# --- НАСТРОЙКИ ---
OUTPUT_DIR = "output_pinkypunk"
os.makedirs(OUTPUT_DIR, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE_URL = "https://pinkypunk.ru/catalog"

# Пути к файлам (Теперь XML!)
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.json")
XML_FILE = os.path.join(OUTPUT_DIR, "pinkypunk_catalog.xml")  # Изменено на .xml
TEMP_XML_FILE = XML_FILE + ".tmp"

# Настройка логирования
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.info


def load_progress():
    """Загружает прогресс парсинга из файла."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            log(f"❌ Ошибка загрузки прогресса: {e}")
    return {"products": []}


def save_progress(products):
    """Сохраняет прогресс парсинга в файл."""
    try:
        clean = [p for p in products if p.get('vendorCode') and p.get('name') and p.get('link')]
        seen = set()
        unique = []
        for p in clean:
            link = p['link'].strip().split('#')[0]
            if not link or '#order' in link or link.endswith('/'):
                continue
            if link not in seen:
                seen.add(link)
                unique.append(p)
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"products": unique}, f, ensure_ascii=False, indent=4)
        log(f"✅ Прогресс сохранён: {len(unique)} товаров")
    except Exception as e:
        log(f"❌ Ошибка сохранения: {e}")


def extract_collections(page):
    """Извлекает список коллекций (категорий) с их ID, названиями и ссылками."""
    collections = []
    try:
        # Находим все кнопки-переключатели коллекций, кроме "Все"
        switchers = page.query_selector_all('.js-store-parts-switcher:not(.t-store__parts-switch-btn-all)')
        for switcher in switchers:
            name = switcher.inner_text().strip()
            uid = switcher.get_attribute('data-storepart-uid') or ""
            link_fragment = switcher.get_attribute('data-storepart-link') or ""
            if name and uid and name != "Все":
                # Конвертируем fragment в реальный URL
                # Пример: "#!/tstore/r/757983339/c/435183164492-Мастурбаторы"
                # -> https://pinkypunk.ru/catalog?tfc_storepartuid%5B757983339%5D=Мастурбаторы&tfc_div=:::
                if '/c/' in link_fragment:
                    # Извлекаем название коллекции после UID
                    collection_name_in_url = link_fragment.split('-')[-1] if '-' in link_fragment else name
                    # URL-кодируем название
                    encoded_name = quote(collection_name_in_url)
                    full_url = f"https://pinkypunk.ru/catalog?tfc_storepartuid%5B757983339%5D={encoded_name}&tfc_div=:::"
                else:
                    full_url = f"https://pinkypunk.ru/catalog"

                collections.append({
                    'id': uid,
                    'name': name,
                    'url': full_url.strip()
                })
        log(f"📚 Найдено {len(collections)} коллекций: {[c['name'] for c in collections]}")
    except Exception as e:
        log(f"⚠️ Ошибка при извлечении коллекций: {e}")
    return collections


def parse_catalog_page(page):
    """Парсит главную страницу каталога и собирает список всех товаров, пропуская отсутствующие в наличии."""
    log("📦 Начинаем парсинг каталога...")
    all_products = []
    collections = []

    try:
        page.goto(BASE_URL, timeout=60000)
        page.wait_for_timeout(5000)

        # --- Извлекаем коллекции ---
        collections = extract_collections(page)

        # Прокручиваем страницу вниз, чтобы подгрузить все товары
        last_height = page.evaluate("document.body.scrollHeight")
        while True:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
            time.sleep(3)
            new_height = page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height

        # Ищем все карточки товаров
        product_elements = page.query_selector_all('.js-product.t-store__card')
        log(f"🔍 Найдено {len(product_elements)} карточек товаров на странице.")

        for card in product_elements:
            try:
                # --- Проверка наличия ---
                buy_button = card.query_selector('.js-store-prod-btn2')
                if not buy_button:
                    continue
                is_disabled = "t-store__prod-popup__btn_disabled" in (buy_button.get_attribute("class") or "")
                button_text = buy_button.inner_text().strip() if buy_button else ""
                if is_disabled or button_text == "Нет в наличии":
                    continue

                # --- Название ---
                name_el = card.query_selector('.js-store-prod-name')
                name = name_el.inner_text().strip() if name_el else ""
                if not name:
                    continue

                # --- Артикул ---
                sku_el = card.query_selector('.js-store-prod-sku')
                vendorCode = sku_el.inner_text().replace('Артикул:', '').strip() if sku_el else ""
                if not vendorCode:
                    hash_input = f"{name}_{int(time.time())}"
                    vendorCode = f"PP_{hashlib.md5(hash_input.encode()).hexdigest()[:8].upper()}"

                # --- Ссылка ---
                link_el = card.query_selector('a[href]:not([href="#order"])')
                link = link_el.get_attribute('href').strip() if link_el else ""
                if not link or link == BASE_URL or link.endswith('#') or '#order' in link:
                    continue

                # --- Цена ---
                price_el = card.query_selector('.js-product-price')
                price = "0"
                if price_el:
                    data_price = price_el.get_attribute('data-product-price-def')
                    if data_price and data_price != '0':
                        price = data_price
                    else:
                        text = price_el.inner_text().replace(' ', '').replace('р.', '').replace('₽', '')
                        match = re.search(r'\d+', text)
                        if match:
                            price = match.group(0)

                # --- Основное изображение ---
                img_el = card.query_selector('.js-product-img')
                image = ""
                if img_el:
                    image = img_el.get_attribute('data-original') or img_el.get_attribute('src') or ""
                    image = image.strip()

                # --- Дополнительные изображения ---
                additional_images = []
                second_img = card.query_selector('.t-store__card__bgimg_second')
                if second_img:
                    second_src = second_img.get_attribute('data-original') or second_img.get_attribute('src') or ""
                    second_src = second_src.strip()
                    if second_src and second_src != image:
                        additional_images.append(second_src)

                # --- Описание ---
                descr_el = card.query_selector('.js-store-prod-descr')
                description = descr_el.inner_text().strip() if descr_el else ""

                # --- Определяем коллекцию товара ---
                part_uid = card.get_attribute('data-product-part-uid') or ""
                collection_id = ""
                if part_uid:
                    first_uid = part_uid.split(',')[0].strip()
                    collection_id = first_uid

                # --- Создаем объект товара ---
                product_data = {
                    'name': name,
                    'vendorCode': vendorCode,
                    'link': link,
                    'image': image,
                    'additional_images': additional_images,
                    'price': price,
                    'description': description,
                    'collection_id': collection_id,
                    'collection': 'default'
                }

                all_products.append(product_data)
                log(f"✅ Добавлен товар: {name} | Арт: {vendorCode} | Цена: {price} ₽")

            except Exception as e:
                log(f"⚠️ Ошибка при парсинге одной карточки: {e}")
                continue

        log(f"📦 Всего успешно извлечено {len(all_products)} товаров (в наличии).")

    except Exception as e:
        log(f"❌ Ошибка при парсинге каталога: {e}")

    return all_products, collections


def generate_xml(products, collections):
    """Генерирует XML-фид на основе собранных данных о товарах."""
    log("📝 Генерация XML-фида...")
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M")

    header_lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        f'<yml_catalog date="{current_date}">',
        '  <shop>',
        '    <name>Секспедиция</name>',
        '    <company>Секспедиция</company>',
        '    <url>https://pinkypunk.ru</url>',
        '    <platform>Tilda</platform>',
        '    <currencies>',
        '      <currency id="RUB" rate="1"/>',
        '    </currencies>',
        '    <categories>',
        '      <category id="1">Секс-игрушки</category>',
        '    </categories>',
        '    <offers>'
    ]

    footer_lines = [
        '    </offers>',
        '    <collections>',  # Добавлено
    ]

    # Добавляем коллекции БЕЗ categoryId
    for coll in collections:
        footer_lines.append(f'      <collection id="{coll["id"]}">')
        footer_lines.append(f'        <name>{coll["name"]}</name>')
        if coll.get('url'):
            url_cdata = f"<![CDATA[{coll['url'].strip()}]]>"
            footer_lines.append(f'        <url>{url_cdata}</url>')
        # 🔥 <categoryId> УДАЛЁН, так как вызывает ошибки валидации
        footer_lines.append('      </collection>')

    footer_lines.extend([
        '    </collections>',  # Закрываем collections
        '  </shop>',
        '</yml_catalog>'
    ])

    offer_lines = []
    used_ids = set()

    for prod in products:
        try:
            if not prod.get('vendorCode') or not prod.get('name'):
                continue

            unique_id = prod["vendorCode"]
            if unique_id in used_ids:
                continue
            used_ids.add(unique_id)

            offer = [
                f'      <offer id="{unique_id}" available="true">',
                f'        <name>{prod["name"]}</name>',
                f'        <vendor>Секспедиция</vendor>',
                f'        <vendorCode>{prod["vendorCode"]}</vendorCode>',
                f'        <model>{prod["name"]}</model>',
                f'        <price>{prod["price"]}</price>',
                f'        <currencyId>RUB</currencyId>',
                f'        <categoryId>1</categoryId>'
            ]

            # Добавляем collectionId, если есть
            if prod.get('collection_id'):
                offer.append(f'        <collectionId>{prod["collection_id"]}</collectionId>')

            # URL
            url_cdata = f"<![CDATA[{prod['link'].strip()}]]>"
            offer.append(f'        <url>{url_cdata}</url>')

            # Изображения
            if prod.get('image'):
                offer.append(f'        <picture>{prod["image"].strip()}</picture>')
            for img in prod.get('additional_images', []):
                if img and img != prod.get('image'):
                    offer.append(f'        <picture>{img.strip()}</picture>')

            # Описание
            if prod.get('description') and prod['description'].strip():
                desc_cdata = f"<![CDATA[{prod['description'].strip()}]]>"
                offer.append(f'        <description>{desc_cdata}</description>')

            # sales_notes
            sales_notes = "Официальный сайт Секспедиция."
            offer.append(f'        <sales_notes>{sales_notes}</sales_notes>')

            offer.append('      </offer>')
            offer_lines.extend(offer)

        except Exception as e:
            log(f"❌ Ошибка при генерации offer для {prod.get('name', 'unknown')}: {e}")
            continue

    # Формирование финального фида
    full_lines = header_lines + offer_lines + footer_lines

    # Резервная копия
    if os.path.exists(XML_FILE):
        backup_name = XML_FILE + ".backup"
        shutil.copy2(XML_FILE, backup_name)
        log(f"📁 Создана резервная копия: {backup_name}")

    # Атомарная запись
    try:
        with open(TEMP_XML_FILE, 'w', encoding='utf-8') as f:
            f.write('\n'.join(full_lines))
        os.replace(TEMP_XML_FILE, XML_FILE)
        log(f"✅ XML-фид успешно сохранён: {XML_FILE}")
    except Exception as e:
        log(f"❌ Ошибка при сохранении фида: {e}")


# --- ЗАПУСК СКРИПТА ---
if __name__ == "__main__":
    log("🚀 Запуск парсера pinkypunk.ru")
    progress = load_progress()
    all_products = progress["products"]
    seen_links = {p['link'].strip() for p in all_products}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        try:
            # Парсим каталог и коллекции
            product_list, collections = parse_catalog_page(page)
            new_items = [item for item in product_list if item['link'].strip() not in seen_links]
            log(f"🆕 Новых товаров для парсинга: {len(new_items)}")

            # Просто добавляем новые товары
            for item in new_items:
                all_products.append(item)
                save_progress(all_products)

            log(f"🎉 Собрано {len(new_items)} новых товаров из каталога.")

            # Генерируем фид
            generate_xml(all_products, collections)
            log(f"🎉 Готово! Всего товаров в фиде: {len(all_products)}")

        except Exception as e:
            log(f"❌ Ошибка: {e}")
            save_progress(all_products)
        finally:
            browser.close()
            log("✅ Браузер закрыт.")

    log("✅ Готово! Проверьте папку 'output_pinkypunk'.")
