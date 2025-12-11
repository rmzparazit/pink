import os
import json
import time
import hashlib
import re
import shutil
import requests
from datetime import datetime
from urllib.parse import quote
from playwright.sync_api import sync_playwright

# --- НАСТРОЙКИ ---
OUTPUT_DIR = "output_pinkypunk"
os.makedirs(OUTPUT_DIR, exist_ok=True)
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
BASE_URL = "https://pinkypunk.ru/catalog"
GITHUB_USER = "rmzparazit"
REPO_NAME = "pink"
BRANCH = "main"

# Пути к файлам
PROGRESS_FILE = os.path.join(OUTPUT_DIR, "progress.json")
XML_FILE = os.path.join(OUTPUT_DIR, "pinkypunk_catalog.xml")
TEMP_XML_FILE = XML_FILE + ".tmp"

# Настройка логирования
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.info

def get_custom_image_url(vendor_code):
    """
    Возвращает ссылку на кастомное изображение в GitHub, если оно существует.
    """
    if not vendor_code:
        return None
    
    image_path = f"images/{vendor_code}.png"
    raw_url = f"https://raw.githubusercontent.com/{GITHUB_USER}/{REPO_NAME}/{BRANCH}/{image_path}"
    
    try:
        response = requests.head(raw_url, timeout=3)
        if response.status_code == 200:
            log(f"✅ Найдено кастомное изображение для {vendor_code}")
            return raw_url
    except requests.exceptions.RequestException as e:
        log(f"⚠️ Ошибка проверки кастомного изображения для {vendor_code}: {e}")

    return None

def normalize_collection_id(val):
    """Нормализует ID коллекции."""
    if not val:
        return ""
    s_val = str(val)
    if 'e' in s_val.lower():
        try:
            return str(int(float(s_val)))
        except:
            pass
    if isinstance(val, (int, float)):
        return str(int(val))
    if '.' in s_val and s_val.replace('.', '').isdigit():
        try:
            return str(int(float(s_val)))
        except:
            pass
    return s_val

def load_progress():
    """Загружает прогресс парсинга из файла."""
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                for p in data.get("products", []):
                    if p.get('collection_id'):
                        p['collection_id'] = normalize_collection_id(p['collection_id'])
                return data
        except Exception as e:
            log(f"❌ Ошибка загрузки прогресса: {e}")
    return {"products": []}

def save_progress(products):
    """Сохраняет прогресс парсинга в файл."""
    try:
        unique_products = []
        seen_vendors = set()
        for p in products:
            vendor_code = p.get('vendorCode')
            if vendor_code and vendor_code not in seen_vendors:
                if p.get('collection_id'):
                    p['collection_id'] = normalize_collection_id(p['collection_id'])
                unique_products.append(p)
                seen_vendors.add(vendor_code)
        
        with open(PROGRESS_FILE, 'w', encoding='utf-8') as f:
            json.dump({"products": unique_products}, f, ensure_ascii=False, indent=4)
        log(f"✅ Прогресс сохранён: {len(unique_products)} товаров")
    except Exception as e:
        log(f"❌ Ошибка сохранения: {e}")

def extract_collections(page):
    """Извлекает список коллекций с сайта."""
    collections = []
    try:
        page.wait_for_selector('.js-store-parts-switcher', timeout=5000)
        switchers = page.query_selector_all('.js-store-parts-switcher:not(.t-store__parts-switch-btn-all)')
        
        for switcher in switchers:
            name = switcher.inner_text().strip()
            uid = switcher.get_attribute('data-storepart-uid') or ""
            link_fragment = switcher.get_attribute('data-storepart-link') or ""
            
            if '/c/' in link_fragment:
                try:
                    uid_candidate = link_fragment.split('/c/')[1].split('-')[0]
                    if uid_candidate.isdigit():
                        uid = uid_candidate
                except: pass

            final_id = normalize_collection_id(uid)
            
            if name and name != "Все":
                full_url = f"https://pinkypunk.ru/catalog"
                if '/c/' in link_fragment:
                    try:
                        encoded_name = quote(link_fragment.split('-')[-1])
                        full_url = f"https://pinkypunk.ru/catalog?tfc_storepartuid%5B757983339%5D={encoded_name}&tfc_div=:::"
                    except: pass
                
                collections.append({'id': final_id, 'name': name, 'url': full_url.strip()})
                if final_id.isdigit():
                    log(f"🏷️ Найдена коллекция: {name} -> ID: {final_id}")
                else:
                    log(f"⚠️ Коллекция '{name}': ID не найден, будет восстановлен из товаров")
                    
    except Exception as e:
        log(f"⚠️ Ошибка при извлечении коллекций: {e}")
    return collections

