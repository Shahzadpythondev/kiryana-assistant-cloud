import os
import json
import secrets
import urllib.parse
import re
import requests
from flask import Flask, request, redirect, url_for, session, render_template_string, flash
from io import BytesIO
from flask import send_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY or secrets.token_hex(32)

BASE_STYLE = """
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{font-family:Arial,sans-serif;background:#f3f6f4;margin:0;color:#17211b}
.wrap{max-width:760px;margin:24px auto;padding:16px}
.card{background:white;border-radius:14px;padding:18px;margin:12px 0;box-shadow:0 2px 9px #0001}
h1,h2{margin-top:0} input,button{box-sizing:border-box;width:100%;padding:12px;margin:6px 0;border-radius:8px;border:1px solid #ccd5cf;font-size:16px}
button{background:#176b45;color:white;border:0;font-weight:bold}
a{color:#176b45} .muted{color:#5b675f;font-size:14px}.error{color:#a21d1d}.ok{color:#176b45}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:520px){.row{grid-template-columns:1fr}}
table{display:block;width:100%;overflow-x:auto;border-collapse:collapse}td,th{text-align:left;padding:8px;border-bottom:1px solid #e2e8e3}
</style>
"""

def config_ok():
    return bool(SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY)

def api_headers(access_token=None, prefer=None):
    headers = {
        "apikey": SUPABASE_PUBLISHABLE_KEY,
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    else:
        headers["Authorization"] = "Bearer " + SUPABASE_PUBLISHABLE_KEY
    if prefer:
        headers["Prefer"] = prefer
    return headers

def supabase_auth(path, payload):
    response = requests.post(
        f"{SUPABASE_URL}/auth/v1/{path}",
        json=payload,
        headers=api_headers(),
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {"message": response.text}
    return response.status_code, data

def rest_request(table, method="GET", params=None, payload=None, prefer=None):
    token = session.get("access_token")
    if not token:
        return 401, {"message": "Please log in again."}
    response = requests.request(
        method,
        f"{SUPABASE_URL}/rest/v1/{table}",
        params=params,
        json=payload,
        headers=api_headers(token, prefer),
        timeout=20,
    )
    if response.status_code == 204 or not response.text:
        return response.status_code, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {"message": response.text}

def rpc_request(function_name, payload):
    return rest_request(
        "rpc/" + function_name,
        method="POST",
        payload=payload,
    )


def current_user_id():
    return session.get("user", {}).get("id")

def ensure_shop():
    user = session.get("user", {})
    user_id = user.get("id")
    if not user_id:
        return False
    metadata = user.get("user_metadata") or {}
    shop_name = session.get("shop_name") or metadata.get("shop_name") or "My Shop"
    status, data = rest_request(
        "shops",
        method="POST",
        params={"on_conflict": "id"},
        payload={"id": user_id, "shop_name": shop_name},
        prefer="resolution=merge-duplicates,return=minimal",
    )
    # If the row already exists, an upsert should succeed. A policy or network
    # error is surfaced on the dashboard instead of silently bypassing RLS.
    if status in (200, 201, 204):
        session["shop_name"] = shop_name
        return True
    session["shop_setup_error"] = (data or {}).get("message", str(data))
    return False

def page(title, body):
    return render_template_string(
        "<!doctype html><html><head><title>{{title}}</title>" + BASE_STYLE +
        "</head><body><div class='wrap'>" + body + "</div></body></html>",
        title=title,
    )

def safe_message(data):
    if isinstance(data, dict):
        return data.get("msg") or data.get("message") or data.get("error_description") or data.get("error") or str(data)
    return str(data)

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/")
def home():
    if session.get("access_token") and current_user_id():
        return redirect(url_for("dashboard"))
    body = """
    <div class="card">
      <h1>🛒 Kiryana Assistant</h1>
      <p>Apni shop ka hisaab mobile se manage karein.</p>
      {% if not configured %}<p class="error">Setup baqi hai: server par Supabase environment variables set karein.</p>{% endif %}
      <div class="row">
        <div><h2>Login</h2>
          <form method="post" action="/login">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
          </form>
        </div>
        <div><h2>Naya account</h2>
          <form method="post" action="/signup">
            <input name="shop_name" placeholder="Shop ka naam" required maxlength="100">
            <input type="email" name="email" placeholder="Email" required>
            <input type="password" name="password" placeholder="Password (kam az kam 8 characters)" required minlength="8">
            <button type="submit">Signup</button>
          </form>
        </div>
      </div>
      <p class="muted">Signup ke baad email verification zaroori ho sakti hai. Apna password kisi ke saath share na karein.</p>
    </div>
    """
    return page("Kiryana Assistant", render_template_string(body, configured=config_ok()))

@app.post("/signup")
def signup():
    if not config_ok():
        flash("Supabase URL/key configuration missing hai.")
        return redirect(url_for("home"))
    shop_name = (request.form.get("shop_name") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    if not shop_name or not email or len(password) < 8:
        flash("Shop name, valid email aur kam az kam 8-character password dein.")
        return redirect(url_for("home"))
    status, data = supabase_auth("signup", {
        "email": email,
        "password": password,
        "data": {"shop_name": shop_name},
    })
    if status not in (200, 201):
        flash("Signup nahi hua: " + safe_message(data))
        return redirect(url_for("home"))
    # With email confirmation enabled, the user normally logs in after confirming.
    if data.get("access_token") and data.get("user"):
        session.clear()
        session["access_token"] = data["access_token"]
        session["user"] = data["user"]
        session["shop_name"] = shop_name
        ensure_shop()
        return redirect(url_for("dashboard"))
    body = """
    <div class="card"><h1>Email check karein</h1>
    <p>Signup request bhej di gayi hai. Supabase se aane wali verification email khol kar email confirm karein, phir login karein.</p>
    <p><a href="/">Login page par wapas</a></p></div>
    """
    return page("Email verification", body)

@app.post("/login")
def login():
    if not config_ok():
        flash("Supabase URL/key configuration missing hai.")
        return redirect(url_for("home"))
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    status, data = supabase_auth("token?grant_type=password", {"email": email, "password": password})
    if status != 200 or not data.get("access_token"):
        flash("Login nahi hua: " + safe_message(data) + ". Email verify hui hai to dobara koshish karein.")
        return redirect(url_for("home"))
    session.clear()
    session["access_token"] = data["access_token"]
    session["user"] = data.get("user") or {}
    session["shop_name"] = (session["user"].get("user_metadata") or {}).get("shop_name", "My Shop")
    if not ensure_shop():
        error = session.pop("shop_setup_error", "Shop record create nahi ho saka.")
        flash("Login ho gaya, lekin shop setup mein masla: " + error)
        return redirect(url_for("dashboard"))
    return redirect(url_for("dashboard"))



def create_daily_local_backup():
    """Create one backup per shop per day and clean up old automatic backups."""
    user_id = current_user_id()
    if not user_id or not session.get("access_token"):
        return False

    backup_dir = os.path.expanduser("~/storage/downloads/kiryana_cloud_backups")
    today = datetime.now().astimezone().strftime("%Y%m%d")
    safe_shop_id = str(user_id).replace("/", "_").replace("\\\\", "_")
    filename = f"kiryana_auto_{today}_{safe_shop_id}.json"
    final_path = os.path.join(backup_dir, filename)
    backup_created = False

    try:
        os.makedirs(backup_dir, mode=0o700, exist_ok=True)

        # Create today's backup only if it does not already exist.
        if not os.path.isfile(final_path):
            tables = {
                "shops": {"select": "*", "id": "eq." + str(user_id)},
                "products": {"select": "*", "shop_id": "eq." + str(user_id)},
                "sales": {"select": "*", "shop_id": "eq." + str(user_id)},
                "expenses": {"select": "*", "shop_id": "eq." + str(user_id)},
                "udhaar": {"select": "*", "shop_id": "eq." + str(user_id)},
                "payments": {"select": "*", "shop_id": "eq." + str(user_id)},
            }
            backup_data = {
                "format": "kiryana-cloud-backup",
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "shop_id": user_id,
                "tables": {},
            }

            # Do not save a partial backup if any table fails.
            for table, params in tables.items():
                status, data = rest_request(table, params=params)
                if status != 200 or not isinstance(data, list):
                    app.logger.warning(
                        "Daily local backup skipped: could not read table %s", table
                    )
                    return False
                backup_data["tables"][table] = data

            temp_path = final_path + ".tmp"
            try:
                with open(temp_path, "w", encoding="utf-8") as f:
                    json.dump(backup_data, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, final_path)
                backup_created = True
            finally:
                if os.path.exists(temp_path):
                    os.remove(temp_path)

        # Run retention cleanup even when today's backup already exists.
        cutoff = datetime.now().astimezone().date().toordinal() - 29
        auto_pattern = re.compile(
            r"^kiryana_auto_(\\d{8})_[0-9a-fA-F-]+\\.json$"
        )
        for old_name in os.listdir(backup_dir):
            match = auto_pattern.match(old_name)
            if not match:
                continue
            try:
                file_day = datetime.strptime(match.group(1), "%Y%m%d").date()
                if file_day.toordinal() < cutoff:
                    os.remove(os.path.join(backup_dir, old_name))
                    app.logger.info("Old automatic backup removed: %s", old_name)
            except (ValueError, OSError):
                app.logger.warning(
                    "Could not process old automatic backup: %s", old_name
                )

        if backup_created:
            app.logger.info("Daily local backup saved: %s", filename)
        return backup_created

    except OSError:
        app.logger.exception("Daily local backup operation failed")
        return False


@app.get("/backup/download")
def download_backup():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    user_id = current_user_id()
    tables = {
        "shops": {"select": "*", "id": "eq." + str(user_id)},
        "products": {"select": "*", "shop_id": "eq." + str(user_id)},
        "sales": {"select": "*", "shop_id": "eq." + str(user_id)},
        "expenses": {"select": "*", "shop_id": "eq." + str(user_id)},
        "udhaar": {"select": "*", "shop_id": "eq." + str(user_id)},
        "payments": {"select": "*", "shop_id": "eq." + str(user_id)},
    }

    backup_data = {
        "format": "kiryana-cloud-backup",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "shop_id": user_id,
        "tables": {},
    }

    for table, params in tables.items():
        status, data = rest_request(table, params=params)
        if status != 200 or not isinstance(data, list):
            flash("Backup nahi bana. " + table + " ka data read nahi ho saka.")
            return redirect(url_for("dashboard"))
        backup_data["tables"][table] = data

    content = json.dumps(
        backup_data, ensure_ascii=False, indent=2
    ).encode("utf-8")

    filename = "kiryana_backup_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".json"
    return send_file(
        BytesIO(content),
        mimetype="application/json",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/backup/restore", methods=["GET", "POST"])
def restore_backup():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    message = ""
    if request.method == "POST":
        uploaded = request.files.get("backup_file")
        if not uploaded or not uploaded.filename:
            message = "<p class='error'>Pehle JSON backup file select karein.</p>"
        else:
            try:
                backup = json.loads(uploaded.read().decode("utf-8"))
                expected = {"shops", "products", "sales", "expenses", "udhaar", "payments"}

                if backup.get("format") != "kiryana-cloud-backup":
                    raise ValueError("Ye Kiryana Cloud backup file nahi hai.")
                if backup.get("version") != 1:
                    raise ValueError("Backup version supported nahi hai.")
                if str(backup.get("shop_id")) != str(current_user_id()):
                    raise ValueError("Ye backup logged-in shop ka nahi hai.")

                tables = backup.get("tables")
                if not isinstance(tables, dict) or set(tables) != expected:
                    raise ValueError("Backup tables ka structure sahi nahi.")
                if not all(isinstance(tables[t], list) for t in expected):
                    raise ValueError("Kisi table ka data format sahi nahi.")

                rpc_status, rpc_data = rpc_request(
                    "kiryana_restore_missing",
                    {"backup_data": backup},
                )

                if rpc_status not in (200, 201):
                    detail = (
                        rpc_data.get("message", "RPC validation failed")
                        if isinstance(rpc_data, dict)
                        else "RPC validation failed"
                    )
                    raise ValueError("Supabase validation failed: " + str(detail))

                lines = ["<p class='ok'>Backup validation successful. Database mein koi change nahi kiya.</p><ul>"]
                failed = False
                changed = 0

                for table in sorted(expected):
                    if table == "shops":
                        params = {"select": "*", "id": "eq." + str(current_user_id())}
                    else:
                        params = {"select": "*", "shop_id": "eq." + str(current_user_id())}

                    status, current = rest_request(table, params=params)
                    if status != 200 or not isinstance(current, list):
                        failed = True
                        lines.append("<li>" + table + ": database se read nahi ho saka.</li>")
                        continue

                    current_by_id = {str(row.get("id")): row for row in current}
                    backup_by_id = {str(row.get("id")): row for row in tables[table]}
                    missing = sorted(set(backup_by_id) - set(current_by_id))
                    different = []

                    for rid in sorted(set(backup_by_id) & set(current_by_id)):
                        b = backup_by_id[rid]
                        c = current_by_id[rid]
                        fields = sorted(set(b) | set(c))
                        diffs = [
                            field for field in fields
                            if str(b.get(field)) != str(c.get(field))
                        ]
                        if diffs:
                            different.append((rid, diffs))

                    changed += len(missing) + len(different)
                    lines.append("<li><b>" + table + "</b>: backup " +
                        str(len(tables[table])) + ", database " + str(len(current)) +
                        ", missing IDs " + str(len(missing)) +
                        ", different records " + str(len(different)) + "</li>")

                    if missing:
                        lines.append("<li>Missing IDs: " +
                            ", ".join(missing[:20]) + "</li>")
                    if different:
                        details = []
                        for rid, fields in different[:10]:
                            details.append("ID " + rid + ": " + ", ".join(fields[:10]))
                        lines.append("<li>Different fields: " +
                            "; ".join(details) + "</li>")

                lines.append("</ul>")
                if failed:
                    lines.append("<p class='error'>Kuch tables read nahi ho sake; comparison incomplete hai.</p>")
                elif changed == 0:
                    lines.append("<p class='ok'>IDs aur fields match karte hain.</p>")
                else:
                    lines.append("<p>Differences mili hain. Ye report sirf check karti hai; data restore nahi karti.</p>")
                message = "".join(lines)

            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as e:
                message = "<p class='error'>Backup verify nahi hua: " + str(e) + "</p>"

    body = """
    <div class="card">
      <h1>Backup Value Check</h1>
      <p>JSON backup select karein. Ye database se values compare karega; koi change nahi hoga.</p>
      <form method="post" enctype="multipart/form-data">
        <input type="file" name="backup_file" accept=".json,application/json" required>
        <button type="submit">Compare Values</button>
      </form>
      <div>""" + message + """</div>
      <p><a href="/dashboard">Dashboard par wapas</a></p>
    </div>
    """
    return page("Backup Value Check", body)

@app.get("/dashboard")
def dashboard():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))
    ensure_shop()
    if create_daily_local_backup():
        flash("Aaj ka automatic backup phone ke Downloads/kiryana_cloud_backups folder mein save ho gaya.")
    products_status, products = rest_request("products", params={
        "select": "id,name,cost,price,stock,low_stock,created_at",
        "order": "name.asc",
    })
    sales_status, sales = rest_request("sales", params={"select": "id,total,created_at", "order": "created_at.desc", "limit": "10"})
    expenses_status, expenses = rest_request("expenses", params={"select": "id,title,amount,created_at", "order": "created_at.desc", "limit": "10"})
    udhaar_status, debts = rest_request("udhaar", params={"select": "id,customer_name,total_amount,paid_amount,remaining,created_at", "order": "created_at.desc", "limit": "10"})
    products = products if isinstance(products, list) else []
    sales = sales if isinstance(sales, list) else []
    expenses = expenses if isinstance(expenses, list) else []
    debts = debts if isinstance(debts, list) else []
    payments_status, payments = rest_request(
        "payments",
        params={
            "select": "id,udhaar_id,amount,created_at",
            "order": "created_at.desc",
            "limit": "50",
        },
    )
    payments = payments if isinstance(payments, list) else []

    debt_names = {
        str(d.get("id")): d.get("customer_name", "Unknown")
        for d in debts
    }
    for payment in payments:
        payment["customer_name"] = debt_names.get(
            str(payment.get("udhaar_id")), "Customer"
        )

    # Fetch all records separately for accurate dashboard totals.
    _, all_sales = rest_request(
        "sales",
        params={"select": "total"},
    )
    _, all_expenses = rest_request(
        "expenses",
        params={"select": "amount"},
    )
    _, all_debts = rest_request(
        "udhaar",
        params={"select": "remaining"},
    )

    all_sales = all_sales if isinstance(all_sales, list) else []
    all_expenses = all_expenses if isinstance(all_expenses, list) else []
    all_debts = all_debts if isinstance(all_debts, list) else []

    total_sales = sum(float(x.get("total") or 0) for x in all_sales)
    total_expenses = sum(float(x.get("amount") or 0) for x in all_expenses)
    total_debt = sum(float(x.get("remaining") or 0) for x in all_debts)
    body = """
    <div class="card">
      <p>
        <a href="/reports">📊 Reports</a> | <a href="/backup/download">📥 Download Backup</a> | <a href="/backup/restore">📤 Restore Backup</a> |
        <a href="/logout">Logout</a>
      </p>
      <h1>🛒 {{ shop_name }}</h1><p class="muted">{{ email }}</p>
      {% if setup_error %}<p class="error">Shop setup: {{setup_error}}</p>{% endif %}
      <div class="row">
        <div class="card"><b>Recent sales total</b><h2>Rs {{ "%.2f"|format(total_sales) }}</h2></div>
        <div class="card"><b>Recent expenses total</b><h2>Rs {{ "%.2f"|format(total_expenses) }}</h2></div>
      </div>
      <p><b>Recent outstanding udhaar:</b> Rs {{ "%.2f"|format(total_debt) }}</p>
    </div>
    <div class="card"><h2>Product add karein</h2>
      <form method="post" action="/products">
        <input name="name" placeholder="Product name" required>
        <div class="row"><input name="cost" type="number" step="0.01" min="0" placeholder="Cost price" required>
        <input name="price" type="number" step="0.01" min="0" placeholder="Sale price" required></div>
        <div class="row"><input name="stock" type="number" step="0.01" min="0" placeholder="Opening stock" required>
        <input name="low_stock" type="number" step="0.01" min="0" value="5" placeholder="Low stock alert" required></div>
        <button type="submit">Product save karein</button>
      </form>
    </div>
    
<div class="card">
  <h2>Products / Stock</h2>

  {% if products %}
  {% for p in products %}
  <div class="card">
    <h3>{{p.name}}</h3>
    <p>Current Stock: {{p.stock}}
      {% if p.stock|float <= p.low_stock|float %} ⚠️{% endif %}
    </p>

    <form method="post" action="/products/edit">
      <input type="hidden" name="product_id" value="{{p.id}}">

      <label>Product Name</label>
      <input type="text" name="name" value="{{p.name}}" required>

      <label>Cost Price</label>
      <input type="number" name="cost" step="0.01" min="0"
             value="{{p.cost}}" required>

      <label>Sale Price</label>
      <input type="number" name="price" step="0.01" min="0"
             value="{{p.price}}" required>

      <label>Low Stock Alert</label>
      <input type="number" name="low_stock" step="0.01" min="0"
             value="{{p.low_stock}}" required>

      <button type="submit">Update Product</button>
    </form>
  </div>
  {% endfor %}
  {% else %}
  <p>Abhi product nahi hai.</p>
  {% endif %}
</div><div class="card"><h2>Products / Stock</h2>
    {% if products %}<table><tr><th>Naam</th><th>Stock</th><th>Cost</th><th>Price</th></tr>
    {% for p in products %}<tr><td>{{p.name}}</td><td>{{p.stock}}{% if p.stock|float <= p.low_stock|float %} ⚠️{% endif %}</td><td>{{p.cost}}</td><td>{{p.price}}</td></tr>{% endfor %}</table>
    {% else %}<p>Abhi product nahi hai.</p>{% endif %}</div>
    
    <div class="card"><h2>Add Stock / Naya Maal</h2>
      <form method="post" action="/restock">
        <label>Product select karein</label>
        <select name="product_id" required>
          <option value="">Product choose karein</option>
          {% for p in products %}
          <option value="{{p.id}}">{{p.name}} — Stock: {{p.stock}}</option>
          {% endfor %}
        </select>
        <label>Nayi quantity</label>
        <input name="quantity" type="number" step="0.01" min="0.01" placeholder="Kitna maal aaya?" required>
        <button type="submit">Stock add karein</button>
      </form>
    </div><div class="card"><h2>Sale Entry</h2>
      <form method="post" action="/sales">
        <label>Product select karein</label>
        <select name="product_id" required>
          <option value="">Product choose karein</option>
          {% for p in products %}
          <option value="{{p.id}}">{{p.name}} — Stock: {{p.stock}} — Rs {{p.price}}</option>
          {% endfor %}
        </select>
        <label>Quantity</label>
        <input name="quantity" type="number" step="0.01" min="0.01" placeholder="Kitni quantity biki?" required>
        <button type="submit">Sale save karein</button>
      </form>
    </div>
    <div class="card"><h2>Kharch add karein</h2>
      <form method="post" action="/expenses">
        <input name="title" placeholder="Kharch ki detail" required>
        <input name="amount" type="number" step="0.01" min="0.01" placeholder="Raqam (Rs)" required>
        <button type="submit">Kharch save karein</button>
      </form>
      
      
{% if expenses %}
<h3>Recent expenses</h3>
<table>
  <tr>
    <th>Detail</th>
    <th>Raqam</th>
    <th>Date</th>
    <th>Edit</th>
  </tr>

  {% for e in expenses %}
  <tr>
    <td>{{e.title}}</td>
    <td>{{e.amount}}</td>
    <td>{{e.created_at[:10] if e.created_at else ""}}</td>
    <td>
      <form method="post" action="/expenses/edit">
        <input type="hidden" name="expense_id" value="{{e.id}}">

        <label>Kharch ki detail</label>
        <input name="title" value="{{e.title}}" required>

        <label>Raqam (Rs)</label>
        <input name="amount" type="number" step="0.01"
               min="0.01" value="{{e.amount}}" required>

        <button type="submit">Update Expense</button>
      </form>
      <form method="post" action="/expenses/delete"
            onsubmit="return confirm('Kya aap ye expense delete karna chahte hain?')">
        <input type="hidden" name="expense_id" value="{{e.id}}">
        <button type="submit" style="background:#b42318">Delete Expense</button>
      </form>
    </td>
  </tr>
  {% endfor %}
</table>
{% endif %}
    </div>
    <div class="card"><h2>Udhaar Entry</h2>
      <form method="post" action="/udhaar">
        <input name="customer_name" placeholder="Customer ka naam" required>
        <input name="phone" type="tel" placeholder="Phone number">
        <div class="row">
          <input name="total_amount" type="number" step="0.01" min="0.01" placeholder="Total udhaar (Rs)" required>
          <input name="paid_amount" type="number" step="0.01" min="0" value="0" placeholder="Abhi paid (Rs)" required>
        </div>
        <input name="notes" placeholder="Notes (optional)">
        <button type="submit">Udhaar save karein</button>
      </form>
    </div>
    <div class="card"><h2>Udhaar Wasooli</h2>
      <form method="post" action="/recovery">
        <select name="udhaar_id" required>
          <option value="">Customer select karein</option>
          {% for d in debts %}
          {% if d.remaining|float > 0 %}
          <option value="{{d.id}}">{{d.customer_name}} — Baqi Rs {{d.remaining}}</option>
          {% endif %}
          {% endfor %}
        </select>
        <input name="amount" type="number" min="0.01" step="0.01"
               placeholder="Receive payment (Rs)" required>
        <button type="submit">Payment save karein</button>
      </form>
    </div>
    <div class="card"><h2>Udhaar records</h2>
    {% if debts %}<table><tr><th>Customer</th><th>Total</th><th>Baqi</th><th>Edit</th></tr>
    {% for d in debts %}<tr><td>{{d.customer_name}}</td><td>{{d.total_amount}}</td><td>{{d.remaining}}</td><td>
    <form method="post" action="/udhaar/edit">
    <input type="hidden" name="id" value="{{d.id}}">
    <input name="customer_name" value="{{d.customer_name}}" placeholder="Customer name" required>
    <input name="phone" value="{{d.phone or ''}}" placeholder="Phone">
    <input name="notes" value="{{d.notes or ''}}" placeholder="Notes">
    <button type="submit">Update</button></form></td></tr>{% endfor %}</table>
    {% else %}<p>Abhi udhaar record nahi hai.</p>{% endif %}</div>
    <div class="card"><h2>Payment History — Wasooli Record</h2>
    {% if payments %}
    <table>
      <tr><th>Customer</th><th>Received (Rs)</th><th>Date</th></tr>
      {% for pay in payments %}
      <tr><td>{{pay.customer_name}}</td><td>{{pay.amount}}</td><td>{{pay.created_at[:10] if pay.created_at else ""}}</td></tr>
      {% endfor %}
    </table>
    {% else %}<p>Abhi payment history available nahi hai.</p>{% endif %}
    </div>
    <div class="card"><h2>Recent sales</h2>
    {% if sales %}<table><tr><th>Total</th><th>Date</th></tr>{% for s in sales %}<tr><td>{{s.total}}</td><td>{{s.created_at[:10] if s.created_at else ""}}</td></tr>{% endfor %}</table>
    {% else %}<p>Abhi sale record nahi hai.</p>{% endif %}</div>
    """
    setup_error = session.pop("shop_setup_error", "")
    return page("Dashboard", render_template_string(
        body, shop_name=session.get("shop_name", "My Shop"),
        email=session.get("user", {}).get("email", ""),
        products=products, sales=sales, expenses=expenses, debts=debts,
        total_sales=total_sales, total_expenses=total_expenses,
        total_debt=total_debt, setup_error=setup_error,
        payments=payments,
    ))

@app.post("/products")
def add_product():
    if not current_user_id():
        return redirect(url_for("home"))
    name = (request.form.get("name") or "").strip()
    try:
        cost = float(request.form.get("cost", "0"))
        price = float(request.form.get("price", "0"))
        stock = float(request.form.get("stock", "0"))
        low_stock = float(request.form.get("low_stock", "5"))
        if not name or min(cost, price, stock, low_stock) < 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Product details aur numbers check karein.")
        return redirect(url_for("dashboard"))
    status, data = rest_request("products", method="POST", payload={
        "shop_id": current_user_id(), "name": name, "cost": cost,
        "price": price, "stock": stock, "low_stock": low_stock,
    }, prefer="return=minimal")
    if status not in (200, 201, 204):
        flash("Product save nahi hua: " + safe_message(data))
    return redirect(url_for("dashboard"))


@app.post("/products/edit")
def edit_product():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        product_id = int(request.form.get("product_id", "0"))
        name = (request.form.get("name") or "").strip()
        cost = float(request.form.get("cost", "0"))
        price = float(request.form.get("price", "0"))
        low_stock = float(request.form.get("low_stock", "5"))

        if product_id <= 0 or not name or min(cost, price, low_stock) < 0:
            raise ValueError()

    except (ValueError, TypeError):
        flash("Product details check karein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "products",
        params={
            "id": f"eq.{product_id}",
            "select": "id"
        }
    )

    if status != 200 or not data:
        flash("Product nahi mila.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "products",
        method="PATCH",
        params={"id": f"eq.{product_id}"},
        payload={
            "name": name,
            "cost": cost,
            "price": price,
            "low_stock": low_stock
        },
        prefer="return=minimal"
    )

    if status not in (200, 204):
        flash("Product update nahi hua: " + safe_message(data))
    else:
        flash("Product successfully update ho gaya!")

    return redirect(url_for("dashboard"))

@app.post("/restock")
def add_restock():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        product_id = int(request.form.get("product_id", "0"))
        quantity = float(request.form.get("quantity", "0"))
        if product_id <= 0 or quantity <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Product aur positive quantity check karein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "products",
        params={
            "id": f"eq.{product_id}",
            "select": "id,stock",
        },
    )

    if status != 200 or not isinstance(data, list) or not data:
        flash("Product nahi mila ya stock read nahi ho saka.")
        return redirect(url_for("dashboard"))

    old_stock = float(data[0].get("stock") or 0)
    new_stock = round(old_stock + quantity, 2)

    status, data = rest_request(
        "products",
        method="PATCH",
        params={"id": f"eq.{product_id}"},
        payload={"stock": new_stock},
        prefer="return=minimal",
    )

    if status in (200, 204):
        flash(f"Stock update ho gaya. Naya stock: {new_stock}")
    else:
        flash("Stock update nahi hua: " + safe_message(data))

    return redirect(url_for("dashboard"))

@app.post("/sales")
def add_sale():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        product_id = int(request.form.get("product_id", "0"))
        quantity = float(request.form.get("quantity", "0"))
        if product_id <= 0 or quantity <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Product aur positive quantity sahi enter karein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "rpc/record_sale",
        method="POST",
        payload={
            "p_product_id": product_id,
            "p_quantity": quantity,
        },
    )

    if status in (200, 201):
        if isinstance(data, dict):
            flash(
                "Sale save ho gayi! Product: "
                + str(data.get("product", ""))
                + " | Total: Rs "
                + str(data.get("total", 0))
                + " | Profit: Rs "
                + str(data.get("profit", 0))
            )
        else:
            flash("Sale save ho gayi.")
    else:
        error = safe_message(data)
        flash("Sale save nahi hui: " + error)

    return redirect(url_for("dashboard"))

