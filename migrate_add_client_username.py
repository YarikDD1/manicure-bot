# migrate_add_client_username.py
import sqlite3
import shutil
import os
from datetime import datetime

DB_PATH = os.path.join("Manictest1", "Manictest1.db")

if not os.path.exists(DB_PATH):
    print("Файл БД не найден:", DB_PATH)
    raise SystemExit(1)

# 1) backup
bak_name = DB_PATH + ".bak." + datetime.now().strftime("%Y%m%d%H%M%S")
print("Создаю резервную копию:", bak_name)
shutil.copy2(DB_PATH, bak_name)

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# 2) check if column exists
cur.execute("PRAGMA table_info(booking);")
cols = [r[1] for r in cur.fetchall()]
print("Колонки booking:", cols)

if "client_username" in cols:
    print("Столбец client_username уже есть — миграция не требуется.")
else:
    print("Добавляю столбец client_username ...")
    try:
        cur.execute("ALTER TABLE booking ADD COLUMN client_username TEXT;")
        conn.commit()
        print("Успешно: столбец добавлен.")
    except Exception as e:
        print("Ошибка при добавлении столбца:", e)
        print("Восстанавливаю из резервной копии...")
        shutil.copy2(bak_name, DB_PATH)
        print("Восстановлено.")
        raise

conn.close()
print("Миграция завершена. Теперь перезапустите Manictest1.py")
