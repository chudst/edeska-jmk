#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stahovani dokumentu z uredni desky Jihomoravskeho kraje
Pro automaticke spousteni pres GitHub Actions

Stahne dokumenty za vcerejsi den, nahraje na FTP, vytvori SQL soubory a logy.
"""

import os
import re
import sys
import time
import random
import ftplib
import requests
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

# ================== NASTAVENI ==================

BASE_URL = "https://eud.jmk.cz/Gordic/Ginis/App/UDE01/"
SAVE_DIR = Path(__file__).parent / "stazene_soubory" / "jihomoravsky_kraj"
LOGS_DIR = Path(__file__).parent / "logs" / "jihomoravsky_kraj"

# ================== REZIM STAHOVANI ==================
# 'yesterday' = automaticky vcerejsek (pro CRON)
# 'range'     = rucni rezim s rozmezim dat (nastavit RANGE_FROM a RANGE_TO)

MODE = 'range'

# Pro rucni rezim (MODE = 'range'):
# Format: YYYY-MM-DD
RANGE_FROM = '2026-01-30'
RANGE_TO = '2065-02-02'

# ================== VYPOCET DAT ==================

if MODE == 'yesterday':
    YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    FROM_DATE = YESTERDAY
    TO_DATE = YESTERDAY
elif MODE == 'range':
    FROM_DATE = RANGE_FROM
    TO_DATE = RANGE_TO
else:
    # Fallback na vcerejsek
    YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    FROM_DATE = YESTERDAY
    TO_DATE = YESTERDAY

# SQL soubory
SQL_IMPORT_FILE = Path(__file__).parent / "jmk_import.sql"
SQL_LOGY_FILE = Path(__file__).parent / "jmk_logy.sql"

# FTP konfigurace se cte z environment variables (GitHub Secrets)
FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH", "")
FTP_LOGS_PATH = os.environ.get("FTP_LOGS_PATH", "")  # /www/domains/chudst.cz/edeska/logs/jihomoravsky_kraj

# Pauzy mezi requesty (v sekundach)
PAUSE_MIN = 2.0
PAUSE_MAX = 4.0
PAUSE_ON_ERROR = 30

# Retry pri chybe
MAX_RETRIES = 3

# User agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# DB identifikace
DB_ZDROJ = "stahování"
DB_STRANKY = "jihomoravsky_kraj"

# ================== LOGGING ==================

LOG_LINES = []
LOG_FILE = None
HAS_ERROR = False

def init_logging():
    """Inicializace logovani."""
    global LOG_FILE
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    LOG_FILE = LOGS_DIR / f"edeska_jmk_{today}.log"

def log(msg: str, level: str = "INFO"):
    """Logovani s casem - do souboru i na stdout."""
    global HAS_ERROR
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)
    
    if level == "ERROR":
        HAS_ERROR = True
    
    # Okamzity zapis do souboru
    if LOG_FILE:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")

def get_log_text() -> str:
    """Vrati cely log jako text."""
    return "\n".join(LOG_LINES)

# ================== HELPER FUNKCE ==================

def random_pause(min_sec: float = PAUSE_MIN, max_sec: float = PAUSE_MAX):
    """Nahodna pauza."""
    pause = random.uniform(min_sec, max_sec)
    time.sleep(pause)


def get_available_filename(directory: Path, filename: str) -> str:
    """Vrati unikatni nazev souboru, pri kolizi prida _1, _2..."""
    if '.' in filename:
        base, ext = filename.rsplit('.', 1)
        ext = '.' + ext
    else:
        base = filename
        ext = ''
    
    new_name = filename
    counter = 1
    
    while (directory / new_name).exists():
        new_name = f"{base}_{counter}{ext}"
        counter += 1
    
    return new_name


def parse_date_cz(date_str: str) -> datetime:
    """Parsuje ceske datum DD.MM.YYYY na datetime."""
    return datetime.strptime(date_str, "%d.%m.%Y")


def format_date_iso(date_str: str) -> str:
    """Prevede DD.MM.YYYY na YYYY-MM-DD."""
    dt = parse_date_cz(date_str)
    return dt.strftime("%Y-%m-%d")


def capitalize_first(text: str) -> str:
    """Prvni pismeno velke, zbytek zachova."""
    if not text:
        return text
    return text[0].upper() + text[1:]


def normalize_text(text: str) -> str:
    """Normalizuje text - odstrani diakritiku, prevede na mala pismena."""
    if not text:
        return text
    normalized = unicodedata.normalize('NFD', text)
    without_diacritics = ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')
    return without_diacritics.lower()


def remove_diacritics(text: str) -> str:
    """Odstrani diakritiku z textu (zachova velikost pismen)."""
    if not text:
        return text
    normalized = unicodedata.normalize('NFD', text)
    return ''.join(c for c in normalized if unicodedata.category(c) != 'Mn')


def sanitize_filename(filename: str) -> str:
    """
    Upravi nazev souboru:
    - Odstrani diakritiku
    - Mezery nahradi podtrzitkem
    - Odstrani nepovolene znaky (ponecha pouze a-z, A-Z, 0-9, _, -, .)
    """
    if not filename:
        return filename
    
    if '.' in filename:
        name, ext = filename.rsplit('.', 1)
        ext = '.' + ext
    else:
        name = filename
        ext = ''
    
    name = remove_diacritics(name)
    ext = remove_diacritics(ext)
    name = name.replace(' ', '_')
    name = name.replace("'", "")
    name = re.sub(r'[^a-zA-Z0-9_\-]', '', name)
    
    return name + ext


def escape_sql(text: str) -> str:
    """Escapuje text pro SQL."""
    if text is None:
        return "NULL"
    escaped = text.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


# ================== SQL GENERATORS ==================

class SQLImportGenerator:
    """Generuje SQL pro import do soubory_z_jihomoravskeho_kraje."""
    
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.statements = []
    
    def add_file(self, nazev_souboru: str, datum_stazeni: str, nadpis: str):
        """Prida INSERT prikaz."""
        nadpis_norm = normalize_text(nadpis) if nadpis else None
        
        sql = f"""INSERT INTO soubory_z_jihomoravskeho_kraje (nazev_souboru, datum_stazeni, nadpis, nadpis_normalizovany)