@app.post("/expenses")
def add_expense():
    if not current_user_id():
        return redirect(url_for("home"))
    title = (request.form.get("title") or "").strip()
    try:
        amount = float(request.form.get("amount", "0"))
        if not title or amount <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Kharch ki detail aur positive raqam dein.")
        return redirect(url_for("dashboard"))
    status, data = rest_request("expenses", method="POST", payload={
        "shop_id": current_user_id(), "title": title, "amount": amount,
    }, prefer="return=minimal")
    if status not in (200, 201, 204):
        flash("Kharch save nahi hua: " + safe_message(data))
    return redirect(url_for("dashboard"))

@app.post("/expenses/edit")
def edit_expense():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        expense_id = int(request.form.get("expense_id", "0"))
        title = (request.form.get("title") or "").strip()
        amount = round(float(request.form.get("amount", "0")), 2)

        if expense_id <= 0 or not title or amount <= 0:
            raise ValueError()

    except (ValueError, TypeError):
        flash("Kharch ki detail aur positive raqam dein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "expenses",
        params={
            "id": f"eq.{expense_id}",
            "select": "id",
        },
    )

    if status != 200 or not isinstance(data, list) or not data:
        flash("Kharch ka record nahi mila.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "expenses",
        method="PATCH",
        params={"id": f"eq.{expense_id}"},
        payload={"title": title, "amount": amount},
        prefer="return=minimal",
    )

    if status in (200, 204):
        flash("Kharcha successfully update ho gaya!")
    else:
        flash("Kharcha update nahi hua: " + safe_message(data))

    return redirect(url_for("dashboard"))


