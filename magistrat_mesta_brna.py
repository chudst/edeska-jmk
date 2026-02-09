#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stahovani dokumentu z uredni desky Magistratu mesta Brna
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
from html import unescape

# ================== NASTAVENI ==================

BASE_URL = "https://edeska.brno.cz/eDeska/"
LIST_URL = BASE_URL + "eDeskaAktualni.jsp"
DOWNLOAD_URL = BASE_URL + "download.jsp?idPriloha="

SAVE_DIR = Path(__file__).parent / "stazene_soubory" / "magistrat_mesta_brna"
LOGS_DIR = Path(__file__).parent / "logs" / "magistrat_mesta_brna"

# ================== REZIM STAHOVANI ==================
# 'yesterday' = automaticky vcerejsek (pro CRON)
# 'range'     = rucni rezim s rozmezim dat (nastavit RANGE_FROM a RANGE_TO)

MODE = 'yesterday'

# Pro rucni rezim (MODE = 'range'):
# Format: YYYY-MM-DD
RANGE_FROM = '2026-02-08'
RANGE_TO = '2026-02-08'

# ================== VYPOCET DAT ==================

if MODE == 'yesterday':
    YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    FROM_DATE = YESTERDAY
    TO_DATE = YESTERDAY
elif MODE == 'range':
    FROM_DATE = RANGE_FROM
    TO_DATE = RANGE_TO
else:
    YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    FROM_DATE = YESTERDAY
    TO_DATE = YESTERDAY

# SQL soubory
SQL_IMPORT_FILE = Path(__file__).parent / "soubory_z_magistratu_mesta_brna.sql"
SQL_LOGY_FILE = Path(__file__).parent / "soubory_z_magistratu_mesta_brna_logy.sql"

# FTP konfigurace se cte z environment variables (GitHub Secrets)
FTP_HOST = os.environ.get("FTP_HOST", "")
FTP_USER = os.environ.get("FTP_USER", "")
FTP_PASS = os.environ.get("FTP_PASS", "")
FTP_PATH = os.environ.get("FTP_PATH_BRNO", "")  # /www/domains/chudst.cz/edeska/stazene_soubory/magistrat_mesta_brna
FTP_LOGS_PATH = os.environ.get("FTP_LOGS_PATH_BRNO", "")  # /www/domains/chudst.cz/edeska/logs/magistrat_mesta_brna

# Pauzy mezi requesty (v sekundach)
PAUSE_MIN = 2.0
PAUSE_MAX = 4.0
PAUSE_ON_ERROR = 30

# Retry pri chybe
MAX_RETRIES = 3

# Pocet zaznamu na stranku
PAGE_SIZE = 25

# User agent
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

# DB identifikace
DB_ZDROJ = "stahování"
DB_STRANKY = "magistrat_mesta_brna"

# ================== LOGGING ==================

LOG_LINES = []
LOG_FILE = None
HAS_ERROR = False

def init_logging():
    """Inicializace logovani."""
    global LOG_FILE
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    LOG_FILE = LOGS_DIR / f"edeska_brno_{today}.log"

def log(msg: str, level: str = "INFO"):
    """Logovani s casem - do souboru i na stdout."""
    global HAS_ERROR
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}"
    LOG_LINES.append(line)
    print(line, flush=True)
    
    if level == "ERROR":
        HAS_ERROR = True
    
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
    return datetime.strptime(date_str.strip(), "%d.%m.%Y")


def format_date_iso(date_str: str) -> str:
    """Prevede DD.MM.YYYY na YYYY-MM-DD."""
    dt = parse_date_cz(date_str)
    return dt.strftime("%Y-%m-%d")


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


def clean_html_text(text: str) -> str:
    """Vycisti HTML text - unescape entities, odstrani tagy, trim."""
    if not text:
        return ""
    # Unescape HTML entities
    text = unescape(text)
    # Odstran tagy
    text = re.sub(r'<[^>]+>', ' ', text)
    # Nahrad vicenasobne mezery/newliny jednou mezerou
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def escape_sql(text: str) -> str:
    """Escapuje text pro SQL."""
    if text is None:
        return "NULL"
    escaped = text.replace("\\", "\\\\").replace("'", "''")
    return f"'{escaped}'"