VALUES ({escape_sql(nazev_souboru)}, {escape_sql(datum_stazeni)}, {escape_sql(nadpis)}, {escape_sql(nadpis_norm)})
ON DUPLICATE KEY UPDATE
    datum_stazeni = VALUES(datum_stazeni),
    nadpis = VALUES(nadpis),
    nadpis_normalizovany = VALUES(nadpis_normalizovany);"""
        self.statements.append(sql)
    
    def save(self) -> bool:
        """Ulozi SQL soubor."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(f"-- Import JMK: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"-- Pocet zaznamu: {len(self.statements)}\n\n")
            for stmt in self.statements:
                f.write(stmt + "\n\n")
        
        log(f"SQL import soubor vytvoren: {self.filepath} ({len(self.statements)} zaznamu)")
        return True


class SQLLogyGenerator:
    """Generuje SQL pro zapis do tabulky logy."""
    
    def __init__(self, filepath: Path):
        self.filepath = filepath
    
    def save(self, log_text: str, has_error: bool, marker_today: bool = True):
        """Ulozi SQL soubor pro logy."""
        datum = datetime.now().strftime("%Y-%m-%d")
        problem = "ano" if has_error else "ne"
        marker = f"'{datum}'" if marker_today else "NULL"
        
        sql = f"""-- Logy JMK: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

INSERT INTO logy (datum, zdroj, stranky, text, problem, marker_datum)
VALUES ('{datum}', {escape_sql(DB_ZDROJ)}, {escape_sql(DB_STRANKY)}, {escape_sql(log_text)}, '{problem}', {marker})
ON DUPLICATE KEY UPDATE
    text = CONCAT(COALESCE(text, ''), '\n', VALUES(text)),
    problem = VALUES(problem),
    marker_datum = VALUES(marker_datum);
"""
        
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(sql)
        
        log(f"SQL logy soubor vytvoren: {self.filepath}")