@app.post("/expenses/delete")
def delete_expense():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        expense_id = int(request.form.get("expense_id", "0"))
        if expense_id <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Expense ID ghalat hai.")
        return redirect(url_for("dashboard"))

    filters = {
        "id": f"eq.{expense_id}",
        "shop_id": f"eq.{current_user_id()}",
        "select": "id",
    }

    status, data = rest_request("expenses", params=filters)

    if status != 200 or not isinstance(data, list) or not data:
        flash("Expense nahi mila ya aapko permission nahi hai.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "expenses",
        method="DELETE",
        params={
            "id": f"eq.{expense_id}",
            "shop_id": f"eq.{current_user_id()}",
        },
        prefer="return=minimal",
    )

    if status in (200, 204):
        flash("Expense delete ho gaya.")
    else:
        flash("Delete nahi hua: " + safe_message(data))

    return redirect(url_for("dashboard"))


@app.post("/udhaar")
def add_udhaar():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    customer_name = (request.form.get("customer_name") or "").strip()
    phone = (request.form.get("phone") or "").strip()
    notes = (request.form.get("notes") or "").strip()

    try:
        total_amount = float(request.form.get("total_amount", "0"))
        paid_amount = float(request.form.get("paid_amount", "0"))
        if not customer_name or total_amount <= 0 or paid_amount < 0:
            raise ValueError()
        if paid_amount > total_amount:
            flash("Paid amount total udhaar se zyada nahi ho sakti.")
            return redirect(url_for("dashboard"))
    except (ValueError, TypeError):
        flash("Customer name aur amount sahi enter karein.")
        return redirect(url_for("dashboard"))

    remaining = round(total_amount - paid_amount, 2)

    status, data = rest_request(
        "udhaar",
        method="POST",
        payload={
            "shop_id": current_user_id(),
            "customer_name": customer_name,
            "phone": phone or None,
            "total_amount": total_amount,
            "paid_amount": paid_amount,
            "remaining": remaining,
            "notes": notes or None,
        },
        prefer="return=minimal",
    )

    if status in (200, 201, 204):
        flash("Udhaar record save ho gaya. Baqi raqam: Rs " + str(remaining))
    else:
        flash("Udhaar save nahi hua: " + safe_message(data))

    return redirect(url_for("dashboard"))