# ================== SQL GENERATORS ==================

class SQLImportGenerator:
    """Generuje SQL pro import do soubory_magistrat_mesta_brna."""
    
    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.statements = []
    
    def add_file(self, detail_id: int, oblast: str, kategorie: str, nazev: str,
                 cislo_jednaci: str, puvodce: str, zverejnit_od: str, zverejnit_do: str,
                 nazev_souboru: str, datum_stazeni: str):
        """Prida INSERT prikaz."""
        nazev_norm = normalize_text(nazev) if nazev else None
        
        sql = f"""INSERT INTO soubory_magistrat_mesta_brna (detail_id, oblast, kategorie, nazev, cislo_jednaci, puvodce, zverejnit_od, zverejnit_do, nazev_souboru, datum_stazeni, nazev_normalizovany)
VALUES ({detail_id}, {escape_sql(oblast)}, {escape_sql(kategorie)}, {escape_sql(nazev)}, {escape_sql(cislo_jednaci)}, {escape_sql(puvodce)}, {escape_sql(zverejnit_od)}, {escape_sql(zverejnit_do)}, {escape_sql(nazev_souboru)}, {escape_sql(datum_stazeni)}, {escape_sql(nazev_norm)})
ON DUPLICATE KEY UPDATE
    oblast = VALUES(oblast),
    kategorie = VALUES(kategorie),
    nazev = VALUES(nazev),
    cislo_jednaci = VALUES(cislo_jednaci),
    puvodce = VALUES(puvodce),
    zverejnit_od = VALUES(zverejnit_od),
    zverejnit_do = VALUES(zverejnit_do),
    datum_stazeni = VALUES(datum_stazeni),
    nazev_normalizovany = VALUES(nazev_normalizovany);"""
        self.statements.append(sql)
    
    def save(self) -> bool:
        """Ulozi SQL soubor."""
        with open(self.filepath, "w", encoding="utf-8") as f:
            f.write(f"-- Import Brno: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
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
        
        sql = f"""-- Logy Brno: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

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
        logs_path = FTP_PATH.rsplit('/', 2)[0] + "/logs/magistrat_mesta_brna" if FTP_PATH else ""
    else:
        logs_path = FTP_LOGS_PATH
    return upload_to_ftp(local_path, logs_path, remote_filename)


def upload_sql_to_ftp(local_path: Path, remote_filename: str) -> bool:
    """Nahraje SQL soubor na FTP do korene edeska."""
    if FTP_PATH:
        base_path = FTP_PATH.rsplit('/', 2)[0]
    else:
        base_path = ""
    return upload_to_ftp(local_path, base_path, remote_filename)


# ================== HTTP FUNKCE ==================

class BrnoScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "cs,en;q=0.5",
        })
    
    def get_page(self, url: str, retry: int = 0) -> str | None:
        """HTTP GET s retry logikou."""
        try:
            response = self.session.get(url, timeout=60)
            if response.status_code == 200:
                return response.text
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
                return response.text
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
    
    def get_list_page(self, first: int = 0) -> str | None:
        """Nacte stranku seznamu. first = offset (0, 25, 50, ...)."""
        if first == 0:
            # Prvni stranka - GET
            html = self.get_page(LIST_URL)
            return html
        else:
            # Dalsi stranky - POST
            data = {
                "order": "vyveseno",
                "desc": "true",
                "first": str(first),
                "count": str(PAGE_SIZE),
            }
            html = self.post_page(LIST_URL, data)
            return html
    
    def parse_total_count(self, html: str) -> int:
        """Zjisti celkovy pocet zaznamu z textu '1 - 25 z 663'."""
        match = re.search(r'(\d+)\s*-\s*\d+\s+z\s+(\d+)', html)
        if match:
            return int(match.group(2))
        return 0
    
    def parse_list_page(self, html: str) -> list[dict]:
        """Parsuje seznam polozek ze stranky."""
        items = []
        
        # Najdi tbody s daty
        tbody_match = re.search(r'<tbody>(.*?)</tbody>', html, re.DOTALL)
        if not tbody_match:
            return items
        
        tbody = tbody_match.group(1)
        
        # Rozdeleni na radky <tr>
        rows = re.findall(r'<tr>(.*?)</tr>', tbody, re.DOTALL)
        
        for row in rows:
            item = self._parse_row(row)
            if item:
                items.append(item)
        
        return items
    
    def _parse_row(self, row_html: str) -> dict | None:
        """Parsuje jeden radek tabulky."""
        
        # Oblast
        oblast_match = re.search(r'edeska-sloupec-oblast">\s*(.*?)\s*</td>', row_html, re.DOTALL)
        oblast = clean_html_text(oblast_match.group(1)) if oblast_match else ""
        
        # Kategorie
        kategorie_match = re.search(r'edeska-sloupec-kategorie">\s*(.*?)\s*</td>', row_html, re.DOTALL)
        kategorie = clean_html_text(kategorie_match.group(1)) if kategorie_match else ""
        
        # Nazev + detail_id
        nazev_match = re.search(r'edeska-sloupec-nazev">\s*<a[^>]+href="eDeskaDetail\.jsp\?detailId=(\d+)"[^>]*>\s*(.*?)\s*</a>', row_html, re.DOTALL)
        if not nazev_match:
            return None
        
        detail_id = int(nazev_match.group(1))
        nazev = clean_html_text(nazev_match.group(2))
        
        # Cislo jednaci
        znacka_match = re.search(r'edeska-sloupec-znacka">\s*(.*?)\s*</td>', row_html, re.DOTALL)
        cislo_jednaci = clean_html_text(znacka_match.group(1)) if znacka_match else ""
        
        # Puvodce (cely obsah bunky vcetne adresy)
        puvodce_match = re.search(r'edeska-sloupec-puvodce">\s*(.*?)\s*</td>', row_html, re.DOTALL)
        puvodce = clean_html_text(puvodce_match.group(1)) if puvodce_match else ""
        
        # Zverejnit od a do - dve samostatne bunky
        dates = re.findall(r'edeska-sloupec-zverejnit">\s*(\d{2}\.\d{2}\.\d{4})\s*</td>', row_html)
        zverejnit_od = dates[0] if len(dates) >= 1 else ""
        zverejnit_do = dates[1] if len(dates) >= 2 else ""
        
        # Dokumenty ke stazeni
        dokument_match = re.search(r'edeska-sloupec-dokument">\s*(.*?)\s*</td>', row_html, re.DOTALL)
        files = []
        if dokument_match:
            doc_html = dokument_match.group(1)
            file_matches = re.findall(
                r'<a[^>]+href="download\.jsp\?idPriloha=(\d+)"[^>]*>\s*(.*?)\s*</a>',
                doc_html,
                re.DOTALL
            )
            for priloha_id, filename in file_matches:
                files.append({
                    "priloha_id": priloha_id,
                    "filename": clean_html_text(filename),
                })
        
        if not zverejnit_od:
            return None
        
        return {
            "detail_id": detail_id,
            "oblast": oblast,
            "kategorie": kategorie,
            "nazev": nazev,
            "cislo_jednaci": cislo_jednaci,
            "puvodce": puvodce,
            "zverejnit_od": zverejnit_od,
            "zverejnit_od_iso": format_date_iso(zverejnit_od),
            "zverejnit_do": zverejnit_do,
            "zverejnit_do_iso": format_date_iso(zverejnit_do) if zverejnit_do else "",
            "files": files,
        }


# ================== HLAVNI LOGIKA ==================

def main():
    init_logging()
    
    from_date = datetime.strptime(FROM_DATE, "%Y-%m-%d")
    to_date = datetime.strptime(TO_DATE, "%Y-%m-%d")
    
    log("=== Stahovani z uredni desky Magistratu mesta Brna ===")
    log(f"Datum od: {FROM_DATE}, do: {TO_DATE}")
    
    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    log(f"Lokalni slozka: {SAVE_DIR}")
    
    # SQL generatory
    sql_import = SQLImportGenerator(SQL_IMPORT_FILE)
    sql_logy = SQLLogyGenerator(SQL_LOGY_FILE)
    
    scraper = BrnoScraper()
    
    stats = {
        "pages_processed": 0,
        "items_processed": 0,
        "files_downloaded": 0,
        "files_uploaded": 0,
        "files_failed": 0,
    }
    
    first = 0
    total_count = 0
    finished = False
    
    while not finished:
        log(f"Nacitam stranku (offset {first})...")
        
        html = scraper.get_list_page(first)
        if not html:
            log("Chyba nacitani stranky, koncim.", "ERROR")
            break
        
        # Zjisti celkovy pocet pri prvnim nacteni
        if first == 0:
            total_count = scraper.parse_total_count(html)
            log(f"Celkovy pocet zaznamu na desce: {total_count}")
        
        items = scraper.parse_list_page(html)
        
        if not items:
            log("Zadne polozky na strance, koncim.")
            break
        
        log(f"Nalezeno {len(items)} polozek na strance")
        stats["pages_processed"] += 1
        
        for item in items:
            item_date = parse_date_cz(item["zverejnit_od"])
            
            # Filtr: preskocit novejsi nez to_date
            if item_date > to_date:
                continue
            
            # Filtr: ukoncit pokud jsme starsi nez from_date
            # (data jsou razena od nejnovejsich)
            if item_date < from_date:
                log(f"Dosazeno data {item['zverejnit_od']} (starsi nez {FROM_DATE}), koncim.")
                finished = True
                break
            
            log(f"-> [{item['detail_id']}] {item['nazev']} ({item['zverejnit_od']})")
            stats["items_processed"] += 1
            
            if not item["files"]:
                log("  (zadne soubory)")
                continue
            
            for file_info in item["files"]:
                original_name = file_info["filename"]
                sanitized_name = sanitize_filename(original_name)
                new_name = f"{item['zverejnit_od_iso']}_{sanitized_name}"
                
                final_name = get_available_filename(SAVE_DIR, new_name)
                save_path = SAVE_DIR / final_name
                
                random_pause()
                
                file_url = DOWNLOAD_URL + file_info["priloha_id"]
                if scraper.download_file(file_url, save_path):
                    log(f"  OK {final_name}")
                    stats["files_downloaded"] += 1
                    
                    # Upload na FTP
                    if upload_file_to_ftp(save_path, final_name):
                        log(f"  FTP OK")
                        stats["files_uploaded"] += 1
                    else:
                        log(f"  FTP CHYBA", "ERROR")
                    
                    # Pridat do SQL importu
                    datum_stazeni = f"{item['zverejnit_od_iso']} 00:00:00"
                    sql_import.add_file(
                        detail_id=item["detail_id"],
                        oblast=item["oblast"],
                        kategorie=item["kategorie"],
                        nazev=item["nazev"],
                        cislo_jednaci=item["cislo_jednaci"],
                        puvodce=item["puvodce"],
                        zverejnit_od=item["zverejnit_od_iso"],
                        zverejnit_do=item["zverejnit_do_iso"],
                        nazev_souboru=final_name,
                        datum_stazeni=datum_stazeni,
                    )
                else:
                    log(f"  CHYBA {original_name}", "ERROR")
                    stats["files_failed"] += 1
        
        if not finished:
            first += PAGE_SIZE
            if first >= total_count:
                log("Dosazeno konce seznamu.")
                break
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
        upload_sql_to_ftp(SQL_IMPORT_FILE, "soubory_z_magistratu_mesta_brna.sql")
    else:
        log("Zadne soubory ke stazeni, SQL import nevytvoren.")
    
    # Ulozit logy SQL
    sql_logy.save(get_log_text(), HAS_ERROR, marker_today=True)
    upload_sql_to_ftp(SQL_LOGY_FILE, "soubory_z_magistratu_mesta_brna_logy.sql")
    
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