def parse_catalog_page(page):
    """Парсит каталог и возвращает список товаров и коллекций."""
    log("📦 Начинаем парсинг каталога...")
    all_products = []
    
    page.goto(BASE_URL, timeout=60000)
    page.wait_for_timeout(5000)
    collections = extract_collections(page)
    
    last_height = page.evaluate("document.body.scrollHeight")
    for _ in range(15):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(2)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
        
    product_elements = page.query_selector_all('.js-product.t-store__card')
    log(f"🔍 Найдено {len(product_elements)} карточек товаров на странице.")
    
    for card in product_elements:
        try:
            buy_button = card.query_selector('.js-store-prod-btn2')
            if not buy_button or "t-store__prod-popup__btn_disabled" in (buy_button.get_attribute("class") or ""):
                continue
            
            name_el = card.query_selector('.js-store-prod-name')
            name = name_el.inner_text().strip() if name_el else ""
            if not name: continue
            
            sku_el = card.query_selector('.js-store-prod-sku')
            vendorCode = sku_el.inner_text().replace('Артикул:', '').strip() if sku_el else ""
            
            link_el = card.query_selector('a[href]')
            link = link_el.get_attribute('href').strip() if link_el else ""
            
            price_el = card.query_selector('.js-product-price')
            price = "0"
            if price_el:
                price = price_el.get_attribute('data-product-price-def') or re.search(r'\d+', price_el.inner_text().replace(' ',''))[0]

            img_el = card.query_selector('.js-product-img')
            image = (img_el.get_attribute('data-original') if img_el else "").strip()

            descr_el = card.query_selector('.js-store-prod-descr')
            description = descr_el.inner_text().strip() if descr_el else ""

            collection_id = normalize_collection_id(card.get_attribute('data-product-part-uid'))

            all_products.append({
                'name': name, 'vendorCode': vendorCode, 'link': link, 'price': price,
                'image': image, 'collection_id': collection_id, 'description': description, 'additional_images': []
            })
        except Exception:
            continue
            
    return all_products, collections

def build_collection_image_info(products):
    """Создает маппинг: ID коллекции -> {артикул, картинка-фолбэк}."""
    collection_info = {}
    for prod in products:
        coll_id = prod.get('collection_id')
        if coll_id and coll_id.isdigit() and coll_id not in collection_info:
            vendor_code = prod.get('vendorCode')
            fallback_image = prod.get('image', '')
            if vendor_code:
                collection_info[coll_id] = {
                    'vendor_code': vendor_code,
                    'fallback_image': fallback_image
                }
    return collection_info

def clean_text_for_xml(text):
    """Экранирует только критические символы для XML, оставляя кавычки как есть."""
    if not text:
        return ""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    # НЕ заменяем кавычки: text = text.replace('"', '&quot;')
    return text