@app.post("/udhaar/edit")
def edit_udhaar():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        udhaar_id = int(request.form.get("id", "0"))
        customer_name = (request.form.get("customer_name") or "").strip()
        phone = (request.form.get("phone") or "").strip()
        notes = (request.form.get("notes") or "").strip()

        if udhaar_id <= 0 or not customer_name:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Customer ki details sahi enter karein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "udhaar",
        params={
            "id": "eq." + str(udhaar_id),
            "shop_id": "eq." + str(current_user_id()),
            "select": "id",
        },
    )

    if status != 200 or not data:
        flash("Udhaar record nahi mila ya access nahi hai.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "udhaar",
        method="PATCH",
        params={
            "id": "eq." + str(udhaar_id),
            "shop_id": "eq." + str(current_user_id()),
        },
        payload={
            "customer_name": customer_name,
            "phone": phone or None,
            "notes": notes or None,
        },
        prefer="return=minimal",
    )

    if status in (200, 204):
        flash("Udhaar customer details update ho gayi hain.")
    else:
        flash("Update nahi hua: " + safe_message(data))

    return redirect(url_for("dashboard"))


@app.post("/recovery")
def add_recovery():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    try:
        udhaar_id = int(request.form.get("udhaar_id", "0"))
        amount = round(float(request.form.get("amount", "0")), 2)
        if udhaar_id <= 0 or amount <= 0:
            raise ValueError()
    except (ValueError, TypeError):
        flash("Customer aur payment amount check karein.")
        return redirect(url_for("dashboard"))

    status, data = rest_request(
        "udhaar",
        params={
            "select": "id,customer_name,paid_amount,remaining",
            "id": "eq." + str(udhaar_id),
            "limit": "1",
        },
    )

    if status != 200 or not isinstance(data, list) or not data:
        flash("Udhaar record nahi mila.")
        return redirect(url_for("dashboard"))

    debt = data[0]
    remaining = round(float(debt.get("remaining") or 0), 2)
    paid_before = round(float(debt.get("paid_amount") or 0), 2)

    if amount > remaining:
        flash("Payment baqi raqam se zyada nahi ho sakti: Rs " + str(remaining))
        return redirect(url_for("dashboard"))

    pay_status, pay_data = rest_request(
        "payments",
        method="POST",
        payload={
            "shop_id": current_user_id(),
            "udhaar_id": udhaar_id,
            "amount": amount,
        },
        prefer="return=minimal",
    )

    if pay_status not in (200, 201, 204):
        flash("Payment save nahi hui: " + safe_message(pay_data))
        return redirect(url_for("dashboard"))

    new_paid = round(paid_before + amount, 2)
    new_remaining = round(remaining - amount, 2)

    update_status, update_data = rest_request(
        "udhaar",
        method="PATCH",
        params={"id": "eq." + str(udhaar_id)},
        payload={
            "paid_amount": new_paid,
            "remaining": new_remaining,
        },
        prefer="return=minimal",
    )

    if update_status not in (200, 204):
        flash("Payment save hui, lekin balance update nahi hua. Dobara payment submit na karein; pehle database check karein.")
        return redirect(url_for("dashboard"))

    flash("Payment receive ho gayi. Baqi udhaar: Rs " + str(new_remaining))
    return redirect(url_for("dashboard"))


