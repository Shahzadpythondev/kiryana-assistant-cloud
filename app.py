#!/usr/bin/env python3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from datetime import datetime
import json, os, io, re

HOST, PORT = "127.0.0.1", 8000
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "kiryana_data.json")
BACKUP_DIR = os.path.join(BASE_DIR, "backup")

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def number(value, label="Raqam"):
    try: n = float(value)
    except (TypeError, ValueError): raise ValueError(label + " durust likhein.")
    if n < 0: raise ValueError(label + " negative nahi ho sakti.")
    return round(n, 2)

def load_data():
    if not os.path.exists(DATA_FILE):
        data = {"products": [], "sales": [], "udhaar": [], "expenses": [], "payments": []}
        save_data(data)
        return data
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict): raise RuntimeError("Data file ka format durust nahi.")
    for key in ("products", "sales", "udhaar", "expenses", "payments"):
        if not isinstance(data.get(key), list): data[key] = []
    return data

def save_data(data):
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

def money(n):
    return "Rs " + format(float(n or 0), ",.2f")

def pdf_escape(s):
    s = str(s).encode("ascii", "replace").decode("ascii")
    return s.replace("\\\\", "\\\\\\\\").replace("(", "\\\\(").replace(")", "\\\\)")

def make_pdf(lines, title):
    wrapped = []
    for line in lines:
        line = str(line)
        while len(line) > 94:
            wrapped.append(line[:94]); line = "  " + line[94:]
        wrapped.append(line)
    pages = [wrapped[i:i+48] for i in range(0, max(1, len(wrapped)), 48)]
    objs = []
    def add(obj): objs.append(obj); return len(objs)
    font_id = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids = []
    for pnum, lines_on_page in enumerate(pages, 1):
        cmds = ["BT", "/F1 10 Tf", "48 800 Td", "14 TL",
                "(" + pdf_escape(title) + ") Tj", "T*",
                "(" + pdf_escape("Generated: " + now_text() + " | Page " + str(pnum)) + ") Tj",
                "T*", "(" + "-" * 88 + ") Tj"]
        for line in lines_on_page:
            cmds += ["T*", "(" + pdf_escape(line) + ") Tj"]
        cmds.append("ET")
        stream = "\n".join(cmds).encode("latin-1", "replace")
        content_id = add("<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream.decode("latin-1")))
        page_id = add("<< /Type /Page /Parent PAGESROOT /MediaBox [0 0 595 842] /Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>" % (font_id, content_id))
        page_ids.append(page_id)
    pages_id = add("<< /Type /Pages /Kids [%s] /Count %d >>" % (" ".join(str(i)+" 0 R" for i in page_ids), len(page_ids)))
    catalog_id = add("<< /Type /Catalog /Pages %d 0 R >>" % pages_id)
    for pid in page_ids: objs[pid-1] = objs[pid-1].replace("PAGESROOT", str(pages_id) + " 0 R")
    out = io.BytesIO(); out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"); offsets = [0]
    for i, obj in enumerate(objs, 1):
        offsets.append(out.tell()); out.write(("%d 0 obj\n" % i).encode())
        out.write(obj.encode("latin-1", "replace")); out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(("xref\n0 %d\n" % (len(objs)+1)).encode()); out.write(b"0000000000 65535 f \n")
    for off in offsets[1:]: out.write(("%010d 00000 n \n" % off).encode())
    out.write(("trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs)+1, catalog_id, xref)).encode())
    return out.getvalue()

def monthly_pdf(data, month):
    if not re.fullmatch(r"\d{4}-\d{2}", month): raise ValueError("Month format YYYY-MM hona chahiye.")
    try: datetime.strptime(month, "%Y-%m")
    except ValueError: raise ValueError("Month durust nahi.")
    def chosen(x): return str(x.get("date", ""))[:7] == month
    sales = [x for x in data["sales"] if chosen(x)]
    expenses = [x for x in data["expenses"] if chosen(x)]
    payments = [x for x in data["payments"] if chosen(x)]
    total_sales = sum(float(x.get("total",0) or 0) for x in sales)
    cash = sum(float(x.get("total",0) or 0) for x in sales if x.get("payment","cash") != "udhaar")
    credit = sum(float(x.get("total",0) or 0) for x in sales if x.get("payment") == "udhaar")
    cost = sum(float(x.get("cost_total",0) or 0) for x in sales)
    exp = sum(float(x.get("amount",0) or 0) for x in expenses)
    recovered = sum(float(x.get("amount",0) or 0) for x in payments)
    debt = sum(float(x.get("remaining",0) or 0) for x in data["udhaar"])
    stock_value = sum(float(p.get("stock",0) or 0)*float(p.get("cost",0) or 0) for p in data["products"])
    lines = [
        "MONTHLY KIRYANA BUSINESS REPORT", "Report month: " + month, "",
        "FINANCIAL SUMMARY", "Total sales: " + money(total_sales), "Cash sales: " + money(cash),
        "Udhaar sales: " + money(credit), "Estimated cost of goods sold: " + money(cost),
        "Estimated gross profit (sales - cost): " + money(total_sales-cost),
        "Recorded expenses: " + money(exp), "Estimated profit after expenses: " + money(total_sales-cost-exp),
        "Udhaar payments recovered this month: " + money(recovered),
        "Current outstanding udhaar: " + money(debt), "Current stock cost value: " + money(stock_value),
        "", "DAILY SALES"
    ]
    daily = {}
    for s in sales:
        d = str(s.get("date",""))[:10] or "Date unavailable"
        daily[d] = daily.get(d,0) + float(s.get("total",0) or 0)
    lines += [d + " | Sales: " + money(daily[d]) for d in sorted(daily)] or ["No sales recorded for this month."]
    lines += ["", "SALES DETAILS"]
    lines += ["%s | %s | Qty %s | %s | %s" % (str(s.get("date",""))[:10], s.get("product_name","Item"), s.get("quantity",0), s.get("payment","cash"), money(s.get("total",0))) for s in sales] or ["No sales."]
    lines += ["", "EXPENSE DETAILS"]
    lines += ["%s | %s | %s" % (str(e.get("date",""))[:10], e.get("note","Expense"), money(e.get("amount",0))) for e in expenses] or ["No expenses."]
    lines += ["", "PAYMENT / UDHAR RECOVERY DETAILS"]
    lines += ["%s | %s | %s" % (str(p.get("date",""))[:10], p.get("customer","Customer"), money(p.get("amount",0))) for p in payments] or ["No dated payment records available for this month."]
    lines += ["", "CURRENT STOCK SNAPSHOT"]
    lines += ["%s | Stock %s | Cost %s | Sale price %s | Stock cost value %s" % (p.get("name","Item"), p.get("stock",0), money(p.get("cost",0)), money(p.get("price",0)), money(float(p.get("stock",0) or 0)*float(p.get("cost",0) or 0))) for p in data["products"]] or ["No products."]
    lines += ["", "IMPORTANT NOTES",
        "- Sales, expenses and dated recoveries are for the selected month.",
        "- Outstanding udhaar and stock are current balances, not historical month-end balances.",
        "- Profit is estimated from recorded costs and expenses.",
        "- Old udhaar without dated payment entries cannot show exact recovery month.",
        "- PDF supports basic English characters; non-English characters may appear as question marks."]
    return make_pdf(lines, "Kiryana Assistant - Monthly Report " + month)

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kiryana Assistant</title><style>
*{box-sizing:border-box}body{font-family:Arial,sans-serif;margin:0;background:#f2f5f7;color:#18212b}header{background:#176b52;color:white;padding:16px}header h1{font-size:22px;margin:0 0 5px}header p{margin:0;font-size:13px}main{padding:12px;max-width:900px;margin:auto}.tabs{display:flex;gap:6px;overflow:auto;margin-bottom:12px}button{border:0;border-radius:8px;padding:11px 13px;background:#176b52;color:white;font-size:14px;cursor:pointer}.tab{white-space:nowrap}.tab.active{background:#102f28}.card{background:white;padding:14px;border-radius:12px;margin:10px 0;box-shadow:0 2px 8px #0000000b}h2{font-size:18px;margin:0 0 12px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px}.metric{background:#edf7f2;padding:12px;border-radius:9px}.metric small{display:block;color:#49655b}.metric strong{display:block;margin-top:5px;font-size:18px;overflow-wrap:anywhere}.row{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid #edf0f2;padding:9px 0;font-size:14px}.hidden{display:none}.note{font-size:12px;color:#586873;line-height:1.5}.primary{width:100%;margin-top:4px}input,select{width:100%;padding:11px;border:1px solid #ccd4da;border-radius:8px;margin:5px 0 10px;font-size:15px}label{font-size:13px;color:#42515b}</style></head><body>
<header><h1>Kiryana Assistant</h1><p>Mobile shop record • Data isi phone mein save hota hai</p></header><main>
<div class="tabs"><button class="tab active" onclick="tab('dashboard',this)">Dashboard</button><button class="tab" onclick="tab('products',this)">Stock</button><button class="tab" onclick="tab('sale',this)">Sale</button><button class="tab" onclick="tab('udhaar',this)">Udhaar</button><button class="tab" onclick="tab('expense',this)">Kharch</button><button class="tab" onclick="tab('reports',this)">Reports / PDF</button></div>
<section id="dashboard"><div class="card"><h2>Overall hisaab</h2><div class="grid" id="metrics"></div></div><div class="card"><h2>Stock list</h2><div id="dashstock"></div></div><div class="card"><button onclick="backupData()" class="primary">Data Backup Banayein</button><p class="note">Backup JSON file download hogi. Safe jagah rakh lein.</p></div></section>
<section id="products" class="hidden"><div class="card"><h2>Naya product</h2><label>Naam</label><input id="pname" placeholder="Cheeni"><div class="grid"><div><label>Purchase cost / unit</label><input id="pcost" type="number" min="0" step=".01"></div><div><label>Sale price / unit</label><input id="pprice" type="number" min="0" step=".01"></div><div><label>Opening stock</label><input id="pstock" type="number" min="0" step=".01"></div><div><label>Low stock alert</label><input id="plow" type="number" min="0" step=".01" value="5"></div></div><button class="primary" onclick="addProduct()">Product Save Karein</button></div><div class="card"><h2>Stock add karein</h2><label>Product</label><select id="restockProduct"></select><label>Quantity</label><input id="restockQty" type="number" min=".01" step=".01"><button class="primary" onclick="restock()">Stock Add Karein</button></div><div class="card"><h2>Products</h2><div id="productslist"></div></div></section>
<section id="sale" class="hidden"><div class="card"><h2>Sale record</h2><label>Product</label><select id="saleProduct" onchange="fillPrice()"></select><label>Quantity</label><input id="saleQty" type="number" min=".01" step=".01" value="1"><label>Sale price per unit</label><input id="salePrice" type="number" min="0" step=".01"><label>Payment type</label><select id="salePayment" onchange="toggleCustomer()"><option value="cash">Cash</option><option value="udhaar">Udhaar</option></select><div id="customerBox" class="hidden"><label>Customer name</label><input id="saleCustomer"></div><button class="primary" onclick="addSale()">Sale Save Karein</button></div></section>
<section id="udhaar" class="hidden"><div class="card"><h2>Udhaar recovery</h2><label>Customer / record</label><select id="debtSelect"></select><label>Wasooli ki raqam</label><input id="payAmount" type="number" min=".01" step=".01"><button class="primary" onclick="payDebt()">Wasooli Save Karein</button></div><div class="card"><h2>Balances</h2><div id="debtslist"></div></div></section>
<section id="expense" class="hidden"><div class="card"><h2>Kharch record</h2><label>Detail</label><input id="expenseNote" placeholder="Bijli bill"><label>Raqam</label><input id="expenseAmount" type="number" min=".01" step=".01"><button class="primary" onclick="addExpense()">Kharch Save Karein</button></div><div class="card"><h2>Recent expenses</h2><div id="expenseslist"></div></div></section>
<section id="reports" class="hidden"><div class="card"><h2>Monthly PDF report</h2><p class="note">PDF mein sales, cash/udhaar, estimated profit, expenses, recovery, daily sales aur current stock shamil hoga.</p><label>Report month</label><input id="reportMonth" type="month"><button class="primary" onclick="getPDF()">Monthly PDF Generate Karein</button></div><div class="card"><h2>Overall summary</h2><div id="summary"></div></div></section><div id="msg" class="card hidden"></div></main>
<script>
let D={products:[],sales:[],udhaar:[],expenses:[],payments:[]};function fmt(n){return 'Rs '+Number(n||0).toLocaleString('en-PK',{minimumFractionDigits:2,maximumFractionDigits:2})}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function msg(t){let e=document.getElementById('msg');e.textContent=t;e.classList.remove('hidden');setTimeout(()=>e.classList.add('hidden'),3500)}
async function api(path,obj){let r=await fetch(path,{method:obj?'POST':'GET',headers:obj?{'Content-Type':'application/json'}:{},body:obj?JSON.stringify(obj):undefined});let j=await r.json();if(!r.ok||j.error)throw Error(j.error||'Error');return j}
async function act(path,obj){try{let j=await api(path,obj);msg(j.message||'Save ho gaya');await refresh()}catch(e){msg(e.message)}}
function tab(id,b){document.querySelectorAll('main section').forEach(s=>s.classList.add('hidden'));document.getElementById(id).classList.remove('hidden');document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');render()}
async function refresh(){try{D=await api('/api/data');render()}catch(e){msg(e.message)}}
function render(){let sales=D.sales.reduce((a,x)=>a+ +x.total,0),cost=D.sales.reduce((a,x)=>a+ +x.cost_total,0),exp=D.expenses.reduce((a,x)=>a+ +x.amount,0),debt=D.udhaar.reduce((a,x)=>a+ +x.remaining,0);
document.getElementById('metrics').innerHTML=[['Total sales',sales],['Gross profit estimate',sales-cost],['Expenses',exp],['Outstanding udhaar',debt]].map(x=>`<div class="metric"><small>${x[0]}</small><strong>${fmt(x[1])}</strong></div>`).join('');
document.getElementById('dashstock').innerHTML=stockHTML();document.getElementById('productslist').innerHTML=stockHTML();
let opts=D.products.map(p=>`<option value="${p.id}">${esc(p.name)} (stock ${p.stock})</option>`).join('')||'<option value="">Pehle product add karein</option>';
['restockProduct','saleProduct'].forEach(id=>document.getElementById(id).innerHTML=opts);
document.getElementById('debtSelect').innerHTML=D.udhaar.filter(x=>+x.remaining>0).map(x=>`<option value="${x.id}">${esc(x.customer)} — ${esc(x.product_name)} — baqi ${fmt(x.remaining)}</option>`).join('')||'<option value="">Koi outstanding udhaar nahi</option>';
document.getElementById('debtslist').innerHTML=D.udhaar.filter(x=>+x.remaining>0).map(x=>`<div class="row"><span>${esc(x.customer)}<br>${esc(x.product_name)} • ${String(x.date||'').slice(0,10)}</span><strong>${fmt(x.remaining)}</strong></div>`).join('')||'Udhaar balance nahi.';
document.getElementById('expenseslist').innerHTML=[...D.expenses].reverse().slice(0,20).map(x=>`<div class="row"><span>${esc(x.note)}<br>${String(x.date||'').slice(0,10)}</span><strong>${fmt(x.amount)}</strong></div>`).join('')||'Abhi kharch record nahi.';
document.getElementById('summary').innerHTML=[['Total sales',sales],['Cost of sold goods',cost],['Gross profit estimate',sales-cost],['Expenses',exp],['Profit after expenses',sales-cost-exp],['Outstanding udhaar',debt]].map(x=>`<div class="row"><span>${x[0]}</span><strong>${fmt(x[1])}</strong></div>`).join('')}
function stockHTML(){return D.products.map(p=>`<div class="row"><span><b>${esc(p.name)}</b><br>Cost ${fmt(p.cost)} • Sale ${fmt(p.price)}</span><strong>${p.stock}${+p.stock<=+(p.low_stock||0)?' ⚠️':''}</strong></div>`).join('')||'Abhi product add nahi kiya.'}
function fillPrice(){let p=D.products.find(x=>String(x.id)===document.getElementById('saleProduct').value);document.getElementById('salePrice').value=p?p.price:''}
function toggleCustomer(){document.getElementById('customerBox').classList.toggle('hidden',document.getElementById('salePayment').value!=='udhaar')}
function addProduct(){act('/api/product',{name:pname.value,cost:pcost.value,price:pprice.value,stock:pstock.value,low_stock:plow.value})}
function restock(){act('/api/restock',{product_id:restockProduct.value,quantity:restockQty.value})}
function addSale(){act('/api/sale',{product_id:saleProduct.value,quantity:saleQty.value,unit_price:salePrice.value,payment:salePayment.value,customer:saleCustomer.value})}
function payDebt(){act('/api/payment',{debt_id:debtSelect.value,amount:payAmount.value})}
function addExpense(){act('/api/expense',{note:expenseNote.value,amount:expenseAmount.value})}
async function backupData(){try{let r=await fetch('/api/backup'),b=await r.blob(),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='kiryana_backup.json';a.click();URL.revokeObjectURL(a.href);msg('Backup download shuru ho gayi')}catch(e){msg(e.message)}}
function getPDF(){let m=document.getElementById('reportMonth').value;if(!m){msg('Pehle month select karein');return}location.href='/api/monthly-pdf?month='+encodeURIComponent(m)}
document.getElementById('reportMonth').value=new Date().toISOString().slice(0,7);refresh();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def send_json(self, obj, status=200):
        raw=json.dumps(obj,ensure_ascii=False).encode("utf-8");self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.send_header("Cache-Control","no-store");self.end_headers();self.wfile.write(raw)
    def read_json(self):
        length=int(self.headers.get("Content-Length","0"))
        if length>1000000: raise ValueError("Request bohat bari hai.")
        try: return json.loads((self.rfile.read(length) if length else b"{}").decode("utf-8"))
        except Exception: raise ValueError("Data format durust nahi.")
    def do_GET(self):
        parsed=urlparse(self.path);path=parsed.path
        try:
            if path in ("/","/index.html"):
                raw=HTML.encode("utf-8");self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
            elif path=="/api/data": self.send_json(load_data())
            elif path=="/api/backup":
                raw=json.dumps(load_data(),ensure_ascii=False,indent=2).encode("utf-8");self.send_response(200);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Disposition",'attachment; filename="kiryana_data_backup.json"');self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
            elif path=="/api/monthly-pdf":
                month=parse_qs(parsed.query).get("month",[datetime.now().strftime("%Y-%m")])[0];raw=monthly_pdf(load_data(),month)
                self.send_response(200);self.send_header("Content-Type","application/pdf");self.send_header("Content-Disposition",'attachment; filename="kiryana_monthly_report_'+month+'.pdf"');self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
            else: self.send_json({"error":"Page nahi mili."},404)
        except Exception as e: self.send_json({"error":str(e)},400)
    def do_POST(self):
        path=urlparse(self.path).path
        try:
            d=self.read_json();data=load_data();now=now_text()
            if path=="/api/product":
                name=str(d.get("name","")).strip()
                if not name: raise ValueError("Product ka naam likhein.")
                if any(str(p.get("name","")).strip().lower()==name.lower() for p in data["products"]): raise ValueError("Ye product pehle se mojood hai.")
                data["products"].append({"id":int(datetime.now().timestamp()*1000),"name":name,"cost":number(d.get("cost"),"Purchase cost"),"price":number(d.get("price"),"Sale price"),"stock":number(d.get("stock",0),"Stock"),"low_stock":number(d.get("low_stock",5),"Low stock"),"created_at":now});message="Product save ho gaya."
            elif path=="/api/restock":
                p=next((x for x in data["products"] if str(x.get("id"))==str(d.get("product_id",""))),None)
                if not p: raise ValueError("Product select karein.")
                qty=number(d.get("quantity"),"Quantity")
                if qty<=0: raise ValueError("Quantity zero se zyada honi chahiye.")
                p["stock"]=round(float(p.get("stock",0) or 0)+qty,2);message="Stock update ho gaya."
            elif path=="/api/sale":
                p=next((x for x in data["products"] if str(x.get("id"))==str(d.get("product_id",""))),None)
                if not p: raise ValueError("Product select karein.")
                qty=number(d.get("quantity"),"Quantity");price=number(d.get("unit_price",p.get("price",0)),"Sale price");payment=str(d.get("payment","cash"));customer=str(d.get("customer","")).strip()
                if qty<=0: raise ValueError("Quantity zero se zyada honi chahiye.")
                if qty>float(p.get("stock",0) or 0): raise ValueError("Stock kam hai. Pehle stock update karein.")
                if payment not in ("cash","udhaar"): raise ValueError("Payment type durust nahi.")
                if payment=="udhaar" and not customer: raise ValueError("Udhaar ke liye customer ka naam zaroori hai.")
                total=round(qty*price,2);ct=round(qty*float(p.get("cost",0) or 0),2);sid=int(datetime.now().timestamp()*1000)
                data["sales"].append({"id":sid,"product_id":p["id"],"product_name":p["name"],"quantity":qty,"unit_price":price,"total":total,"cost_total":ct,"payment":payment,"customer":customer if payment=="udhaar" else "","date":now})
                p["stock"]=round(float(p.get("stock",0) or 0)-qty,2)
                if payment=="udhaar": data["udhaar"].append({"id":sid,"customer":customer,"product_name":p["name"],"amount":total,"paid":0,"remaining":total,"date":now,"sale_id":sid})
                message="Sale save ho gayi."
            elif path=="/api/payment":
                debt=next((x for x in data["udhaar"] if str(x.get("id"))==str(d.get("debt_id",""))),None)
                if not debt: raise ValueError("Udhaar record select karein.")
                amount=number(d.get("amount"),"Raqam");remaining=float(debt.get("remaining",0) or 0)
                if amount<=0: raise ValueError("Raqam zero se zyada honi chahiye.")
                if amount>remaining: raise ValueError("Wasooli baqi udhaar se zyada nahi ho sakti.")
                debt["paid"]=round(float(debt.get("paid",0) or 0)+amount,2);debt["remaining"]=round(remaining-amount,2)
                data["payments"].append({"id":int(datetime.now().timestamp()*1000),"debt_id":debt.get("id"),"customer":debt.get("customer",""),"amount":amount,"date":now});message="Udhaar recovery save ho gayi."
            elif path=="/api/expense":
                note=str(d.get("note","")).strip();amount=number(d.get("amount"),"Raqam")
                if not note: raise ValueError("Kharch ki detail likhein.")
                if amount<=0: raise ValueError("Raqam zero se zyada honi chahiye.")
                data["expenses"].append({"id":int(datetime.now().timestamp()*1000),"note":note,"amount":amount,"date":now});message="Kharch save ho gaya."
            else: self.send_json({"error":"Endpoint nahi mila."},404);return
            save_data(data);self.send_json({"ok":True,"message":message})
        except Exception as e: self.send_json({"error":str(e)},400)

if __name__=="__main__":
    os.makedirs(BACKUP_DIR,exist_ok=True)
    print("\nKiryana Assistant ready!\nMobile browser mein kholein: http://127.0.0.1:8000\nBand karne ke liye Termux mein CTRL+C dabayein.\n")
    server=ThreadingHTTPServer((HOST,PORT),Handler)
    try: server.serve_forever()
    except KeyboardInterrupt: print("\nApp band ho gayi.");server.server_close()