def generate_xml(products, collections):
    """Генерирует XML-фид вручную, блок collections в конце."""
    log("📝 Генерация XML-фида...")
    current_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    active_collection_ids = set()
    for prod in products:
        cid = prod.get('collection_id')
        if cid and cid.isdigit():
            active_collection_ids.add(cid)
            
    collection_image_info = build_collection_image_info(products)
    
    final_collections = {}
    for c in collections:
        cid = str(c.get('id', ''))
        if cid and cid.isdigit() and cid in active_collection_ids:
            final_collections[cid] = c

    for cid in active_collection_ids:
        if cid not in final_collections:
            final_collections[cid] = {'id': cid, 'name': f"Коллекция {cid}", 'url': BASE_URL}

    xml_lines = []
    xml_lines.append('<?xml version="1.0" encoding="UTF-8"?>')
    xml_lines.append(f'<yml_catalog date="{current_date}">')
    xml_lines.append('  <shop>')
    
    xml_lines.append('    <name>Секспедиция</name>')
    xml_lines.append('    <company>Секспедиция</company>')
    xml_lines.append('    <url>https://pinkypunk.ru</url>')
    xml_lines.append('    <platform>Tilda</platform>')
    xml_lines.append('    <version>1.0</version>')
    
    xml_lines.append('    <currencies>')
    xml_lines.append('      <currency id="RUB" rate="1"/>')
    xml_lines.append('    </currencies>')
    
    xml_lines.append('    <categories>')
    xml_lines.append('      <category id="1">Секс-игрушки</category>')
    xml_lines.append('    </categories>')
    
    # 1. Offers
    xml_lines.append('    <offers>')
    
    for prod in products:
        vendor_code = prod.get('vendorCode')
        if not vendor_code: continue
        
        xml_lines.append(f'      <offer id="{vendor_code}" available="true">')
        xml_lines.append(f'        <name>{clean_text_for_xml(prod["name"])}</name>')
        xml_lines.append('        <vendor>Секспедиция</vendor>')
        xml_lines.append(f'        <vendorCode>{vendor_code}</vendorCode>')
        xml_lines.append(f'        <price>{prod["price"]}</price>')
        xml_lines.append('        <currencyId>RUB</currencyId>')
        xml_lines.append('        <categoryId>1</categoryId>')
        
        custom_image = get_custom_image_url(vendor_code)
        pic_url = custom_image if custom_image else prod.get("image")
        if pic_url:
            xml_lines.append(f'        <picture>{pic_url}</picture>')
            
        cid = prod.get("collection_id", "")
        if cid and cid.isdigit() and cid in active_collection_ids:
             xml_lines.append(f'        <collectionId>{cid}</collectionId>')
             
        xml_lines.append(f'        <url><![CDATA[{prod["link"]}]]></url>')
        
        if prod.get("description"):
             xml_lines.append(f'        <description><![CDATA[{prod["description"]}]]></description>')
             
        xml_lines.append('        <sales_notes>Официальный сайт Секспедиция.</sales_notes>')
        xml_lines.append(f'        <custom_label_0>{clean_text_for_xml(prod["name"])}</custom_label_0>')
        
        xml_lines.append('      </offer>')

    xml_lines.append('    </offers>')
    
    # 2. Collections (с тегом picture вместо image)
    if final_collections:
        xml_lines.append('    <collections>')
        for coll_id, coll_data in sorted(final_collections.items()):
            image_url = ""
            if coll_id in collection_image_info:
                info = collection_image_info[coll_id]
                image_url = get_custom_image_url(info['vendor_code']) or info['fallback_image']
            
            xml_lines.append(f'      <collection id="{coll_id}">')
            xml_lines.append(f'        <name>{clean_text_for_xml(coll_data["name"])}</name>')
            xml_lines.append(f'        <url><![CDATA[{coll_data["url"]}]]></url>')
            if image_url:
                xml_lines.append(f'        <picture>{image_url}</picture>')
            xml_lines.append('      </collection>')
        xml_lines.append('    </collections>')

    xml_lines.append('  </shop>')
    xml_lines.append('</yml_catalog>')

    try:
        with open(TEMP_XML_FILE, "w", encoding="utf-8") as f:
            f.write('\n'.join(xml_lines))
            
        if os.path.exists(XML_FILE):
            shutil.copy2(XML_FILE, XML_FILE + ".backup")
        os.replace(TEMP_XML_FILE, XML_FILE)
        log(f"✅ XML сохранён: {XML_FILE}")
    except Exception as e:
        log(f"❌ Ошибка сохранения XML: {e}")

# --- ОСНОВНОЙ ЗАПУСК ---
if __name__ == "__main__":
    log("🚀 Запуск парсера pinkypunk.ru (v8 - picture in collections)")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()
        
        try:
            current_products, collections = parse_catalog_page(page)
            
            progress = load_progress()
            products_map = {p['vendorCode']: p for p in progress.get("products", [])}
            
            for prod in current_products:
                products_map[prod['vendorCode']] = prod
            
            final_products = list(products_map.values())
            save_progress(final_products)
            generate_xml(final_products, collections)
            
            log(f"🎉 Готово! Всего товаров в фиде: {len(final_products)}")
            
        except Exception as e:
            log(f"❌ Критическая ошибка: {e}")
        finally:
            browser.close()
            log("✅ Браузер закрыт.")