@app.get("/reports")
def reports():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    from datetime import date

    _, sales = rest_request("sales", params={
        "select": "id,total,quantity,cost_price,sale_price,created_at",
        "order": "created_at.desc",
    })
    _, expenses = rest_request("expenses", params={
        "select": "id,title,amount,created_at",
        "order": "created_at.desc",
    })
    _, debts = rest_request("udhaar", params={
        "select": "id,remaining",
    })

    sales = sales if isinstance(sales, list) else []
    expenses = expenses if isinstance(expenses, list) else []
    debts = debts if isinstance(debts, list) else []

    today = date.today().isoformat()
    month = today[:7]

    def in_period(record, period):
        created = str(record.get("created_at") or "")
        return created.startswith(period)

    def sale_profit(record):
        try:
            total = float(record.get("total") or 0)
            cost = float(record.get("cost_price") or 0)
            qty = float(record.get("quantity") or 0)
            return total - cost * qty
        except (ValueError, TypeError):
            return 0.0

    today_sales = [x for x in sales if in_period(x, today)]
    month_sales = [x for x in sales if in_period(x, month)]
    today_expenses = [x for x in expenses if in_period(x, today)]
    month_expenses = [x for x in expenses if in_period(x, month)]

    def total(records, key):
        return sum(float(x.get(key) or 0) for x in records)

    body = """
    <div class="card">
      <p><a href="/dashboard">← Dashboard</a></p>
      <h1>Daily & Monthly Reports</h1>
      <p><a href="/reports/pdf"><button type="button">Download PDF Report</button></a></p>
      <p>Today: {{today}}</p>
      <h2>Aaj ki report</h2>
      <p>Sales: Rs {{ "%.2f"|format(today_sales) }}</p>
      <p>Estimated gross profit: Rs {{ "%.2f"|format(today_profit) }}</p>
      <p>Expenses: Rs {{ "%.2f"|format(today_expenses) }}</p>
      <hr>
      <h2>Is mahine ki report</h2>
      <p>Month: {{month}}</p>
      <p>Sales: Rs {{ "%.2f"|format(month_sales) }}</p>
      <p>Estimated gross profit: Rs {{ "%.2f"|format(month_profit) }}</p>
      <p>Expenses: Rs {{ "%.2f"|format(month_expenses) }}</p>
      <hr>
      <h2>Outstanding Udhaar</h2>
      <p>Abhi baqi: Rs {{ "%.2f"|format(outstanding) }}</p>
      <p class="muted">Profit sale records ke cost_price par based estimate hai.
      Agar purane records mein cost_price save nahi hai, profit kam ya zero
      calculate ho sakta hai.</p>
    </div>
    """

    return page("Reports", render_template_string(
        body,
        today=today,
        month=month,
        today_sales=total(today_sales, "total"),
        month_sales=total(month_sales, "total"),
        today_profit=sum(sale_profit(x) for x in today_sales),
        month_profit=sum(sale_profit(x) for x in month_sales),
        today_expenses=total(today_expenses, "amount"),
        month_expenses=total(month_expenses, "amount"),
        outstanding=total(debts, "remaining"),
    ))