# ================== FTP ==================

def upload_to_ftp(local_path: Path, remote_path: str, remote_filename: str) -> bool:
    """Nahraje soubor na FTP."""
    if not all([FTP_HOST, FTP_USER, FTP_PASS, remote_path]):
        log("FTP neni nakonfigurovano, preskakuji upload", "WARN")
        return False
    
    try:
        ftp = ftplib.FTP(FTP_HOST)
        ftp.login(FTP_USER, FTP_PASS)
        ftp.cwd(remote_path)
        
        with open(local_path, 'rb') as f:
            ftp.storbinary(f'STOR {remote_filename}', f)
        
        ftp.quit()
        return True
    except Exception as e:
        log(f"FTP chyba: {e}", "ERROR")
        return False


def upload_file_to_ftp(local_path: Path, remote_filename: str) -> bool:
    """Nahraje soubor na FTP do hlavni slozky."""
    return upload_to_ftp(local_path, FTP_PATH, remote_filename)


def upload_log_to_ftp(local_path: Path, remote_filename: str) -> bool:
    """Nahraje log soubor na FTP do logs slozky."""
    if not FTP_LOGS_PATH:
        # Fallback na hlavni cestu + /logs/jihomoravsky_kraj
        logs_path = FTP_PATH.rsplit('/', 2)[0] + "/logs/jihomoravsky_kraj" if FTP_PATH else ""
    else:
        logs_path = FTP_LOGS_PATH
    return upload_to_ftp(local_path, logs_path, remote_filename)


def upload_sql_to_ftp(local_path: Path, remote_filename: str) -> bool:
    """Nahraje SQL soubor na FTP do korene edeska."""
    # SQL soubory jdou do /www/domains/chudst.cz/edeska/
    if FTP_PATH:
        base_path = FTP_PATH.rsplit('/', 2)[0]  # odstrani /stazene_soubory/jihomoravsky_kraj
    else:
        base_path = ""
    return upload_to_ftp(local_path, base_path, remote_filename)


# ================== HTTP FUNKCE ==================

class JMKScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "cs,en;q=0.5",
        })
        self.viewstate = ""
        self.viewstate_generator = ""
    
    def _extract_viewstate(self, html: str):
        """Extrahuje ViewState z HTML."""
        match = re.search(r'id="__VIEWSTATE"\s+value="([^"]*)"', html)
        if match:
            self.viewstate = match.group(1)
        
        match = re.search(r'id="__VIEWSTATEGENERATOR"\s+value="([^"]*)"', html)
        if match:
            self.viewstate_generator = match.group(1)
    
    def get_page(self, url: str, retry: int = 0) -> str | None:
        """HTTP GET s retry logikou."""
        try:
            response = self.session.get(url, timeout=60)
            
            if response.status_code == 200:
                text = response.text
                if "Service Unavailable" in text or "docasne nedostupna" in text:
                    raise Exception("Server vratil chybovou stranku (503)")
                return text
            else:
                raise Exception(f"HTTP {response.status_code}")
                
        except Exception as e:
            if retry < MAX_RETRIES:
                log(f"Chyba: {e}, cekam {PAUSE_ON_ERROR}s a zkousim znovu...", "WARN")
                time.sleep(PAUSE_ON_ERROR)
                return self.get_page(url, retry + 1)
            else:
                log(f"Chyba po {MAX_RETRIES} pokusech: {e}", "ERROR")
                return None
    
    def post_page(self, url: str, data: dict, retry: int = 0) -> str | None:
        """HTTP POST s retry logikou."""
        try:
            response = self.session.post(url, data=data, timeout=60)
            
            if response.status_code == 200:
                text = response.text
                if "Service Unavailable" in text or "docasne nedostupna" in text:
                    raise Exception("Server vratil chybovou stranku (503)")
                return text
            else:
                raise Exception(f"HTTP {response.status_code}")
                
        except Exception as e:
            if retry < MAX_RETRIES:
                log(f"Chyba: {e}, cekam {PAUSE_ON_ERROR}s a zkousim znovu...", "WARN")
                time.sleep(PAUSE_ON_ERROR)
                return self.post_page(url, data, retry + 1)
            else:
                log(f"Chyba po {MAX_RETRIES} pokusech: {e}", "ERROR")
                return None
    
    def download_file(self, url: str, save_path: Path, retry: int = 0) -> bool:
        """Stahne soubor."""
        try:
            response = self.session.get(url, timeout=120, stream=True)
            
            if response.status_code != 200:
                raise Exception(f"HTTP {response.status_code}")
            
            content_type = response.headers.get("Content-Type", "")
            if "text/html" in content_type and "Content-Disposition" not in response.headers:
                content = response.content
                if b"Service Unavailable" in content or b"docasne nedostupna" in content:
                    raise Exception("Server vratil chybovou stranku")
                content_lower = content.lower()
                if b"dokument" in content_lower and b"dispozici" in content_lower:
                    log(f"Dokument neni k dispozici", "WARN")
                    return False
            
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            return True
            
        except Exception as e:
            if retry < MAX_RETRIES:
                log(f"Chyba stahovani: {e}, cekam {PAUSE_ON_ERROR}s...", "WARN")
                time.sleep(PAUSE_ON_ERROR)
                return self.download_file(url, save_path, retry + 1)
            else:
                log(f"Chyba stahovani po {MAX_RETRIES} pokusech: {e}", "ERROR")
                return False
    
    def get_list_page(self, page: int = 1) -> str | None:
        """Nacte stranku seznamu."""
        list_url = urljoin(BASE_URL, "Seznam.aspx?a=0")
        
        if page == 1:
            html = self.get_page(list_url)
            if html:
                self._extract_viewstate(html)
            return html
        else:
            data = {
                "__EVENTTARGET": "P",
                "__EVENTARGUMENT": str(page),
                "__VIEWSTATE": self.viewstate,
                "__VIEWSTATEGENERATOR": self.viewstate_generator,
            }
            html = self.post_page(list_url, data)
            if html:
                self._extract_viewstate(html)
            return html
    
    def parse_list_page(self, html: str) -> list[dict]:
        """Parsuje seznam polozek ze stranky."""
        items = []
        
        rows = re.findall(r'<tr>\s*<td class="Priloha">.*?</tr>', html, re.DOTALL)
        
        for row in rows:
            date_match = re.search(r'<td class="VyveseniDneTd[LS]"[^>]*>(\d{2}\.\d{2}\.\d{4})</td>', row)
            if not date_match:
                continue
            date = date_match.group(1)
            
            link_match = re.search(r'<td class="NazevTd[LS]"[^>]*><a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>([^<]+)</a>', row)
            if not link_match:
                continue
            
            detail_url = link_match.group(1)
            name = link_match.group(2).strip()
            
            items.append({
                "date": date,
                "date_iso": format_date_iso(date),
                "detail_url": detail_url,
                "name": name,
            })
        
        return items
    
    def parse_detail_page(self, html: str) -> list[dict]:
        """Parsuje soubory ke stazeni z detailu."""
        files = []
        
        matches = re.findall(
            r'<a[^>]+class=["\']soubor_odkaz["\'][^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>',
            html,
            re.IGNORECASE
        )
        
        for url, filename in matches:
            files.append({
                "url": url,
                "filename": filename.strip(),
            })
        
        return files


# ================== HLAVNI LOGIKA ==================

