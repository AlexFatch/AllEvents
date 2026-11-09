import qrcode
import random
import sqlite3 # Changed from pyodbc
import os

# --- CONFIGURATION ---
BASE_URL = "https://anesu.pythonanywhere.com/gate/" # <--- WE WILL CHANGE THIS SOON
NUMBER_OF_TAGS = 50                 
DB_NAME = 'smart_recovery.db'
OUTPUT_FOLDER = 'final_print_batch'
SAFE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"

def generate_unique_code(cursor):
    while True:
        code = ''.join(random.choices(SAFE_CHARS, k=6))
        # Check SQLite
        if cursor.execute("SELECT Count(*) FROM EventTickets WHERE TicketCode = ?", (code,)).fetchone()[0] == 0:
            return code

def create_batch():
    if not os.path.exists(OUTPUT_FOLDER): os.makedirs(OUTPUT_FOLDER)
    
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    
    print(f"Generating {NUMBER_OF_TAGS} tickets into SQLite...")

    for i in range(NUMBER_OF_TAGS):
        tag_id = generate_unique_code(cursor)
        full_url = f"{BASE_URL}{tag_id}"

        # Insert into SQLite
        cursor.execute(
            "INSERT INTO EventTickets (TicketCode, IsUsed, EventName, TicketType) VALUES (?, 0, 'Launch Event', 'Standard')", 
            (tag_id,)
        )
        
        qr = qrcode.QRCode(box_size=10, border=2)
        qr.add_data(full_url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        img.save(os.path.join(OUTPUT_FOLDER, f"{tag_id}.png"))

    conn.commit()
    conn.close()
    print("Success! Ready for upload.")

if __name__ == "__main__":
    create_batch()