@app.get("/reports/pdf")
def reports_pdf():
    if not current_user_id() or not session.get("access_token"):
        return redirect(url_for("home"))

    from datetime import date
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    _, sales = rest_request("sales", params={
        "select": "total,quantity,cost_price,created_at",
        "order": "created_at.desc",
    })
    _, expenses = rest_request("expenses", params={
        "select": "amount,created_at",
        "order": "created_at.desc",
    })
    _, debts = rest_request("udhaar", params={
        "select": "remaining",
    })

    sales = sales if isinstance(sales, list) else []
    expenses = expenses if isinstance(expenses, list) else []
    debts = debts if isinstance(debts, list) else []

    today = date.today().isoformat()
    month = today[:7]

    def period_total(records, period, key):
        return sum(
            float(r.get(key) or 0)
            for r in records
            if str(r.get("created_at") or "").startswith(period)
        )

    def profit(records, period):
        result = 0.0
        for r in records:
            if str(r.get("created_at") or "").startswith(period):
                result += float(r.get("total") or 0) - (
                    float(r.get("cost_price") or 0)
                    * float(r.get("quantity") or 0)
                )
        return result

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    y = height - 60

    pdf.setTitle("Kiryana Assistant Report")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(50, y, "Kiryana Assistant - Reports")
    y -= 35

    pdf.setFont("Helvetica", 12)
    lines = [
        f"Report date: {today}",
        "",
        "DAILY REPORT",
        f"Sales: Rs {period_total(sales, today, 'total'):.2f}",
        f"Estimated gross profit: Rs {profit(sales, today):.2f}",
        f"Expenses: Rs {period_total(expenses, today, 'amount'):.2f}",
        "",
        f"MONTHLY REPORT ({month})",
        f"Sales: Rs {period_total(sales, month, 'total'):.2f}",
        f"Estimated gross profit: Rs {profit(sales, month):.2f}",
        f"Expenses: Rs {period_total(expenses, month, 'amount'):.2f}",
        "",
        f"Outstanding Udhaar: Rs {sum(float(d.get('remaining') or 0) for d in debts):.2f}",
        "",
        "Note: Gross profit is an estimate based on saved sale costs.",
    ]

    for line in lines:
        if y < 50:
            pdf.showPage()
            y = height - 60
            pdf.setFont("Helvetica", 12)
        pdf.drawString(50, y, line)
        y -= 22

    pdf.save()
    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"kiryana_report_{today}.pdf",
    )

@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))

@app.context_processor
def inject_flashes():
    from flask import get_flashed_messages
    messages = get_flashed_messages()
    if not messages:
        return {}
    # Display messages on every page in a small banner.
    return {"flash_messages": messages}

# Attach flash banners to the common page wrapper.
_original_page = page
def page(title, body):
    banner = """
    {% for msg in flash_messages|default([]) %}<div class="card error">{{msg}}</div>{% endfor %}
    """
    return render_template_string(
        "<!doctype html><html><head><title>{{title}}</title>" + BASE_STYLE +
        "</head><body><div class='wrap'>" + banner + body + "</div></body></html>",
        title=title,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