def main():
    init_logging()
    
    from_date = datetime.strptime(FROM_DATE, "%Y-%m-%d")
    to_date = datetime.strptime(TO_DATE, "%Y-%m-%d")
    
    log("=== Stahovani z uredni desky JMK ===")
    log(f"Datum: {FROM_DATE}")
    
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Lokalni slozka: {SAVE_DIR}")
    
    # SQL generatory
    sql_import = SQLImportGenerator(SQL_IMPORT_FILE)
    sql_logy = SQLLogyGenerator(SQL_LOGY_FILE)
    
    scraper = JMKScraper()
    
    stats = {
        "pages_processed": 0,
        "items_processed": 0,
        "files_downloaded": 0,
        "files_uploaded": 0,
        "files_failed": 0,
    }
    
    page = 1
    finished = False
    
    while not finished:
        log(f"Nacitam stranku {page}...")
        
        html = scraper.get_list_page(page)
        if not html:
            log("Chyba nacitani stranky, koncim.", "ERROR")
            break
        
        items = scraper.parse_list_page(html)
        
        if not items:
            log("Zadne polozky na strance, koncim.")
            break
        
        log(f"Nalezeno {len(items)} polozek")
        stats["pages_processed"] += 1
        
        for item in items:
            item_date = parse_date_cz(item["date"])
            
            # Filtr: preskocit novejsi nez to_date
            if item_date > to_date:
                continue
            
            # Filtr: ukoncit pokud jsme starsi nez from_date
            if item_date < from_date:
                log(f"Dosazeno data {item['date']} (starsi nez {FROM_DATE}), koncim.")
                finished = True
                break
            
            log(f"-> {item['name']} ({item['date']})")
            stats["items_processed"] += 1
            
            random_pause()
            
            detail_url = urljoin(BASE_URL, item["detail_url"])
            detail_html = scraper.get_page(detail_url)
            
            if not detail_html:
                log("Chyba nacitani detailu", "ERROR")
                continue
            
            files = scraper.parse_detail_page(detail_html)
            
            if not files:
                log("(zadne soubory)")
                continue
            
            nadpis = capitalize_first(item["name"])
            
            for file_info in files:
                original_name = file_info["filename"]
                sanitized_name = sanitize_filename(original_name)
                new_name = f"{item['date_iso']}_{sanitized_name}"
                
                final_name = get_available_filename(SAVE_DIR, new_name)
                save_path = SAVE_DIR / final_name
                
                random_pause()
                
                file_url = urljoin(BASE_URL, file_info["url"])
                if scraper.download_file(file_url, save_path):
                    log(f"OK {final_name}")
                    stats["files_downloaded"] += 1
                    
                    # Upload na FTP
                    if upload_file_to_ftp(save_path, final_name):
                        log(f"FTP OK")
                        stats["files_uploaded"] += 1
                    else:
                        log(f"FTP CHYBA", "ERROR")
                    
                    # Pridat do SQL importu
                    datum_stazeni = f"{item['date_iso']} 00:00:00"
                    sql_import.add_file(final_name, datum_stazeni, nadpis)
                else:
                    log(f"CHYBA {original_name}", "ERROR")
                    stats["files_failed"] += 1
        
        if not finished:
            page += 1
            random_pause(PAUSE_MIN, PAUSE_MAX)
    
    # Shrnuti
    log("=== HOTOVO ===")
    log(f"Zpracovano stranek: {stats['pages_processed']}")
    log(f"Zpracovano polozek: {stats['items_processed']}")
    log(f"Stazeno souboru: {stats['files_downloaded']}")
    log(f"Nahrano na FTP: {stats['files_uploaded']}")
    log(f"Selhalo: {stats['files_failed']}")
    
    # Ulozit SQL soubory
    if sql_import.statements:
        sql_import.save()
        upload_sql_to_ftp(SQL_IMPORT_FILE, "jmk_import.sql")
    else:
        log("Zadne soubory ke stazeni, SQL import nevytvoren.")
    
    # Ulozit logy SQL
    sql_logy.save(get_log_text(), HAS_ERROR, marker_today=True)
    upload_sql_to_ftp(SQL_LOGY_FILE, "jmk_logy.sql")
    
    # Upload log souboru
    if LOG_FILE and LOG_FILE.exists():
        upload_log_to_ftp(LOG_FILE, LOG_FILE.name)
    
    # Navratovy kod
    if HAS_ERROR:
        log("Skript skoncil s chybami.", "ERROR")
        sys.exit(1)
    else:
        log("Skript skoncil uspesne.")
        sys.exit(0)


if __name__ == "__main__":
    main()
